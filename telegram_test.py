#!/usr/bin/env python3
import os
import sys
import argparse
import requests

TELEGRAM_API = "https://api.telegram.org"

def send_message(bot_token, chat_id, text, parse_mode="Markdown"):
    url = f"{TELEGRAM_API}/bot{bot_token}/sendMessage"
    payload = {
        "chat_id": chat_id,
        "text": text,
        "parse_mode": parse_mode,
        "disable_web_page_preview": True,
    }
    r = requests.post(url, data=payload, timeout=10)
    if r.ok:
        print("✅ Message sent.")
    else:
        print(f"❌ Message failed: {r.status_code} {r.text}", file=sys.stderr)

def send_photo(bot_token, chat_id, photo_path, caption=None):
    if not os.path.exists(photo_path):
        print(f"❌ Photo not found: {photo_path}", file=sys.stderr)
        return
    url = f"{TELEGRAM_API}/bot{bot_token}/sendPhoto"
    with open(photo_path, "rb") as f:
        files = {"photo": f}
        data = {"chat_id": chat_id}
        if caption:
            data["caption"] = caption
        r = requests.post(url, data=data, files=files, timeout=30)
    if r.ok:
        print("✅ Photo sent.")
    else:
        print(f"❌ Photo failed: {r.status_code} {r.text}", file=sys.stderr)

def get_updates(bot_token):
    url = f"{TELEGRAM_API}/bot{bot_token}/getUpdates"
    r = requests.get(url, timeout=10)
    if r.ok:
        print("Current updates (use to extract your chat_id):")
        print(r.text)
    else:
        print(f"❌ getUpdates failed: {r.status_code} {r.text}", file=sys.stderr)

def main():
    parser = argparse.ArgumentParser(description="Test Telegram bot messaging.")
    parser.add_argument("--bot-token", default=os.getenv("TELEGRAM_BOT_TOKEN"), help="Telegram bot token")
    parser.add_argument("--chat-id", default=os.getenv("TELEGRAM_CHAT_ID"), help="Target chat ID")
    parser.add_argument("--message", "-m", help="Text message to send")
    parser.add_argument("--photo", "-p", help="Path to photo to send")
    parser.add_argument("--caption", "-c", help="Caption for photo")
    parser.add_argument("--get-updates", action="store_true", help="Fetch recent updates (to discover chat_id)")

    args = parser.parse_args()

    if not args.bot_token:
        print("❌ Bot token required (via --bot-token or TELEGRAM_BOT_TOKEN env)", file=sys.stderr)
        sys.exit(1)

    if args.get_updates:
        get_updates(args.bot_token)
        return

    if not args.chat_id:
        print("❌ Chat ID required (via --chat-id or TELEGRAM_CHAT_ID env)", file=sys.stderr)
        sys.exit(1)

    if args.photo:
        send_photo(args.bot_token, args.chat_id, args.photo, caption=args.caption)
    elif args.message:
        send_message(args.bot_token, args.chat_id, args.message)
    else:
        print("❌ Nothing to do. Provide --message or --photo.", file=sys.stderr)
        sys.exit(1)

if __name__ == "__main__":
    main()
