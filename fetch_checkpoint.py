"""
fetch_checkpoint.py
Fetches LTA DataMall EstTravelTimes and writes to Supabase checkpoint_traffic table.

Only SG→JB direction is available from LTA (JB→SG queues form on the Malaysian side).

Environment variables required:
    LTA_API_KEY        - LTA DataMall API key
    SUPABASE_URL       - Supabase project URL
    SUPABASE_KEY       - Supabase service role key
"""

import os
import requests
from datetime import datetime, timezone

LTA_URL = "http://datamall2.mytransport.sg/ltaodataservice/EstTravelTimes"

# Map LTA segments to our route keys
# These are the segment names from EstTravelTimes that cover the causeway/second link
# You may need to adjust these after checking what LTA returns for your area
SEGMENT_MAP = {
    "woodlands_sg_jb": {
        "keywords": ["woodlands", "causeway", "bke"],
        "name": "Woodlands Causeway"
    },
    "tuas_sg_jb": {
        "keywords": ["tuas", "second link", "aye"],
        "name": "Tuas Second Link"
    },
}


def fetch_lta(api_key):
    """Fetch EstTravelTimes from LTA DataMall."""
    headers = {"AccountKey": api_key, "accept": "application/json"}
    resp = requests.get(LTA_URL, headers=headers, timeout=30)
    resp.raise_for_status()
    data = resp.json()
    return data.get("value", [])


def match_segment(segment, route_config):
    """Check if a segment matches a route based on keywords."""
    name = (segment.get("Name", "") or "").lower()
    for kw in route_config["keywords"]:
        if kw in name:
            return True
    return False


def parse_segments(segments):
    """Parse LTA segments and map to our route keys."""
    results = {}

    # Debug: print all segment names so you can identify the right ones
    print("--- All LTA segments ---")
    for seg in segments:
        name = seg.get("Name", "")
        est = seg.get("EstTime", 0)
        print(f"  {name}: {est}s")
    print("------------------------")

    for route_key, config in SEGMENT_MAP.items():
        matched = [s for s in segments if match_segment(s, config)]
        if matched:
            # Take the segment with the longest travel time (most relevant)
            seg = max(matched, key=lambda s: s.get("EstTime", 0))
            est_time = seg.get("EstTime", 0)  # in seconds
            # EstTime is total travel time; we estimate delay vs baseline
            # Baseline ~5 min (300s) for Woodlands, ~4 min (240s) for Tuas in free flow
            base = 300 if "woodlands" in route_key else 240
            delay_sec = max(0, est_time - base)
            delay_min = round(delay_sec / 60)

            results[route_key] = {
                "delay": delay_min,
                "duration": est_time,
                "base": base,
            }
            print(
                f"✓ {route_key}: {est_time}s total, {delay_min}min delay (base={base}s)")
        else:
            print(f"✗ {route_key}: no matching segment found")

    return results


def write_to_supabase(results, supabase_url, supabase_key):
    """Write results to Supabase checkpoint_traffic table."""
    headers = {
        "apikey": supabase_key,
        "Authorization": f"Bearer {supabase_key}",
        "Content-Type": "application/json",
        "Prefer": "return=minimal",
    }

    now = datetime.now(timezone.utc).isoformat()

    for route_key, data in results.items():
        row = {
            "route": route_key,
            "delay": data["delay"],
            "duration": data["duration"],
            "base": data["base"],
            "source": "lta",
            "recorded_at": now,
        }
        resp = requests.post(
            f"{supabase_url}/rest/v1/checkpoint_traffic",
            headers=headers,
            json=row,
            timeout=15,
        )
        if resp.status_code in (200, 201):
            print(f"  → Saved {route_key}")
        else:
            print(f"  ✗ Failed {route_key}: {resp.status_code} {resp.text}")


def main():
    api_key = os.environ.get("LTA_API_KEY")
    supabase_url = os.environ.get("SUPABASE_URL")
    supabase_key = os.environ.get("SUPABASE_KEY")

    if not api_key:
        print("ERROR: LTA_API_KEY not set")
        return
    if not supabase_url or not supabase_key:
        print("ERROR: SUPABASE_URL or SUPABASE_KEY not set")
        return

    print(
        f"Fetching LTA EstTravelTimes at {datetime.now(timezone.utc).isoformat()}")
    segments = fetch_lta(api_key)
    print(f"Got {len(segments)} segments")

    if not segments:
        print("No segments returned, exiting")
        return

    results = parse_segments(segments)

    if not results:
        print("No matching segments found, exiting")
        return

    print("Writing to Supabase...")
    write_to_supabase(results, supabase_url, supabase_key)
    print("Done!")


if __name__ == "__main__":
    main()
