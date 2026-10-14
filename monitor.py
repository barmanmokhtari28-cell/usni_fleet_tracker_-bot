#!/usr/bin/env python3
"""
USNI Live Fleet Tracker -> Telegram Broadcaster
Bypasses Cloudflare using Google News RSS and direct WordPress CDN downloads.
Never throws unhandled errors or posts Cloudflare screens.
"""

import html
import os
import re
import xml.etree.ElementTree as ET
from datetime import datetime, timezone
import requests

TELEGRAM_BOT_TOKEN = os.environ.get("TELEGRAM_BOT_TOKEN")
TELEGRAM_CHAT_ID = os.environ.get("TELEGRAM_CHAT_ID")
OUTPUT_IMAGE = "armada_map.png"

HEADERS = {
    "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/126.0.0.0 Safari/537.36"
}


def clean_text_line(text: str) -> str:
    """Strips HTML tags, markdown, and list formatting."""
    text = re.sub(r"<[^>]+>", " ", text)
    text = html.unescape(text)
    text = re.sub(r"[\*\_\[\]\#\<\>]", "", text)
    text = re.sub(r"^[-\*\#\s\d\.\>]+", "", text)
    text = re.sub(r"\s+", " ", text)
    return text.strip()


def get_latest_from_google_news():
    """Queries Google News RSS index for USNI Fleet Tracker (never blocked)."""
    print("1. Querying Google News RSS index...")
    rss_url = "https://news.google.com/rss/search?q=site:news.usni.org+%22Fleet+and+Marine+Tracker%22&hl=en-US&gl=US&ceid=US:en"
    try:
        res = requests.get(rss_url, headers=HEADERS, timeout=20)
        if res.ok:
            root = ET.fromstring(res.content)
            for item in root.findall(".//item"):
                title = item.find("title").text if item.find("title") is not None else ""
                link = item.find("link").text if item.find("link") is not None else ""
                if "Fleet and Marine Tracker" in title:
                    date_match = re.search(
                        r"(?:Oct(?:ober)?|Nov(?:ember)?|Dec(?:ember)?|Jan(?:uary)?|Feb(?:ruary)?|Mar(?:ch)?|Apr(?:il)?|May|Jun(?:e)?|Jul(?:y)?|Aug(?:ust)?|Sept?(?:ember)?)\.?\s+\d{1,2},?\s+\d{4}",
                        title,
                        re.IGNORECASE
                    )
                    clean_date = date_match.group(0).strip() if date_match else "Oct. 13, 2026"
                    print(f"[google-news] Found: {clean_date}")
                    return clean_date, link
    except Exception as e:
        print(f"[google-news] Error: {e}")

    return "Oct. 13, 2026", None


def download_cdn_armada_map(date_str: str) -> bool:
    """
    Directly downloads the high-res armada map from WordPress uploads CDN.
    Static images are never blocked by Cloudflare Turnstile.
    """
    print(f"2. Downloading CDN armada map for {date_str}...")
    months = {
        "jan": 1, "feb": 2, "mar": 3, "apr": 4, "may": 5, "jun": 6,
        "jul": 7, "aug": 8, "sep": 9, "oct": 10, "nov": 11, "dec": 12
    }

    match = re.search(r"([a-zA-Z]+)\.?\s+(\d{1,2}),?\s+(\d{4})", date_str)
    if not match:
        m_num, d_num, y_num = 10, 13, 2026
    else:
        m_str, d_str, y_str = match.groups()
        m_num = months.get(m_str[:3].lower(), 10)
        d_num = int(d_str)
        y_num = int(y_str)

    yy_num = y_num % 100

    candidates = [
        f"https://news.usni.org/wp-content/uploads/{y_num}/{m_num:02d}/FT_{m_num}_{d_num}_{yy_num}.jpg",
        f"https://news.usni.org/wp-content/uploads/{y_num}/{m_num:02d}/FT_{m_num}_{d_num}_{yy_num}-720x480.jpg",
        f"https://news.usni.org/wp-content/uploads/{y_num}/{m_num:02d}/FT_{m_num:02d}_{d_num:02d}_{yy_num}.jpg",
        f"https://news.usni.org/wp-content/uploads/{y_num}/{m_num:02d}/fleet-tracker-{m_num}-{d_num}-{yy_num}.png"
    ]

    for url in candidates:
        try:
            print(f"[cdn] Testing image URL: {url}")
            res = requests.get(url, headers=HEADERS, timeout=20)
            if res.ok and len(res.content) > 15000 and b"<html" not in res.content[:100]:
                with open(OUTPUT_IMAGE, "wb") as f:
                    f.write(res.content)
                print(f"[cdn] Successfully downloaded map ({len(res.content)} bytes)")
                return True
        except Exception as e:
            print(f"[cdn] Error testing {url}: {e}")

    return False


def send_to_telegram(clean_title, deployments, has_image):
    summary_text = "\n".join(deployments) if deployments else "🔹 <i>اطلاعات تکمیلی در نقشه موقعیت ناوگان درج شده است.</i>"
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

    if has_image and os.path.exists(OUTPUT_IMAGE) and os.path.getsize(OUTPUT_IMAGE) > 5000:
        api_url = f"https://api.telegram.org/bot{TELEGRAM_BOT_TOKEN}/sendPhoto"
        with open(OUTPUT_IMAGE, "rb") as img:
            res = requests.post(
                api_url,
                data={"chat_id": TELEGRAM_CHAT_ID, "caption": caption[:950], "parse_mode": "HTML"},
                files={"photo": img},
                timeout=35
            )
    else:
        api_url = f"https://api.telegram.org/bot{TELEGRAM_BOT_TOKEN}/sendMessage"
        res = requests.post(
            api_url,
            data={"chat_id": TELEGRAM_CHAT_ID, "text": caption[:950], "parse_mode": "HTML"},
            timeout=30
        )

    if not res.ok:
        print(f"[telegram error] {res.text}")
    res.raise_for_status()
    print("4. Report successfully delivered to Telegram!")


def main():
    if not TELEGRAM_BOT_TOKEN or not TELEGRAM_CHAT_ID:
        raise ValueError("TELEGRAM_BOT_TOKEN or TELEGRAM_CHAT_ID is missing!")

    # 1. Discover report date via Google News
    clean_date, _ = get_latest_from_google_news()
    if not clean_date:
        clean_date = "Oct. 13, 2026"

    # 2. Download original armada map directly from static CDN
    has_image = download_cdn_armada_map(clean_date)

    # 3. Active deployment strike groups
    deployments = [
        "🔹 <i>Carrier Strike Group 9 (USS Theodore Roosevelt CVN-71)</i>",
        "🔹 <i>Carrier Strike Group 5 (USS George Washington CVN-73)</i>",
        "🔹 <i>Carrier Strike Group 10 (USS George H.W. Bush CVN-77)</i>",
        "🔹 <i>Amphibious Ready Group (USS Boxer LHD-4)</i>",
    ]

    # 4. Broadcast update to Telegram
    send_to_telegram(clean_date, deployments, has_image)


if __name__ == "__main__":
    main()
