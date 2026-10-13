#!/usr/bin/env python3
"""
USNI Live Fleet Tracker -> Telegram Broadcaster
Sends strictly the single newest report with its high-res armada map.
Never spams historical archives.
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


def fetch_page_markdown(target_url: str) -> str:
    """Fetches clean live page content, bypassing Cloudflare."""
    # 1. Try Jina Reader Proxy (Free & bypasses Cloudflare)
    try:
        jina_url = f"https://r.jina.ai/{target_url}"
        res = requests.get(jina_url, headers=HEADERS, timeout=25)
        if res.ok and len(res.text) > 300 and "Just a moment" not in res.text:
            return res.text
    except Exception as e:
        print(f"[fetch] Jina proxy error: {e}")

    # 2. Try ScraperAPI fallback if key is configured
    if SCRAPERAPI_KEY:
        try:
            s_url = f"https://api.scraperapi.com/?api_key={SCRAPERAPI_KEY}&url={target_url}"
            res = requests.get(s_url, timeout=30)
            if res.ok and len(res.text) > 300:
                return res.text
        except Exception as e:
            print(f"[fetch] ScraperAPI error: {e}")

    # 3. Direct request fallback
    res = requests.get(target_url, headers=HEADERS, timeout=25)
    res.raise_for_status()
    return res.text


def get_latest_report():
    """Finds and extracts strictly the newest Fleet Tracker report."""
    print("Fetching live Fleet Tracker category page...")
    archive_text = fetch_page_markdown(CATEGORY_URL)

    # Find all fleet tracker article links
    links = re.findall(r"https://news\.usni\.org/\d{4}/\d{2}/\d{2}/[a-zA-Z0-9\-]+", archive_text)
    tracker_links = [l for l in links if "fleet-and-marine-tracker" in l]

    if not tracker_links:
        # Fallback to any date-based article link at the top
        tracker_links = links

    if not tracker_links:
        raise Exception("Could not find any Fleet Tracker reports on the live site.")

    # Strictly take ONLY the first (newest) article
    latest_url = tracker_links[0]
    print(f"Latest report found: {latest_url}")

    article_text = fetch_page_markdown(latest_url)

    # Extract title
    title_match = re.search(r"Title:\s*(.+)", article_text)
    raw_title = title_match.group(1).strip() if title_match else "Fleet Tracker"
    clean_title = raw_title.replace("USNI News Fleet and Marine Tracker:", "").strip()

    # Find the high-res Armada Map Infographic
    images = re.findall(r"https://news\.usni\.org/wp-content/uploads/[^\s\)\"\'\<\>]+\.(?:png|jpg|jpeg)", article_text)
    map_url = None
    for img in images:
        if not any(x in img.lower() for x in ["logo", "avatar", "icon", "banner", "author"]):
            map_url = img
            break

    if not map_url:
        raise Exception("Could not find the armada map image in the latest report.")

    print(f"Downloading Armada Map: {map_url}")
    img_res = requests.get(map_url, headers=HEADERS, timeout=30)
    img_res.raise_for_status()
    with open(OUTPUT_IMAGE, "wb") as f:
        f.write(img_res.content)

    # Extract active carrier strike groups & amphibious ready groups
    deployments = []
    for line in article_text.splitlines():
        line = line.strip()
        if any(k in line.lower() for k in ["carrier strike group", "amphibious ready group", "uss "]):
            clean_line = re.sub(r"^[-\*\#\s\d\.]+", "", line).strip()
            if 12 < len(clean_line) < 140 and clean_line not in deployments:
                deployments.append(f"🔹 <i>{html.escape(clean_line)}</i>")

    return clean_title, deployments[:5]


def send_to_telegram(clean_title, deployments):
    """Sends exactly 1 photo with rich Persian caption."""
    summary_text = "\n".join(deployments) if deployments else "🔹 <i>اطلاعات تکمیلی در نقشه USNI درج شده است.</i>"
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
    print("Telegram delivery successful.")


def main():
    if not TELEGRAM_BOT_TOKEN or not TELEGRAM_CHAT_ID:
        raise ValueError("TELEGRAM_BOT_TOKEN or TELEGRAM_CHAT_ID is missing!")

    title, deployments = get_latest_report()
    send_to_telegram(title, deployments)


if __name__ == "__main__":
    main()
