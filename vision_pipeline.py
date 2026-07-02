"""
vision_pipeline.py  (Gemini Flash-Lite, 2-per-call batched by checkpoint)
---------------------------------------------------------------------------
Each checkpoint has two cameras. We send BOTH images in a single Gemini call
(grouped by checkpoint), cutting RPD in half:  4 calls → 2 calls per run.

  Woodlands: 2701 + 2702  → 1 API call
  Tuas:      4703 + 4713  → 1 API call

Gemini returns a JSON keyed by camera_id. We unpack and write each direction
as a separate row, exactly as before.

Honesty rule: if an image can't be fetched, or a direction comes back as
"unknown" / unparseable, we write NOTHING for it.

Required environment variables (GitHub Actions secrets):
  LTA_API_KEY            - LTA DataMall AccountKey
  GEMINI_API_KEY         - Google AI Studio key
  SUPABASE_URL           - e.g. https://xxxx.supabase.co
  SUPABASE_SERVICE_KEY   - Supabase service_role key
"""

import base64
import json
import os
import sys
import time
import requests

CAMERAS = [
    {"camera_id": "2701", "checkpoint": "woodlands", "weight": 0.7,
     "anchor": "Camera 2701: The yellow signs indicate DESTINATION — where those vehicles are heading TO.\n- 'WOODLANDS' sign (near the camera, right/bottom of frame): these vehicles are heading TO Singapore = jb_sg.\n- 'JOHOR' sign (far from camera, left/top, along the water): these vehicles are heading TO Johor = sg_jb."},
    {"camera_id": "2702", "checkpoint": "woodlands", "weight": 0.3,
     "anchor": "Camera 2702: The yellow signs indicate DESTINATION — where those vehicles are heading TO.\n- 'BKE' sign (left carriageway): these vehicles are heading TO Singapore via BKE = jb_sg.\n- 'CAUSEWAY' sign (right carriageway): these vehicles are heading TO Johor via the Causeway = sg_jb."},
    {"camera_id": "4703", "checkpoint": "tuas", "weight": 0.7,
     "anchor": "Camera 4703: The curving ramp beside the yellow 'JOHOR' sign carries traffic towards Johor = sg_jb. The OTHER road visible in the upper-right area of the frame, running along the bridge toward the checkpoint buildings, carries traffic towards Singapore = jb_sg. Do NOT count trucks or vehicles parked in staging areas beside the road — only judge vehicles actually on the travel lanes of each carriageway."},
    {"camera_id": "4713", "checkpoint": "tuas", "weight": 0.3,
     "anchor": "Camera 4713: The yellow signs indicate DESTINATION — where those vehicles are heading TO.\n- 'AYE' sign (left carriageway): these vehicles are heading TO Singapore via AYE = jb_sg.\n- 'JOHOR' sign (right carriageway): these vehicles are heading TO Johor = sg_jb."},
]

# Group cameras by checkpoint for batching
CHECKPOINTS = {}
for _c in CAMERAS:
    CHECKPOINTS.setdefault(_c["checkpoint"], []).append(_c)

LTA_IMAGES_URL = "https://datamall2.mytransport.sg/ltaodataservice/Traffic-Imagesv2"

GEMINI_MODEL = "gemini-3.1-flash-lite"
GEMINI_URL = (
    f"https://generativelanguage.googleapis.com/v1beta/models/"
    f"{GEMINI_MODEL}:generateContent"
)

VALID = {"clear", "moderate", "heavy"}
DIRECTIONS = ("sg_jb", "jb_sg")


def build_pair_prompt(cams):
    """Build a prompt for 2 camera images from the same checkpoint."""
    anchors = "\n".join(c["anchor"] for c in cams)
    cam_ids = [c["camera_id"] for c in cams]
    return (
        "You are given 2 live LTA traffic camera images from the same "
        "Singapore-Malaysia checkpoint. The images are provided in this exact "
        f"order: Camera {cam_ids[0]} (Image 1), Camera {cam_ids[1]} (Image 2).\n\n"
        "Here are the orientation guidelines for each camera:\n"
        f"{anchors}\n\n"
        "For EACH camera, use the yellow LTA direction signs to tell the two "
        "directions apart, then judge congestion SEPARATELY for both "
        "directions (sg_jb and jb_sg):\n"
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
        "Respond with a JSON object ONLY, keyed by camera ID:\n"
        "{\n"
        f'  "{cam_ids[0]}": {{"sg_jb": {{"status": "...", "note": "..."}}, "jb_sg": {{"status": "...", "note": "..."}}}},\n'
        f'  "{cam_ids[1]}": {{"sg_jb": {{"status": "...", "note": "..."}}, "jb_sg": {{"status": "...", "note": "..."}}}}\n'
        "}"
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


def classify_pair(image_bytes_list, cams):
    """Send 2 images in one call. Return {camera_id: {sg_jb:{},jb_sg:{}}} or None."""
    parts = []
    for img_bytes in image_bytes_list:
        b64 = base64.standard_b64encode(img_bytes).decode("utf-8")
        parts.append({"inline_data": {"mime_type": "image/jpeg", "data": b64}})
    parts.append({"text": build_pair_prompt(cams)})

    body = {
        "contents": [{"parts": parts}],
        "generationConfig": {"maxOutputTokens": 600, "responseMimeType": "application/json"},
    }
    r = requests.post(
        GEMINI_URL,
        params={"key": os.environ["GEMINI_API_KEY"]},
        json=body,
        timeout=60,
    )
    if r.status_code == 429:
        for attempt in range(1, 4):
            wait = 10 * attempt
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
        print("  ! 429 persisted after 3 retries — skipping this checkpoint")
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
        print(f"  ! could not parse model output: {text[:200]}")
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
        lta_links = get_camera_images()
    except Exception as e:
        print(f"FATAL: could not fetch LTA images: {e}")
        sys.exit(1)

    written = 0
    attempted = 0

    for cp_name, cams in CHECKPOINTS.items():
        print(f"\n=== {cp_name.upper()} ({len(cams)} cameras) ===")

        # Download images for this checkpoint
        imgs = []
        active_cams = []
        for cam in cams:
            cid = cam["camera_id"]
            url = lta_links.get(cid)
            if not url:
                print(f"- cam {cid}: no image link from LTA, skipping")
                continue
            try:
                img_bytes = requests.get(url, timeout=30).content
                imgs.append(img_bytes)
                active_cams.append(cam)
            except Exception as e:
                print(f"- cam {cid}: image download failed ({e}), skipping")

        if not active_cams:
            print(f"- {cp_name}: no images available, skipping checkpoint")
            continue

        # If only 1 camera available, still send as a pair prompt (works fine)
        # but the other camera just won't appear in the response
        try:
            result = classify_pair(imgs, active_cams)
        except Exception as e:
            print(f"- {cp_name}: classify failed ({e}), skipping")
            result = None

        # Delay between checkpoints to avoid RPM limit
        time.sleep(5)

        if not result:
            print(f"- {cp_name}: not classifiable, writing nothing")
            continue

        # Unpack per-camera results
        for cam in active_cams:
            cid = cam["camera_id"]
            cam_result = result.get(cid)
            if not cam_result:
                print(f"- {cp_name} cam {cid}: missing in Gemini response")
                continue

            for direction in DIRECTIONS:
                attempted += 1
                d = cam_result.get(direction) or {}
                status = str(d.get("status", "")).lower().strip()
                tag = f"{cp_name}/{direction} (cam {cid}, w={cam['weight']})"
                if status not in VALID:
                    print(f"- {tag}: {status or 'missing'}, writing nothing")
                    continue
                note = str(d.get("note", "")).strip()[:280]
                try:
                    insert_reading(cp_name, direction, status,
                                   note, cid, cam["weight"])
                    written += 1
                    print(f"- {tag}: {status} - {note}")
                except Exception as e:
                    print(f"- {tag}: DB insert failed ({e})")

    print(f"\nDone. {written}/{attempted} direction-readings written.")


if __name__ == "__main__":
    main()
