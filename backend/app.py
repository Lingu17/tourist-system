import requests
from flask import Flask, render_template, request, jsonify

# ----------------- AGENT CLASSES (your logic) -----------------

class GeocodingError(Exception):
    pass


class WeatherAgent:
    def __init__(self):
        self.base_url = "https://api.open-meteo.com/v1/forecast"

    def get_weather(self, lat: float, lon: float):
        params = {
            "latitude": lat,
            "longitude": lon,
            "current_weather": True,
            "hourly": "precipitation_probability",
            "forecast_days": 1,
            "timezone": "auto",
        }
        resp = requests.get(self.base_url, params=params, timeout=10)
        resp.raise_for_status()
        data = resp.json()

        current_temp = data.get("current_weather", {}).get("temperature")
        precip_list = data.get("hourly", {}).get("precipitation_probability", [])
        rain_chance = max(precip_list) if precip_list else None

        return current_temp, rain_chance


class PlacesAgent:
    def __init__(self):
        self.base_url = "https://overpass-api.de/api/interpreter"

    def get_places(self, lat: float, lon: float, limit: int = 5):
        radius = 5000  # meters

        query = f"""
        [out:json][timeout:25];
        (
          node["tourism"="attraction"](around:{radius},{lat},{lon});
          node["amenity"="park"](around:{radius},{lat},{lon});
          node["leisure"="park"](around:{radius},{lat},{lon});
        );
        out center;
        """

        resp = requests.post(self.base_url, data={"data": query}, timeout=30)
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


class GeocodingAgent:
    def __init__(self, email: str):
        self.base_url = "https://nominatim.openstreetmap.org/search"
        self.headers = {"User-Agent": f"tourism-multi-agent-demo/1.0 ({email})"}

    def geocode(self, place: str):
        params = {"q": place, "format": "json", "limit": 1}

        print(f"[DEBUG] Geocoding query: {params['q']}")  # debug

        resp = requests.get(self.base_url, params=params, headers=self.headers, timeout=10)
        resp.raise_for_status()
        results = resp.json()
        if not results:
            print("[DEBUG] No geocoding results returned from Nominatim.")
            raise GeocodingError("Place not found")

        first = results[0]
        lat = float(first["lat"])
        lon = float(first["lon"])
        display_name = first.get("display_name", place)
        # Use just the first part like "Bengaluru" or "Paris"
        short_name = display_name.split(",")[0]
        return lat, lon, short_name


class TourismAgent:
    def __init__(self, email: str):
        self.geo_agent = GeocodingAgent(email=email)
        self.weather_agent = WeatherAgent()
        self.places_agent = PlacesAgent()

    def handle_request(self, user_input: str):
        text = user_input.lower()

        # --- INTENT DETECTION ---
        wants_weather = ("temperature" in text) or ("weather" in text)
        wants_places = any(w in text for w in ["place", "places", "visit", "attraction", "sightseeing"])

        # If user only writes city name (no weather & no places keywords), default to places
        if not wants_weather and not wants_places:
            wants_places = True

        # --- EXTRACT PLACE ---
        place = self._extract_place(user_input)
        if not place:
            return "Please tell me which place you want to go."

        try:
            lat, lon, display_name = self.geo_agent.geocode(place)
        except GeocodingError:
            return "I don't know this place exist."

        # ---------- LOGIC BRANCHES ----------

        # 1️⃣ WEATHER ONLY
        if wants_weather and not wants_places:
            temp, rain_chance = self.weather_agent.get_weather(lat, lon)

            if temp is not None and rain_chance is not None:
                return f"In {display_name} it's currently {temp}°C with a chance of {rain_chance}% to rain."
            elif temp is not None:
                return f"In {display_name} it's currently {temp}°C."
            else:
                return f"Sorry, I couldn't get the weather for {display_name}."

        parts = []

        # 2️⃣ WEATHER + PLACES
        if wants_weather:
            temp, rain_chance = self.weather_agent.get_weather(lat, lon)
            parts.append(
                f"In {display_name} it's currently {temp}°C with a chance of {rain_chance}% to rain."
            )

        # 3️⃣ PLACES (ONLY or in addition to weather)
        if wants_places:
            places = self.places_agent.get_places(lat, lon, limit=5)

            if places:
                prefix = (
                    "And these are the places you can go:"
                    if wants_weather
                    else f"In {display_name} these are the places you can go,"
                )
                parts.append(prefix + "\n" + "\n".join(places))
            else:
                parts.append(f"Sorry, I couldn't find tourist places near {display_name}.")

        return " ".join(parts)

    def _extract_place(self, user_input: str) -> str:
        text = user_input.strip()
        lower = text.lower()

        if " to " in lower:
            idx = lower.rfind(" to ")
            candidate = text[idx + 4:].strip()
        else:
            candidate = text

        for sep in [",", "?", ".", " and ", " what "]:
            lower_cand = candidate.lower()
            if sep in lower_cand:
                candidate = candidate[:lower_cand.index(sep)].strip()
                break

        stop_words = {
            "i", "im", "i'm", "going", "go", "to", "what", "is", "the",
            "there", "and", "visit", "places", "place", "temperature",
            "weather", "trip", "in", "city", "are", "can", "you",
            "my", "let's", "lets"
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

app = Flask(__name__)
agent = TourismAgent(email="praveenbalure786@gmail.com")  # your email


@app.route("/")
def index():
    return render_template("index.html")


@app.route("/api/query", methods=["POST"])
def api_query():
    data = request.get_json() or {}
    message = data.get("message", "").strip()
    if not message:
        return jsonify({"reply": "Please enter your question or trip plan."}), 400
    reply = agent.handle_request(message)
    return jsonify({"reply": reply})


if __name__ == "__main__":
    app.run(host="127.0.0.1", port=5000, debug=True)
