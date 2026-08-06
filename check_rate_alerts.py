import os
import requests
from supabase import create_client

BOT_TOKEN = os.environ["TELEGRAM_BOT_TOKEN"]
API = f"https://api.telegram.org/bot{BOT_TOKEN}"
sb = create_client(os.environ["SUPABASE_URL"],
                   os.environ["SUPABASE_KEY"])

BOT_USERNAME = "jbsglink_bot"  # 改成你实际的 bot username
GROUP = "@jbsglink"  # 改成你实际的 group username


def is_member(chat_id):
    try:
        r = requests.post(f"{API}/getChatMember",
                          json={"chat_id": GROUP, "user_id": chat_id},
                          timeout=10)
        data = r.json()
        status = data.get("result", {}).get("status", "")
        return status in ("member", "administrator", "creator")
    except Exception:
        return True  # API error = don't block, assume member


def send(chat_id, text):
    requests.post(f"{API}/sendMessage",
                  json={"chat_id": chat_id, "text": text,
                        "parse_mode": "HTML",
                        "disable_web_page_preview": True})


def check_and_notify():
    latest = sb.table("exchange_rates") \
        .select("cimb, fetched_at") \
        .order("fetched_at", desc=True) \
        .limit(1).execute().data

    if not latest:
        return

    row = latest[0]
    best_rate = row.get("cimb")

    if not best_rate:
        return

    best_rate = float(best_rate)

    alerts = sb.table("telegram_alerts") \
        .select("id, chat_id, target_rate") \
        .eq("is_active", True) \
        .eq("direction", "above") \
        .lte("target_rate", best_rate) \
        .execute().data

    sent = 0
    paused = 0

    for a in alerts:
        cid = a["chat_id"]

        if not is_member(cid):
            send(cid,
                 "Your rate alert has been paused · 你的汇率提醒已暂停\n\n"
                 "Rejoin our group to continue receiving alerts.\n"
                 "重新加入群组即可恢复提醒。\n\n"
                 f"https://t.me/{GROUP.replace('@', '')}\n\n"
                 "After rejoining, send /start to reactivate.\n"
                 "加入后发送 /start 重新启用。")

            sb.table("telegram_alerts") \
                .update({"is_active": False}) \
                .eq("id", a["id"]).execute()

            paused += 1
            continue

        text = (
            f"CIMB SGD → MYR hit your target\n\n"
            f"CIMB rate: <b>{best_rate:.4f}</b>\n"
            f"Your target: {float(a['target_rate']):.4f}\n\n"
            f"jbsglink.com/exchange-rate\n\n"
            f"———\n"
            f"觉得实用？转发给在 SG 打工的朋友和家人\n"
            f"Free rate alert → @{BOT_USERNAME}"
        )
        send(cid, text)
        sent += 1

    print(
        f"CIMB rate: {best_rate:.4f} | Sent: {sent} | Paused (left group): {paused}")


if __name__ == "__main__":
    check_and_notify()
