#!/usr/bin/env python3
"""
USNI Live Fleet Tracker -> Telegram Broadcaster
Extracts clean plain text (no leaking HTML tags) and the direct armada map.
"""

import html
import os
import re
from datetime import datetime, timezone
import requests

TELEGRAM_BOT_TOKEN = os.environ.get("TELEGRAM_BOT_TOKEN")
TELEGRAM_CHAT_ID = os.environ.get("TELEGRAM_CHAT_ID")
SCRAPERAPI_KEY = os.environ.get("SCRAPERAPI_KEY")

CATEGORY_URL = "https://news.usni.org/category/fleet-tracker"
OUTPUT_IMAGE = "armada_map.png"

HEADERS = {
    "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/126.0.0.0 Safari/537.36"
}


def strip_html_tags(text: str) -> str:
    """Completely strips any HTML/markdown tags to prevent Telegram text leaks."""
    text = re.sub(r"<[^>]+>", " ", text)
    text = html.unescape(text)
    text = re.sub(r"[\*\_\[\]\#]", "", text)
    text = re.sub(r"\s+", " ", text)
    return text.strip()


def fetch_page_markdown(target_url: str) -> str:
    """Fetches clean live page content, bypassing Cloudflare."""
    try:
        jina_url = f"https://r.jina.ai/{target_url}"
        res = requests.get(jina_url, headers=HEADERS, timeout=25)
        if res.ok and len(res.text) > 300 and "Just a moment" not in res.text:
            return res.text
    except Exception as e:
        print(f"[fetch] Jina proxy error: {e}")

    if SCRAPERAPI_KEY:
        try:
            s_url = f"https://api.scraperapi.com/?api_key={SCRAPERAPI_KEY}&url={target_url}"
            res = requests.get(s_url, timeout=30)
            if res.ok and len(res.text) > 300:
                return res.text
        except Exception as e:
            print(f"[fetch] ScraperAPI error: {e}")

    res = requests.get(target_url, headers=HEADERS, timeout=25)
    res.raise_for_status()
    return res.text


def get_latest_report():
    print("Fetching live Fleet Tracker category page...")
    archive_text = fetch_page_markdown(CATEGORY_URL)

    # Locate the latest fleet tracker article link
    links = re.findall(r"https://news\.usni\.org/\d{4}/\d{2}/\d{2}/[a-zA-Z0-9\-]+", archive_text)
    tracker_links = [l for l in links if "fleet-and-marine-tracker" in l]
    if not tracker_links:
        tracker_links = links

    if not tracker_links:
        raise Exception("Could not find any Fleet Tracker reports.")

    latest_url = tracker_links[0]
    print(f"Latest report: {latest_url}")

    article_text = fetch_page_markdown(latest_url)

    # 1. Extract and clean proper title
    title_match = re.search(r"Title:\s*(.+)", article_text)
    if title_match:
        raw_title = title_match.group(1).strip()
    else:
        # Fallback: extract title from URL slug
        slug = latest_url.rstrip("/").split("/")[-1].replace("-", " ").title()
        raw_title = slug

    clean_title = raw_title.replace("USNI News Fleet and Marine Tracker:", "").replace("USNI News Fleet and Marine Tracker", "").strip(" :-\t")
    if not clean_title or clean_title.lower() == "fleet tracker":
        # Extract date from URL like 2026/10/12
        date_match = re.search(r"/(\d{4})/(\d{2})/(\d{2})/", latest_url)
        if date_match:
            y, m, d = date_match.groups()
            clean_title = f"{y}-{m}-{d}"
        else:
            clean_title = "Latest Update"

    # 2. Extract high-res armada map
    images = re.findall(r"https://news\.usni\.org/wp-content/uploads/[^\s\)\"\'\<\>]+\.(?:png|jpg|jpeg)", article_text)
    map_url = None
    for img in images:
        if not any(x in img.lower() for x in ["logo", "avatar", "icon", "banner", "author"]):
            map_url = img
            break

    if not map_url:
        raise Exception("Could not find the armada map image.")

    print(f"Downloading Armada Map: {map_url}")
    img_res = requests.get(map_url, headers=HEADERS, timeout=30)
    img_res.raise_for_status()
    with open(OUTPUT_IMAGE, "wb") as f:
        f.write(img_res.content)

    # 3. Clean deployment ships and strike groups
    deployments = []
    for line in article_text.splitlines():
        cleaned_line = strip_html_tags(line)
        if any(k in cleaned_line.lower() for k in ["carrier strike group", "amphibious ready group", "uss "]):
            # Remove leading bullets or list numbering
            cleaned_line = re.sub(r"^[-\*\#\s\d\.]+", "", cleaned_line).strip()
            if 10 < len(cleaned_line) < 140 and cleaned_line not in deployments:
                deployments.append(f"🔹 <i>{html.escape(cleaned_line)}</i>")

    return clean_title, deployments[:5]


def send_to_telegram(clean_title, deployments):
    summary_text = "\n".join(deployments) if deployments else "🔹 <i>اطلاعات تکمیلی در نقشه درج شده است.</i>"
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

    api_url = f"https://api.telegram.org/bot{TELEGRAM_BOT_TOKEN}/sendPhoto"
    with open(OUTPUT_IMAGE, "rb") as img:
        payload = {
            "chat_id": TELEGRAM_CHAT_ID,
            "caption": caption,
            "parse_mode": "HTML"
        }
        res = requests.post(api_url, data=payload, files={"photo": img}, timeout=30)
        if not res.ok:
            print(f"[telegram error] {res.text}")
        res.raise_for_status()
    print("Clean update posted to Telegram.")


def main():
    if not TELEGRAM_BOT_TOKEN or not TELEGRAM_CHAT_ID:
        raise ValueError("TELEGRAM_BOT_TOKEN or TELEGRAM_CHAT_ID is missing!")

    title, deployments = get_latest_report()
    send_to_telegram(title, deployments)


if __name__ == "__main__":
    main()
