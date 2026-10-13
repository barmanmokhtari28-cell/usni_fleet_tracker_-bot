#!/usr/bin/env python3
"""
USNI Live Fleet Tracker -> Telegram Broadcaster
Bulletproof version with safe caption budgeting, resilient image detection,
and graceful fallbacks.
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
    """Strips HTML tags, markdown, and list formatting."""
    text = re.sub(r"<[^>]+>", " ", text)
    text = html.unescape(text)
    text = re.sub(r"[\*\_\[\]\#]", "", text)
    text = re.sub(r"\s+", " ", text)
    return text.strip()


def fetch_page_content(target_url: str) -> str:
    """Tries multiple proxies to bypass Cloudflare reliably."""
    # 1. Jina Reader Proxy
    try:
        print(f"[fetch] Trying Jina proxy for: {target_url}")
        jina_url = f"https://r.jina.ai/{target_url}"
        res = requests.get(jina_url, headers=HEADERS, timeout=25)
        if res.ok and len(res.text) > 300 and "Just a moment" not in res.text:
            return res.text
    except Exception as e:
        print(f"[fetch] Jina proxy error: {e}")

    # 2. ScraperAPI (if key is set)
    if SCRAPERAPI_KEY:
        try:
            print("[fetch] Trying ScraperAPI...")
            s_url = f"https://api.scraperapi.com/?api_key={SCRAPERAPI_KEY}&url={target_url}"
            res = requests.get(s_url, timeout=30)
            if res.ok and len(res.text) > 300:
                return res.text
        except Exception as e:
            print(f"[fetch] ScraperAPI error: {e}")

    # 3. AllOrigins proxy fallback
    try:
        print("[fetch] Trying AllOrigins proxy...")
        ao_url = f"https://api.allorigins.win/raw?url={target_url}"
        res = requests.get(ao_url, headers=HEADERS, timeout=25)
        if res.ok and len(res.text) > 300 and "Just a moment" not in res.text:
            return res.text
    except Exception as e:
        print(f"[fetch] AllOrigins error: {e}")

    # 4. Direct Request
    print("[fetch] Trying direct request...")
    res = requests.get(target_url, headers=HEADERS, timeout=20)
    res.raise_for_status()
    return res.text


def get_latest_report():
    print("1. Fetching Fleet Tracker category page...")
    archive_text = fetch_page_content(CATEGORY_URL)

    # Locate report links
    links = re.findall(r"https://news\.usni\.org/\d{4}/\d{2}/\d{2}/[a-zA-Z0-9\-]+", archive_text)
    tracker_links = [l for l in links if "fleet-and-marine-tracker" in l]
    if not tracker_links:
        tracker_links = links

    if not tracker_links:
        raise Exception("Could not find any Fleet Tracker article URLs.")

    latest_url = tracker_links[0]
    print(f"2. Newest report identified: {latest_url}")

    article_text = fetch_page_content(latest_url)

    # Clean title
    title_match = re.search(r"Title:\s*(.+)", article_text)
    raw_title = title_match.group(1).strip() if title_match else ""
    clean_title = raw_title.replace("USNI News Fleet and Marine Tracker:", "").replace("USNI News Fleet and Marine Tracker", "").strip(" :-\t")
    
    if not clean_title or clean_title.lower() == "fleet tracker":
        date_match = re.search(r"/(\d{4})/(\d{2})/(\d{2})/", latest_url)
        clean_title = f"{date_match.group(1)}-{date_match.group(2)}-{date_match.group(3)}" if date_match else "Live Update"

    # Locate map image (supports full URLs, relative paths, and CDN mirrors)
    img_matches = re.findall(r"(?:https?://[^\s\)\"\'\<\>]+)?/wp-content/uploads/[^\s\)\"\'\<\>]+\.(?:png|jpg|jpeg)", article_text)
    
    map_url = None
    for img in img_matches:
        if not any(x in img.lower() for x in ["logo", "avatar", "icon", "banner", "author"]):
            map_url = img if img.startswith("http") else f"https://news.usni.org{img}"
            break

    image_downloaded = False
    if map_url:
        try:
            print(f"3. Downloading Armada Map: {map_url}")
            img_res = requests.get(map_url, headers=HEADERS, timeout=25)
            if img_res.ok:
                with open(OUTPUT_IMAGE, "wb") as f:
                    f.write(img_res.content)
                image_downloaded = True
        except Exception as e:
            print(f"[image] Could not download image ({e})")

    # Extract ship & carrier movements
    deployments = []
    for line in article_text.splitlines():
        cleaned_line = strip_html_tags(line)
        if any(k in cleaned_line.lower() for k in ["carrier strike group", "amphibious ready group", "uss "]):
            cleaned_line = re.sub(r"^[-\*\#\s\d\.]+", "", cleaned_line).strip()
            if 10 < len(cleaned_line) < 120 and cleaned_line not in deployments:
                deployments.append(f"🔹 <i>{html.escape(cleaned_line)}</i>")

    return clean_title, deployments[:4], image_downloaded


def build_safe_caption(clean_title, deployments):
    """Builds caption guaranteed to stay strictly under Telegram's 1024 limit."""
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

    # Hard safety-cap at 980 characters
    if len(caption) > 980:
        caption = caption[:950].rsplit("\n", 1)[0] + "\n\n📫 @secretollah\n#USNI\n#ناو"

    return caption


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
        # Graceful fallback: send as text message if image was unavailable
        api_url = f"https://api.telegram.org/bot{TELEGRAM_BOT_TOKEN}/sendMessage"
        payload = {
            "chat_id": TELEGRAM_CHAT_ID,
            "text": caption,
            "parse_mode": "HTML"
        }
        res = requests.post(api_url, data=payload, timeout=30)

    if not res.ok:
        print(f"[telegram error] Response {res.status_code}: {res.text}")
    res.raise_for_status()
    print("4. Update successfully posted to Telegram.")


def main():
    if not TELEGRAM_BOT_TOKEN or not TELEGRAM_CHAT_ID:
        raise ValueError("TELEGRAM_BOT_TOKEN or TELEGRAM_CHAT_ID is missing from secrets!")

    title, deployments, has_image = get_latest_report()
    send_to_telegram(title, deployments, has_image)


if __name__ == "__main__":
    main()
