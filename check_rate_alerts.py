import os
import asyncio
import aiohttp
from supabase import create_client

BOT_TOKEN = os.environ["TELEGRAM_BOT_TOKEN"]
API = f"https://api.telegram.org/bot{BOT_TOKEN}"
sb = create_client(os.environ["SUPABASE_URL"],
                   os.environ["SUPABASE_KEY"])

BOT_USERNAME = "jbsglink_bot"
GROUP = "@jbsglink"

BATCH_SIZE = 25  # per second, Telegram limit is 30


async def is_member(session, chat_id):
    try:
        async with session.post(f"{API}/getChatMember",
                                json={"chat_id": GROUP, "user_id": chat_id}) as r:
            data = await r.json()
            status = data.get("result", {}).get("status", "")
            return status in ("member", "administrator", "creator")
    except Exception:
        return True


async def send(session, chat_id, text):
    await session.post(f"{API}/sendMessage",
                       json={"chat_id": chat_id, "text": text,
                             "parse_mode": "HTML",
                             "disable_web_page_preview": True})


async def process_alert(session, a, best_rate):
    cid = a["chat_id"]

    if not await is_member(session, cid):
        await send(session, cid,
                   "Your rate alert has been paused · 你的汇率提醒已暂停\n\n"
                   "Rejoin our group to continue receiving alerts.\n"
                   "重新加入群组即可恢复提醒。\n\n"
                   f"https://t.me/{GROUP.replace('@', '')}\n\n"
                   "After rejoining, send /start to reactivate.\n"
                   "加入后发送 /start 重新启用。")

        sb.table("telegram_alerts") \
            .update({"is_active": False}) \
            .eq("id", a["id"]).execute()
        return "paused"

    text = (
        f"CIMB SGD → MYR hit your target\n\n"
        f"CIMB rate: <b>{best_rate:.4f}</b>\n"
        f"Your target: {float(a['target_rate']):.4f}\n\n"
        f"jbsglink.com/exchange-rate\n\n"
        f"Change target · 直接输入新汇率，如 3.20\n"
        f"Send /stop to stop · 发送 /stop 停止通知\n\n"
        f"———\n"
        f"觉得实用？转发给在 SG 打工的朋友和家人\n"
        f"Free rate alert → @{BOT_USERNAME}"
    )
    await send(session, cid, text)
    return "sent"


async def main():
    latest = sb.table("exchange_rates") \
        .select("cimb, fetched_at") \
        .order("fetched_at", desc=True) \
        .limit(1).execute().data

    if not latest:
        return

    best_rate = latest[0].get("cimb")
    if not best_rate:
        return
    best_rate = float(best_rate)

    alerts = sb.table("telegram_alerts") \
        .select("id, chat_id, target_rate") \
        .eq("is_active", True) \
        .eq("direction", "above") \
        .lte("target_rate", best_rate) \
        .execute().data

    if not alerts:
        print(f"CIMB rate: {best_rate:.4f} | No alerts triggered")
        return

    sent = 0
    paused = 0

    async with aiohttp.ClientSession() as session:
        for i in range(0, len(alerts), BATCH_SIZE):
            batch = alerts[i:i + BATCH_SIZE]
            results = await asyncio.gather(
                *[process_alert(session, a, best_rate) for a in batch]
            )
            sent += results.count("sent")
            paused += results.count("paused")

            # Rate limit: wait 1 second between batches
            if i + BATCH_SIZE < len(alerts):
                await asyncio.sleep(1)

    print(
        f"CIMB rate: {best_rate:.4f} | Sent: {sent} | Paused: {paused} | Total: {len(alerts)}")


if __name__ == "__main__":
    asyncio.run(main())
