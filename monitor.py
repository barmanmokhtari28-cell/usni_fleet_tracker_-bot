#!/usr/bin/env python3
"""
USNI Live Fleet Tracker -> Telegram Broadcaster
Fault-tolerant version with instant JavaScript evaluation and lazy-load support.
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
    """Strips HTML tags, markdown, and formatting characters."""
    text = re.sub(r"<[^>]+>", " ", text)
    text = html.unescape(text)
    text = re.sub(r"[\*\_\[\]\#]", "", text)
    text = re.sub(r"\s+", " ", text)
    return text.strip()


def get_latest_report_via_playwright():
    """Ultra-reliable browser automation with lazy-load support."""
    print("[browser] Launching stealth browser...")
    with sync_playwright() as p:
        browser = p.chromium.launch(
            headless=True,
            args=[
                "--disable-blink-features=AutomationControlled",
                "--no-sandbox",
                "--disable-dev-shm-usage"
            ]
        )
        context = browser.new_context(
            viewport={"width": 1440, "height": 900},
            user_agent=HEADERS["User-Agent"]
        )
        page = context.new_page()

        # 1. Open Fleet Tracker archive
        print("1. Navigating to Fleet Tracker archive...")
        page.goto(CATEGORY_URL, wait_until="domcontentloaded", timeout=60000)
        page.wait_for_timeout(3000)

        # 2. Extract latest article URL
        latest_url = page.evaluate("""() => {
            const links = Array.from(document.querySelectorAll('a[href]'));
            const tracker = links.find(a => a.href.includes('fleet-and-marine-tracker'));
            return tracker ? tracker.href : null;
        }""")

        if not latest_url:
            # Fallback to first article link
            latest_url = page.evaluate("""() => {
                const a = document.querySelector('article a, h2 a, h3 a');
                return a ? a.href : null;
            }""")

        if not latest_url:
            raise Exception("Could not find latest report link.")

        print(f"2. Navigating to latest report: {latest_url}")
        page.goto(latest_url, wait_until="domcontentloaded", timeout=60000)
        page.wait_for_timeout(3000)

        # Scroll down slightly to trigger lazy-loaded images
        page.evaluate("window.scrollBy(0, 400)")
        page.wait_for_timeout(2000)

        # 3. Extract title
        raw_title = page.title().split(" - ")[0].strip()
        clean_title = raw_title.replace("USNI News Fleet and Marine Tracker:", "").replace("USNI News Fleet and Marine Tracker", "").strip(" :-\t")
        if not clean_title or clean_title.lower() == "fleet tracker":
            date_match = re.search(r"/(\d{4})/(\d{2})/(\d{2})/", latest_url)
            clean_title = f"{date_match.group(1)}-{date_match.group(2)}-{date_match.group(3)}" if date_match else "Live Update"

        # 4. Extract armada map image using instant JavaScript (no 30s timeout)
        map_url = page.evaluate("""() => {
            // Check OpenGraph image meta tag first
            const og = document.querySelector('meta[property="og:image"]');
            if (og && og.content && !og.content.includes('logo') && !og.content.includes('avatar')) {
                return og.content;
            }
            // Check all images in the document (including lazy-loaded data-src)
            const imgs = Array.from(document.querySelectorAll('article img, .post img, img'));
            for (const img of imgs) {
                const src = img.currentSrc || img.src || img.getAttribute('data-src') || img.getAttribute('data-lazy-src') || '';
                if (src && (src.includes('uploads') || src.includes('wp-content')) && !src.includes('logo') && !src.includes('avatar') && !src.includes('icon')) {
                    return src;
                }
            }
            return null;
        }""")

        image_downloaded = False
        if map_url:
            try:
                if map_url.startswith("/"):
                    map_url = "https://news.usni.org" + map_url
                print(f"3. Downloading Armada Map: {map_url}")
                img_res = requests.get(map_url, headers=HEADERS, timeout=25)
                if img_res.ok and len(img_res.content) > 2000:
                    with open(OUTPUT_IMAGE, "wb") as f:
                        f.write(img_res.content)
                    image_downloaded = True
            except Exception as e:
                print(f"[image] Direct download error: {e}")

        # Fallback: if download failed, screenshot page viewport directly
        if not image_downloaded:
            print("[image] Taking direct screenshot fallback...")
            page.screenshot(path=OUTPUT_IMAGE)
            image_downloaded = True

        # 5. Extract ship & carrier movements safely
        body_text = page.evaluate("() => document.body ? document.body.innerText : ''")
        deployments = []
        for line in body_text.splitlines():
            cleaned_line = strip_html_tags(line)
            if any(k in cleaned_line.lower() for k in ["carrier strike group", "amphibious ready group", "uss "]):
                cleaned_line = re.sub(r"^[-\*\#\s\d\.]+", "", cleaned_line).strip()
                if 10 < len(cleaned_line) < 130 and cleaned_line not in deployments:
                    deployments.append(f"🔹 <i>{html.escape(cleaned_line)}</i>")

        browser.close()
        return clean_title, deployments[:4], image_downloaded


def build_safe_caption(clean_title, deployments):
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
    # Guarantee caption stays strictly under Telegram's 1024 character limit
    return caption[:950]


def send_to_telegram(clean_title, deployments, has_image):
    caption = build_safe_caption(clean_title, deployments)

    if has_image and os.path.exists(OUTPUT_IMAGE) and os.path.getsize(OUTPUT_IMAGE) > 1000:
        api_url = f"https://api.telegram.org/bot{TELEGRAM_BOT_TOKEN}/sendPhoto"
        with open(OUTPUT_IMAGE, "rb") as img:
            payload = {
                "chat_id": TELEGRAM_CHAT_ID,
                "caption": caption,
                "parse_mode": "HTML"
            }
            res = requests.post(api_url, data=payload, files={"photo": img}, timeout=30)
    else:
        api_url = f"https://api.telegram.org/bot{TELEGRAM_BOT_TOKEN}/sendMessage"
        payload = {
            "chat_id": TELEGRAM_CHAT_ID,
            "text": caption,
            "parse_mode": "HTML"
        }
        res = requests.post(api_url, data=payload, timeout=30)

    if not res.ok:
        print(f"[telegram error] {res.status_code}: {res.text}")
    res.raise_for_status()
    print("4. Update successfully posted to Telegram.")


def main():
    if not TELEGRAM_BOT_TOKEN or not TELEGRAM_CHAT_ID:
        raise ValueError("TELEGRAM_BOT_TOKEN or TELEGRAM_CHAT_ID is missing!")

    title, deployments, has_image = get_latest_report_via_playwright()
    send_to_telegram(title, deployments, has_image)


if __name__ == "__main__":
    main()
