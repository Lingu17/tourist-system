import logging
import os
import re
from concurrent.futures import ThreadPoolExecutor

import requests
from flask import Flask, render_template, request, jsonify


# Log to stderr so messages show up in Render's logs right away
# (print() output is buffered under Gunicorn).
logging.basicConfig(level=logging.INFO, format="[%(levelname)s] %(message)s")
log = logging.getLogger("tourist-system")


# ----------------- AGENT CLASSES (your logic) -----------------


class GeocodingError(Exception):
    pass


def client_headers(email: str):
    # Nominatim, Overpass, MET Norway and Wikipedia block or throttle clients
    # without a descriptive User-Agent (Overpass answers 406 Not Acceptable).
    return {
        "User-Agent": (
            "tourism-multi-agent-demo/1.0 "
            f"(+https://github.com/Lingu17/tourist-system; {email})"
        )
    }


def describe_error(e: Exception) -> str:
    # Include the HTTP status and the start of the body, so the logs say *why*
    # an API refused us (rate limit, blocked IP, server at capacity, ...).
    resp = getattr(e, "response", None)
    if resp is None:
        return repr(e)
    body = " ".join(resp.text.split())[:200]
    return f"HTTP {resp.status_code} from {resp.url}: {body}"


class WeatherAgent:
    def __init__(self, email: str):
        self.base_url = "https://api.open-meteo.com/v1/forecast"
        # Used when Open-Meteo is unreachable or rate-limits the server's IP.
        self.fallback_url = "https://api.met.no/weatherapi/locationforecast/2.0/compact"
        self.headers = client_headers(email)

    def get_weather(self, lat: float, lon: float):
        try:
            return self._open_meteo(lat, lon)
        except (requests.RequestException, ValueError) as e:
            log.error("Open-Meteo weather failed: %s", describe_error(e))

        try:
            return self._met_norway(lat, lon), None
        except (requests.RequestException, ValueError, KeyError, IndexError) as e:
            log.error("MET Norway weather failed: %s", describe_error(e))
            return None, None

    def _open_meteo(self, lat: float, lon: float):
        params = {
            "latitude": lat,
            "longitude": lon,
            "current_weather": True,
            "hourly": "precipitation_probability",
            "forecast_days": 1,
            "timezone": "auto",
        }

        resp = requests.get(
            self.base_url, params=params, headers=self.headers, timeout=(4, 6)
        )
        resp.raise_for_status()
        data = resp.json()

        current_temp = data.get("current_weather", {}).get("temperature")
        if current_temp is None:
            raise ValueError("Open-Meteo response has no current temperature")

        precip_list = data.get("hourly", {}).get("precipitation_probability", [])
        precip_list = [p for p in precip_list if p is not None]
        rain_chance = max(precip_list) if precip_list else None

        return current_temp, rain_chance

    def _met_norway(self, lat: float, lon: float):
        # MET Norway asks for at most 4 decimals; it has no rain probability,
        # so only the temperature is returned.
        params = {"lat": round(lat, 4), "lon": round(lon, 4)}
        resp = requests.get(
            self.fallback_url, params=params, headers=self.headers, timeout=(4, 6)
        )
        resp.raise_for_status()
        now = resp.json()["properties"]["timeseries"][0]
        return now["data"]["instant"]["details"]["air_temperature"]


class PlacesAgent:
    # Words in a Wikipedia article's short description that mark a place worth visiting.
    SIGHT_WORDS = re.compile(
        r"\b(palace|museum|gallery|zoo|park|garden|lake|fort|fortress|castle|"
        r"church|cathedral|basilica|temple|mosque|monastery|shrine|tomb|"
        r"mausoleum|monument|memorial|tower|statue|square|bridge|sanctuary|"
        r"beach|waterfall|falls|hill|viewpoint|aquarium|landmark|attraction)s?\b"
    )

    def __init__(self, email: str):
        # The main Overpass server is often at capacity, so a mirror is tried next.
        self.overpass_urls = [
            "https://overpass-api.de/api/interpreter",
            "https://overpass.private.coffee/api/interpreter",
        ]
        self.wiki_url = "https://en.wikipedia.org/w/api.php"
        self.headers = client_headers(email)

    def get_places(self, lat: float, lon: float, limit: int = 5):
        for url in self.overpass_urls:
            try:
                names = self._overpass(url, lat, lon, limit)
            except (requests.RequestException, ValueError) as e:
                log.error("Overpass places failed: %s", describe_error(e))
                continue
            if names:
                return names
            break  # the server answered; a mirror would have the same data

        # Every Overpass server failed or found nothing: use Wikipedia's
        # articles about landmarks near the coordinates instead.
        try:
            return self._wikipedia(lat, lon, limit)
        except (requests.RequestException, ValueError, KeyError) as e:
            log.error("Wikipedia places failed: %s", describe_error(e))
            return []

    def _overpass(self, url: str, lat: float, lon: float, limit: int):
        radius = 5000  # meters

        query = f"""
        [out:json][timeout:10];
        (
          node["tourism"="attraction"](around:{radius},{lat},{lon});
          node["amenity"="park"](around:{radius},{lat},{lon});
          node["leisure"="park"](around:{radius},{lat},{lon});
        );
        out center;
        """

        resp = requests.post(
            url, data={"data": query}, headers=self.headers, timeout=(4, 8)
        )
        resp.raise_for_status()
        data = resp.json()

        names = []
        for el in data.get("elements", []):
            name = el.get("tags", {}).get("name")
            if name and name not in names:
                names.append(name)
            if len(names) >= limit:
                break

        return names

    def _wikipedia(self, lat: float, lon: float, limit: int):
        params = {
            "action": "query",
            "format": "json",
            "formatversion": 2,
            "generator": "geosearch",
            "ggscoord": f"{lat}|{lon}",
            "ggsradius": 5000,
            "ggslimit": 50,
            "prop": "description|coordinates",
            "codistancefrompoint": f"{lat}|{lon}",
        }

        resp = requests.get(
            self.wiki_url, params=params, headers=self.headers, timeout=(4, 6)
        )
        resp.raise_for_status()
        pages = resp.json().get("query", {}).get("pages", [])

        def distance(page):
            coords = page.get("coordinates") or [{}]
            return coords[0].get("dist", float("inf"))

        names = []
        for page in sorted(pages, key=distance):
            description = (page.get("description") or "").lower()
            if self.SIGHT_WORDS.search(description):
                names.append(page["title"])
            if len(names) >= limit:
                break

        return names


class GeocodingAgent:
    def __init__(self, email: str):
        self.base_url = "https://nominatim.openstreetmap.org/search"
        self.headers = client_headers(email)

    def geocode(self, place: str):
        params = {"q": place, "format": "json", "limit": 1}

        log.info("Geocoding query: %s", params["q"])

        try:
            resp = requests.get(
                self.base_url, params=params, headers=self.headers, timeout=10
            )
            resp.raise_for_status()
        except requests.RequestException as e:
            log.error("Geocoding API error: %s", describe_error(e))
            raise GeocodingError("Geocoding request failed")

        results = resp.json()
        if not results:
            log.info("No geocoding results returned from Nominatim.")
            raise GeocodingError("Place not found")

        first = results[0]
        lat = float(first["lat"])
        lon = float(first["lon"])
        display_name = first.get("display_name", place)
        short_name = display_name.split(",")[0]
        return lat, lon, short_name


class TourismAgent:
    def __init__(self, email: str):
        self.geo_agent = GeocodingAgent(email=email)
        self.weather_agent = WeatherAgent(email=email)
        self.places_agent = PlacesAgent(email=email)

    def handle_request(self, user_input: str):
        text = user_input.lower()

        # --- INTENT DETECTION ---
        wants_weather = ("temperature" in text) or ("weather" in text)
        wants_places = any(
            w in text for w in ["place", "places", "visit", "attraction", "sightseeing"]
        )

        # If user only writes city name (no weather & no places keywords), default to places
        if not wants_weather and not wants_places:
            wants_places = True

        # --- EXTRACT PLACE ---
        place = self._extract_place(user_input)
        if not place:
            return "Please tell me which place you want to go."

        try:
            lat, lon, display_name = self.geo_agent.geocode(place)
        except GeocodingError as e:
            log.info("Geocoding error: %s", e)
            return "I don't know this place exist."

        # Weather and places come from different services, so ask both at once.
        with ThreadPoolExecutor(max_workers=2) as pool:
            weather_job = (
                pool.submit(self.weather_agent.get_weather, lat, lon)
                if wants_weather
                else None
            )
            places_job = (
                pool.submit(self.places_agent.get_places, lat, lon, 5)
                if wants_places
                else None
            )

        # ---------- LOGIC BRANCHES ----------

        # 1️⃣ WEATHER ONLY
        if wants_weather and not wants_places:
            temp, rain_chance = weather_job.result()

            if temp is not None and rain_chance is not None:
                return (
                    f"In {display_name} it's currently {temp}°C with a chance of "
                    f"{rain_chance}% to rain."
                )
            elif temp is not None:
                return f"In {display_name} it's currently {temp}°C."
            else:
                return f"Sorry, I couldn't get the weather for {display_name}."

        parts = []

        # 2️⃣ WEATHER + PLACES
        if wants_weather:
            temp, rain_chance = weather_job.result()
            if temp is not None and rain_chance is not None:
                parts.append(
                    f"In {display_name} it's currently {temp}°C "
                    f"with a chance of {rain_chance}% to rain."
                )
            elif temp is not None:
                parts.append(f"In {display_name} it's currently {temp}°C.")
            else:
                parts.append(
                    f"Sorry, I couldn't get the weather for {display_name}."
                )

        # 3️⃣ PLACES (ONLY or in addition to weather)
        if wants_places:
            places = places_job.result()

            if places:
                prefix = (
                    "And these are the places you can go:"
                    if wants_weather
                    else f"In {display_name} these are the places you can go,"
                )
                parts.append(prefix + "\n" + "\n".join(places))
            else:
                parts.append(
                    f"Sorry, I couldn't find tourist places near {display_name}."
                )

        return " ".join(parts)

    def _extract_place(self, user_input: str) -> str:
        text = user_input.strip()
        lower = text.lower()

        if " to " in lower:
            idx = lower.rfind(" to ")
            candidate = text[idx + 4 :].strip()
        else:
            candidate = text

        for sep in [",", "?", ".", " and ", " what "]:
            lower_cand = candidate.lower()
            if sep in lower_cand:
                candidate = candidate[: lower_cand.index(sep)].strip()
                break

        stop_words = {
            "i",
            "im",
            "i'm",
            "going",
            "go",
            "to",
            "what",
            "is",
            "the",
            "there",
            "and",
            "visit",
            "places",
            "place",
            "temperature",
            "weather",
            "trip",
            "in",
            "city",
            "are",
            "can",
            "you",
            "my",
            "let's",
            "lets",
        }
        words = candidate.split()
        filtered = [w for w in words if w.lower() not in stop_words]

        if filtered:
            candidate = " ".join(filtered)
        else:
            candidate = " ".join(words[-2:])

        if len(candidate.split()) > 3:
            candidate = " ".join(candidate.split()[-2:])

        return candidate


# ----------------- FLASK APP -----------------


# Make sure you have:
# backend/
#   app.py
#   templates/index.html
#   static/style.css  (or static/css/style.css)
app = Flask(__name__, template_folder="templates", static_folder="static")

# Contact address sent to the public APIs; set CONTACT_EMAIL to use your own.
agent = TourismAgent(
    email=os.environ.get("CONTACT_EMAIL", "lingrajmalipatil1@gmail.com")
)


@app.route("/")
def index():
    # index.html must be in backend/templates/
    return render_template("index.html")


@app.route("/api/query", methods=["POST"])
def api_query():
    try:
        data = request.get_json(force=True) or {}
    except Exception as e:
        log.error("JSON parsing error: %s", e)
        return jsonify({"reply": "Invalid request format."}), 400

    message = (data.get("message") or "").strip()
    if not message:
        return jsonify({"reply": "Please enter your question or trip plan."}), 400

    try:
        reply = agent.handle_request(message)
    except Exception as e:
        log.exception("While handling request: %s", e)
        reply = "Something went wrong while planning your trip. Please try again."

    return jsonify({"reply": reply})


if __name__ == "__main__":
    # Access at http://127.0.0.1:5000/
    app.run(host="127.0.0.1", port=5000, debug=True)
