"""
fetch_news.py
Fetches JB-SG corridor news from RSS feeds, filters by relevance,
auto-assigns tags and location, deduplicates, and inserts into
explore_items (type='news') in Supabase.

Does NOT need Playwright — pure HTTP + feedparser. Lightweight.

Environment variables:
    SUPABASE_URL  - Supabase project URL
    SUPABASE_KEY  - Supabase service role key
"""

import os
import re
import html
from datetime import datetime, timezone, timedelta
from email.utils import parsedate_to_datetime

import feedparser
import requests


SUPABASE_URL = os.environ["SUPABASE_URL"]
SUPABASE_KEY = os.environ["SUPABASE_KEY"]

# ---------------------------------------------------------------------------
# RSS SOURCES
# ---------------------------------------------------------------------------
RSS_FEEDS = [
    # Google News — broad corridor search
    {
        "name": "Google News (MY-SG corridor)",
        "url": "https://news.google.com/rss/search?q=%22johor+singapore%22+OR+%22causeway+checkpoint%22+OR+%22woodlands+checkpoint%22+OR+%22tuas+checkpoint%22+OR+%22second+link+tuas%22+OR+%22VEP+malaysia%22+OR+%22VEP+singapore%22&hl=en&gl=SG&ceid=SG:en",
        "needs_filter": True,
    },
    {
        "name": "Google News (RTS Link)",
        "url": "https://news.google.com/rss/search?q=%22RTS+Link%22+OR+%22johor+singapore+rail%22&hl=en&gl=SG&ceid=SG:en",
        "needs_filter": False,
    },
    # Work passes, employment, cross-border work
    {
        "name": "Google News (MY-SG work)",
        "url": "https://news.google.com/rss/search?q=%28%22work+pass%22+OR+%22employment+pass%22+OR+%22s+pass%22+OR+%22work+permit%22%29+%28singapore+malaysia+OR+johor%29&hl=en&gl=SG&ceid=SG:en",
        "needs_filter": True,
    },
    # Passport, immigration policy
    {
        "name": "Google News (passport & immigration)",
        "url": "https://news.google.com/rss/search?q=%28%22malaysia+passport%22+OR+%22autogate%22+OR+%22ICA+checkpoint%22+OR+%22immigration+johor%22+OR+%22immigration+singapore%22%29&hl=en&gl=SG&ceid=SG:en",
        "needs_filter": True,
    },
    # Causeway accidents, traffic incidents
    {
        "name": "Google News (causeway incidents)",
        "url": "https://news.google.com/rss/search?q=%28accident+OR+crash+OR+jam+OR+congestion%29+%28causeway+OR+%22tuas+checkpoint%22+OR+%22second+link%22%29&hl=en&gl=SG&ceid=SG:en",
        "needs_filter": False,
    },
    # Cross-border bus & transport
    {
        "name": "Google News (cross-border transport)",
        "url": "https://news.google.com/rss/search?q=%28%22cross+border+bus%22+OR+%22causeway+link%22+OR+%22transtar%22+OR+%22170X%22+OR+%22CW1%22+OR+%22shuttle+bus%22%29+%28johor+OR+singapore+OR+causeway%29&hl=en&gl=SG&ceid=SG:en",
        "needs_filter": False,
    },
]

# ---------------------------------------------------------------------------
# RELEVANCE KEYWORDS — article must contain at least one to pass filter
# ---------------------------------------------------------------------------
RELEVANCE_KEYWORDS = [
    # --- Border crossings ---
    "causeway", "second link",
    "woodlands checkpoint", "tuas checkpoint",
    # --- Cross-border transport ---
    "rts link", "ktm shuttle",
    "bukit chagar", "woodlands north",
    "transtar", "causeway link",
    # --- VEP ---
    "vep", "vehicle entry permit", "autopass",
    # --- Geographic anchors ---
    "johor singapore", "singapore johor",
    "johor-singapore", "singapore-johor",
    "malaysia singapore", "singapore malaysia",
    "jb-sg", "jb sg", "jb to sg", "sg to jb",
    # --- Currency pair ---
    "sgd myr", "myr sgd", "sgd to myr", "myr to sgd",
    # --- Work & employment (cross-border) ---
    "work pass", "employment pass", "s pass", "work permit",
    "ep application", "mom singapore", "foreign worker",
    "cpf contribution", "levy", "compass framework",
    # --- Passport & immigration ---
    "malaysia passport", "passport renewal", "autogate",
    "ica checkpoint", "immigration clearance",
    "mydigital id", "mysejahtera",
    # --- Accidents & incidents on corridor ---
    "causeway accident", "causeway crash", "causeway jam",
    "tuas accident", "second link accident",
    "causeway congestion", "checkpoint queue",
    # --- Cross-border bus & transport ---
    "cross border bus", "cross-border bus",
    "causeway link", "transtar", "handal indah",
    "170x", "cw1", "cw2", "cw3", "cw4", "cw5", "cw6", "cw7",
    "sbs transit 170", "shuttle bus causeway",
    "bus fare causeway", "bus route johor",
    "grab cross border", "taxi causeway",
]

_kw_patterns = [re.compile(re.escape(kw), re.IGNORECASE)
                for kw in RELEVANCE_KEYWORDS]

# ---------------------------------------------------------------------------
# EXCLUSION KEYWORDS — reject only crime/vice, not traffic incidents
# ---------------------------------------------------------------------------
EXCLUDE_KEYWORDS = [
    # Drug crime
    "heroin", "methamphetamine", "cocaine", "cannabis",
    # Smuggling / contraband
    "smuggl", "contraband", "duty-unpaid",
    # Violent crime
    "murder", "assault", "robbery", "theft",
    "jailed", "sentenced", "arrested", "charged", "prison",
    # Vice / noise
    "casino", "gambling", "scam",
]

_exclude_patterns = [re.compile(re.escape(kw), re.IGNORECASE)
                     for kw in EXCLUDE_KEYWORDS]


def is_relevant(title, description):
    """Return True if relevant AND not excluded."""
    text = f"{title} {description}"
    if not any(p.search(text) for p in _kw_patterns):
        return False
    if any(p.search(text) for p in _exclude_patterns):
        return False
    return True


# ---------------------------------------------------------------------------
# RSS PARSING
# ---------------------------------------------------------------------------
def parse_published(entry):
    """Extract publish datetime from an RSS entry, fall back to now."""
    for field in ("published", "updated"):
        raw = entry.get(field)
        if raw:
            try:
                return parsedate_to_datetime(raw).astimezone(timezone.utc)
            except Exception:
                pass
    return datetime.now(timezone.utc)


def extract_image(entry):
    """Try to find an image URL from RSS entry metadata."""
    for media in entry.get("media_content", []):
        url = media.get("url", "")
        if url and ("jpg" in url or "jpeg" in url or "png" in url or "webp" in url or "image" in media.get("medium", "")):
            return url
    media_thumb = entry.get("media_thumbnail")
    if media_thumb and isinstance(media_thumb, list) and media_thumb[0].get("url"):
        return media_thumb[0]["url"]
    for enc in entry.get("enclosures", []):
        if "image" in enc.get("type", ""):
            return enc.get("href") or enc.get("url")
    return None


def fetch_articles(feed_config):
    """Fetch and parse one RSS feed, returning a list of article dicts."""
    name = feed_config["name"]
    url = feed_config["url"]
    needs_filter = feed_config["needs_filter"]

    print(f"  Fetching {name}...")
    try:
        d = feedparser.parse(url)
    except Exception as e:
        print(f"  ✗ Failed to parse {name}: {e}")
        return []

    if d.bozo and not d.entries:
        print(f"  ✗ Feed error for {name}: {d.bozo_exception}")
        return []

    articles = []
    cutoff = datetime.now(timezone.utc) - timedelta(days=7)

    for entry in d.entries:
        title = (entry.get("title") or "").strip()
        description = (entry.get("summary") or entry.get(
            "description") or "").strip()
        link = entry.get("link", "")

        if not title or not link:
            continue

        published = parse_published(entry)
        if published < cutoff:
            continue

        # Strip HTML tags and decode entities
        description = re.sub(r"<[^>]+>", "", description).strip()
        description = html.unescape(description)
        title = html.unescape(title)

        # Google News titles end with " - Source Name"
        source_match = re.search(r"\s*[-–—]\s*([A-Z][\w\s.'']+)$", title)
        source = source_match.group(1).strip() if source_match else None
        title = re.sub(r"\s*[-–—]\s*[A-Z][\w\s.'']+$", "", title).strip()

        # Relevance filter
        if needs_filter and not is_relevant(title, description):
            continue

        image = extract_image(entry)

        articles.append({
            "type": "news",
            "title": title,
            "description": source,
            "cta_url": link,
            "image_url": image,
            "tag": None,
            "location": "Both",
            "is_active": False,
            "is_featured": False,
            "created_at": published.isoformat(),
            "expires_at": (published + timedelta(days=30)).isoformat(),
        })

    print(f"  ✓ {name}: {len(d.entries)} entries → {len(articles)} relevant")
    return articles


# ---------------------------------------------------------------------------
# SUPABASE
# ---------------------------------------------------------------------------
def normalize_title(title):
    """Normalize title for fuzzy dedup."""
    t = re.sub(r"[^a-z0-9 ]", "", title.lower()).strip()
    return t[:50]


def get_existing_articles():
    """Fetch existing news URLs and titles from explore_items for dedup."""
    url = f"{SUPABASE_URL}/rest/v1/explore_items?type=eq.news&select=cta_url,title"
    headers = {
        "apikey": SUPABASE_KEY,
        "Authorization": f"Bearer {SUPABASE_KEY}",
    }
    resp = requests.get(url, headers=headers, timeout=30)
    resp.raise_for_status()
    rows = resp.json()
    urls = {row["cta_url"] for row in rows if row.get("cta_url")}
    titles = {normalize_title(row["title"])
              for row in rows if row.get("title")}
    return urls, titles


def insert_articles(articles):
    """Insert new articles into explore_items."""
    if not articles:
        print("  Nothing to insert.")
        return

    url = f"{SUPABASE_URL}/rest/v1/explore_items"
    headers = {
        "apikey": SUPABASE_KEY,
        "Authorization": f"Bearer {SUPABASE_KEY}",
        "Content-Type": "application/json",
        "Prefer": "return=minimal",
    }

    for i in range(0, len(articles), 20):
        batch = articles[i:i + 20]
        resp = requests.post(url, headers=headers, json=batch, timeout=30)
        if resp.status_code in (200, 201):
            print(f"  ✓ Inserted batch {i // 20 + 1} ({len(batch)} articles)")
        else:
            print(f"  ✗ Insert failed: {resp.status_code} — {resp.text[:200]}")


def cleanup_expired():
    """Deactivate news items older than 30 days."""
    cutoff = (datetime.now(timezone.utc) - timedelta(days=30)).isoformat()
    url = (
        f"{SUPABASE_URL}/rest/v1/explore_items"
        f"?type=eq.news&is_active=eq.true&created_at=lt.{cutoff}"
    )
    headers = {
        "apikey": SUPABASE_KEY,
        "Authorization": f"Bearer {SUPABASE_KEY}",
        "Content-Type": "application/json",
        "Prefer": "return=minimal",
    }
    resp = requests.patch(url, headers=headers, json={
                          "is_active": False}, timeout=30)
    if resp.status_code in (200, 204):
        print("  ✓ Expired old news deactivated")
    else:
        print(f"  ⚠ Cleanup response: {resp.status_code}")


# ---------------------------------------------------------------------------
# MAIN
# ---------------------------------------------------------------------------
def main():
    print("=" * 50)
    print(
        f"News scraper — {datetime.now(timezone.utc).strftime('%Y-%m-%d %H:%M UTC')}")
    print("=" * 50)

    print("\n[1/4] Loading existing articles for dedup...")
    existing_urls, existing_titles = get_existing_articles()
    print(
        f"  Found {len(existing_urls)} existing news URLs, {len(existing_titles)} titles")

    print("\n[2/4] Fetching RSS feeds...")
    all_articles = []
    for feed in RSS_FEEDS:
        articles = fetch_articles(feed)
        all_articles.extend(articles)

    print(f"\n  Total relevant articles: {len(all_articles)}")

    print("\n[3/4] Deduplicating...")
    seen_urls = set()
    seen_titles = set()
    new_articles = []
    for article in all_articles:
        url = article["cta_url"]
        norm_title = normalize_title(article["title"])
        if url in existing_urls or url in seen_urls:
            continue
        if norm_title in existing_titles or norm_title in seen_titles:
            continue
        seen_urls.add(url)
        seen_titles.add(norm_title)
        new_articles.append(article)

    print(f"  New articles to insert: {len(new_articles)}")

    if new_articles:
        for a in new_articles:
            print(f"    [{a['tag']}] {a['title'][:60]}")

    print("\n[4/4] Inserting into Supabase...")
    insert_articles(new_articles)

    print("\n  Cleaning up expired news...")
    cleanup_expired()

    print(f"\n{'=' * 50}")
    print(f"Done. {len(new_articles)} new articles added.")
    print(f"{'=' * 50}")


if __name__ == "__main__":
    main()
