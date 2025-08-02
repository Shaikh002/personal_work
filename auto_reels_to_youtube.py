
#!/usr/bin/env python3
import os, json, time, asyncio, subprocess, re, random, sys
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
from PIL import Image, ImageDraw, ImageFont
import openai

# === CONFIG / ENV ===
CLIENT_SECRETS = Path("client_secrets.json") 
INSTAGRAM_PROFILE = os.getenv("INSTAGRAM_PROFILE", "").strip()
IG_COOKIES_JSON = os.getenv("IG_COOKIES_JSON")
PROCESSED_FILE = Path("processed_reels.json")
DOWNLOAD_DIR = Path("downloads")
THUMBNAIL_DIR = Path("thumbnails")
TOKEN_FILE = Path("token.json")
UPLOAD_LIMIT = int(os.getenv("UPLOAD_LIMIT", "1"))
YOUTUBE_SCOPES = ["https://www.googleapis.com/auth/youtube.upload"]
MAX_SHORT_SECONDS = 60
USER_AGENT_IPHONE = "Mozilla/5.0 (iPhone; CPU iPhone OS 14_0 like Mac OS X) AppleWebKit/605.1.15 (KHTML, like Gecko) Version/14.0 Mobile/15E148 Safari/604.1"
FFMPEG = "ffmpeg"
FFPROBE = "ffprobe"

TELEGRAM_BOT_TOKEN = os.getenv("TELEGRAM_BOT_TOKEN")
TELEGRAM_CHAT_ID = os.getenv("TELEGRAM_CHAT_ID")
openai.api_key = os.getenv("OPENAI_API_KEY")

_STOPWORDS = {"the", "and", "for", "with", "this", "that", "from", "your", "you", "are", "about", "have", "has", "not", "but", "just", "what", "when", "where", "who", "why", "how", "its", "it's", "can", "will", "get", "like", "new"}
HACKING_TAGS = ["#ethicalhacking", "#cybersecurity", "#bugbounty", "#infosec", "#penetrationtesting", "#redteam", "#vulnerability", "#securityresearch", "#threatintel", "#whitehat", "#hackerlife", "#securitytips", "#hackingtools"]
TRENDING_TAGS = ["#viral", "#trending", "#Shorts", "#foryou", "#explore", "#tech", "#contentcreator", "#daily", "#automation", "#viralshorts"]

def send_telegram(msg):
    if TELEGRAM_BOT_TOKEN and TELEGRAM_CHAT_ID:
        try:
            url = f"https://api.telegram.org/bot{TELEGRAM_BOT_TOKEN}/sendMessage"
            payload = {"chat_id": TELEGRAM_CHAT_ID, "text": msg, "parse_mode": "Markdown"}
            requests.post(url, data=payload)
        except Exception as e:
            print(f"⚠️ Telegram error: {e}")

def load_processed():
    if PROCESSED_FILE.exists():
        try:
            return set(json.loads(PROCESSED_FILE.read_text(encoding="utf-8")))
        except Exception as e:
            send_telegram(f"❌ Error loading processed JSON: {e}")
    return set()

def save_processed(processed_set):
    try:
        PROCESSED_FILE.write_text(json.dumps(sorted(list(processed_set)), indent=2), encoding="utf-8")
        if TELEGRAM_BOT_TOKEN and TELEGRAM_CHAT_ID:
            url = f"https://api.telegram.org/bot{TELEGRAM_BOT_TOKEN}/sendDocument"
            with open(PROCESSED_FILE, "rb") as f:
                files = {"document": f}
                data = {"chat_id": TELEGRAM_CHAT_ID, "caption": "📄 Updated processed_reels.json"}
                requests.post(url, files=files, data=data)
    except Exception as e:
        send_telegram(f"❌ Failed to save processed file: {e}")

def extract_keywords(text, count=2):
    words = re.findall(r"\b[a-zA-Z0-9]{3,}\b", text.lower())
    freq = {}
    for w in words:
        if w in _STOPWORDS:
            continue
        freq[w] = freq.get(w, 0) + 1
    return [f"#{w}" for w, _ in sorted(freq.items(), key=lambda x: -x[1])[:count]]

def generate_hacking_trending_hashtags(caption, total=8):
    keywords = extract_keywords(caption, count=2)
    chosen = list(keywords)
    for tag in HACKING_TAGS + TRENDING_TAGS:
        if len(chosen) >= total:
            break
        if tag.lower() not in (t.lower() for t in chosen):
            chosen.append(tag)
    return " ".join(chosen[:total])

def generate_thumbnail(text, output_path):
    try:
        THUMBNAIL_DIR.mkdir(exist_ok=True)
        img = Image.new("RGB", (1280, 720), color=(0, 0, 0))
        draw = ImageDraw.Draw(img)
        font = ImageFont.truetype("DejaVuSans-Bold.ttf", 48)
        draw.text((100, 300), text, font=font, fill=(255, 255, 255))
        img.save(output_path)
        return output_path
    except Exception as e:
        send_telegram(f"❌ Thumbnail error: {e}")
        return None

def generate_ai_title(caption: str) -> str:
    prompt = f"Generate a catchy YouTube Shorts title (max 70 characters) for a hacking-themed reel with this caption:\n\n{caption}\n\nAvoid clickbait, keep it smart and tech-focused."
    try:
        response = openai.ChatCompletion.create(
            model="gpt-4",
            messages=[{"role": "user", "content": prompt}],
            max_tokens=50,
            temperature=0.8
        )
        return response.choices[0].message.content.strip()
    except Exception as e:
        send_telegram(f"❌ AI Title Error: {e}")
        return caption[:60]

def get_youtube_client():
    try:
        if TOKEN_FILE.exists():
            creds = Credentials.from_authorized_user_file(str(TOKEN_FILE), YOUTUBE_SCOPES)
        else:
            raise Exception("token.json missing")
        if not creds.valid and creds.refresh_token:
            creds.refresh(Request())
        return build("youtube", "v3", credentials=creds)
    except Exception as e:
        send_telegram(f"❌ YouTube auth error: {e}")
        sys.exit(1)

def upload_to_youtube(video_path, caption):
    youtube = get_youtube_client()
    ai_title = generate_ai_title(caption)
    title = f"{ai_title} #shorts"
    hashtags = generate_hacking_trending_hashtags(caption)
    description = f"{caption}\n\n{hashtags}"
    thumb_path = generate_thumbnail(ai_title, THUMBNAIL_DIR / "thumb.jpg")

    body = {
        "snippet": {
            "title": title,
            "description": description,
            "tags": hashtags.split()[:10],
            "categoryId": "27"
        },
        "status": {"privacyStatus": "public"}
    }

    try:
        req = youtube.videos().insert(
            part="snippet,status",
            body=body,
            media_body=MediaFileUpload(video_path)
        )
        res = req.execute()
        vid = res.get("id")
        send_telegram(f"✅ Uploaded → https://youtu.be/{vid}")
        return True
    except HttpError as e:
        send_telegram(f"❌ Upload failed: {e}")
        return False

def download_reel(url):
    DOWNLOAD_DIR.mkdir(exist_ok=True)
    opts = {"format": "mp4", "outtmpl": str(DOWNLOAD_DIR / "%(id)s.%(ext)s"), "quiet": True}
    try:
        with YoutubeDL(opts) as ydl:
            info = ydl.extract_info(url, download=True)
            filename = ydl.prepare_filename(info)
            caption = info.get("description") or info.get("title") or ""
            caption = re.sub(r'@\w+', '', caption)
            return filename, caption
    except Exception as e:
        send_telegram(f"❌ Download error for {url}: {e}")
        raise

async def inject_cookies(context):
    try:
        cookies = json.loads(IG_COOKIES_JSON)
        await context.add_cookies(cookies)
    except Exception as e:
        send_telegram(f"❌ Cookie injection error: {e}")

async def fetch_reel_links():
    async with async_playwright() as p:
        browser = await p.chromium.launch(headless=True)
        context = await browser.new_context(user_agent=USER_AGENT_IPHONE)
        await inject_cookies(context)
        page = await context.new_page()
        try:
            url = f"https://www.instagram.com/{INSTAGRAM_PROFILE}/reels/"
            await page.goto(url, timeout=60000)
            await page.wait_for_load_state("networkidle")
            await asyncio.sleep(2)
            hrefs = await page.eval_on_selector_all('a[href*="/reel/"]', "els => els.map(e => e.href)")
            return list(dict.fromkeys(hrefs))
        except Exception as e:
            send_telegram(f"❌ IG Reel Fetch Error: {e}")
            return []
        finally:
            await browser.close()

async def main():
    send_telegram(f"🚀 Starting IG → YT run | Profile: @{INSTAGRAM_PROFILE} | Limit: {UPLOAD_LIMIT}")
    processed = load_processed()
    reels = await fetch_reel_links()
    to_upload = [r for r in reels if r not in processed][:UPLOAD_LIMIT]
    if not to_upload:
        send_telegram("⚠️ No new reels found.")
        return

    for link in to_upload:
        try:
            file, caption = download_reel(link)
            success = upload_to_youtube(file, caption)
            processed.add(link)
            os.remove(file)
        except Exception as e:
            send_telegram(f"❌ Processing error for {link}: {e}")

    save_processed(processed)

if __name__ == "__main__":
    try:
        asyncio.run(main())
    except KeyboardInterrupt:
        send_telegram("🛑 Run interrupted.")
