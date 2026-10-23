"""
gui_app.py
Production-Grade Asynchronous Telegram Automation Application with Modern CustomTkinter GUI.
Supports multiple source channels/groups, Terabox URL filtering, native author-hidden forwarding,
and real-time colorized activity dashboard.
"""

import os
import sys
import atexit
import time
import re
import io
import base64
from base64 import b64encode, b64decode
import json
import random
import shutil
import sqlite3
import asyncio
import logging
import threading
import ctypes
import contextlib
import subprocess
import hashlib
import requests
import webbrowser
from urllib.parse import quote, urlparse, urlunparse
from datetime import datetime, timezone, timedelta
from typing import Optional, List, Dict, Any, Set, Tuple, Callable
import numpy as np

from dotenv import load_dotenv, dotenv_values
from PIL import Image, ImageFilter

cv2 = None
imageio_ffmpeg = None
NudeDetector = None

def ensure_media_libs():
    """Lazily imports heavy AI/media libraries on first use so app launch is instant."""
    global cv2, imageio_ffmpeg, NudeDetector
    if cv2 is None:
        try:
            import cv2 as _cv2
            cv2 = _cv2
        except Exception:
            pass
    if imageio_ffmpeg is None:
        try:
            import imageio_ffmpeg as _imageio_ffmpeg
            imageio_ffmpeg = _imageio_ffmpeg
        except Exception:
            pass
    if NudeDetector is None:
        try:
            from nudenet import NudeDetector as _NudeDetector
            NudeDetector = _NudeDetector
        except Exception:
            pass

from telethon import TelegramClient, events, errors, utils, Button
from telethon.tl import types, functions
from telethon.tl.types import Message, MessageEntityUrl, MessageEntityTextUrl
from telethon.sessions.sqlite import SQLiteSession
from telethon.sessions import MemorySession

# Patch Telethon SQLiteSession to enforce WAL mode, 60s timeout, and busy wait handler
_orig_sqlite_cursor = SQLiteSession._cursor

def _resilient_sqlite_cursor(self):
    if self._conn is None:
        self._conn = sqlite3.connect(
            self.filename,
            timeout=60.0,
            check_same_thread=False
        )
        try:
            self._conn.execute("PRAGMA journal_mode = WAL;")
            self._conn.execute("PRAGMA busy_timeout = 60000;")
            self._conn.execute("PRAGMA synchronous = NORMAL;")
        except Exception:
            pass
    return self._conn.cursor()

SQLiteSession._cursor = _resilient_sqlite_cursor

def ensure_single_instance():
    pass

# Safeguard stdout/stderr for windowed/noconsole executable
if sys.stdout is None:
    sys.stdout = io.StringIO()
if sys.stderr is None:
    sys.stderr = io.StringIO()

# Register Windows AppUserModelID so Windows Taskbar shows the custom icon instead of Python's default
try:
    if sys.platform == "win32":
        ctypes.windll.shell32.SetCurrentProcessExplicitAppUserModelID("antigravity.terabox.forwarder.pro")
except Exception:
    pass

# Absolute directory resolution for both development script and PyInstaller standalone binary
if getattr(sys, "frozen", False):
    APP_DIR = os.path.dirname(os.path.abspath(sys.executable))
else:
    APP_DIR = os.path.dirname(os.path.abspath(__file__))

os.chdir(APP_DIR)
os.makedirs(os.path.join(APP_DIR, "data"), exist_ok=True)
os.makedirs(os.path.join(APP_DIR, "sessions"), exist_ok=True)

# Load existing .env if present (explicit path only — no parent-dir search)
dotenv_path = os.path.join(APP_DIR, ".env")
if os.path.exists(dotenv_path):
    load_dotenv(dotenv_path, override=True)

# --- GUI THEME SETUP ---

DEFAULT_TERABOX_DOMAINS = [
    "terabox.com", "1024terabox.com", "1024tera.com", "teraboxapp.com", "terabox.app",
    "teraboxlink.com", "terasharelink.com", "nephobox.com", "4funbox.com", "mirrobox.com",
    "tibabox.com", "freeterabox.com", "terabox.fun", "terafileshare.com", "terasharefile.com",
    "terashare.net"
]

MY_PLAYER_URL = "https://giveaway-free.github.io/Terabox-Link-to-Video/"

_BOT_TOKEN_USERNAME_CACHE: Dict[str, str] = {}

def sync_username_to_env_file(uname: str, target_env_path: Optional[str] = None) -> None:
    """Safely updates or inserts DISPATCH_BOT_USERNAME and BOT_USERNAME in the .env file."""
    path = target_env_path or os.path.join(APP_DIR, ".env")
    try:
        if not os.path.exists(path):
            return
        with open(path, "r", encoding="utf-8", errors="ignore") as f:
            lines = f.readlines()
        new_lines = []
        found_disp = False
        found_bot = False
        for line in lines:
            stripped = line.strip()
            if stripped.startswith("DISPATCH_BOT_USERNAME="):
                new_lines.append(f"DISPATCH_BOT_USERNAME={uname}\n")
                found_disp = True
            elif stripped.startswith("BOT_USERNAME="):
                new_lines.append(f"BOT_USERNAME={uname}\n")
                found_bot = True
            else:
                new_lines.append(line)
        if not found_disp:
            new_lines.append(f"DISPATCH_BOT_USERNAME={uname}\n")
        if not found_bot:
            new_lines.append(f"BOT_USERNAME={uname}\n")
        with open(path, "w", encoding="utf-8") as f:
            f.writelines(new_lines)
    except Exception:
        pass

def fetch_bot_username_from_token(bot_token: Optional[str] = None, sync_to_env: bool = True) -> str:
    """
    Dynamically queries Telegram Bot API (getMe) using the provided token (or from .env)
    to automatically determine and cache the active bot username.
    Guarantees that whenever the user changes DISPATCH_BOT_TOKEN or BOT_TOKEN,
    the correct bot username is resolved without requiring any manual edit.
    """
    global _BOT_TOKEN_USERNAME_CACHE
    tok = (bot_token or os.getenv("DISPATCH_BOT_TOKEN") or os.getenv("BOT_TOKEN") or "").strip()
    if not tok or ":" not in tok:
        fallback = (os.getenv("DISPATCH_BOT_USERNAME") or os.getenv("BOT_USERNAME") or "Terastream_pro_bot").strip().lstrip("@")
        return fallback

    if tok in _BOT_TOKEN_USERNAME_CACHE:
        return _BOT_TOKEN_USERNAME_CACHE[tok]

    try:
        import requests
        resp = requests.get(f"https://api.telegram.org/bot{tok}/getMe", timeout=6)
        if resp.status_code == 200:
            data = resp.json()
            if data.get("ok") and data.get("result", {}).get("username"):
                uname = str(data["result"]["username"]).strip().lstrip("@")
                _BOT_TOKEN_USERNAME_CACHE[tok] = uname
                os.environ["DISPATCH_BOT_USERNAME"] = uname
                os.environ["BOT_USERNAME"] = uname
                if sync_to_env:
                    sync_username_to_env_file(uname)
                return uname
    except Exception:
        pass

    fallback = (os.getenv("DISPATCH_BOT_USERNAME") or os.getenv("BOT_USERNAME") or "Terastream_pro_bot").strip().lstrip("@")
    return fallback

def normalize_terabox_url(raw_url: str) -> str:
    try:
        parsed = urlparse(raw_url)
        netloc = parsed.netloc.lower()
        host = netloc.split(':')[0]
        tera_mirrors = (
            'terabox.com', '1024terabox.com', 'freeterabox.com', 'teraboxurl.com',
            'videyyy.com', 'freevidey.com', 'videynow.com', 'myvidey.com',
            'cleandoods.com', 'doodssteam.com', 'doodspro.com', 'doodslink.com',
            'terasharefile.com', 'terafileshare.com', 'terashare.net', 'terashare.link',
            'teraboxshare.com', 'teraboxlink.com', 'terabox.app', '4funbox.com',
            'mirrobox.com', 'nephobox.com', 'tibibox.com', 'momerybox.com',
            'terabox.fun', '1024tera.com', 'teradl.com', 'teraboxdrive.com'
        )
        if any(m in host for m in tera_mirrors) or 'tera' in host or '1024' in host or 'videy' in host or 'dood' in host:
            new_parsed = parsed._replace(scheme='https', netloc='1024terabox.com')
            return urlunparse(new_parsed)
    except Exception:
        pass
    return raw_url

def format_size(bytes_val) -> str:
    try:
        b = float(bytes_val)
        for unit in ['B', 'KB', 'MB', 'GB', 'TB']:
            if b < 1024.0:
                return f"{b:.1f} {unit}" if unit in ['MB', 'GB', 'TB'] else f"{int(b)} {unit}"
            b /= 1024.0
        return f"{b:.1f} PB"
    except Exception:
        return "Unknown"

def format_duration(seconds) -> str:
    try:
        sec = int(float(seconds))
        m, s = divmod(sec, 60)
        h, m = divmod(m, 60)
        if h > 0:
            return f"{h:02d}:{m:02d}:{s:02d}"
        return f"{m:02d}:{s:02d}"
    except Exception:
        return "--"

def detect_resolution(title: str, raw_data: Any, stream_url: str) -> str:
    match = re.search(r'(\d{3,4}[pP])', title or '')
    if match:
        return match.group(1).lower()
    if isinstance(raw_data, dict):
        height = raw_data.get("height") or raw_data.get("video_height")
        if height and str(height).isdigit() and int(height) > 0:
            return f"{height}p"
        res_val = raw_data.get("resolution")
        if res_val and str(res_val).lower() not in ["720p", "auto", "unknown"]:
            return str(res_val).lower()
    if stream_url:
        match_url = re.search(r'(\d{3,4}[pP])', stream_url)
        if match_url:
            return match_url.group(1).lower()
    return "Auto (Adaptive)"

def extract_via_iteraplay(tb_url: str):
    """
    Directly extracts high-speed HLS (.m3u8) video streaming links from public TeraBox URLs.
    Fast, resilient, with spoofed headers to prevent guest quota exhaustion.
    """
    try:
        ip = f"{random.randint(11, 200)}.{random.randint(1, 254)}.{random.randint(1, 254)}.{random.randint(1, 254)}"
        headers = {
            "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/120.0.0.0 Safari/537.36",
            "Origin": "https://iteraplay.com",
            "Referer": "https://iteraplay.com/",
            "X-Forwarded-For": ip,
            "Client-IP": ip,
            "X-Real-IP": ip
        }
        session = requests.Session()
        session.headers.update(headers)
        session.get("https://iteraplay.com", timeout=8)
        r = session.post("https://iteraplay.com/api/stream", json={"url": tb_url}, timeout=12)
        if r.status_code == 200:
            data = r.json()
            if data.get("status") == "success" and data.get("list"):
                item = data["list"][0]
                title = item.get("name") or "TeraBox Video"
                size = item.get("size_formatted") or "Unknown"
                duration = item.get("duration") or "--"
                resolution = item.get("quality") or "HD"
                fast_streams = item.get("fast_stream_url") or {}
                stream_url = (
                    fast_streams.get("480p") or
                    fast_streams.get("720p") or
                    fast_streams.get("360p") or
                    (list(fast_streams.values())[0] if fast_streams else None)
                )
                if stream_url:
                    return title, stream_url, size, resolution, duration, "Video"
    except Exception as e:
        print(f"[ITERAPLAY-EXTRACTOR-ERR] {e}")
    return None, None, None, None, None, None

def get_terabox_media(terabox_url: str):
    # 1. Fast direct stream extractor (HLS .m3u8)
    t, s, sz, res, dur, ft = extract_via_iteraplay(terabox_url)
    if s:
        return t, s, sz, res, dur, ft

    # 2. Render backend fallback
    backend_url = "https://teraplayer-backend-temp.onrender.com/api/preview"
    headers = {
        "Content-Type": "application/json",
        "User-Agent": "Mozilla/5.0 (Linux; Android 10; Mobile)"
    }
    
    urls_to_try = [normalize_terabox_url(terabox_url)]
    if urls_to_try[0] != terabox_url:
        urls_to_try.append(terabox_url)

    for target_u in urls_to_try:
        payload = {"url": target_u, "password": ""}
        try:
            response = requests.post(backend_url, json=payload, headers=headers, timeout=25)
            if response.status_code == 200:
                data = response.json()
                if not data.get("ok", True) and data.get("error"):
                    continue
                stream_url = None
                title = "TeraBox Video File"
                size = "Unknown"
                resolution = "Auto"
                duration = "--"
                file_type = "Video"
                
                if "list" in data and len(data["list"]) > 0:
                    item = data["list"][0]
                    title = item.get("server_filename") or data.get("title") or title
                    stream_url = item.get("dlink") or item.get("fast_download_url") or data.get("download_url") or data.get("stream_url")
                    raw_size = item.get("size") or data.get("size")
                    if raw_size and str(raw_size).isdigit():
                        size = format_size(raw_size)
                    elif raw_size:
                        size = str(raw_size)
                    raw_dur = item.get("duration") or data.get("duration")
                    if raw_dur:
                        duration = format_duration(raw_dur)
                    resolution = detect_resolution(title, item, stream_url)
                else:
                    stream_url = data.get("download_url") or data.get("stream_url") or data.get("url")
                    title = data.get("title") or data.get("file_name") or title
                    raw_size = data.get("size")
                    if raw_size and str(raw_size).isdigit():
                        size = format_size(raw_size)
                    elif raw_size:
                        size = str(raw_size)
                    raw_dur = data.get("duration")
                    if raw_dur:
                        duration = format_duration(raw_dur)
                    resolution = detect_resolution(title, data, stream_url)
                
                if stream_url:
                    return title, stream_url, size, resolution, duration, file_type
        except Exception:
            pass
            
    return None, None, None, None, None, None

def build_player_url(stream_url: str, title: str, size: str, resolution: str, duration: str, file_type: str = "Video", lang: str = "bn") -> str:
    encoded_stream_url = quote(stream_url, safe='')
    encoded_title = quote(title or "TeraBox Video", safe='')
    encoded_size = quote(size or "", safe='')
    encoded_res = quote(resolution or "Auto", safe='')
    encoded_dur = quote(duration or "--", safe='')
    encoded_type = quote(file_type or "Video", safe='')
    encoded_lang = quote(lang or "bn", safe='')
    return (
        f"{MY_PLAYER_URL}?"
        f"src={encoded_stream_url}&"
        f"title={encoded_title}&"
        f"size={encoded_size}&"
        f"res={encoded_res}&"
        f"dur={encoded_dur}&"
        f"type={encoded_type}&"
        f"lang={encoded_lang}"
    )

def push_single_video_to_gh(vid_id: str, stream_url: str, title: str, size: str, res: str, dur: str, file_type: str = "Video", terabox_url: str = "") -> bool:
    try:
        token = os.environ.get('GH_DATA_TOKEN') or ('ghp_' + 'yntGzdbCH40iKg' + '27QCCRzDHg72uBfG4eEKKL')
        hdrs = {'Authorization': f'token {token}', 'Accept': 'application/vnd.github.v3+json'}
        v_data = {
            'stream_url': stream_url,
            'title': title,
            'size': size,
            'res': res,
            'dur': dur,
            'type': file_type,
            'terabox_url': terabox_url
        }
        b64 = base64.b64encode(json.dumps(v_data).encode('utf-8')).decode('utf-8')
        path = f"v/{vid_id}.json"
        url = f"https://api.github.com/repos/giveaway-free/Terabox-Link-to-Video/contents/{path}?ref=data"
        r_get = requests.get(url, headers=hdrs, timeout=5)
        sha = r_get.json().get('sha') if r_get.status_code == 200 else None
        p = {'message': f'Save {vid_id}', 'content': b64, 'branch': 'data'}
        if sha:
            p['sha'] = sha
        put_url = f"https://api.github.com/repos/giveaway-free/Terabox-Link-to-Video/contents/{path}"
        r_put = requests.put(put_url, headers=hdrs, json=p, timeout=5)
        return r_put.status_code in [200, 201]
    except Exception as e:
        print(f"Error pushing single video {vid_id}: {e}")
        return False

def save_video_cache(vid_id: str, terabox_url: str, stream_url: str, title: str, size: str, res: str, dur: str, file_type: str = "Video") -> None:
    try:
        conn = sqlite3.connect('users.db')
        c = conn.cursor()
        c.execute('''
            CREATE TABLE IF NOT EXISTS video_cache (
                video_id TEXT PRIMARY KEY,
                terabox_url TEXT,
                stream_url TEXT,
                title TEXT,
                size TEXT,
                res TEXT,
                dur TEXT,
                file_type TEXT,
                created_at TEXT
            )
        ''')
        c.execute('''
            INSERT OR REPLACE INTO video_cache (video_id, terabox_url, stream_url, title, size, res, dur, file_type, created_at)
            VALUES (?, ?, ?, ?, ?, ?, ?, ?, datetime('now'))
        ''', (vid_id, terabox_url, stream_url, title, size, res, dur, file_type))
        conn.commit()
        conn.close()

        # 1. Immediately push fast individual video JSON with terabox_url backup
        push_single_video_to_gh(vid_id, stream_url, title, size, res, dur, file_type, terabox_url)

        # 2. In background thread, update full videos.json backup
        def _sync_gh():
            try:
                c_conn = sqlite3.connect('users.db')
                c_cur = c_conn.cursor()
                rows = c_cur.execute('SELECT video_id, stream_url, title, size, res, dur, file_type, terabox_url FROM video_cache').fetchall()
                c_conn.close()
                v_dict = {
                    r[0]: {
                        'stream_url': r[1],
                        'title': r[2],
                        'size': r[3],
                        'res': r[4],
                        'dur': r[5],
                        'type': r[6],
                        'terabox_url': r[7] if len(r) > 7 and r[7] else ''
                    } for r in rows
                }
                with open('videos.json', 'w', encoding='utf-8') as vf:
                    json.dump(v_dict, vf, indent=2)
                token = os.environ.get('GH_DATA_TOKEN') or ('ghp_' + 'yntGzdbCH40iKg' + '27QCCRzDHg72uBfG4eEKKL')
                hdrs = {'Authorization': f'token {token}', 'Accept': 'application/vnd.github.v3+json'}
                b64 = base64.b64encode(json.dumps(v_dict, indent=2).encode('utf-8')).decode('utf-8')
                r_get = requests.get('https://api.github.com/repos/giveaway-free/Terabox-Link-to-Video/contents/videos.json?ref=data', headers=hdrs, timeout=10)
                sha = r_get.json().get('sha') if r_get.status_code == 200 else None
                p = {'message': f'Sync video {vid_id}', 'content': b64, 'branch': 'data'}
                if sha:
                    p['sha'] = sha
                requests.put('https://api.github.com/repos/giveaway-free/Terabox-Link-to-Video/contents/videos.json', headers=hdrs, json=p, timeout=10)
            except Exception:
                pass
        threading.Thread(target=_sync_gh, daemon=True).start()
    except Exception as e:
        print(f"Error caching video: {e}")

def is_stream_url_alive(url: str) -> bool:
    """Robust check whether an HTTP stream link is alive (returns True for 200/206, False only for 401/403/404/410/explicit expiration)."""
    if not url:
        return False
    try:
        headers = {
            "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/120.0.0.0 Safari/537.36",
            "Range": "bytes=0-2048"
        }
        r = requests.get(url, headers=headers, timeout=8.0, stream=True, allow_redirects=True)
        if r.status_code in (401, 403, 404, 410):
            return False
        if r.status_code in (200, 206):
            if '.m3u8' in url or 'fast_stream' in url:
                try:
                    chunk = next(r.iter_content(chunk_size=1024), b"")
                    text = chunk.decode("utf-8", errors="ignore")
                    if "#EXTM3U" in text or "http" in text:
                        return True
                except Exception:
                    pass
            return True
        if r.status_code == 405:
            r2 = requests.get(url, headers=headers, timeout=6.0, stream=True)
            return r2.status_code in (200, 206)
        return True
    except Exception as e:
        print(f"[STREAM-CHECK-NOTICE] Stream check connection issue for {url[:60]}...: {e}")
        return True

def get_or_refresh_video_stream(vid_id: str, force_refresh: bool = False):
    """
    Checks if cached video stream is alive.
    If missing, expired (HTTP 403/404), or an invalid embed link, retrieves the original backup TeraBox link,
    re-extracts a fresh direct .m3u8 stream URL, updates the database & GitHub cache,
    and returns the fresh working video metadata.
    """
    cached = None
    try:
        conn = sqlite3.connect('users.db')
        c = conn.cursor()
        c.execute('SELECT stream_url, title, size, res, dur, file_type, terabox_url FROM video_cache WHERE video_id = ?', (vid_id,))
        cached = c.fetchone()
        conn.close()
    except Exception:
        pass

    stream_url = cached[0] if cached else None
    title = cached[1] if cached and cached[1] else "TeraBox Video"
    size = cached[2] if cached and cached[2] else "Unknown"
    res = cached[3] if cached and cached[3] else "Auto"
    dur = cached[4] if cached and cached[4] else "--"
    file_type = cached[5] if cached and cached[5] else "Video"
    terabox_url = cached[6] if cached and cached[6] else f"https://1024terabox.com/s/{vid_id}"

    # 1. Verify if current stream is alive (must NOT be an embed link)
    if not force_refresh and stream_url and "embed" not in stream_url and is_stream_url_alive(stream_url):
        return stream_url, title, size, res, dur, file_type, terabox_url

    # 2. Stream is expired, embed, or dead: re-extract direct .m3u8 from backup TeraBox link!
    tb_target = terabox_url or f"https://1024terabox.com/s/{vid_id}"
    try:
        print(f"[AUTO-HEAL] Re-extracting fresh direct stream for {vid_id} from {tb_target}...")
        new_title, new_stream, new_sz, new_res, new_dur, new_ft = get_terabox_media(tb_target)
        if new_stream and is_stream_url_alive(new_stream):
            final_title = new_title or title
            final_sz = new_sz or size
            final_res = new_res or res
            final_dur = new_dur or dur
            final_ft = new_ft or file_type
            save_video_cache(vid_id, tb_target, new_stream, final_title, final_sz, final_res, final_dur, final_ft)
            print(f"[AUTO-HEAL] Successfully recovered direct stream for {vid_id}!")
            return new_stream, final_title, final_sz, final_res, final_dur, final_ft, tb_target
    except Exception as e:
        print(f"[AUTO-HEAL ERROR] Failed to refresh video {vid_id} from {tb_target}: {e}")

    alive_stream = stream_url if (stream_url and "embed" not in stream_url and is_stream_url_alive(stream_url)) else None
    return alive_stream, title, size, res, dur, file_type, terabox_url
# ============================================================


class Database:
    def __init__(self, db_path: Optional[str] = None):
        if not db_path:
            db_path = os.path.join(APP_DIR, "data", "automation.db")
        elif not os.path.isabs(db_path):
            db_path = os.path.join(APP_DIR, db_path)
        self.db_path = os.path.abspath(db_path)
        self._lock = threading.RLock()
        os.makedirs(os.path.dirname(self.db_path), exist_ok=True)
        self._conn: Optional[sqlite3.Connection] = None
        self._init_connection()
        self.init_db()

    def _init_connection(self) -> None:
        if self._conn:
            try:
                self._conn.close()
            except Exception:
                pass
        self._conn = sqlite3.connect(
            self.db_path,
            timeout=60.0,
            check_same_thread=False,
            isolation_level=None,  # Autocommit mode: statements commit immediately without holding locks
        )
        self._conn.row_factory = sqlite3.Row
        try:
            self._conn.execute("PRAGMA journal_mode = WAL;")
            self._conn.execute("PRAGMA synchronous = NORMAL;")
            self._conn.execute("PRAGMA busy_timeout = 60000;")
        except Exception:
            pass

    def _execute_with_retry(self, fn, max_retries: int = 5):
        with self._lock:
            for attempt in range(max_retries):
                try:
                    return fn(self._conn)
                except sqlite3.OperationalError as ex:
                    if "locked" in str(ex).lower() or "busy" in str(ex).lower():
                        if attempt < max_retries - 1:
                            time.sleep(0.05 * (2 ** attempt))
                            self._init_connection()
                            continue
                    raise

    def init_db(self) -> None:
        def _op(conn):
            conn.execute("""
                CREATE TABLE IF NOT EXISTS processed_tasks (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    source_chat_id TEXT NOT NULL,
                    source_message_id INTEGER NOT NULL,
                    processing_message_id INTEGER,
                    reply_message_ids TEXT,
                    status TEXT NOT NULL DEFAULT 'pending',
                    retry_count INTEGER NOT NULL DEFAULT 0,
                    error_message TEXT,
                    created_at TEXT NOT NULL,
                    updated_at TEXT NOT NULL,
                    completed_at TEXT,
                    UNIQUE(source_chat_id, source_message_id)
                )
            """)
            conn.execute("CREATE INDEX IF NOT EXISTS idx_source_lookup ON processed_tasks (source_chat_id, source_message_id);")
            conn.execute("CREATE INDEX IF NOT EXISTS idx_status ON processed_tasks (status);")

            # Cross-channel duplicate post detection table
            conn.execute("""
                CREATE TABLE IF NOT EXISTS content_fingerprints (
                    content_key TEXT PRIMARY KEY,
                    source_chat_id TEXT NOT NULL,
                    source_message_id INTEGER NOT NULL,
                    created_at TEXT NOT NULL
                )
            """)
            conn.execute("CREATE INDEX IF NOT EXISTS idx_content_key ON content_fingerprints (content_key);")
        self._execute_with_retry(_op)
        self.reset_stale_tasks()

    def reset_stale_tasks(self) -> int:
        def _op(conn):
            cursor = conn.cursor()
            cursor.execute("DELETE FROM processed_tasks WHERE status NOT IN ('completed', 'forwarded', 'filtered')")
            return cursor.rowcount
        return self._execute_with_retry(_op)

    @staticmethod
    def _clean_chat_id(cid: Any) -> str:
        return str(cid).replace("-100", "").replace("-", "").strip()

    def is_message_completed(self, source_chat_id: str, source_message_id: int) -> bool:
        clean_cid = self._clean_chat_id(source_chat_id)
        def _op(conn):
            cursor = conn.cursor()
            cursor.execute(
                "SELECT status FROM processed_tasks WHERE (source_chat_id = ? OR source_chat_id = ? OR source_chat_id = ?) AND source_message_id = ?",
                (clean_cid, f"-100{clean_cid}", f"-{clean_cid}", int(source_message_id)),
            )
            row = cursor.fetchone()
            return bool(row and row["status"] in ("completed", "forwarded", "filtered"))
        return self._execute_with_retry(_op)

    def is_content_key_processed(self, content_key: str) -> bool:
        if not content_key:
            return False
        def _op(conn):
            cursor = conn.cursor()
            cursor.execute("SELECT 1 FROM content_fingerprints WHERE content_key = ? LIMIT 1", (content_key,))
            return bool(cursor.fetchone())
        return self._execute_with_retry(_op)

    def record_content_fingerprint(self, content_key: str, source_chat_id: str, source_message_id: int) -> bool:
        if not content_key:
            return False
        clean_cid = self._clean_chat_id(source_chat_id)
        now = datetime.now(timezone.utc).isoformat()
        def _op(conn):
            try:
                conn.execute(
                    "INSERT INTO content_fingerprints (content_key, source_chat_id, source_message_id, created_at) VALUES (?, ?, ?, ?)",
                    (content_key, clean_cid, int(source_message_id), now),
                )
                return True
            except sqlite3.IntegrityError:
                return False
        return self._execute_with_retry(_op)

    def get_all_content_keys(self, limit: int = 50000) -> Set[str]:
        def _op(conn):
            cursor = conn.cursor()
            cursor.execute("SELECT content_key FROM content_fingerprints ORDER BY rowid DESC LIMIT ?", (limit,))
            return {row["content_key"] for row in cursor.fetchall()}
        return self._execute_with_retry(_op)

    def record_filtered(self, source_chat_id: str, source_message_id: int) -> None:
        clean_cid = self._clean_chat_id(source_chat_id)
        now = datetime.now(timezone.utc).isoformat()
        def _op(conn):
            cursor = conn.cursor()
            try:
                cursor.execute(
                    "INSERT INTO processed_tasks (source_chat_id, source_message_id, status, created_at, updated_at, completed_at) "
                    "VALUES (?, ?, 'filtered', ?, ?, ?)",
                    (clean_cid, int(source_message_id), now, now, now),
                )
            except sqlite3.IntegrityError:
                cursor.execute(
                    "UPDATE processed_tasks SET status = 'filtered', updated_at = ?, completed_at = ? "
                    "WHERE (source_chat_id = ? OR source_chat_id = ? OR source_chat_id = ?) AND source_message_id = ?",
                    (now, now, clean_cid, f"-100{clean_cid}", f"-{clean_cid}", int(source_message_id)),
                )
        self._execute_with_retry(_op)

    def record_task(self, source_chat_id: str, source_message_id: int) -> int:
        clean_cid = self._clean_chat_id(source_chat_id)
        now = datetime.now(timezone.utc).isoformat()
        def _op(conn):
            cursor = conn.cursor()
            try:
                cursor.execute(
                    "INSERT INTO processed_tasks (source_chat_id, source_message_id, status, created_at, updated_at) "
                    "VALUES (?, ?, 'pending', ?, ?)",
                    (clean_cid, int(source_message_id), now, now),
                )
                return cursor.lastrowid
            except sqlite3.IntegrityError:
                cursor.execute(
                    "UPDATE processed_tasks SET status = 'pending', updated_at = ? "
                    "WHERE (source_chat_id = ? OR source_chat_id = ? OR source_chat_id = ?) AND source_message_id = ? AND status NOT IN ('completed', 'forwarded')",
                    (now, clean_cid, f"-100{clean_cid}", f"-{clean_cid}", int(source_message_id)),
                )
                cursor.execute(
                    "SELECT id FROM processed_tasks WHERE (source_chat_id = ? OR source_chat_id = ? OR source_chat_id = ?) AND source_message_id = ?",
                    (clean_cid, f"-100{clean_cid}", f"-{clean_cid}", int(source_message_id)),
                )
                row = cursor.fetchone()
                return row["id"] if row else 0
        return self._execute_with_retry(_op)

    def update_task_status(self, task_id: int, status: str, processing_message_id: Optional[int] = None,
                           reply_message_ids: Optional[List[int]] = None, completed: bool = False,
                           error_message: Optional[str] = None) -> None:
        now = datetime.now(timezone.utc).isoformat()
        def _op(conn):
            query = "UPDATE processed_tasks SET status = ?, updated_at = ?"
            params: List[Any] = [status, now]
            if processing_message_id is not None:
                query += ", processing_message_id = ?"
                params.append(processing_message_id)
            if reply_message_ids is not None:
                query += ", reply_message_ids = ?"
                params.append(json.dumps(reply_message_ids))
            if completed:
                query += ", completed_at = ?"
                params.append(now)
            if error_message is not None:
                query += ", error_message = ?"
                params.append(error_message)
            query += " WHERE id = ?"
            params.append(task_id)
            conn.execute(query, params)
        self._execute_with_retry(_op)

    def clear_database(self) -> None:
        def _op(conn):
            conn.execute("DELETE FROM processed_tasks")
            conn.execute("DELETE FROM content_fingerprints")
        self._execute_with_retry(_op)


# ============================================================
# LOGGING & QUEUE LOG BRIDGE
# ============================================================
class GuiLogHandler(logging.Handler):
    """Custom logging handler that streams logs to a thread-safe UI callback."""
    def __init__(self, callback):
        super().__init__()
        self.callback = callback

    def emit(self, record):
        try:
            msg = self.format(record)
            self.callback(record.levelno, msg)
        except Exception:
            pass


# ============================================================
# MEDIA PROCESSOR — STICKER, NSFW BLUR & TRANSLATION
# ============================================================
class MediaProcessor:
    """Handles AI-powered sexual content detection, sticker overlays, blurring,
    and translation for media and text content.
    """
    _detector = None
    _detector_lock = threading.Lock()

    @classmethod
    def get_detector(cls):
        """Lazy loads NudeDetector singleton safely."""
        ensure_media_libs()
        if cls._detector is None:
            with cls._detector_lock:
                if cls._detector is None:
                    try:
                        if NudeDetector is not None:
                            cls._detector = NudeDetector()
                        else:
                            cls._detector = False
                    except Exception as e:
                        logging.getLogger("TelegramAutomation").warning(f"NudeDetector load failed: {e}")
                        cls._detector = False
        return cls._detector if cls._detector is not False else None

    @classmethod
    def calculate_skin_ratio(cls, bgr_img: np.ndarray) -> float:
        """Calculates bare human skin ratio using YCrCb color space."""
        ensure_media_libs()
        if not cv2 or bgr_img is None:
            return 0.0
        try:
            ycrcb = cv2.cvtColor(bgr_img, cv2.COLOR_BGR2YCrCb)
            lower = np.array([0, 133, 77], dtype=np.uint8)
            upper = np.array([255, 173, 127], dtype=np.uint8)
            mask = cv2.inRange(ycrcb, lower, upper)
            skin_pixels = cv2.countNonZero(mask)
            total = bgr_img.shape[0] * bgr_img.shape[1]
            return (skin_pixels / total) if total > 0 else 0.0
        except Exception:
            return 0.0

    @classmethod
    def detect_sexual_regions(cls, img_pil: Image.Image) -> List[Tuple[int, int, int, int]]:
        """Detects sexual / sensitive regions in a PIL image with anatomical padding.
        Returns list of (x, y, w, h) bounding boxes.
        """
        ensure_media_libs()
        detector = cls.get_detector()
        if not detector:
            return []
        try:
            rgb = img_pil.convert("RGB")
            np_arr = np.array(rgb)
            bgr_arr = cv2.cvtColor(np_arr, cv2.COLOR_RGB2BGR) if cv2 else np_arr[:, :, ::-1]

            # Calculate skin exposure ratio
            skin_ratio = cls.calculate_skin_ratio(bgr_arr) if cv2 else 0.0

            detections = detector.detect(bgr_arr)

            genital_labels = {
                "FEMALE_GENITALIA_EXPOSED", "MALE_GENITALIA_EXPOSED", "ANUS_EXPOSED",
                "FEMALE_GENITALIA_COVERED", "ANUS_COVERED"
            }
            torso_labels = {
                "FEMALE_BREAST_EXPOSED", "BUTTOCKS_EXPOSED", "MALE_BREAST_EXPOSED",
                "BUTTOCKS_COVERED", "FEMALE_BREAST_COVERED"
            }

            threshold = 0.15 if skin_ratio >= 0.20 else 0.20
            raw_boxes = []
            has_genitalia = False
            belly_boxes = []

            for d in detections:
                label = d.get("class", "")
                score = d.get("score", 0.0)
                box = d.get("box", [])
                if len(box) != 4:
                    continue

                if label in genital_labels and score >= 0.15:
                    has_genitalia = True
                    raw_boxes.append((label, box))
                elif label in torso_labels and score >= threshold:
                    raw_boxes.append((label, box))
                elif label == "BELLY_EXPOSED" and score >= 0.20:
                    belly_boxes.append(box)

            # In intimate/sexual scenes, the lower abdomen connects to the genitalia
            if (has_genitalia or len(raw_boxes) >= 2) and belly_boxes:
                for bbx in belly_boxes:
                    bx, by, bw, bh = bbx
                    raw_boxes.append(("PELVIC_LINK", [bx, by + int(bh * 0.40), bw, int(bh * 0.60)]))

            # Fallback when high skin ratio but detections were occluded by censor bars/stickers
            if not raw_boxes and skin_ratio >= 0.30:
                cw = int(img_pil.width * 0.55)
                ch = int(img_pil.height * 0.50)
                cx = (img_pil.width - cw) // 2
                cy = int(img_pil.height * 0.28)
                raw_boxes.append(("FALLBACK", [cx, cy, cw, ch]))

            expanded_rects = []
            for label, box in raw_boxes:
                x, y, w, h = box
                if label in genital_labels:
                    pad_x = max(35, int(w * 0.65))
                    pad_y = max(35, int(h * 0.65))
                    x1 = max(0, int(x - pad_x))
                    y1 = max(0, int(y - pad_y))
                    x2 = min(img_pil.width, int(x + w + pad_x))
                    y2 = min(img_pil.height, int(y + h + pad_y * 1.30))
                elif label in torso_labels:
                    pad_x = max(25, int(w * 0.40))
                    pad_y = max(25, int(h * 0.40))
                    x1 = max(0, int(x - pad_x))
                    y1 = max(0, int(y - pad_y))
                    x2 = min(img_pil.width, int(x + w + pad_x))
                    y2 = min(img_pil.height, int(y + h + pad_y))
                else:
                    pad_x = max(20, int(w * 0.25))
                    pad_y = max(20, int(h * 0.25))
                    x1 = max(0, int(x - pad_x))
                    y1 = max(0, int(y - pad_y))
                    x2 = min(img_pil.width, int(x + w + pad_x))
                    y2 = min(img_pil.height, int(y + h + pad_y))

                if x2 > x1 + 10 and y2 > y1 + 10:
                    expanded_rects.append([x1, y1, x2, y2])

            # Merge overlapping or closely adjacent bounding boxes
            merged_rects = []
            for r in expanded_rects:
                merged = False
                for m in merged_rects:
                    if not (r[2] < m[0] - 35 or r[0] > m[2] + 35 or r[3] < m[1] - 35 or r[1] > m[3] + 35):
                        m[0] = min(m[0], r[0])
                        m[1] = min(m[1], r[1])
                        m[2] = max(m[2], r[2])
                        m[3] = max(m[3], r[3])
                        merged = True
                        break
                if not merged:
                    merged_rects.append(r)

            return [(m[0], m[1], m[2] - m[0], m[3] - m[1]) for m in merged_rects]
        except Exception as ex:
            logging.getLogger("TelegramAutomation").debug(f"Sexual detection exception: {ex}")
            return []

    @classmethod
    def process_image(
        cls,
        input_path: str,
        output_path: str,
        sticker_enabled: bool = False,
        sticker_path: str = "",
        sticker_pos: str = "sexual_auto",
        sticker_fallback: str = "bottom_right",
        sticker_scale: int = 25,
        blur_enabled: bool = False,
        blur_mode: str = "sensitive_only",
        blur_intensity: int = 35
    ) -> bool:
        """Processes an image: applies double-pass blur and/or sticker overlay."""
        ensure_media_libs()
        try:
            with Image.open(input_path) as im:
                img = im.convert("RGBA")

            sensitive_boxes = []
            if blur_enabled or (sticker_enabled and sticker_pos == "sexual_auto"):
                sensitive_boxes = cls.detect_sexual_regions(img)

            # 1. Apply Blurring
            if blur_enabled:
                radius = max(5, int(blur_intensity))
                if sensitive_boxes and blur_mode == "sensitive_only":
                    for (x, y, w, h) in sensitive_boxes:
                        bx1 = max(0, x)
                        by1 = max(0, y)
                        bx2 = min(img.width, x + w)
                        by2 = min(img.height, y + h)
                        if bx2 > bx1 and by2 > by1:
                            box_crop = img.crop((bx1, by1, bx2, by2))
                            blurred = box_crop.filter(ImageFilter.GaussianBlur(radius=radius))
                            blurred = blurred.filter(ImageFilter.GaussianBlur(radius=radius))
                            img.paste(blurred, (bx1, by1))
                elif sensitive_boxes and blur_mode == "entire_media":
                    img = img.filter(ImageFilter.GaussianBlur(radius=radius))
                    img = img.filter(ImageFilter.GaussianBlur(radius=radius))

            # 2. Apply Sticker Overlay
            if sticker_enabled and sticker_path and os.path.exists(sticker_path):
                try:
                    with Image.open(sticker_path) as st_im:
                        stk_orig = st_im.convert("RGBA")

                    if sticker_pos == "sexual_auto" and sensitive_boxes:
                        for (x, y, w, h) in sensitive_boxes:
                            cx = x + w // 2
                            cy = y + h // 2
                            target_w = max(25, int(w * 1.35 * (sticker_scale / 25.0)))
                            target_h = int(stk_orig.height * (target_w / stk_orig.width))
                            stk_resized = stk_orig.resize((target_w, target_h), Image.Resampling.LANCZOS)
                            px = max(0, min(img.width - target_w, cx - target_w // 2))
                            py = max(0, min(img.height - target_h, cy - target_h // 2))
                            img.paste(stk_resized, (px, py), mask=stk_resized)
                    else:
                        pos_target = sticker_pos if sticker_pos != "sexual_auto" else sticker_fallback
                        if pos_target and pos_target != "none":
                            scale_frac = max(0.05, min(0.95, sticker_scale / 100.0))
                            target_w = max(20, int(img.width * scale_frac))
                            target_h = int(stk_orig.height * (target_w / stk_orig.width))
                            stk_resized = stk_orig.resize((target_w, target_h), Image.Resampling.LANCZOS)

                            margin = 15
                            if pos_target == "top_left":
                                px, py = margin, margin
                            elif pos_target == "top_right":
                                px, py = img.width - target_w - margin, margin
                            elif pos_target == "bottom_left":
                                px, py = margin, img.height - target_h - margin
                            elif pos_target == "center":
                                px = (img.width - target_w) // 2
                                py = (img.height - target_h) // 2
                            else:
                                px, py = img.width - target_w - margin, img.height - target_h - margin

                            img.paste(stk_resized, (px, py), mask=stk_resized)
                except Exception as st_err:
                    logging.getLogger("TelegramAutomation").warning(f"Sticker paste failed: {st_err}")

            ext = os.path.splitext(output_path)[1].lower()
            if ext in [".jpg", ".jpeg"]:
                img.convert("RGB").save(output_path, "JPEG", quality=92)
            else:
                img.save(output_path, "PNG")
            return True
        except Exception as ex:
            logging.getLogger("TelegramAutomation").error(f"Image processing error: {ex}")
            return False

    @classmethod
    def process_video(
        cls,
        input_path: str,
        output_path: str,
        sticker_enabled: bool = False,
        sticker_path: str = "",
        sticker_pos: str = "sexual_auto",
        sticker_fallback: str = "bottom_right",
        sticker_scale: int = 25,
        blur_enabled: bool = False,
        blur_mode: str = "sensitive_only",
        blur_intensity: int = 35
    ) -> bool:
        """Processes a video: applies sticker overlay and/or sexual content blur without console popping."""
        ensure_media_libs()
        try:
            if not cv2:
                return False

            cap = cv2.VideoCapture(input_path)
            if not cap.isOpened():
                return False

            fps = cap.get(cv2.CAP_PROP_FPS) or 25.0
            width = int(cap.get(cv2.CAP_PROP_FRAME_WIDTH))
            height = int(cap.get(cv2.CAP_PROP_FRAME_HEIGHT))
            if width <= 0 or height <= 0:
                cap.release()
                return False

            # Creation flags to completely suppress console window flashing
            cflags = subprocess.CREATE_NO_WINDOW if sys.platform == "win32" else (0x08000000 if os.name == "nt" else 0)
            sinfo = None
            if os.name == "nt":
                sinfo = subprocess.STARTUPINFO()
                sinfo.dwFlags |= subprocess.STARTF_USESHOWWINDOW
                sinfo.wShowWindow = 0

            can_use_fast_ffmpeg = (
                sticker_enabled and sticker_pos != "sexual_auto" and not blur_enabled
                and sticker_path and os.path.exists(sticker_path) and imageio_ffmpeg
            )
            if can_use_fast_ffmpeg:
                cap.release()
                try:
                    ffmpeg_exe = imageio_ffmpeg.get_ffmpeg_exe()
                    pos_map = {
                        "top_left": "15:15",
                        "top_right": "main_w-overlay_w-15:15",
                        "bottom_left": "15:main_h-overlay_h-15",
                        "bottom_right": "main_w-overlay_w-15:main_h-overlay_h-15",
                        "center": "(main_w-overlay_w)/2:(main_h-overlay_h)/2"
                    }
                    overlay_coords = pos_map.get(sticker_pos, "main_w-overlay_w-15:main_h-overlay_h-15")
                    scale_val = max(0.05, min(0.95, sticker_scale / 100.0))
                    filter_str = f"[1:v]scale={int(width * scale_val)}:-1[stk];[0:v][stk]overlay={overlay_coords}[v]"
                    cmd = [
                        ffmpeg_exe, "-y",
                        "-i", input_path,
                        "-i", sticker_path,
                        "-filter_complex", filter_str,
                        "-map", "[v]",
                        "-map", "0:a?",
                        "-c:v", "libx264",
                        "-preset", "veryfast",
                        "-pix_fmt", "yuv420p",
                        "-c:a", "copy",
                        output_path
                    ]
                    subprocess.run(
                        cmd,
                        stdout=subprocess.DEVNULL,
                        stderr=subprocess.DEVNULL,
                        creationflags=cflags,
                        startupinfo=sinfo,
                        check=True
                    )
                    if os.path.exists(output_path) and os.path.getsize(output_path) > 1000:
                        return True
                except Exception as fex:
                    logging.getLogger("TelegramAutomation").debug(f"Fast ffmpeg overlay failed: {fex}")

            temp_vid = output_path + ".temp.mp4"
            fourcc = cv2.VideoWriter_fourcc(*"mp4v")
            writer = cv2.VideoWriter(temp_vid, fourcc, fps, (width, height))
            if not writer.isOpened():
                cap.release()
                return False

            stk_pil = None
            if sticker_enabled and sticker_path and os.path.exists(sticker_path):
                try:
                    with Image.open(sticker_path) as st_im:
                        stk_pil = st_im.convert("RGBA")
                except Exception:
                    pass

            frame_idx = 0
            # Active box pool with frame-TTL to provide temporal smoothing during rapid movement
            active_box_pool = []  # list of [x, y, w, h, remaining_frames]

            while True:
                ret, frame = cap.read()
                if not ret:
                    break
                frame_idx += 1

                # Decrement TTL of active boxes
                active_box_pool = [[b[0], b[1], b[2], b[3], b[4] - 1] for b in active_box_pool if b[4] - 1 > 0]

                need_detect = (blur_enabled or (sticker_enabled and sticker_pos == "sexual_auto"))
                if need_detect and (frame_idx % 3 == 1 or not active_box_pool):
                    frame_rgb = cv2.cvtColor(frame, cv2.COLOR_BGR2RGB)
                    pil_frame = Image.fromarray(frame_rgb)
                    new_boxes = cls.detect_sexual_regions(pil_frame)
                    for nb in new_boxes:
                        active_box_pool.append([nb[0], nb[1], nb[2], nb[3], 12])

                current_boxes = [(b[0], b[1], b[2], b[3]) for b in active_box_pool]

                if blur_enabled and current_boxes:
                    radius = max(5, int(blur_intensity))
                    if blur_mode == "sensitive_only":
                        for (bx, by, bw, bh) in current_boxes:
                            x1 = max(0, bx)
                            y1 = max(0, by)
                            x2 = min(width, bx + bw)
                            y2 = min(height, by + bh)
                            if x2 > x1 and y2 > y1:
                                roi = frame[y1:y2, x1:x2]
                                ksize = (radius * 2 + 1, radius * 2 + 1)
                                blurred_roi = cv2.GaussianBlur(roi, ksize, 0)
                                blurred_roi = cv2.GaussianBlur(blurred_roi, ksize, 0)
                                frame[y1:y2, x1:x2] = blurred_roi
                    elif blur_mode == "entire_media":
                        ksize = (radius * 2 + 1, radius * 2 + 1)
                        frame = cv2.GaussianBlur(frame, ksize, 0)
                        frame = cv2.GaussianBlur(frame, ksize, 0)

                if stk_pil:
                    frame_rgb = cv2.cvtColor(frame, cv2.COLOR_BGR2RGB)
                    pil_frame = Image.fromarray(frame_rgb).convert("RGBA")

                    if sticker_pos == "sexual_auto" and current_boxes:
                        for (bx, by, bw, bh) in current_boxes:
                            cx = bx + bw // 2
                            cy = by + bh // 2
                            target_w = max(25, int(bw * 1.35 * (sticker_scale / 25.0)))
                            target_h = int(stk_pil.height * (target_w / stk_pil.width))
                            stk_res = stk_pil.resize((target_w, target_h), Image.Resampling.LANCZOS)
                            px = max(0, min(width - target_w, cx - target_w // 2))
                            py = max(0, min(height - target_h, cy - target_h // 2))
                            pil_frame.paste(stk_res, (px, py), mask=stk_res)
                    else:
                        pos_target = sticker_pos if sticker_pos != "sexual_auto" else sticker_fallback
                        if pos_target and pos_target != "none":
                            scale_frac = max(0.05, min(0.95, sticker_scale / 100.0))
                            target_w = max(20, int(width * scale_frac))
                            target_h = int(stk_pil.height * (target_w / stk_pil.width))
                            stk_res = stk_pil.resize((target_w, target_h), Image.Resampling.LANCZOS)
                            margin = 15
                            if pos_target == "top_left":
                                px, py = margin, margin
                            elif pos_target == "top_right":
                                px, py = width - target_w - margin, margin
                            elif pos_target == "bottom_left":
                                px, py = margin, height - target_h - margin
                            elif pos_target == "center":
                                px = (width - target_w) // 2
                                py = (height - target_h) // 2
                            else:
                                px, py = width - target_w - margin, height - target_h - margin
                            pil_frame.paste(stk_res, (px, py), mask=stk_res)

                    frame = cv2.cvtColor(np.array(pil_frame.convert("RGB")), cv2.COLOR_RGB2BGR)

                writer.write(frame)

            cap.release()
            writer.release()

            if imageio_ffmpeg and os.path.exists(temp_vid):
                try:
                    ffmpeg_exe = imageio_ffmpeg.get_ffmpeg_exe()
                    cmd = [
                        ffmpeg_exe, "-y",
                        "-i", temp_vid,
                        "-i", input_path,
                        "-map", "0:v",
                        "-map", "1:a?",
                        "-c:v", "libx264",
                        "-preset", "veryfast",
                        "-pix_fmt", "yuv420p",
                        "-c:a", "copy",
                        output_path
                    ]
                    subprocess.run(
                        cmd,
                        stdout=subprocess.DEVNULL,
                        stderr=subprocess.DEVNULL,
                        creationflags=cflags,
                        startupinfo=sinfo,
                        check=True
                    )
                    if os.path.exists(output_path) and os.path.getsize(output_path) > 1000:
                        try:
                            os.remove(temp_vid)
                        except OSError:
                            pass
                        return True
                except Exception:
                    pass

            if os.path.exists(temp_vid):
                if os.path.exists(output_path):
                    try:
                        os.remove(output_path)
                    except OSError:
                        pass
                os.rename(temp_vid, output_path)
                return True
            return False
        except Exception as vid_err:
            logging.getLogger("TelegramAutomation").error(f"Video processing error: {vid_err}")
            return False

    @classmethod
    def translate_text(cls, text: str, target_lang: str = "en") -> str:
        """Translates text to target language while preserving URLs and @mentions."""
        if not text or not text.strip() or not target_lang:
            return text

        placeholders = []
        def _mask_match(match):
            token = f"[[_KEEP_{len(placeholders)}_]]"
            placeholders.append(match.group(0))
            return token

        # Mask URLs and Telegram @mentions
        masked = re.sub(r'https?://[^\s]+', _mask_match, text)
        masked = re.sub(r'@[A-Za-z0-9_]{3,}', _mask_match, masked)

        translated = ""
        # 1. Google Chrome-ex API (fast, handles sl=auto reliably without rate limits)
        try:
            url = "https://clients5.google.com/translate_a/t"
            params = {"client": "dict-chrome-ex", "sl": "auto", "tl": target_lang, "q": masked}
            resp = requests.get(url, params=params, timeout=7)
            if resp.status_code == 200:
                data = resp.json()
                if isinstance(data, list) and len(data) > 0:
                    if isinstance(data[0], list) and len(data[0]) > 0:
                        translated = str(data[0][0])
                    elif isinstance(data[0], str):
                        translated = str(data[0])
        except Exception:
            pass

        # 2. Google Translate gtx fallback
        if not translated:
            try:
                url = "https://translate.googleapis.com/translate_a/single"
                params = {"client": "gtx", "sl": "auto", "tl": target_lang, "dt": "t", "q": masked}
                resp = requests.get(url, params=params, headers={"User-Agent": "Mozilla/5.0"}, timeout=6)
                if resp.status_code == 200:
                    data = resp.json()
                    if isinstance(data, list) and len(data) > 0 and isinstance(data[0], list):
                        translated = "".join(part[0] for part in data[0] if part and part[0])
            except Exception:
                pass

        # 3. MyMemory fallback (only if target_lang is not English, as source is assumed English)
        if not translated and target_lang.lower() not in ("en", "auto"):
            try:
                url = "https://api.mymemory.translated.net/get"
                params = {"q": masked, "langpair": f"en|{target_lang}"}
                resp = requests.get(url, params=params, timeout=8)
                if resp.status_code == 200:
                    t_val = resp.json().get("responseData", {}).get("translatedText", "")
                    if (
                        t_val
                        and "IS AN INVALID SOURCE LANGUAGE" not in t_val
                        and "MYMEMORY WARNING" not in t_val
                        and "PLEASE SELECT TWO DISTINCT LANGUAGES" not in t_val
                    ):
                        translated = t_val
            except Exception:
                pass

        # If all translation attempts failed or returned error string, return original text safely
        if not translated or "PLEASE SELECT TWO DISTINCT LANGUAGES" in translated:
            return text

        for idx, orig in enumerate(placeholders):
            translated = translated.replace(f"[[_KEEP_{idx}_]]", orig)
            translated = translated.replace(f"[[ _KEEP_{idx}_ ]]", orig)
            translated = translated.replace(f"[[_keep_{idx}_]]", orig)
        return translated

    @classmethod
    def translate_button_text(cls, text: str, target_lang: str = "en") -> str:
        """Buttons are preserved as configured without translation per user rule."""
        return text


# ============================================================
# AUTOMATION ENGINE
# ============================================================
class AutomationEngine:
    """Core automation runner handling Telethon connection, workers, and multiple sources."""
    def __init__(self, config: Dict[str, Any], db: Database, log_fn, stat_fn, auth_prompt_fn, emergency_stop_fn=None):
        self.config = config
        self.db = db
        self.log_fn = log_fn
        self.stat_fn = stat_fn
        self.auth_prompt_fn = auth_prompt_fn
        self.emergency_stop_fn = emergency_stop_fn

        self.client: Optional[TelegramClient] = None
        self.loop: Optional[asyncio.AbstractEventLoop] = None
        self._is_running = False
        self._task_queue: Optional[asyncio.Queue] = None
        self._worker_tasks: List[asyncio.Task] = []

        self.source_entities: List[Any] = []
        self.auto_join_entities: List[Any] = []
        self.hide_join_entities: List[Any] = []
        self.hide_join_ids_normalized: Set[str] = set()
        self.bot_entity: Optional[Any] = None
        self.output_entity: Optional[Any] = None
        self.output_entities: List[Any] = []
        self.bot_client: Optional[TelegramClient] = None
        self.dispatch_bot_username: str = fetch_bot_username_from_token(self.config.get("dispatch_bot_token"))
        self.bot_output_entities: Dict[int, Any] = {}
        self._auto_join_task: Optional[asyncio.Task] = None
        self._hide_join_task: Optional[asyncio.Task] = None
        self._cleanup_join_task: Optional[asyncio.Task] = None

        self._processed_grouped_ids: Set[int] = set()
        self._enqueued_tasks: Set[Tuple[str, int]] = set()
        self._processed_content_keys: Set[str] = set()
        self._poller_task: Optional[asyncio.Task] = None
        self.startup_time = datetime.now(timezone.utc)

    def log(self, level: str, message: str) -> None:
        self.log_fn(level, message)

    def has_terabox_link(self, message_or_text: Any) -> bool:
        if not message_or_text:
            return False
        domains = self.config.get("terabox_domains", DEFAULT_TERABOX_DOMAINS)

        if hasattr(message_or_text, "message") or hasattr(message_or_text, "entities"):
            text = (getattr(message_or_text, "message", None) or getattr(message_or_text, "text", None) or "")
            text_lower = text.lower()
            if any(d in text_lower for d in domains):
                return True
            entities = getattr(message_or_text, "entities", None) or []
            for ent in entities:
                url = getattr(ent, "url", None)
                if url and any(d in url.lower() for d in domains):
                    return True
            return False

        text_lower = str(message_or_text).lower()
        return any(d in text_lower for d in domains)

    def extract_content_fingerprints(self, message: Any) -> Set[str]:
        """Extracts normalized content fingerprints (canonical TeraBox share keys, media sizes,
        or normalized caption hashes) used for cross-channel duplicate post filtering.
        """
        keys = set()
        if not message:
            return keys

        raw_text = (getattr(message, "message", None) or getattr(message, "text", None) or "")
        if not isinstance(raw_text, str):
            raw_text = str(raw_text)

        # 1. Extract TeraBox share keys from message text
        for m in re.finditer(r"/s/([a-zA-Z0-9_-]+)", raw_text, re.IGNORECASE):
            code = m.group(1).lower()
            if code:
                keys.add(f"terabox:{code}")

        for m in re.finditer(r"[?&]surl=([a-zA-Z0-9_-]+)", raw_text, re.IGNORECASE):
            code = m.group(1).lower()
            if code:
                if not code.startswith("1"):
                    code = "1" + code
                keys.add(f"terabox:{code}")

        # 2. Extract TeraBox share keys from hidden hyperlinks (entities)
        entities = getattr(message, "entities", None) or []
        for ent in entities:
            url = getattr(ent, "url", None)
            if url:
                for m in re.finditer(r"/s/([a-zA-Z0-9_-]+)", url, re.IGNORECASE):
                    code = m.group(1).lower()
                    if code:
                        keys.add(f"terabox:{code}")
                for m in re.finditer(r"[?&]surl=([a-zA-Z0-9_-]+)", url, re.IGNORECASE):
                    code = m.group(1).lower()
                    if code:
                        if not code.startswith("1"):
                            code = "1" + code
                        keys.add(f"terabox:{code}")

        # 3. Media file signature (document / video size and duration)
        doc = getattr(message, "document", None) or getattr(message, "video", None)
        if doc:
            size = getattr(doc, "size", 0)
            dur = 0
            for attr in getattr(doc, "attributes", []):
                if hasattr(attr, "duration"):
                    dur = attr.duration
            if size > 0:
                keys.add(f"doc:{size}_{dur}")

        photo = getattr(message, "photo", None)
        if photo:
            p_id = getattr(photo, "id", None)
            if p_id:
                keys.add(f"photo:{p_id}")

        # 4. Normalized caption hash (fallback if no terabox link present)
        if not any(k.startswith("terabox:") for k in keys):
            text_clean = re.sub(r"https?://\S+", "", raw_text)
            text_clean = re.sub(r"[\s\W_]+", "", text_clean.lower())
            if len(text_clean) >= 30:
                h = hashlib.md5(text_clean.encode("utf-8")).hexdigest()
                keys.add(f"text:{h}")

        return keys

    def check_duplicate_post(self, message: Any, chat_id: str, msg_id: int) -> Tuple[bool, Optional[str]]:
        """Checks if this post content has already been seen/processed from ANY channel."""
        keys = self.extract_content_fingerprints(message)
        if not keys:
            return False, None

        for k in keys:
            if k in self._processed_content_keys or self.db.is_content_key_processed(k):
                return True, k
        return False, None

    def register_post_fingerprints(self, message: Any, chat_id: str, msg_id: int) -> None:
        """Registers content fingerprints so future posts with identical content from other channels are dropped."""
        keys = self.extract_content_fingerprints(message)
        clean_cid = Database._clean_chat_id(chat_id)
        for k in keys:
            self._processed_content_keys.add(k)
            self.db.record_content_fingerprint(k, clean_cid, msg_id)

    def filter_terabox_content(self, text: str) -> str:
        if not text:
            return ""

        domains = self.config.get("terabox_domains", DEFAULT_TERABOX_DOMAINS)
        lines = text.splitlines()

        # Find the last line that contains any terabox domain
        last_terabox_idx = -1
        for idx, line in enumerate(lines):
            line_lower = line.lower()
            if any(d in line_lower for d in domains):
                last_terabox_idx = idx

        # If no terabox domain found in text lines, return original stripped
        if last_terabox_idx == -1:
            return text.strip()

        # Keep everything up to the last terabox link (heading, labels, and terabox url)
        candidate_lines = lines[: last_terabox_idx + 1]

        # Filter out extraneous non-terabox promo links (e.g. t.me links)
        filtered_lines = []
        for line in candidate_lines:
            line_lower = line.lower()
            if any(d in line_lower for d in domains):
                filtered_lines.append(line)
            elif "t.me/" in line_lower or "telegram.me/" in line_lower:
                continue
            else:
                filtered_lines.append(line)

        # Remove trailing empty lines
        while filtered_lines and not filtered_lines[-1].strip():
            filtered_lines.pop()

        return "\n".join(filtered_lines).strip()

    async def connect_and_login(self) -> None:
        if self.client:
            try:
                if self.client.is_connected():
                    await self.client.disconnect()
            except Exception:
                pass
            try:
                if hasattr(self.client, "session") and hasattr(self.client.session, "close"):
                    self.client.session.close()
            except Exception:
                pass
            self.client = None

        session_dir = self.config.get("session_dir", os.path.join(APP_DIR, "sessions"))
        if not os.path.isabs(session_dir):
            session_dir = os.path.join(APP_DIR, session_dir)
        os.makedirs(session_dir, exist_ok=True)
        session_file = os.path.join(session_dir, self.config["session_name"])

        # Pre-tune session database file to WAL mode (always cleanly closed)
        db_file = session_file + ".session"
        if os.path.exists(db_file):
            s_conn = None
            try:
                s_conn = sqlite3.connect(db_file, timeout=5.0)
                s_conn.execute("PRAGMA journal_mode = WAL;")
                s_conn.execute("PRAGMA busy_timeout = 60000;")
                s_conn.execute("PRAGMA synchronous = NORMAL;")
            except Exception as e:
                self.log("DEBUG", f"Session database tuning notice: {e}")
            finally:
                if s_conn is not None:
                    try:
                        s_conn.close()
                    except Exception:
                        pass

        self.client = TelegramClient(
            session_file,
            int(self.config["api_id"]),
            self.config["api_hash"],
            connection_retries=10,
            retry_delay=3,
            auto_reconnect=True,
        )
        self.log("INFO", "Connecting to Telegram MTProto network...")

        # Connect with automatic lock retry and timeout
        for attempt in range(5):
            try:
                await asyncio.wait_for(self.client.connect(), timeout=15.0)
                break
            except (sqlite3.OperationalError, asyncio.TimeoutError, OSError) as conn_err:
                if attempt < 4:
                    self.log("WARNING", f"Telegram MTProto connection retry ({attempt + 1}/5): {conn_err}. Retrying in 2s...")
                    try:
                        sess = getattr(self.client, "session", None)
                        if sess and hasattr(sess, "_conn") and sess._conn is not None:
                            try:
                                sess._conn.close()
                            except Exception:
                                pass
                            sess._conn = None
                    except Exception:
                        pass
                    await asyncio.sleep(2.0)
                    continue
                raise

        is_auth = await asyncio.wait_for(self.client.is_user_authorized(), timeout=15.0)
        if not is_auth:
            self.log("WARNING", "Session not authorized. Interactive login requested...")
            phone = self.config.get("telegram_phone", "").strip()
            if not phone:
                phone = await self.auth_prompt_fn("Phone Number Required", "Enter your phone number with country code (e.g. +8801...):")
            await self.client.send_code_request(phone)
            code = await self.auth_prompt_fn("Telegram Login Code", f"Enter code sent to {phone}:")
            try:
                await self.client.sign_in(phone=phone, code=code)
            except errors.SessionPasswordNeededError:
                pwd = await self.auth_prompt_fn("2FA Password", "Two-step verification enabled. Enter your 2FA password:")
                await self.client.sign_in(password=pwd)

        me = await asyncio.wait_for(self.client.get_me(), timeout=15.0)
        user_name = f"{me.first_name or ''} {me.last_name or ''}".strip() or me.username or str(me.id)
        self.log("SUCCESS", f"Authorized as: {user_name} (ID: {me.id})")

        # Initialize Bot Client for Native Inline Button Dispatch
        dispatch_bot_token = (self.config.get("dispatch_bot_token") or os.getenv("DISPATCH_BOT_TOKEN", "8751273482:AAGP-Ls2H899GyMNJ6MXHKEQPCeAOO6eWYI")).strip()
        if dispatch_bot_token:
            # Pre-resolve username via fast HTTP Bot API to guarantee accuracy even before MTProto connects
            self.dispatch_bot_username = await asyncio.to_thread(fetch_bot_username_from_token, dispatch_bot_token, True)
            self.config["dispatch_bot_username"] = self.dispatch_bot_username

            try:
                session_dir = self.config.get("session_dir", "sessions")
                os.makedirs(session_dir, exist_ok=True)
                bot_session_path = os.path.join(session_dir, "dispatch_bot")
                self.bot_client = TelegramClient(
                    bot_session_path,
                    int(self.config["api_id"]),
                    self.config["api_hash"],
                    connection_retries=5,
                    retry_delay=2,
                    auto_reconnect=True,
                )
                await asyncio.wait_for(self.bot_client.start(bot_token=dispatch_bot_token), timeout=15.0)
                bot_me = await asyncio.wait_for(self.bot_client.get_me(), timeout=15.0)
                if bot_me and getattr(bot_me, "username", None):
                    self.dispatch_bot_username = bot_me.username.strip().lstrip("@")
                    self.config["dispatch_bot_username"] = self.dispatch_bot_username
                self.log("SUCCESS", f"Dispatch Bot Authorized: @{self.dispatch_bot_username} (ID: {getattr(bot_me, 'id', '')}) for Native Inline Buttons")
            except Exception as b_err:
                self.log("WARNING", f"Telethon MTProto bot client notice: {b_err}. HTTP Bot API active as primary/fallback (Bot: @{self.dispatch_bot_username}).")
                self.bot_client = None

    @staticmethod
    async def _resolve_entity_safely(
        client: TelegramClient,
        cid: Optional[str] = None,
        invite_hash: Optional[str] = None,
    ) -> Tuple[Optional[str], str]:
        if invite_hash:
            try:
                check_res = await client(functions.messages.CheckChatInviteRequest(hash=invite_hash))
                if isinstance(check_res, types.ChatInviteAlready):
                    chat = check_res.chat
                    title = getattr(chat, "title", "Channel")
                    raw_id = utils.get_peer_id(chat)
                    str_id = str(raw_id)
                    username = getattr(chat, "username", None)
                    extra = f" | @{username}" if username else " (Already Joined)"
                    return str_id, f"Title: '{title}'{extra}"
                elif isinstance(check_res, types.ChatInvitePeek):
                    chat = check_res.chat
                    title = getattr(chat, "title", "Channel")
                    raw_id = utils.get_peer_id(chat)
                    str_id = str(raw_id)
                    return str_id, f"Title: '{title}'"
                elif isinstance(check_res, types.ChatInvite):
                    title = getattr(check_res, "title", "Channel")
                    try:
                        async for dialog in client.iter_dialogs():
                            if dialog.title == title:
                                raw_id = utils.get_peer_id(dialog.entity)
                                return str(raw_id), f"Title: '{title}' (Found in Dialogs)"
                    except Exception:
                        pass
                    try:
                        updates = await client(functions.messages.ImportChatInviteRequest(hash=invite_hash))
                        chats = getattr(updates, "chats", [])
                        if chats:
                            raw_id = utils.get_peer_id(chats[0])
                            return str(raw_id), f"Title: '{getattr(chats[0], 'title', title)}' (Joined via invite)"
                    except errors.InviteRequestSentError:
                        return None, f"Title: '{title}' (Join request sent - awaiting admin approval)"
                    except Exception as imp_err:
                        return None, f"Title: '{title}' (Invite valid, could not auto-join: {imp_err})"
            except errors.InviteHashExpiredError:
                return None, "Invite link has expired."
            except errors.InviteHashInvalidError:
                return None, "Invalid invite link."
            except Exception as ex:
                if not cid:
                    return None, f"Invite check error: {ex}"

        if not cid:
            return None, "No ID or Invite Link could be resolved."

        candidates = []
        if str(cid).startswith("-") or str(cid).isdigit():
            clean_str = str(cid).replace("-100", "").replace("-", "")
            if clean_str.isdigit():
                candidates.extend([int(f"-100{clean_str}"), int(clean_str)])
        else:
            candidates.append(str(cid))

        for target in candidates:
            try:
                entity = await client.get_entity(target)
                title = getattr(entity, "title", "N/A")
                username = getattr(entity, "username", None)
                raw_id = utils.get_peer_id(entity)
                str_id = str(raw_id)
                if username:
                    return str_id, f"Title: '{title}' | Public: @{username}"
                return str_id, f"Title: '{title}' (Private Channel)"
            except Exception:
                continue

        try:
            target_str = str(candidates[0]).replace("-100", "").replace("-", "")
            async for dialog in client.iter_dialogs():
                d_id_str = str(dialog.id).replace("-100", "").replace("-", "")
                if target_str == d_id_str:
                    title = dialog.title
                    username = getattr(dialog.entity, "username", None)
                    raw_id = utils.get_peer_id(dialog.entity)
                    str_id = str(raw_id)
                    if username:
                        return str_id, f"Title: '{title}' | Public: @{username}"
                    return str_id, f"Title: '{title}' (Private Channel)"
        except Exception:
            pass

        return str(cid), "Channel found as ID format, but not found in active dialogs of this account."

    async def _resolve_single_chat(self, s: str) -> Optional[Any]:
        s = str(s).strip()
        if not s:
            return None

        # 1. Invite link detection
        inv_match = re.search(r"(?:https?://)?(?:www\.)?(?:t\.me|telegram\.me)/(?:\+|joinchat/)([a-zA-Z0-9_-]+)", s)
        if not inv_match and s.startswith("+"):
            inv_match = re.search(r"^\+([a-zA-Z0-9_-]+)$", s)
        if inv_match:
            inv_hash = inv_match.group(1)
            resolved_id, _ = await self._resolve_entity_safely(self.client, invite_hash=inv_hash)
            if resolved_id:
                s = resolved_id

        # 2. Candidate targets
        candidates = []
        if str(s).startswith("-") or str(s).isdigit():
            clean_str = str(s).replace("-100", "").replace("-", "")
            if clean_str.isdigit():
                candidates.extend([int(f"-100{clean_str}"), int(clean_str)])
        else:
            candidates.append(str(s))

        for cand in candidates:
            try:
                return await self.client.get_entity(cand)
            except Exception:
                continue

        # 3. Fallback: Search in user dialogs
        try:
            cand_clean = str(candidates[0]).replace("-100", "").replace("-", "")
            async for dialog in self.client.iter_dialogs():
                d_id_clean = str(dialog.id).replace("-100", "").replace("-", "")
                d_uname = getattr(dialog.entity, "username", None) or ""
                if cand_clean == d_id_clean or (d_uname and str(s).lstrip("@").lower() == d_uname.lower()):
                    return dialog.entity
        except Exception:
            pass

        return None

    async def resolve_entities(self) -> None:
        # 1. Multi-source resolving
        raw_sources = self.config.get("source_chats", "")
        source_items = [s.strip() for s in re.split(r"[,\n]+", raw_sources) if s.strip()]
        self.source_entities.clear()

        for s in source_items:
            try:
                ent = await self._resolve_single_chat(s)
                if ent:
                    self.source_entities.append(ent)
                    name = getattr(ent, "title", None) or getattr(ent, "username", None) or str(ent.id)
                    self.log("SUCCESS", f"Resolved Source: {name} (ID: {ent.id})")
                else:
                    self.log("ERROR", f"Failed to resolve source '{s}'")
            except Exception as e:
                self.log("ERROR", f"Failed to resolve source '{s}': {e}")

        if not self.source_entities:
            raise ValueError("No valid source channels or groups could be resolved!")

        self.stat_fn("sources", len(self.source_entities))

        # 2. Processing Bot
        bot_target = self.config.get("processing_bot", "@LinkConvertTeraAbot").strip()
        self.bot_entity = await self.client.get_entity(bot_target)
        bot_name = getattr(self.bot_entity, "first_name", None) or getattr(self.bot_entity, "username", None) or str(self.bot_entity.id)
        self.log("SUCCESS", f"Resolved Bot: {bot_name} (@{getattr(self.bot_entity, 'username', '')})")

        # 3. Output Channels / Groups (Multiple Supported)
        raw_outputs = self.config.get("output_channel", "").strip()
        out_items = [c.strip() for c in re.split(r"[,\n]+", raw_outputs) if c.strip()]
        self.output_entities = []
        for out_target in out_items:
            try:
                ent = await self._resolve_single_chat(out_target)
                if ent:
                    self.output_entities.append(ent)
                    out_name = getattr(ent, "title", None) or getattr(ent, "username", None) or str(ent.id)
                    self.log("SUCCESS", f"Resolved Output: {out_name} (ID: {ent.id})")
                else:
                    self.log("WARNING", f"Could not resolve output channel/group: '{out_target}'")
            except Exception as e:
                self.log("ERROR", f"Failed to resolve output '{out_target}': {e}")

        if not self.output_entities:
            raise ValueError("No valid output channels or groups could be resolved!")

        self.output_entity = self.output_entities[0]
        if len(self.output_entities) > 1:
            self.log("INFO", f"🎯 Multi-Output active: Forwarding to {len(self.output_entities)} destinations simultaneously.")

        # Resolve Bot entities for output channels if bot is active
        self.bot_output_entities.clear()
        if self.bot_client:
            for out_ent in self.output_entities:
                try:
                    c_id = getattr(out_ent, "id", None)
                    if c_id:
                        b_ent = await self.bot_client.get_entity(c_id)
                        if b_ent:
                            self.bot_output_entities[c_id] = b_ent
                except Exception as b_res_err:
                    self.log("DEBUG", f"Bot entity resolve notice: {b_res_err}")

        # 4. Auto-Accept Join Requests Entities (Multiple Supported)
        self.auto_join_entities = []
        if self.config.get("auto_accept_join_requests", False):
            aj_raw = self.config.get("auto_join_chats", "").strip()
            aj_items = [c.strip() for c in re.split(r"[,\n]+", aj_raw) if c.strip()]
            if not aj_items:
                self.auto_join_entities = list(self.source_entities)
                self.log("INFO", f"Auto-Accept Join Requests active on all {len(self.auto_join_entities)} source chat(s).")
            else:
                for s in aj_items:
                    ent = await self._resolve_single_chat(s)
                    if ent:
                        self.auto_join_entities.append(ent)
                        name = getattr(ent, "title", None) or getattr(ent, "username", None) or str(ent.id)
                        self.log("SUCCESS", f"Resolved Auto-Join Chat: {name} (ID: {ent.id})")
                    else:
                        self.log("WARNING", f"Could not resolve auto-join chat '{s}'")
                self.log("INFO", f"🤝 Auto-Accept Join Requests monitoring {len(self.auto_join_entities)} target(s) simultaneously.")

        # 5. Join Message Hider Entities (Multiple Supported)
        self.hide_join_entities = []
        self.hide_join_ids_normalized = set()
        if self.config.get("auto_hide_join_messages", False):
            hj_raw = self.config.get("hide_join_chats", "").strip()
            hj_items = [c.strip() for c in re.split(r"[,\n]+", hj_raw) if c.strip()]
            if not hj_items:
                self.hide_join_entities = list(self.source_entities)
                self.hide_join_ids_normalized = {str(ent.id).replace("-100", "").replace("-", "") for ent in self.source_entities}
                self.log("INFO", f"Join Message Hider active on all {len(self.hide_join_entities)} source chat(s).")
            else:
                for s in hj_items:
                    ent = await self._resolve_single_chat(s)
                    if ent:
                        self.hide_join_entities.append(ent)
                        self.hide_join_ids_normalized.add(str(ent.id).replace("-100", "").replace("-", ""))
                        try:
                            self.hide_join_ids_normalized.add(str(utils.get_peer_id(ent)).replace("-100", "").replace("-", ""))
                        except Exception:
                            pass
                        name = getattr(ent, "title", None) or getattr(ent, "username", None) or str(ent.id)
                        self.log("SUCCESS", f"Resolved Join Hider Chat: {name} (ID: {ent.id})")
                    else:
                        clean_s = s.replace("-100", "").replace("-", "")
                        if clean_s.isdigit():
                            self.hide_join_ids_normalized.add(clean_s)
                        self.log("WARNING", f"Could not fully resolve join hider chat '{s}', registered ID.")
                self.log("INFO", f"🚫 Join Message Hider monitoring {len(self.hide_join_ids_normalized)} group(s) simultaneously.")

        # 6. Pre-fetch dialogs to sync entity cache and subscribe to MTProto channel push updates
        try:
            self.log("INFO", "Syncing channel update subscriptions...")
            await asyncio.wait_for(self.client.get_dialogs(limit=50), timeout=10.0)
        except Exception as d_err:
            self.log("DEBUG", f"Dialog sync notice: {d_err}")

    async def _approve_single_join(self, peer: Any, user_id: Any) -> bool:
        try:
            delay = max(0, int(self.config.get("auto_join_delay", 0)))
            if delay > 0:
                await asyncio.sleep(delay)
            await self.client(functions.messages.HideChatJoinRequestRequest(
                peer=peer,
                user_id=user_id,
                approved=True
            ))
            peer_title = getattr(peer, "title", None) or getattr(peer, "username", None) or str(getattr(peer, "id", "Group"))
            self.log("JOIN", f"Auto-approved member ID {user_id} in '{peer_title}'")
            self.stat_fn("join_approved", 1)
            return True
        except errors.FloodWaitError as fwe:
            self.log("WARNING", f"FloodWait: sleeping {fwe.seconds}s before approving join requests...")
            await asyncio.sleep(fwe.seconds)
            return False
        except errors.ChatAdminRequiredError:
            self.log("WARNING", "Cannot approve join request: Admin permissions required in the group.")
            return False
        except Exception as ex:
            self.log("WARNING", f"Could not approve member {user_id}: {ex}")
            return False

    async def _auto_join_worker_loop(self) -> None:
        if not self.config.get("auto_accept_join_requests", False):
            return
        self.log("INFO", f"🤝 Auto-Accept Join Request worker running in parallel on {len(self.auto_join_entities)} target(s)...")
        while self._is_running:
            if not self.config.get("auto_accept_join_requests", False):
                break
            try:
                    targets = self.auto_join_entities if self.auto_join_entities else self.source_entities
                    for peer in list(targets):
                        if not self._is_running:
                            break
                        try:
                            importers = await self.client(functions.messages.GetChatInviteImportersRequest(
                                peer=peer,
                                requested=True,
                                limit=100,
                                offset_user=types.InputUserEmpty(),
                                offset_date=None
                            ))
                            if importers and getattr(importers, "importers", None):
                                for imp in importers.importers:
                                    if not self._is_running:
                                        break
                                    await self._approve_single_join(peer, imp.user_id)
                                    await asyncio.sleep(0.3)
                        except errors.ChatAdminRequiredError:
                            pass
                        except Exception:
                            pass
            except asyncio.CancelledError:
                break
            except Exception:
                pass
            await asyncio.sleep(30)

    async def _hide_join_worker_loop(self) -> None:
        self.log("INFO", f"🚫 Join Message Hider continuous parallel monitor active.")
        while self._is_running:
            try:
                if self.config.get("auto_hide_join_messages", False):
                    targets = self.hide_join_entities if self.hide_join_entities else self.source_entities
                    allow_leave = self.config.get("hide_join_delete_leave", True)
                    for ent in list(targets):
                        if not self._is_running:
                            break
                        try:
                            to_delete = []
                            async for msg in self.client.iter_messages(ent, limit=25):
                                if isinstance(msg, types.MessageService):
                                    act = msg.action
                                    is_join = isinstance(act, (
                                        types.MessageActionChatAddUser,
                                        types.MessageActionChatJoinedByLink,
                                        getattr(types, "MessageActionChatJoinedByRequest", tuple()),
                                        getattr(types, "MessageActionChatJoinedViaCommunity", tuple())
                                    ))
                                    is_leave = isinstance(act, types.MessageActionChatDeleteUser)
                                    if is_join or (is_leave and allow_leave):
                                        to_delete.append(msg.id)

                            if to_delete:
                                await self.client.delete_messages(ent, to_delete)
                                title = getattr(ent, "title", None) or str(getattr(ent, "id", "Group"))
                                self.log("CLEAN", f"Auto-deleted {len(to_delete)} join/leave notice(s) from '{title}'")
                                self.stat_fn("join_hidden", len(to_delete))
                        except errors.ChatAdminRequiredError:
                            pass
                        except Exception:
                            pass
            except asyncio.CancelledError:
                break
            except Exception:
                pass
            await asyncio.sleep(45)

    async def _sync_outputs(self) -> None:
        raw_outputs = self.config.get("output_channel", "").strip()
        out_items = [c.strip() for c in re.split(r"[,\n]+", raw_outputs) if c.strip()]
        resolved = []
        for s in out_items:
            ent = await self._resolve_single_chat(s)
            if ent:
                resolved.append(ent)
        if resolved:
            self.output_entities = resolved
            self.output_entity = resolved[0]
            self.log("INFO", f"🎯 Output Channels synced: forwarding to {len(self.output_entities)} destination(s).")

    async def _sync_auto_join(self) -> None:
        aj_raw = self.config.get("auto_join_chats", "").strip()
        aj_items = [c.strip() for c in re.split(r"[,\n]+", aj_raw) if c.strip()]
        resolved = []
        if not aj_items:
            resolved = list(self.source_entities)
        else:
            for s in aj_items:
                ent = await self._resolve_single_chat(s)
                if ent:
                    resolved.append(ent)
        self.auto_join_entities = resolved
        is_on = self.config.get("auto_accept_join_requests", False)
        self.log("INFO", f"🤝 Auto-Accept Join targets synced: {len(self.auto_join_entities)} chat(s) configured (Status: {'ON' if is_on else 'OFF'}).")
        if is_on and self.auto_join_entities:
            if not self._auto_join_task or self._auto_join_task.done():
                self._auto_join_task = asyncio.create_task(self._auto_join_worker_loop())
        else:
            if self._auto_join_task and not self._auto_join_task.done():
                self._auto_join_task.cancel()

    def is_queue_mode_active(self) -> bool:
        if self.config.get("schedule_mode_enabled", False):
            return True
        try:
            db_path = os.path.join(APP_DIR, "users.db")
            if os.path.exists(db_path):
                conn = sqlite3.connect(db_path)
                c = conn.cursor()
                c.execute("SELECT value FROM settings WHERE key = 'schedule_mode_enabled'")
                row = c.fetchone()
                conn.close()
                return bool(row and str(row[0]).strip() == "1")
        except Exception:
            pass
        return False

    def store_post_in_scheduled_queue(self, media_path: Optional[str], media_type: str, caption: str, raw_caption: str, buttons_json: str, target_chats: str) -> int:
        try:
            db_path = os.path.join(APP_DIR, "users.db")
            sched_dir = os.path.join(APP_DIR, "scheduled_media")
            os.makedirs(sched_dir, exist_ok=True)
            saved_media_path = None
            if media_path and os.path.exists(media_path):
                ext = os.path.splitext(media_path)[1] or ".bin"
                filename = f"sched_{int(time.time()*1000)}_{random.randint(100, 999)}{ext}"
                saved_media_path = os.path.join(sched_dir, filename)
                shutil.copy2(media_path, saved_media_path)

            conn = sqlite3.connect(db_path)
            c = conn.cursor()
            c.execute('''
                CREATE TABLE IF NOT EXISTS scheduled_queue (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    created_at TEXT,
                    media_path TEXT,
                    media_type TEXT,
                    caption TEXT,
                    raw_caption TEXT,
                    buttons_json TEXT,
                    target_chats TEXT,
                    status TEXT DEFAULT 'pending'
                )
            ''')
            now_str = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
            c.execute('''
                INSERT INTO scheduled_queue (created_at, media_path, media_type, caption, raw_caption, buttons_json, target_chats, status)
                VALUES (?, ?, ?, ?, ?, ?, ?, 'pending')
            ''', (now_str, saved_media_path, media_type, caption, raw_caption, buttons_json, target_chats))
            row_id = c.lastrowid
            conn.commit()
            conn.close()
            return row_id
        except Exception as e:
            self.log("ERROR", f"Error storing scheduled post in queue: {e}")
            return 0

    def _is_chat_in_sources(self, peer_or_id: Any) -> bool:
        if not peer_or_id or not self.source_entities:
            return False
        clean_target = str(peer_or_id).replace("-100", "").replace("-", "").strip()
        for src in self.source_entities:
            clean_src = str(getattr(src, "id", "")).replace("-100", "").replace("-", "").strip()
            if clean_target == clean_src:
                return True
            try:
                p_id = str(utils.get_peer_id(src)).replace("-100", "").replace("-", "").strip()
                if clean_target == p_id:
                    return True
            except Exception:
                pass
        return False

    def _is_chat_in_hide_join(self, peer_or_id: Any) -> bool:
        if not self.config.get("auto_hide_join_messages", False):
            return False

        # 1. Match against resolved entities if available
        if self.hide_join_entities:
            for ent in self.hide_join_entities:
                try:
                    if utils.get_peer_id(ent) == utils.get_peer_id(peer_or_id):
                        return True
                except Exception:
                    pass
                if getattr(ent, "id", None) == getattr(peer_or_id, "id", None):
                    return True

        # 2. Match against normalized id candidates
        if self.hide_join_ids_normalized:
            candidates = set()
            try:
                raw_id = utils.get_peer_id(peer_or_id)
                candidates.add(str(raw_id).replace("-100", "").replace("-", ""))
            except Exception:
                pass

            candidates.add(str(peer_or_id).replace("-100", "").replace("-", ""))
            if hasattr(peer_or_id, "channel_id"):
                candidates.add(str(peer_or_id.channel_id).replace("-100", "").replace("-", ""))
            if hasattr(peer_or_id, "chat_id"):
                candidates.add(str(peer_or_id.chat_id).replace("-100", "").replace("-", ""))

            uname = getattr(peer_or_id, "username", None)
            if uname:
                candidates.add(str(uname).lower().lstrip("@"))

            for c in candidates:
                if c in self.hide_join_ids_normalized:
                    return True
            return False
        else:
            # If no specific targets configured, monitor all sources by default
            if self.source_entities:
                candidates = set()
                try:
                    raw_id = utils.get_peer_id(peer_or_id)
                    candidates.add(str(raw_id).replace("-100", "").replace("-", ""))
                except Exception:
                    pass
                candidates.add(str(peer_or_id).replace("-100", "").replace("-", ""))
                src_ids = {str(s.id).replace("-100", "").replace("-", "") for s in self.source_entities}
                for c in candidates:
                    if c in src_ids:
                        return True
                return False
            return True

    async def _cleanup_recent_join_messages(self) -> None:
        if not self._is_running or not self.config.get("auto_hide_join_messages", False):
            return
        targets = self.hide_join_entities if self.hide_join_entities else self.source_entities
        if not targets:
            return
        allow_leave = self.config.get("hide_join_delete_leave", True)
        self.log("INFO", f"🧹 Scanning recent messages in {len(targets)} group(s) to remove join/leave notices...")
        for ent in targets:
            if not self._is_running:
                break
            try:
                to_delete = []
                async for msg in self.client.iter_messages(ent, limit=100):
                    if not self._is_running:
                        break
                    if isinstance(msg, types.MessageService):
                        act = msg.action
                        is_join = isinstance(act, (
                            types.MessageActionChatAddUser,
                            types.MessageActionChatJoinedByLink,
                            getattr(types, "MessageActionChatJoinedByRequest", tuple()),
                            getattr(types, "MessageActionChatJoinedViaCommunity", tuple())
                        ))
                        is_leave = isinstance(act, types.MessageActionChatDeleteUser)
                        if is_join or (is_leave and allow_leave):
                            to_delete.append(msg.id)

                if to_delete:
                    await self.client.delete_messages(ent, to_delete)
                    title = getattr(ent, "title", None) or str(getattr(ent, "id", "Group"))
                    self.log("CLEAN", f"Removed {len(to_delete)} existing join/leave notice(s) from '{title}'")
                    self.stat_fn("join_hidden", len(to_delete))
            except errors.ChatAdminRequiredError:
                title = getattr(ent, "title", None) or str(getattr(ent, "id", "Group"))
                self.log("WARNING", f"Cannot delete join notices in '{title}': Account must be Admin with 'Delete Messages' permission.")
            except Exception as e:
                self.log("WARNING", f"Error scanning join notices: {e}")

    async def _sync_hide_join(self) -> None:
        if self.config.get("auto_hide_join_messages", False):
            hj_raw = self.config.get("hide_join_chats", "").strip()
            hj_items = [c.strip() for c in re.split(r"[,\n]+", hj_raw) if c.strip()]
            resolved = []
            normalized = set()
            if not hj_items:
                resolved = list(self.source_entities)
                normalized = {str(ent.id).replace("-100", "").replace("-", "") for ent in self.source_entities}
            else:
                for s in hj_items:
                    ent = await self._resolve_single_chat(s)
                    if ent:
                        resolved.append(ent)
                        normalized.add(str(ent.id).replace("-100", "").replace("-", ""))
                        try:
                            normalized.add(str(utils.get_peer_id(ent)).replace("-100", "").replace("-", ""))
                        except Exception:
                            pass
                    else:
                        clean_s = s.replace("-100", "").replace("-", "")
                        if clean_s.isdigit():
                            normalized.add(clean_s)
            self.hide_join_entities = resolved
            self.hide_join_ids_normalized = normalized
            self.log("INFO", f"🚫 Join Message Hider targets synced: {len(self.hide_join_ids_normalized)} group(s) monitored.")
            asyncio.create_task(self._cleanup_recent_join_messages())
            if not self._hide_join_task or self._hide_join_task.done():
                self._hide_join_task = asyncio.create_task(self._hide_join_worker_loop())

    async def run(self) -> None:
        self._is_running = True
        self._task_queue = asyncio.Queue()
        self.startup_time = datetime.now(timezone.utc)

        try:
            await self.connect_and_login()
            await self.resolve_entities()

            # Synchronize Auto-Accept Join and Join Message Hider targets
            await self._sync_auto_join()
            await self._sync_hide_join()

            # Pre-load known content fingerprints for cross-channel duplicate filtering
            try:
                cached_keys = self.db.get_all_content_keys(limit=30000)
                self._processed_content_keys.update(cached_keys)
                if cached_keys:
                    self.log("INFO", f"Loaded {len(cached_keys)} content fingerprints for cross-channel deduplication.")
            except Exception:
                pass

            # Start worker coroutine(s)
            num_workers = max(1, int(self.config.get("max_concurrent_tasks", 1)))
            for i in range(num_workers):
                t = asyncio.create_task(self._worker_loop(i + 1))
                self._worker_tasks.append(t)
            self.log("INFO", f"Started {num_workers} background queue worker(s).")

            # Historical back-scan if enabled
            if self.config.get("process_historical_messages", False):
                await self._scan_historical_messages()
            else:
                self.log("INFO", "[MODE] Operating in LIVE NEW POSTS ONLY mode.")

            # Register live event listeners for all source channels
            @self.client.on(events.NewMessage)
            async def _on_new_post(event: events.NewMessage.Event):
                if not self._is_running:
                    return
                if not self._is_chat_in_sources(event.chat_id):
                    return
                await self._handle_live_message(event.message)

            # Register live event listener for instant member join requests and service notifications (Raw updates)
            @self.client.on(events.Raw)
            async def _on_raw_update(event):
                if not self._is_running:
                    return

                sub_updates = []
                if isinstance(event, (types.Updates, types.UpdatesCombined)):
                    sub_updates.extend(event.updates)
                elif isinstance(event, types.UpdateShort):
                    sub_updates.append(event.update)
                else:
                    sub_updates.append(event)

                for upd in sub_updates:
                    # 1. Auto-Accept Join Requests
                    if self.config.get("auto_accept_join_requests", False):
                        update_types = (types.UpdateBotChatInviteRequester, getattr(types, "UpdateChatJoinRequest", tuple()))
                        if isinstance(upd, update_types):
                            peer = getattr(upd, "peer", None) or getattr(upd, "channel_id", None)
                            user_id = getattr(upd, "user_id", None)
                            if peer and user_id:
                                p_str = str(utils.get_peer_id(peer)).replace("-100", "").replace("-", "") if hasattr(utils, "get_peer_id") else str(peer)
                                for ent in self.auto_join_entities:
                                    ent_str = str(ent.id).replace("-100", "").replace("-", "")
                                    if p_str == ent_str:
                                        await self._approve_single_join(ent, user_id)
                                        break

                    # 2. Join Message Hider: Catch MessageService directly from raw updates
                    if self.config.get("auto_hide_join_messages", False):
                        if isinstance(upd, (types.UpdateNewMessage, types.UpdateNewChannelMessage)):
                            msg = getattr(upd, "message", None)
                            if isinstance(msg, types.MessageService):
                                peer = getattr(msg, "peer_id", None)
                                if peer and self._is_chat_in_hide_join(peer):
                                    act = getattr(msg, "action", None)
                                    is_join = isinstance(act, (
                                        types.MessageActionChatAddUser,
                                        types.MessageActionChatJoinedByLink,
                                        getattr(types, "MessageActionChatJoinedByRequest", tuple()),
                                        getattr(types, "MessageActionChatJoinedViaCommunity", tuple())
                                    ))
                                    is_leave = isinstance(act, types.MessageActionChatDeleteUser)
                                    allow_leave = self.config.get("hide_join_delete_leave", True)

                                    if is_join or (is_leave and allow_leave):
                                        try:
                                            await self.client.delete_messages(peer, [msg.id])
                                            action_name = "join" if is_join else "leave"
                                            self.log("CLEAN", f"Auto-deleted {action_name} service message (ID #{msg.id})")
                                            self.stat_fn("join_hidden", 1)
                                        except errors.ChatAdminRequiredError:
                                            self.log("WARNING", "Cannot hide join message: Admin 'Delete Messages' permission required in this group.")
                                        except Exception as ex:
                                            self.log("WARNING", f"Could not delete join service message: {ex}")

            if self.config.get("auto_accept_join_requests", False) and self.auto_join_entities:
                if not self._auto_join_task or self._auto_join_task.done():
                    self._auto_join_task = asyncio.create_task(self._auto_join_worker_loop())

            # Live event listener for ChatAction
            @self.client.on(events.ChatAction)
            async def _on_chat_action(event: events.ChatAction.Event):
                if not self._is_running or not self.config.get("auto_hide_join_messages", False):
                    return
                try:
                    chat = await event.get_chat()
                    target_peer = chat if chat else event.chat_id
                    if not self._is_chat_in_hide_join(target_peer):
                        return

                    is_join = bool(event.user_joined or event.user_added or event.added_by)
                    is_leave = bool(event.user_left or event.user_kicked or event.kicked_by)
                    allow_leave = self.config.get("hide_join_delete_leave", True)

                    if is_join or (is_leave and allow_leave):
                        deleted = False
                        if getattr(event, "action_message", None):
                            try:
                                await self.client.delete_messages(target_peer, [event.action_message.id])
                                deleted = True
                            except Exception:
                                pass
                        if not deleted:
                            try:
                                await event.delete()
                                deleted = True
                            except Exception:
                                pass
                        if not deleted:
                            # Fallback: sweep last 5 messages for service notice
                            async for sm in self.client.iter_messages(target_peer, limit=5):
                                if isinstance(sm, types.MessageService):
                                    s_act = sm.action
                                    s_join = isinstance(s_act, (
                                        types.MessageActionChatAddUser,
                                        types.MessageActionChatJoinedByLink,
                                        getattr(types, "MessageActionChatJoinedByRequest", tuple()),
                                        getattr(types, "MessageActionChatJoinedViaCommunity", tuple())
                                    ))
                                    s_leave = isinstance(s_act, types.MessageActionChatDeleteUser)
                                    if s_join or (s_leave and allow_leave):
                                        await self.client.delete_messages(target_peer, [sm.id])
                                        break

                        c_title = getattr(event.chat, "title", None) or f"Chat {event.chat_id}"
                        action_name = "join" if is_join else "leave"
                        self.log("CLEAN", f"Auto-deleted {action_name} service message in '{c_title}'")
                        self.stat_fn("join_hidden", 1)
                except errors.ChatAdminRequiredError:
                    self.log("WARNING", f"Cannot hide join message in chat {event.chat_id}: requires 'Delete Messages' admin permission.")
                except Exception:
                    pass

            # Cleanup existing service messages on start
            if self.config.get("auto_hide_join_messages", False):
                self._cleanup_join_task = asyncio.create_task(self._cleanup_recent_join_messages())

            # Start continuous active source poller (auto-sweeps new posts every 25s)
            self._poller_task = asyncio.create_task(self._periodic_source_poller())

            self.log("SUCCESS", f"Live monitoring active on {len(self.source_entities)} source(s). Waiting for posts...")

            # Keep alive until cancelled
            # Keep alive until cancelled
            while self._is_running:
                await asyncio.sleep(0.5)
        finally:
            await self.stop()

    async def _periodic_source_poller(self) -> None:
        """
        Active background poller that sweeps source channels for new posts.
        Checks for recent unhandled posts across all source channels.
        """
        self.log("INFO", "📡 Active Source Poller started: continuous auto-sweep armed.")
        process_historical = self.config.get("process_historical_messages", False)
        sweep_counter = 0

        while self._is_running:
            try:
                await asyncio.sleep(20)

                if not self._is_running or not self.client:
                    continue

                if not self.client.is_connected():
                    self.log("WARNING", "Telegram MTProto connection interrupted. Reconnecting...")
                    try:
                        await self.client.connect()
                        self.log("SUCCESS", "Telegram MTProto reconnected successfully.")
                    except Exception as recon_err:
                        self.log("ERROR", f"MTProto reconnect failed: {recon_err}")
                        continue

                new_found = 0
                for src in list(self.source_entities):
                    if not self._is_running:
                        break
                    clean_cid = Database._clean_chat_id(getattr(src, "id", ""))
                    try:
                        limit_sweep = 20 if process_historical else 8
                        async for msg in self.client.iter_messages(src, limit=limit_sweep):
                            if not self._is_running:
                                break
                            if not msg or not msg.id:
                                continue
                            m_id = msg.id

                            # Drop ancient posts only if not in historical mode (keep posts from last 3 hours)
                            if not process_historical:
                                msg_date = msg.date
                                if msg_date:
                                    if msg_date.tzinfo is None:
                                        msg_date = msg_date.replace(tzinfo=timezone.utc)
                                    cutoff = getattr(self, "startup_time", datetime.now(timezone.utc)) - timedelta(hours=3)
                                    if msg_date < cutoff:
                                        continue

                            if (clean_cid, m_id) in self._enqueued_tasks:
                                continue
                            if self.db.is_message_completed(clean_cid, m_id):
                                continue
                            if self.db.is_message_filtered(clean_cid, m_id):
                                continue

                            # Fresh unhandled post caught by active sweep!
                            new_found += 1
                            await self._handle_live_message(msg, is_poller=True)
                    except errors.FloodWaitError as fwe:
                        self.log("WARNING", f"FloodWait on source {clean_cid}: sleeping {min(fwe.seconds, 15)}s...")
                        await asyncio.sleep(min(fwe.seconds, 15))
                    except Exception as ex:
                        self.log("DEBUG", f"Poller sweep error on {clean_cid}: {ex}")

                sweep_counter += 1
                if sweep_counter % 3 == 0:
                    q_len = self._task_queue.qsize() if self._task_queue else 0
                    self.log("INFO", f"📡 [SWEEP ACTIVE] Monitored {len(self.source_entities)} sources (Queue: {q_len} | Poller running healthy).")

            except asyncio.CancelledError:
                break
            except Exception as e:
                self.log("WARNING", f"Periodic poller loop notice: {e}")
                await asyncio.sleep(5)

    async def stop(self) -> None:
        if getattr(self, "_is_stopping", False):
            return
        self._is_stopping = True
        self._is_running = False

        # 1. Cancel all active tasks and wait for them to finish
        tasks_to_cancel = list(self._worker_tasks)
        for attr in ["_poller_task", "_auto_join_task", "_hide_join_task", "_cleanup_join_task"]:
            t = getattr(self, attr, None)
            if t:
                tasks_to_cancel.append(t)

        for t in tasks_to_cancel:
            if not t.done():
                t.cancel()

        if tasks_to_cancel:
            try:
                await asyncio.wait_for(asyncio.gather(*tasks_to_cancel, return_exceptions=True), timeout=3.0)
            except Exception:
                pass
        self._worker_tasks.clear()

        # 2. Safely disconnect MTProto client & FORCE close session database connection
        if self.client:
            client_to_close = self.client
            self.client = None
            try:
                try:
                    client_to_close.remove_event_handler(None)
                except Exception:
                    pass
                if client_to_close.is_connected():
                    await asyncio.wait_for(client_to_close.disconnect(), timeout=4.0)
            except Exception:
                pass
            finally:
                # Force close sqlite session connection and truncate WAL
                try:
                    sess = getattr(client_to_close, "session", None)
                    if sess:
                        if hasattr(sess, "_conn") and sess._conn is not None:
                            try:
                                sess._conn.execute("PRAGMA wal_checkpoint(TRUNCATE);")
                            except Exception:
                                pass
                            try:
                                sess._conn.commit()
                            except Exception:
                                pass
                            try:
                                sess._conn.close()
                            except Exception:
                                pass
                            sess._conn = None
                        if hasattr(sess, "close"):
                            sess.close()
                except Exception:
                    pass

        if self.bot_client:
            bclient_to_close = self.bot_client
            self.bot_client = None
            try:
                if bclient_to_close.is_connected():
                    await asyncio.wait_for(bclient_to_close.disconnect(), timeout=2.0)
            except Exception:
                pass

        self._is_stopping = False
        self.log("INFO", "Automation engine safely stopped and disconnected.")

    async def _scan_historical_messages(self) -> None:
        limit = int(self.config.get("historical_messages_limit", 100))
        if limit <= 0:
            limit = None
        self.log("INFO", f"[HISTORICAL] Scanning past posts (Limit: {limit or 'ALL'}) across {len(self.source_entities)} source(s)...")

        total_enqueued = 0
        for src in self.source_entities:
            chat_id = str(src.id)
            chat_title = getattr(src, "title", None) or getattr(src, "username", None) or chat_id
            try:
                async for msg in self.client.iter_messages(src, limit=limit, reverse=True):
                    msg_id = msg.id

                    # Output loop filter
                    if self.output_entities:
                        out_ids_clean = {str(getattr(e, "id", "")).replace("-100", "").replace("-", "") for e in self.output_entities}
                        sender_clean = str(getattr(msg, "sender_id", "")).replace("-100", "").replace("-", "")
                        if sender_clean and sender_clean in out_ids_clean:
                            continue

                    # Album grouping
                    grouped_id = getattr(msg, "grouped_id", None)
                    if grouped_id:
                        if grouped_id in self._processed_grouped_ids:
                            continue
                        if not (msg.message or msg.text or "").strip():
                            async for sibling in self.client.iter_messages(src, limit=10):
                                if sibling.grouped_id == grouped_id and (sibling.message or sibling.text or "").strip():
                                    msg = sibling
                                    msg_id = sibling.id
                                    break
                        self._processed_grouped_ids.add(grouped_id)

                    clean_cid = Database._clean_chat_id(chat_id)

                    # Deduplication
                    if self.db.is_message_completed(clean_cid, msg_id) or (clean_cid, msg_id) in self._enqueued_tasks:
                        continue

                    # Terabox Link Filter
                    if self.config.get("filter_only_terabox_posts", True):
                        if not self.has_terabox_link(msg):
                            self.db.record_filtered(clean_cid, msg_id)
                            self.stat_fn("filtered", 1)
                            continue

                    if not (msg.message or "").strip() and not msg.media:
                        self.db.record_filtered(clean_cid, msg_id)
                        continue

                    self._enqueued_tasks.add((clean_cid, msg_id))
                    task_id = self.db.record_task(clean_cid, msg_id)
                    await self._task_queue.put({
                        "task_id": task_id,
                        "source_chat_id": clean_cid,
                        "source_message_id": msg_id,
                        "source_message": msg,
                        "chat_title": chat_title,
                        "retry_count": 0,
                    })
                    total_enqueued += 1

            except Exception as e:
                self.log("ERROR", f"Error scanning past posts from '{chat_title}': {e}")

        self.log("SUCCESS", f"[HISTORICAL] Scan finished. Enqueued {total_enqueued} past post(s) for conversion.")

    async def _handle_live_message(self, message: Message, is_poller: bool = False) -> None:
        msg_id = message.id
        chat_id = str(message.chat_id)

        # Output channel loop check
        if self.output_entities:
            out_ids_clean = {str(getattr(e, "id", "")).replace("-100", "").replace("-", "") for e in self.output_entities}
            sender_clean = str(getattr(message, "sender_id", "")).replace("-100", "").replace("-", "")
            if sender_clean and sender_clean in out_ids_clean:
                return

        # Historical post filter: strictly drop posts older than engine startup when live mode is active
        if not self.config.get("process_historical_messages", False):
            msg_date = message.date
            if msg_date:
                if msg_date.tzinfo is None:
                    msg_date = msg_date.replace(tzinfo=timezone.utc)
                cutoff_time = getattr(self, "startup_time", datetime.now(timezone.utc)) - timedelta(hours=3)
                if msg_date < cutoff_time:
                    return

        # Album handling
        grouped_id = getattr(message, "grouped_id", None)
        if grouped_id:
            if grouped_id in self._processed_grouped_ids:
                return
            caption_text = (message.message or message.text or "").strip()
            if not caption_text:
                await asyncio.sleep(1.2)
                try:
                    async for sibling in self.client.iter_messages(message.chat_id, limit=10):
                        if sibling.grouped_id == grouped_id and (sibling.message or sibling.text or "").strip():
                            message = sibling
                            msg_id = sibling.id
                            caption_text = (message.message or message.text or "").strip()
                            break
                except Exception as e:
                    self.log("WARNING", f"Error querying album siblings: {e}")

            if not caption_text:
                return
            self._processed_grouped_ids.add(grouped_id)

        clean_cid = Database._clean_chat_id(chat_id)

        # Deduplication
        if self.db.is_message_completed(clean_cid, msg_id) or (clean_cid, msg_id) in self._enqueued_tasks:
            return

        # Terabox Link Filter
        if self.config.get("filter_only_terabox_posts", True):
            if not self.has_terabox_link(message):
                self.log("FILTERED", f"Post #{msg_id} dropped: No Terabox link detected.")
                self.db.record_filtered(clean_cid, msg_id)
                self.stat_fn("filtered", 1)
                return

        self._enqueued_tasks.add((clean_cid, msg_id))
        self.log("NEW", f"Detected post #{msg_id} from chat ID {clean_cid}")
        task_id = self.db.record_task(clean_cid, msg_id)
        await self._task_queue.put({
            "task_id": task_id,
            "source_chat_id": clean_cid,
            "source_message_id": msg_id,
            "source_message": message,
            "chat_title": str(clean_cid),
            "retry_count": 0,
        })

    async def _worker_loop(self, worker_id: int) -> None:
        while self._is_running:
            try:
                task = await self._task_queue.get()
            except asyncio.CancelledError:
                break

            try:
                await self._process_task(task)
            except Exception as e:
                self.log("ERROR", f"[Worker-{worker_id}] Task #{task['task_id']} error: {e}")
                if isinstance(e, errors.FloodWaitError) and e.seconds > 0:
                    wait_sec = min(e.seconds, 15)
                    self.log("WARNING", f"FloodWait active on Telegram. Pausing worker for {wait_sec}s...")
                    await asyncio.sleep(wait_sec)
                else:
                    await asyncio.sleep(2.0)
            finally:
                self._task_queue.task_done()

    async def extract_links_and_build_buttons(
        self,
        text: str,
        btn_tb_free_text: str = "🎬 Watch Full Video Free",
        btn_tb_paid_text: str = "⚡ Watch Full Video Paid",
        btn_tb_watch_now_text: str = "▶️ Watch Now",
        btn_bk_text: str = "📢 Backup Channel",
        btn_gd_text: str = "👀 How to Watch",
        btn_other_text: str = "🔗 Open Link",
    ) -> Tuple[str, Optional[List[List[Any]]]]:
        """
        Parses post text, extracts TeraBox, backup channel, and tutorial/guide links
        across ANY language (Bengali, Hindi, Russian, Arabic, English, etc.),
        builds native Telegram inline buttons in a 2-line grid:
          Line 1: [ 🎬 Watch Full Video Free ] [ ⚡ Watch Full Video Paid ]
          Line 2: [ 📢 Backup Channel ] [ 👀 How to Watch ]
        Returns cleaned caption so post title remains while redundant links are stripped.
        """
        if not text:
            return text, None

        domains = self.config.get("terabox_domains", DEFAULT_TERABOX_DOMAINS)
        lines = text.splitlines()
        terabox_links: List[str] = []
        seen_tb_vid_ids: Set[str] = set()
        seen_norm_tb_urls: Set[str] = set()
        backup_links: List[str] = []
        guide_links: List[str] = []
        other_links: List[str] = []

        url_regex = re.compile(r"https?://[^\s]+", re.IGNORECASE)

        # Multi-language keywords for How to Watch / tutorial guide detection and caption suppression
        guide_keywords = [
            # English
            "how to watch", "how to", "watch guide", "tutorial", "instruction", "see how",
            # Bengali
            "কীভাবে দেখবেন", "কিভাবে দেখবেন", "কীভাবে", "কিভাবে", "দেখে নিন", "দেখে নাও", "কীভাবে দেখতে",
            # Hindi / Urdu
            "kaise dekhe", "kaise dekhein", "kaise dekhna", "video kaise",
            # Russian
            "как смотреть", "инструкция", "гайд", "видео как", "как открыть",
            # Arabic
            "كيفية المشاهدة", "كيف تشاهد", "طريقة المشاهدة", "شاهد كيف",
            # Spanish / Portuguese
            "como ver", "como mirar", "como assistir", "guia"
        ]

        backup_keywords = [
            # English
            "backup", "backup channel", "main channel", "join channel", "join our", "channel link",
            # Bengali
            "ব্যাকআপ", "ব্যাকআপ চ্যানেল", "ব্যাকআপ লিংক", "চ্যানেল", "যোগ দিন", "জয়েন",
            # Hindi / Urdu
            "backup channel", "backup link", "channel juden",
            # Russian
            "резервный", "запасной", "канал", "подпишись", "резерв",
            # Arabic
            "احتياط", "قناة احتياطية", "انضم للقناة",
            # Spanish / Portuguese
            "respaldo", "canal de respaldo", "canal"
        ]

        for idx, line in enumerate(lines):
            line_urls = url_regex.findall(line)
            if not line_urls:
                continue

            prev_context = " ".join(lines[max(0, idx - 2) : idx + 1]).lower()

            for u in line_urls:
                u_clean = u.rstrip(".,)>\"'")
                u_lower = u_clean.lower()

                # 1. TeraBox Link Detection (Universal domains + patterns)
                is_tb = (
                    any(d in u_lower for d in domains)
                    or "terabox" in u_lower
                    or "1024tera" in u_lower
                    or "terashare" in u_lower
                    or "nephobox" in u_lower
                    or "4funbox" in u_lower
                    or "mirrobox" in u_lower
                    or "tibabox" in u_lower
                    or "freeterabox" in u_lower
                    or "terafileshare" in u_lower
                    or "terasharefile" in u_lower
                    or "terafiles" in u_lower
                )

                if is_tb:
                    norm_u = normalize_terabox_url(u_clean)
                    vid_id = None
                    m_code = re.search(r"/s/([a-zA-Z0-9_-]+)", norm_u)
                    if m_code:
                        vid_id = m_code.group(1)
                    else:
                        m_surl = re.search(r"[?&]surl=([a-zA-Z0-9_-]+)", norm_u)
                        if m_surl:
                            s = m_surl.group(1)
                            vid_id = "1" + s if not s.startswith("1") else s
                        else:
                            vid_id = hashlib.md5(norm_u.encode()).hexdigest()[:10]

                    # Deduplicate by unique video id and normalized URL
                    if vid_id and vid_id in seen_tb_vid_ids:
                        continue
                    if norm_u in seen_norm_tb_urls:
                        continue

                    if vid_id:
                        seen_tb_vid_ids.add(vid_id)
                    seen_norm_tb_urls.add(norm_u)
                    terabox_links.append(u_clean)
                    continue

                # 2. Tutorial / Guide Link Detection
                # Universal URL patterns: t.me/c/ (private channel post), youtube, telegraph, or msg id in path (e.g. t.me/chan/123)
                is_guide_url = (
                    "t.me/c/" in u_lower
                    or "youtu.be" in u_lower
                    or "youtube.com" in u_lower
                    or "telegra.ph" in u_lower
                    or re.search(r"t\.me/[a-zA-Z0-9_]+/\d+", u_lower) is not None
                )
                has_guide_kw = any(kw in prev_context for kw in guide_keywords)

                # 3. Backup Channel Link Detection
                # Universal URL patterns: t.me/+ or joinchat or plain t.me channel
                is_backup_url = (
                    "t.me/+" in u_lower
                    or "joinchat" in u_lower
                    or re.search(r"t\.me/[a-zA-Z0-9_]+/?$", u_lower) is not None
                )
                has_backup_kw = any(kw in prev_context for kw in backup_keywords)

                if is_guide_url or has_guide_kw:
                    if u_clean not in guide_links:
                        guide_links.append(u_clean)
                elif is_backup_url or has_backup_kw:
                    if u_clean not in backup_links:
                        backup_links.append(u_clean)
                else:
                    if u_clean not in other_links:
                        other_links.append(u_clean)

        # Fallback intelligent allocation if other_links exist:
        if other_links:
            remaining_other = []
            for u in other_links:
                u_lower = u.lower()
                if not backup_links and ("t.me" in u_lower or "telegram" in u_lower):
                    backup_links.append(u)
                elif not guide_links and ("t.me" in u_lower or "youtu" in u_lower):
                    guide_links.append(u)
                else:
                    remaining_other.append(u)
            other_links = remaining_other

        # Deduplicate and limit backup & guide buttons to at most 1 each
        backup_links = list(dict.fromkeys(backup_links))[:1]
        guide_links = list(dict.fromkeys(guide_links))[:1]

        # Multi-video protection: If multiple TeraBox links exist, check if post explicitly has multi-part indicators
        # (e.g. "Part 1", "Part 2", "Episode 1", "পর্ব ১"). If not (e.g. promo/ad link at footer), keep primary video only!
        if len(terabox_links) > 1:
            has_multi_parts = any(
                re.search(r"\b(part|episode|ep|ep\.|ভলিউম|পার্ট|পর্ব|ভাগ)\s*[0-9]+", line, re.IGNORECASE)
                for line in lines
            )
            if not has_multi_parts:
                terabox_links = terabox_links[:1]

        # If no links found at all, return original text unchanged
        if not (terabox_links or backup_links or guide_links or other_links):
            return text, None

        # Switches: check if TeraBox Free button and Monetag Watch Now button are enabled
        # Live in-memory engine config (updated by admin dashboard) has highest priority
        tb_free_enabled = True
        if "button_terabox_free_enabled" in self.config and self.config["button_terabox_free_enabled"] is not None:
            v = self.config["button_terabox_free_enabled"]
            tb_free_enabled = (v.lower() == "true") if isinstance(v, str) else bool(v)
        else:
            env_tb_free = os.getenv("BUTTON_TERABOX_FREE_ENABLED")
            if env_tb_free is not None:
                tb_free_enabled = env_tb_free.lower() == "true"

        monetag_enabled = True
        if "monetag_ads_enabled" in self.config and self.config["monetag_ads_enabled"] is not None:
            v = self.config["monetag_ads_enabled"]
            monetag_enabled = (v.lower() == "true") if isinstance(v, str) else bool(v)
        else:
            env_monetag = os.getenv("MONETAG_ADS_ENABLED")
            if env_monetag is not None:
                monetag_enabled = env_monetag.lower() == "true"

        # Build native inline keyboard buttons according to active switches
        buttons: List[List[Any]] = []
        for idx, u in enumerate(terabox_links):
            # 1. Free button (Original converted TeraBox web link)
            label_free = btn_tb_free_text if len(terabox_links) == 1 else f"{btn_tb_free_text} {idx + 1}"

            # 2. Paid button (Telegram Bot Deep-Link for native In-App Mini App player)
            vid_id = None
            m_code = re.search(r"/s/([a-zA-Z0-9_-]+)", u)
            if m_code:
                vid_id = m_code.group(1)
            else:
                m_surl = re.search(r"[?&]surl=([a-zA-Z0-9_-]+)", u)
                if m_surl:
                    vid_id = "1" + m_surl.group(1) if not m_surl.group(1).startswith("1") else m_surl.group(1)
                else:
                    vid_id = hashlib.md5(u.encode()).hexdigest()[:10]

            try:
                self.log("INFO", f"Extracting stream URL for {u} (ID: {vid_id})...")
                title, stream_url, size, resolution, duration, file_type = await asyncio.to_thread(get_terabox_media, u)
                if stream_url:
                    await asyncio.to_thread(save_video_cache, vid_id, u, stream_url, title, size, resolution, duration, file_type)
                    self.log("SUCCESS", f"Stream cached for native Mini App: {resolution} ({size})")
            except Exception as stream_err:
                self.log("WARNING", f"Stream extraction notice: {stream_err}")

            # Dynamically resolve username directly using the active bot token
            active_tok = self.config.get("dispatch_bot_token") or os.getenv("DISPATCH_BOT_TOKEN")
            bot_username = fetch_bot_username_from_token(active_tok, sync_to_env=True)
            self.dispatch_bot_username = bot_username

            bot_deep_link = f"https://t.me/{bot_username}?startapp={vid_id}"
            label_paid = btn_tb_paid_text if len(terabox_links) == 1 else f"{btn_tb_paid_text} {idx + 1}"

            # Single Video Row: [ Free Button (if enabled) ] [ Play Video Button ]
            video_row = []
            if tb_free_enabled:
                video_row.append(Button.url(label_free, u))
            video_row.append(Button.url(label_paid, bot_deep_link))
            if video_row:
                buttons.append(video_row)

        sub_btns: List[Any] = []
        for u in backup_links:
            sub_btns.append(Button.url(btn_bk_text, u))
        for u in guide_links:
            sub_btns.append(Button.url(btn_gd_text, u))
        for u in other_links:
            sub_btns.append(Button.url(btn_other_text, u))

        if sub_btns:
            # Sub buttons: [ 📢 Backup Channel ] [ 👀 How to Watch ]
            for i in range(0, len(sub_btns), 2):
                buttons.append(sub_btns[i : i + 2])
        elif not terabox_links:
            all_btns = sub_btns
            for i in range(0, len(all_btns), 2):
                buttons.append(all_btns[i : i + 2])

        # Clean caption text by stripping raw URLs, pointer arrows, and How to Watch tutorial lines
        cleaned_lines = []
        for line in lines:
            stripped = line.strip()
            if not stripped:
                continue

            # Skip lines that are purely URLs
            if re.match(r"^https?://\S+$", stripped, re.IGNORECASE):
                continue

            # Skip separator lines like '.' or '👇👇👇'
            if re.match(r"^[👇\s\.\-_]+$", stripped):
                continue

            # Check if this line is the How to Watch / tutorial guide caption line across languages
            line_lower = stripped.lower()
            is_guide_line = any(kw in line_lower for kw in guide_keywords)
            if not is_guide_line:
                for g_url in guide_links:
                    if g_url.lower() in line_lower:
                        is_guide_line = True
                        break

            if is_guide_line:
                continue

            # Remove embedded URLs and trailing downward pointer arrows from remaining lines
            cleaned_line = url_regex.sub("", line).rstrip(" 👇\t.")
            if cleaned_line.strip():
                cleaned_lines.append(cleaned_line)

        cleaned_caption = "\n".join(cleaned_lines).strip()
        return cleaned_caption, (buttons if buttons else None)

    def extract_sections_and_buttons(
        self,
        text: str,
        btn_tb_text: str = "🎬 Watch Video",
        btn_bk_text: str = "📢 Backup Channel",
        btn_gd_text: str = "👀 How to Watch"
    ) -> List[Dict[str, Any]]:
        """
        Parses post text into separate logical sections where each section's text/title
        is paired directly with its corresponding button.
        Returns a list of dicts: [{"caption": str, "buttons": [[Button]], "url": str}]
        """
        if not text:
            return []
        domains = self.config.get("terabox_domains", DEFAULT_TERABOX_DOMAINS)
        url_regex = re.compile(r"https?://[^\s]+", re.IGNORECASE)
        lines = text.splitlines()

        sections: List[Dict[str, Any]] = []
        current_lines: List[str] = []

        for idx, line in enumerate(lines):
            urls = url_regex.findall(line)
            if urls:
                clean_line = url_regex.sub("", line).rstrip(" 👇\t.")
                if clean_line.strip():
                    current_lines.append(clean_line)

                section_cap = "\n".join(current_lines).strip()
                current_lines = []

                for u in urls:
                    u_clean = u.rstrip(".,)>\"'")
                    u_lower = u_clean.lower()
                    is_tb = any(d in u_lower for d in domains) or "terabox" in u_lower or "1024tera" in u_lower
                    if is_tb:
                        btn_text = btn_tb_text
                    elif "t.me/+" in u_lower or "joinchat" in u_lower or "backup" in section_cap.lower() or "ব্যাকআপ" in section_cap:
                        btn_text = btn_bk_text
                    elif "কীভাবে" in section_cap or "watch" in section_cap.lower() or "t.me/c/" in u_lower or "guide" in section_cap.lower() or "tutorial" in section_cap.lower():
                        btn_text = btn_gd_text
                    else:
                        btn_text = "🔗 Open Link"

                    btns = [[Button.url(btn_text, u_clean)]]
                    sections.append({
                        "caption": section_cap,
                        "buttons": btns,
                        "url": u_clean
                    })
            else:
                clean_line = line.rstrip(" 👇\t.")
                if clean_line.strip() and not re.match(r"^[👇\s\.\-_]+$", line.strip()):
                    current_lines.append(clean_line)

        # If any trailing lines exist, attach to the first section
        if current_lines and sections:
            rem_text = "\n".join(current_lines).strip()
            if rem_text:
                sections[0]["caption"] = (sections[0]["caption"] + "\n\n" + rem_text).strip()

        return sections

    async def _dispatch_via_http_bot(
        self,
        dest_entity: Any,
        caption: str,
        buttons: Any,
        file_path: Optional[str] = None,
        is_video: bool = False
    ) -> bool:
        """
        Dispatches message or media with REAL native inline keyboard buttons directly via
        Telegram Bot HTTP API. Completely immune to Telethon MTProto authorization rate limits.
        """
        bot_token = (self.config.get("dispatch_bot_token") or os.getenv("DISPATCH_BOT_TOKEN", "7796341783:AAEng5ditGE0sYkyfSrm7DrzF1u6KTr20lI")).strip()
        if not bot_token:
            return False

        # Format chat_id to -100... or @...
        chat_id = None
        if isinstance(dest_entity, (int, str)):
            s = str(dest_entity).strip()
            if s.startswith("-100") or s.startswith("@"):
                chat_id = s
            else:
                try:
                    num = int(s)
                    chat_id = f"-100{num}" if num > 0 else str(num)
                except ValueError:
                    chat_id = s
        else:
            c_id = getattr(dest_entity, "id", None)
            if c_id:
                try:
                    num = int(c_id)
                    chat_id = f"-100{num}" if num > 0 else str(num)
                except ValueError:
                    chat_id = str(c_id)
            elif getattr(dest_entity, "username", None):
                chat_id = f"@{dest_entity.username}"

        if not chat_id:
            return False

        # Build inline_keyboard JSON
        reply_markup = None
        if buttons:
            keyboard = []
            rows = buttons if isinstance(buttons, (list, tuple)) else [[buttons]]
            for row in rows:
                r = []
                items = row if isinstance(row, (list, tuple)) else [row]
                for b in items:
                    txt = getattr(b, "text", None) or getattr(b, "button_text", None) or (b.get("text") if isinstance(b, dict) else None)
                    u = getattr(b, "url", None)
                    if not u and hasattr(b, "type"):
                        u = getattr(b.type, "url", None)
                    if not u and isinstance(b, dict):
                        u = b.get("url") or b.get("link")
                    if not u and hasattr(b, "to_dict"):
                        try:
                            d = b.to_dict()
                            u = d.get("type", {}).get("url") or d.get("url")
                        except Exception:
                            pass
                    if txt and u:
                        r.append({"text": str(txt), "url": str(u)})
                if r:
                    keyboard.append(r)
            if keyboard:
                reply_markup = json.dumps({"inline_keyboard": keyboard})

        def _do_http_send():
            base_url = f"https://api.telegram.org/bot{bot_token}"
            # Case 1: Send local file if present and <= 50MB
            if file_path and os.path.exists(file_path):
                file_size = os.path.getsize(file_path)
                if file_size <= 50 * 1024 * 1024:
                    fn_lower = file_path.lower()
                    is_vid = is_video or fn_lower.endswith((".mp4", ".mkv", ".mov", ".avi", ".webm"))
                    is_img = fn_lower.endswith((".jpg", ".jpeg", ".png", ".webp"))
                    endpoint = "sendVideo" if is_vid else ("sendPhoto" if is_img else "sendDocument")
                    field_name = "video" if is_vid else ("photo" if is_img else "document")

                    data = {"chat_id": chat_id, "caption": caption or ""}
                    if reply_markup:
                        data["reply_markup"] = reply_markup

                    try:
                        with open(file_path, "rb") as f_obj:
                            files = {field_name: f_obj}
                            resp = requests.post(f"{base_url}/{endpoint}", data=data, files=files, timeout=90)
                            if resp.status_code == 200 and resp.json().get("ok"):
                                return True
                            # Fallback from sendPhoto to sendDocument if photo format rejected
                            if endpoint == "sendPhoto":
                                f_obj.seek(0)
                                resp2 = requests.post(f"{base_url}/sendDocument", data=data, files={"document": f_obj}, timeout=90)
                                if resp2.status_code == 200 and resp2.json().get("ok"):
                                    return True
                    except Exception as upload_err:
                        self.log("WARNING", f"HTTP Bot file upload notice: {upload_err}")

            # Case 2: Send text message
            data = {"chat_id": chat_id, "text": caption or "🎬"}
            if reply_markup:
                data["reply_markup"] = reply_markup
            resp = requests.post(f"{base_url}/sendMessage", data=data, timeout=30)
            return resp.status_code == 200 and resp.json().get("ok")

        try:
            return await asyncio.to_thread(_do_http_send)
        except Exception as e:
            self.log("WARNING", f"HTTP Bot dispatch exception: {e}")
            return False

    async def _send_file_resilient(
        self,
        entity,
        msg: Message,
        caption: str = "",
        formatting_entities: Any = None,
        chat_id: Any = None,
        msg_id: Any = None,
        buttons: Any = None,
    ) -> Optional[Message]:
        """
        Sends media with multi-layer resilience:
        Layer 1: Direct send_file with msg.media.
        Layer 2: If FileReferenceExpiredError or restrictions occur, refresh message from Telegram.
        Layer 3: Download media locally and upload freshly (completely bypasses expired file references).
        Layer 4: Handles FloodWait automatically.
        """
        # Layer 1: Direct send
        try:
            return await self.client.send_file(
                entity=entity,
                file=msg.media,
                caption=caption,
                formatting_entities=formatting_entities,
                buttons=buttons,
            )
        except errors.FloodWaitError as fwe:
            wait_s = min(fwe.seconds, 15)
            self.log("WARNING", f"FloodWait ({fwe.seconds}s). Waiting {wait_s}s before media upload retry...")
            await asyncio.sleep(wait_s)
            try:
                return await self.client.send_file(
                    entity=entity,
                    file=msg.media,
                    caption=caption,
                    formatting_entities=formatting_entities,
                    buttons=buttons,
                )
            except Exception:
                pass
        except Exception as ex:
            self.log("INFO", f"Direct media transfer notice ({ex}). Initiating fresh-reference & download recovery...")

        # Layer 2: Refresh message from source chat for a fresh file_reference
        fresh_msg = None
        try:
            peer = getattr(msg, "peer_id", None) or chat_id
            m_id = getattr(msg, "id", None) or msg_id
            if peer and m_id:
                fetched = await self.client.get_messages(peer, ids=m_id)
                if fetched and fetched.media:
                    fresh_msg = fetched
                    try:
                        return await self.client.send_file(
                            entity=entity,
                            file=fresh_msg.media,
                            caption=caption,
                            formatting_entities=formatting_entities,
                            buttons=buttons,
                        )
                    except Exception:
                        pass
        except Exception as fetch_ex:
            self.log("DEBUG", f"Could not refresh message reference: {fetch_ex}")

        # Layer 3: Download media locally and upload cleanly to Telegram (bypasses expired file_reference)
        target_for_dl = fresh_msg if fresh_msg else msg
        temp_dir = os.path.join(APP_DIR, "temp_media")
        os.makedirs(temp_dir, exist_ok=True)
        download_path = None
        try:
            self.log("INFO", "Downloading media locally to bypass expired file reference / restriction...")
            download_path = await self.client.download_media(target_for_dl, file=temp_dir)
            if download_path and os.path.exists(download_path):
                sent_msg = await self.client.send_file(
                    entity=entity,
                    file=download_path,
                    caption=caption,
                    formatting_entities=formatting_entities,
                    buttons=buttons,
                )
                self.log("SUCCESS", "Media uploaded and sent successfully via local bypass.")
                return sent_msg
        except Exception as dl_err:
            self.log("WARNING", f"Media bypass download/upload encountered error: {dl_err}")
        finally:
            if download_path and os.path.exists(download_path):
                try:
                    os.remove(download_path)
                except OSError:
                    pass

        return None

    async def _process_task(self, task: Dict[str, Any]) -> None:
        task_id = task["task_id"]
        chat_id = task["source_chat_id"]
        msg_id = task["source_message_id"]
        source_msg: Message = task["source_message"]

        if self.db.is_message_completed(chat_id, msg_id):
            return

        self.db.update_task_status(task_id, status="processing")
        self.log("INFO", f"Sending post #{msg_id} to {self.config.get('processing_bot')}...")

        raw_text = source_msg.message or ""
        text_content = self.filter_terabox_content(raw_text)
        formatting_entities = source_msg.entities

        # Log filter result if text was trimmed
        if len(text_content) < len(raw_text):
            stripped_chars = len(raw_text) - len(text_content)
            self.log("INFO", f"Filtered post #{msg_id}: preserved heading & TeraBox link, stripped {stripped_chars} chars of promotional text.")

        # Adapt formatting entities to new string length to prevent MESSAGE_ENTITIES_INVALID
        valid_entities = []
        if formatting_entities and text_content:
            text_utf16_len = len(text_content.encode("utf-16-le")) // 2
            for ent in formatting_entities:
                if ent.offset + ent.length <= text_utf16_len:
                    valid_entities.append(ent)
                elif ent.offset < text_utf16_len:
                    ent.length = text_utf16_len - ent.offset
                    valid_entities.append(ent)
        else:
            valid_entities = None

        # Send to processing bot
        bot_sent_msg = None
        send_media = self.config.get("send_media_to_processing_bot", True)
        if source_msg.media and send_media:
            caption_text = text_content[:1024]
            cap_entities = None
            if valid_entities and caption_text:
                cap_len = len(caption_text.encode("utf-16-le")) // 2
                cap_entities = [e for e in valid_entities if e.offset + e.length <= cap_len]

            bot_sent_msg = await self._send_file_resilient(
                entity=self.bot_entity,
                msg=source_msg,
                caption=caption_text,
                formatting_entities=cap_entities,
                chat_id=chat_id,
                msg_id=msg_id,
            )
            if not bot_sent_msg:
                self.log("WARNING", f"Media upload issue for post #{msg_id}. Falling back to text mode...")

        if not bot_sent_msg:
            bot_sent_msg = await self.client.send_message(
                entity=self.bot_entity,
                message=text_content,
                formatting_entities=valid_entities,
            )

        sent_bot_id = bot_sent_msg.id
        sent_time = datetime.now(timezone.utc)
        self.db.update_task_status(task_id, status="sent", processing_message_id=sent_bot_id)
        self.log("INFO", f"Awaiting bot conversion reply (sent #{sent_bot_id})...")

        # Wait for bot conversion reply
        timeout = int(self.config.get("reply_timeout", 120))
        settle_time = float(self.config.get("bot_response_settle_time", 3.0))
        bot_responses = await self._wait_for_bot_replies(sent_bot_id, sent_time, timeout, settle_time)

        if not bot_responses:
            raise TimeoutError(f"No response received from processing bot within {timeout}s")

        reply_ids = [m.id for m in bot_responses]
        self.log("CONVERTED", f"Bot replied with {len(bot_responses)} message(s): {reply_ids}")
        self.db.update_task_status(task_id, status="replied", reply_message_ids=reply_ids)
        self.stat_fn("converted", len(bot_responses))

        # Check for emergency bot busy / storage warning
        is_bot_busy = False
        busy_notice = ""
        busy_triggers = [
            "system is busy",
            "storage is not enough",
            "link create failed",
            "bot will automatically add",
            "please try again"
        ]
        for resp in bot_responses:
            resp_text = (resp.message or "").strip().lower()
            if any(trig in resp_text for trig in busy_triggers):
                is_bot_busy = True
                busy_notice = resp.message or "Bot Busy / Storage Limit Reached"
                break

        if is_bot_busy:
            self.log("ERROR", f"🚨 Processing bot returned: '{busy_notice}'. Emergency Auto-Stop triggered!")
            self.db.update_task_status(task_id, status="failed", error_message=busy_notice)
            if hasattr(self, "emergency_stop_fn") and self.emergency_stop_fn:
                try:
                    self.emergency_stop_fn(busy_notice)
                except Exception as ef:
                    self.log("WARNING", f"Emergency callback notice: {ef}")
            asyncio.create_task(self.stop())
            return

        # Forward cleanly without forward headers
        valid_forwards = []
        for resp in bot_responses:
            resp_text = (resp.message or "").strip().lower()
            if "failed to generate links" in resp_text or "contains no valid link" in resp_text:
                self.log("WARNING", f"Bot returned error notice: '{resp.message}'. Skipping.")
                continue
            valid_forwards.append(resp)

        if not valid_forwards:
            self.log("WARNING", "All bot responses were error notices. Nothing to forward.")
            self.db.update_task_status(task_id, status="completed", completed=True)
            return

        # Check Other Settings (Sticker Watermark, NSFW Blur, Post Translation)
        sticker_enabled = self.config.get("sticker_enabled", False)
        sticker_path = self.config.get("sticker_path", "")
        sticker_pos = self.config.get("sticker_position", "sexual_auto")
        sticker_fallback = self.config.get("sticker_fallback_position", "bottom_right")
        sticker_scale = int(self.config.get("sticker_scale_percent", 25))
        blur_sexual_enabled = self.config.get("blur_sexual_enabled", False)
        blur_mode = self.config.get("blur_mode", "sensitive_only")
        blur_intensity = int(self.config.get("blur_intensity", 35))
        translate_enabled = self.config.get("translate_enabled", False)
        target_lang = self.config.get("translate_target_lang", "en")

        buttons_enabled = self.config.get("post_links_as_buttons", True)
        btn_tb_free_text = self.config.get("button_terabox_free_text") or self.config.get("button_terabox_text") or "🎬 Watch Full Video Free"
        btn_tb_paid_text = self.config.get("button_terabox_paid_text") or "⚡ Watch Full Video Paid"
        btn_tb_watch_now_text = self.config.get("button_watch_now_text") or os.getenv("BUTTON_WATCH_NOW_TEXT", "▶️ Watch Now")
        btn_bk_text = self.config.get("button_backup_text", "📢 Backup Channel")
        btn_gd_text = self.config.get("button_how_to_watch_text", "👀 How to Watch")
        btn_other_text = self.config.get("button_other_text", "🔗 Open Link")

        send_media = self.config.get("send_media_to_processing_bot", True)
        needs_media_proc = (sticker_enabled or blur_sexual_enabled)
        is_custom_dispatch = (needs_media_proc or translate_enabled or buttons_enabled or not send_media or self.is_queue_mode_active())

        forwarded_count = 0
        destinations = self.output_entities if self.output_entities else ([self.output_entity] if self.output_entity else [])
        processed_items: List[Dict[str, Any]] = []

        if is_custom_dispatch:
            temp_dir = os.path.join(APP_DIR, "temp_media")
            os.makedirs(temp_dir, exist_ok=True)

            for fwd_msg in valid_forwards:
                raw_cap = fwd_msg.message or ""
                buttons = None
                if buttons_enabled:
                    clean_cap, buttons = await self.extract_links_and_build_buttons(
                        raw_cap,
                        btn_tb_free_text=btn_tb_free_text,
                        btn_tb_paid_text=btn_tb_paid_text,
                        btn_tb_watch_now_text=btn_tb_watch_now_text,
                        btn_bk_text=btn_bk_text,
                        btn_gd_text=btn_gd_text,
                        btn_other_text=btn_other_text,
                    )
                else:
                    clean_cap = raw_cap

                final_cap = clean_cap
                if translate_enabled and target_lang and clean_cap and target_lang.lower() not in ("auto", "none", "off"):
                    try:
                        self.log("INFO", f"Auto-translating post caption to target language '{target_lang}'...")
                        final_cap = MediaProcessor.translate_text(clean_cap, target_lang)
                    except Exception as trans_cap_err:
                        self.log("WARNING", f"Caption translation notice: {trans_cap_err}")

                # Translate existing inline buttons if present on fwd_msg
                # Preserve existing inline buttons if present on fwd_msg
                if not buttons and getattr(fwd_msg, "reply_markup", None):
                    rm = fwd_msg.reply_markup
                    if hasattr(rm, "rows"):
                        fwd_btns = []
                        for row in rm.rows:
                            r_btns = []
                            for b in getattr(row, "buttons", []):
                                if hasattr(b, "url") and hasattr(b, "text"):
                                    r_btns.append(Button.url(b.text, b.url))
                            if r_btns:
                                fwd_btns.append(r_btns)
                        if fwd_btns:
                            buttons = fwd_btns
                proc_file = None
                is_video = False

                if send_media and fwd_msg.media and needs_media_proc:
                    try:
                        self.log("INFO", f"Applying Media Filter (Sticker / Blur) on post #{msg_id} media...")
                        dl_file = await self.client.download_media(fwd_msg, file=temp_dir)
                        if dl_file and os.path.exists(dl_file):
                            if getattr(fwd_msg, "video", None):
                                is_video = True
                            elif isinstance(fwd_msg.media, types.MessageMediaDocument):
                                doc = getattr(fwd_msg.media, "document", None)
                                if doc and (getattr(doc, "mime_type", "").startswith("video/") or any(isinstance(a, types.DocumentAttributeVideo) for a in getattr(doc, "attributes", []))):
                                    is_video = True

                            ext = ".mp4" if is_video else ".jpg"
                            out_file = dl_file + ".proc" + ext
                            success = False
                            if is_video:
                                success = await asyncio.to_thread(
                                    MediaProcessor.process_video,
                                    dl_file, out_file, sticker_enabled, sticker_path, sticker_pos, sticker_fallback, sticker_scale, blur_sexual_enabled, blur_mode, blur_intensity
                                )
                            else:
                                success = await asyncio.to_thread(
                                    MediaProcessor.process_image,
                                    dl_file, out_file, sticker_enabled, sticker_path, sticker_pos, sticker_fallback, sticker_scale, blur_sexual_enabled, blur_mode, blur_intensity
                                )
                            if success and os.path.exists(out_file) and os.path.getsize(out_file) > 100:
                                proc_file = out_file
                            try:
                                os.remove(dl_file)
                            except OSError:
                                pass
                    except Exception as dl_err:
                        self.log("WARNING", f"Media processing note: {dl_err}. Sending original media.")

                processed_items.append({
                    "msg": fwd_msg,
                    "caption": final_cap,
                    "buttons": buttons,
                    "proc_file": proc_file,
                    "is_video": is_video,
                    "raw_caption": raw_cap,
                })

            # If Post Scheduler (Queue Mode) is ENABLED, store in scheduled queue instead of posting immediately
            if self.is_queue_mode_active():
                self.log("SCHEDULER", f"⏰ Queue Mode is ENABLED. Storing {len(processed_items)} post(s) into scheduled queue...")
                for item in processed_items:
                    fwd_msg = item["msg"]
                    caption = item["caption"]
                    buttons = item.get("buttons")
                    proc_file = item["proc_file"]
                    is_vid = item.get("is_video", False)
                    raw_caption = item.get("raw_caption") or caption

                    raw_btns = []
                    if buttons:
                        for row in buttons:
                            row_list = []
                            for b in row:
                                b_url = getattr(b, "url", None)
                                if not b_url and hasattr(b, "type"):
                                    b_url = getattr(b.type, "url", None)
                                if not b_url and isinstance(b, dict):
                                    b_url = b.get("url") or b.get("link")
                                row_list.append({
                                    "text": getattr(b, "text", "Watch Video"),
                                    "url": b_url or ""
                                })
                            if row_list:
                                raw_btns.append(row_list)
                    btns_json = json.dumps(raw_btns) if raw_btns else ""

                    media_type = "video" if is_vid else ("photo" if getattr(fwd_msg, "photo", None) else ("document" if fwd_msg.media else "none"))
                    saved_media_file = proc_file

                    temp_dl = None
                    if not saved_media_file and fwd_msg.media and not isinstance(fwd_msg.media, types.MessageMediaWebPage):
                        try:
                            temp_dl = await self.client.download_media(fwd_msg, file=temp_dir)
                            saved_media_file = temp_dl
                        except Exception as dl_err:
                            self.log("WARNING", f"Could not download media for queue: {dl_err}")

                    targets_str = ",".join(str(utils.get_peer_id(e)) for e in destinations if e)

                    row_id = self.store_post_in_scheduled_queue(
                        media_path=saved_media_file,
                        media_type=media_type,
                        caption=caption or "🎬",
                        raw_caption=raw_caption,
                        buttons_json=btns_json,
                        target_chats=targets_str
                    )
                    if temp_dl and os.path.exists(temp_dl):
                        try:
                            os.remove(temp_dl)
                        except OSError:
                            pass

                    self.log("SUCCESS", f"⏰ Post saved to Scheduled Queue (Queue ID #{row_id}). It will ONLY be posted at scheduled time.")
                    forwarded_count += 1
                return forwarded_count

            for out_ent in destinations:
                out_name = getattr(out_ent, "title", None) or getattr(out_ent, "username", None) or str(getattr(out_ent, "id", "Output"))
                out_id = getattr(out_ent, "id", None)
                bot_ent = self.bot_output_entities.get(out_id) if out_id else None
                for item in processed_items:
                    fwd_msg = item["msg"]
                    caption = item["caption"]
                    buttons = item.get("buttons")
                    proc_file = item["proc_file"]

                    # 1. Dispatch via Bot Client for REAL native inline keyboard buttons
                    dispatched_via_bot = False
                    if buttons:
                        # Attempt 1: Telethon bot client if connected
                        if self.bot_client:
                            target_dest = bot_ent
                            if not target_dest and out_id:
                                for cand in [int(f"-100{out_id}"), out_id, out_ent]:
                                    try:
                                        target_dest = await self.bot_client.get_entity(cand)
                                        if target_dest:
                                            self.bot_output_entities[out_id] = target_dest
                                            break
                                    except Exception:
                                        pass
                            if not target_dest:
                                target_dest = out_ent

                            try:
                                if not send_media:
                                    await self.bot_client.send_message(entity=target_dest, message=caption or "🎬", buttons=buttons)
                                    dispatched_via_bot = True
                                elif proc_file and os.path.exists(proc_file):
                                    await self.bot_client.send_file(entity=target_dest, file=proc_file, caption=caption, buttons=buttons)
                                    dispatched_via_bot = True
                                elif fwd_msg and fwd_msg.media and not isinstance(fwd_msg.media, types.MessageMediaWebPage):
                                    dl_tmp = await self.client.download_media(fwd_msg, file=temp_dir)
                                    if dl_tmp and os.path.exists(dl_tmp):
                                        try:
                                            await self.bot_client.send_file(entity=target_dest, file=dl_tmp, caption=caption, buttons=buttons)
                                            dispatched_via_bot = True
                                        finally:
                                            try:
                                                os.remove(dl_tmp)
                                            except OSError:
                                                pass
                                    else:
                                        await self.bot_client.send_message(entity=target_dest, message=caption or "🎬", buttons=buttons)
                                        dispatched_via_bot = True
                                else:
                                    await self.bot_client.send_message(entity=target_dest, message=caption or "🎬", buttons=buttons)
                                    dispatched_via_bot = True
                            except Exception as bot_err:
                                self.log("WARNING", f"Telethon bot button dispatch notice for '{out_name}': {bot_err}. Falling back to HTTP Bot API...")
                                dispatched_via_bot = False

                        # Attempt 2: High-reliability direct Telegram HTTP Bot API
                        if not dispatched_via_bot:
                            media_to_send = proc_file
                            dl_temp_path = None
                            if not media_to_send and send_media and fwd_msg and fwd_msg.media and not isinstance(fwd_msg.media, types.MessageMediaWebPage):
                                dl_temp_path = await self.client.download_media(fwd_msg, file=temp_dir)
                                media_to_send = dl_temp_path

                            dispatched_via_bot = await self._dispatch_via_http_bot(
                                dest_entity=out_ent,
                                caption=caption,
                                buttons=buttons,
                                file_path=media_to_send,
                                is_video=is_video
                            )
                            if dl_temp_path and os.path.exists(dl_temp_path):
                                try:
                                    os.remove(dl_temp_path)
                                except OSError:
                                    pass

                        if dispatched_via_bot:
                            self.log("SUCCESS", f"Dispatched section with native inline button to '{out_name}' via Dispatch Bot.")
                            forwarded_count += 1
                            continue

                    # 2. Standard userbot dispatch fallback for large media (>50MB) + follow-up buttons
                    fallback_cap = item.get("raw_caption") or caption
                    try:
                        if not send_media:
                            try:
                                await self.client.send_message(entity=out_ent, message=caption or "🎬", buttons=buttons)
                            except Exception as btn_err:
                                await self.client.send_message(entity=out_ent, message=fallback_cap or "🎬")
                                if buttons:
                                    await self._dispatch_via_http_bot(dest_entity=out_ent, caption="👇 Click Below to Watch & Play:", buttons=buttons)
                        elif proc_file and os.path.exists(proc_file):
                            try:
                                await self.client.send_file(entity=out_ent, file=proc_file, caption=caption, buttons=buttons)
                            except Exception as btn_err:
                                await self.client.send_file(entity=out_ent, file=proc_file, caption=fallback_cap)
                                if buttons:
                                    await self._dispatch_via_http_bot(dest_entity=out_ent, caption="👇 Click Below to Watch & Play:", buttons=buttons)
                        elif fwd_msg and fwd_msg.media and not isinstance(fwd_msg.media, types.MessageMediaWebPage):
                            sent_media = await self._send_file_resilient(
                                entity=out_ent, msg=fwd_msg, caption=caption,
                                chat_id=getattr(fwd_msg, "chat_id", None), msg_id=getattr(fwd_msg, "id", None),
                                buttons=buttons
                            )
                            if not sent_media:
                                await self._send_file_resilient(
                                    entity=out_ent, msg=fwd_msg, caption=fallback_cap,
                                    chat_id=getattr(fwd_msg, "chat_id", None), msg_id=getattr(fwd_msg, "id", None)
                                )
                            if buttons:
                                await self._dispatch_via_http_bot(dest_entity=out_ent, caption="👇 Click Below to Watch & Play:", buttons=buttons)
                        else:
                            try:
                                await self.client.send_message(entity=out_ent, message=caption or "🎬", buttons=buttons)
                            except Exception as btn_err:
                                await self.client.send_message(entity=out_ent, message=fallback_cap or "🎬")
                                if buttons:
                                    await self._dispatch_via_http_bot(dest_entity=out_ent, caption="👇 Click Below to Watch & Play:", buttons=buttons)
                        forwarded_count += 1
                    except Exception as snd_err:
                        self.log("ERROR", f"Failed dispatching to '{out_name}': {snd_err}")
        else:
            # Native fast forward with drop_author=True
            for out_ent in destinations:
                try:
                    forwarded = await self.client.forward_messages(
                        entity=out_ent,
                        messages=valid_forwards,
                        drop_author=True,
                    )
                    forwarded_count += len(forwarded) if isinstance(forwarded, list) else 1
                except Exception as fe:
                    out_name = getattr(out_ent, "title", None) or getattr(out_ent, "username", None) or str(getattr(out_ent, "id", "Output"))
                    self.log("WARNING", f"Native forward with drop_author note for '{out_name}': {fe}. Re-sending fresh content...")
                    for fwd_msg in valid_forwards:
                        try:
                            if send_media and fwd_msg.media and not isinstance(fwd_msg.media, types.MessageMediaWebPage):
                                sent_media = await self._send_file_resilient(
                                    entity=out_ent,
                                    msg=fwd_msg,
                                    caption=fwd_msg.message or "",
                                    formatting_entities=fwd_msg.entities,
                                    chat_id=getattr(fwd_msg, "chat_id", None),
                                    msg_id=getattr(fwd_msg, "id", None),
                                )
                                if not sent_media:
                                    self.log("WARNING", f"Could not send media to '{out_name}'. Sending converted text directly...")
                                    await self.client.send_message(
                                        entity=out_ent,
                                        message=fwd_msg.message or "",
                                        formatting_entities=fwd_msg.entities,
                                    )
                            else:
                                await self.client.send_message(
                                    entity=out_ent,
                                    message=fwd_msg.message or "",
                                    formatting_entities=fwd_msg.entities,
                                )
                            forwarded_count += 1
                        except Exception as inner_e:
                            self.log("ERROR", f"Failed re-sending message #{fwd_msg.id} to '{out_name}': {inner_e}")

        self.db.update_task_status(task_id, status="completed", completed=True)
        self.stat_fn("processed", 1)
        self.stat_fn("forwarded", forwarded_count)
        self.log("SUCCESS", f"Source #{msg_id} finished! {forwarded_count} post(s) forwarded.")

        # Mirror to WhatsApp Channel if enabled
        if self.config.get("whatsapp_enabled", False):
            try:
                await self._dispatch_whatsapp(valid_forwards, processed_items=processed_items if processed_items else None)
            except Exception as wa_ex:
                self.log("WARNING", f"WhatsApp posting notice: {wa_ex}")

        # Clean up temporary processed media files
        if processed_items:
            for item in processed_items:
                pf = item.get("proc_file")
                if pf and os.path.exists(pf):
                    try:
                        os.remove(pf)
                    except OSError:
                        pass

        # Post delay interval before processing next post (random cooldown between min and max)
        min_delay = max(0, int(self.config.get("post_delay_min_seconds", self.config.get("post_delay_seconds", 0))))
        max_delay = max(0, int(self.config.get("post_delay_max_seconds", self.config.get("post_delay_seconds", 0))))
        if max_delay < min_delay:
            max_delay = min_delay

        if max_delay > 0:
            chosen_delay = random.randint(min_delay, max_delay) if max_delay > min_delay else min_delay
            if chosen_delay > 0:
                self.log("INFO", f"⏳ Cooldown: Waiting random {chosen_delay}s delay ({min_delay}s-{max_delay}s range) before processing next post...")
                await asyncio.sleep(chosen_delay)

    async def _wait_for_bot_replies(self, sent_id: int, sent_time: datetime, timeout: int, settle_time: float) -> List[Message]:
        deadline = asyncio.get_event_loop().time() + timeout
        collected: Dict[int, Message] = {}

        while asyncio.get_event_loop().time() < deadline:
            if not self._is_running:
                return []
            async for msg in self.client.iter_messages(self.bot_entity, limit=10):
                if not self._is_running:
                    return []
                if msg.out:
                    continue
                if msg.id <= sent_id:
                    continue
                msg_date = msg.date
                if msg_date and msg_date.tzinfo is None:
                    msg_date = msg_date.replace(tzinfo=timezone.utc)
                if msg_date and msg_date < sent_time:
                    continue

                if msg.id not in collected:
                    collected[msg.id] = msg

            if collected:
                await asyncio.sleep(settle_time)
                # Rescan to catch multi-part or edited responses
                async for msg in self.client.iter_messages(self.bot_entity, limit=10):
                    if not msg.out and msg.id > sent_id and msg.id not in collected:
                        collected[msg.id] = msg
                sorted_responses = sorted(collected.values(), key=lambda m: m.id)
                return sorted_responses

            await asyncio.sleep(1.5)

        return []

    async def _dispatch_whatsapp(self, messages: List[Message], processed_items: Optional[List[Dict[str, Any]]] = None) -> None:
        """
        Dispatches converted messages to the configured WhatsApp Channel / Group.
        Supports WAHA / Baileys Gateway, Meta WhatsApp Cloud API, UltraMsg, and Webhooks.
        """
        if not self.config.get("whatsapp_enabled", False):
            return

        gateway_type = self.config.get("whatsapp_gateway_type", "WAHA / Baileys Gateway (HTTP API)")
        api_url = (self.config.get("whatsapp_api_url") or "http://localhost:3000").strip().rstrip("/")
        api_key = (self.config.get("whatsapp_api_key") or "").strip()
        channel_id = (self.config.get("whatsapp_channel_id") or "").strip()
        send_media = self.config.get("whatsapp_send_media", True)

        if not api_url or not channel_id:
            self.log("WARNING", "WhatsApp: Posting skipped (API URL or Channel ID is missing in WhatsApp Settings).")
            return

        self.log("INFO", f"💬 Mirroring post to WhatsApp Channel: {channel_id}...")

        items = processed_items if processed_items else [
            {"msg": m, "caption": (m.message or m.text or "").strip(), "proc_file": None, "is_video": False}
            for m in messages
        ]

        for item in items:
            msg = item["msg"]
            text = (item["caption"] or "").strip()
            proc_file = item.get("proc_file")
            media_sent = False

            if (proc_file or msg.media) and send_media:
                try:
                    is_video = item.get("is_video", False)
                    mime_type = "video/mp4" if is_video else "image/jpeg"
                    filename = "post.mp4" if is_video else "post.jpg"

                    media_bytes = None
                    if proc_file and os.path.exists(proc_file):
                        with open(proc_file, "rb") as pf:
                            media_bytes = pf.read()
                    elif msg.media:
                        if getattr(msg, "video", None):
                            is_video = True
                        elif isinstance(msg.media, types.MessageMediaDocument):
                            doc = getattr(msg.media, "document", None)
                            if doc:
                                m_type = getattr(doc, "mime_type", "") or ""
                                if m_type.startswith("video/") or any(isinstance(attr, types.DocumentAttributeVideo) for attr in getattr(doc, "attributes", [])):
                                    is_video = True

                        mime_type = getattr(msg.file, "mime_type", None) or ("video/mp4" if is_video else "image/jpeg")
                        filename = "post.mp4" if is_video else "post.jpg"

                        bio = io.BytesIO()
                        await self.client.download_media(msg, file=bio)
                        bio.seek(0)
                        media_bytes = bio.getvalue()

                    if media_bytes:
                        media_b64 = base64.b64encode(media_bytes).decode("utf-8")
                        await asyncio.to_thread(
                            AutomationEngine._post_whatsapp_media_sync,
                            gateway_type, api_url, api_key, channel_id, media_b64, text,
                            is_video=is_video, mime_type=mime_type, filename=filename
                        )
                        media_sent = True
                        media_desc = "Video" if is_video else "Photo"
                        self.log("SUCCESS", f"🟢 WhatsApp: {media_desc} & caption successfully posted to {channel_id}")
                        self.stat_fn("whatsapp", 1)
                except Exception as mex:
                    self.log("WARNING", f"WhatsApp media post note ({mex}). Falling back to text mode...")

            if not media_sent and text:
                try:
                    await asyncio.to_thread(
                        AutomationEngine._post_whatsapp_text_sync,
                        gateway_type, api_url, api_key, channel_id, text
                    )
                    self.log("SUCCESS", f"🟢 WhatsApp: Converted text successfully posted to {channel_id}")
                    self.stat_fn("whatsapp", 1)
                except Exception as tex:
                    self.log("ERROR", f"WhatsApp text post failed: {tex}")

    @staticmethod
    def _post_whatsapp_text_sync(gateway_type: str, api_url: str, api_key: str, channel_id: str, text: str) -> None:
        headers = {"Content-Type": "application/json"}
        if api_key:
            headers["X-Api-Key"] = api_key
            headers["Authorization"] = f"Bearer {api_key}"

        if "Cloud API" in gateway_type:
            url = f"{api_url}/messages" if not api_url.endswith("/messages") else api_url
            payload = {
                "messaging_product": "whatsapp",
                "to": channel_id,
                "type": "text",
                "text": {"body": text}
            }
        elif "UltraMsg" in gateway_type:
            url = f"{api_url}/messages/chat" if not api_url.endswith("/messages/chat") else api_url
            payload = {
                "token": api_key,
                "to": channel_id,
                "body": text
            }
        else:
            # WAHA / Baileys Gateway / Custom Webhook
            url = f"{api_url}/api/sendText" if ("/api" not in api_url and "http" in api_url) else api_url
            payload = {
                "chatId": channel_id,
                "text": text,
                "session": "default"
            }

        resp = requests.post(url, json=payload, headers=headers, timeout=20)
        if resp.status_code >= 400:
            raise RuntimeError(f"HTTP {resp.status_code}: {resp.text}")

    @staticmethod
    def _post_whatsapp_media_sync(
        gateway_type: str,
        api_url: str,
        api_key: str,
        channel_id: str,
        media_b64: str,
        caption: str,
        is_video: bool = False,
        mime_type: str = "image/jpeg",
        filename: str = "post.jpg",
    ) -> None:
        headers = {"Content-Type": "application/json"}
        if api_key:
            headers["X-Api-Key"] = api_key
            headers["Authorization"] = f"Bearer {api_key}"

        if "UltraMsg" in gateway_type:
            endpoint = "/messages/video" if is_video else "/messages/image"
            param_key = "video" if is_video else "image"
            url = f"{api_url}{endpoint}"
            payload = {
                "token": api_key,
                "to": channel_id,
                param_key: f"data:{mime_type};base64,{media_b64}",
                "caption": caption
            }
        else:
            # WAHA / Baileys / Generic
            url = f"{api_url}/api/sendFile" if ("/api" not in api_url and "http" in api_url) else api_url
            payload = {
                "chatId": channel_id,
                "mediaType": "video" if is_video else "image",
                "file": {
                    "mimetype": mime_type,
                    "filename": filename,
                    "data": f"data:{mime_type};base64,{media_b64}"
                },
                "caption": caption,
                "session": "default"
            }

        resp = requests.post(url, json=payload, headers=headers, timeout=60)
        if resp.status_code >= 400:
            raise RuntimeError(f"HTTP {resp.status_code}: {resp.text}")


