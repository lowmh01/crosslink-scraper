import os
import requests
import re
import json
from playwright.sync_api import sync_playwright
from datetime import datetime, timezone
from dotenv import load_dotenv
load_dotenv()

SUPABASE_URL = os.getenv("SUPABASE_URL")
SUPABASE_KEY = os.getenv("SUPABASE_KEY")

results = {}
MID_RATE = None


def fetch_mid_rate():
    """拿中间价做 sanity check 用(ECB via Frankfurter)。失败不影响主流程,只是跳过校验。"""
    global MID_RATE
    try:
        r = requests.get(
            "https://api.frankfurter.dev/v1/latest?base=SGD&symbols=MYR", timeout=10)
        MID_RATE = r.json()["rates"]["MYR"]
        print(f"✓ Mid-market (ECB): {MID_RATE}")
    except Exception as e:
        MID_RATE = None
        print(f"✗ Mid-market unavailable, sanity check skipped: {e}")


def sanity_check(platform):
    """平台汇率偏离中间价 >3% 视为抓错,置 None(save 时会自动用上一笔补)。"""
    rate = results.get(platform, {}).get("rate")
    if rate is None or MID_RATE is None:
        return
    if abs(rate - MID_RATE) / MID_RATE > 0.03:
        print(
            f"⚠ {platform}: {rate} deviates >3% from mid {MID_RATE}, discarded")
        results[platform]["rate"] = None
        results[platform]["status"] = "sanity_failed"


def make_browser(p):
    browser = p.chromium.launch(headless=True)
    context = browser.new_context(
        user_agent="Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/122.0.0.0 Safari/537.36",
        viewport={"width": 1280, "height": 720},
        locale="en-SG",
        timezone_id="Asia/Singapore",
    )
    page = context.new_page()
    page.add_init_script(
        "Object.defineProperty(navigator, 'webdriver', {get: () => undefined})")
    return browser, page


def fetch_wise():
    try:
        with sync_playwright() as p:
            browser, page = make_browser(p)
            page.goto(
                "https://wise.com/us/currency-converter/sgd-to-myr-rate", timeout=20000)
            page.wait_for_timeout(4000)
            content = page.content()
            browser.close()

        matches = re.findall(r'(\d+\.\d{3,5})\s*MYR', content)
        rate = float(matches[0]) if matches else None
        results["wise"] = {"rate": rate, "fee": "Small fee", "status": "ok"}
        print(f"✓ Wise: {rate}")
    except Exception as e:
        results["wise"] = {"rate": None, "status": "error", "error": str(e)}
        print(f"✗ Wise: {e}")


def fetch_cimb():
    try:
        with sync_playwright() as p:
            browser, page = make_browser(p)
            page.goto("https://www.cimbclicks.com.sg/sgd-to-myr", timeout=30000)
            page.wait_for_timeout(6000)
            body = page.inner_text("body")
            browser.close()

        match = re.search(r'SGD\s*1\.00\s*=\s*MYR\s*(\d+\.\d{4})', body)
        rate = float(match.group(1)) if match else None
        results["cimb"] = {"rate": rate, "fee": "None", "status": "ok"}
        print(f"✓ CIMB: {rate}")
    except Exception as e:
        results["cimb"] = {"rate": None, "status": "error", "error": str(e)}
        print(f"✗ CIMB: {e}")


def fetch_western_union():
    try:
        with sync_playwright() as p:
            browser, page = make_browser(p)
            page.goto(
                "https://www.westernunion.com/sg/en/currency-converter/sgd-to-myr-rate.html", timeout=30000)
            page.wait_for_timeout(6000)
            body = page.inner_text("body")
            browser.close()

        rate = None
        for line in body.split("\n"):
            line = line.strip()
            if line and re.search(r'3\.\d{2,4}', line) and len(line) < 100:
                match = re.search(r'(\d+\.\d{4})', line)
                if match:
                    val = float(match.group(1))
                    if 3.0 < val < 4.0:
                        rate = val
                        break

        results["western_union"] = {
            "rate": rate, "fee": "~SGD 5", "status": "ok"}
        print(f"✓ Western Union: {rate}")
    except Exception as e:
        results["western_union"] = {"rate": None,
                                    "status": "error", "error": str(e)}
        print(f"✗ Western Union: {e}")


def fetch_panda_remit():
    try:
        with sync_playwright() as p:
            browser, page = make_browser(p)
            page.goto(
                "https://www.pandaremit.com/en/sgp/send-money-to-malaysia", timeout=30000)
            page.wait_for_timeout(8000)
            body = page.inner_text("body")
            browser.close()

        # 直接找 "3.XXXX MYR" 的格式
        match = re.search(r'(\d+\.\d{4})MYR', body)
        if match:
            rate = float(match.group(1))
        else:
            # fallback
            matches = re.findall(r'(\d+\.\d{4,5})', body)
            rate = next((float(m)
                        for m in matches if 3.0 < float(m) < 4.0), None)

        results["panda_remit"] = {"rate": rate,
                                  "fee": "Low fee", "status": "ok"}
        print(f"✓ Panda Remit: {rate}")
    except Exception as e:
        results["panda_remit"] = {"rate": None,
                                  "status": "error", "error": str(e)}
        print(f"✗ Panda Remit: {e}")


def fetch_instarem():
    try:
        with sync_playwright() as p:
            browser, page = make_browser(p)
            page.goto(
                "https://www.instarem.com/en-sg/send-money-to-malaysia/", timeout=30000)
            # 等到汇率真的被 JS 填进页面,而不是固定 sleep;
            # 超时也不抛,继续往下走,抓不到会自然落到 None
            try:
                page.wait_for_function(
                    r"() => /\d\.\d{4}\s*MYR/.test(document.body.innerText)",
                    timeout=15000
                )
            except Exception:
                pass
            body = page.inner_text("body")
            browser.close()

        rate = None
        # 优先:锚定在 "Our rate" 标签附近提取,
        # 避免抓到页面下方 "Compare with banks" 表格里别家银行的汇率
        m = re.search(r'Our rates?[\s\S]{0,80}?(\d\.\d{4,5})', body)
        if m:
            rate = float(m.group(1))
        else:
            m = re.search(r'(\d\.\d{4,5})\s*MYR', body)
            if m:
                rate = float(m.group(1))
        # 注意:这里刻意不做 3.0–4.0 的全文 fallback——
        # 这个页面下方有银行汇率比较表,乱抓会以 Instarem 的名义发布别家的汇率。
        # 抓不到就返回 None,save 时用上一笔补,比错数字安全。

        results["instarem"] = {"rate": rate, "fee": "Low fee", "status": "ok"}
        print(f"✓ Instarem: {rate}")
    except Exception as e:
        results["instarem"] = {"rate": None,
                               "status": "error", "error": str(e)}
        print(f"✗ Instarem: {e}")


def save_to_supabase():
    try:
        url = f"{SUPABASE_URL}/rest/v1/exchange_rates"
        headers = {
            "apikey": SUPABASE_KEY,
            "Authorization": f"Bearer {SUPABASE_KEY}",
            "Content-Type": "application/json",
            "Prefer": "return=minimal"
        }

        # 先拿上一筆數據
        prev_res = requests.get(
            f"{url}?order=fetched_at.desc&limit=1",
            headers=headers
        )
        prev = prev_res.json()[
            0] if prev_res.status_code == 200 and prev_res.json() else {}

        # 哪個是 None 就用上一筆補
        def fallback(platform):
            val = results.get(platform, {}).get("rate")
            if val is None:
                return prev.get(platform)
            return val

        row = {
            "fetched_at": datetime.now(timezone.utc).isoformat(),
            "wise": fallback("wise"),
            "cimb": fallback("cimb"),
            "western_union": fallback("western_union"),
            "panda_remit": fallback("panda_remit"),
            "instarem": fallback("instarem"),
        }

        res = requests.post(url, json=row, headers=headers)
        if res.status_code in [200, 201]:
            print(f"\n✓ Saved to Supabase: {row}")
        else:
            print(f"\n✗ Supabase error: {res.status_code} {res.text}")
    except Exception as e:
        print(f"\n✗ Supabase error: {e}")


if __name__ == "__main__":
    print(
        f"\nFetching rates — {datetime.now(timezone.utc).strftime('%Y-%m-%d %H:%M')} UTC\n")
    fetch_mid_rate()
    fetch_wise()
    fetch_cimb()
    fetch_western_union()
    fetch_panda_remit()
    fetch_instarem()

    # 全平台 sanity check:偏离中间价 >3% 的一律丢弃
    for platform in ["wise", "cimb", "western_union", "panda_remit", "instarem"]:
        sanity_check(platform)

    print("\n── Results ──")
    print(json.dumps(results, indent=2))
    save_to_supabase()
