import os
import re
import time
import asyncio
import threading
from urllib.parse import urlparse, parse_qs, unquote
from typing import Dict, Any, Optional, List
import concurrent.futures

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

# ==================== RENDER WEB SERVER (KEEP ALIVE) ====================
web_app = Flask(__name__)

@web_app.route("/")
def home():
    return "⚡ TeraBox Turbo Telegram Bot is Active & Running 24/7 on Render!"

def run_web():
    port = int(os.environ.get("PORT", 10000))
    web_app.run(host="0.0.0.0", port=port)

# ==================== TERABOX RESOLVER ENGINE (STANDALONE) ====================
API_ENDPOINT = "https://api.teraboxdl.site/api/test"
USER_AGENT = "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/124.0.0.0 Safari/537.36"

_cached_working_proxy: Optional[str] = None
_proxy_pool: List[str] = []
_proxy_pool_time: float = 0

def fetch_fresh_proxies() -> List[str]:
    global _proxy_pool, _proxy_pool_time
    now = time.time()
    if _proxy_pool and (now - _proxy_pool_time < 300):
        return _proxy_pool

    sources = [
        "https://api.proxyscrape.com/v2/?request=displayproxies&protocol=http&timeout=3000&country=all&ssl=yes&anonymity=elite",
        "https://raw.githubusercontent.com/TheSpeedX/SOCKS-List/master/http.txt",
    ]
    proxies = []
    for src in sources:
        try:
            r = requests.get(src, timeout=4)
            if r.status_code == 200:
                lines = [line.strip() for line in r.text.splitlines() if line.strip() and ":" in line]
                proxies.extend(lines)
                if len(proxies) >= 30:
                    break
        except Exception:
            continue

    _proxy_pool = list(dict.fromkeys(proxies))[:50]
    _proxy_pool_time = now
    return _proxy_pool

def _try_single_request(url: str, proxy: Optional[str] = None, timeout: int = 6) -> Optional[dict]:
    headers = {"User-Agent": USER_AGENT, "Accept": "application/json", "Content-Type": "application/json"}
    payload = {"url": url.strip()}
    proxies = {"http": f"http://{proxy}", "https": f"http://{proxy}"} if proxy and not proxy.startswith("http") else ({"http": proxy, "https": proxy} if proxy else None)

    try:
        r = requests.post(API_ENDPOINT, json=payload, headers=headers, proxies=proxies, timeout=timeout)
        if r.status_code == 200:
            data = r.json()
            if data.get("status") == "success" and data.get("data", {}).get("list"):
                return data
    except Exception:
        pass
    return None

def resolve_via_rotating_proxies(share_url: str) -> dict:
    global _cached_working_proxy
    if _cached_working_proxy:
        data = _try_single_request(share_url, _cached_working_proxy, timeout=5)
        if data:
            return data

    proxies = fetch_fresh_proxies()
    if not proxies:
        raise RuntimeError("Could not retrieve active proxies for IP bypass.")

    with concurrent.futures.ThreadPoolExecutor(max_workers=15) as executor:
        future_to_proxy = {executor.submit(_try_single_request, share_url, p, 6): p for p in proxies[:25]}
        for future in concurrent.futures.as_completed(future_to_proxy):
            p = future_to_proxy[future]
            try:
                data = future.result()
                if data:
                    _cached_working_proxy = p
                    return data
            except Exception:
                continue

    raise RuntimeError("All proxy rotation nodes failed.")

def resolve_terabox_link(share_url: str) -> Dict[str, Any]:
    url_str = share_url.strip()

    if "player.teraboxdl.site" in url_str:
        parsed = urlparse(url_str)
        qs = parse_qs(parsed.query)
        return {
            "filename": qs.get("filename", ["video.mp4"])[0],
            "size": 0,
            "formatted_size": qs.get("size", [""])[0] or "--",
            "direct_link": qs.get("direct", [""])[0],
            "thumbnail_url": qs.get("poster", [None])[0],
        }

    if "dl-worker.teraboxdl.site" in url_str:
        return {
            "filename": "video.mp4",
            "size": 0,
            "formatted_size": "--",
            "direct_link": url_str,
            "thumbnail_url": None,
        }

    headers = {"User-Agent": USER_AGENT, "Accept": "application/json", "Content-Type": "application/json"}
    payload = {"url": url_str}
    data = None
    try:
        r = requests.post(API_ENDPOINT, json=payload, headers=headers, timeout=5)
        if r.status_code == 200:
            resp_json = r.json()
            if resp_json.get("status") == "success" and resp_json.get("data", {}).get("list"):
                data = resp_json
        else:
            data = resolve_via_rotating_proxies(url_str)
    except Exception:
        data = resolve_via_rotating_proxies(url_str)

    if not data or data.get("status") != "success":
        raise RuntimeError("Failed to resolve TeraBox link.")

    file_list = data.get("data", {}).get("list", [])
    if not file_list:
        raise ValueError("No files found in the provided TeraBox link.")

    f = file_list[0]
    return {
        "filename": f.get("server_filename", "video.mp4"),
        "size": int(f.get("size", 0)),
        "formatted_size": f.get("formatted_size", f"{int(f.get('size', 0)) / (1024*1024):.2f} MB"),
        "direct_link": f.get("direct_link") or f.get("stream_download_url") or f.get("dlink"),
        "thumbnail_url": f.get("thumbs", {}).get("url3")
    }

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

# সকল TeraBox ও Videy ডোমেইন লিস্ট
TERABOX_DOMAINS = [
    "terabox.com", "1024tera.com", "1024terabox.com", "terasharefile.com",
    "terabox.app", "freeterabox.com", "mirrobox.com", "nephobox.com",
    "4funbox.com", "momerybox.com", "tibibox.com", "teraboxlink.com",
    "teraboxshare.com", "playterabox.com", "flexdisk.net", "teraboxdl.site",
    "teraboxurl.com", "videyyy.com", "freevidey.com", "videynow.com", "myvidey.com"
]

def extract_terabox_url(text: str) -> str:
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
        r = requests.get(direct_url, stream=True, headers={"User-Agent": USER_AGENT}, timeout=120)
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
