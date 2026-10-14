#!/usr/bin/env python3
"""
USNI Live Fleet Tracker -> Telegram Broadcaster
Fault-tolerant version with a multi-layered fetch strategy (Proxy -> ScraperAPI -> Stealth Browser).
"""

import html
import os
import re
from datetime import datetime, timezone
import requests
from playwright.sync_api import sync_playwright

TELEGRAM_BOT_TOKEN = os.environ.get("TELEGRAM_BOT_TOKEN")
TELEGRAM_CHAT_ID = os.environ.get("TELEGRAM_CHAT_ID")
SCRAPERAPI_KEY = os.environ.get("SCRAPERAPI_KEY")

CATEGORY_URL = "https://news.usni.org/category/fleet-tracker"
OUTPUT_IMAGE = "armada_map.png"

HEADERS = {
    "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/126.0.0.0 Safari/537.36"
}


def strip_html_tags(text: str) -> str:
    text = re.sub(r"<[^>]+>", " ", text)
    text = html.unescape(text)
    text = re.sub(r"[\*\_\[\]\#]", "", text)
    text = re.sub(r"\s+", " ", text)
    return text.strip()


def fetch_via_proxy(target_url: str) -> str | None:
    """Tries Jina proxy first."""
    try:
        jina_url = f"https://r.jina.ai/{target_url}"
        res = requests.get(jina_url, headers=HEADERS, timeout=25)
        if res.ok and len(res.text) > 300 and "Just a moment" not in res.text:
            return res.text
    except Exception as e:
        print(f"[proxy] Jina failed: {e}")

    if SCRAPERAPI_KEY:
        try:
            s_url = f"https://api.scraperapi.com/?api_key={SCRAPERAPI_KEY}&url={target_url}"
            res = requests.get(s_url, timeout=30)
            if res.ok and len(res.text) > 300:
                return res.text
            else:
                print(f"[proxy] ScraperAPI returned non-OK status: {res.status_code}")
        except Exception as e:
            print(f"[proxy] ScraperAPI failed: {e}")
    return None


def get_latest_report_via_playwright():
    """Ultra-reliable fallback using a stealth browser."""
    print("[fallback] Launching stealth browser to bypass blocks...")
    with sync_playwright() as p:
        browser = p.chromium.launch(
            headless=True,
            args=["--disable-blink-features=AutomationControlled"]
        )
        context = browser.new_context(user_agent=HEADERS["User-Agent"])
        page = context.new_page()

        page.goto(CATEGORY_URL, wait_until="domcontentloaded", timeout=60000)
        page.wait_for_timeout(3000)

        # Find the link to the newest report
        latest_url = page.locator('a[href*="fleet-and-marine-tracker"]').first.get_attribute("href")
        if not latest_url:
            raise Exception("Playwright could not find the latest report link.")
        print(f"[fallback] Found latest report via browser: {latest_url}")
        
        page.goto(latest_url, wait_until="domcontentloaded", timeout=60000)
        page.wait_for_timeout(4000)

        # Extract title
        raw_title = page.title().split(" - ")[0].strip()
        clean_title = raw_title.replace("USNI News Fleet and Marine Tracker:", "").strip()

        # Download map image
        map_url = page.locator('img[src*="/wp-content/uploads/"]').first.get_attribute("src")
        image_downloaded = False
        if map_url:
            img_res = requests.get(map_url, headers=HEADERS, timeout=25)
            if img_res.ok:
                with open(OUTPUT_IMAGE, "wb") as f:
                    f.write(img_res.content)
                image_downloaded = True
        
        # Extract deployments
        body_text = page.inner_text("article")
        deployments = []
        for line in body_text.splitlines():
            cleaned_line = strip_html_tags(line)
            if any(k in cleaned_line.lower() for k in ["carrier strike group", "amphibious ready group", "uss "]):
                cleaned_line = re.sub(r"^[-\*\#\s\d\.]+", "", cleaned_line).strip()
                if 10 < len(cleaned_line) < 140:
                    deployments.append(f"🔹 <i>{html.escape(cleaned_line)}</i>")

        browser.close()
        return clean_title, deployments[:4], image_downloaded


def get_latest_report():
    print("1. Fetching live Fleet Tracker via proxy...")
    archive_text = fetch_via_proxy(CATEGORY_URL)
    if not archive_text:
        return get_latest_report_via_playwright()

    links = re.findall(r"https://news\.usni\.org/\d{4}/\d{2}/\d{2}/[a-zA-Z0-9\-]+", archive_text)
    tracker_links = [l for l in links if "fleet-and-marine-tracker" in l] or links
    if not tracker_links:
        raise Exception("Proxy could not find any Fleet Tracker article URLs.")

    latest_url = tracker_links[0]
    print(f"2. Newest report identified: {latest_url}")
    article_text = fetch_via_proxy(latest_url)
    if not article_text:
        return get_latest_report_via_playwright()

    # (Parsing logic is the same as the fallback, but on raw text)
    title_match = re.search(r"Title:\s*(.+)", article_text)
    raw_title = title_match.group(1).strip() if title_match else "Fleet Tracker"
    clean_title = raw_title.replace("USNI News Fleet and Marine Tracker:", "").strip()

    img_matches = re.findall(r"https?://[^\s\)\"\'\<\>]+\.(?:png|jpg|jpeg)", article_text)
    map_url = next((img for img in img_matches if "wp-content/uploads" in img and "logo" not in img), None)
    
    image_downloaded = False
    if map_url:
        img_res = requests.get(map_url, headers=HEADERS, timeout=25)
        if img_res.ok:
            with open(OUTPUT_IMAGE, "wb") as f: f.write(img_res.content)
            image_downloaded = True

    deployments = []
    for line in article_text.splitlines():
        cleaned_line = strip_html_tags(line)
        if any(k in cleaned_line.lower() for k in ["carrier strike group", "amphibious ready group", "uss "]):
            cleaned_line = re.sub(r"^[-\*\#\s\d\.]+", "", cleaned_line).strip()
            if 10 < len(cleaned_line) < 140:
                deployments.append(f"🔹 <i>{html.escape(cleaned_line)}</i>")

    return clean_title, deployments[:4], image_downloaded


def build_safe_caption(clean_title, deployments):
    # (Caption logic unchanged)
    summary_text = "\n".join(deployments) if deployments else "🔹 <i>اطلاعات تکمیلی در گزارش USNI درج شده است.</i>"
    now_str = datetime.now(timezone.utc).strftime("%Y-%m-%d %H:%M UTC")
    caption = (
        f"🧭 <b>آخرین موقعیت ناوگان و ناوهای جنگی آمریکا</b>\n"
        f"<blockquote><b>گزارش:</b> {html.escape(clean_title)}\n"
        f"🕒 <b>به‌روزرسانی:</b> {now_str}</blockquote>\n\n"
        f"📍 <b>موقعیت ناوهای هواپیمابر و گروه‌های رزمی:</b>\n"
        f"{summary_text}\n\n"
        f"📫 @secretollah\n"
        f"#USNI\n"
        f"#ناو"
    )
    return caption[:1024]


def send_to_telegram(clean_title, deployments, has_image):
    # (Sending logic unchanged)
    caption = build_safe_caption(clean_title, deployments)
    if has_image and os.path.exists(OUTPUT_IMAGE):
        with open(OUTPUT_IMAGE, "rb") as img:
            res = requests.post(
                f"https://api.telegram.org/bot{TELEGRAM_BOT_TOKEN}/sendPhoto",
                data={"chat_id": TELEGRAM_CHAT_ID, "caption": caption, "parse_mode": "HTML"},
                files={"photo": img}, timeout=30
            )
    else:
        res = requests.post(
            f"https://api.telegram.org/bot{TELEGRAM_BOT_TOKEN}/sendMessage",
            data={"chat_id": TELEGRAM_CHAT_ID, "text": caption, "parse_mode": "HTML"},
            timeout=30
        )
    if not res.ok:
        print(f"[telegram error] {res.status_code}: {res.text}")
    res.raise_for_status()
    print("4. Update successfully posted to Telegram.")


if __name__ == "__main__":
    if not TELEGRAM_BOT_TOKEN or not TELEGRAM_CHAT_ID:
        raise ValueError("TELEGRAM_BOT_TOKEN or TELEGRAM_CHAT_ID is missing!")
    
    title, deployments, has_image = get_latest_report()
    send_to_telegram(title, deployments, has_image)
