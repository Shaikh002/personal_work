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
from openai import OpenAI
from pytrends.request import TrendReq

# === CONFIG / ENV ===
CLIENT_SECRETS = Path("client_secrets.json") 
INSTAGRAM_PROFILE = os.getenv("INSTAGRAM_PROFILE", "").strip()
IG_COOKIES_JSON = os.getenv("IG_COOKIES_JSON")
PROCESSED_FILE = Path("processed_reels.json")
DOWNLOAD_DIR = Path("downloads")
THUMBNAIL_DIR = Path("thumbnails")
TOKEN_FILE = Path("token.json")
UPLOAD_LIMIT = int(os.getenv("UPLOAD_LIMIT", "1"))
YOUTUBE_SCOPES = [
    "https://www.googleapis.com/auth/youtube.upload",
    "https://www.googleapis.com/auth/youtube.force-ssl"
]
MAX_SHORT_SECONDS = 60
USER_AGENT_IPHONE = "Mozilla/5.0 (iPhone; CPU iPhone OS 14_0 like Mac OS X) AppleWebKit/605.1.15 (KHTML, like Gecko) Version/14.0 Mobile/15E148 Safari/604.1"
FFMPEG = "ffmpeg"
FFPROBE = "ffprobe"

TELEGRAM_BOT_TOKEN = os.getenv("TELEGRAM_BOT_TOKEN")
TELEGRAM_CHAT_ID = os.getenv("TELEGRAM_CHAT_ID")
client = OpenAI()

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

def determine_category_id(caption: str) -> str:
    caption_lower = caption.lower()
    if any(kw in caption_lower for kw in ["hack", "wifi", "nmap", "bug", "exploit", "payload", "malware", "phishing", "ethical", "osint"]):
        return "26"  # How-to & Style
    if any(kw in caption_lower for kw in ["tutorial", "learn", "how to", "guide", "class", "course", "lesson"]):
        return "27"  # Education
    if any(kw in caption_lower for kw in ["review", "setup", "tech", "gadget", "automation", "linux", "ai", "tools", "app", "code", "script"]):
        return "28"  # Science & Tech
    return "22"  # Default

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

def get_video_probe(path: str):
    """Return width, height, fps, duration (sec) using ffprobe."""
    try:
        cmd = [
            FFPROBE, "-v", "error",
            "-select_streams", "v:0",
            "-show_entries", "stream=width,height,avg_frame_rate:format=duration",
            "-of", "json", path
        ]
        out = subprocess.check_output(cmd).decode("utf-8", errors="ignore")
        data = json.loads(out)
        stream = data["streams"][0]
        width = int(stream.get("width", 0))
        height = int(stream.get("height", 0))
        afr = stream.get("avg_frame_rate", "0/1")
        num, den = (afr.split("/") + ["1"])[:2]
        fps = float(num) / float(den) if float(den) != 0 else 0.0
        duration = float(data.get("format", {}).get("duration", 0.0))
        return width, height, fps, duration
    except Exception as e:
        send_telegram(f"⚠️ ffprobe failed: {e}")
        return 0, 0, 0.0, 0.0

def quality_label(w: int, h: int) -> str:
    """Classify as FULL HD if short side >= 1080, else SD."""
    return "FULL HD" if min(w, h) >= 1080 else "SD"
# --- Robust Google Trends with retry + fallback ---
CACHED_TRENDS = [
    "AI tools", "Cybersecurity", "Ethical hacking", "Bug bounty",
    "Linux commands", "Automation tools", "Python", "Nmap", "Kali Linux", "Malware analysis"
]

def _fetch_trends_india_raw(max_items=10, retries=3):
    """Low-level fetch from Google Trends with retries, returns raw list (no filtering)."""
    delay = 2
    for attempt in range(1, retries + 1):
        try:
            pytrends = TrendReq(hl='en-IN', tz=330)
            df = pytrends.trending_searches(pn='india')
            items = [t for t in df[0].tolist() if isinstance(t, str)]
            if items:
                return items[:max_items]
        except Exception as e:
            if attempt == retries:
                send_telegram(f"⚠️ Trends fetch failed after {retries} attempts: {e}")
                break
            time.sleep(delay)
            delay *= 2  # exponential backoff
    return []

def get_trending_keywords_india(limit=5):
    """
    Return up to `limit` *relevant* trend keywords for hashtagging.
    Filters to your niche; falls back to cached topics when Trends fails.
    """
    niche_needles = ("tech", "hack", "cyber", "ai", "app", "gadget", "phone", "security", "linux", "tools")
    raw = _fetch_trends_india_raw(max_items=20, retries=3)
    if raw:
        filtered = [kw for kw in raw if any(n in kw.lower() for n in niche_needles)]
        if not filtered:
            # If nothing matches niche, at least return the top few raw trends
            filtered = raw[:limit]
        chosen = filtered[:limit]
        send_telegram("📈 Google Trends India (live): " + ", ".join(chosen))
        return chosen
    # Fallback
    fallback = CACHED_TRENDS[:limit]
    send_telegram("📉 Using cached trends (fallback): " + ", ".join(fallback))
    return fallback

def get_live_trends(count=5):
    """
    Return up to `count` live trends (unfiltered) for titles.
    Falls back to cached topics when Trends fails.
    """
    raw = _fetch_trends_india_raw(max_items=count, retries=3)
    if raw:
        return raw[:count]
    return CACHED_TRENDS[:count]
# --- End robust trends ---

def generate_hacking_trending_hashtags(caption, total=8):
    ig_tags = re.findall(r"#\w+", caption)
    ig_tags = list(dict.fromkeys(ig_tags))[:3]  # keep first 3 IG tags
    hacking_tags = ["#ethicalhacking", "#cybersecurity"]
    trending_tags = ["#tech", "#shorts", "#trending", "#automation"]

    # Add trending keywords as hashtags
    trend_keywords = get_trending_keywords_india(limit=3)
    trend_tags = [f"#{t.replace(' ', '')}" for t in trend_keywords]

    combined = list(dict.fromkeys(ig_tags + hacking_tags + trending_tags + trend_tags))
    return " ".join(combined[:total])

def ensure_exact_hashtags(tags, desired=8):
    """Deduplicate, then pad with stable niche tags to reach exactly N."""
    base_pool = list(dict.fromkeys(tags))
    if len(base_pool) >= desired:
        return base_pool[:desired]
    # deterministic padding so tags are stable
    padding_source = list(dict.fromkeys(
        HACKING_TAGS + TRENDING_TAGS + [f"#{t.replace(' ', '')}" for t in CACHED_TRENDS]
    ))
    for t in padding_source:
        if t not in base_pool:
            base_pool.append(t)
        if len(base_pool) == desired:
            break
    return base_pool[:desired]

def generate_thumbnail(text, output_path):
    try:
        THUMBNAIL_DIR.mkdir(exist_ok=True)
        img = Image.new("RGB", (1280, 720), color=(0, 0, 0))
        draw = ImageDraw.Draw(img)
        try:
            font = ImageFont.truetype("DejaVuSans-Bold.ttf", 48)
        except:
            font = ImageFont.load_default()
        draw.text((100, 300), text, font=font, fill=(255, 255, 255))
        img.save(output_path)
        return output_path
    except Exception as e:
        send_telegram(f"❌ Thumbnail error: {e}")
        return None
 
def fallback_title_from_caption(caption: str) -> str:
    # Extract keywords from caption
    words = re.findall(r"\b[a-zA-Z0-9]{3,}\b", caption.lower())
    filtered = [w for w in words if w not in _STOPWORDS][:3]

    # Fetch live trends
    trends = get_live_trends(count=3)
    trends_part = " ".join(trends)

    # Merge caption + trends
    base = " ".join(w.title() for w in filtered) if filtered else "Tech Tips"
    final_title = f"{base} – {trends_part} #shorts".strip()
    return final_title

def generate_ai_title(caption: str) -> str:
    trends = get_live_trends(count=3)
    trends_text = ", ".join(trends) if trends else ""
    prompt = (
        f"Generate a catchy YouTube Shorts title (max 70 characters) for a hacking-themed reel with this caption:\n\n"
        f"{caption}\n\n"
        f"Include at least one of these trending Indian keywords if possible: {trends_text}\n"
        f"Avoid clickbait, keep it smart and tech-focused."
    )

    try:
        response = client.chat.completions.create(
            model="gpt-4",
            messages=[{"role": "user", "content": prompt}],
            max_tokens=50,
            temperature=0.8
        )
        return response.choices[0].message.content.strip()
    except Exception as e:
        send_telegram(f"⚠️ GPT-4 failed → gpt-3.5 fallback\nReason: {e}")
        try:
            response = client.chat.completions.create(
                model="gpt-3.5-turbo",
                messages=[{"role": "user", "content": prompt}],
                max_tokens=50,
                temperature=0.8
            )
            return response.choices[0].message.content.strip()
        except Exception as ex:
            send_telegram(f"❌ AI title unavailable → Using caption+trends\nReason: {ex}")
            return fallback_title_from_caption(caption)

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
                creds.refresh(Request())
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

def comment_and_pin(youtube, video_id, comment_text="🔥 Follow for more hacking tips!"):
    try:
        comment_response = youtube.commentThreads().insert(
            part="snippet",
            body={
                "snippet": {
                    "videoId": video_id,
                    "topLevelComment": {
                        "snippet": {
                            "textOriginal": comment_text
                        }
                    }
                }
            }
        ).execute()

        comment_id = comment_response["id"]
        youtube.comments().setModerationStatus(
            id=comment_id,
            moderationStatus="published"
        ).execute()

        youtube.comments().markAsSpam(
            id=comment_id,
            spam=False
        ).execute()

        youtube.comments().setModerationStatus(
            id=comment_id,
            moderationStatus="published"
        ).execute()
        
        send_telegram("📌 Auto-comment added and pinned.")

    except Exception as e:
        send_telegram(f"❌ Failed to comment/pin: {e}")
        
# Modify upload_to_youtube to call comment_and_pin after upload

def upload_to_youtube(video_path, caption, ig_tags):
    youtube = get_youtube_client()
    try:
        ai_title = generate_ai_title(caption)
        title = ai_title.strip()
        if not title.lower().endswith("#shorts"):
            title += " #shorts"
    except Exception as e:
        send_telegram(f"❌ AI title generation failed, using fallback.\n{e}")
        title = fallback_title_from_caption(caption)

    # --- Build final hashtags ---
    trend_keywords = get_trending_keywords_india(limit=3)
    trend_tags = [f"#{t.replace(' ', '')}" for t in trend_keywords]

    raw_tags = ig_tags + HACKING_TAGS[:3] + TRENDING_TAGS[:3] + trend_tags
    final_tags = ensure_exact_hashtags(raw_tags, desired=8)

    description = f"""{caption}

🎯 Learn hacking tools, tech tricks, automation, and ethical cybersecurity tips.
🚀 Follow for daily tutorials and #shorts content that educates and entertains!
   Comment your favorite tool below!

    {' '.join(final_tags)}
"""
    thumb_path = generate_thumbnail(title, THUMBNAIL_DIR / "thumb.jpg")

    body = {
        "snippet": {
            "title": title,
            "description": description,
            "tags": final_tags,
            "categoryId": determine_category_id(caption)
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
        comment_and_pin(youtube, vid)
        return True
    except HttpError as e:
        send_telegram(f"❌ Upload failed: {e}")
        return False


def filter_relevant_hashtags(caption, allowed_keywords, max_count=3):
    """Extracts up to `max_count` hashtags from caption that match allowed_keywords."""
    hashtags = re.findall(r"#\w+", caption)
    hashtags = [tag for tag in hashtags if any(kw in tag.lower() for kw in allowed_keywords)]
    return list(dict.fromkeys(hashtags))[:max_count]

def download_reel(url, idx=None, total=None):
    DOWNLOAD_DIR.mkdir(exist_ok=True)
    opts = {"format": "mp4", "outtmpl": str(DOWNLOAD_DIR / "%(id)s.%(ext)s"), "quiet": True}
    try:
        with YoutubeDL(opts) as ydl:
            info = ydl.extract_info(url, download=True)
            filename = ydl.prepare_filename(info)

            # --- Keep FULL caption for AI title ---
            full_caption = info.get("description") or info.get("title") or ""
            full_caption = full_caption.strip()

            # --- Extract only relevant hashtags ---
            niche_keywords = ["hack", "hacking", "cyber", "security", "bug", "tech", "ai", "automation", "linux", "tools"]
            filtered_tags = filter_relevant_hashtags(full_caption, niche_keywords, max_count=3)

            # --- Remove hashtags & @mentions from the clean caption for description/title ---
            clean_caption = re.sub(r'@\w+', '', full_caption)
            clean_caption = re.sub(r'#\w+', '', clean_caption).strip()

            # 📊 Check resolution
            w, h, fps, dur = get_video_probe(filename)
            reel_num = f"[{idx}/{total}]" if idx and total else ""
            send_telegram(
                f"🎥 {w}x{h} @ {round(fps,1)}fps ({quality_label(w, h)}) | {round(dur,1)}s {reel_num}\n"
                f"Profile: {INSTAGRAM_PROFILE}\nURL: {url}\n"
                f"📌 Kept hashtags: {' '.join(filtered_tags) if filtered_tags else 'None'}"
            )

            return filename, clean_caption, filtered_tags
    except Exception as e:
        send_telegram(f"❌ Download error for {url}: {e}")
        raise


async def inject_cookies(context):
    try:
        cookies = json.loads(IG_COOKIES_JSON)
        await context.add_cookies(cookies)
    except Exception as e:
        send_telegram(f"❌ Cookie injection error: {e}")
        raise  # stop if cookies fail, so IG login doesn’t break

async def upload_debug_screenshot_and_html(page):
    try:
        page_screenshot = "debug_reels.png"
        page_html = "debug_reels.html"
        await page.screenshot(path=page_screenshot, full_page=True)
        html_content = await page.content()
        Path(page_html).write_text(html_content, encoding="utf-8")

        url = f"https://api.telegram.org/bot{TELEGRAM_BOT_TOKEN}/sendPhoto"
        with open(page_screenshot, "rb") as photo:
            requests.post(url, files={"photo": photo}, data={"chat_id": TELEGRAM_CHAT_ID, "caption": "⚠️ No reels found. Screenshot of IG page."})

        url = f"https://api.telegram.org/bot{TELEGRAM_BOT_TOKEN}/sendDocument"
        with open(page_html, "rb") as doc:
            requests.post(url, files={"document": doc}, data={"chat_id": TELEGRAM_CHAT_ID, "caption": "📄 HTML content from IG page."})
    except Exception as e:
        send_telegram(f"❌ Failed to upload debug screenshot: {e}")

# ---------------------- Reel Fetching Logic ----------------------


async def fetch_reel_links():
    async with async_playwright() as p:
        browser = await p.chromium.launch(
            headless=False,
            args=["--disable-blink-features=AutomationControlled", "--no-sandbox"]
        )
        context = await browser.new_context(
            user_agent=USER_AGENT_IPHONE,
            viewport={"width": 375, "height": 812},
            device_scale_factor=2,
            is_mobile=True,
            has_touch=True,
            locale="en-US"
        )
        await inject_cookies(context)
        page = await context.new_page()

        try:
            url = f"https://www.instagram.com/{INSTAGRAM_PROFILE}/reels/"
            await page.goto(url, timeout=60000)
            await page.wait_for_load_state("networkidle")
            await asyncio.sleep(3)

            for _ in range(10):
                await page.mouse.wheel(0, 2000)
                await asyncio.sleep(1.5)

            hrefs = await page.eval_on_selector_all(
                'a[href*="/reel/"]',
                "els => els.map(e => e.href)"
            )

            print(f"🔗 Reels fetched: {len(hrefs)}")
            for h in hrefs[:5]:
                print("Sample reel:", h)

            if not hrefs:
                await upload_debug_screenshot_and_html(page)
                html_content = await page.content()
                Path("debug_reels.html").write_text(html_content, encoding="utf-8")

            return list(dict.fromkeys(hrefs))

        except Exception as e:
            send_telegram(f"❌ IG Reel Fetch Error: {e}")
            return []
        finally:
            await browser.close()


# ---------------------- Main Upload Logic ----------------------
def extract_shortcode(url):
    return url.split("/")[-2]

async def main():
    send_telegram(f"🚀 Starting IG → YT run | Profile: @{INSTAGRAM_PROFILE} | Limit: {UPLOAD_LIMIT}")
    processed = load_processed()
    reels = await fetch_reel_links()

    to_upload = []
    for url in reels:
        shortcode = extract_shortcode(url)
        if shortcode not in processed:
            to_upload.append((url, shortcode))
        if len(to_upload) >= UPLOAD_LIMIT:
            break

    if not to_upload:
        print("⚠️ No new reels found. Sending alert...")
        send_telegram("⚠️ No new reels found. Either all reels are uploaded or fetch failed.")
        return

    for idx, (link, shortcode) in enumerate(to_upload, start=1):
        try:
            file, clean_caption, filtered_tags = download_reel(link, idx=idx, total=len(to_upload))
            success = upload_to_youtube(file, clean_caption, filtered_tags)
            if success:
                processed.add(shortcode)
                print(f"✅ Uploaded: {shortcode}")
            os.remove(file)
        except Exception as e:
            send_telegram(f"❌ Processing error for {link}: {e}")

    save_processed(processed)


# ---------------------- Run It ----------------------
if __name__ == "__main__":
    asyncio.run(main())