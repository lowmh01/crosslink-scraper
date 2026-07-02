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
     "anchor": "The lanes beside the yellow 'WOODLANDS' sign carry traffic towards Singapore = jb_sg (this side is nearer the camera). The lanes beside the yellow 'JOHOR' sign carry traffic towards Johor = sg_jb (this side is along the water)."},
    {"camera_id": "2702", "checkpoint": "woodlands", "weight": 0.3,
     "anchor": "The lanes beside the yellow 'BKE' sign carry traffic towards Singapore = jb_sg (left carriageway). The lanes beside the yellow 'CAUSEWAY' sign carry traffic towards Johor = sg_jb (right carriageway)."},
    {"camera_id": "4703", "checkpoint": "tuas", "weight": 0.7,
     "anchor": "Camera 4703: The curving ramp beside the yellow 'JOHOR' sign carries traffic towards Johor = sg_jb. The OTHER road visible in the upper-right area of the frame, running along the bridge toward the checkpoint buildings, carries traffic towards Singapore = jb_sg. Do NOT count trucks or vehicles parked in staging areas beside the road — only judge vehicles actually on the travel lanes of each carriageway."},
    {"camera_id": "4713", "checkpoint": "tuas", "weight": 0.3,
     "anchor": "The lanes beside the yellow 'AYE' sign carry traffic towards Singapore = jb_sg (left carriageway). The lanes beside the yellow 'JOHOR' sign carry traffic towards Johor = sg_jb (right carriageway)."},
]

LTA_IMAGES_URL = "https://datamall2.mytransport.sg/ltaodataservice/Traffic-Imagesv2"

GEMINI_MODEL = "gemini-3.1-flash-lite"
GEMINI_URL = (
    f"https://generativelanguage.googleapis.com/v1beta/models/"
    f"{GEMINI_MODEL}:generateContent"
)

VALID = {"clear", "moderate", "heavy"}
DIRECTIONS = ("sg_jb", "jb_sg")


def build_prompt(anchor):
    return (
        "This is a live LTA traffic camera at a Singapore-Malaysia land "
        "checkpoint (Woodlands Causeway or Tuas Second Link). The frame "
        "contains yellow LTA direction signs that label where each carriageway "
        "leads. Use these signs as your anchor to tell the two directions "
        "apart. Do NOT guess direction from left/right or from the scene.\n\n"
        f"{anchor}\n\n"
        "Now judge congestion SEPARATELY for each direction, looking only at "
        "the lanes that belong to it:\n"
        "- sg_jb = heading towards Johor Bahru / Malaysia\n"
        "- jb_sg = heading towards Singapore\n\n"
        "Classify each as exactly one of:\n"
        '- "clear": light, moving freely, low density\n'
        '- "moderate": noticeable build-up, dense but still moving\n'
        '- "heavy": packed, queued, or stationary\n\n'
        "IMPORTANT: Only evaluate congestion based on cars, motorcycles, and "
        "buses. Ignore large trucks, container trucks, trailers, and lorries "
        "— they are usually parked in staging areas and do not reflect "
        "commuter traffic conditions.\n\n"
        "If you cannot locate a sign or judge its lanes, use \"unknown\" for "
        "that direction. Do not guess.\n\n"
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


def classify(image_bytes, anchor):
    """Return {'sg_jb': {status,note}, 'jb_sg': {status,note}} or None."""
    b64 = base64.standard_b64encode(image_bytes).decode("utf-8")
    body = {
        "contents": [
            {
                "parts": [
                    {"inline_data": {"mime_type": "image/jpeg", "data": b64}},
                    {"text": build_prompt(anchor)},
                ]
            }
        ],
        "generationConfig": {"maxOutputTokens": 300, "responseMimeType": "application/json"},
    }
    r = requests.post(
        GEMINI_URL,
        params={"key": os.environ["GEMINI_API_KEY"]},
        json=body,
        timeout=60,
    )
    # Retry on 429 (rate limit) — wait and try again, up to 3 times
    if r.status_code == 429:
        for attempt in range(1, 4):
            wait = 10 * attempt  # 10s, 20s, 30s (exponential-ish backoff)
            print(f"  ! 429 rate-limited, waiting {wait}s (retry {attempt}/3)")
            time.sleep(wait)
            r = requests.post(
                GEMINI_URL,
                params={"key": os.environ["GEMINI_API_KEY"]},
                json=body,
                timeout=60,
            )
            if r.status_code != 429:
                break
    if r.status_code == 429:
        print("  ! 429 persisted after 3 retries — skipping this camera")
        return None
    r.raise_for_status()
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
    for cam in CAMERAS:
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

        try:
            result = classify(img, cam["anchor"])
        except Exception as e:
            print(f"- {cp} (cam {cid}): classify failed ({e}), skipping")
            result = None
        # Delay between cameras to stay under Gemini's rate limit
        time.sleep(5)
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
