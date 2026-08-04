"""
deals_scrape.py
爬取 promo 页面的文字内容，保存到一个 .txt 文件。
你拿这个文件的内容直接丢给 Gemini Pro 提取结构化 deals。

Usage:  python deals_scrape.py
Output: deals_raw_YYYY-MM-DD.txt
Needs:  pip install playwright && playwright install chromium
"""

import os
import asyncio
from datetime import datetime
from playwright.async_api import async_playwright

# ─── 你的 promo 页面清单，按需增减 ──────────────────────────────
URLS = [
    # F&B - JB
    ("McDonalds MY", "JB", "F&B", "https://www.mcdonalds.com.my/promotion"),
    ("KFC MY", "JB", "F&B", "https://www.kfc.com.my/promotions"),
    ("Burger King", "JB", "F&B", "https://www.burgerking.com.my/"),
    ("Secret Recipe", "JB", "F&B", "https://www.secretrecipe.com.my/"),
    ("Big Apple Donuts & Coffee", "JB", "F&B", "https://www.bigappledonuts.com/"),
    ("Pizza Hut MY", "JB", "F&B", "https://www.pizzahut.com.my"),
    ("Texas Chicken MY", "JB", "F&B", "https://texaschickenmalaysia.com/promotions/"),

    # F&B - SG
    ("McDonalds SG", "SG", "F&B", "https://www.mcdonalds.com.sg/promotions"),
    ("KFC SG", "SG", "F&B", "https://www.kfc.com.sg/promotions"),
    ("Starbucks SG", "SG", "F&B", "https://www.starbucks.com.sg/promotions"),

    # Shopping - JB
    ("Mid Valley Southkey", "JB", "Shopping",
     "https://www.midvalleysouthkey.com/deal/"),
    ("KSL City", "JB", "Shopping", "https://www.kslcity.com.my/promotions/"),
    ("City Square JB", "JB", "Shopping",
     "https://www.citysquarejb.com/events-promotions"),
    ("AEON Tebrau", "JB", "Shopping", "https://www.aeonretail.com.my/promotion"),

    # Shopping - SG
    ("VivoCity", "SG", "Shopping", "https://www.vivocity.com.sg/promotions"),
    ("HarbourFront Centre", "SG", "Shopping",
     "https://www.harbourfrontcentre.com.sg/promotions/"),
    ("JEM", "SG", "Shopping", "https://www.jem.sg/promotions"),

    # Lifestyle
    ("Grab MY", "JB", "Lifestyle", "https://www.grab.com/my/promotions/"),

    # TODO: 加更多
    # ("Name", "JB/SG", "Tag", "https://..."),
]

TODAY = datetime.now().strftime("%Y-%m-%d")
OUTPUT = f"deals_raw_{TODAY}.txt"


async def scrape_deals():
    results = []

    async with async_playwright() as p:
        browser = await p.chromium.launch(headless=True)
        context = await browser.new_context(
            viewport={"width": 1280, "height": 900},
            locale="en-MY",
        )

        for name, location, tag, url in URLS:
            page = await context.new_page()

            try:
                await page.goto(url, timeout=20000, wait_until="domcontentloaded")
                await page.wait_for_timeout(3000)

                # dismiss cookie banners
                for sel in ["[class*=cookie] button", "[class*=consent] button"]:
                    try:
                        btn = page.locator(sel).first
                        if await btn.is_visible(timeout=1000):
                            await btn.click()
                            await page.wait_for_timeout(500)
                    except:
                        pass

                # extract text from main content area
                text = await page.inner_text("body")

                # clean up: collapse whitespace, limit length
                lines = [l.strip() for l in text.splitlines() if l.strip()]
                cleaned = "\n".join(lines)

                # cap at 3000 chars per page to stay within Gemini context
                if len(cleaned) > 3000:
                    cleaned = cleaned[:3000] + "\n... (truncated)"

                results.append(
                    f"===== {name} =====\n"
                    f"Location: {location}\n"
                    f"Tag: {tag}\n"
                    f"URL: {url}\n"
                    f"Scraped: {TODAY}\n"
                    f"---\n"
                    f"{cleaned}\n"
                )
                print(f"✓ {name} ({len(cleaned)} chars)")

            except Exception as e:
                results.append(
                    f"===== {name} =====\n"
                    f"URL: {url}\n"
                    f"ERROR: {e}\n"
                )
                print(f"✗ {name} — {e}")
            finally:
                await page.close()

        await browser.close()

    # write everything to one file
    with open(OUTPUT, "w", encoding="utf-8") as f:
        f.write(f"JB-SG Link Deals Scrape — {TODAY}\n")
        f.write(f"Total sources: {len(URLS)}\n")
        f.write(f"{'=' * 50}\n\n")
        f.write("\n\n".join(results))

    print(f"\n完成 → {OUTPUT}")
    print(f"把这个文件的内容贴到 Gemini Pro，用以下 prompt:")
    print(f"─" * 40)
    print(GEMINI_PROMPT)


GEMINI_PROMPT = """
以下是从多个网站爬取的促销页面内容。请从中提取所有有效的 deals/优惠。

每个 deal 输出 JSON 格式:
{
  "title": "简短标题",
  "description": "一句话描述",
  "tag": "用每个 section 标注的 Tag",
  "location": "用每个 section 标注的 Location",
  "deal_price": "优惠价（没有就 null）",
  "original_price": "原价（没有就 null）",
  "discount_label": "折扣标签如 Buy 1 Free 1（没有就 null）",
  "starts_at": "YYYY-MM-DD（没有就 null）",
  "expires_at": "YYYY-MM-DD（没有就 null）",
  "cta_url": "用每个 section 标注的 URL"
}

规则:
- 只提取目前有效或即将开始的优惠
- 已过期的跳过
- 不确定的字段写 null
- 输出纯 JSON array
""".strip()


if __name__ == "__main__":
    asyncio.run(scrape_deals())
