"""
scrape_ticketmaster_events.py
Fetches Singapore events from Ticketmaster Discovery API into Supabase `events`.

Behaviour:
  - New events insert with is_active = false (manual review, same as news flow).
  - Existing events: only fact fields are refreshed (title, dates, status, url,
    image). Editorial fields (is_active, crossing_impact, editor_note) are
    NEVER touched after the initial insert.
  - crossing_impact is only a suggestion at insert time, derived from venue.

Env vars: TM_API_KEY, SUPABASE_URL, SUPABASE_SERVICE_KEY
Quota: 1 API call per 200 events -> a daily run uses ~1-2 of the 5000/day quota.
"""

import os
import sys
import time
import requests

TM_API_KEY = os.environ["TM_API_KEY"]
SUPABASE_URL = os.environ["SUPABASE_URL"].rstrip("/")
SUPABASE_KEY = os.environ["SUPABASE_SERVICE_KEY"]

TM_BASE = "https://app.ticketmaster.com/discovery/v2/events.json"
HEADERS = {
    "apikey": SUPABASE_KEY,
    "Authorization": f"Bearer {SUPABASE_KEY}",
    "Content-Type": "application/json",
}

# Venue -> suggested crossing impact at insert time (admin can override).
# National Stadium ~55k capacity: direct causeway pressure on event nights.
# Indoor Stadium ~12k: noticeable. Theatres ~5k: negligible for checkpoints.
VENUE_IMPACT = {
    "national stadium": "heavy",
    "singapore indoor stadium": "moderate",
    "singapore national stadium": "heavy",
}


def suggest_impact(venue_name):
    if not venue_name:
        return "none"
    return VENUE_IMPACT.get(venue_name.strip().lower(), "none")


def pick_image(images):
    """Prefer a 16:9 image around 1024px wide; fall back to the largest."""
    if not images:
        return None
    wide = [i for i in images if i.get("ratio") == "16_9" and i.get("url")]
    pool = wide or [i for i in images if i.get("url")]
    if not pool:
        return None
    pool.sort(key=lambda i: abs(i.get("width", 0) - 1024))
    return pool[0]["url"]


def fetch_tm_events():
    """Fetch all SG events, paginated. Discovery API caps size*page at 1000."""
    events, page = [], 0
    while True:
        r = requests.get(TM_BASE, params={
            "countryCode": "SG",
            "size": 200,
            "page": page,
            "sort": "date,asc",
            "apikey": TM_API_KEY,
        }, timeout=30)
        if r.status_code == 429:
            time.sleep(5)
            continue
        r.raise_for_status()
        data = r.json()
        events.extend(data.get("_embedded", {}).get("events", []))
        total_pages = data.get("page", {}).get("totalPages", 0)
        page += 1
        if page >= total_pages or page * 200 >= 1000:
            break
    return events


def to_row(e):
    venue = None
    venues = e.get("_embedded", {}).get("venues") or []
    if venues:
        venue = venues[0].get("name")

    start = e.get("dates", {}).get("start", {})
    local_date = start.get("localDate")
    if not local_date:
        return None  # honesty rule: no fabricated dates, skip TBA events

    cls = (e.get("classifications") or [{}])[0]
    category = (cls.get("segment") or {}).get("name")

    sales = e.get("sales", {}).get("public", {})

    return {
        "source": "ticketmaster",
        "source_event_id": e["id"],
        "title": e.get("name", "").strip(),
        "venue": venue,
        "start_date": local_date,
        "start_time": start.get("localTime"),
        "end_date": None,
        "category": category,
        "status": (e.get("dates", {}).get("status") or {}).get("code", "onsale"),
        "url": e.get("url"),
        "image_url": pick_image(e.get("images")),
        "onsale_date": sales.get("startDateTime"),
    }


def get_existing_ids():
    """Fetch source_event_ids already in the table for this source."""
    ids, offset = set(), 0
    while True:
        r = requests.get(
            f"{SUPABASE_URL}/rest/v1/events",
            headers=HEADERS,
            params={
                "select": "source_event_id",
                "source": "eq.ticketmaster",
                "limit": 1000,
                "offset": offset,
            },
            timeout=30,
        )
        r.raise_for_status()
        batch = r.json()
        ids.update(row["source_event_id"] for row in batch)
        if len(batch) < 1000:
            break
        offset += 1000
    return ids


def insert_new(rows):
    """New events: include editorial defaults (is_active false, suggested impact)."""
    payload = []
    for row in rows:
        payload.append({
            **row,
            "is_active": False,
            "crossing_impact": suggest_impact(row["venue"]),
        })
    r = requests.post(
        f"{SUPABASE_URL}/rest/v1/events",
        headers={**HEADERS, "Prefer": "return=minimal"},
        json=payload,
        timeout=30,
    )
    r.raise_for_status()


def update_existing(row):
    """Existing events: patch fact fields only. Editorial columns untouched."""
    r = requests.patch(
        f"{SUPABASE_URL}/rest/v1/events",
        headers={**HEADERS, "Prefer": "return=minimal"},
        params={
            "source": "eq.ticketmaster",
            "source_event_id": f"eq.{row['source_event_id']}",
        },
        json={
            "title": row["title"],
            "venue": row["venue"],
            "start_date": row["start_date"],
            "start_time": row["start_time"],
            "category": row["category"],
            "status": row["status"],
            "url": row["url"],
            "image_url": row["image_url"],
            "onsale_date": row["onsale_date"],
            "updated_at": "now()",
        },
        timeout=30,
    )
    r.raise_for_status()


def main():
    raw = fetch_tm_events()
    rows = [r for r in (to_row(e) for e in raw) if r]
    print(f"Fetched {len(raw)} events, {len(rows)} usable (dated).")

    existing = get_existing_ids()
    new_rows = [r for r in rows if r["source_event_id"] not in existing]
    old_rows = [r for r in rows if r["source_event_id"] in existing]

    if new_rows:
        insert_new(new_rows)
    for row in old_rows:
        update_existing(row)

    print(
        f"Inserted {len(new_rows)} new (pending review), refreshed {len(old_rows)}.")


if __name__ == "__main__":
    try:
        main()
    except Exception as exc:
        print(f"FAILED: {exc}", file=sys.stderr)
        sys.exit(1)
