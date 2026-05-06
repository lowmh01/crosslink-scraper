import os
import requests
from supabase import create_client
from datetime import datetime, timezone

SUPABASE_URL = os.environ["SUPABASE_URL"]
SUPABASE_KEY = os.environ["SUPABASE_SERVICE_ROLE_KEY"]
HERE_API_KEY = os.environ["HERE_API_KEY"]

supabase = create_client(SUPABASE_URL, SUPABASE_KEY)

ROUTES = {
    "woodlands_jb_sg": {
        "origin": (1.4464, 103.7638),
        "destination": (1.4470, 103.7669),
    },
    "woodlands_sg_jb": {
        "origin": (1.4470, 103.7669),
        "destination": (1.4464, 103.7638),
    },
    "tuas_jb_sg": {
        "origin": (1.3502, 103.6368),
        "destination": (1.3411, 103.6367),
    },
    "tuas_sg_jb": {
        "origin": (1.3411, 103.6367),
        "destination": (1.3502, 103.6368),
    },
}


def fallback_status():
    hour = datetime.now().hour
    is_weekend = datetime.now().weekday() >= 5

    if 7 <= hour < 10:
        level = 2 if is_weekend else 4
    elif 10 <= hour < 16:
        level = 1
    elif 17 <= hour < 21:
        level = 3
    else:
        level = 1

    status_map = {
        1: {"label": "Clear", "color": "green", "delay": 0},
        2: {"label": "Moderate", "color": "amber", "delay": 20},
        3: {"label": "Heavy", "color": "red", "delay": 45},
        4: {"label": "Very Heavy", "color": "red", "delay": 65},
    }
    return {**status_map[level], "source": "fallback", "duration": None, "base": None}


def fetch_route(route_key):
    route = ROUTES[route_key]
    origin = route["origin"]
    destination = route["destination"]

    departure = datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")

    url = (
        f"https://router.hereapi.com/v8/routes"
        f"?transportMode=car"
        f"&origin={origin[0]},{origin[1]}"
        f"&destination={destination[0]},{destination[1]}"
        f"&return=summary,travelSummary"
        f"&departureTime={departure}"
        f"&apiKey={HERE_API_KEY}"
    )

    try:
        res = requests.get(url, timeout=10)
        print(f"{route_key}: HTTP {res.status_code}")

        if not res.ok:
            print(f"{route_key}: error response: {res.text}")
            return fallback_status()

        data = res.json()

        # Safe extraction — check each level explicitly
        routes_list = data.get("routes")
        if not routes_list or len(routes_list) == 0:
            print(f"{route_key}: no routes in response, using fallback")
            return fallback_status()

        sections_list = routes_list[0].get("sections")
        if not sections_list or len(sections_list) == 0:
            print(f"{route_key}: no sections in response, using fallback")
            return fallback_status()

        section = sections_list[0]
        travel = section.get("travelSummary")
        if not travel:
            print(f"{route_key}: no travelSummary, using fallback")
            return fallback_status()

        duration = travel.get("duration")
        base = travel.get("baseDuration")

        if not duration or not base or base == 0:
            print(f"{route_key}: missing duration or base, using fallback")
            return fallback_status()

        ratio = base / duration
        delay = round((duration - base) / 60)

        if ratio > 0.90:
            status_label, color = "Clear", "green"
        elif ratio > 0.65:
            status_label, color = "Moderate", "amber"
        elif ratio > 0.40:
            status_label, color = "Heavy", "red"
        else:
            status_label, color = "Very Heavy", "red"

        print(f"{route_key}: {status_label} (ratio={ratio:.2f}, duration={duration}s, base={base}s, delay={delay}min)")
        return {
            "label": status_label,
            "color": color,
            "duration": duration,
            "base": base,
            "delay": delay,
            "source": "here",
        }

    except Exception as e:
        print(f"{route_key}: exception {e}, using fallback")
        return fallback_status()


def main():
    print(
        f"Fetching checkpoint traffic at {datetime.now(timezone.utc).isoformat()}")

    rows = []
    for route_key in ROUTES:
        result = fetch_route(route_key)
        rows.append({
            "route": route_key,
            "label": result["label"],
            "color": result["color"],
            "duration": result.get("duration"),
            "base": result.get("base"),
            "delay": result.get("delay"),
            "source": result["source"],
        })

    supabase.table("checkpoint_traffic").insert(rows).execute()
    print(f"Inserted {len(rows)} rows into Supabase")
    print(f"Done at {datetime.now(timezone.utc).isoformat()}")


if __name__ == "__main__":
    main()
