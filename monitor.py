#!/usr/bin/env python3
"""
USNI Live Fleet Tracker -> Telegram Broadcaster
Bypasses Cloudflare Turnstile via Reader Proxy and directly downloads
the high-resolution fleet map image. Never captures or sends Cloudflare screens.
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


def clean_text_line(text: str) -> str:
    """Completely removes <h3>, <li>, <em> and markdown characters."""
    text = re.sub(r"<[^>]+>", " ", text)
    text = html.unescape(text)
    text = re.sub(r"[\*\_\[\]\#\<\>]", "", text)
    text = re.sub(r"^[-\*\#\s\d\.\>]+", "", text)
    text = re.sub(r"\s+", " ", text)
    return text.strip()


def fetch_via_clean_proxy(target_url: str) -> str:
    """Fetches article content bypassing Cloudflare."""
    # 1. Primary: Jina Reader Proxy (Free, bypasses Turnstile)
    try:
        print(f"[proxy] Fetching via Jina: {target_url}")
        jina_url = f"https://r.jina.ai/{target_url}"
        res = requests.get(jina_url, headers=HEADERS, timeout=30)
        if res.ok and len(res.text) > 400 and "security verification" not in res.text.lower():
            return res.text
    except Exception as e:
        print(f"[proxy] Jina error: {e}")

    # 2. Secondary: ScraperAPI if key is available
    if SCRAPERAPI_KEY:
        try:
            print("[proxy] Trying ScraperAPI...")
            s_url = f"https://api.scraperapi.com/?api_key={SCRAPERAPI_KEY}&url={target_url}"
            res = requests.get(s_url, timeout=35)
            if res.ok and len(res.text) > 400:
                return res.text
        except Exception as e:
            print(f"[proxy] ScraperAPI error: {e}")

    # 3. Tertiary: AllOrigins Proxy
    try:
        print("[proxy] Trying AllOrigins proxy...")
        ao_url = f"https://api.allorigins.win/raw?url={target_url}"
        res = requests.get(ao_url, headers=HEADERS, timeout=30)
        if res.ok and len(res.text) > 400 and "security verification" not in res.text.lower():
            return res.text
    except Exception as e:
        print(f"[proxy] AllOrigins error: {e}")

    raise RuntimeError("All proxies failed to bypass Cloudflare. Skipping to prevent sending challenge screens.")


def get_latest_report():
    print("1. Fetching live Fleet Tracker page...")
    archive_text = fetch_via_clean_proxy(CATEGORY_URL)

    # Locate report links
    links = re.findall(r"https://news\.usni\.org/\d{4}/\d{2}/\d{2}/[a-zA-Z0-9\-]+", archive_text)
    tracker_links = [l for l in links if "fleet-and-marine-tracker" in l] or links

    if not tracker_links:
        raise Exception("Could not find any Fleet Tracker URLs.")

    latest_url = tracker_links[0]
    print(f"2. Found latest report: {latest_url}")

    article_text = fetch_via_clean_proxy(latest_url)

    # Extract proper human date (e.g. 'Oct. 13, 2026')
    date_match = re.search(
        r"(?:Oct(?:ober)?|Nov(?:ember)?|Dec(?:ember)?|Jan(?:uary)?|Feb(?:ruary)?|Mar(?:ch)?|Apr(?:il)?|May|Jun(?:e)?|Jul(?:y)?|Aug(?:ust)?|Sept?(?:ember)?)\.?\s+\d{1,2},?\s+\d{4}",
        article_text[:1200],
        re.IGNORECASE
    )
    if date_match:
        clean_title = date_match.group(0).strip()
    else:
        # Fallback date from URL slug
        ymd = re.search(r"/(\d{4})/(\d{2})/(\d{2})/", latest_url)
        clean_title = f"{ymd.group(1)}-{ymd.group(2)}-{ymd.group(3)}" if ymd else "Live Update"

    # Locate original high-res armada map image
    img_matches = re.findall(
        r"https://news\.usni\.org/wp-content/uploads/\d{4}/\d{2}/[^\s\)\"\'\<\>]+\.(?:png|jpg|jpeg)",
        article_text
    )

    map_url = None
    for img in img_matches:
        if not any(x in img.lower() for x in ["logo", "avatar", "icon", "banner", "author", "lockup"]):
            map_url = img
            break

    if not map_url:
        raise Exception("Could not find the armada map image URL.")

    print(f"3. Downloading original Armada Map: {map_url}")
    img_res = requests.get(map_url, headers=HEADERS, timeout=30)
    img_res.raise_for_status()

    with open(OUTPUT_IMAGE, "wb") as f:
        f.write(img_res.content)

    # Extract clean deployment lines
    deployments = []
    for line in article_text.splitlines():
        cleaned = clean_text_line(line)
        if any(k in cleaned.lower() for k in ["carrier strike group", "amphibious ready group", "uss "]):
            if 10 < len(cleaned) < 130 and cleaned not in deployments:
                deployments.append(f"🔹 <i>{html.escape(cleaned)}</i>")

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
            "caption": caption[:950],
            "parse_mode": "HTML"
        }
        res = requests.post(api_url, data=payload, files={"photo": img}, timeout=35)
        if not res.ok:
            print(f"[telegram error] {res.text}")
        res.raise_for_status()
    print("4. Report successfully posted to Telegram!")


def main():
    if not TELEGRAM_BOT_TOKEN or not TELEGRAM_CHAT_ID:
        raise ValueError("TELEGRAM_BOT_TOKEN or TELEGRAM_CHAT_ID is missing!")

    title, deployments = get_latest_report()
    send_to_telegram(title, deployments)


if __name__ == "__main__":
    main()
