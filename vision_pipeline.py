"""
vision_pipeline.py  (Gemini Flash-Lite, 1 camera per direction)
---------------------------------------------------------------
Each direction uses only its primary camera — no cross-camera mixing.
Secondary cameras were reading "post-checkpoint" traffic (vehicles that
already cleared), diluting the signal and inflating Moderate readings.

  Woodlands JB→SG: 2701 (diagonal crop, below-the-line carriageway)
  Woodlands SG→JB: 2702 (sign-based, CAUSEWAY side only)
  Tuas JB→SG:      4703 (polyline crop, far carriageway)
  Tuas SG→JB:      4713 (sign-based, JOHOR side only)

  Woodlands: 2701 crop + 2702 full → 1 API call (2 images)
  Tuas:      4703 crop + 4713 full → 1 API call (2 images)
  Total: 2 calls per run, 1 reading per direction per run (4 total).

Required env vars: LTA_API_KEY, GEMINI_API_KEY, SUPABASE_URL, SUPABASE_SERVICE_KEY
"""

import base64
import io
import json
import os
import sys
import time
import requests
from PIL import Image, ImageDraw

# ---- Crop configs ----

# 2701 diagonal crop: the dividing line between the two carriageways.
# Below the line = toward Singapore (4 wide lanes) = jb_sg (primary).
CROP_2701 = {
    "left_y_pct": 0.43,   # at left edge of image, line is at 43% from top
    "right_y_pct": 0.33,  # at right edge, line is at 33% from top
}

# 4703 polyline divider traced from a hand-annotated native 1920x1080 LTA
# frame. Above/right of the line = jb_sg (far carriageway, primary).
# Coordinates are (x_pct, y_pct), resolution-independent.
CROP_4703_DIVIDER = [
    (0.0852, 0.0),      # line enters at top edge
    (1.0, 0.6035),      # line exits at right edge
]

LTA_IMAGES_URL = "https://datamall2.mytransport.sg/ltaodataservice/Traffic-Imagesv2"
GEMINI_MODEL = "gemini-3.1-flash-lite"
GEMINI_URL = f"https://generativelanguage.googleapis.com/v1beta/models/{GEMINI_MODEL}:generateContent"
VALID = {"clear", "moderate", "heavy"}
WANTED_CAMERAS = {"2701", "2702", "4703", "4713"}


CLASSIFICATION_CRITERIA = (
    "Classify each based on TWO visual axes — vehicle SPACING and queue "
    "COVERAGE of the visible road. Do NOT guess whether vehicles are moving "
    "or stopped — a static image cannot tell you that.\n\n"
    '- "clear": road mostly empty, only a few scattered vehicles with '
    "large gaps between them.\n"
    '- "moderate": a queue of vehicles is visible, but you can still '
    "distinguish individual vehicles and see road surface between them. "
    "The queue may be long but vehicles are not bumper-to-bumper.\n"
    '- "heavy": vehicles tightly packed with minimal gaps between them — '
    "you may still see individual rooftops or colours, but the spacing "
    "between vehicles is less than one car length across most of the queue. "
    "Road surface between vehicles is mostly hidden by vehicle bodies. "
    "A dense queue that fills the lanes counts as heavy.\n\n"
    "NIGHT-TIME NOTE: headlight glow can make a queue appear denser than it "
    "really is. At night, count distinct pairs of headlights to judge "
    "spacing — if you can tell individual vehicles apart by their lights, "
    "it is moderate, not heavy.\n\n"
)


def crop_2701_jb_sg(img_bytes):
    """Extract the jb_sg carriageway from 2701 (below the diagonal line)."""
    img = Image.open(io.BytesIO(img_bytes)).convert("RGB")
    w, h = img.size
    left_y = int(h * CROP_2701["left_y_pct"])
    right_y = int(h * CROP_2701["right_y_pct"])
    # Mask out above the line (sg_jb side)
    draw = ImageDraw.Draw(img)
    draw.polygon([(0, 0), (w, 0), (w, right_y), (0, left_y)], fill=(0, 0, 0))
    buf = io.BytesIO()
    img.save(buf, format="JPEG", quality=85)
    return buf.getvalue()


def crop_4703_jb_sg(img_bytes):
    """Extract the jb_sg carriageway from 4703 (above/right of the divider)."""
    img = Image.open(io.BytesIO(img_bytes)).convert("RGB")
    w, h = img.size
    div = [(x * w, y * h) for x, y in CROP_4703_DIVIDER]
    # Mask out the sg_jb region (below/left of line)
    poly_below = div + [(w, h), (0, h), (0, 0)]
    draw = ImageDraw.Draw(img)
    draw.polygon(poly_below, fill=(0, 0, 0))
    buf = io.BytesIO()
    img.save(buf, format="JPEG", quality=85)
    return buf.getvalue()


def build_woodlands_prompt():
    """Prompt for Woodlands: 2701 jb_sg crop + 2702 sg_jb only."""
    return (
        "You are given 2 traffic camera images from the Woodlands border "
        "checkpoint between Singapore and Johor Bahru.\n\n"
        "Image 1: The Causeway bridge — the visible road portion only (the "
        "blacked-out area is masked and must be completely ignored). "
        "This carriageway carries traffic heading TO Singapore (jb_sg). "
        "This is an aerial/diagonal view — from this angle, some road surface "
        "between vehicle rooftops is always visible even in bumper-to-bumper "
        "traffic. Judge spacing by the gap between the FRONT of one vehicle "
        "and the REAR of the next, not by visible road surface between rooftops. "
        "Report as \"2701_jb_sg\".\n\n"
        "Image 2: Camera 2702 showing two carriageways separated by yellow signs. "
        "ONLY classify the RIGHT carriageway marked 'CAUSEWAY' — these vehicles "
        "are heading TO Johor (sg_jb). Completely ignore the left carriageway "
        "marked 'BKE'. Report as \"2702_sg_jb\".\n\n"
        "CONTEXT: These cameras are at a border crossing. Some vehicles queuing "
        "near the checkpoint entrance is NORMAL daytime activity — it does not "
        "automatically mean heavy. Only classify heavy when congestion is "
        "genuinely severe.\n\n"
        + CLASSIFICATION_CRITERIA +
        "IMPORTANT:\n"
        "- Only evaluate cars, motorcycles, and buses. Ignore large trucks, "
        "container trucks, trailers, and lorries entirely.\n"
        "- A queue of buses counts — buses are commuter traffic.\n"
        "- Judge ONLY what you see: density and coverage. Never infer speed.\n\n"
        "Respond with JSON ONLY:\n"
        "{\n"
        '  "2701_jb_sg": {"status": "...", "note": "..."},\n'
        '  "2702_sg_jb": {"status": "...", "note": "..."}\n'
        "}"
    )


def build_tuas_prompt():
    """Prompt for Tuas: 4703 jb_sg crop + 4713 sg_jb only."""
    return (
        "You are given 2 traffic camera images from the Tuas border "
        "checkpoint between Singapore and Johor Bahru.\n\n"
        "Image 1: The Second Link bridge — the visible road portion only (the "
        "blacked-out area is masked and must be completely ignored). "
        "This carriageway carries traffic heading TO Singapore (jb_sg). "
        "This is an aerial/diagonal view — from this angle, some road surface "
        "between vehicle rooftops is always visible even in bumper-to-bumper "
        "traffic. Judge spacing by the gap between the FRONT of one vehicle "
        "and the REAR of the next, not by visible road surface between rooftops. "
        "Report as \"4703_jb_sg\".\n\n"
        "Image 2: Camera 4713 showing two carriageways separated by yellow signs. "
        "ONLY classify the RIGHT carriageway marked 'JOHOR' — these vehicles "
        "are heading TO Johor (sg_jb). Completely ignore the left carriageway "
        "marked 'AYE'. Report as \"4713_sg_jb\".\n\n"
        "CONTEXT: These cameras are at a border crossing. Some vehicles queuing "
        "near the checkpoint entrance is NORMAL daytime activity — it does not "
        "automatically mean heavy. Only classify heavy when congestion is "
        "genuinely severe.\n\n"
        + CLASSIFICATION_CRITERIA +
        "IMPORTANT:\n"
        "- Only evaluate cars, motorcycles, and buses. Ignore large trucks, "
        "container trucks, trailers, and lorries entirely.\n"
        "- A queue of buses counts — buses are commuter traffic.\n"
        "- Judge ONLY what you see: density and coverage. Never infer speed.\n"
        "- Do NOT count vehicles parked in staging areas beside the road — "
        "only judge vehicles actually on the travel lanes.\n\n"
        "Respond with JSON ONLY:\n"
        "{\n"
        '  "4703_jb_sg": {"status": "...", "note": "..."},\n'
        '  "4713_sg_jb": {"status": "...", "note": "..."}\n'
        "}"
    )


def get_camera_images():
    headers = {
        "AccountKey": os.environ["LTA_API_KEY"], "accept": "application/json"}
    r = requests.get(LTA_IMAGES_URL, headers=headers, timeout=30)
    r.raise_for_status()
    out = {}
    for cam in r.json().get("value", []):
        cid = str(cam.get("CameraID"))
        if cid in WANTED_CAMERAS and cam.get("ImageLink"):
            out[cid] = cam["ImageLink"]
    return out


def call_gemini(image_bytes_list, prompt_text):
    """Send N images + prompt to Gemini. Return parsed JSON or None."""
    parts = []
    for img_bytes in image_bytes_list:
        b64 = base64.standard_b64encode(img_bytes).decode("utf-8")
        parts.append({"inline_data": {"mime_type": "image/jpeg", "data": b64}})
    parts.append({"text": prompt_text})

    body = {
        "contents": [{"parts": parts}],
        "generationConfig": {"maxOutputTokens": 800, "responseMimeType": "application/json", "temperature": 0},
    }
    r = requests.post(GEMINI_URL, params={
                      "key": os.environ["GEMINI_API_KEY"]}, json=body, timeout=60)
    if r.status_code == 429:
        for attempt in range(1, 4):
            wait = 10 * attempt
            print(f"  ! 429 rate-limited, waiting {wait}s (retry {attempt}/3)")
            time.sleep(wait)
            r = requests.post(GEMINI_URL, params={
                              "key": os.environ["GEMINI_API_KEY"]}, json=body, timeout=60)
            if r.status_code != 429:
                break
    if r.status_code == 429:
        print("  ! 429 persisted after 3 retries — skipping")
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
        print(f"  ! could not parse: {text[:200]}")
        return None


def insert_reading(checkpoint, direction, status, note, camera_id):
    url = os.environ["SUPABASE_URL"].rstrip("/") + "/rest/v1/traffic_readings"
    key = os.environ["SUPABASE_SERVICE_KEY"]
    headers = {
        "apikey": key, "Authorization": f"Bearer {key}",
        "Content-Type": "application/json", "Prefer": "return=minimal",
    }
    payload = {
        "checkpoint": checkpoint, "direction": direction, "status": status,
        "vision_note": note, "source": "vision", "camera_id": camera_id,
        "weight": 1.0,
    }
    r = requests.post(url, headers=headers, json=payload, timeout=30)
    r.raise_for_status()


def write_result(result_key, direction, result_data, checkpoint, camera_id):
    """Write one result entry to DB."""
    d = result_data or {}
    status = str(d.get("status", "")).lower().strip()
    tag = f"{checkpoint}/{direction} (cam {camera_id})"
    if status not in VALID:
        print(f"- {tag}: {status or 'missing'}, writing nothing")
        return 0
    note = str(d.get("note", "")).strip()[:280]
    try:
        insert_reading(checkpoint, direction, status, note, camera_id)
        print(f"- {tag}: {status} - {note}")
        return 1
    except Exception as e:
        print(f"- {tag}: DB insert failed ({e})")
        return 0


def download_image(url):
    return requests.get(url, timeout=30).content


def main():
    try:
        lta_links = get_camera_images()
    except Exception as e:
        print(f"FATAL: could not fetch LTA images: {e}")
        sys.exit(1)

    written = 0
    attempted = 0

    # ===== WOODLANDS =====
    # 2701 (cropped) → jb_sg only
    # 2702 (full)    → sg_jb only
    print("\n=== WOODLANDS ===")
    imgs_wl = []
    cam2701_ok = False
    cam2702_ok = False

    url_2701 = lta_links.get("2701")
    if url_2701:
        try:
            raw = download_image(url_2701)
            imgs_wl.append(crop_2701_jb_sg(raw))   # Image 1
            cam2701_ok = True
            print("- 2701: downloaded + cropped (jb_sg)")
        except Exception as e:
            print(f"- 2701: crop failed ({e})")
    else:
        print("- 2701: no image link from LTA")

    url_2702 = lta_links.get("2702")
    if url_2702:
        try:
            imgs_wl.append(download_image(url_2702))  # Image 2
            cam2702_ok = True
            print("- 2702: downloaded (sg_jb)")
        except Exception as e:
            print(f"- 2702: download failed ({e})")
    else:
        print("- 2702: no image link from LTA")

    if imgs_wl:
        try:
            result = call_gemini(imgs_wl, build_woodlands_prompt())
        except Exception as e:
            print(f"- woodlands: API call failed ({e})")
            result = None

        if result:
            if cam2701_ok:
                attempted += 1
                written += write_result("2701_jb_sg", "jb_sg",
                                        result.get("2701_jb_sg"),
                                        "woodlands", "2701")
            if cam2702_ok:
                attempted += 1
                written += write_result("2702_sg_jb", "sg_jb",
                                        result.get("2702_sg_jb"),
                                        "woodlands", "2702")
    else:
        print("- woodlands: no images, skipping")

    time.sleep(5)

    # ===== TUAS =====
    # 4703 (cropped) → jb_sg only
    # 4713 (full)    → sg_jb only
    print("\n=== TUAS ===")
    imgs_tu = []
    cam4703_ok = False
    cam4713_ok = False

    url_4703 = lta_links.get("4703")
    if url_4703:
        try:
            raw = download_image(url_4703)
            imgs_tu.append(crop_4703_jb_sg(raw))   # Image 1
            cam4703_ok = True
            print("- 4703: downloaded + cropped (jb_sg)")
        except Exception as e:
            print(f"- 4703: crop failed ({e})")
    else:
        print("- 4703: no image link from LTA")

    url_4713 = lta_links.get("4713")
    if url_4713:
        try:
            imgs_tu.append(download_image(url_4713))  # Image 2
            cam4713_ok = True
            print("- 4713: downloaded (sg_jb)")
        except Exception as e:
            print(f"- 4713: download failed ({e})")
    else:
        print("- 4713: no image link from LTA")

    if imgs_tu:
        try:
            result = call_gemini(imgs_tu, build_tuas_prompt())
        except Exception as e:
            print(f"- tuas: API call failed ({e})")
            result = None

        if result:
            if cam4703_ok:
                attempted += 1
                written += write_result("4703_jb_sg", "jb_sg",
                                        result.get("4703_jb_sg"),
                                        "tuas", "4703")
            if cam4713_ok:
                attempted += 1
                written += write_result("4713_sg_jb", "sg_jb",
                                        result.get("4713_sg_jb"),
                                        "tuas", "4713")
    else:
        print("- tuas: no images, skipping")

    print(f"\nDone. {written}/{attempted} readings written.")


if __name__ == "__main__":
    main()
