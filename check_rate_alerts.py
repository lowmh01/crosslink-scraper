import os
import requests
from supabase import create_client

BOT_TOKEN = os.environ["TELEGRAM_BOT_TOKEN"]
API = f"https://api.telegram.org/bot{BOT_TOKEN}"
sb = create_client(os.environ["SUPABASE_URL"],
                   os.environ["SUPABASE_KEY"])


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

    # Find all active alerts where target has been reached
    alerts = sb.table("telegram_alerts") \
        .select("id, chat_id, target_rate") \
        .eq("is_active", True) \
        .eq("direction", "above") \
        .lte("target_rate", best_rate) \
        .execute().data

    for a in alerts:
        text = (
            f"CIMB SGD → MYR hit your target\n\n"
            f"CIMB rate: <b>{best_rate:.4f}</b>\n"
            f"Your target: {float(a['target_rate']):.4f}\n\n"
            f"You will keep getting notified while the rate stays above your target.\n"
            f"Send /stop to pause notifications.\n\n"
            f"jbsglink.com/tools/exchange-rate"
        )
        requests.post(f"{API}/sendMessage",
                      json={"chat_id": a["chat_id"], "text": text,
                            "parse_mode": "HTML"})

    if alerts:
        print(f"Notified {len(alerts)} alert(s), CIMB rate: {best_rate:.4f}")
    else:
        print(f"No alerts triggered, CIMB rate: {best_rate:.4f}")


if __name__ == "__main__":
    check_and_notify()
