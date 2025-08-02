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

import requests
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
IG_COOKIES_JSON = os.getenv("IG_COOKIES_JSON")
PROCESSED_FILE = Path("processed_reels.json")
DOWNLOAD_DIR = Path("downloads")
TOKEN_FILE = Path("token.json")
UPLOAD_LIMIT = int(os.getenv("UPLOAD_LIMIT", "1"))
YOUTUBE_SCOPES = ["https://www.googleapis.com/auth/youtube.upload"]
MAX_SHORT_SECONDS = 60
USER_AGENT_IPHONE = (
    "Mozilla/5.0 (iPhone; CPU iPhone OS 14_0 like Mac OS X) "
    "AppleWebKit/605.1.15 (KHTML, like Gecko) Version/14.0 "
    "Mobile/15E148 Safari/604.1"
)
FFMPEG = "ffmpeg"
FFPROBE = "ffprobe"

TELEGRAM_BOT_TOKEN = os.getenv("TELEGRAM_BOT_TOKEN")
TELEGRAM_CHAT_ID = os.getenv("TELEGRAM_CHAT_ID")

_STOPWORDS = {"the", "and", "for", "with", "this", "that", "from", "your", "you", "are",
    "about", "have", "has", "not", "but", "just", "what", "when", "where",
    "who", "why", "how", "its", "it's", "can", "will", "get", "like", "new"
}

HACKING_TAGS = ["#ethicalhacking", "#cybersecurity", "#bugbounty", "#infosec",
    "#penetrationtesting", "#redteam", "#vulnerability", "#securityresearch",
    "#threatintel", "#whitehat", "#hackerlife", "#securitytips", "#hackingtools"]
TRENDING_TAGS = ["#viral", "#trending", "#Shorts", "#foryou", "#explore", "#tech",
    "#contentcreator", "#daily", "#automation", "#viralshorts"]

def send_telegram(msg: str):
    if TELEGRAM_BOT_TOKEN and TELEGRAM_CHAT_ID:
        try:
            requests.post(f"https://api.telegram.org/bot{TELEGRAM_BOT_TOKEN}/sendMessage",
                data={"chat_id": TELEGRAM_CHAT_ID, "text": msg, "parse_mode": "Markdown"}, timeout=10)
        except: pass

def load_processed():
    if PROCESSED_FILE.exists():
        try:
            return set(json.loads(PROCESSED_FILE.read_text(encoding="utf-8")))
        except: return set()
    return set()

def save_processed(processed_set):
    PROCESSED_FILE.write_text(json.dumps(sorted(list(processed_set)), indent=2), encoding="utf-8")
    print("📝 Processed reels saved locally.")
    if TELEGRAM_BOT_TOKEN and TELEGRAM_CHAT_ID:
        try:
            with open(PROCESSED_FILE, "rb") as f:
                requests.post(f"https://api.telegram.org/bot{TELEGRAM_BOT_TOKEN}/sendDocument",
                    files={"document": f},
                    data={"chat_id": TELEGRAM_CHAT_ID, "caption": "📄 Updated processed_reels.json"}, timeout=10)
        except: pass

def extract_keywords(text, count=2):
    words = re.findall(r"\b[a-zA-Z0-9]{3,}\b", text.lower())
    freq = {}
    for w in words:
        if w not in _STOPWORDS:
            freq[w] = freq.get(w, 0) + 1
    return [f"#{w}" for w, _ in sorted(freq.items(), key=lambda x: -x[1])[:count]]

def generate_hashtags(caption, total=8):
    keywords = extract_keywords(caption, count=2)
    tags = keywords + [t for t in HACKING_TAGS + TRENDING_TAGS if t not in keywords]
    return " ".join(tags[:total])

def get_duration(path):
    try:
        return float(subprocess.check_output([FFPROBE, "-v", "error", "-show_entries",
            "format=duration", "-of", "default=noprint_wrappers=1:nokey=1", path]).decode().strip())
    except: return 0.0

def trim_short(path):
    dur = get_duration(path)
    if dur > MAX_SHORT_SECONDS:
        out = f"{os.path.splitext(path)[0]}_short.mp4"
        subprocess.run([FFMPEG, "-y", "-i", path, "-t", str(MAX_SHORT_SECONDS), "-c", "copy", out])
        return out
    return path

def get_youtube_client():
    creds = None
    if TOKEN_FILE.exists():
        try: creds = Credentials.from_authorized_user_file(str(TOKEN_FILE), YOUTUBE_SCOPES)
        except: pass
    if not creds or not creds.valid:
        if creds and creds.expired and creds.refresh_token:
            try: creds.refresh(Request())
            except: creds = None
        if not creds or not creds.valid:
            flow = InstalledAppFlow.from_client_secrets_file(str(CLIENT_SECRETS), YOUTUBE_SCOPES)
            creds = flow.run_local_server(port=0)
            with open(TOKEN_FILE, "w", encoding="utf-8") as f: f.write(creds.to_json())
    return build("youtube", "v3", credentials=creds)

def upload_to_youtube(path, caption):
    client = get_youtube_client()
    title = f"{caption[:60].strip()} | Hack Tips"
    description = f"{caption}\n\n{generate_hashtags(caption)}"
    body = {"snippet": {"title": title, "description": description, "tags": description.split()[:10], "categoryId": "22"}, "status": {"privacyStatus": "public"}}
    try:
        req = client.videos().insert(part="snippet,status", body=body, media_body=MediaFileUpload(path))
        vid = req.execute().get("id")
        msg = f"✅ Uploaded → https://youtu.be/{vid}"
        print(msg)
        send_telegram(msg)
        return True
    except HttpError as e:
        print(f"❌ Upload failed: {e}")
        send_telegram(f"❌ Upload failed: {e}")
        return False

async def inject_cookies(context):
    if IG_COOKIES_JSON:
        try: await context.add_cookies(json.loads(IG_COOKIES_JSON))
        except: pass

async def fetch_reel_links():
    if not INSTAGRAM_PROFILE:
        send_telegram("❌ Missing INSTAGRAM_PROFILE")
        return []
    async with async_playwright() as p:
        browser = await p.chromium.launch(headless=True)
        context = await browser.new_context(user_agent=USER_AGENT_IPHONE)
        await inject_cookies(context)
        page = await context.new_page()
        try:
            await page.goto(f"https://www.instagram.com/{INSTAGRAM_PROFILE}/reels/", timeout=60000)
            await page.wait_for_selector('a[href*="/reel/"]', timeout=30000)
            await page.wait_for_timeout(3000)
            for _ in range(3):
                await page.mouse.wheel(0, 2000)
                await page.wait_for_timeout(1000)
            hrefs = await page.eval_on_selector_all('a[href*="/reel/"]', "els => els.map(e => e.href)")
            links = [h for h in hrefs if re.match(r"https://www.instagram.com/reel/[\w\-]+/?", h)]
            return list(dict.fromkeys(links))
        except Exception as e:
            await page.screenshot(path="debug_reels_error.png")
            send_telegram(f"❌ Error fetching reels: {e}")
            return []
        finally:
            await browser.close()

def download_reel(url):
    DOWNLOAD_DIR.mkdir(exist_ok=True)
    opts = {"format": "mp4", "outtmpl": str(DOWNLOAD_DIR / "%(id)s.%(ext)s"), "quiet": True}
    with YoutubeDL(opts) as ydl:
        info = ydl.extract_info(url, download=True)
        return ydl.prepare_filename(info), info.get("description") or info.get("title") or ""

async def main():
    send_telegram(f"🚀 Upload started for @{INSTAGRAM_PROFILE}")
    processed = load_processed()
    reels = await fetch_reel_links()
    to_upload = [r for r in reels if r not in processed][:UPLOAD_LIMIT]
    if not to_upload:
        send_telegram("⚠️ No new reels found")
        return
    for url in to_upload:
        try:
            path, caption = download_reel(url)
            trimmed = trim_short(path)
            if upload_to_youtube(trimmed, caption):
                processed.add(url)
                os.remove(path)
                if trimmed != path: os.remove(trimmed)
            else: break
        except Exception as e:
            send_telegram(f"❌ Error processing {url}: {e}")
    save_processed(processed)

if __name__ == "__main__":
    try: asyncio.run(main())
    except KeyboardInterrupt:
        send_telegram("🛑 Script interrupted")
