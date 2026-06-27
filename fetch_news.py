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
# Each entry: { "name": ..., "url": ..., "needs_filter": bool }
# needs_filter=True  → only keep articles matching JB-SG keywords
# needs_filter=False → every article is assumed relevant (niche feed)

RSS_FEEDS = [
    # Google News — broad search, must filter
    {
        "name": "Google News (MY-SG corridor)",
        "url": "https://news.google.com/rss/search?q=%22johor+singapore%22+OR+%22causeway+checkpoint%22+OR+%22woodlands+checkpoint%22+OR+%22tuas+checkpoint%22+OR+%22cross-border%22+OR+%22VEP+singapore%22+OR+%22work+pass+singapore%22&hl=en&gl=SG&ceid=SG:en",
        "needs_filter": True,
    },
    {
        "name": "Google News (RTS Link)",
        "url": "https://news.google.com/rss/search?q=%22RTS+Link%22+OR+%22johor+singapore+rail%22&hl=en&gl=SG&ceid=SG:en",
        "needs_filter": False,
    },
    # CNA — Singapore section, must filter
    {
        "name": "CNA Singapore",
        "url": "https://www.channelnewsasia.com/api/v1/rss-outbound-feed?_format=xml&category=6511",
        "needs_filter": True,
    },
    # CNA — Asia section (catches MY-SG bilateral news)
    {
        "name": "CNA Asia",
        "url": "https://www.channelnewsasia.com/api/v1/rss-outbound-feed?_format=xml&category=6936",
        "needs_filter": True,
    },
    # The Star — Nation section
    {
        "name": "The Star",
        "url": "https://www.thestar.com.my/rss/News/Nation",
        "needs_filter": True,
    },
]

# ---------------------------------------------------------------------------
# RELEVANCE KEYWORDS — article must contain at least one to pass filter
# ---------------------------------------------------------------------------
# Groups: if ANY keyword in a group matches title OR description, it's relevant.
# We use groups so compound terms like "work pass" match as a phrase.

RELEVANCE_KEYWORDS = [
    # --- Pillar 1: Border & Crossing ---
    "causeway", "second link",
    "woodlands checkpoint", "tuas checkpoint",
    "immigration checkpoint", "customs checkpoint",
    "cross-border", "cross border",
    # --- Pillar 2: Cross-border Transport ---
    "rts link", "ktm shuttle",
    "bukit chagar", "woodlands north",
    "transtar", "causeway link",
    "cw1", "cw2", "bus 170",
    "cross-border bus",
    # --- Pillar 3: VEP & Driving ---
    "vep", "vehicle entry permit", "autopass",
    # --- Pillar 4: Working in SG (cross-border context) ---
    "work pass", "employment pass", "s pass", "work permit",
    "foreign worker", "malaysian worker",
    "compass framework",
    # --- Pillar 5: Money & Remittance (SG-MY specific) ---
    "sgd myr", "myr sgd", "sgd to myr", "myr to sgd",
    "remittance", "duitnow", "paynow",
    "cpf foreign", "cpf withdrawal",
    # --- Pillar 6: Living in SG as Malaysian ---
    "arrival card", "sgac", "myica",
    # --- Pillar 7: Malaysia-Singapore Bilateral ---
    "malaysia singapore", "singapore malaysia",
    "johor singapore", "singapore johor",
    "bilateral", "jb-sg", "jb sg",
    "johor-singapore",
    "special economic zone",
]

# Pre-compile for speed
_kw_patterns = [re.compile(re.escape(kw), re.IGNORECASE)
                for kw in RELEVANCE_KEYWORDS]


def is_relevant(title, description):
    """Return True if title or description contains at least one relevance keyword."""
    text = f"{title} {description}"
    return any(p.search(text) for p in _kw_patterns)


# ---------------------------------------------------------------------------
# AUTO-TAGGING
# ---------------------------------------------------------------------------
TAG_RULES = [
    # (tag_name, keywords_to_check)  — first match wins
    ("Transport", ["rts link", "ktm", "shuttle", "bukit chagar", "woodlands north",
                   "transtar", "causeway link", "cw1", "cw2", "170",
                   "cross-border bus"]),
    ("Border",    ["checkpoint", "causeway", "second link", "immigration",
                   "customs", "crossing", "congestion", "queue", "jam"]),
    ("VEP",       ["vep", "vehicle entry permit", "autopass", "rfid",
                   "foreign vehicle"]),
    ("Work",      ["work pass", "employment pass", "s pass", "work permit",
                   "foreign worker", "malaysian worker", "compass"]),
    ("Finance",   ["sgd", "myr", "ringgit", "remittance",
                   "cpf", "duitnow", "paynow", "wise", "instarem"]),
    ("Policy",    ["bilateral", "agreement", "special economic zone",
                   "malaysia singapore", "singapore malaysia"]),
]

_tag_patterns = [
    (tag, [re.compile(re.escape(kw), re.IGNORECASE) for kw in kws])
    for tag, kws in TAG_RULES
]


def auto_tag(title, description):
    """Return the best matching tag, or 'News' as default."""
    text = f"{title} {description}"
    for tag, patterns in _tag_patterns:
        if any(p.search(text) for p in patterns):
            return tag
    return "News"


# ---------------------------------------------------------------------------
# AUTO-LOCATION
# ---------------------------------------------------------------------------
MY_KEYWORDS = re.compile(
    r"johor|malaysia|jb|iskandar|tebrau|bukit chagar|ringgit|bernama|the star"
    r"|causeway|second link",
    re.IGNORECASE,
)
SG_KEYWORDS = re.compile(
    r"singapore|woodlands|tuas|cpf|hdb|mrt|lta|iras|ica|mom\b|sgd|cna"
    r"|causeway|second link",
    re.IGNORECASE,
)


def auto_location(title, description, source_name):
    """Return 'JB', 'SG', or 'Both'."""
    text = f"{title} {description} {source_name}"
    has_my = bool(MY_KEYWORDS.search(text))
    has_sg = bool(SG_KEYWORDS.search(text))
    if has_my and has_sg:
        return "Both"
    if has_my:
        return "JB"
    if has_sg:
        return "SG"
    return "Both"


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
    # media:content or media:thumbnail
    for media in entry.get("media_content", []):
        url = media.get("url", "")
        if url and ("jpg" in url or "jpeg" in url or "png" in url or "webp" in url or "image" in media.get("medium", "")):
            return url
    media_thumb = entry.get("media_thumbnail")
    if media_thumb and isinstance(media_thumb, list) and media_thumb[0].get("url"):
        return media_thumb[0]["url"]
    # enclosure
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

        # Google News titles end with " - Source Name", strip it
        title = re.sub(r"\s*[-–—]\s*[A-Z][\w\s.'']+$", "", title).strip()

        # Truncate long descriptions
        if len(description) > 300:
            description = description[:297] + "..."

        # Relevance filter
        if needs_filter and not is_relevant(title, description):
            continue

        tag = auto_tag(title, description)
        location = auto_location(title, description, name)
        image = extract_image(entry)

        articles.append({
            "type": "news",
            "title": title,
            "description": description,
            "cta_url": link,
            "image_url": image,
            "tag": tag,
            "location": location,
            "is_active": True,
            "is_featured": False,
            "created_at": published.isoformat(),
            # Auto-expire news after 30 days
            "expires_at": (published + timedelta(days=30)).isoformat(),
        })

    print(f"  ✓ {name}: {len(d.entries)} entries → {len(articles)} relevant")
    return articles


# ---------------------------------------------------------------------------
# SUPABASE
# ---------------------------------------------------------------------------
def get_existing_urls():
    """Fetch all existing news cta_urls from explore_items to avoid duplicates."""
    url = f"{SUPABASE_URL}/rest/v1/explore_items?type=eq.news&select=cta_url"
    headers = {
        "apikey": SUPABASE_KEY,
        "Authorization": f"Bearer {SUPABASE_KEY}",
    }
    resp = requests.get(url, headers=headers, timeout=30)
    resp.raise_for_status()
    return {row["cta_url"] for row in resp.json() if row.get("cta_url")}


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

    # Insert in batches of 20
    for i in range(0, len(articles), 20):
        batch = articles[i:i + 20]
        resp = requests.post(url, headers=headers, json=batch, timeout=30)
        if resp.status_code in (200, 201):
            print(f"  ✓ Inserted batch {i // 20 + 1} ({len(batch)} articles)")
        else:
            print(f"  ✗ Insert failed: {resp.status_code} — {resp.text[:200]}")


def cleanup_expired():
    """Deactivate news items older than 30 days (belt-and-suspenders with expires_at)."""
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

    # 1. Fetch existing URLs for dedup
    print("\n[1/4] Loading existing articles for dedup...")
    existing_urls = get_existing_urls()
    print(f"  Found {len(existing_urls)} existing news URLs")

    # 2. Fetch from all RSS feeds
    print("\n[2/4] Fetching RSS feeds...")
    all_articles = []
    for feed in RSS_FEEDS:
        articles = fetch_articles(feed)
        all_articles.extend(articles)

    print(f"\n  Total relevant articles: {len(all_articles)}")

    # 3. Deduplicate
    print("\n[3/4] Deduplicating...")
    seen_urls = set()
    new_articles = []
    for article in all_articles:
        url = article["cta_url"]
        if url in existing_urls:
            continue
        if url in seen_urls:
            continue
        seen_urls.add(url)
        new_articles.append(article)

    print(f"  New articles to insert: {len(new_articles)}")

    if new_articles:
        for a in new_articles:
            print(f"    [{a['tag']}] {a['title'][:60]}")

    # 4. Insert + cleanup
    print("\n[4/4] Inserting into Supabase...")
    insert_articles(new_articles)

    print("\n  Cleaning up expired news...")
    cleanup_expired()

    print(f"\n{'=' * 50}")
    print(f"Done. {len(new_articles)} new articles added.")
    print(f"{'=' * 50}")


if __name__ == "__main__":
    main()
