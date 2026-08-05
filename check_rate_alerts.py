import os
import requests
from supabase import create_client

BOT_TOKEN = os.environ["TELEGRAM_BOT_TOKEN"]
API = f"https://api.telegram.org/bot{BOT_TOKEN}"
sb = create_client(os.environ["SUPABASE_URL"],
                   os.environ["SUPABASE_KEY"])


def check_and_notify():
    # Get latest best rate from your exchange_rates table
    latest = sb.table("exchange_rates") \
        .select("*") \
        .order("fetched_at", desc=True) \
        .limit(1).execute().data

    if not latest:
        return

    row = latest[0]
    # Pick the best rate across platforms
    platforms = {
        "Wise": row.get("wise"),
        "CIMB": row.get("cimb"),
        "Panda Remit": row.get("panda_remit"),
        "Instarem": row.get("instarem"),
        "Western Union": row.get("western_union"),
    }
    valid = {k: float(v) for k, v in platforms.items() if v}
    if not valid:
        return

    best_platform = max(valid, key=valid.get)
    best_rate = valid[best_platform]

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
            f"Best rate: <b>{best_rate:.4f}</b> via {best_platform}\n"
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
