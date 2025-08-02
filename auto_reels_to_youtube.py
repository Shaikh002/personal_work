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
from google_auth_oauthlib.flow import InstalledAppFlow
from googleapiclient.errors import HttpError
from googleapiclient.http import MediaFileUpload
from google.auth.transport.requests import Request

# === CONFIG / ENV ===
CLIENT_SECRETS = Path("client_secrets.json") 
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
def send_telegram(message: str):
    if not TELEGRAM_BOT_TOKEN or not TELEGRAM_CHAT_ID:
        return
    try:
        url = f"https://api.telegram.org/bot{TELEGRAM_BOT_TOKEN}/sendMessage"
        payload = {"chat_id": TELEGRAM_CHAT_ID, "text": message, "parse_mode": "Markdown"}
        requests.post(url, data=payload, timeout=10)
    except Exception as e:
        print(f"⚠️ Failed to send Telegram message: {e}")

def load_processed():
    if PROCESSED_FILE.exists():
        try:
            return set(json.loads(PROCESSED_FILE.read_text(encoding="utf-8")))
        except Exception as e:
            print(f"⚠️ Failed to load processed file: {e}")
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
            FFMPEG.replace("ffmpeg", "ffprobe") if False else FFPROBE, "-v", "error",
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

def get_youtube_client():
    creds = None
    if TOKEN_FILE.exists():
        try:
            creds = Credentials.from_authorized_user_file(str(TOKEN_FILE), YOUTUBE_SCOPES)
        except Exception:
            creds = None

    if not creds or not creds.valid:
        if creds and creds.expired and creds.refresh_token:
            try:
                creds.refresh(Request())  # requires import if used
            except Exception as e:
                print("⚠️ Failed to refresh credentials:", e)
                creds = None
        if not creds or not creds.valid:
            if not CLIENT_SECRETS.exists():
                print("❌ client_secrets.json not found. Cannot initiate OAuth flow.")
                sys.exit(1)
            print("🔑 You need to authorize YouTube access. Starting console flow...")
            flow = InstalledAppFlow.from_client_secrets_file(str(CLIENT_SECRETS), YOUTUBE_SCOPES)
            creds = flow.run_local_server(port=0)
            with open(TOKEN_FILE, "w", encoding="utf-8") as f:
                f.write(creds.to_json())
            print("✅ New token saved to", TOKEN_FILE)
    return build("youtube", "v3", credentials=creds)

def upload_to_youtube(video_path, caption):
    youtube = get_youtube_client()
    title_snippet = caption[:60].strip() or "Viral Hack Reel"
    title = f"{title_snippet} | Hack Tips"
    hashtags = generate_hacking_trending_hashtags(caption)
    description = f"{caption}\n\n{hashtags}"
    body = {
        "snippet": {
            "title": title,
            "description": description,
            "tags": hashtags.split()[:10],
            "categoryId": "22"
        },
        "status": {"privacyStatus": "public"}
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
        msg = f"✅ Uploaded → https://youtu.be/{vid}"
        print(msg)
        send_telegram(f"✅ [Upload] {msg}")
        return True
    except HttpError as e:
        msg = f"❌ Upload failed: {e}"
        print(msg)
        send_telegram(f"❌ [Upload] {msg}")
        return False

async def inject_cookies(context):
    if not IG_COOKIES_JSON:
        msg = "IG_COOKIES_JSON env not set."
        print(f"❌ {msg}")
        send_telegram(f"❌ [Cookies] {msg}")
        return
    try:
        cookies = json.loads(IG_COOKIES_JSON)
        await context.add_cookies(cookies)
        print("✅ Cookies injected")
    except Exception as e:
        msg = f"❌ Failed to inject cookies: {e}"
        print(msg)
        send_telegram(f"❌ [Cookies] {msg}")

async def fetch_reel_links():
    if not INSTAGRAM_PROFILE:
        msg = "INSTAGRAM_PROFILE not set."
        print(f"❌ {msg}")
        send_telegram(f"❌ [Config] {msg}")
        return []
    async with async_playwright() as p:
        browser = await p.chromium.launch(headless=True)
        context = await browser.new_context(user_agent=USER_AGENT_IPHONE)
        await inject_cookies(context)
        page = await context.new_page()
        target = f"https://www.instagram.com/{INSTAGRAM_PROFILE}/reels/"
        print(f"🔍 Loading {target}")
        try:
            await page.goto(target, timeout=60000)
            await page.wait_for_selector('a[href*="/reel/"]', timeout=30000)
            await asyncio.sleep(2)
            hrefs = await page.eval_on_selector_all('a[href*="/reel/"]', "els => els.map(e => e.href)")
            return list(dict.fromkeys(hrefs))
        except Exception as e:
            await page.screenshot(path="debug_reels_error.png")
            msg = f"Error loading reels: {e}"
            print(msg)
            send_telegram(f"❌ [Reels] {msg}")
            return []
        finally:
            await browser.close()

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
        return filename, caption

async def main():
    send_telegram(f"🚀 Run started for @{INSTAGRAM_PROFILE}, limit={UPLOAD_LIMIT}")
    processed = load_processed()
    reels = await fetch_reel_links()
    to_upload = [r for r in reels if r not in processed][:UPLOAD_LIMIT]
    if not to_upload:
        send_telegram("⚠️ No new reels to upload.")
        return
    for link in to_upload:
        try:
            file, caption = download_reel(link)
            trimmed = trim_short(file)
            success = upload_to_youtube(trimmed, caption)
            if success:
                processed.add(link)
                os.remove(file)
                if trimmed != file:
                    os.remove(trimmed)
            else:
                break
        except Exception as e:
            msg = f"Error processing {link}: {e}"
            print(msg)
            send_telegram(f"❌ [Process] {msg}")
def save_processed(processed_set):
    # Sort and save to file
    content = json.dumps(sorted(list(processed_set)), indent=2)
    PROCESSED_FILE.write_text(content, encoding="utf-8")
    print("📝 Processed reels saved locally.")

    # Optional Telegram backup
    if TELEGRAM_BOT_TOKEN and TELEGRAM_CHAT_ID:
        try:
            url = f"https://api.telegram.org/bot{TELEGRAM_BOT_TOKEN}/sendDocument"
            with open(PROCESSED_FILE, "rb") as f:
                files = {"document": f}
                data = {
                    "chat_id": TELEGRAM_CHAT_ID,
                    "caption": "📄 Updated processed_reels.json"
                }
                response = requests.post(url, files=files, data=data, timeout=10)
                if response.status_code == 200:
                    print("✅ Backup sent to Telegram.")
                else:
                    print(f"⚠️ Telegram upload failed: {response.text}")
        except Exception as e:
            print(f"⚠️ Failed to upload processed file to Telegram: {e}")

    send_telegram("🏁 Done. Uploaded reels.")

if __name__ == "__main__":
    try:
        asyncio.run(main())
    except KeyboardInterrupt:
        send_telegram("🛑 Run interrupted by user.")
