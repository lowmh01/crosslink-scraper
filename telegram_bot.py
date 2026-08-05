import os
import requests
from supabase import create_client

BOT_TOKEN = os.environ["TELEGRAM_BOT_TOKEN"]
API = f"https://api.telegram.org/bot{BOT_TOKEN}"
sb = create_client(os.environ["SUPABASE_URL"],
                   os.environ["SUPABASE__KEY"])


def process_updates():
    """Poll for new messages and handle commands."""
    r = requests.get(f"{API}/getUpdates", params={"timeout": 0})
    updates = r.json().get("result", [])

    for u in updates:
        msg = u.get("message", {})
        chat_id = msg.get("chat", {}).get("id")
        text = (msg.get("text") or "").strip()

        if not chat_id:
            continue

        if text == "/start":
            send(chat_id,
                 "JB-SG Link Rate Alert\n\n"
                 "Set a target SGD→MYR rate and get notified.\n\n"
                 "Usage:\n"
                 "/alert 3.42  — notify when best rate ≥ 3.42\n"
                 "/status      — check your active alerts\n"
                 "/stop        — cancel all alerts")

        elif text.startswith("/alert"):
            parts = text.split()
            if len(parts) != 2:
                send(chat_id, "Format: /alert 3.42")
                continue
            try:
                target = round(float(parts[1]), 4)
                assert 3.0 <= target <= 4.0
            except:
                send(chat_id, "Enter a rate between 3.0 and 4.0")
                continue

            sb.table("telegram_alerts").upsert({
                "chat_id": chat_id,
                "target_rate": target,
                "direction": "above",
                "is_active": True,
                "triggered_at": None,
            }, on_conflict="chat_id,target_rate,direction").execute()

            send(chat_id, f"Alert set: notify when best rate ≥ {target:.4f}")

        elif text == "/status":
            rows = sb.table("telegram_alerts") \
                .select("target_rate, is_active, triggered_at") \
                .eq("chat_id", chat_id) \
                .eq("is_active", True) \
                .execute().data
            if not rows:
                send(chat_id, "No active alerts. Use /alert 3.42 to set one.")
            else:
                lines = [f"  {r['target_rate']:.4f}" for r in rows]
                send(chat_id, "Active alerts:\n" + "\n".join(lines))

        elif text == "/stop":
            sb.table("telegram_alerts") \
                .update({"is_active": False}) \
                .eq("chat_id", chat_id).execute()
            send(chat_id, "All alerts cancelled.")

    # Acknowledge processed updates
    if updates:
        last_id = updates[-1]["update_id"]
        requests.get(f"{API}/getUpdates",
                     params={"offset": last_id + 1, "timeout": 0})


def send(chat_id, text):
    requests.post(f"{API}/sendMessage",
                  json={"chat_id": chat_id, "text": text,
                        "parse_mode": "HTML"})


if __name__ == "__main__":
    process_updates()
