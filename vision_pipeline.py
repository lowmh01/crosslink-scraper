"""
vision_pipeline.py  (Gemini Flash-Lite version)
-----------------------------------------------
Reads LTA DataMall traffic camera images, classifies congestion with
Gemini Flash-Lite (free tier), and writes one qualitative status row per
camera into the Supabase `traffic_readings` table.

Runs every 15 minutes via GitHub Actions.

Honesty rule baked in: if an image can't be fetched, or the model returns
something we can't parse into clear/moderate/heavy, we write NOTHING for
that camera. A gap is honest; a guessed status is not.

Required environment variables (set as GitHub Actions secrets):
  LTA_ACCOUNT_KEY        - LTA DataMall AccountKey
  GEMINI_API_KEY         - Google AI Studio key (free, no credit card)
  SUPABASE_URL           - e.g. https://xxxx.supabase.co
  SUPABASE_SERVICE_KEY   - Supabase service_role key (writes only, server-side)
"""

import base64
import json
import os
import sys
import requests

# ---------------------------------------------------------------------
# Which camera represents which checkpoint + direction.
# CameraIDs from LTA DataMall, matched to your site's camConfigs.
# NOTE: confirm 4713's real direction - your camConfigs had it as JB->SG.
#       Adjust the `direction` field here if needed; the heatmap accuracy
#       depends entirely on this mapping being right.
# ---------------------------------------------------------------------
CAMERAS = [
    {"camera_id": "2701", "checkpoint": "woodlands", "direction": "sg_jb"},
    {"camera_id": "2702", "checkpoint": "woodlands", "direction": "jb_sg"},
    {"camera_id": "4703", "checkpoint": "tuas",      "direction": "jb_sg"},
    {"camera_id": "4713", "checkpoint": "tuas",      "direction": "sg_jb"},
]

LTA_IMAGES_URL = "https://datamall2.mytransport.sg/ltaodataservice/Traffic-Imagesv2"

# Model name as shown in Google AI Studio. Flash-Lite is free-tier eligible
# and ideal for simple classification. Swap to "gemini-3-flash" if you want
# a slightly stronger model (also free, lower RPM).
GEMINI_MODEL = "gemini-3.1-flash-lite"
GEMINI_URL = (
    f"https://generativelanguage.googleapis.com/v1beta/models/"
    f"{GEMINI_MODEL}:generateContent"
)

VALID = {"clear", "moderate", "heavy"}

CLASSIFY_PROMPT = (
    "This is a live traffic camera image at the Singapore-Malaysia land "
    "checkpoint (Woodlands Causeway or Tuas Second Link). Judge how congested "
    "the road in view is, based on vehicle density and whether traffic looks "
    "like it is moving or queued.\n\n"
    "Classify into exactly one of:\n"
    '- "clear": light traffic, vehicles moving freely, low density\n'
    '- "moderate": noticeable build-up, dense but still moving\n'
    '- "heavy": packed, queued, or stationary traffic\n\n'
    "If the image is too dark, blurry, or obstructed to judge, use status "
    '"unknown".\n\n'
    'Respond with a JSON object: '
    '{"status": "clear|moderate|heavy|unknown", "note": "<one short factual sentence>"}'
)


def get_camera_images():
    """Return {camera_id: image_url} for the cameras we care about."""
    wanted = {c["camera_id"] for c in CAMERAS}
    headers = {
        "AccountKey": os.environ["LTA_ACCOUNT_KEY"], "accept": "application/json"}
    r = requests.get(LTA_IMAGES_URL, headers=headers, timeout=30)
    r.raise_for_status()
    out = {}
    for cam in r.json().get("value", []):
        cid = str(cam.get("CameraID"))
        if cid in wanted and cam.get("ImageLink"):
            out[cid] = cam["ImageLink"]
    return out


def classify(image_bytes):
    """Return {'status', 'note'} or None if unusable."""
    b64 = base64.standard_b64encode(image_bytes).decode("utf-8")
    body = {
        "contents": [
            {
                "parts": [
                    {"inline_data": {"mime_type": "image/jpeg", "data": b64}},
                    {"text": CLASSIFY_PROMPT},
                ]
            }
        ],
        "generationConfig": {"maxOutputTokens": 200, "responseMimeType": "application/json"},
    }
    r = requests.post(
        GEMINI_URL,
        params={"key": os.environ["GEMINI_API_KEY"]},
        json=body,
        timeout=60,
    )
    r.raise_for_status()
    try:
        text = r.json()["candidates"][0]["content"]["parts"][0]["text"].strip()
    except (KeyError, IndexError):
        print("  ! unexpected Gemini response shape")
        return None
    text = text.replace("```json", "").replace("```", "").strip()
    try:
        parsed = json.loads(text)
    except json.JSONDecodeError:
        print(f"  ! could not parse model output: {text[:120]}")
        return None
    status = str(parsed.get("status", "")).lower().strip()
    if status not in VALID:
        print(f"  - skipped (status='{status}')")
        return None
    note = str(parsed.get("note", "")).strip()[:280]
    return {"status": status, "note": note}


def insert_reading(checkpoint, direction, status, note):
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
    for cam in CAMERAS:
        cid = cam["camera_id"]
        tag = f"{cam['checkpoint']}/{cam['direction']} (cam {cid})"
        url = images.get(cid)
        if not url:
            print(f"- {tag}: no image link from LTA, skipping")
            continue
        try:
            img = requests.get(url, timeout=30).content
        except Exception as e:
            print(f"- {tag}: image download failed ({e}), skipping")
            continue

        result = classify(img)
        if not result:
            print(f"- {tag}: not classifiable, writing nothing")
            continue

        try:
            insert_reading(cam["checkpoint"], cam["direction"],
                           result["status"], result["note"])
            written += 1
            print(f"- {tag}: {result['status']} - {result['note']}")
        except Exception as e:
            print(f"- {tag}: DB insert failed ({e})")

    print(f"\nDone. {written}/{len(CAMERAS)} readings written.")


if __name__ == "__main__":
    main()
