from playwright.sync_api import sync_playwright

with sync_playwright() as p:
    browser = p.chromium.launch(headless=False)
    page = browser.new_page()
    page.goto("https://www.pandaremit.com/en/sgp/send-money-to-malaysia")
    page.wait_for_timeout(8000)
    body = page.inner_text("body")
    print(body[:5000])
    browser.close()
