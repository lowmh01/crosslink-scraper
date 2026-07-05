"""
aggregate_hourly.py
-------------------
Daily aggregation: rolls up `traffic_readings` into `traffic_hourly_summary`,
one row per (SGT day, hour, checkpoint, direction), tagged with SG/Johor
public holidays from the `public_holidays` table.

Runs once a day via GitHub Actions (triggered by cron-job.org
workflow_dispatch, same pattern as vision_pipeline.py). Default mode
aggregates YESTERDAY in SGT. Idempotent: re-running a day upserts.

Modes:
  python aggregate_hourly.py                  # yesterday (SGT)
  python aggregate_hourly.py --day 2026-07-01 # one specific SGT day
  python aggregate_hourly.py --backfill       # every day from the earliest
                                              # reading up to yesterday

Honesty rules baked in:
  * Only hours that actually have readings get a row. No fabricated cells.
  * Counts per status are stored raw; avg_score is derived but downstream
    UI must apply its own minimum-sample threshold (recommend n_total >= 3).
  * Readings with a status outside clear/moderate/heavy are skipped and
    reported in the log, never coerced.

Required environment variables (already set as GitHub Actions secrets):
  SUPABASE_URL           - e.g. https://xxxx.supabase.co
  SUPABASE_SERVICE_KEY   - service_role key
"""

import argparse
import os
import sys
from collections import defaultdict
from datetime import date, datetime, time, timedelta, timezone
from zoneinfo import ZoneInfo

import requests

SGT = ZoneInfo("Asia/Singapore")
SCORE = {"clear": 1, "moderate": 2, "heavy": 3}
PAGE_SIZE = 1000  # PostgREST pagination

SUPABASE_URL = os.environ.get("SUPABASE_URL", "").rstrip("/")
SUPABASE_KEY = os.environ.get("SUPABASE_SERVICE_KEY", "")

HEADERS = {
    "apikey": SUPABASE_KEY,
    "Authorization": f"Bearer {SUPABASE_KEY}",
    "Content-Type": "application/json",
}


def die(msg):
    print(f"FATAL: {msg}")
    sys.exit(1)


# ---------------------------------------------------------------------
# Supabase REST helpers
# ---------------------------------------------------------------------
def fetch_all(path, params):
    """GET with Range-header pagination until exhausted."""
    rows = []
    offset = 0
    while True:
        headers = dict(HEADERS)
        headers["Range-Unit"] = "items"
        headers["Range"] = f"{offset}-{offset + PAGE_SIZE - 1}"
        r = requests.get(f"{SUPABASE_URL}/rest/v1/{path}",
                         headers=headers, params=params, timeout=30)
        r.raise_for_status()
        batch = r.json()
        rows.extend(batch)
        if len(batch) < PAGE_SIZE:
            return rows
        offset += PAGE_SIZE


def upsert_summaries(rows):
    """Upsert on (day, hour, checkpoint, direction)."""
    if not rows:
        return
    headers = dict(HEADERS)
    headers["Prefer"] = "resolution=merge-duplicates,return=minimal"
    r = requests.post(
        f"{SUPABASE_URL}/rest/v1/traffic_hourly_summary"
        "?on_conflict=day,hour,checkpoint,direction",
        headers=headers, json=rows, timeout=30,
    )
    if not r.ok:
        die(f"upsert failed ({r.status_code}): {r.text[:300]}")


# ---------------------------------------------------------------------
# Holiday tagging
# ---------------------------------------------------------------------
def load_holidays():
    """Return dict: date -> list of 'SG: name' / 'MY: name' strings."""
    rows = fetch_all("public_holidays",
                     {"select": "holiday_date,region,name"})
    out = defaultdict(list)
    for row in rows:
        d = date.fromisoformat(row["holiday_date"])
        out[d].append(f"{row['region'].upper()}: {row['name']}")
    return out


def holiday_flags(day, holidays):
    is_holiday = day in holidays
    adjacent = (day - timedelta(days=1)) in holidays or \
               (day + timedelta(days=1)) in holidays
    names = "; ".join(holidays[day]) if is_holiday else None
    return is_holiday, adjacent, names


# ---------------------------------------------------------------------
# Aggregation
# ---------------------------------------------------------------------
def sgt_day_utc_bounds(day):
    """UTC ISO bounds [start, end) covering one SGT calendar day."""
    start = datetime.combine(
        day, time.min, tzinfo=SGT).astimezone(timezone.utc)
    end = start + timedelta(days=1)
    return start.isoformat(), end.isoformat()


def aggregate_day(day, holidays):
    lo, hi = sgt_day_utc_bounds(day)
    readings = fetch_all("traffic_readings", {
        "select": "checkpoint,direction,status,created_at",
        "and": f"(created_at.gte.{lo},created_at.lt.{hi})",
    })

    if not readings:
        print(f"{day}: no readings, nothing written (honest gap)")
        return 0

    buckets = defaultdict(lambda: {"clear": 0, "moderate": 0, "heavy": 0})
    skipped = 0
    for row in readings:
        status = (row.get("status") or "").strip().lower()
        if status not in SCORE:
            skipped += 1
            continue
        ts = datetime.fromisoformat(row["created_at"].replace("Z", "+00:00"))
        hour = ts.astimezone(SGT).hour
        buckets[(hour, row["checkpoint"], row["direction"])][status] += 1

    is_hol, is_adj, names = holiday_flags(day, holidays)

    out = []
    for (hour, checkpoint, direction), c in sorted(buckets.items()):
        n_total = c["clear"] + c["moderate"] + c["heavy"]
        score_sum = c["clear"] * 1 + c["moderate"] * 2 + c["heavy"] * 3
        out.append({
            "day": day.isoformat(),
            "hour": hour,
            "checkpoint": checkpoint,
            "direction": direction,
            "n_clear": c["clear"],
            "n_moderate": c["moderate"],
            "n_heavy": c["heavy"],
            "n_total": n_total,
            "avg_score": round(score_sum / n_total, 2),
            "is_holiday": is_hol,
            "is_holiday_adjacent": is_adj,
            "holiday_names": names,
            "updated_at": datetime.now(timezone.utc).isoformat(),
        })

    upsert_summaries(out)
    tag = " [HOLIDAY]" if is_hol else (" [adjacent]" if is_adj else "")
    note = f", {skipped} unparsable skipped" if skipped else ""
    print(f"{day}: {len(readings)} readings -> {len(out)} hourly rows{tag}{note}")
    return len(out)


def earliest_reading_day():
    rows = fetch_all("traffic_readings", {
        "select": "created_at", "order": "created_at.asc", "limit": "1",
    })
    if not rows:
        return None
    ts = datetime.fromisoformat(rows[0]["created_at"].replace("Z", "+00:00"))
    return ts.astimezone(SGT).date()


# ---------------------------------------------------------------------
def main():
    if not SUPABASE_URL or not SUPABASE_KEY:
        die("SUPABASE_URL / SUPABASE_SERVICE_KEY not set")

    parser = argparse.ArgumentParser()
    parser.add_argument("--day", help="aggregate one SGT day (YYYY-MM-DD)")
    parser.add_argument("--backfill", action="store_true",
                        help="aggregate every day from earliest reading to yesterday")
    args = parser.parse_args()

    yesterday = (datetime.now(SGT) - timedelta(days=1)).date()
    holidays = load_holidays()
    print(f"Loaded {sum(len(v) for v in holidays.values())} holiday entries "
          f"across {len(holidays)} dates")

    if args.backfill:
        start = earliest_reading_day()
        if start is None:
            die("no readings in traffic_readings yet")
        total = 0
        d = start
        while d <= yesterday:
            total += aggregate_day(d, holidays)
            d += timedelta(days=1)
        print(f"Backfill done: {start} -> {yesterday}, {total} rows upserted")
    elif args.day:
        aggregate_day(date.fromisoformat(args.day), holidays)
    else:
        aggregate_day(yesterday, holidays)


if __name__ == "__main__":
    main()
