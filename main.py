import os
import re
import asyncio
import threading
from flask import Flask
from pyrogram import Client, filters
from pyrogram.types import Message
import requests

# ==================== RENDER WEB SERVER (KEEP ALIVE) ====================
# Render-এর Free Web Service সচল রাখার জন্য একটি লাইটওয়েট ওয়েব সার্ভার
web_app = Flask(__name__)

@web_app.route("/")
def home():
    return "⚡ TeraBox Turbo Telegram Bot is Active & Running 24/7 on Render!"

def run_web():
    port = int(os.environ.get("PORT", 10000))
    web_app.run(host="0.0.0.0", port=port)

# ==================== CONFIGURATION ====================
# Render Environment Variables থেকে নেওয়া হবে
API_ID = int(os.environ.get("API_ID", "1234567"))
API_HASH = os.environ.get("API_HASH", "your_api_hash_here")
BOT_TOKEN = os.environ.get("BOT_TOKEN", "your_bot_token_here")

bot = Client(
    "terabox_render_bot",
    api_id=API_ID,
    api_hash=API_HASH,
    bot_token=BOT_TOKEN
)

TERABOX_REGEX = r'(https?://(?:www\.)?(?:terabox|1024tera|terasharefile|teraboxapp|freeterabox|mirrobox|nephobox)\.com/\S+)'

def resolve_terabox_direct(share_url: str):
    """TeraBoxDL API থেকে ডাইরেক্ট CDN লিঙ্ক ফেচ করে"""
    api_url = "https://api.teraboxdl.site/api/test"
    headers = {
        "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36",
        "Referer": "https://teraboxdl.site/"
    }
    r = requests.post(api_url, json={"url": share_url}, headers=headers, timeout=20)
    r.raise_for_status()
    data = r.json()
    if data.get("status") == "success" and data.get("data", {}).get("list"):
        item = data["data"]["list"][0]
        return {
            "title": item.get("server_filename", "video.mp4"),
            "size": int(item.get("size", 0)),
            "direct_url": item.get("direct_link") or item.get("stream_download_url")
        }
    raise RuntimeError("Failed to extract direct CDN link from TeraBox.")

@bot.on_message(filters.command("start"))
async def start_cmd(client, message: Message):
    await message.reply_text(
        "⚡ <b>TeraBox Turbo Cloud Bot</b>\n\n"
        "যেকোনো TeraBox ভিডিও লিঙ্ক পাঠান, Render Cloud Pipe দিয়ে হাই-স্পিডে ভিডিও চলে আসবে!",
        parse_mode="html"
    )

@bot.on_message(filters.text & filters.private)
async def handle_link(client, message: Message):
    text = message.text.strip()
    match = re.search(TERABOX_REGEX, text)
    if not match:
        await message.reply_text("❌ অনুগ্রহ করে একটি ভ্যালিড TeraBox ভিডিও লিঙ্ক পাঠান।")
        return

    url = match.group(1)
    status_msg = await message.reply_text("🔍 <i>TeraBox ক্লাউড লিঙ্ক অ্যানালাইসিস করছি...</i>", parse_mode="html")

    try:
        info = resolve_terabox_direct(url)
        title = info["title"]
        size_mb = info["size"] / (1024 * 1024)
        direct_url = info["direct_url"]

        await status_msg.edit_text(
            f"⚡ <b>ক্লাউড পাইপলাইনে পাঠানো হচ্ছে...</b>\n"
            f"📁 <code>{title}</code>\n"
            f"📦 <b>Size:</b> {size_mb:.2f} MB\n\n"
            f"🚀 <i>Render 1 Gbps Backbone দিয়ে সরাসরি ডেলিভারি হচ্ছে...</i>",
            parse_mode="html"
        )

        safe_name = "".join(c for c in title if c not in r'\/:*?"<>|').strip() or "video.mp4"
        temp_path = f"temp_{message.id}_{safe_name}"

        # Render-এর 1 Gbps ক্লাউডে ডাউনলোড
        r = requests.get(direct_url, stream=True, headers={"User-Agent": "Mozilla/5.0"}, timeout=60)
        with open(temp_path, "wb") as f:
            for chunk in r.iter_content(chunk_size=1024 * 1024 * 2):
                if chunk:
                    f.write(chunk)

        # টেলিগ্রামে পাঠানো (Pyrogram MTProto - No 20MB / 50MB limits!)
        await client.send_video(
            chat_id=message.chat.id,
            video=temp_path,
            caption=(
                f"🎬 <b>{title}</b>\n"
                f"📦 <b>Size:</b> {size_mb:.2f} MB\n\n"
                f"⚡ <i>Delivered via Render Cloud Backbone</i>"
            ),
            parse_mode="html",
            supports_streaming=True
        )

        if os.path.exists(temp_path):
            os.remove(temp_path)

        await status_msg.delete()

    except Exception as e:
        await status_msg.edit_text(f"❌ এরর হয়েছে: {str(e)}")


if __name__ == "__main__":
    # 1. ব্যাকগ্রাউন্ডে Flask ওয়েব সার্ভার চালু রাখা (Render Free Web Service-এর জন্য)
    threading.Thread(target=run_web, daemon=True).start()
    
    # 2. মূল টেলিগ্রাম বট চালু করা
    print("[+] TeraBox Turbo Bot Starting on Render...")
    bot.run()
