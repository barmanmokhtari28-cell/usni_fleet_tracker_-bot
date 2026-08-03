#!/usr/bin/env python3
"""
USNI News Fleet and Marine Tracker -> Telegram monitor.

USNI publishes fleet-position updates as a small number of long-lived
WordPress posts (e.g. "USNI News Fleet and Marine Tracker: July 27, 2026")
that get SILENTLY EDITED over time as new ship movements are confirmed
(the site shows "Updated: <date>" on the post). A brand new post also
appears roughly weekly.

This script therefore watches for TWO kinds of events, not one:
  1. NEW  - a tracker post we've never seen before
  2. EDIT - a previously-seen post whose content changed

State (which posts we've seen, and a hash + snippet of their content)
is kept in state.json, which the GitHub Actions workflow commits back
to the repo after every run so state persists between scheduled runs.

--------------------------------------------------------------------
BILINGUAL CAPTIONS
--------------------------------------------------------------------
Every caption shows a Persian translation first, then the original
English text underneath hidden behind a Telegram spoiler (tap to
reveal). Translation uses the free MyMemory API (no key required,
same service used in your other forwarding automations). It has a
daily quota and can occasionally fail/rate-limit; if it does, the
script falls back to English-only rather than sending anything broken.

--------------------------------------------------------------------
NOTE ON CLOUDFLARE
--------------------------------------------------------------------
news.usni.org sits behind Cloudflare. Fetching goes through a chain
of strategies until one works: cloudscraper, a free read-through
proxy, then (if you've set SCRAPERAPI_KEY) ScraperAPI.

--------------------------------------------------------------------
TEST / BACKFILL MODE
--------------------------------------------------------------------
Set env var TEST_MODE=true to do a one-off historical test run:
  - Sends every tracker post published within the last TEST_DAYS days
    (default 30) to Telegram, each prefixed "🧪 TEST BACKFILL" so it's
    unmistakably not a live alert.
  - Reads/writes a SEPARATE state file (test_state.json), never touching
    the real state.json. This means running test mode has zero effect
    on live monitoring — you can test as many times as you want, then
    just run the workflow normally (TEST_MODE unset) to go live for real.
"""

import calendar
import hashlib
import html
import json
import os
import re
import sys
import time
import urllib.parse
from difflib import SequenceMatcher
from pathlib import Path

import feedparser
import requests

try:
    import cloudscraper
    _SCRAPER = cloudscraper.create_scraper(
        browser={"browser": "chrome", "platform": "windows", "mobile": False}
    )
except ImportError:
    _SCRAPER = None
    print("[warn] cloudscraper not installed — skipping that fetch strategy.", file=sys.stderr)

# ---------------------------------------------------------------------------
# Config
# ---------------------------------------------------------------------------

FEED_URLS = [
    "https://news.usni.org/category/fleet-tracker/feed/",
    "https://news.usni.org/tag/fleet-and-marine-tracker/feed/",
]

TEST_MODE = os.environ.get("TEST_MODE", "false").strip().lower() == "true"
TEST_DAYS = int(os.environ.get("TEST_DAYS", "30"))

STATE_FILE = Path(__file__).parent / (
    "test_state.json" if TEST_MODE else "state.json"
)

TELEGRAM_BOT_TOKEN = os.environ.get("TELEGRAM_BOT_TOKEN")
TELEGRAM_CHAT_ID = os.environ.get("TELEGRAM_CHAT_ID")

# Optional: if you sign up for a Cloudflare-bypass scraping API (ScraperAPI,
# ScrapingBee, ZenRows, etc.) and add SCRAPERAPI_KEY as a repo secret, this
# strategy activates automatically as a fallback. Not required to run.
SCRAPERAPI_KEY = os.environ.get("SCRAPERAPI_KEY")

# Optional: a contact email raises MyMemory's free daily translation quota
# (see mymemory.translated.net docs). Not required.
MYMEMORY_EMAIL = os.environ.get("MYMEMORY_EMAIL")

REQUEST_TIMEOUT = 30
TRANSLATE_TIMEOUT = 15
MAX_TELEGRAM_MSG = 4096      # Telegram sendMessage hard cap
MAX_TELEGRAM_CAPTION = 1024  # Telegram sendPhoto caption hard cap
SNIPPET_KEEP_CHARS = 6000
MYMEMORY_CHUNK_CHARS = 450   # stay under MyMemory's ~500-char per-request limit

# Footer appended to the bottom of every message/caption. Telegram HTML
# parse_mode is used, so <b>/<i>/etc. render as rich text (not literal tags).
FOOTER = "\n\n🖲️ <b>@secretollah</b>\n<b>#USNI #ناو</b>"


# ---------------------------------------------------------------------------
# Translation (Persian)
# ---------------------------------------------------------------------------

def _chunk_text(text: str, max_len: int) -> list[str]:
    """Split text into chunks <= max_len, breaking on sentence/word
    boundaries where possible so translation quality isn't mangled."""
    if len(text) <= max_len:
        return [text] if text else []
    chunks = []
    remaining = text
    while len(remaining) > max_len:
        window = remaining[:max_len]
        split_at = max(window.rfind(". "), window.rfind("! "), window.rfind("? "))
        if split_at < max_len * 0.4:  # no good sentence break, fall back to word break
            split_at = window.rfind(" ")
        if split_at <= 0:
            split_at = max_len
        else:
            split_at += 1
        chunks.append(remaining[:split_at].strip())
        remaining = remaining[split_at:].strip()
    if remaining:
        chunks.append(remaining)
    return chunks


def translate_to_persian(text: str) -> str | None:
    """Translate English text to Persian via MyMemory's free API.
    Returns None (not an exception) on any failure so callers can cleanly
    fall back to English-only rather than crash or send garbled text."""
    text = (text or "").strip()
    if not text:
        return ""
    try:
        translated_parts = []
        for chunk in _chunk_text(text, MYMEMORY_CHUNK_CHARS):
            params = {"q": chunk, "langpair": "en|fa"}
            if MYMEMORY_EMAIL:
                params["de"] = MYMEMORY_EMAIL
            resp = requests.get(
                "https://api.mymemory.translated.net/get",
                params=params,
                timeout=TRANSLATE_TIMEOUT,
            )
            if not resp.ok:
                print(f"[translate] MyMemory HTTP {resp.status_code} for chunk", file=sys.stderr)
                return None
            data = resp.json()
            translated = data.get("responseData", {}).get("translatedText", "")
            if not translated or "MYMEMORY WARNING" in translated.upper():
                print(f"[translate] MyMemory quota/error: {translated[:150]!r}", file=sys.stderr)
                return None
            translated_parts.append(translated)
        return " ".join(translated_parts).strip()
    except Exception as exc:  # noqa: BLE001
        print(f"[translate] failed: {exc}", file=sys.stderr)
        return None


# ---------------------------------------------------------------------------
# Telegram helpers
# ---------------------------------------------------------------------------

def with_footer(body: str, limit: int) -> str:
    """Append FOOTER to body, truncating body (not the footer) if needed so
    the whole thing fits Telegram's length limit. This is a safety-net
    truncation — build_bilingual_caption already sizes body to fit, so this
    should rarely need to actually cut anything."""
    room_for_body = limit - len(FOOTER)
    if len(body) > room_for_body:
        body = body[:room_for_body].rsplit(" ", 1)[0] + "…"
    return body + FOOTER


def truncate_plain(text: str, limit: int) -> str:
    if limit <= 0:
        return ""
    if len(text) <= limit:
        return text
    return text[:limit].rsplit(" ", 1)[0] + "…"


def build_bilingual_caption(tag_line: str, title_en: str, body_en: str, link: str, limit: int) -> str:
    """Builds: tag line -> bold Persian title+body -> spoiler-hidden
    original English -> link. Falls back to English-only if translation
    isn't available, and sizes each language block to fit the given
    character limit (minus footer, which is added separately by the
    Telegram send functions via with_footer)."""
    title_fa = translate_to_persian(title_en)
    body_fa = translate_to_persian(body_en)
    have_translation = title_fa is not None and body_fa is not None

    # Reserve room for the footer (added later), the tag line, the link,
    # and the fixed decorative text/newlines this template always includes.
    fixed_overhead = (
        len(tag_line) + len(link) + len(FOOTER)
        + len("\n\n") * 4  # spacing between sections
        + len("<tg-spoiler></tg-spoiler>")
        + 40  # safety margin for HTML escaping expansion (&amp; etc.)
    )
    remaining = max(limit - fixed_overhead, 150)

    if have_translation:
        fa_budget = int(remaining * 0.6)
        en_budget = remaining - fa_budget
    else:
        fa_budget = 0
        en_budget = remaining

    title_en_t = truncate_plain(title_en, min(len(title_en), en_budget // 3 or 40))
    body_en_t = truncate_plain(body_en, max(en_budget - len(title_en_t), 0))

    parts = [tag_line]

    if have_translation:
        title_fa_t = truncate_plain(title_fa, min(len(title_fa), fa_budget // 3 or 40))
        body_fa_t = truncate_plain(body_fa, max(fa_budget - len(title_fa_t), 0))
        parts.append(f"<b>{html.escape(title_fa_t)}</b>\n{html.escape(body_fa_t)}")
        parts.append(
            f"<tg-spoiler>{html.escape(title_en_t)}\n{html.escape(body_en_t)}</tg-spoiler>"
        )
    else:
        # Translation unavailable this run — English only, no spoiler split.
        parts.append(f"<b>{html.escape(title_en_t)}</b>\n{html.escape(body_en_t)}")

    parts.append(link)
    return "\n\n".join(parts)


def send_telegram_message(text: str, disable_preview: bool = False) -> None:
    if not TELEGRAM_BOT_TOKEN or not TELEGRAM_CHAT_ID:
        raise RuntimeError("TELEGRAM_BOT_TOKEN / TELEGRAM_CHAT_ID env vars are not set.")
    url = f"https://api.telegram.org/bot{TELEGRAM_BOT_TOKEN}/sendMessage"
    resp = requests.post(
        url,
        data={
            "chat_id": TELEGRAM_CHAT_ID,
            "text": with_footer(text, MAX_TELEGRAM_MSG),
            "parse_mode": "HTML",
            "disable_web_page_preview": disable_preview,
        },
        timeout=REQUEST_TIMEOUT,
    )
    if not resp.ok:
        print(f"[telegram] sendMessage failed: {resp.status_code} {resp.text}", file=sys.stderr)


def send_telegram_photo(photo_url: str, caption: str) -> bool:
    if not TELEGRAM_BOT_TOKEN or not TELEGRAM_CHAT_ID:
        raise RuntimeError("TELEGRAM_BOT_TOKEN / TELEGRAM_CHAT_ID env vars are not set.")
    url = f"https://api.telegram.org/bot{TELEGRAM_BOT_TOKEN}/sendPhoto"
    resp = requests.post(
        url,
        data={
            "chat_id": TELEGRAM_CHAT_ID,
            "caption": with_footer(caption, MAX_TELEGRAM_CAPTION),
            "photo": photo_url,
            "parse_mode": "HTML",
        },
        timeout=REQUEST_TIMEOUT,
    )
    if not resp.ok:
        print(f"[telegram] sendPhoto failed: {resp.status_code} {resp.text}", file=sys.stderr)
        return False
    return True


# ---------------------------------------------------------------------------
# State
# ---------------------------------------------------------------------------

def load_state() -> dict:
    if STATE_FILE.exists():
        try:
            return json.loads(STATE_FILE.read_text())
        except json.JSONDecodeError:
            print(f"[state] {STATE_FILE.name} was corrupt, starting fresh", file=sys.stderr)
    return {}


def save_state(state: dict) -> None:
    STATE_FILE.write_text(json.dumps(state, indent=2, ensure_ascii=False))


# ---------------------------------------------------------------------------
# Feed parsing / content extraction
# ---------------------------------------------------------------------------

def clean_html(raw_html: str) -> str:
    text = re.sub(r"<[^>]+>", " ", raw_html or "")
    text = html.unescape(text)
    text = re.sub(r"\s+", " ", text).strip()
    return text


def extract_first_image(raw_html: str) -> str | None:
    match = re.search(r'<img[^>]+src="([^"]+)"', raw_html or "")
    return match.group(1) if match else None


def get_entry_content(entry) -> str:
    if hasattr(entry, "content") and entry.content:
        return entry.content[0].value
    return getattr(entry, "summary", "") or ""


def get_published_epoch(entry) -> float | None:
    """Returns a UTC epoch timestamp for the entry's published date, or None."""
    struct = entry.get("published_parsed") or entry.get("updated_parsed")
    if not struct:
        return None
    return calendar.timegm(struct)


def _looks_like_cloudflare_block(text: str) -> bool:
    lowered = text[:500].lower()
    return "just a moment" in lowered or "cloudflare" in lowered or "cf-browser-verification" in lowered


def fetch_via_cloudscraper(url: str):
    if _SCRAPER is None:
        return None, "cloudscraper not installed"
    try:
        resp = _SCRAPER.get(url, timeout=REQUEST_TIMEOUT)
    except Exception as exc:  # noqa: BLE001
        return None, f"request error: {exc}"
    if resp.status_code != 200:
        return None, f"HTTP {resp.status_code}, preview: {resp.text[:200]!r}"
    if _looks_like_cloudflare_block(resp.text):
        return None, "got Cloudflare challenge page, not real content"
    return resp.content, None


def fetch_via_allorigins(url: str):
    """Fetch through a public read-through proxy (different egress IP than
    GitHub's runners). Free, no API key, but not guaranteed to bypass every
    Cloudflare config either."""
    proxied = "https://api.allorigins.win/raw?url=" + urllib.parse.quote(url, safe="")
    try:
        resp = requests.get(proxied, timeout=REQUEST_TIMEOUT)
    except Exception as exc:  # noqa: BLE001
        return None, f"request error: {exc}"
    if resp.status_code != 200:
        return None, f"HTTP {resp.status_code}, preview: {resp.text[:200]!r}"
    if _looks_like_cloudflare_block(resp.text):
        return None, "got Cloudflare challenge page via proxy too"
    return resp.content, None


def fetch_via_scraperapi(url: str):
    """Only runs if you've added a SCRAPERAPI_KEY repo secret. ScraperAPI
    (and similar paid services) maintain residential/rotating IPs and
    handle Cloudflare bypass as their core product."""
    if not SCRAPERAPI_KEY:
        return None, "SCRAPERAPI_KEY not set, skipping"
    proxied = (
        "https://api.scraperapi.com/?api_key=" + SCRAPERAPI_KEY
        + "&url=" + urllib.parse.quote(url, safe="")
    )
    try:
        resp = requests.get(proxied, timeout=REQUEST_TIMEOUT)
    except Exception as exc:  # noqa: BLE001
        return None, f"request error: {exc}"
    if resp.status_code != 200:
        return None, f"HTTP {resp.status_code}, preview: {resp.text[:200]!r}"
    if _looks_like_cloudflare_block(resp.text):
        return None, "got Cloudflare challenge page via ScraperAPI too"
    return resp.content, None


FETCH_STRATEGIES = [
    ("cloudscraper", fetch_via_cloudscraper),
    ("allorigins proxy", fetch_via_allorigins),
    ("scraperapi", fetch_via_scraperapi),
]


def fetch_feed_content(url: str) -> bytes | None:
    for name, strategy in FETCH_STRATEGIES:
        content, error = strategy(url)
        if content is not None:
            print(f"[feed] {url} — succeeded via {name}")
            return content
        print(f"[feed] {url} — {name} failed: {error}", file=sys.stderr)
    print(
        f"[feed] {url} — ALL fetch strategies failed.",
        file=sys.stderr,
    )
    return None


def fetch_all_entries() -> dict:
    """Returns {link: entry} merged across all monitored feeds, de-duped."""
    entries_by_link = {}
    for feed_url in FEED_URLS:
        raw = fetch_feed_content(feed_url)
        if raw is None:
            continue

        parsed = feedparser.parse(raw)
        if parsed.bozo and not parsed.entries:
            print(
                f"[feed] {feed_url} gave unparsable content "
                f"(bozo_exception={parsed.get('bozo_exception')})",
                file=sys.stderr,
            )
            continue

        for entry in parsed.entries:
            link = entry.get("link")
            if link and link not in entries_by_link:
                entries_by_link[link] = entry
    return entries_by_link


def diff_summary(old_text: str, new_text: str, max_chars: int = 900) -> str:
    matcher = SequenceMatcher(None, old_text, new_text)
    added_chunks = []
    for tag, i1, i2, j1, j2 in matcher.get_opcodes():
        if tag in ("insert", "replace"):
            chunk = new_text[j1:j2].strip()
            if len(chunk) > 20:
                added_chunks.append(chunk)
    if not added_chunks:
        return "(Content changed, but no clear new sentences were detected — check the article.)"
    summary = " […] ".join(added_chunks)
    if len(summary) > max_chars:
        summary = summary[:max_chars].rsplit(" ", 1)[0] + "…"
    return summary


# ---------------------------------------------------------------------------
# Main
# ---------------------------------------------------------------------------

def main() -> None:
    if TEST_MODE:
        print(f"[main] TEST MODE — backfilling posts from the last {TEST_DAYS} day(s). "
              f"Using {STATE_FILE.name}, real state.json is untouched.")

    state = load_state()
    entries = fetch_all_entries()

    if not entries:
        print("[main] No entries fetched from any feed this run — exiting without changes.")
        return

    cutoff_epoch = time.time() - TEST_DAYS * 86400 if TEST_MODE else None

    new_count = 0
    updated_count = 0
    skipped_old = 0

    for link, entry in entries.items():
        if TEST_MODE:
            published_epoch = get_published_epoch(entry)
            if published_epoch is not None and published_epoch < cutoff_epoch:
                skipped_old += 1
                continue

        title = clean_html(entry.get("title", "Untitled"))
        raw_content = get_entry_content(entry)
        text_content = clean_html(raw_content)
        content_hash = hashlib.sha256(text_content.encode("utf-8")).hexdigest()
        image_url = extract_first_image(raw_content)

        prior = state.get(link)

        if prior is None:
            new_count += 1
            tag_line = "🧪 TEST BACKFILL" if TEST_MODE else "🆕 New USNI Fleet &amp; Marine Tracker post"
            excerpt = text_content[:700] + ("…" if len(text_content) > 700 else "")
            limit = MAX_TELEGRAM_CAPTION if image_url else MAX_TELEGRAM_MSG
            caption = build_bilingual_caption(tag_line, title, excerpt, link, limit)

            sent_as_photo = False
            if image_url:
                sent_as_photo = send_telegram_photo(image_url, caption)
            if not sent_as_photo:
                send_telegram_message(caption)

            state[link] = {
                "title": title,
                "hash": content_hash,
                "text_snapshot": text_content[:SNIPPET_KEEP_CHARS],
                "first_seen": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime()),
                "last_updated": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime()),
            }

        elif prior.get("hash") != content_hash:
            updated_count += 1
            old_text = prior.get("text_snapshot", "")
            summary = diff_summary(old_text, text_content)
            tag_line = "🧪 TEST BACKFILL (update)" if TEST_MODE else "🔄 USNI Fleet Tracker updated"
            limit = MAX_TELEGRAM_CAPTION if image_url else MAX_TELEGRAM_MSG
            caption = build_bilingual_caption(tag_line, title, summary, link, limit)

            sent_as_photo = False
            if image_url:
                sent_as_photo = send_telegram_photo(image_url, caption)
            if not sent_as_photo:
                send_telegram_message(caption)

            state[link]["hash"] = content_hash
            state[link]["text_snapshot"] = text_content[:SNIPPET_KEEP_CHARS]
            state[link]["last_updated"] = time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime())
        # else: unchanged, nothing to do

    save_state(state)
    print(f"[main] Done. {new_count} new post(s), {updated_count} update(s), "
          f"{skipped_old} skipped as older than {TEST_DAYS}d this run.")


if __name__ == "__main__":
    main()
