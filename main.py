import os
import re
import asyncio
import threading

# Python 3.12+ / 3.14 compatibility fix
try:
    asyncio.get_event_loop()
except RuntimeError:
    loop = asyncio.new_event_loop()
    asyncio.set_event_loop(loop)

from flask import Flask
from pyrogram import Client, filters
from pyrogram.types import Message
import requests
from terabox_api import resolve_terabox_link

# ==================== RENDER WEB SERVER (KEEP ALIVE) ====================
web_app = Flask(__name__)

@web_app.route("/")
def home():
    return "⚡ TeraBox Turbo Telegram Bot is Active & Running 24/7 on Render!"

def run_web():
    port = int(os.environ.get("PORT", 10000))
    web_app.run(host="0.0.0.0", port=port)

# ==================== CONFIGURATION ====================
API_ID = int(os.environ.get("API_ID", "1234567"))
API_HASH = os.environ.get("API_HASH", "your_api_hash_here")
BOT_TOKEN = os.environ.get("BOT_TOKEN", "your_bot_token_here")

bot = Client(
    "terabox_render_bot",
    api_id=API_ID,
    api_hash=API_HASH,
    bot_token=BOT_TOKEN
)

# ==================== ALL TERABOX & VIDEY DOMAINS ====================
TERABOX_DOMAINS = [
    "terabox.com", "1024tera.com", "1024terabox.com", "terasharefile.com",
    "terabox.app", "freeterabox.com", "mirrobox.com", "nephobox.com",
    "4funbox.com", "momerybox.com", "tibibox.com", "teraboxlink.com",
    "teraboxshare.com", "playterabox.com", "flexdisk.net", "teraboxdl.site",
    "teraboxurl.com", "videyyy.com", "freevidey.com", "videynow.com", "myvidey.com"
]

def extract_terabox_url(text: str) -> str:
    """Detects and extracts any valid TeraBox or mirror share link."""
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

@bot.on_message(filters.command("start"))
async def start_cmd(client, message: Message):
    await message.reply_text(
        "⚡ <b>TeraBox Turbo Cloud Bot</b>\n\n"
        "যেকোনো TeraBox বা Videy ভিডিও লিঙ্ক পাঠান, Render Cloud Pipe দিয়ে হাই-স্পিডে ভিডিও চলে আসবে!",
        parse_mode="html"
    )

@bot.on_message(filters.text & filters.private)
async def handle_link(client, message: Message):
    text = message.text.strip()
    url = extract_terabox_url(text)
    if not url:
        await message.reply_text("❌ অনুগ্রহ করে একটি ভ্যালিড TeraBox ভিডিও লিঙ্ক পাঠান।")
        return

    status_msg = await message.reply_text("🔍 <i>TeraBox ক্লাউড লিঙ্ক অ্যানালাইসিস করছি...</i>", parse_mode="html")

    try:
        info = resolve_terabox_link(url)
        title = info["filename"]
        size_bytes = info["size"]
        size_mb = (size_bytes / (1024 * 1024)) if size_bytes else 0
        direct_url = info["direct_link"]
        size_label = f"{size_mb:.2f} MB" if size_mb else info.get("formatted_size", "--")

        await status_msg.edit_text(
            f"⚡ <b>ক্লাউড পাইপলাইনে পাঠানো হচ্ছে...</b>\n"
            f"📁 <code>{title}</code>\n"
            f"📦 <b>Size:</b> {size_label}\n\n"
            f"🚀 <i>Render 1 Gbps Backbone দিয়ে সরাসরি ডেলিভারি হচ্ছে...</i>",
            parse_mode="html"
        )

        safe_name = "".join(c for c in title if c not in r'\/:*?"<>|').strip() or "video.mp4"
        temp_path = f"temp_{message.id}_{safe_name}"

        # Render-এর 1 Gbps ক্লাউডে ডাউনলোড
        r = requests.get(direct_url, stream=True, headers={"User-Agent": "Mozilla/5.0"}, timeout=120)
        r.raise_for_status()
        with open(temp_path, "wb") as f:
            for chunk in r.iter_content(chunk_size=1024 * 1024 * 2):
                if chunk:
                    f.write(chunk)

        # টেলিগ্রামে পাঠানো (Pyrogram MTProto - No 20MB limit!)
        await client.send_video(
            chat_id=message.chat.id,
            video=temp_path,
            caption=(
                f"🎬 <b>{title}</b>\n"
                f"📦 <b>Size:</b> {size_label}\n\n"
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
    threading.Thread(target=run_web, daemon=True).start()
    print("[+] TeraBox Turbo Bot Starting on Render...")
    bot.run()
