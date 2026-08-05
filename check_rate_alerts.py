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
    best_platform = "CIMB"
    best_rate = row.get("cimb")

    if not best_rate:
        return

    best_rate = float(best_rate)

    # Find alerts that should fire
    alerts = sb.table("telegram_alerts") \
        .select("id, chat_id, target_rate") \
        .eq("is_active", True) \
        .eq("direction", "above") \
        .lte("target_rate", best_rate) \
        .is_("triggered_at", "null") \
        .execute().data

    for a in alerts:
        text = (
            f"SGD → MYR rate hit your target\n\n"
            f"CIMB rate: <b>{best_rate:.4f}</b>\n"
            f"Your target: {float(a['target_rate']):.4f}\n\n"
            f"jbsglink.com/tools/exchange-rate"
        )
        requests.post(f"{API}/sendMessage",
                      json={"chat_id": a["chat_id"], "text": text,
                            "parse_mode": "HTML"})

        sb.table("telegram_alerts") \
            .update({"triggered_at": "now()"}) \
            .eq("id", a["id"]).execute()


if __name__ == "__main__":
    check_and_notify()
