"""
vision_pipeline.py  (Gemini Flash-Lite, 4 cameras x 2 directions, raw rows)
---------------------------------------------------------------------------
Each camera frame contains BOTH directions of travel, so Gemini judges each
direction separately from one image. Every checkpoint has a primary and a
secondary camera; we store the RAW per-camera reading for each direction
(tagged with camera_id + weight) and let the display layer combine them.
This keeps all source data, including disagreements between cameras.

  Woodlands: 2701 primary (0.7), 2702 secondary (0.3)
  Tuas:      4703 primary (0.7), 4713 secondary (0.3)

Runs every 15 minutes via GitHub Actions.

Honesty rule: if an image can't be fetched, or a direction comes back as
"unknown" / unparseable, we write NOTHING for it. A gap is honest; a guess
is not.

Required environment variables (GitHub Actions secrets):
  LTA_API_KEY            - LTA DataMall AccountKey
  GEMINI_API_KEY         - Google AI Studio key (free, no credit card)
  SUPABASE_URL           - e.g. https://xxxx.supabase.co
  SUPABASE_SERVICE_KEY   - Supabase service_role key (writes only, server-side)
"""

import base64
import json
import os
import sys
import time
import requests

# ---------------------------------------------------------------------
# Each camera maps to a CHECKPOINT (both directions live in the frame),
# plus a weight: primary 0.7, secondary 0.3. The label gives the model
# orientation context to tell the two directions apart.
# ---------------------------------------------------------------------
CAMERAS = [
    {"camera_id": "2701", "checkpoint": "woodlands", "weight": 0.7,
     "label": "Woodlands Causeway, primary view towards Johor Bahru"},
    {"camera_id": "2702", "checkpoint": "woodlands", "weight": 0.3,
     "label": "Woodlands Checkpoint, secondary view towards BKE / Singapore"},
    {"camera_id": "4703", "checkpoint": "tuas", "weight": 0.7,
     "label": "Second Link at Tuas, primary view"},
    {"camera_id": "4713", "checkpoint": "tuas", "weight": 0.3,
     "label": "Tuas Checkpoint, secondary view"},
]

LTA_IMAGES_URL = "https://datamall2.mytransport.sg/ltaodataservice/Traffic-Imagesv2"

GEMINI_MODEL = "gemini-3.1-flash-lite"
GEMINI_URL = (
    f"https://generativelanguage.googleapis.com/v1beta/models/"
    f"{GEMINI_MODEL}:generateContent"
)

VALID = {"clear", "moderate", "heavy"}
DIRECTIONS = ("sg_jb", "jb_sg")

# Rate-limit safety: seconds to wait between Gemini calls
GEMINI_CALL_DELAY = 3
# Retry config for 429
RETRY_DELAY = 10
MAX_RETRIES = 1


def build_prompt(label):
    return (
        "This is a live traffic camera at a Singapore-Malaysia land checkpoint "
        f"(Woodlands Causeway or Tuas Second Link). Camera context: {label}.\n\n"
        "The view contains traffic moving in two opposite directions. Judge "
        "congestion SEPARATELY for each direction:\n"
        "- sg_jb = traffic heading FROM Singapore TOWARDS Johor Bahru / Malaysia\n"
        "- jb_sg = traffic heading FROM Johor Bahru / Malaysia TOWARDS Singapore\n\n"
        "For each direction classify as exactly one of:\n"
        '- "clear": light, moving freely, low density\n'
        '- "moderate": noticeable build-up, dense but still moving\n'
        '- "heavy": packed, queued, or stationary\n\n'
        "If a direction is not visible in the frame, or you cannot tell which "
        'lanes belong to it, use "unknown" for that direction. Do not guess.\n\n'
        "Respond with a JSON object only:\n"
        '{"sg_jb": {"status": "clear|moderate|heavy|unknown", "note": "<one short sentence>"}, '
        '"jb_sg": {"status": "clear|moderate|heavy|unknown", "note": "<one short sentence>"}}'
    )


def get_camera_images():
    """Return {camera_id: image_url} for the cameras we care about."""
    wanted = {c["camera_id"] for c in CAMERAS}
    headers = {
        "AccountKey": os.environ["LTA_API_KEY"], "accept": "application/json"}
    r = requests.get(LTA_IMAGES_URL, headers=headers, timeout=30)
    r.raise_for_status()
    out = {}
    for cam in r.json().get("value", []):
        cid = str(cam.get("CameraID"))
        if cid in wanted and cam.get("ImageLink"):
            out[cid] = cam["ImageLink"]
    return out


def classify(image_bytes, label):
    """Return {'sg_jb': {status,note}, 'jb_sg': {status,note}} or None.
    Retries once on 429 (rate limit) with a delay."""
    b64 = base64.standard_b64encode(image_bytes).decode("utf-8")
    body = {
        "contents": [
            {
                "parts": [
                    {"inline_data": {"mime_type": "image/jpeg", "data": b64}},
                    {"text": build_prompt(label)},
                ]
            }
        ],
        "generationConfig": {"maxOutputTokens": 300, "responseMimeType": "application/json"},
    }

    for attempt in range(1 + MAX_RETRIES):
        r = requests.post(
            GEMINI_URL,
            params={"key": os.environ["GEMINI_API_KEY"]},
            json=body,
            timeout=60,
        )
        if r.status_code == 429:
            if attempt < MAX_RETRIES:
                wait = RETRY_DELAY * (attempt + 1)
                print(f"  ! 429 rate limited, waiting {wait}s before retry...")
                time.sleep(wait)
                continue
            else:
                print("  ! 429 rate limited, no retries left, skipping")
                return None
        r.raise_for_status()
        break

    try:
        text = r.json()["candidates"][0]["content"]["parts"][0]["text"].strip()
    except (KeyError, IndexError):
        print("  ! unexpected Gemini response shape")
        return None
    text = text.replace("```json", "").replace("```", "").strip()
    try:
        return json.loads(text)
    except json.JSONDecodeError:
        print(f"  ! could not parse model output: {text[:120]}")
        return None


def insert_reading(checkpoint, direction, status, note, camera_id, weight):
    url = os.environ["SUPABASE_URL"].rstrip("/") + "/rest/v1/traffic_readings"
    key = os.environ["SUPABASE_SERVICE_KEY"]
    headers = {
        "apikey": key,
        "Authorization": f"Bearer {key}",
        "Content-Type": "application/json",
        "Prefer": "return=minimal",
    }
    payload = {
        "checkpoint": checkpoint,
        "direction": direction,
        "status": status,
        "vision_note": note,
        "source": "vision",
        "camera_id": camera_id,
        "weight": weight,
    }
    r = requests.post(url, headers=headers, json=payload, timeout=30)
    r.raise_for_status()


def main():
    try:
        images = get_camera_images()
    except Exception as e:
        print(f"FATAL: could not fetch LTA images: {e}")
        sys.exit(1)

    written = 0
    attempted = 0
    for i, cam in enumerate(CAMERAS):
        cid = cam["camera_id"]
        cp = cam["checkpoint"]
        url = images.get(cid)
        if not url:
            print(f"- {cp} (cam {cid}): no image link from LTA, skipping")
            continue
        try:
            img = requests.get(url, timeout=30).content
        except Exception as e:
            print(f"- {cp} (cam {cid}): image download failed ({e}), skipping")
            continue

        # Rate-limit: wait between Gemini calls (skip before the first one)
        if i > 0:
            time.sleep(GEMINI_CALL_DELAY)

        result = classify(img, cam["label"])
        if not result:
            print(f"- {cp} (cam {cid}): not classifiable, writing nothing")
            continue

        for direction in DIRECTIONS:
            attempted += 1
            d = result.get(direction) or {}
            status = str(d.get("status", "")).lower().strip()
            tag = f"{cp}/{direction} (cam {cid}, w={cam['weight']})"
            if status not in VALID:
                print(f"- {tag}: {status or 'missing'}, writing nothing")
                continue
            note = str(d.get("note", "")).strip()[:280]
            try:
                insert_reading(cp, direction, status, note, cid, cam["weight"])
                written += 1
                print(f"- {tag}: {status} - {note}")
            except Exception as e:
                print(f"- {tag}: DB insert failed ({e})")

    print(f"\nDone. {written}/{attempted} direction-readings written.")


if __name__ == "__main__":
    main()
