"""
vision_pipeline.py  (Gemini Flash-Lite, batched by checkpoint, 2701 diagonal crop)
-----------------------------------------------------------------------------------
Cameras 2701 and 4703 are split into two masked crops (sg_jb / jb_sg) so Gemini
never has to judge direction — it only sees one carriageway per crop.
2701 uses a straight diagonal line; 4703 uses a polyline divider traced from a
hand-annotated frame. Remaining cameras use sign-based anchors.

  Woodlands: 2701 (2 crops) + 2702  → 1 API call (3 images)
  Tuas:      4703 (2 crops) + 4713  → 1 API call (3 images)
  Total: 2 calls per run.

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

# ---- Camera configs ----
CAMERAS = [
    {"camera_id": "2701", "checkpoint": "woodlands", "weight": 0.7},
    {"camera_id": "2702", "checkpoint": "woodlands", "weight": 0.3,
     "anchor": "Camera 2702: The yellow signs indicate DESTINATION — where those vehicles are heading TO.\n- 'BKE' sign (left carriageway): these vehicles are heading TO Singapore via BKE = jb_sg.\n- 'CAUSEWAY' sign (right carriageway): these vehicles are heading TO Johor via the Causeway = sg_jb."},
    {"camera_id": "4703", "checkpoint": "tuas", "weight": 0.7},
    {"camera_id": "4713", "checkpoint": "tuas", "weight": 0.3,
     "anchor": "Camera 4713: The yellow signs indicate DESTINATION — where those vehicles are heading TO.\n- 'AYE' sign (left carriageway): these vehicles are heading TO Singapore via AYE = jb_sg.\n- 'JOHOR' sign (right carriageway): these vehicles are heading TO Johor = sg_jb."},
]

# 2701 diagonal crop config: the dividing line between the two carriageways
CROP_2701 = {
    "left_y_pct": 0.43,   # at left edge of image, line is at 43% from top
    "right_y_pct": 0.33,  # at right edge, line is at 33% from top
    # above the line = toward Johor (2 narrow lanes, water side)
    "above": "sg_jb",
    "below": "jb_sg",     # below the line = toward Singapore (4 wide lanes)
}

# 4703 crop config: straight divider traced from a hand-annotated native
# 1920x1080 LTA frame. Above/right of the line = jb_sg (far carriageway +
# arrival road into Tuas Checkpoint). Below/left = sg_jb (near bridge
# carriageway + departure ramp + curved plaza road). The swing lane sits on
# the sg_jb side by design: contraflow (SG customs lending an sg_jb lane to
# jb_sg at night) is rare, and this placement is correct the rest of the time.
# Coordinates are (x_pct, y_pct) of the frame, resolution-independent.
CROP_4703_DIVIDER = [
    (0.0852, 0.0),      # line enters at top edge
    (1.0, 0.6035),      # line exits at right edge
]

CHECKPOINTS = {}
for _c in CAMERAS:
    CHECKPOINTS.setdefault(_c["checkpoint"], []).append(_c)

LTA_IMAGES_URL = "https://datamall2.mytransport.sg/ltaodataservice/Traffic-Imagesv2"
GEMINI_MODEL = "gemini-3.1-flash-lite"
GEMINI_URL = f"https://generativelanguage.googleapis.com/v1beta/models/{GEMINI_MODEL}:generateContent"
VALID = {"clear", "moderate", "heavy"}
DIRECTIONS = ("sg_jb", "jb_sg")


def crop_2701(img_bytes):
    """Split 2701 image diagonally into two masked halves."""
    img = Image.open(io.BytesIO(img_bytes))
    w, h = img.size
    left_y = int(h * CROP_2701["left_y_pct"])
    right_y = int(h * CROP_2701["right_y_pct"])

    # Above the line = sg_jb: mask out below
    above = img.copy()
    ImageDraw.Draw(above).polygon(
        [(0, left_y), (w, right_y), (w, h), (0, h)], fill=(0, 0, 0))
    buf_a = io.BytesIO()
    above.save(buf_a, format="JPEG", quality=85)

    # Below the line = jb_sg: mask out above
    below = img.copy()
    ImageDraw.Draw(below).polygon(
        [(0, 0), (w, 0), (w, right_y), (0, left_y)], fill=(0, 0, 0))
    buf_b = io.BytesIO()
    below.save(buf_b, format="JPEG", quality=85)

    return buf_a.getvalue(), buf_b.getvalue()


def crop_4703(img_bytes):
    """Split 4703 into two masked halves along the traced polyline divider.

    Returns (sg_jb_bytes, jb_sg_bytes)."""
    img = Image.open(io.BytesIO(img_bytes)).convert("RGB")
    w, h = img.size
    div = [(x * w, y * h) for x, y in CROP_4703_DIVIDER]
    poly_above = div + [(w, 0)]                    # jb_sg region
    poly_below = div + [(w, h), (0, h), (0, 0)]    # sg_jb region

    # sg_jb crop: mask out the jb_sg region
    sg_jb = img.copy()
    ImageDraw.Draw(sg_jb).polygon(poly_above, fill=(0, 0, 0))
    buf_s = io.BytesIO()
    sg_jb.save(buf_s, format="JPEG", quality=85)

    # jb_sg crop: mask out the sg_jb region
    jb_sg = img.copy()
    ImageDraw.Draw(jb_sg).polygon(poly_below, fill=(0, 0, 0))
    buf_j = io.BytesIO()
    jb_sg.save(buf_j, format="JPEG", quality=85)

    return buf_s.getvalue(), buf_j.getvalue()


def build_woodlands_prompt():
    """Prompt for Woodlands: 2701 (2 crops) + 2702."""
    cam2702 = next(c for c in CAMERAS if c["camera_id"] == "2702")
    return (
        "You are given 3 traffic camera images from Woodlands Checkpoint.\n\n"
        "Image 1: One carriageway of the Causeway bridge (the visible road portion only — "
        "the blacked-out area is masked and should be completely ignored). "
        "Judge congestion of the VISIBLE road. Report as \"2701_sg_jb\".\n\n"
        "Image 2: The other carriageway of the same bridge (again, only judge the visible "
        "road, ignore the black mask). Report as \"2701_jb_sg\".\n\n"
        f"Image 3: {cam2702['anchor']}\nReport as \"2702\" with both sg_jb and jb_sg.\n\n"
        "Classify each as exactly one of:\n"
        '- "clear": light, moving freely, low density\n'
        '- "moderate": noticeable build-up, dense but still moving\n'
        '- "heavy": packed, queued, or stationary\n\n'
        "IMPORTANT: Only evaluate based on cars, motorcycles, and buses. "
        "Ignore large trucks, container trucks, trailers, and lorries. "
        "Buses are a major part of commuter traffic — a queue of buses means heavy.\n\n"
        "Respond with JSON ONLY:\n"
        "{\n"
        '  "2701_sg_jb": {"status": "...", "note": "..."},\n'
        '  "2701_jb_sg": {"status": "...", "note": "..."},\n'
        '  "2702": {"sg_jb": {"status": "...", "note": "..."}, "jb_sg": {"status": "...", "note": "..."}}\n'
        "}"
    )


def build_tuas_prompt():
    """Prompt for Tuas: 4703 (2 crops) + 4713."""
    cam4713 = next(c for c in CAMERAS if c["camera_id"] == "4713")
    return (
        "You are given 3 traffic camera images from Tuas Checkpoint.\n\n"
        "Image 1: One carriageway of the Second Link crossing (the visible road "
        "portions only — the blacked-out area is masked and must be completely "
        "ignored). Judge congestion of the VISIBLE roads. "
        "Report as \"4703_sg_jb\".\n\n"
        "Image 2: The other carriageway of the same crossing (again, only judge "
        "the visible roads, ignore the black mask). "
        "Report as \"4703_jb_sg\".\n\n"
        f"Image 3: {cam4713['anchor']}\nReport as \"4713\" with both sg_jb and jb_sg.\n\n"
        "Classify each as exactly one of:\n"
        '- "clear": light, moving freely, low density\n'
        '- "moderate": noticeable build-up, dense but still moving\n'
        '- "heavy": packed, queued, or stationary\n\n'
        "IMPORTANT: Only evaluate based on cars, motorcycles, and buses. "
        "Ignore large trucks, container trucks, trailers, and lorries. "
        "Buses are a major part of commuter traffic — a queue of buses means heavy. "
        "Do NOT count vehicles parked in staging areas beside the road — only "
        "judge vehicles actually on the travel lanes. An empty or near-empty "
        "road means clear.\n\n"
        "Respond with JSON ONLY:\n"
        "{\n"
        '  "4703_sg_jb": {"status": "...", "note": "..."},\n'
        '  "4703_jb_sg": {"status": "...", "note": "..."},\n'
        '  "4713": {"sg_jb": {"status": "...", "note": "..."}, "jb_sg": {"status": "...", "note": "..."}}\n'
        "}"
    )


def get_camera_images():
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


def call_gemini(image_bytes_list, prompt_text):
    """Send N images + prompt to Gemini. Return parsed JSON or None."""
    parts = []
    for img_bytes in image_bytes_list:
        b64 = base64.standard_b64encode(img_bytes).decode("utf-8")
        parts.append({"inline_data": {"mime_type": "image/jpeg", "data": b64}})
    parts.append({"text": prompt_text})

    body = {
        "contents": [{"parts": parts}],
        "generationConfig": {"maxOutputTokens": 800, "responseMimeType": "application/json"},
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


def insert_reading(checkpoint, direction, status, note, camera_id, weight):
    url = os.environ["SUPABASE_URL"].rstrip("/") + "/rest/v1/traffic_readings"
    key = os.environ["SUPABASE_SERVICE_KEY"]
    headers = {
        "apikey": key, "Authorization": f"Bearer {key}",
        "Content-Type": "application/json", "Prefer": "return=minimal",
    }
    payload = {
        "checkpoint": checkpoint, "direction": direction, "status": status,
        "vision_note": note, "source": "vision", "camera_id": camera_id, "weight": weight,
    }
    r = requests.post(url, headers=headers, json=payload, timeout=30)
    r.raise_for_status()


def write_result(key, result_data, checkpoint, camera_id, weight):
    """Write one result entry to DB. key = 'sg_jb' or 'jb_sg'."""
    d = result_data or {}
    status = str(d.get("status", "")).lower().strip()
    tag = f"{checkpoint}/{key} (cam {camera_id})"
    if status not in VALID:
        print(f"- {tag}: {status or 'missing'}, writing nothing")
        return 0
    note = str(d.get("note", "")).strip()[:280]
    try:
        insert_reading(checkpoint, key, status, note, camera_id, weight)
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
    print("\n=== WOODLANDS ===")
    imgs_wl = []
    cam2701_ok = False
    cam2702_ok = False

    # 2701: download + diagonal crop
    url_2701 = lta_links.get("2701")
    if url_2701:
        try:
            raw = download_image(url_2701)
            sg_jb_crop, jb_sg_crop = crop_2701(raw)
            imgs_wl.append(sg_jb_crop)   # Image 1
            imgs_wl.append(jb_sg_crop)   # Image 2
            cam2701_ok = True
            print("- 2701: downloaded + cropped into 2 halves")
        except Exception as e:
            print(f"- 2701: crop failed ({e})")
    else:
        print("- 2701: no image link from LTA")

    # 2702: download normally
    url_2702 = lta_links.get("2702")
    if url_2702:
        try:
            imgs_wl.append(download_image(url_2702))  # Image 3
            cam2702_ok = True
            print("- 2702: downloaded")
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
                # 2701 crops: result keyed as "2701_sg_jb" and "2701_jb_sg"
                attempted += 2
                written += write_result("sg_jb", result.get("2701_sg_jb"),
                                        "woodlands", "2701", 0.7)
                written += write_result("jb_sg", result.get("2701_jb_sg"),
                                        "woodlands", "2701", 0.7)
            if cam2702_ok:
                # 2702: normal keyed result
                cam_r = result.get("2702") or {}
                for d in DIRECTIONS:
                    attempted += 1
                    written += write_result(d, cam_r.get(d),
                                            "woodlands", "2702", 0.3)
    else:
        print("- woodlands: no images, skipping")

    time.sleep(5)

    # ===== TUAS =====
    print("\n=== TUAS ===")
    imgs_tu = []
    cam4703_ok = False
    cam4713_ok = False

    # 4703: download + polyline crop into two halves
    url_4703 = lta_links.get("4703")
    if url_4703:
        try:
            raw = download_image(url_4703)
            sg_jb_crop, jb_sg_crop = crop_4703(raw)
            imgs_tu.append(sg_jb_crop)   # Image 1
            imgs_tu.append(jb_sg_crop)   # Image 2
            cam4703_ok = True
            print("- 4703: downloaded + cropped into 2 halves")
        except Exception as e:
            print(f"- 4703: crop failed ({e})")
    else:
        print("- 4703: no image link from LTA")

    # 4713: download normally
    url_4713 = lta_links.get("4713")
    if url_4713:
        try:
            imgs_tu.append(download_image(url_4713))  # Image 3
            cam4713_ok = True
            print("- 4713: downloaded")
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
                # 4703 crops: result keyed as "4703_sg_jb" and "4703_jb_sg"
                attempted += 2
                written += write_result("sg_jb", result.get("4703_sg_jb"),
                                        "tuas", "4703", 0.7)
                written += write_result("jb_sg", result.get("4703_jb_sg"),
                                        "tuas", "4703", 0.7)
            if cam4713_ok:
                # 4713: normal keyed result
                cam_r = result.get("4713") or {}
                for d in DIRECTIONS:
                    attempted += 1
                    written += write_result(d, cam_r.get(d),
                                            "tuas", "4713", 0.3)
    else:
        print("- tuas: no images, skipping")

    print(f"\nDone. {written}/{attempted} direction-readings written.")


if __name__ == "__main__":
    main()