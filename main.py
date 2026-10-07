import os
import re
import time
import threading
import requests
from flask import Flask
import terabox_api

# ==================== WEB SERVER (RENDER KEEP-ALIVE) ====================
web_app = Flask(__name__)

@web_app.route("/")
def home():
    return "⚡ TeraBox Turbo Telegram Bot is Active & Running 24/7!"

def run_web():
    port = int(os.environ.get("PORT", 8080))
    web_app.run(host="0.0.0.0", port=port)

# ==================== CONFIGURATION ====================
BOT_TOKEN = os.environ.get("BOT_TOKEN", "7796341783:AAGqJ3wgAimmyaYPastm7MMFSyXxdHYRuO8")
API_URL = f"https://api.telegram.org/bot{BOT_TOKEN}"

# ALL TERABOX & VIDEY DOMAINS
TERABOX_DOMAINS = [
    "terabox.com", "1024tera.com", "1024terabox.com", "terasharefile.com",
    "terabox.app", "freeterabox.com", "mirrobox.com", "nephobox.com",
    "4funbox.com", "momerybox.com", "tibibox.com", "teraboxlink.com",
    "teraboxshare.com", "playterabox.com", "flexdisk.net", "teraboxdl.site",
    "teraboxurl.com", "videyyy.com", "freevidey.com", "videynow.com", "myvidey.com"
]

def extract_url(text: str) -> str:
    urls = re.findall(r'(https?://[^\s]+)', text)
    for u in urls:
        u_lower = u.lower()
        if any(d in u_lower for d in TERABOX_DOMAINS):
            return u
        if any(k in u_lower for k in ["tera", "box", "1024", "videy"]) and any(p in u_lower for p in ["/s/", "/sharing/", "surl=", "/v/"]):
            return u
    if urls:
        return urls[0]
    return ""

def send_message(chat_id, text):
    try:
        requests.post(f"{API_URL}/sendMessage", json={"chat_id": chat_id, "text": text, "parse_mode": "HTML"}, timeout=15)
    except Exception:
        pass

def process_message(chat_id, text, msg_id):
    if text.startswith("/start"):
        send_message(chat_id, "⚡ <b>TeraBox Turbo Bot Active!</b>\n\nযেকোনো TeraBox বা Videy লিঙ্ক পাঠান, আমি ভিডিও পাঠিয়ে দেব!")
        return

    url = extract_url(text)
    if not url:
        send_message(chat_id, "❌ অনুগ্রহ করে একটি ভ্যালিড TeraBox ভিডিও লিঙ্ক পাঠান।")
        return

    send_message(chat_id, "🔍 <i>TeraBox ক্লাউড লিঙ্ক অ্যানালাইসিস করছি...</i>")

    try:
        meta = terabox_api.resolve_terabox_link(url)
        filename = meta["filename"]
        formatted_size = meta["formatted_size"]
        direct_link = meta["direct_link"]

        send_message(chat_id, f"⚡ <b>ডাউনলোড ও ডেলিভারি হচ্ছে...</b>\n📁 <code>{filename}</code>\n📦 <b>Size:</b> {formatted_size}")

        safe_filename = "".join(c for c in filename if c not in r'\/:*?"<>|').strip() or "video.mp4"
        temp_file = os.path.abspath(f"./temp_{msg_id}_{safe_filename}")

        # Download from CDN
        r = requests.get(direct_link, stream=True, headers={"User-Agent": terabox_api.USER_AGENT}, timeout=90)
        r.raise_for_status()
        with open(temp_file, "wb") as f:
            for chunk in r.iter_content(chunk_size=1024 * 512):
                if chunk:
                    f.write(chunk)

        # Upload video to Telegram
        caption = f"🎬 <b>{filename}</b>\n📦 <b>Size:</b> {formatted_size}\n\n⚡ <i>Delivered via TeraBox Turbo Bot</i>"
        with open(temp_file, "rb") as vf:
            files = {"video": (safe_filename, vf, "video/mp4")}
            data = {"chat_id": chat_id, "caption": caption, "parse_mode": "HTML", "supports_streaming": True}
            requests.post(f"{API_URL}/sendVideo", data=data, files=files, timeout=600)

        if os.path.exists(temp_file):
            os.remove(temp_file)

    except Exception as e:
        send_message(chat_id, f"❌ এরর হয়েছে: {str(e)}")

def run_bot():
    print("[+] TeraBox Turbo HTTP Bot Started! Listening for messages...")
    offset = None
    while True:
        try:
            params = {"timeout": 20}
            if offset:
                params["offset"] = offset

            resp = requests.get(f"{API_URL}/getUpdates", params=params, timeout=25)
            if resp.status_code == 200:
                data = resp.json()
                for update in data.get("result", []):
                    offset = update["update_id"] + 1
                    msg = update.get("message")
                    if msg and "text" in msg:
                        chat_id = msg["chat"]["id"]
                        text = msg["text"].strip()
                        msg_id = msg["message_id"]
                        print(f"[*] Received from {chat_id}: {text[:40]}")
                        process_message(chat_id, text, msg_id)
        except Exception as e:
            time.sleep(2)

if __name__ == "__main__":
    threading.Thread(target=run_web, daemon=True).start()
    run_bot()
