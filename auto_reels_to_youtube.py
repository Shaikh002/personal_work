#!/usr/bin/env python3
import os
import json
import time
import asyncio
import subprocess
import re
import random
import sys
from pathlib import Path

import requests  # ensure in requirements.txt
from yt_dlp import YoutubeDL
from playwright.async_api import async_playwright
from google.oauth2.credentials import Credentials
from googleapiclient.discovery import build
from googleapiclient.errors import HttpError
from googleapiclient.http import MediaFileUpload
from google.auth.transport.requests import Request

# === CONFIG / ENV ===
INSTAGRAM_PROFILE = os.getenv("INSTAGRAM_PROFILE", "").strip()
IG_COOKIES_JSON = os.getenv("IG_COOKIES_JSON")  # raw JSON string of Instagram cookies
PROCESSED_FILE = Path("processed_reels.json")
DOWNLOAD_DIR = Path("downloads")
TOKEN_FILE = Path("token.json")  # token.json must already exist (restored by workflow)
UPLOAD_LIMIT = int(os.getenv("UPLOAD_LIMIT", "1"))  # one reel per run
YOUTUBE_SCOPES = ["https://www.googleapis.com/auth/youtube.upload"]
MAX_SHORT_SECONDS = 60
WAIT_BETWEEN_UPLOADS = 3 * 60 * 60  # if multiple, wait (not used when limit=1)
USER_AGENT_IPHONE = (
    "Mozilla/5.0 (iPhone; CPU iPhone OS 14_0 like Mac OS X) "
    "AppleWebKit/605.1.15 (KHTML, like Gecko) Version/14.0 "
    "Mobile/15E148 Safari/604.1"
)
FFMPEG = "ffmpeg"
FFPROBE = "ffprobe"

# Telegram
TELEGRAM_BOT_TOKEN = os.getenv("TELEGRAM_BOT_TOKEN")
TELEGRAM_CHAT_ID = os.getenv("TELEGRAM_CHAT_ID")

def send_telegram(message: str):
    if not TELEGRAM_BOT_TOKEN or not TELEGRAM_CHAT_ID:
        # silent if not configured
        return
    try:
        url = f"https://api.telegram.org/bot{TELEGRAM_BOT_TOKEN}/sendMessage"
        payload = {
            "chat_id": TELEGRAM_CHAT_ID,
            "text": message,
            "parse_mode": "Markdown"
        }
        requests.post(url, data=payload, timeout=10)
    except Exception as e:
        print(f"⚠️ Failed to send Telegram message: {e}")

# === STOPWORDS for keyword extraction ===
_STOPWORDS = {
    "the", "and", "for", "with", "this", "that", "from", "your", "you", "are",
    "about", "have", "has", "not", "but", "just", "what", "when", "where",
    "who", "why", "how", "its", "it's", "can", "will", "get", "like", "new"
}

# === HASHTAGS POOLS ===
HACKING_TAGS = [
    "#ethicalhacking", "#cybersecurity", "#bugbounty", "#infosec",
    "#penetrationtesting", "#redteam", "#vulnerability", "#securityresearch",
    "#threatintel", "#whitehat", "#hackerlife", "#securitytips", "#hackingtools"
]
TRENDING_TAGS = [
    "#viral", "#trending", "#Shorts", "#foryou", "#explore", "#tech",
    "#contentcreator", "#daily", "#automation", "#viralshorts"
]

# === UTILITIES ===
def load_processed():
    if PROCESSED_FILE.exists():
        try:
            return set(json.loads(PROCESSED_FILE.read_text(encoding="utf-8")))
        except Exception:
            print("⚠️ Warning: processed_reels.json corrupted, resetting.")
            return set()
    return set()

def save_processed(processed_set):
    PROCESSED_FILE.write_text(json.dumps(list(processed_set), indent=2), encoding="utf-8")

def extract_keywords(text, count=2):
    if not text:
        return []
    words = re.findall(r"\b[a-zA-Z0-9]{3,}\b", text.lower())
    freq = {}
    for w in words:
        if w in _STOPWORDS:
            continue
        freq[w] = freq.get(w, 0) + 1
    sorted_words = sorted(freq.items(), key=lambda x: -x[1])
    return [f"#{w}" for w, _ in sorted_words[:count]]

def generate_hacking_trending_hashtags(caption, total=8):
    keywords = extract_keywords(caption, count=2)
    chosen = []
    for k in keywords:
        if len(chosen) < total:
            chosen.append(k)
    for tag in HACKING_TAGS:
        if len(chosen) >= total:
            break
        if tag.lower() not in (t.lower() for t in chosen):
            chosen.append(tag)
    for tag in TRENDING_TAGS:
        if len(chosen) >= total:
            break
        if tag.lower() not in (t.lower() for t in chosen):
            chosen.append(tag)
    return " ".join(chosen[:total])

def get_duration(path):
    try:
        out = subprocess.check_output([
            FFMPEG.replace("ffmpeg","ffprobe") if False else FFPROBE, "-v", "error",
            "-show_entries", "format=duration",
            "-of", "default=noprint_wrappers=1:nokey=1",
            path
        ]).decode().strip()
        return float(out)
    except Exception:
        return 0.0

def trim_short(path):
    dur = get_duration(path)
    if dur > MAX_SHORT_SECONDS:
        base, ext = os.path.splitext(path)
        out = f"{base}_short{ext}"
        subprocess.run([
            FFMPEG, "-y", "-i", path,
            "-t", str(MAX_SHORT_SECONDS),
            "-c", "copy", out
        ], check=True)
        return out
    return path

# === YOUTUBE CLIENT ===
def get_youtube_client():
    if not TOKEN_FILE.exists():
        msg = "❌ YouTube credentials missing. token.json not found. You must generate a fresh token locally with offline OAuth and update the secret."
        print(msg)
        send_telegram(f"❌ [YouTube] {msg}")
        sys.exit(1)
    try:
        creds = Credentials.from_authorized_user_file(str(TOKEN_FILE), YOUTUBE_SCOPES)
    except Exception as e:
        msg = f"❌ Failed to load YouTube credentials: {e}"
        print(msg)
        send_telegram(f"❌ [YouTube] {msg}")
        sys.exit(1)

    if not creds.valid:
        if creds.expired and creds.refresh_token:
            try:
                creds.refresh(Request())
                with open(TOKEN_FILE, "w", encoding="utf-8") as f:
                    f.write(creds.to_json())
                print("✅ Refreshed YouTube token saved back to token.json")
            except Exception as e:
                msg = f"❌ Failed to refresh YouTube token: {e}"
                print(msg)
                send_telegram(f"❌ [YouTube] {msg}")
                sys.exit(1)
        else:
            msg = "❌ YouTube credentials invalid or missing refresh token. Regenerate token locally with offline access and update secret."
            print(msg)
            send_telegram(f"❌ [YouTube] {msg}")
            sys.exit(1)
    return build("youtube", "v3", credentials=creds)

def upload_to_youtube(video_path, caption):
    youtube = get_youtube_client()
    title_snippet = caption[:40].strip() or f"Reel from @{INSTAGRAM_PROFILE}"
    title = f"#{INSTAGRAM_PROFILE} Hack Reel: {title_snippet}"
    hashtags = generate_hacking_trending_hashtags(caption)
    description = f"{caption}\n\n{hashtags}"
    body = {
        "snippet": {
            "title": title,
            "description": description,
            "tags": hashtags.split()[:10],
            "categoryId": "22"
        },
        "status": {
            "privacyStatus": "public"
        }
    }
    print(f"📤 Uploading: {video_path}")
    try:
        req = youtube.videos().insert(
            part="snippet,status",
            body=body,
            media_body=MediaFileUpload(video_path)
        )
        res = req.execute()
        vid = res.get("id")
        msg = f"✅ Video uploaded successfully → https://youtu.be/{vid}"
        print(msg)
        send_telegram(f"✅ [Upload] {msg}")
        return True
    except HttpError as e:
        reason = ""
        try:
            info = json.loads(e.content.decode("utf-8"))
            reason = info.get("error", {}).get("errors", [{}])[0].get("reason", "")
        except Exception:
            pass
        if reason == "uploadLimitExceeded":
            msg = "⚠️ Daily upload limit reached—stopping further uploads."
            print(msg)
            send_telegram(f"⚠️ [Upload] {msg}")
            return False
        msg = f"❌ Upload failed: {e}"
        print(msg)
        send_telegram(f"❌ [Upload] {msg}")
        return False

# === INSTAGRAM / PLAYWRIGHT ===
async def inject_cookies(context):
    if not IG_COOKIES_JSON:
        msg = "IG_COOKIES_JSON env var is empty; cannot inject cookies."
        print(f"❌ {msg}")
        send_telegram(f"❌ [Cookies] {msg}")
        return
    try:
        cookies = json.loads(IG_COOKIES_JSON)
    except Exception as e:
        msg = f"Failed to parse IG_COOKIES_JSON: {e}"
        print(f"❌ {msg}")
        send_telegram(f"❌ [Cookies] {msg}")
        return
    normalized = []
    for c in cookies:
        same_site = c.get("sameSite")
        if same_site not in ("Strict", "Lax", "None"):
            same_site = "Lax"
        entry = {
            "name": c["name"],
            "value": c["value"],
            "domain": c["domain"],
            "path": c.get("path", "/"),
            "httpOnly": bool(c.get("httpOnly", False)),
            "secure": bool(c.get("secure", False)),
            "sameSite": same_site
        }
        expires = c.get("expirationDate") or c.get("expires")
        if isinstance(expires, (int, float)) and expires > 0:
            entry["expires"] = int(expires)
        normalized.append(entry)
    try:
        await context.add_cookies(normalized)
        print("✅ IG cookies injected")
    except Exception as e:
        msg = f"Cookie injection failed: {e}"
        print(f"❌ {msg}")
        send_telegram(f"❌ [Cookies] {msg}")

async def fetch_reel_links():
    if not INSTAGRAM_PROFILE:
        msg = "INSTAGRAM_PROFILE not set."
        print(f"❌ {msg}")
        send_telegram(f"❌ [Config] {msg}")
        return []
    async with async_playwright() as p:
        browser = await p.chromium.launch(headless=True)
        context = await browser.new_context(
            user_agent=USER_AGENT_IPHONE,
            viewport={"width": 375, "height": 812}
        )
        await inject_cookies(context)
        page = await context.new_page()
        target = f"https://www.instagram.com/{INSTAGRAM_PROFILE}/reels/"
        print(f"🔍 Loading {target}")
        try:
            await page.goto(target, timeout=60000, wait_until="networkidle")
            for _ in range(3):
                await page.evaluate("window.scrollBy(0, window.innerHeight)")
                await asyncio.sleep(1)
            await page.wait_for_selector('a[href*="/reel/"]', timeout=30000)
        except Exception as e:
            await page.screenshot(path="debug_reels_error.png")
            msg = f"Timeout or failed loading reels page; saved debug_reels_error.png. Error: {e}"
            print(f"⚠️ {msg}")
            send_telegram(f"⚠️ [Reels] {msg}")
            await browser.close()
            return []
        hrefs = await page.eval_on_selector_all('a[href*="/reel/"]', "els => els.map(e => e.href)")
        unique = list(dict.fromkeys(hrefs))
        print(f"🔗 Found {len(unique)} reels")
        await browser.close()
        return unique

def download_reel(url):
    DOWNLOAD_DIR.mkdir(exist_ok=True)
    opts = {
        "format": "mp4",
        "outtmpl": str(DOWNLOAD_DIR / "%(id)s.%(ext)s"),
        "quiet": True,
        "no_warnings": True
    }
    with YoutubeDL(opts) as ydl:
        info = ydl.extract_info(url, download=True)
        filename = ydl.prepare_filename(info)
        caption = info.get("description") or info.get("title") or ""
        caption = re.sub(r'@\w+', '', caption)
        caption = re.sub(r'\s+', ' ', caption).strip()
        return filename, caption

# === MAIN ===
async def main():
    start_msg = f"🚀 Run started for profile @{INSTAGRAM_PROFILE}, limit={UPLOAD_LIMIT}"
    print(start_msg)
    send_telegram(start_msg)

    processed = load_processed()
    reels = await fetch_reel_links()
    to_upload = [r for r in reels if r not in processed][:UPLOAD_LIMIT]

    if not to_upload:
        msg = "⚠️ No new reels to upload."
        print(msg)
        send_telegram(msg)
        return

    for link in to_upload:
        print(f"\n▶️ Processing {link}")
        try:
            video_file, caption = download_reel(link)
            short = trim_short(video_file)
            success = upload_to_youtube(short, caption)
            if not success:
                break
            processed.add(link)
            # cleanup
            try:
                if Path(video_file).exists():
                    os.remove(video_file)
                if short != video_file and Path(short).exists():
                    os.remove(short)
            except Exception:
                pass
            if len(processed) < UPLOAD_LIMIT:
                time.sleep(WAIT_BETWEEN_UPLOADS)
        except Exception as e:
            msg = f"Error processing reel {link}: {e}"
            print(f"❌ {msg}")
            send_telegram(f"❌ [Process] {msg}")

    save_processed(processed)
    done_msg = f"🏁 All done. Uploaded {len(processed)} reel(s)."
    print(f"\n{done_msg}")
    send_telegram(done_msg)

if __name__ == "__main__":
    try:
        asyncio.run(main())
    except KeyboardInterrupt:
        print("Interrupted by user.")
        send_telegram("🛑 Run interrupted by user.")
