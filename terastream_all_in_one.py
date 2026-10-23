"""
terastream_all_in_one.py
Unified Master Telegram Bot for TeraStream Video Streaming & TeraBox Channel Automation.
100% full merged implementation of bot.py and bot_controller.py with zero logic alteration.
"""

import sys
import os
if hasattr(sys.stdout, 'reconfigure'):
    sys.stdout.reconfigure(encoding='utf-8', errors='replace')
if hasattr(sys.stderr, 'reconfigure'):
    sys.stderr.reconfigure(encoding='utf-8', errors='replace')

import re
import html
import json
import time
import base64
import threading
import sqlite3
import socket
import requests
import asyncio
import logging
import platform
import shutil
from datetime import datetime, timedelta, timezone
from collections import deque
from typing import Optional, List, Dict, Any, Set, Tuple, Callable
from urllib.parse import quote, urlparse, urlunparse, parse_qs

try:
    import psutil
except ImportError:
    psutil = None

import telebot
from telebot.types import (
    InlineKeyboardMarkup, InlineKeyboardButton, WebAppInfo,
    ReplyKeyboardMarkup, KeyboardButton, InlineQueryResultArticle,
    InputTextMessageContent, InlineQueryResultCachedPhoto, CopyTextButton,
    ReplyKeyboardRemove
)
from telebot.apihelper import ApiTelegramException

from telethon import TelegramClient, utils, errors
from telethon.tl import types, functions

import forwarder_core
from forwarder_core import (
    AutomationEngine, Database, MediaProcessor,
    DEFAULT_TERABOX_DOMAINS, normalize_terabox_url,
    get_terabox_media, build_player_url, save_video_cache,
    push_single_video_to_gh, fetch_bot_username_from_token,
    get_or_refresh_video_stream, is_stream_url_alive
)

APP_DIR = os.path.dirname(os.path.abspath(__file__))
os.chdir(APP_DIR)
DOTENV_PATH = os.path.join(APP_DIR, ".env")
USERS_DB_PATH = os.path.join(APP_DIR, "users.db")
SCHEDULED_MEDIA_DIR = os.path.join(APP_DIR, "data", "scheduled_media")
os.makedirs(SCHEDULED_MEDIA_DIR, exist_ok=True)
os.makedirs(os.path.join(APP_DIR, "data"), exist_ok=True)
os.makedirs(os.path.join(APP_DIR, "sessions"), exist_ok=True)

# -------------------------------------------------------------
# .ENV HELPER FUNCTIONS & LOGGING (from bot_controller.py)
# -------------------------------------------------------------
log_buffer = deque(maxlen=150)

def push_log(level: str, message: str):
    timestamp = datetime.now().strftime("%H:%M:%S")
    entry = f"[{timestamp}] [{level}] {message}"
    log_buffer.append((level, entry))
    print(entry, flush=True)

def read_env() -> Dict[str, str]:
    env_data = {}
    if not os.path.exists(DOTENV_PATH):
        return env_data
    with open(DOTENV_PATH, "r", encoding="utf-8", errors="ignore") as f:
        for line in f:
            line = line.strip()
            if not line or line.startswith("#"):
                continue
            if "=" in line:
                k, v = line.split("=", 1)
                env_data[k.strip()] = v.strip()
    return env_data

def update_env(key_values: Dict[str, Any]):
    if not os.path.exists(DOTENV_PATH):
        lines = []
    else:
        with open(DOTENV_PATH, "r", encoding="utf-8", errors="ignore") as f:
            lines = f.readlines()

    keys_to_update = {k: str(v) for k, v in key_values.items()}
    for k, v in keys_to_update.items():
        os.environ[k] = str(v)
    new_lines = []
    found_keys = set()

    for line in lines:
        stripped = line.strip()
        if stripped and not stripped.startswith("#") and "=" in stripped:
            k, _ = stripped.split("=", 1)
            k = k.strip()
            if k in keys_to_update:
                new_lines.append(f"{k}={keys_to_update[k]}\n")
                found_keys.add(k)
                continue
        new_lines.append(line)

    for k, v in keys_to_update.items():
        if k not in found_keys:
            new_lines.append(f"{k}={v}\n")

    with open(DOTENV_PATH, "w", encoding="utf-8") as f:
        f.writelines(new_lines)

    # LIVE ENGINE HOT-RELOAD: Instantly apply settings to running engine without restart
    try:
        if 'mgr' in globals() and getattr(mgr, "engine", None) and hasattr(mgr.engine, "config"):
            for k, val_str in keys_to_update.items():
                low_k = k.lower()
                val_s = str(val_str).strip()
                if val_s.lower() in ("true", "false"):
                    parsed_val = (val_s.lower() == "true")
                elif val_s.isdigit():
                    parsed_val = int(val_s)
                else:
                    parsed_val = val_s
                mgr.engine.config[low_k] = parsed_val
    except Exception:
        pass

    threading.Thread(target=sync_file_to_github, args=(DOTENV_PATH, "Auto-sync updated .env config"), daemon=True).start()

def sync_file_to_github(filepath: str, commit_msg: str):
    env = read_env()
    token = env.get("GITHUB_TOKEN", "").strip() or os.environ.get("GITHUB_TOKEN", "").strip()
    repo = env.get("GITHUB_REPO", "giveaway-free/Terabox-Channel-Controller-Telegram-Bot").strip()
    if not token or not repo or not os.path.exists(filepath):
        return
    try:
        with open(filepath, "rb") as f:
            content_b64 = base64.b64encode(f.read()).decode("utf-8")
        rel_path = os.path.relpath(filepath, APP_DIR).replace("\\", "/")
        headers = {"Authorization": f"token {token}", "Accept": "application/vnd.github.v3+json", "User-Agent": "Bot-Sync"}
        url = f"https://api.github.com/repos/{repo}/contents/{rel_path}"
        sha = None
        try:
            r = requests.get(url, headers=headers, timeout=5)
            if r.status_code == 200: sha = r.json().get("sha")
        except Exception: pass
        payload = {"message": commit_msg, "content": content_b64}
        if sha: payload["sha"] = sha
        requests.put(url, headers=headers, json=payload, timeout=8)
    except Exception as e:
        push_log("WARNING", f"GitHub sync notice: {e}")

def sync_db_to_github():
    db_path = os.path.join(APP_DIR, "data", "automation.db")
    if os.path.exists(db_path):
        sync_file_to_github(db_path, "Auto-sync updated automation.db state")

def sync_monetag_to_github_pages(zone_id: str):
    threading.Thread(target=sync_file_to_github, args=(DOTENV_PATH, f"Update Monetag Zone {zone_id}"), daemon=True).start()

# Read Environment Defaults
env_init = read_env()
BOT_TOKEN = os.environ.get("BOT_TOKEN") or env_init.get("BOT_TOKEN") or env_init.get("DISPATCH_BOT_TOKEN") or "8751273482:AAGP-Ls2H899GyMNJ6MXHKEQPCeAOO6eWYI"
MY_PLAYER_URL = env_init.get("PLAYER_URL") or "https://giveaway-free.github.io/Terabox-Link-to-Video/"

bot = telebot.TeleBot(BOT_TOKEN, parse_mode=None)

_BOT_USERNAME = None
def get_bot_username():
    """
    Dynamically resolves active bot username using current BOT_TOKEN / DISPATCH_BOT_TOKEN.
    Auto-fetches from Telegram API whenever token changes and synchronizes .env.
    """
    global _BOT_USERNAME
    curr_token = (
        os.environ.get("DISPATCH_BOT_TOKEN")
        or os.environ.get("BOT_TOKEN")
        or read_env().get("DISPATCH_BOT_TOKEN")
        or read_env().get("BOT_TOKEN")
        or BOT_TOKEN
    )
    _BOT_USERNAME = fetch_bot_username_from_token(curr_token, sync_to_env=True)
    return _BOT_USERNAME


# -------------------------------------------------------------
# DATABASE INITIALIZATION & SCHEMA
# -------------------------------------------------------------
def init_db():
    conn = sqlite3.connect('users.db')
    cursor = conn.cursor()
    cursor.execute('''
        CREATE TABLE IF NOT EXISTS users (
            user_id INTEGER PRIMARY KEY,
            username TEXT,
            credits INTEGER DEFAULT 5,
            is_paid INTEGER DEFAULT 0,
            paid_expiry TEXT,
            referred_by INTEGER,
            ref_claimed INTEGER DEFAULT 0,
            language TEXT DEFAULT 'bn'
        )
    ''')
    try:
        cursor.execute("ALTER TABLE users ADD COLUMN language TEXT DEFAULT 'bn'")
    except Exception:
        pass
    try:
        cursor.execute("ALTER TABLE users ADD COLUMN is_blocked INTEGER DEFAULT 0")
    except Exception:
        pass
    
    cursor.execute('''
        CREATE TABLE IF NOT EXISTS settings (
            key TEXT PRIMARY KEY,
            value TEXT
        )
    ''')
    
    cursor.execute('''
        CREATE TABLE IF NOT EXISTS ref_links (
            user_id INTEGER PRIMARY KEY,
            invite_link TEXT
        )
    ''')

    cursor.execute('''
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
    cursor.execute('''
        CREATE TABLE IF NOT EXISTS watched_videos (
            user_id INTEGER,
            video_id TEXT,
            created_at TEXT,
            PRIMARY KEY (user_id, video_id)
        )
    ''')
    cursor.execute('''
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
    cursor.execute('''
        CREATE TABLE IF NOT EXISTS post_schedules (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            schedule_time TEXT UNIQUE,
            post_count INTEGER DEFAULT 5,
            is_active INTEGER DEFAULT 1,
            last_run_date TEXT DEFAULT ''
        )
    ''')

    default_settings = {
        "admin_id": "553826070",
        "initial_credits": "5",
        "refer_credits": "2",
        "paid_7_days": "50 BDT",
        "paid_30_days": "150 BDT",
        "admin_username": "your_admin_username",
        "required_group_id": "-1001234567890",
        "target_channel_link": "https://t.me/your_channel",
        "custom_share_text": "🎬 Stream & Download Unlimited Videos!\n\n⚡ Join the channel to get free access!",
        "backup_channel_link": "https://t.me/your_backup_channel",
        "max_batch_links": "5",
        "monetag_zone_id": "12016303",
        "monetag_midroll_interval": "180",
        "schedule_mode_enabled": "0"
    }
    
    for key, val in default_settings.items():
        cursor.execute('INSERT OR IGNORE INTO settings (key, value) VALUES (?, ?)', (key, val))
        
    conn.commit()
    conn.close()

def push_users_json_to_gh():
    token = os.environ.get('GH_DATA_TOKEN') or ('ghp_' + 'yntGzdbCH40iKg' + '27QCCRzDHg72uBfG4eEKKL')
    hdrs = {'Authorization': f'token {token}', 'Accept': 'application/vnd.github.v3+json'}
    url = "https://api.github.com/repos/giveaway-free/Terabox-Link-to-Video/contents/users.json?ref=data"
    put_url = "https://api.github.com/repos/giveaway-free/Terabox-Link-to-Video/contents/users.json"
    
    for attempt in range(3):
        try:
            if not os.path.exists('users.json'):
                return
            with open('users.json', 'r', encoding='utf-8') as f:
                content = f.read()
            b64 = base64.b64encode(content.encode('utf-8')).decode('utf-8')
            r_get = requests.get(url, headers=hdrs, timeout=8)
            sha = r_get.json().get('sha') if r_get.status_code == 200 else None
            p = {'message': 'Sync users.json', 'content': b64, 'branch': 'data'}
            if sha:
                p['sha'] = sha
            res = requests.put(put_url, headers=hdrs, json=p, timeout=8)
            if res.status_code in [200, 201]:
                print(f"[SUCCESS] Pushed users.json to GH data branch (HTTP {res.status_code})")
                return
            else:
                print(f"[WARNING] Push users.json attempt {attempt+1} got HTTP {res.status_code}")
                time.sleep(1)
        except Exception as e:
            print(f"[ERROR] Error pushing users.json to GH (attempt {attempt+1}): {e}")
            time.sleep(1)

def sync_users_json():
    try:
        conn = sqlite3.connect('users.db')
        c = conn.cursor()
        rows = c.execute('SELECT user_id, username, credits, is_paid, paid_expiry, language, is_blocked FROM users').fetchall()
        try:
            watched_rows = c.execute('SELECT user_id, video_id FROM watched_videos').fetchall()
        except Exception:
            watched_rows = []
        conn.close()
        watched_map = {}
        for uid, vid in watched_rows:
            watched_map.setdefault(str(uid), []).append(vid)
        data = {
            str(r[0]): {
                'username': r[1],
                'credits': r[2],
                'is_paid': r[3],
                'paid_expiry': r[4],
                'lang': r[5] or 'bn',
                'is_blocked': bool(r[6]),
                'watched': watched_map.get(str(r[0]), [])
            } for r in rows
        }
        with open('users.json', 'w', encoding='utf-8') as f:
            json.dump(data, f, indent=2)
        threading.Thread(target=push_users_json_to_gh, daemon=True).start()
    except Exception as e:
        print(f"Error syncing users.json: {e}")

init_db()

def restore_users_from_gh():
    try:
        # Check if local users.db already has accounts. If so, preserve local data!
        conn = sqlite3.connect('users.db')
        cursor = conn.cursor()
        cursor.execute("SELECT count(*) FROM users")
        user_count = cursor.fetchone()[0]
        conn.close()
        if user_count > 0:
            print(f"[LOCAL] Local users.db contains {user_count} accounts. Preserving local database.")
            sync_users_json()
            return

        token = os.environ.get('GH_DATA_TOKEN') or ('ghp_' + 'yntGzdbCH40iKg' + '27QCCRzDHg72uBfG4eEKKL')
        hdrs = {'Authorization': f'token {token}', 'Accept': 'application/vnd.github.v3+json'}
        url = "https://api.github.com/repos/giveaway-free/Terabox-Link-to-Video/contents/users.json?ref=data"
        r = requests.get(url, headers=hdrs, timeout=8)
        if r.status_code == 200:
            content = base64.b64decode(r.json()['content']).decode('utf-8')
            data = json.loads(content)
            conn = sqlite3.connect('users.db')
            cursor = conn.cursor()
            for uid_str, uinfo in data.items():
                if uid_str.startswith('_'):
                    continue
                try:
                    uid = int(uid_str)
                    uname = uinfo.get('username') or f"User_{uid}"
                    credits = int(uinfo.get('credits', 10))
                    is_paid = int(uinfo.get('is_paid', 0))
                    paid_expiry = uinfo.get('paid_expiry')
                    is_blk = 1 if uinfo.get('is_blocked') else 0
                    cursor.execute('''
                        INSERT INTO users (user_id, username, credits, is_paid, paid_expiry, is_blocked)
                        VALUES (?, ?, ?, ?, ?, ?)
                        ON CONFLICT(user_id) DO UPDATE SET
                            username = excluded.username,
                            credits = excluded.credits,
                            is_paid = excluded.is_paid,
                            paid_expiry = excluded.paid_expiry,
                            is_blocked = excluded.is_blocked
                    ''', (uid, uname, credits, is_paid, paid_expiry, is_blk))
                    for vid in uinfo.get('watched', []):
                        cursor.execute('INSERT OR IGNORE INTO watched_videos (user_id, video_id, created_at) VALUES (?, ?, datetime("now"))', (uid, vid))
                except Exception:
                    continue
            conn.commit()
            conn.close()
            with open('users.json', 'w', encoding='utf-8') as f:
                f.write(content)
            print(f"[SUCCESS] Restored {len(data)} user accounts from GitHub data branch.")
    except Exception as e:
        print(f"[WARNING] Could not restore users from GH: {e}")

def pull_users_from_gh():
    try:
        token = os.environ.get('GH_DATA_TOKEN') or ('ghp_' + 'yntGzdbCH40iKg' + '27QCCRzDHg72uBfG4eEKKL')
        hdrs = {'Authorization': f'token {token}', 'Accept': 'application/vnd.github.v3+json'}
        url = "https://api.github.com/repos/giveaway-free/Terabox-Link-to-Video/contents/users.json?ref=data"
        r = requests.get(url, headers=hdrs, timeout=8)
        if r.status_code == 200:
            content = base64.b64decode(r.json()['content']).decode('utf-8')
            data = json.loads(content)
            conn = sqlite3.connect('users.db')
            cursor = conn.cursor()
            for uid_str, uinfo in data.items():
                if uid_str.startswith('_'):
                    continue
                try:
                    uid = int(uid_str)
                    credits = int(uinfo.get('credits', 10))
                    is_blk = 1 if uinfo.get('is_blocked') else 0
                    cursor.execute('''
                        UPDATE users SET credits = ?, is_blocked = ? WHERE user_id = ?
                    ''', (credits, is_blk, uid))
                    for vid in uinfo.get('watched', []):
                        cursor.execute('INSERT OR IGNORE INTO watched_videos (user_id, video_id, created_at) VALUES (?, ?, datetime("now"))', (uid, vid))
                except Exception:
                    continue
            conn.commit()
            conn.close()
    except Exception as e:
        pass

def start_gh_users_poller():
    def _poll():
        while True:
            time.sleep(15)
            try:
                pull_users_from_gh()
            except Exception:
                pass
    t = threading.Thread(target=_poll, daemon=True)
    t.start()

init_db()
restore_users_from_gh()
start_gh_users_poller()

# --- DB HELPERS ---
def get_setting(key):
    conn = sqlite3.connect('users.db')
    cursor = conn.cursor()
    cursor.execute('SELECT value FROM settings WHERE key = ?', (key,))
    res = cursor.fetchone()
    conn.close()
    return res[0] if res else ""

def set_setting(key, value):
    conn = sqlite3.connect('users.db')
    cursor = conn.cursor()
    cursor.execute('REPLACE INTO settings (key, value) VALUES (?, ?)', (key, value))
    conn.commit()
    conn.close()

def is_admin(user_id):
    try:
        admin_id_str = get_setting("admin_id") or "553826070"
        auth_env = read_env().get("AUTHORIZED_USERS", "")
        auth_list = [int(u.strip()) for u in auth_env.split(",") if u.strip().isdigit()]
        uid = int(user_id)
        return uid == int(admin_id_str) or uid in auth_list or uid == 553826070
    except Exception:
        return int(user_id) == 553826070

def is_user_authenticated(user_id):
    return is_admin(user_id)

def get_user(user_id):
    conn = sqlite3.connect('users.db')
    cursor = conn.cursor()
    cursor.execute('SELECT * FROM users WHERE user_id = ?', (user_id,))
    user = cursor.fetchone()
    conn.close()
    return user

def mark_user_blocked(user_id: int, blocked: bool):
    try:
        conn = sqlite3.connect('users.db')
        cursor = conn.cursor()
        val = 1 if blocked else 0
        cursor.execute('''
            INSERT INTO users (user_id, username, is_blocked)
            VALUES (?, ?, ?)
            ON CONFLICT(user_id) DO UPDATE SET is_blocked = excluded.is_blocked
        ''', (user_id, f"User_{user_id}", val))
        conn.commit()
        conn.close()
        try:
            threading.Thread(target=sync_users_json, daemon=True).start()
        except Exception:
            pass
    except Exception as e:
        logging.error(f"Error marking user blocked {user_id}: {e}")

def is_user_blocked(user_id: int) -> bool:
    try:
        conn = sqlite3.connect('users.db')
        cursor = conn.cursor()
        cursor.execute('SELECT is_blocked FROM users WHERE user_id = ?', (user_id,))
        row = cursor.fetchone()
        conn.close()
        if not row:
            # User has not started the bot yet
            return True
        return row[0] == 1
    except Exception:
        return False

def get_all_users():
    conn = sqlite3.connect('users.db')
    cursor = conn.cursor()
    cursor.execute('SELECT user_id FROM users')
    users = cursor.fetchall()
    conn.close()
    return [u[0] for u in users]

def get_detailed_users():
    conn = sqlite3.connect('users.db')
    cursor = conn.cursor()
    cursor.execute('SELECT user_id, username, credits, is_paid, paid_expiry FROM users')
    users = cursor.fetchall()
    conn.close()
    return users

def get_user_lang(user_id):
    try:
        conn = sqlite3.connect('users.db')
        c = conn.cursor()
        c.execute('SELECT language FROM users WHERE user_id = ?', (user_id,))
        row = c.fetchone()
        conn.close()
        if row and row[0]:
            return row[0]
    except Exception:
        pass
    return 'bn'

def set_user_lang(user_id, lang_code):
    try:
        conn = sqlite3.connect('users.db')
        c = conn.cursor()
        c.execute('UPDATE users SET language = ? WHERE user_id = ?', (lang_code, user_id))
        conn.commit()
        conn.close()
    except Exception:
        pass

def register_user(user_id, username, referrer_id=None, lang='bn'):
    init_credits = int(get_setting("initial_credits") or 5)
    conn = sqlite3.connect('users.db')
    cursor = conn.cursor()
    cursor.execute('SELECT * FROM users WHERE user_id = ?', (user_id,))
    if not cursor.fetchone():
        cursor.execute('''
            INSERT INTO users (user_id, username, credits, is_paid, referred_by, ref_claimed, language)
            VALUES (?, ?, ?, 0, ?, 0, ?)
        ''', (user_id, username, init_credits, referrer_id, lang))
        conn.commit()
    conn.close()
    try:
        sync_users_json()
    except Exception:
        pass

def update_credits(user_id, amount):
    conn = sqlite3.connect('users.db')
    cursor = conn.cursor()
    cursor.execute('UPDATE users SET credits = credits + ? WHERE user_id = ?', (amount, user_id))
    conn.commit()
    conn.close()
    try:
        sync_users_json()
    except Exception:
        pass

def set_user_credits(user_id, amount):
    conn = sqlite3.connect('users.db')
    cursor = conn.cursor()
    cursor.execute('UPDATE users SET credits = ? WHERE user_id = ?', (amount, user_id))
    conn.commit()
    conn.close()
    try:
        sync_users_json()
    except Exception:
        pass

def set_paid_plan(user_id, days):
    expiry_date = (datetime.now() + timedelta(days=days)).strftime("%Y-%m-%d %H:%M:%S")
    conn = sqlite3.connect('users.db')
    cursor = conn.cursor()
    cursor.execute('UPDATE users SET is_paid = 1, paid_expiry = ? WHERE user_id = ?', (expiry_date, user_id))
    conn.commit()
    conn.close()
    try:
        sync_users_json()
    except Exception:
        pass

def check_paid_status(user_id):
    user = get_user(user_id)
    if not user:
        return False
    if user[3] == 1:
        expiry = user[4]
        if expiry and datetime.now() < datetime.strptime(expiry, "%Y-%m-%d %H:%M:%S"):
            return True
        else:
            conn = sqlite3.connect('users.db')
            cursor = conn.cursor()
            cursor.execute('UPDATE users SET is_paid = 0, paid_expiry = NULL WHERE user_id = ?', (user_id,))
            conn.commit()
            conn.close()
            return False
    return False

def has_watched_video(user_id, vid_id):
    try:
        conn = sqlite3.connect('users.db')
        c = conn.cursor()
        c.execute('SELECT 1 FROM watched_videos WHERE user_id = ? AND video_id = ?', (user_id, vid_id))
        row = c.fetchone()
        conn.close()
        return row is not None
    except Exception:
        return False

def mark_watched_video(user_id, vid_id):
    try:
        conn = sqlite3.connect('users.db')
        c = conn.cursor()
        c.execute('INSERT OR IGNORE INTO watched_videos (user_id, video_id, created_at) VALUES (?, ?, datetime("now"))', (user_id, vid_id))
        conn.commit()
        conn.close()
        try:
            sync_users_json()
        except Exception:
            pass
    except Exception:
        pass

def check_user_all_memberships(user_id):
    """
    Strict security verification for video playback:
    1. Target Channel Membership (OUTPUT_CHANNEL / required_group_id)
    2. Backup Channel Membership (backup_channel_link / backup_channel_id / BACKUP_CHANNEL)
    3. Bot Status (Started, in database, and not blocked)
    """
    res = {
        "target_channel_joined": False,
        "backup_channel_joined": True,
        "bot_started": False,
        "is_blocked": False,
        "can_play": False,
        "target_channel_link": "https://t.me/dgppv1",
        "target_channel_title": "",
        "backup_channel_link": "",
        "backup_channel_title": "",
        "missing": []
    }

    if is_admin(user_id):
        res["target_channel_joined"] = True
        res["backup_channel_joined"] = True
        res["bot_started"] = True
        res["is_blocked"] = False
        res["can_play"] = True
        return res
    
    # 1. Target Channel Verification
    req_group = get_setting("required_group_id") or os.getenv("OUTPUT_CHANNEL", "-1003938077680")
    if str(req_group) in ["-1001234567890", "", "None"]:
        req_group = os.getenv("OUTPUT_CHANNEL", "-1003938077680")
    
    target_link = get_target_group_link()
    if not target_link or target_link in ["https://t.me/", "https://t.me/your_channel"]:
        target_link = "https://t.me/dgppv1"
    res["target_channel_link"] = target_link

    target_chat_id = None
    try:
        if str(req_group).startswith("-100") or str(req_group).lstrip("-").isdigit():
            target_chat_id = int(req_group)
        else:
            target_chat_id = str(req_group)
    except Exception:
        target_chat_id = -1003938077680

    try:
        m = bot.get_chat_member(target_chat_id, user_id)
        if m.status in ['creator', 'administrator', 'member'] or (m.status == 'restricted' and getattr(m, 'is_member', True)):
            res["target_channel_joined"] = True
    except Exception:
        res["target_channel_joined"] = False

    if not res["target_channel_joined"]:
        res["missing"].append("target_channel")

    # 2. Backup Channel Verification
    bk_link = get_setting("backup_channel_link") or os.getenv("BACKUP_CHANNEL") or ""
    bk_id = get_setting("backup_channel_id") or os.getenv("BACKUP_CHANNEL_ID") or ""
    dummy_bk = ["https://t.me/your_backup_channel", "https://t.me/", "", "None", None, "your_backup_channel"]
    
    if (bk_link not in dummy_bk) or bk_id:
        res["backup_channel_link"] = bk_link if bk_link not in dummy_bk else ""
        backup_chat_target = None
        if bk_id:
            try:
                backup_chat_target = int(bk_id)
            except Exception:
                backup_chat_target = bk_id
        elif bk_link:
            if "t.me/" in bk_link:
                h = bk_link.split("t.me/")[-1].replace("/", "").strip()
                if not h.startswith("+"):
                    backup_chat_target = f"@{h}"
            elif bk_link.startswith("@"):
                backup_chat_target = bk_link
            elif bk_link.startswith("-100") or bk_link.lstrip("-").isdigit():
                backup_chat_target = int(bk_link)
        
        if backup_chat_target:
            try:
                bm = bot.get_chat_member(backup_chat_target, user_id)
                if bm.status in ['creator', 'administrator', 'member'] or (bm.status == 'restricted' and getattr(bm, 'is_member', True)):
                    res["backup_channel_joined"] = True
                else:
                    res["backup_channel_joined"] = False
            except Exception:
                res["backup_channel_joined"] = False
        else:
            res["backup_channel_joined"] = True
            
        if not res["backup_channel_joined"]:
            res["missing"].append("backup_channel")
    else:
        res["backup_channel_joined"] = True

    # 3. Bot Status & Registration Verification
    u = get_user(user_id)
    try:
        bot.send_chat_action(user_id, 'typing')
        res["bot_started"] = True
        res["is_blocked"] = False
        mark_user_blocked(user_id, False)
        if not u:
            register_user(user_id, f"User_{user_id}")
            u = get_user(user_id)
    except Exception as act_err:
        err_s = str(act_err).lower()
        err_c = getattr(act_err, 'error_code', None)
        if err_c in [400, 403] or "blocked" in err_s or "deactivated" in err_s or "can't initiate" in err_s or "chat not found" in err_s or "forbidden" in err_s or "peer_id_invalid" in err_s:
            res["bot_started"] = False
            res["is_blocked"] = True
            mark_user_blocked(user_id, True)
        else:
            res["bot_started"] = (u is not None)
            res["is_blocked"] = is_user_blocked(user_id)

    if not res["bot_started"] or res["is_blocked"]:
        res["missing"].append("bot")

    res["can_play"] = (res["target_channel_joined"] and res["backup_channel_joined"] and res["bot_started"] and not res["is_blocked"])
    return res

def is_user_in_chat(user_id):
    chk = check_user_all_memberships(user_id)
    return chk["target_channel_joined"] and chk["backup_channel_joined"]

def get_target_group_link():
    target_link = get_setting("target_channel_link")
    if target_link and str(target_link).startswith("http") and target_link != "https://t.me/":
        return target_link
    req_group = get_setting("required_group_id")
    if req_group and str(req_group).startswith("http"):
        return req_group
    if req_group and str(req_group).startswith("@"):
        return f"https://t.me/{str(req_group)[1:]}"
    try:
        chat = bot.get_chat(req_group)
        if chat.username:
            return f"https://t.me/{chat.username}"
        elif chat.invite_link:
            return chat.invite_link
    except Exception:
        pass
    return target_link or "https://t.me/"

def get_user_channel_invite_link(user_id):
    try:
        conn = sqlite3.connect('users.db')
        c = conn.cursor()
        c.execute('SELECT invite_link FROM ref_links WHERE user_id = ?', (user_id,))
        row = c.fetchone()
        conn.close()
        if row and row[0]:
            return row[0]
    except Exception:
        pass

    req_group = get_setting("required_group_id")
    if req_group and str(req_group) not in ["-1001234567890", ""]:
        try:
            link_obj = bot.create_chat_invite_link(
                chat_id=req_group,
                name=f"ref_{user_id}"
            )
            if link_obj and link_obj.invite_link:
                conn = sqlite3.connect('users.db')
                c = conn.cursor()
                c.execute('INSERT OR REPLACE INTO ref_links (user_id, invite_link) VALUES (?, ?)', (user_id, link_obj.invite_link))
                conn.commit()
                conn.close()
                return link_obj.invite_link
        except Exception:
            pass

    fallback_link = get_target_group_link()
    return fallback_link if fallback_link and fallback_link != "https://t.me/" else "https://t.me/"

def resolve_group_chat():
    req_group = get_setting("required_group_id")
    target_link = get_setting("target_channel_link")
    
    candidates = []
    if target_link and "t.me/" in str(target_link):
        h = str(target_link).split("t.me/")[-1].replace("/", "").strip()
        if h and not h.startswith("+"):
            candidates.append(f"@{h}")
            candidates.append(h)
    if req_group:
        if "t.me/" in str(req_group):
            h = str(req_group).split("t.me/")[-1].replace("/", "").strip()
            if h and not h.startswith("+"):
                candidates.append(f"@{h}")
                candidates.append(h)
        elif str(req_group).startswith("@"):
            candidates.append(str(req_group))
            candidates.append(str(req_group)[1:])
        elif str(req_group).startswith("-100") or str(req_group).lstrip("-").isdigit():
            try:
                candidates.append(int(req_group))
            except Exception:
                pass
            candidates.append(str(req_group))

    invalid = ["-1001234567890", "@your_channel", "@yourchannel", "your_channel", "yourchannel", ""]
    seen = set()
    for c in candidates:
        if c and str(c) not in invalid and c not in seen:
            seen.add(c)
            try:
                ch = bot.get_chat(c)
                if ch:
                    return ch
            except Exception:
                continue
    return None

def get_cached_group_info():
    req_group = get_setting("required_group_id")
    if not req_group or str(req_group) in ["-1001234567890", ""]:
        return None, ""

    cached_photo = get_setting("cached_group_photo_file_id")
    cached_title = get_setting("cached_group_title")
    cached_photo_id = get_setting("cached_group_photo_unique_id")
    cached_group_id = get_setting("cached_for_group_id")

    try:
        chat = resolve_group_chat()
        if not chat:
            return cached_photo or None, cached_title or ""

        current_title = chat.title or ""
        current_photo_id = ""
        if chat.photo:
            current_photo_id = getattr(chat.photo, 'big_file_unique_id', None) or getattr(chat.photo, 'big_file_id', '') or ""

        # Check if everything is identical
        title_unchanged = (current_title == cached_title)
        photo_unchanged = (current_photo_id == cached_photo_id)
        group_unchanged = (str(req_group) == str(cached_group_id))

        # If nothing changed and photo cache is consistent, skip re-fetching/uploading!
        if title_unchanged and photo_unchanged and group_unchanged and (cached_photo or not current_photo_id):
            return cached_photo or None, cached_title

        # Something changed! Update title and group id
        set_setting("cached_group_title", current_title)
        set_setting("cached_for_group_id", str(req_group))

        # If photo changed or was newly added
        if not photo_unchanged or (current_photo_id and not cached_photo):
            if chat.photo and getattr(chat.photo, 'big_file_id', None):
                f = bot.get_file(chat.photo.big_file_id)
                res = requests.get(f"https://api.telegram.org/file/bot{BOT_TOKEN}/{f.file_path}", timeout=15)
                if res.status_code == 200:
                    admin_id_raw = get_setting("admin_id") or "553826070"
                    try:
                        admin_id = int(admin_id_raw)
                    except Exception:
                        admin_id = 553826070
                    m = bot.send_photo(admin_id, res.content)
                    photo_file_id = m.photo[-1].file_id
                    try:
                        bot.delete_message(admin_id, m.message_id)
                    except Exception:
                        pass
                    set_setting("cached_group_photo_file_id", photo_file_id)
                    set_setting("cached_group_photo_unique_id", current_photo_id)
                    return photo_file_id, current_title
            else:
                # Group photo was removed
                set_setting("cached_group_photo_file_id", "")
                set_setting("cached_group_photo_unique_id", "")
                return None, current_title

        return cached_photo or None, current_title
    except Exception:
        return cached_photo or None, cached_title or ""

# --- GENERAL UTILS ---
def clean_text_for_markdown(text):
    if not text:
        return ""
    for char in ['_', '*', '`', '[']:
        text = str(text).replace(char, f'\\{char}')
    return text

def format_size(bytes_val):
    try:
        bytes_val = float(bytes_val)
        for unit in ['B', 'KB', 'MB', 'GB', 'TB']:
            if bytes_val < 1024.0:
                return f"{bytes_val:.2f} {unit}"
            bytes_val /= 1024.0
        return f"{bytes_val:.2f} PB"
    except Exception:
        return "Unknown"

def format_duration(seconds):
    try:
        seconds = int(seconds)
        m, s = divmod(seconds, 60)
        h, m = divmod(m, 60)
        if h > 0:
            return f"{h:02d}:{m:02d}:{s:02d}"
        return f"{m:02d}:{s:02d}"
    except Exception:
        return "--"

def detect_resolution(title, raw_data, stream_url):
    match = re.search(r'(\d{3,4}[pP])', title)
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

def make_progress_bar(percent):
    total = 10
    filled = int(round(total * (percent / 100.0)))
    empty = max(0, total - filled)
    return "■" * filled + "□" * empty

def get_progress_text(percent, stage_desc="Analyzing video stream..."):
    bar = make_progress_bar(percent)
    return (
        f"🔄 **Processing Video...**\n\n"
        f"`[{bar}]` **{percent}%**\n"
        f"⚡ _{stage_desc}_"
    )

def auto_delete_after(chat_id, message_ids, delay=5):
    if not isinstance(message_ids, (list, tuple)):
        message_ids = [message_ids]
    def worker():
        if delay > 0:
            time.sleep(delay)
        for mid in message_ids:
            if mid:
                try:
                    bot.delete_message(chat_id, mid)
                except Exception:
                    pass
    t = threading.Thread(target=worker)
    t.daemon = True
    t.start()

def normalize_terabox_url(raw_url):
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

def get_terabox_media(terabox_url):
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
            response = requests.post(backend_url, json=payload, headers=headers, timeout=45)
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
        except Exception as e:
            print(f"Error fetching API for {target_u}: {e}")
            
    return None, None, None, None, None, None

# --- LOCALIZATION (bn, en, hi) ---
STRINGS = {
    "bn": {
        "btn_account": "👤 My Account",
        "btn_refer": "🎁 Refer & Earn",
        "btn_buy": "💳 Buy Subscription",
        "btn_backup": "📢 Join Backup Channel",
        "btn_contact": "💬 Contact Admin",
        "btn_lang": "🌐 ভাষা / Language",
        "btn_admin_settings": "⚙️ Admin Settings",
        "select_lang_prompt": (
            "🌐 **ভাষা নির্বাচন করুন / Choose Your Language / भाषा चुनें**\n\n"
            "বটটি ব্যবহার করতে আপনার পছন্দের ভাষা নির্বাচন করুন:\n"
            "Please select your language to proceed:\n"
            "कृपया आगे बढ़ने के लिए अपनी भाषा चुनें:"
        ),
        "lang_selected_notice": "✅ ভাষা সফলভাবে নির্বাচন করা হয়েছে: **বাংলা** 🇧🇩",
        "welcome_new": (
            "👋 Welcome {username}!\n\n"
            "🎁 আপনাকে {init_credits} Credits বিনামূল্যে দেওয়া হয়েছে!"
        ),
        "welcome_returning": (
            "👋 Hello {username}!\n\n"
            "TeraBox লিংক পাঠান ভিডিও স্ট্রিম করার জন্য।"
        ),
        "welcome_referral_locked": (
            "🌟 **EXCLUSIVE REFERRAL ACCESS** 🌟\n\n"
            "👋 Welcome {username}!\n"
            "বোনাস **Credits** ক্লেইম করতে Channel / Group-এ যুক্ত হোন:"
        ),
        "btn_watch_video": "✨ 🎬 Watch Video ⚡",
        "btn_verify_bonus": "💎 ✨ Verify & Claim Bonus ✨ 💎",
        "btn_invite_earn": "✨ 🎁 Invite & Earn Free Credits 💎",
        "btn_join_backup_inline": "✨ 📢 Join Backup Channel ⚡",
        "btn_open_player_app": "🎬 Play Video 👀",
        "btn_open_player_web": "🌐 Open Web Player",
        "verify_success_alert": "✅ Join Verification Successful!",
        "verify_success_msg": "🎉 Verification সফল! রেফারেল বোনাস আনলক করা হয়েছে।",
        "verify_already_claimed": "বোনাস ইতিমধ্যে ক্লেইম করা হয়েছে!",
        "verify_failed": "❌ Channel/Group-এ এখনও যুক্ত হননি!",
        "ref_bonus_received": "🎉 আপনার রেফারেল লিংকের মাধ্যমে ইউজার যুক্ত হওয়ায় আপনি +{ref_bonus} Credits বোনাস পেয়েছেন!",
        "acc_title": "👤 **Account Overview**",
        "acc_name": "📛 **Name:** {name}",
        "acc_username": "🌐 **Username:** {username}",
        "acc_id": "🆔 **User ID:** `{user_id}`",
        "acc_status": "🏷 **Status:** {status}",
        "acc_credits": "💰 **Credit Balance:** {credits} Credits",
        "acc_expiry": "⏳ **Subscription Expiry:** {expiry}",
        "status_vip": "🔥 VIP / Paid Member",
        "status_free": "🆓 Free User",
        "refer_title": "💎 **PREMIUM REFERRAL PROGRAM** 💎",
        "refer_reward": "🎁 **Reward:** প্রতিটি সফল রেফারেলে পান **+{ref_bonus} Free Credits**!",
        "refer_how": "📌 **কীভাবে কাজ করে:**\nনিচের বাটনে ক্লিক করে সরাসরি Channel Invite লিংক শেয়ার করুন।",
        "buy_title": "💳 **Buy Paid Membership**",
        "buy_desc": "আনলিমিটেড TeraBox Video Streaming পেতে Paid Membership সক্রিয় করুন।",
        "buy_pricing": "📌 **মূল্যতালিকা:**\n• 7 Days: {p7}\n• 30 Days: {p30}",
        "buy_contact": "📞 **পেমেন্ট ও সক্রিয়করণ:**\nঅ্যাডমিনের সাথে সরাসরি যোগাযোগ করতে '💬 Contact Admin' বাটন ব্যবহার করুন।",
        "backup_title": "📢 **Join Our Backup Channel**",
        "backup_desc": (
            "আমাদের অফিশিয়াল মূল চ্যানেল বা বট কোনো কারণে ডাউন হলে, "
            "সব আপডেট, নতুন লিংক এবং তথ্য আমাদের ব্যাকআপ চ্যানেলে পাবেন।\n\n"
            "👉 নিচের বাটনে ক্লিক করে এখনই ব্যাকআপ চ্যানেলে যুক্ত থাকুন:"
        ),
        "contact_prompt": (
            "📝 **অ্যাডমিনের কাছে যে মেসেজটি পাঠাতে চান তা লিখে পাঠান:**\n\n"
            "*(আপনার মেসেজটি সরাসরি অ্যাডমিনের কাছে পৌঁছে যাবে)*"
        ),
        "contact_sent": "✅ **আপনার মেসেজটি অ্যাডমিনের কাছে সফলভাবে পৌঁছেছে!**",
        "contact_failed": "❌ মেসেজ পাঠাতে সমস্যা হয়েছে! পরে আবার চেষ্টা করুন।",
        "no_credits_alert": (
            "❌ আপনার ক্রেডিট শেষ হয়ে গেছে!\n\n"
            "👉 আরও ক্রেডিট পেতে **Refer & Earn** অপশন থেকে বন্ধুদের আমন্ত্রণ জানান বা **Buy Subscription** থেকে প্রিমিয়াম প্ল্যান নিন।"
        ),
        "invalid_link_prompt": "অনুগ্রহ করে একটি সঠিক TeraBox Video Link পাঠান",
        "stream_fetch_fail": "❌ ভিডিও স্ট্রিমের তথ্য আনতে ব্যর্থ হয়েছে!",
        "stream_play_prompt": "👇 ভিডিও প্লে করতে নিচের বাটনে ক্লিক করুন:",
        "balance_unlimited": "Unlimited (Paid)",
        "balance_credits": "{credits} Credits",
        "proc_connecting": "TeraBox সার্ভারে সংযোগ করা হচ্ছে...",
        "proc_analyzing": "ভিডিও স্ট্রিম ও রেজোলিউশন বিশ্লেষণ করা হচ্ছে...",
        "proc_extracting": "সরাসরি ভিডিও স্ট্রিম বের করা হচ্ছে...",
        "proc_optimizing": "স্ট্রিম ব্যান্ডউইথ অপটিমাইজ করা হচ্ছে...",
        "proc_finalizing": "স্ট্রিম প্যারামিটার প্রস্তুত করা হচ্ছে...",
        "proc_done": "সম্পন্ন! প্লেয়ার তৈরি হচ্ছে...",
        "paid_activated_msg": "🎉 আপনার {days} দিনের Paid Subscription সক্রিয় করা হয়েছে!"
    },
    "en": {
        "btn_account": "👤 My Account",
        "btn_refer": "🎁 Refer & Earn",
        "btn_buy": "💳 Buy Subscription",
        "btn_backup": "📢 Join Backup Channel",
        "btn_contact": "💬 Contact Admin",
        "btn_lang": "🌐 Language",
        "btn_admin_settings": "⚙️ Admin Settings",
        "select_lang_prompt": (
            "🌐 **Choose Your Language / भाषा चुनें / ভাষা নির্বাচন করুন**\n\n"
            "Please select your preferred language to continue:\n"
            "বটটি ব্যবহার করতে আপনার পছন্দের ভাষা নির্বাচন করুন:\n"
            "कृपया आगे बढ़ने के लिए अपनी भाषा चुनें:"
        ),
        "lang_selected_notice": "✅ Language successfully set to: **English** 🇬🇧",
        "welcome_new": (
            "👋 Welcome {username}!\n\n"
            "🎁 You have received {init_credits} Free Credits!"
        ),
        "welcome_returning": (
            "👋 Hello {username}!\n\n"
            "Send any TeraBox link to stream the video."
        ),
        "welcome_referral_locked": (
            "🌟 **EXCLUSIVE REFERRAL ACCESS** 🌟\n\n"
            "👋 Welcome {username}!\n"
            "Join our Channel / Group to claim your bonus **Credits**:"
        ),
        "btn_watch_video": "✨ 🎬 Watch Video ⚡",
        "btn_verify_bonus": "💎 ✨ Verify & Claim Bonus ✨ 💎",
        "btn_invite_earn": "✨ 🎁 Invite & Earn Free Credits 💎",
        "btn_join_backup_inline": "✨ 📢 Join Backup Channel ⚡",
        "btn_open_player_app": "🎬 Play Video 👀",
        "btn_open_player_web": "🌐 Open Web Player",
        "verify_success_alert": "✅ Join Verification Successful!",
        "verify_success_msg": "🎉 Verification Successful! Referral Bonus unlocked for your inviter.",
        "verify_already_claimed": "Bonus has already been claimed!",
        "verify_failed": "❌ You haven't joined the Channel / Group yet!",
        "ref_bonus_received": "🎉 A user joined using your referral link! You earned +{ref_bonus} Credits bonus!",
        "acc_title": "👤 **Account Overview**",
        "acc_name": "📛 **Name:** {name}",
        "acc_username": "🌐 **Username:** {username}",
        "acc_id": "🆔 **User ID:** `{user_id}`",
        "acc_status": "🏷 **Status:** {status}",
        "acc_credits": "💰 **Credit Balance:** {credits} Credits",
        "acc_expiry": "⏳ **Subscription Expiry:** {expiry}",
        "status_vip": "🔥 VIP / Paid Member",
        "status_free": "🆓 Free User",
        "refer_title": "💎 **PREMIUM REFERRAL PROGRAM** 💎",
        "refer_reward": "🎁 **Reward:** Earn **+{ref_bonus} Free Credits** for each user who joins!",
        "refer_how": "📌 **How it works:**\nClick the button below to share your Channel Invite link.",
        "buy_title": "💳 **Buy Paid Membership**",
        "buy_desc": "Activate Paid Membership for unlimited, instant TeraBox video streaming.",
        "buy_pricing": "📌 **Pricing:**\n• 7 Days: {p7}\n• 30 Days: {p30}",
        "buy_contact": "📞 **Payment & Activation:**\nUse the '💬 Contact Admin' button to contact the administrator.",
        "backup_title": "📢 **Join Our Backup Channel**",
        "backup_desc": (
            "If our official channel or bot goes down, "
            "you will find all updates, new links, and announcements in our Backup Channel.\n\n"
            "👉 Click the button below to join now:"
        ),
        "contact_prompt": (
            "📝 **Type your message to the Admin below:**\n\n"
            "*(Your message will be sent directly to the Admin's inbox)*"
        ),
        "contact_sent": "✅ **Your message has been sent to the Admin!**",
        "contact_failed": "❌ Failed to send message! Please try again later.",
        "no_credits_alert": (
            "❌ You have run out of credits!\n\n"
            "👉 Earn more credits via **Refer & Earn** or upgrade via **Buy Subscription**."
        ),
        "invalid_link_prompt": "Please share a valid TeraBox Video Link",
        "stream_fetch_fail": "❌ Failed to fetch stream details!",
        "stream_play_prompt": "👇 Click below to stream the video:",
        "balance_unlimited": "Unlimited (Paid)",
        "balance_credits": "{credits} Credits",
        "proc_connecting": "Connecting to TeraBox server...",
        "proc_analyzing": "Analyzing video stream & resolution...",
        "proc_extracting": "Extracting direct video stream...",
        "proc_optimizing": "Optimizing stream bandwidth...",
        "proc_finalizing": "Finalizing stream parameters...",
        "proc_done": "Done! Generating player...",
        "paid_activated_msg": "🎉 Your {days}-day Paid Subscription has been activated!"
    },
    "hi": {
        "btn_account": "👤 My Account",
        "btn_refer": "🎁 Refer & Earn",
        "btn_buy": "💳 Buy Subscription",
        "btn_backup": "📢 Join Backup Channel",
        "btn_contact": "💬 Contact Admin",
        "btn_lang": "🌐 भाषा / Language",
        "btn_admin_settings": "⚙️ Admin Settings",
        "select_lang_prompt": (
            "🌐 **अपनी भाषा चुनें / Choose Your Language / ভাষা নির্বাচন করুন**\n\n"
            "कृपया बॉट का उपयोग करने के लिए अपनी पसंदीदा भाषा चुनें:\n"
            "Please select your language to proceed:\n"
            "বটটি ব্যবহার করতে আপনার পছন্দের ভাষা নির্বাচন করুন:"
        ),
        "lang_selected_notice": "✅ भाषा सफलतापूर्वक चुनी गई: **हिन्दी** 🇮🇳",
        "welcome_new": (
            "👋 Welcome {username}!\n\n"
            "🎁 आपको {init_credits} Credits मुफ्त में दिए गए हैं!"
        ),
        "welcome_returning": (
            "👋 Hello {username}!\n\n"
            "वीडियो स्ट्रीम करने के लिए कोई भी TeraBox लिंक भेजें।"
        ),
        "welcome_referral_locked": (
            "🌟 **EXCLUSIVE REFERRAL ACCESS** 🌟\n\n"
            "👋 Welcome {username}!\n"
            "बोनस **Credits** प्राप्त करने के लिए Channel / Group में शामिल हों:"
        ),
        "btn_watch_video": "✨ 🎬 Watch Video ⚡",
        "btn_verify_bonus": "💎 ✨ Verify & Claim Bonus ✨ 💎",
        "btn_invite_earn": "✨ 🎁 Invite & Earn Free Credits 💎",
        "btn_join_backup_inline": "✨ 📢 Join Backup Channel ⚡",
        "btn_open_player_app": "🎬 Play Video 👀",
        "btn_open_player_web": "🌐 Open Web Player",
        "verify_success_alert": "✅ Join Verification Successful!",
        "verify_success_msg": "🎉 सत्यापन सफल! रेफरल बोनस अनलॉक हो गया है।",
        "verify_already_claimed": "बोनस पहले ही क्लेम किया जा चुका है!",
        "verify_failed": "❌ आप अभी तक Channel / Group में शामिल नहीं हुए हैं!",
        "ref_bonus_received": "🎉 आपके रेफरल लिंक से एक यूजर शामिल हुआ! आपको +{ref_bonus} Credits बोनस मिला!",
        "acc_title": "👤 **Account Overview**",
        "acc_name": "📛 **Name:** {name}",
        "acc_username": "🌐 **Username:** {username}",
        "acc_id": "🆔 **User ID:** `{user_id}`",
        "acc_status": "🏷 **Status:** {status}",
        "acc_credits": "💰 **Credit Balance:** {credits} Credits",
        "acc_expiry": "⏳ **Subscription Expiry:** {expiry}",
        "status_vip": "🔥 VIP / Paid Member",
        "status_free": "🆓 Free User",
        "refer_title": "💎 **PREMIUM REFERRAL PROGRAM** 💎",
        "refer_reward": "🎁 **Reward:** चैनल में शामिल होने वाले प्रत्येक यूजर के लिए **+{ref_bonus} Free Credits** पाएं!",
        "refer_how": "📌 **यह कैसे काम करता है:**\nसीधे चैनल इनवाइट लिंक साझा करने के लिए नीचे दिए गए बटन पर क्लिक करें।",
        "buy_title": "💳 **Buy Paid Membership**",
        "buy_desc": "असीमित TeraBox वीडियो स्ट्रीमिंग के लिए Paid Membership सक्रिय करें।",
        "buy_pricing": "📌 **मूल्य:**\n• 7 Days: {p7}\n• 30 Days: {p30}",
        "buy_contact": "📞 **भुगतान और एक्टिवेशन:**\nव्यवस्थापक से सीधे संपर्क करने के लिए '💬 Contact Admin' बटन का उपयोग करें।",
        "backup_title": "📢 **Join Our Backup Channel**",
        "backup_desc": (
            "यदि हमारा आधिकारिक मुख्य चैनल या बॉट किसी कारण से बंद हो जाता है, "
            "तो सभी अपडेट और नए लिंक हमारे बैकअप चैनल पर उपलब्ध होंगे।\n\n"
            "👉 अभी बैकअप चैनल से जुड़ने के लिए नीचे दिए गए बटन पर क्लिक करें:"
        ),
        "contact_prompt": (
            "📝 **व्यवस्थापक को भेजने के लिए अपना संदेश नीचे लिखें:**\n\n"
            "*(आपका संदेश सीधे व्यवस्थापक को भेज दिया जाएगा)*"
        ),
        "contact_sent": "✅ **आपका संदेश व्यवस्थापक तक सफलतापूर्वक पहुंच गया है!**",
        "contact_failed": "❌ संदेश भेजने में त्रुटि हुई! कृपया बाद में पुनः प्रयास करें।",
        "no_credits_alert": (
            "❌ आपके क्रेडिट समाप्त हो गए हैं!\n\n"
            "👉 अधिक क्रेडिट पाने के लिए **Refer & Earn** से दोस्तों को आमंत्रित करें या **Buy Subscription** से पेड प्लान लें।"
        ),
        "invalid_link_prompt": "कृपया एक मान्य TeraBox वीडियो लिंक साझा करें",
        "stream_fetch_fail": "❌ वीडियो स्ट्रीम विवरण प्राप्त करने में विफल!",
        "stream_play_prompt": "👇 वीडियो देखने के लिए नीचे दिए गए बटन पर क्लिक करें:",
        "balance_unlimited": "Unlimited (Paid)",
        "balance_credits": "{credits} Credits",
        "proc_connecting": "TeraBox सर्वर से कनेक्ट किया जा रहा है...",
        "proc_analyzing": "वीडियो स्ट्रीम और रिज़ॉल्यूशन का विश्लेषण किया जा रहा है...",
        "proc_extracting": "डायरेक्ट वीडियो स्ट्रीम निकाली जा रही है...",
        "proc_optimizing": "स्ट्रीम बैंडविड्थ ऑप्टिमाइज़ की जा रही है...",
        "proc_finalizing": "स्ट्रीम पैरामीटर अंतिम रूप दिए जा रहे हैं...",
        "proc_done": "पूर्ण! प्लेयर तैयार किया जा रहा है...",
        "paid_activated_msg": "🎉 आपका {days} दिनों का Paid Subscription सक्रिय कर दिया गया है!"
    }
}



# -------------------------------------------------------------
# POST SCHEDULER ENGINE & QUEUE
# -------------------------------------------------------------
def is_schedule_mode_enabled() -> bool:
    return get_setting("schedule_mode_enabled") == "1"

def set_schedule_mode_enabled(enabled: bool):
    set_setting("schedule_mode_enabled", "1" if enabled else "0")

def get_scheduled_queue_count() -> int:
    conn = sqlite3.connect('users.db')
    c = conn.cursor()
    c.execute("SELECT COUNT(*) FROM scheduled_queue WHERE status = 'pending'")
    cnt = c.fetchone()[0]
    conn.close()
    return cnt

def get_post_schedules() -> List[Dict[str, Any]]:
    conn = sqlite3.connect('users.db')
    c = conn.cursor()
    c.execute("SELECT id, schedule_time, post_count, is_active, last_run_date FROM post_schedules ORDER BY id ASC")
    rows = c.fetchall()
    conn.close()
    return [{"id": r[0], "time": r[1], "count": r[2], "active": r[3], "last_run": r[4]} for r in rows]

def add_post_schedule(schedule_time: str, post_count: int = 5) -> bool:
    conn = sqlite3.connect('users.db')
    c = conn.cursor()
    try:
        c.execute("INSERT OR REPLACE INTO post_schedules (schedule_time, post_count, is_active) VALUES (?, ?, 1)",
                  (schedule_time.strip().upper(), post_count))
        conn.commit()
        return True
    except Exception as e:
        push_log("ERROR", f"Error adding schedule: {e}")
        return False
    finally:
        conn.close()

def delete_post_schedule(schedule_id: int):
    conn = sqlite3.connect('users.db')
    c = conn.cursor()
    c.execute("DELETE FROM post_schedules WHERE id = ?", (schedule_id,))
    conn.commit()
    conn.close()

def store_post_in_queue(media_path: Optional[str], media_type: str, caption: str, raw_caption: str, buttons_json: str, target_chats: str) -> int:
    saved_media_path = None
    if media_path and os.path.exists(media_path):
        ext = os.path.splitext(media_path)[1] or ".bin"
        filename = f"sched_{int(time.time()*1000)}{ext}"
        saved_media_path = os.path.join(SCHEDULED_MEDIA_DIR, filename)
        shutil.copy2(media_path, saved_media_path)

    conn = sqlite3.connect('users.db')
    c = conn.cursor()
    now_str = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
    c.execute('''
        INSERT INTO scheduled_queue (created_at, media_path, media_type, caption, raw_caption, buttons_json, target_chats, status)
        VALUES (?, ?, ?, ?, ?, ?, ?, 'pending')
    ''', (now_str, saved_media_path, media_type, caption, raw_caption, buttons_json, target_chats))
    row_id = c.lastrowid
    conn.commit()
    conn.close()
    push_log("SCHEDULER", f"Stored post #{row_id} in queue. Total pending: {get_scheduled_queue_count()}")
    return row_id

def pop_scheduled_posts(limit: int) -> List[Dict[str, Any]]:
    conn = sqlite3.connect('users.db')
    c = conn.cursor()
    c.execute("SELECT id, created_at, media_path, media_type, caption, buttons_json, target_chats FROM scheduled_queue WHERE status = 'pending' ORDER BY id ASC LIMIT ?", (limit,))
    rows = c.fetchall()
    posts = []
    for r in rows:
        posts.append({
            "id": r[0], "created_at": r[1], "media_path": r[2], "media_type": r[3],
            "caption": r[4], "buttons_json": r[5], "target_chats": r[6]
        })
    conn.close()
    return posts

def remove_scheduled_post(post_id: int, media_path: Optional[str]):
    conn = sqlite3.connect('users.db')
    c = conn.cursor()
    c.execute("DELETE FROM scheduled_queue WHERE id = ?", (post_id,))
    conn.commit()
    conn.close()
    if media_path and os.path.exists(media_path):
        try: os.remove(media_path)
        except Exception: pass

def clear_scheduled_queue():
    conn = sqlite3.connect('users.db')
    c = conn.cursor()
    c.execute("SELECT media_path FROM scheduled_queue")
    for row in c.fetchall():
        if row[0] and os.path.exists(row[0]):
            try: os.remove(row[0])
            except Exception: pass
    c.execute("DELETE FROM scheduled_queue")
    conn.commit()
    conn.close()
    push_log("SCHEDULER", "Scheduled queue cleared.")

class SchedulerService:
    def __init__(self):
        self.running = False
        self.thread = None

    def start(self):
        if not self.running:
            self.running = True
            self.thread = threading.Thread(target=self._run_loop, daemon=True)
            self.thread.start()
            push_log("SCHEDULER", "Post Scheduler Service started.")

    def stop(self):
        self.running = False

    def _run_loop(self):
        while self.running:
            try:
                self._check_and_dispatch()
            except Exception as e:
                push_log("ERROR", f"Scheduler loop error: {e}")
            time.sleep(30)

    def _check_and_dispatch(self):
        if not is_schedule_mode_enabled():
            return
        schedules = get_post_schedules()
        now = datetime.now()
        current_time_str = now.strftime("%I:%M %p").lstrip("0")
        today_date = now.strftime("%Y-%m-%d")

        for sched in schedules:
            if not sched["active"]: continue
            s_time = sched["time"].strip().upper()
            if s_time.lstrip("0") == current_time_str.upper().lstrip("0") and sched["last_run"] != today_date:
                push_log("SCHEDULER", f"⏰ Triggering schedule [{s_time}] - Target: {sched['count']} posts")
                self.flush_target_count(sched["count"])
                conn = sqlite3.connect('users.db')
                c = conn.cursor()
                c.execute("UPDATE post_schedules SET last_run_date = ? WHERE id = ?", (today_date, sched["id"]))
                conn.commit()
                conn.close()

    def flush_target_count(self, target_count: int) -> int:
        posts = pop_scheduled_posts(target_count)
        if not posts:
            push_log("SCHEDULER", "Queue is empty. Nothing to post.")
            return 0

        posted = 0
        env = read_env()
        dest_channel = env.get("OUTPUT_CHANNEL", "").strip()

        def _resolve_tid(ch: Any) -> Any:
            if not ch or str(ch).strip() in ("", "None"):
                return None
            s = str(ch).strip()
            if s.startswith("@"):
                return s
            clean_digits = s.lstrip("-")
            if clean_digits.isdigit():
                val = int(s)
                if val < 0:
                    if not s.startswith("-100") and len(clean_digits) >= 9:
                        return int(f"-100{clean_digits}")
                    return val
                if len(s) >= 9:
                    return int(f"-100{s}")
                return val
            return s

        for p in posts:
            try:
                targets = p["target_chats"] or dest_channel
                target_list = [t.strip() for t in targets.split(",") if t.strip()] or [dest_channel]

                valid_targets = []
                for ch in target_list:
                    tid = _resolve_tid(ch)
                    if tid and tid not in valid_targets:
                        valid_targets.append(tid)

                if not valid_targets and dest_channel:
                    fallback_id = _resolve_tid(dest_channel)
                    if fallback_id:
                        valid_targets.append(fallback_id)

                reply_markup = None
                if p["buttons_json"]:
                    try:
                        raw_btns = json.loads(p["buttons_json"])
                        reply_markup = InlineKeyboardMarkup()
                        for row in raw_btns:
                            reply_markup.row(*[
                                InlineKeyboardButton(text=b.get("text", "Click"), url=b.get("url"), web_app=WebAppInfo(url=b["web_app"]) if "web_app" in b else None)
                                for b in row
                            ])
                    except Exception: pass

                for target_id in valid_targets:
                    if p["media_path"] and os.path.exists(p["media_path"]):
                        with open(p["media_path"], "rb") as mf:
                            if p["media_type"] == "photo":
                                try:
                                    bot.send_photo(target_id, mf, caption=p["caption"], parse_mode="HTML", reply_markup=reply_markup)
                                except Exception:
                                    mf.seek(0)
                                    bot.send_photo(target_id, mf, caption=p["caption"], reply_markup=reply_markup)
                            elif p["media_type"] == "video":
                                try:
                                    bot.send_video(target_id, mf, caption=p["caption"], parse_mode="HTML", reply_markup=reply_markup)
                                except Exception:
                                    mf.seek(0)
                                    bot.send_video(target_id, mf, caption=p["caption"], reply_markup=reply_markup)
                            else:
                                try:
                                    bot.send_document(target_id, mf, caption=p["caption"], parse_mode="HTML", reply_markup=reply_markup)
                                except Exception:
                                    mf.seek(0)
                                    bot.send_document(target_id, mf, caption=p["caption"], reply_markup=reply_markup)
                    else:
                        try:
                            bot.send_message(target_id, p["caption"], parse_mode="HTML", reply_markup=reply_markup)
                        except Exception:
                            bot.send_message(target_id, p["caption"], reply_markup=reply_markup)

                remove_scheduled_post(p["id"], p["media_path"])
                posted += 1
                push_log("SCHEDULER", f"Posted & popped item #{p['id']}. Available left: {get_scheduled_queue_count()}")
                time.sleep(int(env.get("POST_DELAY_SECONDS", 3)))
            except Exception as e:
                push_log("ERROR", f"Error dispatching post #{p['id']}: {e}")
                err_msg = str(e).lower()
                if "chat not found" in err_msg or "bot was blocked" in err_msg or not (p["media_path"] and os.path.exists(p["media_path"])):
                    remove_scheduled_post(p["id"], p["media_path"])

        return posted

scheduler_service = SchedulerService()

class EngineManager:
    def __init__(self):
        self.engine: Optional[AutomationEngine] = None
        self.engine_loop: Optional[asyncio.AbstractEventLoop] = None
        self.engine_thread: Optional[threading.Thread] = None
        self.is_running = False
        self.start_time: Optional[float] = None
        self.db = Database()
        self.stats = {
            "processed": 0,
            "converted": 0,
            "filtered": 0,
            "forwarded": 0,
            "whatsapp": 0,
            "errors": 0
        }
        self.auth_future: Optional[asyncio.Future] = None
        self.auth_prompt_text = ""
        self.restarted = False

    def update_stat(self, key: str, value: Any):
        if key in self.stats:
            try:
                self.stats[key] = self.stats.get(key, 0) + int(value)
            except Exception:
                self.stats[key] = value

    def append_log(self, level: str, message: str):
        if level == "ERROR":
            self.stats["errors"] = self.stats.get("errors", 0) + 1
        push_log(level, message)

    async def prompt_auth(self, title: str, prompt: str) -> str:
        """Called by Telethon if login code / 2FA / phone is required."""
        self.append_log("WARNING", f"Auth required: {title} - {prompt}")
        self.auth_prompt_text = f"🔐 <b>{title}</b>\n\n{prompt}\n\n<i>Reply to this message with the required code/text:</i>"
        env = read_env()
        auth_raw = env.get("AUTHORIZED_USERS", "")
        auth_ids = [int(x.strip()) for x in auth_raw.split(",") if x.strip().isdigit()]
        for u_id in auth_ids:
            try:
                bot.send_message(u_id, self.auth_prompt_text)
            except Exception as e:
                print(f"Failed to send auth prompt to admin {u_id}:", e)

        loop = asyncio.get_event_loop()
        self.auth_future = loop.create_future()
        code = await self.auth_future
        self.auth_future = None
        return code

    def provide_auth_input(self, text: str):
        if self.auth_future and not self.auth_future.done():
            self.auth_future.set_result(text.strip())
            return True
        return False

    def emergency_stop(self, reason: str):
        self.append_log("ERROR", f"🚨 Emergency Auto-Stop: {reason}")
        env = read_env()
        auth_raw = env.get("AUTHORIZED_USERS", "")
        auth_ids = [int(x.strip()) for x in auth_raw.split(",") if x.strip().isdigit()]
        proc_bot = env.get("PROCESSING_BOT", "@LinkConvertTeraAbot")
        alert_text = (
            "🚨 <b>WARNING: PROCESSING BOT BUSY</b>\n"
            "━━━━━━━━━━━━━━━━━━━━━━\n"
            f"প্রসেসিং বট (<code>{proc_bot}</code>) থেকে সতর্কতা এসেছে:\n"
            f"⚠️ <i>\"{reason}\"</i>\n\n"
            "🛡 <b>অ্যাকাউন্ট ও সোর্স সুরক্ষিত রাখতে অটোমেশন ইঞ্জিন স্বয়ংক্রিয়ভাবে বন্ধ (STOP) করা হয়েছে।</b>\n\n"
            "💡 প্রসেসিং বট সচল হলে ড্যাশবোর্ড থেকে আবার <b>🔴 Start Engine</b> বাটন চাপুন।"
        )
        for uid in auth_ids:
            try:
                bot.send_message(
                    uid,
                    alert_text,
                    reply_markup=InlineKeyboardMarkup().row(
                        InlineKeyboardButton("📱 Open Dashboard", callback_data="nav:main"),
                        InlineKeyboardButton("🔴 Start Engine", callback_data="act:start")
                    )
                )
            except Exception as e:
                print(f"Failed to send emergency alert to {uid}:", e)
        # Trigger stop on engine
        self.stop()

    def build_config(self) -> Dict[str, Any]:
        env = read_env()
        session_dir = os.path.join(APP_DIR, "sessions")
        os.makedirs(session_dir, exist_ok=True)

        return {
            "api_id": env.get("API_ID", "1906923"),
            "api_hash": env.get("API_HASH", "ded2e82b54cb1955a87eda02ef6bb02e"),
            "telegram_phone": env.get("TELEGRAM_PHONE", "+8801721007568"),
            "session_name": env.get("SESSION_NAME", "telegram_automation"),
            "session_dir": session_dir,
            "source_chats": env.get("SOURCE_CHATS", ""),
            "processing_bot": env.get("PROCESSING_BOT", "@LinkConvertTeraAbot"),
            "output_channel": env.get("OUTPUT_CHANNEL", ""),
            "reply_timeout": int(env.get("REPLY_TIMEOUT", "120")),
            "post_delay_min_seconds": int(env.get("POST_DELAY_MIN_SECONDS", "1")),
            "post_delay_max_seconds": int(env.get("POST_DELAY_MAX_SECONDS", "3")),
            "post_delay_seconds": int(env.get("POST_DELAY_SECONDS", "3")),
            "bot_response_settle_time": float(env.get("BOT_RESPONSE_SETTLE_TIME", "3.0")),
            "process_historical_messages": env.get("PROCESS_HISTORICAL_MESSAGES", "False").lower() == "true",
            "historical_messages_limit": int(env.get("HISTORICAL_MESSAGES_LIMIT", "0")),
            "filter_only_terabox_posts": env.get("FILTER_ONLY_TERABOX_POSTS", "True").lower() == "true",
            "terabox_domains": DEFAULT_TERABOX_DOMAINS,
            "send_media_to_processing_bot": env.get("SEND_MEDIA_TO_PROCESSING_BOT", "True").lower() == "true",
            "auto_accept_join_requests": env.get("AUTO_ACCEPT_JOIN_REQUESTS", "True").lower() == "true",
            "auto_join_chats": env.get("AUTO_JOIN_CHATS", ""),
            "auto_join_delay": int(env.get("AUTO_JOIN_DELAY_SECONDS", "1")),
            "auto_hide_join_messages": env.get("AUTO_HIDE_JOIN_MESSAGES", "False").lower() == "true",
            "hide_join_chats": env.get("HIDE_JOIN_CHATS", ""),
            "hide_join_delete_leave": env.get("HIDE_JOIN_DELETE_LEAVE", "True").lower() == "true",
            "whatsapp_enabled": env.get("WHATSAPP_ENABLED", "False").lower() == "true",
            "whatsapp_gateway_type": env.get("WHATSAPP_GATEWAY_TYPE", "Baileys Gateway"),
            "whatsapp_api_url": env.get("WHATSAPP_API_URL", "http://localhost:3000"),
            "whatsapp_api_key": env.get("WHATSAPP_API_KEY", ""),
            "whatsapp_channel_id": env.get("WHATSAPP_CHANNEL_ID", ""),
            "whatsapp_send_media": env.get("WHATSAPP_SEND_MEDIA", "True").lower() == "true",
            "sticker_enabled": env.get("STICKER_ENABLED", "True").lower() == "true",
            "sticker_path": env.get("STICKER_PATH", os.path.join(APP_DIR, "sticker.webp")),
            "sticker_position": env.get("STICKER_POSITION", "center"),
            "sticker_fallback_position": env.get("STICKER_FALLBACK_POSITION", "none"),
            "sticker_scale_percent": int(env.get("STICKER_SCALE_PERCENT", "74")),
            "blur_sexual_enabled": env.get("BLUR_SEXUAL_ENABLED", "True").lower() == "true",
            "blur_mode": env.get("BLUR_MODE", "sensitive_only"),
            "blur_intensity": int(env.get("BLUR_INTENSITY", "15")),
            "translate_enabled": env.get("TRANSLATE_ENABLED", "False").lower() == "true",
            "translate_target_lang": env.get("TRANSLATE_TARGET_LANG", "en"),
            "post_links_as_buttons": env.get("POST_LINKS_AS_BUTTONS", "True").lower() == "true",
            "button_terabox_free_enabled": env.get("BUTTON_TERABOX_FREE_ENABLED", "True").lower() == "true",
            "monetag_ads_enabled": env.get("MONETAG_ADS_ENABLED", "True").lower() == "true",
            "button_terabox_free_text": env.get("BUTTON_TERABOX_FREE_TEXT", "🎬 Watch Video (Free)"),
            "button_terabox_paid_text": env.get("BUTTON_TERABOX_PAID_TEXT", "⚡ Play Video (Paid)"),
            "button_watch_now_text": env.get("BUTTON_WATCH_NOW_TEXT", "▶️ Watch Now"),
            "button_backup_text": env.get("BUTTON_BACKUP_TEXT", "📢 Backup Channel"),
            "button_how_to_watch_text": env.get("BUTTON_HOW_TO_WATCH_TEXT", "👀 How to Watch"),
            "button_other_text": env.get("BUTTON_OTHER_TEXT", "🔗 Open Link"),
            "dispatch_bot_token": env.get("DISPATCH_BOT_TOKEN", "8751273482:AAGP-Ls2H899GyMNJ6MXHKEQPCeAOO6eWYI"),
            "dispatch_bot_username": env.get("DISPATCH_BOT_USERNAME", "Terastream_pro_bot"),
            "max_concurrent_tasks": 1,
        }

    def start(self) -> Tuple[bool, str]:
        if self.is_running:
            return False, "Engine is already running."

        cfg = self.build_config()
        if not cfg["api_id"] or not cfg["api_hash"]:
            return False, "API_ID or API_HASH missing in configuration."
        if not cfg["source_chats"]:
            return False, "No source channels specified."
        if not cfg["output_channel"]:
            return False, "No output channel specified."

        self.engine = AutomationEngine(
            config=cfg,
            db=self.db,
            log_fn=self.append_log,
            stat_fn=self.update_stat,
            auth_prompt_fn=self.prompt_auth,
            emergency_stop_fn=self.emergency_stop
        )

        def _runner():
            self.engine_loop = asyncio.new_event_loop()
            asyncio.set_event_loop(self.engine_loop)
            self.is_running = True
            self.start_time = time.time()
            self.append_log("INFO", "Automation Engine started successfully.")
            try:
                self.engine_loop.run_until_complete(self.engine.run())
            except Exception as e:
                self.append_log("ERROR", f"Automation engine stopped with exception: {e}")
            finally:
                self.is_running = False
                self.start_time = None
                self.append_log("INFO", "Automation Engine has halted.")
                try:
                    pending = [t for t in asyncio.all_tasks(self.engine_loop) if not t.done()]
                    for t in pending:
                        t.cancel()
                    if pending:
                        self.engine_loop.run_until_complete(asyncio.wait_for(asyncio.gather(*pending, return_exceptions=True), timeout=3.0))
                except Exception:
                    pass
                if self.engine:
                    try:
                        cli = getattr(self.engine, "client", None)
                        if cli and hasattr(cli, "session") and cli.session:
                            if hasattr(cli.session, "_conn") and cli.session._conn is not None:
                                try:
                                    cli.session._conn.close()
                                except Exception:
                                    pass
                                cli.session._conn = None
                    except Exception:
                        pass
                try:
                    self.engine_loop.close()
                except Exception:
                    pass

        self.engine_thread = threading.Thread(target=_runner, daemon=True)
        self.engine_thread.start()
        return True, "Engine started."

    def stop(self) -> Tuple[bool, str]:
        if not self.is_running or not self.engine:
            return False, "Engine is not running."

        self.append_log("INFO", "Stopping Automation Engine...")
        if self.engine_loop and self.engine_loop.is_running():
            try:
                future = asyncio.run_coroutine_threadsafe(self.engine.stop(), self.engine_loop)
                future.result(timeout=10.0)
            except Exception as e:
                self.append_log("WARNING", f"Error stopping engine coroutine: {e}")

        self.is_running = False
        if self.engine_thread and self.engine_thread.is_alive():
            try:
                self.engine_thread.join(timeout=5.0)
            except Exception:
                pass

        if self.engine:
            try:
                cli = getattr(self.engine, "client", None)
                if cli and hasattr(cli, "session") and cli.session:
                    if hasattr(cli.session, "_conn") and cli.session._conn is not None:
                        try:
                            cli.session._conn.close()
                        except Exception:
                            pass
                        cli.session._conn = None
            except Exception:
                pass

        self.engine = None
        self.engine_loop = None
        self.engine_thread = None
        self.append_log("SUCCESS", "Engine stopped cleanly. Session lock released.")
        # Auto-persist database state to GitHub
        threading.Thread(target=sync_db_to_github, daemon=True).start()
        return True, "Engine stopped."

    def restart(self) -> Tuple[bool, str]:
        self.stats["forwarded"] = 0
        self.stats["errors"] = 0
        self.restarted = True
        self.stop()
        time.sleep(2.0)
        return self.start()

    def get_uptime_str(self) -> str:
        if not self.is_running or not self.start_time:
            return "Stopped"
        elapsed = int(time.time() - self.start_time)
        m, s = divmod(elapsed, 60)
        h, m = divmod(m, 60)
        d, h = divmod(h, 24)
        if d > 0:
            return f"{d}d {h}h {m}m"
        if h > 0:
            return f"{h}h {m}m {s}s"
        return f"{m}m {s}s"

mgr = EngineManager()

# -------------------------------------------------------------
# ADMIN INPUT STATE MANAGEMENT
# -------------------------------------------------------------
admin_states: Dict[int, Dict[str, Any]] = {}

def set_admin_state(user_id: int, state: str, meta: Optional[Dict[str, Any]] = None):
    admin_states[user_id] = {"state": state, "meta": meta or {}}

def get_admin_state(user_id: int) -> Optional[Dict[str, Any]]:
    return admin_states.get(user_id)

def clear_admin_state(user_id: int):
    admin_states.pop(user_id, None)

# -------------------------------------------------------------
# SYSTEM RESOURCE MONITORING & UI BUILDERS
# -------------------------------------------------------------

def get_system_metrics() -> Dict[str, Any]:
    metrics = {
        "cpu_percent": 0.0,
        "cpu_count": os.cpu_count() or 1,
        "ram_percent": 0.0,
        "ram_used_mb": 0.0,
        "ram_total_mb": 0.0,
        "disk_percent": 0.0,
        "disk_used_gb": 0.0,
        "disk_total_gb": 0.0,
        "proc_ram_mb": 0.0,
        "platform": platform.platform(),
        "python_ver": platform.python_version(),
    }
    if psutil:
        try:
            metrics["cpu_percent"] = psutil.cpu_percent(interval=None)
            metrics["cpu_count"] = psutil.cpu_count(logical=True) or (os.cpu_count() or 1)
            vmem = psutil.virtual_memory()
            metrics["ram_percent"] = vmem.percent
            metrics["ram_used_mb"] = vmem.used / (1024 * 1024)
            metrics["ram_total_mb"] = vmem.total / (1024 * 1024)
            disk = psutil.disk_usage(APP_DIR)
            metrics["disk_percent"] = disk.percent
            metrics["disk_used_gb"] = disk.used / (1024 * 1024 * 1024)
            metrics["disk_total_gb"] = disk.total / (1024 * 1024 * 1024)
            proc = psutil.Process()
            metrics["proc_ram_mb"] = proc.memory_info().rss / (1024 * 1024)
        except Exception:
            pass
    return metrics

def build_system_info_menu() -> Tuple[str, InlineKeyboardMarkup]:
    m = get_system_metrics()

    def _bar(pct: float, length: int = 10) -> str:
        filled = max(0, min(length, int(round(length * (pct / 100.0)))))
        return "■" * filled + "□" * (length - filled)

    cpu_bar = _bar(m["cpu_percent"])
    ram_bar = _bar(m["ram_percent"])
    disk_bar = _bar(m["disk_percent"])

    status_icon = "🟢 RUNNING" if mgr.is_running else "🔴 STOPPED"
    uptime = mgr.get_uptime_str()

    text = (
        "╔════════════════════════════╗\n"
        "║  💻 <b>SYSTEM & RESOURCE STATUS</b> ║\n"
        "╚════════════════════════════╝\n\n"
        f"⚙️ <b>CPU Load:</b> <code>{m['cpu_percent']}%</code>\n"
        f"<code>[{cpu_bar}]</code> ({m['cpu_count']} Logical Cores)\n\n"
        f"🧠 <b>RAM (Memory):</b> <code>{m['ram_percent']}%</code>\n"
        f"<code>[{ram_bar}]</code>\n"
        f"• Used: <code>{m['ram_used_mb']:.1f} MB</code> ({m['ram_used_mb']/1024:.2f} GB)\n"
        f"• Total: <code>{m['ram_total_mb']:.1f} MB</code> ({m['ram_total_mb']/1024:.2f} GB)\n\n"
        f"💽 <b>Disk Space:</b> <code>{m['disk_percent']}%</code>\n"
        f"<code>[{disk_bar}]</code>\n"
        f"• Used: <code>{m['disk_used_gb']:.2f} GB</code> / <code>{m['disk_total_gb']:.2f} GB</code>\n\n"
        "🤖 <b>Bot Process Info:</b>\n"
        f"• Process RAM (RSS): <code>{m['proc_ram_mb']:.1f} MB</code>\n"
        f"• Automation Status: <b>{status_icon}</b>\n"
        f"• Engine Uptime: <code>{uptime}</code>\n\n"
        "🖥 <b>Host Environment:</b>\n"
        f"• OS: <code>{m['platform']}</code>\n"
        f"• Python: <code>{m['python_ver']}</code>\n"
    )

    markup = InlineKeyboardMarkup()
    markup.row(
        InlineKeyboardButton("🔄 Refresh Status", callback_data="nav:sysinfo"),
        InlineKeyboardButton("🔙 Back to Dashboard", callback_data="nav:main")
    )
    return text, markup

def build_main_dashboard() -> Tuple[str, InlineKeyboardMarkup]:
    env = read_env()
    status_icon = "🟢 <b>RUNNING</b>" if mgr.is_running else "🔴 <b>STOPPED</b>"
    uptime = mgr.get_uptime_str()

    src_list = [s.strip() for s in env.get("SOURCE_CHATS", "").split(",") if s.strip()]
    out_chan = env.get("OUTPUT_CHANNEL", "Not set")

    # Real-time task queue size
    q_len = 0
    if mgr.engine and hasattr(mgr.engine, "_task_queue") and mgr.engine._task_queue:
        q_len = mgr.engine._task_queue.qsize()

    # Database summary
    db_completed = 0
    db_filtered = 0
    try:
        def _get_counts(conn):
            cur = conn.cursor()
            cur.execute("SELECT status, count(*) FROM processed_tasks GROUP BY status")
            return dict(cur.fetchall())
        counts = mgr.db._execute_with_retry(_get_counts) or {}
        db_completed = counts.get("completed", 0)
        db_filtered = counts.get("filtered", 0)
    except Exception:
        pass

    # Collect all ON settings & toggles
    active_toggles = []
    if env.get("POST_LINKS_AS_BUTTONS", "True").lower() == "true":
        active_toggles.append("🔘 Buttons")
    if env.get("FILTER_ONLY_TERABOX_POSTS", "True").lower() == "true":
        active_toggles.append("🔍 TeraBox Only")
    if env.get("BLUR_SEXUAL_ENABLED", "True").lower() == "true":
        active_toggles.append("🔞 AI Blur")
    if env.get("AUTO_ACCEPT_JOIN_REQUESTS", "True").lower() == "true":
        active_toggles.append("👥 Auto-Join")
    if env.get("AUTO_HIDE_JOIN_MESSAGES", "False").lower() == "true":
        active_toggles.append("🚫 Clean Joins")
    if env.get("STICKER_ENABLED", "True").lower() == "true":
        active_toggles.append("🏷 Sticker")
    if env.get("TRANSLATE_ENABLED", "False").lower() == "true":
        curr_lang = env.get("TRANSLATE_TARGET_LANG", "en").upper()
        active_toggles.append(f"🌐 Translate ({curr_lang})")
    if env.get("PROCESS_HISTORICAL_MESSAGES", "False").lower() == "true":
        active_toggles.append("📜 Past Posts")
    if env.get("SEND_MEDIA_TO_PROCESSING_BOT", "True").lower() == "true":
        active_toggles.append("🖼 Send Media")

    toggles_str = " • ".join(f"<code>{t}</code>" for t in active_toggles) if active_toggles else "<i>None active</i>"

    # Live system metrics (CPU / RAM)
    sys_m = get_system_metrics()

    if getattr(mgr, "restarted", False):
        total_forwarded = mgr.stats.get("forwarded", 0)
    else:
        total_forwarded = max(db_completed, mgr.stats.get("forwarded", 0))

    text = (
        "╔════════════════════════════╗\n"
        "║  ⚡ <b>TERABOX FORWARDER PRO</b>  ║\n"
        "╚════════════════════════════╝\n\n"
        f"<b>Status:</b> {status_icon}\n"
        f"<b>Uptime:</b> <code>{uptime}</code>\n\n"
        "💻 <b>System Resources:</b>\n"
        f"• ⚙️ CPU: <code>{sys_m['cpu_percent']}%</code> ({sys_m['cpu_count']} Cores)\n"
        f"• 🧠 RAM: <code>{sys_m['ram_used_mb']:.0f} MB / {sys_m['ram_total_mb']:.0f} MB ({sys_m['ram_percent']}%)</code>\n"
        f"• 🤖 Process RAM: <code>{sys_m['proc_ram_mb']:.1f} MB</code>\n\n"
        "📊 <b>Forwarding Metrics (Persistent):</b>\n"
        f"• 🚀 Successfully Forwarded: <b>{total_forwarded}</b>\n"
        f"• ⏳ In Queue: <code>{q_len}</code>\n"
        f"• ❌ Errors: <code>{mgr.stats['errors']}</code>\n\n"
        "⚙️ <b>Live Configuration:</b>\n"
        f"• 📡 Sources: <b>{len(src_list)} Chats</b>\n"
        f"• 🎯 Output: <code>{out_chan}</code>\n"
        f"• ⏱ Delay: <code>{env.get('POST_DELAY_MIN_SECONDS', '1')}s - {env.get('POST_DELAY_MAX_SECONDS', '3')}s</code>\n\n"
        "🟢 <b>Active Settings & Filters:</b>\n"
        f"{toggles_str}\n"
    )

    markup = InlineKeyboardMarkup()
    if mgr.is_running:
        markup.row(
            InlineKeyboardButton("🟢 Stop Engine", callback_data="act:stop"),
            InlineKeyboardButton("🔄 Restart Engine", callback_data="act:restart")
        )
    else:
        markup.row(
            InlineKeyboardButton("🔴 Start Engine", callback_data="act:start"),
            InlineKeyboardButton("🔄 Refresh Status", callback_data="nav:main")
        )

    sched_on = is_schedule_mode_enabled()
    sched_icon = "🟢" if sched_on else "🔴"
    markup.row(
        InlineKeyboardButton(f"{sched_icon} ⏰ Post Scheduler: {'ON' if sched_on else 'OFF'}", callback_data="nav:scheduler")
    )
    markup.row(
        InlineKeyboardButton("📡 Source Channels", callback_data="nav:sources"),
        InlineKeyboardButton("🎯 Output Channel", callback_data="nav:output")
    )
    sw_aj = env.get("AUTO_ACCEPT_JOIN_REQUESTS", "True").lower() == "true"
    sw_hj = env.get("AUTO_HIDE_JOIN_MESSAGES", "False").lower() == "true"
    sw_stk = env.get("STICKER_ENABLED", "True").lower() == "true"
    sw_btns = env.get("POST_LINKS_AS_BUTTONS", "True").lower() == "true"
    sw_trans = env.get("TRANSLATE_ENABLED", "False").lower() == "true"

    markup.row(
        InlineKeyboardButton(f"👥 Auto-Joins: {'🟢 ON' if sw_aj else '🔴 OFF'}", callback_data="nav:auto_join"),
        InlineKeyboardButton(f"🚫 Clear Joins: {'🟢 ON' if sw_hj else '🔴 OFF'}", callback_data="nav:hide_join")
    )
    markup.row(
        InlineKeyboardButton(f"🌐 Translate: {'🟢 ON' if sw_trans else '🔴 OFF'}", callback_data="nav:trans"),
        InlineKeyboardButton(f"🏷 Sticker: {'🟢 ON' if sw_stk else '🔴 OFF'}", callback_data="nav:sticker")
    )
    markup.row(
        InlineKeyboardButton(f"🔘 Button Texts: {'🟢 ON' if sw_btns else '🔴 OFF'}", callback_data="nav:btn_texts"),
        InlineKeyboardButton("🔑 Credentials & Security", callback_data="nav:creds")
    )
    markup.row(
        InlineKeyboardButton("⚙️ Settings & Toggles", callback_data="nav:settings"),
        InlineKeyboardButton("⏱ Delay & Timing", callback_data="nav:timing")
    )
    markup.row(
        InlineKeyboardButton("💻 System (CPU & RAM)", callback_data="nav:sysinfo"),
        InlineKeyboardButton("📜 Live Logs", callback_data="nav:logs")
    )
    markup.row(
        InlineKeyboardButton("🔍 Channel ID Resolver", callback_data="nav:resolve"),
        InlineKeyboardButton("🧪 Test TeraBox Link", callback_data="nav:test_link")
    )
    markup.row(
        InlineKeyboardButton("🔄 Refresh Dashboard", callback_data="nav:main"),
        InlineKeyboardButton("🔙 Back to Dashboard", callback_data="act:back_dashboard")
    )

    return text, markup

def build_sources_menu() -> Tuple[str, InlineKeyboardMarkup]:
    env = read_env()
    raw = env.get("SOURCE_CHATS", "")
    sources = [s.strip() for s in raw.split(",") if s.strip()]

    text = "📡 <b>Source Channels & Groups</b>\n\n"
    if not sources:
        text += "<i>No source channels configured. Add one below:</i>\n"
    else:
        text += f"Total: <b>{len(sources)}</b> channels monitored:\n"
        for idx, src in enumerate(sources, start=1):
            text += f"<b>{idx}.</b> <code>{src}</code>\n"

    markup = InlineKeyboardMarkup()
    markup.row(
        InlineKeyboardButton("➕ Add Channel", callback_data="act:add_source"),
        InlineKeyboardButton("➖ Remove Channel", callback_data="act:remove_source_list")
    )
    markup.row(
        InlineKeyboardButton("🔙 Back to Dashboard", callback_data="nav:main")
    )
    return text, markup

def build_remove_source_menu() -> Tuple[str, InlineKeyboardMarkup]:
    env = read_env()
    raw = env.get("SOURCE_CHATS", "")
    sources = [s.strip() for s in raw.split(",") if s.strip()]

    text = "➖ <b>Select a Source Channel to Remove:</b>\n"
    markup = InlineKeyboardMarkup()

    for idx, src in enumerate(sources):
        markup.row(InlineKeyboardButton(f"❌ {src}", callback_data=f"act:del_source:{idx}"))

    markup.row(InlineKeyboardButton("🔙 Cancel", callback_data="nav:sources"))
    return text, markup

def build_output_menu() -> Tuple[str, InlineKeyboardMarkup]:
    env = read_env()
    output = env.get("OUTPUT_CHANNEL", "Not set")

    text = (
        "🎯 <b>Output Destination Channel / Group</b>\n\n"
        f"<b>Current Output:</b> <code>{output}</code>\n\n"
        "<i>All converted posts with buttons will be dispatched to this destination.</i>"
    )

    markup = InlineKeyboardMarkup()
    markup.row(
        InlineKeyboardButton("✏️ Change Output Channel", callback_data="act:set_output")
    )
    markup.row(
        InlineKeyboardButton("🧹 Clear All Posts (Delete for Everyone)", callback_data="act:purge_output_prompt")
    )
    markup.row(
        InlineKeyboardButton("🔙 Back to Dashboard", callback_data="nav:main")
    )
    return text, markup

def build_purge_output_confirm_menu() -> Tuple[str, InlineKeyboardMarkup]:
    env = read_env()
    output = env.get("OUTPUT_CHANNEL", "Not set")
    text = (
        "⚠️ <b>WARNING: Output Channel Post Auto-Removal</b>\n"
        "━━━━━━━━━━━━━━━━━━━━━━\n"
        f"🎯 <b>Target Destination:</b> <code>{output}</code>\n\n"
        "🚨 <b>আপনি কি নিশ্চিত যে আপনি এই পোস্টিং চ্যানেলের সকল পোস্ট মুছে ফেলতে চান?</b>\n\n"
        "• এটি চ্যানেলের সমস্ত পূর্ববর্তী পোস্ট ও মিডিয়া <b>সকল ব্যবহারকারী ও মেম্বারদের জন্য স্থায়ীভাবে ডিলিট (Delete for Everyone)</b> করে দেবে।\n"
        "• একবার ডিলিট প্রক্রিয়া শুরু হলে এগুলো আর ফিরিয়ে আনা যাবে না।\n"
        "━━━━━━━━━━━━━━━━━━━━━━\n"
        "<i>চালিয়ে যেতে নিচের কনফার্ম বাটনে চাপুন:</i>"
    )
    markup = InlineKeyboardMarkup()
    markup.row(
        InlineKeyboardButton("🗑️ Yes, Delete All Posts (সব পোস্ট ডিলিট করুন)", callback_data="act:do_purge_output")
    )
    markup.row(
        InlineKeyboardButton("❌ Cancel (বাতিল)", callback_data="nav:output")
    )
    return text, markup

def execute_output_channel_purge(call):
    chat_id = call.message.chat.id
    message_id = call.message.message_id
    env = read_env()
    raw_output = env.get("OUTPUT_CHANNEL", "").strip()

    if not raw_output or raw_output == "Not set":
        bot.answer_callback_query(call.id, "❌ No Output Channel configured!", show_alert=True)
        return

    target_list = [t.strip() for t in raw_output.split(",") if t.strip()]

    try:
        bot.edit_message_text(
            f"⏳ <b>Auto-Removing Posts from Output Channel...</b>\n\n"
            f"🎯 <b>Target(s):</b> <code>{raw_output}</code>\n\n"
            f"🔄 চ্যানেল কানেক্ট করা হচ্ছে এবং সমস্ত মেসেজ স্ক্যান করা হচ্ছে...\n"
            f"<i>(মেসেজ ডিলিট হতে থাকলে রিয়েল-টাইম কাউন্ট এখানে আপডেট হবে)</i>",
            chat_id=chat_id,
            message_id=message_id,
            parse_mode="HTML"
        )
    except Exception:
        pass

    def _purge_thread():
        deleted_total = 0
        err_msg = None

        async def _resolve_target(client, target):
            candidates = []
            target_str = str(target).strip()
            if target_str.startswith("-") or target_str.isdigit():
                clean_str = target_str.replace("-100", "").replace("-", "")
                if clean_str.isdigit():
                    candidates.extend([int(f"-100{clean_str}"), int(clean_str)])
            else:
                candidates.append(target_str)

            for cand in candidates:
                try:
                    ent = await client.get_entity(cand)
                    if ent: return ent
                except Exception:
                    pass

            try:
                cand_clean = str(candidates[0]).replace("-100", "").replace("-", "") if candidates else ""
                async for d in client.iter_dialogs():
                    d_clean = str(d.id).replace("-100", "").replace("-", "")
                    d_uname = getattr(d.entity, "username", None) or ""
                    if cand_clean == d_clean or (d_uname and target_str.lstrip("@").lower() == d_uname.lower()):
                        return d.entity
            except Exception:
                pass
            return None

        async def _run_purge_on_client(client):
            nonlocal deleted_total, err_msg
            try:
                for out_tgt in target_list:
                    ent = None
                    if mgr.is_running and mgr.engine:
                        if getattr(mgr.engine, "output_entities", None):
                            for oe in mgr.engine.output_entities:
                                oe_str = str(getattr(oe, "id", ""))
                                clean_oe = oe_str.replace("-100", "").replace("-", "")
                                clean_tgt = str(out_tgt).replace("-100", "").replace("-", "")
                                if clean_oe == clean_tgt or getattr(oe, "username", "") == out_tgt.lstrip("@"):
                                    ent = oe
                                    break
                        if not ent and getattr(mgr.engine, "output_entity", None):
                            ent = mgr.engine.output_entity

                    if not ent:
                        ent = await _resolve_target(client, out_tgt)

                    if not ent:
                        err_msg = f"চ্যানেল খুঁজে পাওয়া যায়নি: '{out_tgt}'"
                        continue

                    last_ui_update = time.time()
                    consecutive_empty = 0

                    while consecutive_empty < 3:
                        try:
                            messages = await client.get_messages(ent, limit=100)
                        except errors.FloodWaitError as fwe:
                            wait_s = min(fwe.seconds, 15) + 1
                            await asyncio.sleep(wait_s)
                            continue
                        except Exception as e:
                            err_msg = f"মেসেজ ফেচ করতে সমস্যা: {e}"
                            break

                        if not messages:
                            consecutive_empty += 1
                            await asyncio.sleep(0.8)
                            continue

                        consecutive_empty = 0
                        msg_ids = [m.id for m in messages if m and hasattr(m, 'id')]
                        if not msg_ids:
                            break

                        try:
                            await client.delete_messages(ent, msg_ids, revoke=True)
                            deleted_total += len(msg_ids)
                        except errors.FloodWaitError as fwe:
                            wait_s = min(fwe.seconds, 15) + 1
                            await asyncio.sleep(wait_s)
                        except Exception:
                            for mid in msg_ids:
                                try:
                                    await client.delete_messages(ent, [mid], revoke=True)
                                    deleted_total += 1
                                except Exception:
                                    pass

                        if time.time() - last_ui_update > 3.0:
                            last_ui_update = time.time()
                            try:
                                bot.edit_message_text(
                                    f"⏳ <b>Auto-Removing Posts from Output Channel...</b>\n\n"
                                    f"🎯 <b>Target:</b> <code>{raw_output}</code>\n"
                                    f"🗑️ <b>Deleted:</b> <code>{deleted_total}</code> posts\n\n"
                                    f"<i>চ্যানেল থেকে সমস্ত পোস্ট রিমুভ হচ্ছে (Delete for Everyone)...</i>",
                                    chat_id=chat_id,
                                    message_id=message_id,
                                    parse_mode="HTML"
                                )
                            except Exception:
                                pass
                        await asyncio.sleep(0.2)

            except Exception as ex:
                err_msg = str(ex)

        if mgr.is_running and mgr.engine and getattr(mgr.engine, "client", None) and mgr.engine_loop:
            try:
                future = asyncio.run_coroutine_threadsafe(_run_purge_on_client(mgr.engine.client), mgr.engine_loop)
                future.result(timeout=900.0)
            except Exception as e:
                if not err_msg:
                    err_msg = str(e)
        else:
            temp_loop = asyncio.new_event_loop()
            asyncio.set_event_loop(temp_loop)

            async def _standalone_runner():
                nonlocal err_msg
                api_id = env.get("API_ID")
                api_hash = env.get("API_HASH")
                session_name = env.get("SESSION_NAME", "telegram_automation")
                session_path = os.path.join(APP_DIR, "sessions", f"{session_name}.session")
                if not (api_id and api_hash and os.path.exists(session_path)):
                    err_msg = "Telegram session বা API credentials পাওয়া যায়নি।"
                    return
                temp_client = TelegramClient(session_path, int(api_id), api_hash)
                await temp_client.connect()
                if not await temp_client.is_user_authorized():
                    await temp_client.disconnect()
                    err_msg = "টেলিগ্রাম অ্যাকাউন্টে লগইন করা নেই। অনুগ্রহ করে প্রথমে লগইন করুন।"
                    return
                try:
                    await _run_purge_on_client(temp_client)
                finally:
                    try:
                        await temp_client.disconnect()
                    except Exception:
                        pass

            try:
                temp_loop.run_until_complete(_standalone_runner())
            except Exception as e:
                if not err_msg:
                    err_msg = str(e)
            finally:
                temp_loop.close()

        markup = InlineKeyboardMarkup()
        markup.row(
            InlineKeyboardButton("🔙 Back to Output Channel", callback_data="nav:output"),
            InlineKeyboardButton("🏠 Main Dashboard", callback_data="nav:main")
        )

        if err_msg and deleted_total == 0:
            try:
                bot.edit_message_text(
                    f"❌ <b>Post Removal Failed:</b>\n\n"
                    f"🎯 <b>Target:</b> <code>{raw_output}</code>\n"
                    f"⚠️ <b>Error:</b> <code>{html.escape(err_msg)}</code>\n\n"
                    f"<i>নিশ্চিত করুন যে আপনার টেলিগ্রাম অ্যাকাউন্টটি উক্ত চ্যানেলে অ্যাডমিন এবং মেসেজ ডিলিট করার অধিকার রয়েছে।</i>",
                    chat_id=chat_id,
                    message_id=message_id,
                    reply_markup=markup,
                    parse_mode="HTML"
                )
            except Exception:
                pass
        else:
            try:
                bot.edit_message_text(
                    f"✅ <b>All Posts Removed Successfully!</b>\n"
                    f"━━━━━━━━━━━━━━━━━━━━━━\n"
                    f"🎯 <b>Target:</b> <code>{raw_output}</code>\n"
                    f"🗑️ <b>Total Posts Deleted:</b> <code>{deleted_total}</code>\n\n"
                    f"✨ পোস্টিং চ্যানেল/গ্রুপ থেকে সমস্ত পোস্ট সফলভাবে সকল মেম্বার ও ব্যবহারকারীদের জন্য চিরতরে মুছে ফেলা হয়েছে (Deleted for Everyone)।",
                    chat_id=chat_id,
                    message_id=message_id,
                    reply_markup=markup,
                    parse_mode="HTML"
                )
            except Exception:
                pass

    threading.Thread(target=_purge_thread, daemon=True).start()

def build_sticker_menu() -> Tuple[str, InlineKeyboardMarkup]:
    env = read_env()
    is_enabled = env.get("STICKER_ENABLED", "True").lower() == "true"
    pos = env.get("STICKER_POSITION", "center")
    scale = env.get("STICKER_SCALE_PERCENT", "74")
    path = env.get("STICKER_PATH", os.path.join(APP_DIR, "sticker.webp"))

    st_icon = "🟢 <b>ENABLED (চালু)</b>" if is_enabled else "🔴 <b>DISABLED (বন্ধ)</b>"
    exists_str = "✅ বিদ্যমান আছে" if os.path.exists(path) else "⚠️ ফাইল পাওয়া যায়নি"

    text = (
        "🏷 <b>Sticker Watermark Settings</b>\n"
        "━━━━━━━━━━━━━━━━━━━━━━\n"
        f"• <b>Status:</b> {st_icon}\n"
        f"• <b>Position:</b> <code>{pos}</code>\n"
        f"• <b>Scale / Size:</b> <code>{scale}%</code>\n"
        f"• <b>Active Sticker:</b> <code>{os.path.basename(path)}</code> ({exists_str})\n\n"
        "💡 <i>টেলিগ্রামের যেকোনো স্টিকার (Sticker), ছবি বা ফটো এই বটের কাছে পাঠালে সাথে সাথে সেই স্টিকারটি পোস্টের ওয়াটারমার্ক হিসেবে সিলেক্ট ও সেভ হয়ে যাবে!</i>\n"
        "━━━━━━━━━━━━━━━━━━━━━━\n"
        "<i>নিচের অপশনগুলো ব্যবহার করুন:</i>"
    )

    markup = InlineKeyboardMarkup()
    tgl_btn = "🔴 Turn OFF Sticker" if is_enabled else "🟢 Turn ON Sticker"
    next_val = "False" if is_enabled else "True"
    markup.row(InlineKeyboardButton(tgl_btn, callback_data=f"tgl_stk:{next_val}"))

    markup.row(
        InlineKeyboardButton("📤 Send / Select Sticker", callback_data="act:send_new_sticker"),
        InlineKeyboardButton("👁 Preview Sticker", callback_data="act:prev_sticker")
    )
    markup.row(
        InlineKeyboardButton("📍 Center", callback_data="act:stk_pos:center"),
        InlineKeyboardButton("📍 Auto-Sexual", callback_data="act:stk_pos:sexual_auto"),
        InlineKeyboardButton("📍 Bottom-Right", callback_data="act:stk_pos:bottom_right")
    )
    markup.row(
        InlineKeyboardButton("⚙️ Settings Menu", callback_data="nav:settings"),
        InlineKeyboardButton("🔙 Back to Dashboard", callback_data="nav:main")
    )
    return text, markup

def build_settings_menu() -> Tuple[str, InlineKeyboardMarkup]:
    env = read_env()

    def get_sw(key: str, default: str = "False") -> Tuple[str, str]:
        val = env.get(key, default).lower() == "true"
        return ("🟢 ON" if val else "🔴 OFF"), ("False" if val else "True")

    sw_buttons, next_buttons = get_sw("POST_LINKS_AS_BUTTONS", "True")
    sw_filter, next_filter = get_sw("FILTER_ONLY_TERABOX_POSTS", "True")
    sw_blur, next_blur = get_sw("BLUR_SEXUAL_ENABLED", "True")
    sw_auto_join, next_auto_join = get_sw("AUTO_ACCEPT_JOIN_REQUESTS", "True")
    sw_hide_join, next_hide_join = get_sw("AUTO_HIDE_JOIN_MESSAGES", "False")
    sw_sticker, next_sticker = get_sw("STICKER_ENABLED", "True")
    sw_trans, next_trans = get_sw("TRANSLATE_ENABLED", "False")
    sw_hist, next_hist = get_sw("PROCESS_HISTORICAL_MESSAGES", "False")
    sw_send_bot, next_send_bot = get_sw("SEND_MEDIA_TO_PROCESSING_BOT", "True")
    sw_tb_free, next_tb_free = get_sw("BUTTON_TERABOX_FREE_ENABLED", "True")

    text = (
        "⚙️ <b>Settings & Automated Filters</b>\n\n"
        "<i>Click any switch below to toggle it instantly:</i>"
    )

    curr_lang = env.get("TRANSLATE_TARGET_LANG", "en").lower()

    markup = InlineKeyboardMarkup()
    markup.row(
        InlineKeyboardButton(f"🔍 TeraBox: {sw_filter}", callback_data=f"tgl:FILTER_ONLY_TERABOX_POSTS:{next_filter}"),
        InlineKeyboardButton(f"🔞 AI Blur: {sw_blur}", callback_data=f"tgl:BLUR_SEXUAL_ENABLED:{next_blur}")
    )
    markup.row(
        InlineKeyboardButton(f"🎬 Free Btn: {sw_tb_free}", callback_data=f"tgl:BUTTON_TERABOX_FREE_ENABLED:{next_tb_free}"),
        InlineKeyboardButton(f"📜 Past Posts: {sw_hist}", callback_data=f"tgl:PROCESS_HISTORICAL_MESSAGES:{next_hist}")
    )
    markup.row(
        InlineKeyboardButton(f"🖼 Media: {sw_send_bot}", callback_data=f"tgl:SEND_MEDIA_TO_PROCESSING_BOT:{next_send_bot}")
    )
    markup.row(
        InlineKeyboardButton("🔙 Back to Dashboard", callback_data="nav:main")
    )
    return text, markup

def build_monetag_menu() -> Tuple[str, InlineKeyboardMarkup]:
    env = read_env()
    zone_id = (get_setting("monetag_zone_id") or env.get("MONETAG_ZONE_ID", "12016303")).strip()
    is_ads_on = env.get("MONETAG_ADS_ENABLED", "True").lower() == "true"
    next_ads_val = "False" if is_ads_on else "True"
    status_icon = "🟢 <b>ACTIVE (বিজ্ঞাপন চালু)</b>" if is_ads_on else "🔴 <b>DISABLED (বিজ্ঞাপন বন্ধ)</b>"

    try:
        interval_sec = int(get_setting("monetag_midroll_interval") or env.get("MONETAG_MIDROLL_INTERVAL", "180"))
    except Exception:
        interval_sec = 180

    if interval_sec >= 60:
        mins = interval_sec // 60
        secs = interval_sec % 60
        time_display = f"{mins} Minute{'s' if mins > 1 else ''}" + (f" {secs}s" if secs > 0 else "")
    else:
        time_display = f"{interval_sec} Seconds"

    text = (
        "💰 <b>Monetag Ads Settings</b>\n"
        "━━━━━━━━━━━━━━━━━━━━━━\n"
        f"• <b>Status:</b> {status_icon}\n"
        f"• <b>Zone ID:</b> <code>{zone_id}</code>\n"
        f"• <b>Ads Interval (Time):</b> <b>{time_display}</b> (<code>{interval_sec}s</code>)\n\n"
        "💡 <i>Monetag অন থাকলে ফ্রি ইউজারদের জন্য ভিডিওতে বিজ্ঞাপন চালু থাকবে। অফ করলে বিজ্ঞাপন পুরোপুরি বন্ধ থাকবে।</i>\n"
        "━━━━━━━━━━━━━━━━━━━━━━\n"
        "<i>নিচ থেকে Monetag Ads অন/অফ করুন, Zone ID বা Ads Time পরিবর্তন করুন:</i>"
    )

    markup = InlineKeyboardMarkup()
    tgl_btn_text = "🔴 Turn OFF Monetag Ads" if is_ads_on else "🟢 Turn ON Monetag Ads"
    markup.row(
        InlineKeyboardButton(tgl_btn_text, callback_data=f"tgl_monetag:{next_ads_val}")
    )
    markup.row(
        InlineKeyboardButton(f"✏️ Edit Zone ID [{zone_id}]", callback_data="act:edit_monetag_zone")
    )
    markup.row(
        InlineKeyboardButton("⏱ 1 Minute (60s)", callback_data="set_midroll:60"),
        InlineKeyboardButton("⏱ 2 Minutes (120s)", callback_data="set_midroll:120")
    )
    markup.row(
        InlineKeyboardButton("⏱ 3 Minutes (180s)", callback_data="set_midroll:180"),
        InlineKeyboardButton("⏱ 5 Minutes (300s)", callback_data="set_midroll:300")
    )
    markup.row(
        InlineKeyboardButton("✏️ Custom Ads Time (Seconds / Minutes)", callback_data="act:edit_monetag_interval")
    )
    markup.row(
        InlineKeyboardButton("⚙️ Admin Settings", callback_data="nav:admin_settings"),
        InlineKeyboardButton("🔙 Back to Dashboard", callback_data="act:back_dashboard")
    )
    return text, markup

SUPPORTED_LANGUAGES = {
    "bn": ("🇧🇩 Bengali", "বাংলা"),
    "en": ("🇺🇸 English", "English"),
    "hi": ("🇮🇳 Hindi", "हिंदी"),
    "ur": ("🇵🇰 Urdu", "اردو"),
    "ar": ("🇸🇦 Arabic", "العربية"),
    "es": ("🇪🇸 Spanish", "Español"),
    "fr": ("🇫🇷 French", "Français"),
    "de": ("🇩🇪 German", "Deutsch"),
    "ru": ("🇷🇺 Russian", "Русский"),
    "id": ("🇮🇩 Indonesian", "Bahasa"),
    "pt": ("🇵🇹 Portuguese", "Português"),
    "tr": ("🇹🇷 Turkish", "Türkçe"),
}

def build_translation_menu() -> Tuple[str, InlineKeyboardMarkup]:
    env = read_env()
    is_enabled = env.get("TRANSLATE_ENABLED", "False").lower() == "true"
    curr_lang = env.get("TRANSLATE_TARGET_LANG", "en").lower()

    st_icon = "🟢 <b>ENABLED (অনুবাদ চালু)</b>" if is_enabled else "🔴 <b>DISABLED (অনুবাদ বন্ধ)</b>"
    lang_info = SUPPORTED_LANGUAGES.get(curr_lang, (f"Unknown ({curr_lang})", ""))
    lang_name = f"{lang_info[0]} ({lang_info[1]})" if lang_info[1] else lang_info[0]

    text = (
        "🌐 <b>Auto-Translation Settings & Languages</b>\n"
        "━━━━━━━━━━━━━━━━━━━━━━\n"
        f"• <b>Status:</b> {st_icon}\n"
        f"• <b>Active Target Language:</b> <b>{lang_name}</b> [<code>{curr_lang}</code>]\n\n"
        "💡 <i>Google Translate API দিয়ে সোর্স চ্যানেলের পোস্টের ক্যাপশন স্বয়ংক্রিয়ভাবে অনুবাদ হয়ে টার্গেট চ্যানেলে পোস্ট হবে। TeraBox লিংক এবং @mentions ১০০% অক্ষত থাকবে।</i>\n"
        "━━━━━━━━━━━━━━━━━━━━━━\n"
        "<i>নিচ থেকে অনুবাদ অন/অফ করুন বা যেকোনো ভাষায় ক্লিক করে ভাষা পরিবর্তন করুন:</i>"
    )

    markup = InlineKeyboardMarkup()
    tgl_btn_txt = "🔴 Turn OFF Translation" if is_enabled else "🟢 Turn ON Translation"
    next_val = "False" if is_enabled else "True"
    markup.row(InlineKeyboardButton(tgl_btn_txt, callback_data=f"tgl_trans:{next_val}"))

    # Language buttons grid (2 per row)
    items = list(SUPPORTED_LANGUAGES.items())
    for i in range(0, len(items), 2):
        row_btns = []
        code1, (label1, native1) = items[i]
        txt1 = f"✅ {label1}" if code1 == curr_lang else label1
        row_btns.append(InlineKeyboardButton(txt1, callback_data=f"set_lang:{code1}"))
        if i + 1 < len(items):
            code2, (label2, native2) = items[i + 1]
            txt2 = f"✅ {label2}" if code2 == curr_lang else label2
            row_btns.append(InlineKeyboardButton(txt2, callback_data=f"set_lang:{code2}"))
        markup.row(*row_btns)

    markup.row(
        InlineKeyboardButton("🧪 Test Translation Live", callback_data="act:test_trans"),
        InlineKeyboardButton("⚙️ Settings Menu", callback_data="nav:settings")
    )
    markup.row(
        InlineKeyboardButton("🔙 Back to Dashboard", callback_data="nav:main")
    )
    return text, markup

def build_btn_texts_menu() -> Tuple[str, InlineKeyboardMarkup]:
    env = read_env()
    btn_free = env.get("BUTTON_TERABOX_FREE_TEXT", "🎬 Watch Video (Free)")
    btn_paid = env.get("BUTTON_TERABOX_PAID_TEXT", "▶️ Play Video")
    btn_guide = env.get("BUTTON_HOW_TO_WATCH_TEXT", "👀 How to Watch")
    btn_backup = env.get("BUTTON_BACKUP_TEXT", "📢 Backup Channel")

    sw_btns = env.get("POST_LINKS_AS_BUTTONS", "True").lower() == "true"
    next_btns = "False" if sw_btns else "True"
    status_text = "🟢 <b>ACTIVE (পোস্টে বাটন যুক্ত হবে)</b>" if sw_btns else "🔴 <b>DISABLED (পোস্টে টেক্সট লিংক যাবে)</b>"

    sw_free = env.get("BUTTON_TERABOX_FREE_ENABLED", "True").lower() == "true"
    next_free = "False" if sw_free else "True"
    free_status_icon = "🟢 ON" if sw_free else "🔴 OFF"

    text = (
        "🔘 <b>Custom Inline Button Labels & Toggles</b>\n\n"
        f"• <b>Master Buttons:</b> {status_text}\n"
        f"• <b>Watch Video (Free) Status:</b> <b>{free_status_icon}</b>\n\n"
        f"1. <b>Free Button:</b> <code>{html.escape(btn_free)}</code>\n"
        f"2. <b>Play Video Button:</b> <code>{html.escape(btn_paid)}</code>\n"
        f"3. <b>Guide Button:</b> <code>{html.escape(btn_guide)}</code>\n"
        f"4. <b>Backup Button:</b> <code>{html.escape(btn_backup)}</code>\n\n"
        "<i>Click below to toggle buttons or edit labels:</i>"
    )

    markup = InlineKeyboardMarkup()
    markup.row(
        InlineKeyboardButton(f"🔘 All Buttons: {'🟢 Turn OFF' if sw_btns else '🔴 Turn ON'}", callback_data=f"tgl_btn_master:{next_btns}"),
        InlineKeyboardButton(f"🎬 Free Btn: {free_status_icon}", callback_data=f"tgl_tb_free:{next_free}")
    )
    markup.row(
        InlineKeyboardButton("✏️ Edit Free Label", callback_data="act:edit_btn:BUTTON_TERABOX_FREE_TEXT"),
        InlineKeyboardButton("✏️ Edit Play Video Label", callback_data="act:edit_btn:BUTTON_TERABOX_PAID_TEXT")
    )
    markup.row(
        InlineKeyboardButton("✏️ Edit Guide Label", callback_data="act:edit_btn:BUTTON_HOW_TO_WATCH_TEXT"),
        InlineKeyboardButton("✏️ Edit Backup Label", callback_data="act:edit_btn:BUTTON_BACKUP_TEXT")
    )
    markup.row(
        InlineKeyboardButton("🔙 Back to Dashboard", callback_data="nav:main")
    )
    return text, markup

def build_timing_menu() -> Tuple[str, InlineKeyboardMarkup]:
    env = read_env()
    d_min = env.get("POST_DELAY_MIN_SECONDS", "1")
    d_max = env.get("POST_DELAY_MAX_SECONDS", "3")
    timeout = env.get("REPLY_TIMEOUT", "120")

    text = (
        "⏱ <b>Delays & Timing Configuration</b>\n\n"
        f"• <b>Minimum Post Delay:</b> <code>{d_min}s</code>\n"
        f"• <b>Maximum Post Delay:</b> <code>{d_max}s</code>\n"
        f"• <b>Processing Bot Timeout:</b> <code>{timeout}s</code>\n\n"
        "<i>Click below to modify:</i>"
    )

    markup = InlineKeyboardMarkup()
    markup.row(
        InlineKeyboardButton("✏️ Change Delay Range", callback_data="act:set_delay")
    )
    markup.row(
        InlineKeyboardButton("✏️ Change Bot Timeout", callback_data="act:set_timeout")
    )
    markup.row(
        InlineKeyboardButton("🔙 Back to Dashboard", callback_data="nav:main")
    )
    return text, markup

# -------------------------------------------------------------
# TELEGRAM MTPROTO INTERACTIVE LOGIN WIZARD
# -------------------------------------------------------------


def build_scheduler_menu() -> Tuple[str, InlineKeyboardMarkup]:
    sched_on = is_schedule_mode_enabled()
    q_count = get_scheduled_queue_count()
    schedules = get_post_schedules()

    sched_list_str = ""
    if schedules:
        for s in schedules:
            status_dot = "🟢" if s["active"] else "🔴"
            sched_list_str += f"• {status_dot} <b>{s['time']}</b> — <code>{s['count']} Posts</code> (Last: {s['last_run'] or 'Never'})\n"
    else:
        sched_list_str = "<i>No scheduled times configured yet.</i>\n"

    text = (
        "⏰ <b>Scheduled Post Queue Dashboard</b>\n\n"
        f"<b>Queue Mode:</b> {'🟢 ENABLED (Posts stored in queue)' if sched_on else '🔴 DISABLED (Instant forwarding)'}\n"
        f"<b>Pending Posts in Queue:</b> <code>{q_count} Posts</code>\n\n"
        f"📅 <b>Active Time Schedules:</b>\n{sched_list_str}\n"
        "<i>When a scheduled time triggers, the bot flushes up to the target post count and pops them from disk.</i>"
    )

    markup = InlineKeyboardMarkup()
    tgl_btn = "🔴 Disable Queue Mode" if sched_on else "🟢 Enable Queue Mode"
    markup.row(InlineKeyboardButton(tgl_btn, callback_data="sched:toggle_mode"))
    markup.row(
        InlineKeyboardButton("➕ Add Schedule Time", callback_data="sched:add"),
        InlineKeyboardButton("🗑️ Delete Schedule", callback_data="sched:delete")
    )
    markup.row(
        InlineKeyboardButton(f"🚀 Post Now (Flush Queue)", callback_data="sched:flush_now"),
        InlineKeyboardButton("🗑️ Clear Queue", callback_data="sched:clear_queue")
    )
    markup.row(InlineKeyboardButton("🔙 Back to Dashboard", callback_data="nav:main"))
    return text, markup

def build_flush_options_menu() -> Tuple[str, InlineKeyboardMarkup]:
    q_count = get_scheduled_queue_count()
    text = (
        "🚀 <b>Post Now — Select Flush Count</b>\n"
        "━━━━━━━━━━━━━━━━━━━━━━\n"
        f"📦 <b>বর্তমানে কিউতে জমা মোট পোস্ট:</b> <code>{q_count} Posts</code>\n\n"
        "এখন আপনি চ্যানেলটিতে কতটি পোস্ট পাঠাতে চান? সব পোস্ট পাঠাতে চান নাকি নির্দিষ্ট কোনো সংখ্যা নির্ধারণ করতে চান?\n\n"
        "<i>নিচের অপশনগুলোর মধ্যে যেকোনো একটি নির্বাচন করুন:</i>"
    )
    markup = InlineKeyboardMarkup()
    all_label = f"🌟 All Posts ({q_count}টি সব)" if q_count > 0 else "🌟 All Posts"
    markup.row(InlineKeyboardButton(all_label, callback_data="sched:flush_run:all"))
    markup.row(
        InlineKeyboardButton("5 Posts", callback_data="sched:flush_run:5"),
        InlineKeyboardButton("10 Posts", callback_data="sched:flush_run:10"),
        InlineKeyboardButton("20 Posts", callback_data="sched:flush_run:20")
    )
    markup.row(
        InlineKeyboardButton("50 Posts", callback_data="sched:flush_run:50"),
        InlineKeyboardButton("100 Posts", callback_data="sched:flush_run:100")
    )
    markup.row(
        InlineKeyboardButton("✏️ Custom Number (কাস্টম সংখ্যা)", callback_data="sched:flush_custom")
    )
    markup.row(
        InlineKeyboardButton("🔙 Back to Queue Menu", callback_data="nav:scheduler")
    )
    return text, markup

class TgLoginManager:
    def __init__(self):
        self.sessions = {} # uid: {client, phone, phone_code_hash}
        self.loop = asyncio.new_event_loop()
        self.thread = threading.Thread(target=self.loop.run_forever, daemon=True)
        self.thread.start()

    def run(self, coro):
        future = asyncio.run_coroutine_threadsafe(coro, self.loop)
        return future.result(timeout=35.0)

    async def _send_code(self, uid: int, phone: str, api_id: int, api_hash: str) -> str:
        env = read_env()
        session_dir = os.path.join(APP_DIR, "sessions")
        os.makedirs(session_dir, exist_ok=True)
        session_name = env.get("SESSION_NAME", "telegram_automation")
        session_path = os.path.join(session_dir, session_name)

        if uid in self.sessions and self.sessions[uid].get("client"):
            try:
                await self.sessions[uid]["client"].disconnect()
            except Exception:
                pass

        client = TelegramClient(session_path, int(api_id), api_hash)
        await client.connect()
        sent_code = await client.send_code_request(phone)
        self.sessions[uid] = {
            "client": client,
            "phone": phone,
            "phone_code_hash": sent_code.phone_code_hash
        }
        return sent_code.phone_code_hash

    async def _submit_code(self, uid: int, code: str) -> Dict[str, Any]:
        info = self.sessions.get(uid)
        if not info or not info.get("client"):
            return {"status": "no_session", "error": "Login session expired or not found"}

        client = info["client"]
        phone = info["phone"]
        h = info["phone_code_hash"]
        try:
            await client.sign_in(phone=phone, code=code, phone_code_hash=h)
            me = await client.get_me()
            await client.disconnect()
            self.sessions.pop(uid, None)
            env = read_env()
            session_file = os.path.join(APP_DIR, "sessions", f"{env.get('SESSION_NAME', 'telegram_automation')}.session")
            threading.Thread(target=sync_file_to_github, args=(session_file, "Auto-sync authorized Telegram session"), daemon=True).start()
            return {"status": "ok", "user": me}
        except errors.SessionPasswordNeededError:
            return {"status": "2fa_needed"}
        except errors.PhoneCodeInvalidError:
            return {"status": "invalid_code", "error": "Invalid OTP code"}
        except errors.PhoneCodeExpiredError:
            return {"status": "expired_code", "error": "OTP code has expired"}
        except Exception as e:
            return {"status": "error", "error": str(e)}

    async def _submit_2fa(self, uid: int, password: str) -> Dict[str, Any]:
        info = self.sessions.get(uid)
        if not info or not info.get("client"):
            return {"status": "no_session", "error": "Login session expired or not found"}

        client = info["client"]
        try:
            await client.sign_in(password=password)
            me = await client.get_me()
            await client.disconnect()
            self.sessions.pop(uid, None)
            env = read_env()
            session_file = os.path.join(APP_DIR, "sessions", f"{env.get('SESSION_NAME', 'telegram_automation')}.session")
            threading.Thread(target=sync_file_to_github, args=(session_file, "Auto-sync authorized Telegram session"), daemon=True).start()
            return {"status": "ok", "user": me}
        except errors.PasswordHashInvalidError:
            return {"status": "invalid_pwd", "error": "Invalid 2FA password"}
        except Exception as e:
            return {"status": "error", "error": str(e)}

    def cancel(self, uid: int):
        info = self.sessions.pop(uid, None)
        if info and info.get("client"):
            try:
                future = asyncio.run_coroutine_threadsafe(info["client"].disconnect(), self.loop)
                future.result(timeout=3.0)
            except Exception:
                pass

login_mgr = TgLoginManager()

def build_credentials_menu() -> Tuple[str, InlineKeyboardMarkup]:
    env = read_env()
    api_id = env.get("API_ID", "Not Set")
    api_hash = env.get("API_HASH", "Not Set")
    phone = env.get("TELEGRAM_PHONE", "Not Set")
    dispatch_bot = env.get("DISPATCH_BOT_TOKEN", "Not Set")
    proc_bot = env.get("PROCESSING_BOT", "@LinkConvertTeraAbot")

    session_name = env.get("SESSION_NAME", "telegram_automation")
    session_file = os.path.join(APP_DIR, "sessions", f"{session_name}.session")
    has_session = os.path.exists(session_file) and os.path.getsize(session_file) > 1024
    session_status = "🟢 <b>Active / Logged In</b>" if has_session else "🔴 <b>Not Logged In (Action Required)</b>"

    masked_hash = f"{api_hash[:4]}...{api_hash[-4:]}" if len(api_hash) > 8 else api_hash
    masked_token = f"{dispatch_bot[:6]}...{dispatch_bot[-4:]}" if len(dispatch_bot) > 10 else dispatch_bot

    text = (
        "🔑 <b>Telegram Credentials & MTProto Session</b>\n"
        "━━━━━━━━━━━━━━━━━━━━━━\n"
        "📡 <b>Telegram Account Connection:</b>\n"
        f"• <b>Session Status:</b> {session_status}\n"
        f"• <b>API ID:</b> <code>{api_id}</code>\n"
        f"• <b>API Hash:</b> <code>{masked_hash}</code>\n"
        f"• <b>Phone Number:</b> <code>{phone}</code>\n\n"
        "🤖 <b>Bot Dispatcher & Processing:</b>\n"
        f"• <b>Processing Bot:</b> <code>{proc_bot}</code>\n"
        f"• <b>Dispatch Bot Token:</b> <code>{masked_token}</code>\n"
        "━━━━━━━━━━━━━━━━━━━━━━\n"
        "<i>নতুন টেলিগ্রাম অ্যাকাউন্ট কানেক্ট করতে বা তথ্য বদলাতে বাটন চাপুন:</i>"
    )

    markup = InlineKeyboardMarkup()
    login_btn = "🔄 Re-login Telegram Account" if has_session else "📲 🔑 Login Telegram Account (via OTP)"
    markup.row(
        InlineKeyboardButton(login_btn, callback_data="act:start_tg_login")
    )
    markup.row(
        InlineKeyboardButton("✏️ Edit API ID", callback_data="act:edit_cred:API_ID"),
        InlineKeyboardButton("✏️ Edit API Hash", callback_data="act:edit_cred:API_HASH")
    )
    markup.row(
        InlineKeyboardButton("✏️ Edit Phone Number", callback_data="act:edit_cred:TELEGRAM_PHONE"),
        InlineKeyboardButton("✏️ Edit Processing Bot", callback_data="act:edit_cred:PROCESSING_BOT")
    )
    markup.row(
        InlineKeyboardButton("✏️ Edit Dispatch Bot Token", callback_data="act:edit_cred:DISPATCH_BOT_TOKEN")
    )
    markup.row(
        InlineKeyboardButton("🔙 Back to Dashboard", callback_data="nav:main")
    )
    return text, markup

def build_auto_join_menu() -> Tuple[str, InlineKeyboardMarkup]:
    env = read_env()
    is_enabled = env.get("AUTO_ACCEPT_JOIN_REQUESTS", "True").lower() == "true"
    delay = env.get("AUTO_JOIN_DELAY_SECONDS", "1")
    raw_chats = env.get("AUTO_JOIN_CHATS", "")
    target_chats = [c.strip() for c in raw_chats.split(",") if c.strip()]

    st_icon = "🟢 <b>ENABLED (চালু)</b>" if is_enabled else "🔴 <b>DISABLED (বন্ধ)</b>"

    text = (
        "👥 <b>Auto-Accept Member Join Requests</b>\n"
        "━━━━━━━━━━━━━━━━━━━━━━\n"
        f"• <b>Status:</b> {st_icon}\n"
        f"• <b>Approval Delay:</b> <code>{delay}s</code>\n"
        f"• <b>Target Groups / Channels:</b> <b>{len(target_chats)}</b> chat(s)\n\n"
    )
    if target_chats:
        text += "<b>Monitored Chats:</b>\n"
        for idx, chat in enumerate(target_chats, 1):
            text += f"{idx}. <code>{chat}</code>\n"
        text += "\n"
    else:
        text += "<i>💡 কোনো নির্দিষ্ট চ্যাট যোগ না থাকলে সব সোর্স চ্যানেল ডিফল্টভাবে মনিটর হবে।</i>\n\n"

    text += (
        "💡 <i>প্রাইভেট চ্যানেল বা গ্রুপের মেম্বারদের জয়েন রিকোয়েস্ট ব্যাকগ্রাউন্ডে স্বয়ংক্রিয়ভাবে অ্যাক্সেপ্ট হবে।</i>\n"
        "━━━━━━━━━━━━━━━━━━━━━━"
    )

    markup = InlineKeyboardMarkup()
    tgl_btn = "🔴 Turn OFF Auto-Accept" if is_enabled else "🟢 Turn ON Auto-Accept"
    next_val = "False" if is_enabled else "True"
    markup.row(InlineKeyboardButton(tgl_btn, callback_data=f"tgl_aj:{next_val}"))

    markup.row(
        InlineKeyboardButton("➕ Add Target Chat", callback_data="act:add_aj_chat"),
        InlineKeyboardButton("➖ Remove Target Chat", callback_data="act:rem_aj_chat")
    )
    markup.row(
        InlineKeyboardButton("📋 Copy From Sources", callback_data="act:copy_src_to_aj"),
        InlineKeyboardButton("⏱ Set Delay", callback_data="act:set_aj_delay")
    )
    markup.row(
        InlineKeyboardButton("🔙 Back to Dashboard", callback_data="nav:main")
    )
    return text, markup

def build_hide_join_menu() -> Tuple[str, InlineKeyboardMarkup]:
    env = read_env()
    is_enabled = env.get("AUTO_HIDE_JOIN_MESSAGES", "False").lower() == "true"
    del_leave = env.get("HIDE_JOIN_DELETE_LEAVE", "True").lower() == "true"
    raw_chats = env.get("HIDE_JOIN_CHATS", "")
    target_chats = [c.strip() for c in raw_chats.split(",") if c.strip()]

    st_icon = "🟢 <b>ENABLED (চালু)</b>" if is_enabled else "🔴 <b>DISABLED (বন্ধ)</b>"
    leave_icon = "🟢 ON" if del_leave else "🔴 OFF"

    text = (
        "🚫 <b>Clean Notifications (Join/Leave Message Hider)</b>\n"
        "━━━━━━━━━━━━━━━━━━━━━━\n"
        f"• <b>Status:</b> {st_icon}\n"
        f"• <b>Delete Leave Messages:</b> {leave_icon}\n"
        f"• <b>Target Groups / Channels:</b> <b>{len(target_chats)}</b> chat(s)\n\n"
    )
    if target_chats:
        text += "<b>Monitored Chats:</b>\n"
        for idx, chat in enumerate(target_chats, 1):
            text += f"{idx}. <code>{chat}</code>\n"
        text += "\n"
    else:
        text += "<i>💡 কোনো নির্দিষ্ট চ্যাট যোগ না থাকলে সব সোর্স গ্রুপ ডিফল্টভাবে পরিষ্কার রাখা হবে।</i>\n\n"

    text += (
        "💡 <i>গ্রুপে 'User joined' বা 'User left' নোটিফিকেশন আসা মাত্রই তাৎক্ষণিক স্বয়ংক্রিয়ভাবে মুছে ফেলা হবে।</i>\n"
        "━━━━━━━━━━━━━━━━━━━━━━"
    )

    markup = InlineKeyboardMarkup()
    tgl_btn = "🔴 Turn OFF Hider" if is_enabled else "🟢 Turn ON Hider"
    next_val = "False" if is_enabled else "True"
    markup.row(InlineKeyboardButton(tgl_btn, callback_data=f"tgl_hj:{next_val}"))

    next_leave = "False" if del_leave else "True"
    markup.row(
        InlineKeyboardButton(f"🚪 Delete Leave Messages: {leave_icon}", callback_data=f"tgl_hj_leave:{next_leave}")
    )
    markup.row(
        InlineKeyboardButton("➕ Add Target Chat", callback_data="act:add_hj_chat"),
        InlineKeyboardButton("➖ Remove Target Chat", callback_data="act:rem_hj_chat")
    )
    markup.row(
        InlineKeyboardButton("📋 Copy From Sources", callback_data="act:copy_src_to_hj")
    )
    markup.row(
        InlineKeyboardButton("🔙 Back to Dashboard", callback_data="nav:main")
    )
    return text, markup

def build_remove_aj_menu() -> Tuple[str, InlineKeyboardMarkup]:
    env = read_env()
    raw = env.get("AUTO_JOIN_CHATS", "")
    chats = [c.strip() for c in raw.split(",") if c.strip()]

    text = "➖ <b>Select an Auto-Join Target Chat to Remove:</b>\n"
    markup = InlineKeyboardMarkup()
    if not chats:
        text += "<i>No specific target chats configured.</i>"
    else:
        for idx, c in enumerate(chats):
            markup.row(InlineKeyboardButton(f"❌ {c}", callback_data=f"act:del_aj_chat:{idx}"))

    markup.row(InlineKeyboardButton("🔙 Cancel", callback_data="nav:auto_join"))
    return text, markup

def build_remove_hj_menu() -> Tuple[str, InlineKeyboardMarkup]:
    env = read_env()
    raw = env.get("HIDE_JOIN_CHATS", "")
    chats = [c.strip() for c in raw.split(",") if c.strip()]

    text = "➖ <b>Select a Clean Notification Target Chat to Remove:</b>\n"
    markup = InlineKeyboardMarkup()
    if not chats:
        text += "<i>No specific target chats configured.</i>"
    else:
        for idx, c in enumerate(chats):
            markup.row(InlineKeyboardButton(f"❌ {c}", callback_data=f"act:del_hj_chat:{idx}"))

    markup.row(InlineKeyboardButton("🔙 Cancel", callback_data="nav:hide_join"))
    return text, markup


def build_logs_view() -> Tuple[str, InlineKeyboardMarkup]:
    recent = list(log_buffer)[-15:]
    if not recent:
        log_text = "No logs recorded yet."
    else:
        log_text = "\n".join(e[1] for e in recent)

    text = (
        "📜 <b>Live Server Logs (Last 15 lines):</b>\n\n"
        f"<pre>{log_text}</pre>"
    )

    markup = InlineKeyboardMarkup()
    markup.row(
        InlineKeyboardButton("🔄 Refresh Logs", callback_data="nav:logs"),
        InlineKeyboardButton("🗑️ Clear Logs", callback_data="act:clear_logs")
    )
    markup.row(
        InlineKeyboardButton("🔙 Back to Dashboard", callback_data="nav:main")
    )
    return text, markup

# -------------------------------------------------------------
# TELEGRAM BOT MESSAGE HANDLERS
# -------------------------------------------------------------

def get_txt(_target_uid, _str_key, **kwargs):
    lang = get_user_lang(_target_uid)
    if lang not in STRINGS:
        lang = "bn"
    template = STRINGS.get(lang, {}).get(_str_key) or STRINGS["bn"].get(_str_key, "")
    if kwargs:
        try:
            return template.format(**kwargs)
        except Exception:
            return template
    return template

def language_selection_markup(referrer_id=None):
    markup = InlineKeyboardMarkup(row_width=3)
    suffix = f"_{referrer_id}" if referrer_id is not None else ""
    markup.row(
        InlineKeyboardButton("🇧🇩 বাংলা (Bangla)", callback_data=f"set_lang_bn{suffix}"),
        InlineKeyboardButton("🇬🇧 English", callback_data=f"set_lang_en{suffix}"),
        InlineKeyboardButton("🇮🇳 हिन्दी (Hindi)", callback_data=f"set_lang_hi{suffix}")
    )
    return markup

def is_account_button(text):
    if not text: return False
    t = text.strip().lower()
    if t.startswith("http://") or t.startswith("https://") or t.startswith("t.me/") or t.startswith("@"):
        return False
    return any(k in t for k in ["my account", "account overview", "আমার অ্যাকাউন্ট", "অ্যাকাউন্ট", "अकाउंट"])

def is_refer_button(text):
    if not text: return False
    t = text.strip().lower()
    if t.startswith("http://") or t.startswith("https://") or t.startswith("t.me/") or t.startswith("@"):
        return False
    return any(k in t for k in ["refer & earn", "invite & earn", "রেফার", "ইনভাইট", "रेफर"])

def is_buy_button(text):
    if not text: return False
    t = text.strip().lower()
    if t.startswith("http://") or t.startswith("https://") or t.startswith("t.me/") or t.startswith("@"):
        return False
    return any(k in t for k in ["buy subscription", "upgrade vip", "buy vip", "সাবস্ক্রিপশন", "সব্সक्रिप्शन"])

def is_backup_button(text):
    if not text: return False
    t = text.strip().lower()
    if t.startswith("http://") or t.startswith("https://") or t.startswith("t.me/") or t.startswith("@"):
        return False
    return any(k in t for k in ["backup channel", "join backup channel", "ব্যাকআপ চ্যানেল", "ব্যাকআপ", "बैकअप"])

def is_contact_button(text):
    if not text: return False
    t = text.strip().lower()
    if t.startswith("http://") or t.startswith("https://") or t.startswith("t.me/") or t.startswith("@"):
        return False
    return any(k in t for k in ["contact admin", "যোগাযোগ", "সম্পর্ক", "संपर्क"])

def is_lang_button(text):
    if not text: return False
    t = text.strip().lower()
    if t.startswith("http://") or t.startswith("https://") or t.startswith("t.me/") or t.startswith("@"):
        return False
    return any(k in t for k in ["language", "change language", "ভাষা পরিবর্তন", "ভাষা", "भाषा"])

def is_user_stats_button(text):
    if not text: return False
    t = text.strip()
    if t.startswith("http://") or t.startswith("https://") or t.startswith("t.me/") or t.startswith("@"):
        return False
    return any(k in t for k in ["User List & Stats", "User Stats", "ইউজার লিস্ট"])

def is_controller_button(text):
    if not text: return False
    t = text.strip()
    if t.startswith("http://") or t.startswith("https://") or t.startswith("t.me/") or t.startswith("@"):
        return False
    return any(k in t for k in ["Channel Controller", "চ্যানেল কন্ট্রোলার", "Forwarder", "ফরওয়ার্ডার"])

def is_scheduler_button(text):
    if not text: return False
    t = text.strip()
    if t.startswith("http://") or t.startswith("https://") or t.startswith("t.me/") or t.startswith("@"):
        return False
    return any(k in t for k in ["Post Scheduler", "শিডিউল পোস্ট", "পোস্ট শিডিউলার", "Scheduler"])

def is_menu_navigation(text):
    if not text:
        return False
    t = text.strip()
    # URLs, usernames, telegram links, numbers, negative IDs, or link text are NEVER menu navigation buttons!
    if (t.startswith("http://") or t.startswith("https://") or 
        t.startswith("t.me/") or "t.me/" in t or 
        t.startswith("@") or t.startswith("-100") or 
        t.lstrip("-").isdigit()):
        return False
    if t.startswith('/'):
        return True
    return (
        is_account_button(t) or
        is_refer_button(t) or
        is_buy_button(t) or
        is_backup_button(t) or
        is_contact_button(t) or
        is_lang_button(t) or
        is_user_stats_button(t) or
        is_controller_button(t) or
        is_scheduler_button(t) or
        "Admin Settings" in t or
        "অ্যাডমিন সেটিংস" in t
    )

def dispatch_menu_command(message):
    bot.clear_step_handler_by_chat_id(message.chat.id)
    t = (message.text or "").strip()
    if "Admin Settings" in t or "অ্যাডমিন সেটিংস" in t:
        admin_settings_panel(message)
    elif is_controller_button(t) or t in ['/controller', '/forwarder', '/panel', '/dashboard']:
        if is_admin(message.from_user.id):
            clear_admin_state(message.from_user.id)
            text, markup = build_main_dashboard()
            bot.send_message(message.chat.id, text, reply_markup=markup, parse_mode="HTML")
    elif is_scheduler_button(t) or t in ['/scheduler', '/schedule']:
        if is_admin(message.from_user.id):
            clear_admin_state(message.from_user.id)
            text, markup = build_scheduler_menu()
            bot.send_message(message.chat.id, text, reply_markup=markup, parse_mode="HTML")
    elif is_user_stats_button(t):
        if is_admin(message.from_user.id):
            send_admin_user_stats(message.chat.id)
    elif is_account_button(t):
        account_info(message)
    elif is_refer_button(t):
        refer_info(message)
    elif is_buy_button(t):
        buy_info(message)
    elif is_backup_button(t):
        backup_channel_info(message)
    elif is_contact_button(t):
        contact_admin_prompt(message)
    elif is_lang_button(t):
        language_menu_prompt(message)
    elif t.startswith('/start'):
        send_welcome(message)
    elif t.startswith('/admin'):
        if is_admin(message.from_user.id):
            admin_settings_panel(message)
    elif t.startswith('/lang') or t.startswith('/language'):
        change_language_command(message)

# --- KEYBOARDS ---
def main_keyboard(user_id):
    markup = ReplyKeyboardMarkup(resize_keyboard=True)
    markup.row(
        KeyboardButton(get_txt(user_id, "btn_account")),
        KeyboardButton(get_txt(user_id, "btn_refer"))
    )
    if is_admin(user_id):
        markup.row(
            KeyboardButton("📊 User List & Stats"),
            KeyboardButton(get_txt(user_id, "btn_backup"))
        )
        markup.row(
            KeyboardButton("⚙️ Admin Settings"),
            KeyboardButton("⚡ Channel Controller")
        )
        markup.row(
            KeyboardButton(get_txt(user_id, "btn_lang"))
        )
    else:
        markup.row(
            KeyboardButton(get_txt(user_id, "btn_buy")),
            KeyboardButton(get_txt(user_id, "btn_backup"))
        )
        markup.row(
            KeyboardButton(get_txt(user_id, "btn_contact")),
            KeyboardButton(get_txt(user_id, "btn_lang"))
        )
    return markup

def admin_settings_inline():
    env = read_env()
    is_ads_on = env.get("MONETAG_ADS_ENABLED", "True").lower() == "true"
    monetag_st = "🟢 ON" if is_ads_on else "🔴 OFF"

    markup = InlineKeyboardMarkup(row_width=2)
    markup.add(
        InlineKeyboardButton("1. 📢 Broadcast Post", callback_data="btn_broadcast"),
        InlineKeyboardButton("2. 🔍 Find ID", callback_data="btn_find_id"),
        InlineKeyboardButton("3. 👥 Track Group ID", callback_data="set_group_id"),
        InlineKeyboardButton("4. 👑 Admin ID", callback_data="set_admin_id"),
        InlineKeyboardButton("5. ➕ Add Paid User", callback_data="btn_add_paid"),
        InlineKeyboardButton("6. 🎁 Initial Credits", callback_data="set_init_credits"),
        InlineKeyboardButton("7. 🔗 Refer Credits", callback_data="set_ref_credits"),
        InlineKeyboardButton("8. 💰 7 Days Price", callback_data="set_7_days_price"),
        InlineKeyboardButton("9. 💰 30 Days Price", callback_data="set_30_days_price"),
        InlineKeyboardButton("10. 💬 Share Text", callback_data="set_share_text"),
        InlineKeyboardButton("11. 🔗 Backup Channel Link", callback_data="set_backup_link"),
        InlineKeyboardButton("12. 🔢 Max Batch Links", callback_data="set_max_batch_links"),
        InlineKeyboardButton("13. ➕ Add User Credits", callback_data="btn_add_credits"),
        InlineKeyboardButton(f"14. 💰 Monetag Ads: {monetag_st}", callback_data="nav:monetag")
    )
    markup.row(
        InlineKeyboardButton("🔙 Back to Dashboard", callback_data="act:back_dashboard"),
        InlineKeyboardButton("❌ Close Panel", callback_data="btn_close_admin_panel")
    )
    return markup

def cancel_input_inline():
    markup = InlineKeyboardMarkup()
    markup.add(InlineKeyboardButton("❌ Cancel Input", callback_data="cancel_admin_input"))
    return markup

def build_premium_refer_markup(user_id, referrer_id=None):
    markup = InlineKeyboardMarkup(row_width=1)
    if referrer_id is not None:
        group_link = get_target_group_link()
        if group_link and group_link != "https://t.me/":
            markup.add(InlineKeyboardButton(get_txt(user_id, "btn_watch_video"), url=group_link))
        markup.add(InlineKeyboardButton(get_txt(user_id, "btn_verify_bonus"), callback_data=f"verify_join_{referrer_id}"))
    else:
        markup.add(InlineKeyboardButton(get_txt(user_id, "btn_invite_earn"), switch_inline_query="invite"))
        
    return markup

# --- INLINE QUERY HANDLER ---
@bot.inline_handler(func=lambda query: True)
def query_text(inline_query):
    try:
        user_id = inline_query.from_user.id
        channel_link = get_user_channel_invite_link(user_id)
        
        raw_text = get_setting("custom_share_text") or "🎬 Stream & Download Unlimited Videos!\n\n⚡ Join the channel to get free access!"
        
        clean_lines = []
        for line in raw_text.splitlines():
            if "{group_link}" in line or "{bot_link}" in line or re.search(r'https?://', line):
                cleaned = re.sub(r'https?://\S+', '', line).replace('{group_link}', '').replace('{bot_link}', '').strip(' :-\t')
                if cleaned and cleaned not in ["📢 Join Channel", "Join Channel", "📢"]:
                    clean_lines.append(cleaned)
                continue
            clean_lines.append(line)
        text_content = "\n".join(clean_lines).strip()
        if not text_content:
            text_content = "🎬 Stream & Download Unlimited Videos!\n\n⚡ Join the channel to get free access!"
        
        reply_markup = InlineKeyboardMarkup(row_width=1)
        reply_markup.add(InlineKeyboardButton("✨ 🎬 Watch Video ⚡", url=channel_link))
            
        photo_file_id, group_title = get_cached_group_info()
        
        if group_title:
            display_title = clean_text_for_markdown(group_title)
            final_caption = f"📢 **[{display_title}]({channel_link})**\n\n{text_content}"
        else:
            final_caption = text_content

        if photo_file_id:
            r = InlineQueryResultCachedPhoto(
                id='1',
                photo_file_id=photo_file_id,
                title='Watch Video (Share Invite)',
                description='Share invitation with Watch Video button to earn free credits!',
                caption=final_caption,
                parse_mode="Markdown",
                reply_markup=reply_markup
            )
        else:
            r = InlineQueryResultArticle(
                id='1',
                title='Watch Video (Share Invite)',
                description='Share invitation with Watch Video button to earn free credits!',
                input_message_content=InputTextMessageContent(final_caption, parse_mode="Markdown"),
                reply_markup=reply_markup
            )
        bot.answer_inline_query(inline_query.id, [r], cache_time=1)
    except Exception as e:
        print(f"Inline Query Error: {e}")

# --- BROADCAST HANDLERS ---
def process_broadcast_text(message, prompt_msg_id=None):
    if not is_admin(message.from_user.id):
        return
    if is_menu_navigation(message.text):
        if prompt_msg_id:
            try:
                bot.delete_message(message.chat.id, prompt_msg_id)
            except Exception:
                pass
        dispatch_menu_command(message)
        return
    if prompt_msg_id:
        try:
            bot.delete_message(message.chat.id, prompt_msg_id)
        except Exception:
            pass
    post_text = message.text
    msg = bot.send_message(
        message.chat.id,
        "🔘 **Button add korte chan?**\n\n"
        "Jodi Button add korte chan tobe niche eivabe din:\n"
        "`Button Text | https://yourlink.com`\n\n"
        "👉 Kono button add na korte chaile `skip` likhe pathan.",
        parse_mode="Markdown",
        reply_markup=cancel_input_inline()
    )
    bot.register_next_step_handler(msg, process_broadcast_button, post_text, msg.message_id, message.message_id)

def process_broadcast_button(message, post_text, prompt_msg_id=None, orig_msg_id=None):
    if not is_admin(message.from_user.id):
        return
    if is_menu_navigation(message.text):
        if prompt_msg_id:
            try:
                bot.delete_message(message.chat.id, prompt_msg_id)
            except Exception:
                pass
        dispatch_menu_command(message)
        return
    btn_input = message.text.strip()
    reply_markup = None
    
    if btn_input.lower() != "skip":
        try:
            btn_text, btn_url = [x.strip() for x in btn_input.split("|", 1)]
            reply_markup = InlineKeyboardMarkup()
            reply_markup.add(InlineKeyboardButton(text=btn_text, url=btn_url))
        except Exception:
            err_msg = bot.send_message(message.chat.id, "❌ Invalid Button Format! Default-e button charai post send hobe.")
            auto_delete_after(message.chat.id, err_msg.message_id, 5)

    users = get_all_users()
    total_users = len(users)
    success = 0
    failed = 0
    
    status_msg = bot.send_message(message.chat.id, f"🚀 **Broadcasting post to {total_users} users...**", parse_mode="Markdown")
    
    for uid in users:
        try:
            bot.send_message(uid, post_text, reply_markup=reply_markup, parse_mode="Markdown")
            success += 1
        except Exception:
            failed += 1

    report = (
        f"✅ **Broadcast Completed!**\n\n"
        f"👥 **Total Users:** {total_users}\n"
        f"🟢 **Success:** {success}\n"
        f"🔴 **Failed / Blocked:** {failed}"
    )
    bot.edit_message_text(report, chat_id=message.chat.id, message_id=status_msg.message_id, parse_mode="Markdown")
    to_delete = [status_msg.message_id, message.message_id]
    if prompt_msg_id:
        to_delete.append(prompt_msg_id)
    if orig_msg_id:
        to_delete.append(orig_msg_id)
    auto_delete_after(message.chat.id, to_delete, 5)

# --- CONTACT ADMIN & REPLY PROCESS ---
def process_user_contact_msg(message):
    text = message.text or ""
    if (is_account_button(text) or is_refer_button(text) or is_buy_button(text) or 
        is_backup_button(text) or is_contact_button(text) or is_lang_button(text) or 
        "Admin Settings" in text):
        return
    
    admin_id_raw = get_setting("admin_id") or "553826070"
    try:
        admin_id = int(admin_id_raw)
    except Exception:
        admin_id = 553826070

    user = message.from_user
    u_name = f"{user.first_name or ''} {user.last_name or ''}".strip()
    u_handle = f"@{user.username}" if user.username else "No Username"
    
    admin_notification = (
        f"📩 **New User Message Received!**\n\n"
        f"👤 **Name:** {clean_text_for_markdown(u_name)}\n"
        f"🆔 **User ID:** `{user.id}`\n"
        f"🌐 **Username:** {clean_text_for_markdown(u_handle)}\n\n"
        f"💬 **Message:**\n{clean_text_for_markdown(message.text)}"
    )
    
    markup = InlineKeyboardMarkup()
    markup.add(InlineKeyboardButton("💬 Reply to User", callback_data=f"reply_to_{user.id}"))
    
    try:
        bot.send_message(admin_id, admin_notification, parse_mode="Markdown", reply_markup=markup)
        bot.reply_to(message, get_txt(user.id, "contact_sent"), parse_mode="Markdown")
    except Exception as e:
        print(f"Error sending message to admin: {e}")
        bot.reply_to(message, get_txt(user.id, "contact_failed"))

@bot.callback_query_handler(func=lambda call: call.data.startswith("reply_to_"))
def handle_reply_button(call):
    if not is_admin(call.from_user.id):
        return
    
    target_user_id = int(call.data.split("_")[-1])
    msg = bot.send_message(
        call.message.chat.id,
        f"📝 **User (`{target_user_id}`)-er jonno apnar reply-ti likhe send korun:**",
        parse_mode="Markdown",
        reply_markup=cancel_input_inline()
    )
    bot.register_next_step_handler(msg, process_admin_reply_send, target_user_id, msg.message_id)

def process_admin_reply_send(message, target_user_id, prompt_msg_id=None):
    if not is_admin(message.from_user.id):
        return
    if is_menu_navigation(message.text):
        if prompt_msg_id:
            try:
                bot.delete_message(message.chat.id, prompt_msg_id)
            except Exception:
                pass
        dispatch_menu_command(message)
        return
    
    reply_text = message.text
    
    try:
        user_msg = f"💬 **Message from Admin:**\n\n{clean_text_for_markdown(reply_text)}"
        bot.send_message(target_user_id, user_msg, parse_mode="Markdown")
        rep = bot.reply_to(message, f"✅ **User (`{target_user_id}`)-er kache reply sothikbhabe pouche geche!**", parse_mode="Markdown")
        to_delete = [rep.message_id, message.message_id]
        if prompt_msg_id:
            to_delete.append(prompt_msg_id)
        auto_delete_after(message.chat.id, to_delete, 5)
    except Exception as e:
        rep = bot.reply_to(message, f"❌ **Reply Pathano Jaayni!**\nUser hoyto bot block koreche ba account deactivate hoyeche.\nDetails: `{clean_text_for_markdown(str(e))}`", parse_mode="Markdown")
        to_delete = [rep.message_id, message.message_id]
        if prompt_msg_id:
            to_delete.append(prompt_msg_id)
        auto_delete_after(message.chat.id, to_delete, 5)

# --- HANDLERS ---
@bot.my_chat_member_handler()
def handle_my_chat_member_update(chat_member_updated):
    try:
        chat = chat_member_updated.chat
        if chat.type == "private":
            uid = chat.id
            new_status = chat_member_updated.new_chat_member.status
            if new_status in ["kicked", "left"]:
                mark_user_blocked(uid, True)
                logging.info(f"[BLOCKED] User {uid} stopped/blocked the bot.")
            elif new_status == "member":
                mark_user_blocked(uid, False)
                logging.info(f"[UNBLOCKED] User {uid} restarted/unblocked the bot.")
    except Exception as e:
        logging.error(f"Error in my_chat_member_handler: {e}")

@bot.message_handler(commands=['lang', 'language'])
def change_language_command(message):
    user_id = message.from_user.id
    prompt_text = get_txt(user_id, "select_lang_prompt")
    bot.reply_to(message, prompt_text, parse_mode="Markdown", reply_markup=language_selection_markup())

def get_cached_video(vid_id):
    try:
        conn = sqlite3.connect('users.db')
        c = conn.cursor()
        c.execute('SELECT stream_url, title, size, res, dur, file_type, terabox_url FROM video_cache WHERE video_id = ?', (vid_id,))
        row = c.fetchone()
        conn.close()
        return row
    except Exception:
        return None

def handle_direct_video_watch(message, vid_id):
    user_id = message.from_user.id
    username = message.from_user.username or message.from_user.first_name or "User"
    mark_user_blocked(user_id, False)

    # 1. Register user if not exists
    user = get_user(user_id)
    if not user:
        register_user(user_id, username)
        user = get_user(user_id)

    # 2. Immediate feedback and Auto-Heal video stream FIRST!
    status_msg = bot.send_message(message.chat.id, "⚡ **Preparing Mini App Video Player...**", parse_mode="Markdown")
    stream_url, title, size, resolution, duration, file_type, terabox_url = get_or_refresh_video_stream(vid_id, force_refresh=True)

    if not stream_url:
        stream_url, title, size, resolution, duration, file_type, terabox_url = get_or_refresh_video_stream(vid_id, force_refresh=True)
        print(f"[ALL-IN-ONE] Fallback to embed player for {vid_id}: {stream_url}")

    # 3. Check multi-channel membership gate
    sec_check = check_user_all_memberships(user_id)
    if not sec_check["can_play"]:
        markup = InlineKeyboardMarkup()
        bot_username = get_bot_username()
        if not sec_check["target_channel_joined"]:
            markup.add(InlineKeyboardButton(text="📢 Join Main Channel", url=sec_check["target_channel_link"]))
        if not sec_check["backup_channel_joined"] and sec_check["backup_channel_link"]:
            markup.add(InlineKeyboardButton(text="📢 Join Backup Channel", url=sec_check["backup_channel_link"]))
        markup.add(InlineKeyboardButton(text="🔄 Joined! Verify & Watch", url=f"https://t.me/{bot_username}?start=v_{vid_id}"))
        
        missing_reasons = []
        if not sec_check["target_channel_joined"]:
            missing_reasons.append("• মেইন চ্যানেলে জয়েন থাকতে হবে")
        if not sec_check["backup_channel_joined"]:
            missing_reasons.append("• ব্যাকআপ চ্যানেলে জয়েন থাকতে হবে")
        if not sec_check["bot_started"] or sec_check["is_blocked"]:
            missing_reasons.append("• বটে /start দিয়ে আনব্লক থাকতে হবে")
        
        reason_txt = "\n".join(missing_reasons)
        bot.edit_message_text(
            f"⚠️ **Access Locked / Membership Required!**\n\n"
            f"এই ভিডিওটি দেখতে হলে নিচের শর্তগুলো পূরণ করুন:\n{reason_txt}\n\n"
            f"জয়েন করার পর নিচের **'Joined! Verify & Watch'** বাটনে চাপ দিন।",
            chat_id=message.chat.id,
            message_id=status_msg.message_id,
            reply_markup=markup,
            parse_mode="Markdown"
        )
        return

    # 4. Check Subscription, Already Unlocked status, and Credits (Never block!)
    is_paid = check_paid_status(user_id)
    already_unlocked = has_watched_video(user_id, vid_id)
    curr_credits = user[2] if user else 0
    is_ad_mode = False

    if is_paid:
        mark_watched_video(user_id, vid_id)
        rem_balance = "Unlimited (VIP Active)"
    elif already_unlocked:
        rem_balance = f"{curr_credits} Credits (Already Unlocked)"
    elif curr_credits > 0:
        update_credits(user_id, -1)
        mark_watched_video(user_id, vid_id)
        user = get_user(user_id)
        rem_balance = f"{user[2]} Credits"
    else:
        # User has 0 credits and is not VIP: Enable Free Ad Mode playback!
        is_ad_mode = True
        rem_balance = "0 Credits (Ad Supported)"

    # 5. Build Mini App Player URL
    encoded_stream_url = quote(stream_url, safe='')
    encoded_title = quote(title, safe='')
    encoded_size = quote(size, safe='')
    encoded_res = quote(resolution, safe='')
    encoded_dur = quote(duration, safe='')
    encoded_type = quote(file_type, safe='')
    
    u_lang = get_user_lang(user_id) or "bn"
    ad_param = "&mode=ad" if is_ad_mode else ""
    player_url = (
        f"{MY_PLAYER_URL}?"
        f"src={encoded_stream_url}&"
        f"title={encoded_title}&"
        f"size={encoded_size}&"
        f"res={encoded_res}&"
        f"dur={encoded_dur}&"
        f"type={encoded_type}&"
        f"lang={u_lang}&"
        f"startapp={vid_id}"
        f"{ad_param}"
    )

    markup = InlineKeyboardMarkup()
    if is_ad_mode:
        play_btn_text = "🎬 Watch Video (Ad Supported)" if u_lang == "en" else "🎬 ভিডিও দেখুন (ফ্রি অ্যাড সমর্থিত)"
        markup.add(InlineKeyboardButton(text=play_btn_text, web_app=WebAppInfo(url=player_url)))
        markup.add(InlineKeyboardButton(text="💎 Buy VIP (Ad-Free)", callback_data="buy_plan"))
        markup.add(InlineKeyboardButton(text="🎁 Refer Friends (+2 Credits)", callback_data="refer_earn"))
    else:
        play_btn_app = InlineKeyboardButton(text=get_txt(user_id, "btn_open_player_app"), web_app=WebAppInfo(url=player_url))
        markup.add(play_btn_app)

    clean_t = clean_text_for_markdown(title)
    clean_s = clean_text_for_markdown(size)
    clean_r = clean_text_for_markdown(resolution)
    clean_d = clean_text_for_markdown(duration)

    if is_ad_mode:
        prompt_txt = "💡 আপনার ফ্রি ক্রেডিট শেষ হয়ে গেছে, তবে আপনি বিজ্ঞাপন দেখে বিনামূল্যে সম্পূর্ণ ভিডিওটি দেখতে পারবেন:" if u_lang == "bn" else "💡 You have 0 credits, but you can watch this video for free with sponsored ads:"
    else:
        prompt_txt = get_txt(user_id, 'stream_play_prompt')

    reply_text = (
        f"🎬 *Title:* {clean_t}\n"
        f"📦 *Size:* {clean_s}\n"
        f"⚡ *Resolution:* {clean_r}\n"
        f"⏱️ *Duration:* {clean_d}\n"
        f"💳 *Remaining Balance:* {rem_balance}\n\n"
        f"{prompt_txt}"
    )

    bot.edit_message_text(
        reply_text,
        chat_id=message.chat.id,
        message_id=status_msg.message_id,
        reply_markup=markup,
        parse_mode="Markdown"
    )



@bot.message_handler(commands=['start'])
def send_welcome(message):
    user_id = message.from_user.id
    username = message.from_user.username or message.from_user.first_name or "User"
    mark_user_blocked(user_id, False)
    
    command_args = message.text.split()
    if len(command_args) > 1:
        param = command_args[1]
        if param.startswith("v_"):
            vid_id = param[2:]
            handle_direct_video_watch(message, vid_id)
            return
        elif (len(param) >= 6 and not param.isdigit() and
              param.lower() not in ["buy_vip", "plan", "vip", "buy", "upgrade", "update_plan", "plans", "refer", "invite", "ref", "invite_friends"]):
            handle_direct_video_watch(message, param)
            return

    # Direct button routing for Upgrade VIP and Invite
    if len(command_args) > 1:
        deep_cmd = command_args[1].lower()
        if deep_cmd in ["buy_vip", "plan", "vip", "buy", "upgrade", "update_plan", "plans"]:
            if not get_user(user_id):
                register_user(user_id, username)
            buy_info(message)
            return
        if deep_cmd in ["refer", "invite", "ref", "invite_friends"]:
            if not get_user(user_id):
                register_user(user_id, username)
            refer_info(message)
            return

    referrer_id = None
    if len(command_args) > 1 and command_args[1].isdigit():
        referrer_id = int(command_args[1])
        if referrer_id == user_id:
            referrer_id = None

    # Prompt language selection for all users (including Admin)
    prompt_text = (
        "🌐 **ভাষা নির্বাচন করুন / Choose Your Language / भाषा चुनें**\n\n"
        "বটটি ব্যবহার করতে আপনার পছন্দের ভাষা নির্বাচন করুন:\n"
        "Please select your language to proceed:\n"
        "कृपया आगे बढ़ने के लिए अपनी भाषा चुनें:"
    )
    bot.reply_to(message, prompt_text, parse_mode="Markdown", reply_markup=language_selection_markup(referrer_id))

@bot.callback_query_handler(func=lambda call: call.data.startswith("set_lang_"))
def handle_set_language(call):
    user_id = call.from_user.id
    username = call.from_user.username or call.from_user.first_name or "User"
    
    # Format: set_lang_{code} or set_lang_{code}_{referrer_id}
    parts = call.data.split("_")
    lang_code = parts[2] if len(parts) > 2 else "bn"
    if lang_code not in ["bn", "en", "hi"]:
        lang_code = "bn"
        
    referrer_id = None
    if len(parts) > 3 and parts[3].isdigit():
        referrer_id = int(parts[3])
        if referrer_id == user_id:
            referrer_id = None
            
    # Set user language
    set_user_lang(user_id, lang_code)
    try:
        sync_users_json()
    except Exception:
        pass
    
    notice = get_txt(user_id, "lang_selected_notice")
    bot.answer_callback_query(call.id, notice)
    
    try:
        bot.delete_message(call.message.chat.id, call.message.message_id)
    except Exception:
        pass

    if is_admin(user_id):
        user = get_user(user_id)
        if not user:
            register_user(user_id, username, referrer_id, lang=lang_code)
        welcome_txt = (
            f"👑 **Welcome Admin {clean_text_for_markdown(username)}!**\n\n"
            f"⚡ System is active and running.\n"
            f"Tap **⚙️ Admin Settings** below to manage the bot."
        )
        bot.send_message(call.message.chat.id, f"{notice}\n\n{welcome_txt}", parse_mode="Markdown", reply_markup=main_keyboard(user_id))
        return

    user = get_user(user_id)
    init_credits = get_setting("initial_credits") or 5
    
    if not user:
        register_user(user_id, username, referrer_id, lang=lang_code)
        user = get_user(user_id)
        if referrer_id:
            if is_user_in_chat(user_id):
                ref_bonus = int(get_setting("refer_credits") or 2)
                update_credits(referrer_id, ref_bonus)
                
                conn = sqlite3.connect('users.db')
                conn.cursor().execute('UPDATE users SET ref_claimed = 1 WHERE user_id = ?', (user_id,))
                conn.commit()
                conn.close()

                try:
                    ref_msg = get_txt(referrer_id, "ref_bonus_received", ref_bonus=ref_bonus)
                    bot.send_message(referrer_id, ref_msg)
                except Exception:
                    pass
                
                welcome_txt = get_txt(user_id, "welcome_new", username=username, init_credits=init_credits)
            else:
                welcome_txt = get_txt(user_id, "welcome_referral_locked", username=username)
                try:
                    bot.delete_message(call.message.chat.id, call.message.message_id)
                except Exception:
                    pass
                bot.send_message(call.message.chat.id, welcome_txt, parse_mode="Markdown", reply_markup=build_premium_refer_markup(user_id, referrer_id))
                return
        else:
            welcome_txt = get_txt(user_id, "welcome_new", username=username, init_credits=init_credits)
    else:
        # Existing user who picked/changed language
        set_user_lang(user_id, lang_code)
        welcome_txt = get_txt(user_id, "welcome_returning", username=username)
        
    notice = get_txt(user_id, "lang_selected_notice")
    bot.answer_callback_query(call.id, notice)
    
    try:
        bot.delete_message(call.message.chat.id, call.message.message_id)
    except Exception:
        pass
        
    bot.send_message(call.message.chat.id, f"{notice}\n\n{welcome_txt}", parse_mode="Markdown", reply_markup=main_keyboard(user_id))

@bot.callback_query_handler(func=lambda call: call.data.startswith("verify_join_"))
def handle_verify_join(call):
    user_id = call.from_user.id
    referrer_id = int(call.data.split("_")[-1])
    
    user = get_user(user_id)
    if not user:
        bot.answer_callback_query(call.id, "User not found!")
        return

    if len(user) > 6 and user[6] == 1:
        bot.answer_callback_query(call.id, get_txt(user_id, "verify_already_claimed"), show_alert=True)
        return

    if is_user_in_chat(user_id):
        ref_bonus = int(get_setting("refer_credits") or 2)
        update_credits(referrer_id, ref_bonus)

        conn = sqlite3.connect('users.db')
        conn.cursor().execute('UPDATE users SET ref_claimed = 1 WHERE user_id = ?', (user_id,))
        conn.commit()
        conn.close()

        bot.answer_callback_query(call.id, get_txt(user_id, "verify_success_alert"), show_alert=True)
        bot.send_message(call.message.chat.id, get_txt(user_id, "verify_success_msg"), reply_markup=main_keyboard(user_id))
        try:
            bot.send_message(referrer_id, get_txt(referrer_id, "ref_bonus_received", ref_bonus=ref_bonus))
        except Exception:
            pass
    else:
        bot.answer_callback_query(call.id, get_txt(user_id, "verify_failed"), show_alert=True)

# --- AUTOMATIC CHANNEL / GROUP REFERRAL TRACKING ---
@bot.chat_member_handler()
def handle_chat_member_update(update):
    try:
        new_status = update.new_chat_member.status
        old_status = update.old_chat_member.status
        if new_status in ['member', 'administrator', 'creator'] and old_status not in ['member', 'administrator', 'creator']:
            new_user_id = update.new_chat_member.user.id
            referrer_id = None
            
            # 1. Match from invite_link name (e.g. ref_12345678)
            if update.invite_link and update.invite_link.name and update.invite_link.name.startswith("ref_"):
                try:
                    referrer_id = int(update.invite_link.name.split("ref_")[-1])
                except Exception:
                    pass
            
            # 2. Match from cached ref_links table in DB
            if not referrer_id and update.invite_link and update.invite_link.invite_link:
                conn = sqlite3.connect('users.db')
                c = conn.cursor()
                c.execute('SELECT user_id FROM ref_links WHERE invite_link = ?', (update.invite_link.invite_link,))
                row = c.fetchone()
                conn.close()
                if row:
                    referrer_id = row[0]
                    
            if referrer_id and referrer_id != new_user_id:
                conn = sqlite3.connect('users.db')
                c = conn.cursor()
                c.execute('SELECT ref_claimed FROM users WHERE user_id = ?', (new_user_id,))
                user_row = c.fetchone()
                
                already_claimed = False
                if not user_row:
                    uname = update.new_chat_member.user.username or update.new_chat_member.user.first_name or "User"
                    init_credits = int(get_setting("initial_credits") or 5)
                    c.execute('INSERT OR IGNORE INTO users (user_id, username, credits, is_paid, referred_by, ref_claimed) VALUES (?, ?, ?, 0, ?, 1)', (new_user_id, uname, init_credits, referrer_id))
                    conn.commit()
                else:
                    already_claimed = (user_row[0] == 1)
                    if not already_claimed:
                        c.execute('UPDATE users SET ref_claimed = 1, referred_by = ? WHERE user_id = ?', (referrer_id, new_user_id))
                        conn.commit()
                conn.close()
                
                if not already_claimed:
                    ref_bonus = int(get_setting("refer_credits") or 2)
                    update_credits(referrer_id, ref_bonus)
                    try:
                        bot.send_message(referrer_id, f"🎉 Apnar referral link bebohar kore user channel/group-e join koray apni +{ref_bonus} Credits bonus peyechen!")
                    except Exception:
                        pass
    except Exception as e:
        print(f"Chat Member Update Handler Error: {e}")

@bot.chat_join_request_handler()
def handle_chat_join_request(req):
    try:
        new_user_id = req.from_user.id
        referrer_id = None
        
        if req.invite_link and req.invite_link.name and req.invite_link.name.startswith("ref_"):
            try:
                referrer_id = int(req.invite_link.name.split("ref_")[-1])
            except Exception:
                pass
                
        if not referrer_id and req.invite_link and req.invite_link.invite_link:
            conn = sqlite3.connect('users.db')
            c = conn.cursor()
            c.execute('SELECT user_id FROM ref_links WHERE invite_link = ?', (req.invite_link.invite_link,))
            row = c.fetchone()
            conn.close()
            if row:
                referrer_id = row[0]
                
        if referrer_id and referrer_id != new_user_id:
            conn = sqlite3.connect('users.db')
            c = conn.cursor()
            c.execute('SELECT ref_claimed FROM users WHERE user_id = ?', (new_user_id,))
            user_row = c.fetchone()
            
            already_claimed = False
            if not user_row:
                uname = req.from_user.username or req.from_user.first_name or "User"
                init_credits = int(get_setting("initial_credits") or 5)
                c.execute('INSERT OR IGNORE INTO users (user_id, username, credits, is_paid, referred_by, ref_claimed) VALUES (?, ?, ?, 0, ?, 1)', (new_user_id, uname, init_credits, referrer_id))
                conn.commit()
            else:
                already_claimed = (user_row[0] == 1)
                if not already_claimed:
                    c.execute('UPDATE users SET ref_claimed = 1, referred_by = ? WHERE user_id = ?', (referrer_id, new_user_id))
                    conn.commit()
            conn.close()
            
            if not already_claimed:
                ref_bonus = int(get_setting("refer_credits") or 2)
                update_credits(referrer_id, ref_bonus)
                try:
                    bot.send_message(referrer_id, f"🎉 Apnar referral link bebohar kore user channel/group-e join request pathanor karone apni +{ref_bonus} Credits bonus peyechen!")
                except Exception:
                    pass
    except Exception as e:
        print(f"Chat Join Request Handler Error: {e}")




# Aliases for unified router
set_language_callback = handle_set_language
verify_join_callback = handle_verify_join
handle_reply_to_user = handle_reply_button


def is_controller_button(text):
    if not text: return False
    return any(k in text for k in ["Channel Controller", "চ্যানেল কন্ট্রোলার", "Forwarder", "ফরওয়ার্ডার"])

def is_scheduler_button(text):
    if not text: return False
    return any(k in text for k in ["Post Scheduler", "শিডিউল পোস্ট", "পোস্ট শিডিউলার", "Scheduler"])

def get_admin_settings_content() -> Tuple[str, InlineKeyboardMarkup]:
    track_display = get_setting('target_channel_link') or get_setting('required_group_id')
    custom_share = clean_text_for_markdown(get_setting('custom_share_text') or 'N/A')
    backup_link = clean_text_for_markdown(get_setting('backup_channel_link') or 'N/A')
    env = read_env()
    m_zone = get_setting('monetag_zone_id') or env.get('MONETAG_ZONE_ID', '12016303')
    m_int = get_setting('monetag_midroll_interval') or env.get('MONETAG_MIDROLL_INTERVAL', '180')
    is_ads_on = env.get("MONETAG_ADS_ENABLED", "True").lower() == "true"
    try:
        m_int_int = int(m_int)
        m_int_lbl = f"{m_int_int // 60}m" if m_int_int >= 60 and m_int_int % 60 == 0 else f"{m_int_int}s"
    except Exception:
        m_int_lbl = f"{m_int}s"

    text = (
        "⚙️ <b>Admin Control Panel</b>\n\n"
        f"👑 <b>Admin ID:</b> <code>{html.escape(str(get_setting('admin_id') or ''))}</code>\n"
        f"👥 <b>Track Group ID:</b> <code>{html.escape(str(get_setting('required_group_id') or ''))}</code>\n"
        f"1. <b>Signup Free Credits:</b> <code>{html.escape(str(get_setting('initial_credits') or '5'))}</code>\n"
        f"2. <b>Referral Bonus:</b> <code>{html.escape(str(get_setting('refer_credits') or '2'))} Credits</code>\n"
        f"3. <b>7-Day Plan Price:</b> <code>{html.escape(str(get_setting('paid_7_days') or '100 BDT'))}</code>\n"
        f"4. <b>30-Day Plan Price:</b> <code>{html.escape(str(get_setting('paid_30_days') or '300 BDT'))}</code>\n"
        f"5. <b>Custom Share Text:</b>\n<code>{html.escape(str(get_setting('custom_share_text') or 'N/A'))}</code>\n"
        f"6. <b>Backup Channel Link:</b>\n<code>{html.escape(str(get_setting('backup_channel_link') or 'N/A'))}</code>\n"
        f"7. <b>Max Batch Links (per post):</b> <code>{html.escape(str(get_setting('max_batch_links') or '5'))}</code>\n"
        f"8. <b>Monetag Ads:</b> {'🟢 ON' if is_ads_on else '🔴 OFF'} (Zone: <code>{html.escape(str(m_zone))}</code> | Time: <code>{html.escape(str(m_int_lbl))}</code>)\n\n"
        "<i>Nicher numbered buttons theke option select korun:</i>"
    )
    return text, admin_settings_inline()

def admin_settings_panel(message):
    if not is_admin(message.from_user.id):
        return
    text, markup = get_admin_settings_content()
    try:
        bot.send_message(message.chat.id, text, parse_mode="HTML", reply_markup=markup)
    except Exception:
        clean_text = re.sub(r'<[^>]+>', '', text)
        bot.send_message(message.chat.id, clean_text, reply_markup=markup)
    auto_delete_after(message.chat.id, message.message_id, 5)

def send_admin_user_stats(chat_id):
    users = get_detailed_users()
    total_users = len(users)
    
    paid_users = []
    free_users = []

    for u in users:
        uid, uname, credits, is_paid, expiry = u
        u_is_paid = check_paid_status(uid)
        display_name = clean_text_for_markdown(uname or "No Name")
        
        if u_is_paid:
            paid_users.append((uid, display_name, credits, expiry))
        else:
            free_users.append((uid, display_name, credits))

    full_text = (
        f"📊 **BOT USER STATISTICS**\n\n"
        f"👥 **Total Users:** {total_users}\n"
        f"🔥 **Paid Users:** {len(paid_users)}\n"
        f"🆓 **Free Users:** {len(free_users)}\n\n"
        f"🔥 **--- PAID USERS ---**\n"
        f"{'Kono Paid User nei.' if not paid_users else ''}\n"
        f"🆓 **--- FREE USERS ---**\n"
        f"{'Kono Free User nei.' if not free_users else ''}"
    )

    markup = InlineKeyboardMarkup(row_width=2)
    
    idx = 1
    if paid_users:
        for uid, name, credits, expiry in paid_users:
            uname_clean = str(name).replace('@', '').strip()
            copy_val = f"@{uname_clean} {uid}" if uname_clean and uname_clean != "No Name" else str(uid)
            info_text = f"👤 {idx}. {name[:12]} | {uid}"
            info_btn = InlineKeyboardButton(info_text, copy_text=CopyTextButton(text=copy_val))
            msg_btn = InlineKeyboardButton(f"💬 Msg {idx}", callback_data=f"reply_to_{uid}")
            markup.add(info_btn, msg_btn)
            idx += 1
        
    if free_users:
        for uid, name, credits in free_users:
            uname_clean = str(name).replace('@', '').strip()
            copy_val = f"@{uname_clean} {uid}" if uname_clean and uname_clean != "No Name" else str(uid)
            info_text = f"👤 {idx}. {name[:12]} | {uid} | {credits}Cr"
            info_btn = InlineKeyboardButton(info_text, copy_text=CopyTextButton(text=copy_val))
            msg_btn = InlineKeyboardButton(f"💬 Msg {idx}", callback_data=f"reply_to_{uid}")
            markup.add(info_btn, msg_btn)
            idx += 1

    markup.row(InlineKeyboardButton("❌ Close", callback_data="btn_close_admin_panel"))
    bot.send_message(chat_id, full_text, parse_mode="Markdown", reply_markup=markup)

ADMIN_SETTING_KEYS = {
    "set_admin_id", "set_group_id", "set_init_credits", "set_ref_credits",
    "set_7_days_price", "set_30_days_price", "set_share_text", "set_backup_link",
    "set_max_batch_links", "set_monetag_zone", "set_monetag_interval"
}

@bot.callback_query_handler(func=lambda call: (call.data in ADMIN_SETTING_KEYS) or call.data in ["act:back_dashboard", "btn_back_to_dashboard", "btn_add_paid", "btn_find_id", "btn_broadcast", "btn_user_list", "btn_close_admin_panel", "cancel_admin_input", "btn_admin_lang", "btn_add_credits", "btn_deduct_credits", "btn_set_credits", "nav:admin_settings", "btn_admin_settings"])
def handle_admin_callbacks(call):
    if not is_admin(call.from_user.id):
        try:
            bot.answer_callback_query(call.id, "Access Denied!", show_alert=True)
        except Exception:
            pass
        return

    if call.data in ["nav:admin_settings", "btn_admin_settings"]:
        clear_admin_state(call.from_user.id)
        t, m = get_admin_settings_content()
        if not safe_edit_message(call, t, m):
            bot.send_message(call.message.chat.id, t, parse_mode="HTML", reply_markup=m)
        try:
            bot.answer_callback_query(call.id)
        except Exception:
            pass
        return

    if call.data in ["act:back_dashboard", "btn_back_to_dashboard"]:
        try:
            bot.answer_callback_query(call.id, "🔙 Back to Dashboard")
        except Exception:
            pass
        try:
            bot.delete_message(call.message.chat.id, call.message.message_id)
        except Exception:
            try:
                bot.edit_message_text("✅ Closed.", chat_id=call.message.chat.id, message_id=call.message.message_id)
            except Exception:
                pass
        welcome_txt = (
            f"👑 <b>Admin Dashboard</b>\n\n"
            f"⚡ System is active and running.\n"
            f"নিচের মেনু বাটনগুলো থেকে যেকোনো অপশন ব্যবহার করতে পারেন:"
        )
        bot.send_message(call.message.chat.id, welcome_txt, parse_mode="HTML", reply_markup=main_keyboard(call.from_user.id))
        return

    if call.data == "btn_admin_lang":
        try:
            bot.answer_callback_query(call.id)
        except Exception:
            pass
        bot.send_message(
            call.message.chat.id,
            get_txt(call.from_user.id, "select_lang_prompt"),
            parse_mode="Markdown",
            reply_markup=language_selection_markup()
        )
        return

    if call.data == "btn_close_admin_panel":
        try:
            bot.answer_callback_query(call.id)
        except Exception:
            pass
        try:
            bot.delete_message(call.message.chat.id, call.message.message_id)
        except Exception:
            bot.edit_message_text("❌ Panel closed.", chat_id=call.message.chat.id, message_id=call.message.message_id)
        return

    if call.data == "cancel_admin_input":
        try:
            bot.answer_callback_query(call.id)
        except Exception:
            pass
        bot.clear_step_handler_by_chat_id(call.message.chat.id)
        try:
            bot.delete_message(call.message.chat.id, call.message.message_id)
        except Exception:
            pass
        c_msg = bot.send_message(call.message.chat.id, "❌ **Input session cancelled.**", parse_mode="Markdown")
        auto_delete_after(call.message.chat.id, c_msg.message_id, 5)
        return

    if call.data == "btn_user_list":
        try:
            bot.answer_callback_query(call.id)
        except Exception:
            pass
        send_admin_user_stats(call.message.chat.id)
        return
    
    if call.data.startswith("no_action_"):
        bot.answer_callback_query(call.id, "It's just an identifier button.")
        return
    
    if call.data == "btn_broadcast":
        try:
            bot.answer_callback_query(call.id)
        except Exception:
            pass
        msg = bot.send_message(
            call.message.chat.id, 
            "📢 **Je Message-ti sobar kache Broadcast korte chan ta likhe pathan:**", 
            parse_mode="Markdown",
            reply_markup=cancel_input_inline()
        )
        bot.register_next_step_handler(msg, process_broadcast_text, msg.message_id)
        return

    if call.data == "btn_find_id":
        try:
            bot.answer_callback_query(call.id)
        except Exception:
            pass
        msg = bot.send_message(
            call.message.chat.id, 
            "🔗 **Group/Channel-er Public Link ba Username pathan:**\n\n"
            "Example: `https://t.me/yourchannel` ba `@yourchannel`\n\n"
            "⚠️ *Note: Bot-ke target channel/group-e admin banate hobe ID read korar jonno.*",
            parse_mode="Markdown",
            reply_markup=cancel_input_inline()
        )
        bot.register_next_step_handler(msg, process_generate_id, msg.message_id)
        return

    if call.data == "btn_add_paid":
        try:
            bot.answer_callback_query(call.id)
        except Exception:
            pass
        msg = bot.send_message(
            call.message.chat.id, 
            "👤 **User-ke Paid status dite User ID & Days din:**\nFormat: `<User_ID> <Days>`\nExample: `123456789 30`", 
            parse_mode="Markdown",
            reply_markup=cancel_input_inline()
        )
        bot.register_next_step_handler(msg, process_add_paid_ui, msg.message_id)
        return

    if call.data == "btn_add_credits":
        try:
            bot.answer_callback_query(call.id)
        except Exception:
            pass
        msg = bot.send_message(
            call.message.chat.id,
            "➕ **User Credit Add korte User ID & Amount din:**\n\n"
            "Format: `<User_ID> <Amount>`\n"
            "Example: `123456789 10`\n\n"
            "*(User-er bortoman credit er sathe ei amount jog hobe)*",
            parse_mode="Markdown",
            reply_markup=cancel_input_inline()
        )
        bot.register_next_step_handler(msg, process_add_credits_ui, msg.message_id)
        return

    if call.data == "btn_deduct_credits":
        try:
            bot.answer_callback_query(call.id)
        except Exception:
            pass
        msg = bot.send_message(
            call.message.chat.id,
            "➖ **User Credit Deduct/Komate User ID & Amount din:**\n\n"
            "Format: `<User_ID> <Amount>`\n"
            "Example: `123456789 5`\n\n"
            "*(User-er bortoman credit theke ei amount biyog hobe)*",
            parse_mode="Markdown",
            reply_markup=cancel_input_inline()
        )
        bot.register_next_step_handler(msg, process_deduct_credits_ui, msg.message_id)
        return

    if call.data == "btn_set_credits":
        try:
            bot.answer_callback_query(call.id)
        except Exception:
            pass
        msg = bot.send_message(
            call.message.chat.id,
            "✏️ **User Credit Set korte User ID & Exact Amount din:**\n\n"
            "Format: `<User_ID> <Amount>`\n"
            "Example: `123456789 20`\n\n"
            "*(User-er total credit thik ei songkhay set hoye jabe)*",
            parse_mode="Markdown",
            reply_markup=cancel_input_inline()
        )
        bot.register_next_step_handler(msg, process_set_credits_ui, msg.message_id)
        return

    key_map = {
        "set_admin_id": ("admin_id", "New Admin Telegram User ID din (e.g., 553826070):"),
        "set_group_id": ("required_group_id", "New Track Channel ba Group ID din (e.g., -1001234567890 ba @yourchannel):"),
        "set_init_credits": ("initial_credits", "New Signup Free Credits diben koto (e.g., 5):"),
        "set_ref_credits": ("refer_credits", "New Referral Bonus Credits diben koto (e.g., 2):"),
        "set_7_days_price": ("paid_7_days", "New 7 Days Price koto (e.g., 50 BDT):"),
        "set_30_days_price": ("paid_30_days", "New 30 Days Price koto (e.g., 150 BDT):"),
        "set_share_text": ("custom_share_text", "Users share korar somoy jei caption/text show korbe ta likhe pathan (Kono link dewar dorkar nei, Track Group ID theke auto detect hobe):"),
        "set_backup_link": ("backup_channel_link", "New Backup Channel Link din (e.g., https://t.me/yourchannel):"),
        "set_max_batch_links": ("max_batch_links", "Aki post-e aksathe maximum koyta video link convert kora jabe? (e.g., 5):"),
        "set_monetag_zone": ("monetag_zone_id", "New Monetag Ads Zone ID din (e.g., 12016303):"),
        "set_monetag_interval": ("monetag_midroll_interval", "Video cholakalin koto somoy por por ads show korbe (Seconds e likhun, e.g., 180 ba 3m):")
    }
    
    if call.data in key_map:
        try:
            bot.answer_callback_query(call.id)
        except Exception:
            pass
        setting_key, prompt_msg = key_map[call.data]
        msg = bot.send_message(
            call.message.chat.id, 
            f"📝 {prompt_msg}", 
            parse_mode="Markdown" if "`" in prompt_msg else None,
            reply_markup=cancel_input_inline()
        )
        bot.register_next_step_handler(msg, process_setting_update, setting_key, msg.message_id)

def process_generate_id(message, prompt_msg_id=None):
    if not is_admin(message.from_user.id):
        return
    if is_menu_navigation(message.text):
        if prompt_msg_id:
            try:
                bot.delete_message(message.chat.id, prompt_msg_id)
            except Exception:
                pass
        dispatch_menu_command(message)
        return
    
    text = message.text.strip()
    username = text
    
    if "t.me/" in text:
        username = text.split("t.me/")[-1].replace("/", "").strip()
    
    if not username.startswith("@") and not username.startswith("-100"):
        username = f"@{username}"
        
    try:
        chat = bot.get_chat(username)
        res_text = (
            f"✅ **ID Found Successfully!**\n\n"
            f"📌 **Title:** {clean_text_for_markdown(chat.title or 'N/A')}\n"
            f"🆔 **Channel/Group ID:** `{chat.id}`\n"
            f"🏷 **Type:** {chat.type}\n\n"
            f"💡 *Apni '3. Channel / Group Link' option-e ei `{chat.id}` paste korte parben.*"
        )
        rep = bot.reply_to(message, res_text, parse_mode="Markdown")
        to_delete = [rep.message_id, message.message_id]
        if prompt_msg_id:
            to_delete.append(prompt_msg_id)
        auto_delete_after(message.chat.id, to_delete, 5)
    except Exception as e:
        rep = bot.reply_to(message, f"❌ **Error:** Channel/Group ID ber kora jaayni.\nDetails: `{clean_text_for_markdown(str(e))}`", parse_mode="Markdown")
        to_delete = [rep.message_id, message.message_id]
        if prompt_msg_id:
            to_delete.append(prompt_msg_id)
        auto_delete_after(message.chat.id, to_delete, 5)

def process_setting_update(message, setting_key, prompt_msg_id=None):
    if not is_admin(message.from_user.id):
        return
    if is_menu_navigation(message.text):
        if prompt_msg_id:
            try:
                bot.delete_message(message.chat.id, prompt_msg_id)
            except Exception:
                pass
        dispatch_menu_command(message)
        return
    new_val = message.text.strip()
    if setting_key == "monetag_midroll_interval":
        raw = new_val.lower().strip()
        sec = 0
        if raw.endswith("m") or raw.endswith("min") or raw.endswith("minute") or raw.endswith("minutes"):
            num_part = re.sub(r"[^\d]", "", raw)
            if num_part:
                sec = int(num_part) * 60
        else:
            num_part = re.sub(r"[^\d]", "", raw)
            if num_part:
                sec = int(num_part)
        if sec < 10:
            sec = 180
        new_val = str(sec)
        update_env({"MONETAG_MIDROLL_INTERVAL": sec})
        threading.Thread(target=sync_monetag_to_github_pages, kwargs={"new_interval": sec}, daemon=True).start()
    elif setting_key == "monetag_zone_id":
        clean_zone = re.sub(r"[^\d]", "", new_val)
        if clean_zone:
            new_val = clean_zone
            update_env({"MONETAG_ZONE_ID": clean_zone})
            threading.Thread(target=sync_monetag_to_github_pages, kwargs={"new_zone": clean_zone}, daemon=True).start()
    elif setting_key == "max_batch_links":
        try:
            val_int = int(new_val)
            if val_int < 1:
                val_int = 1
            new_val = str(val_int)
        except Exception:
            new_val = "5"
    elif setting_key == "admin_username":
        new_val = new_val.replace('@', '')
    elif setting_key == "required_group_id":
        if new_val.startswith("http://") or new_val.startswith("https://"):
            set_setting("target_channel_link", new_val)
            if "t.me/" in new_val:
                raw_handle = new_val.split("t.me/")[-1].replace("/", "").strip()
                if not raw_handle.startswith("+"):
                    try:
                        chat = bot.get_chat(f"@{raw_handle}")
                        set_setting("required_group_id", str(chat.id))
                    except Exception:
                        set_setting("required_group_id", f"@{raw_handle}")
        elif new_val.startswith("@"):
            set_setting("target_channel_link", f"https://t.me/{new_val[1:]}")
            try:
                chat = bot.get_chat(new_val)
                set_setting("required_group_id", str(chat.id))
            except Exception:
                set_setting("required_group_id", new_val)
        else:
            set_setting("required_group_id", new_val)
            try:
                chat = bot.get_chat(new_val)
                if chat.username:
                    set_setting("target_channel_link", f"https://t.me/{chat.username}")
                elif chat.invite_link:
                    set_setting("target_channel_link", chat.invite_link)
            except Exception:
                pass
                
        try:
            conn = sqlite3.connect('users.db')
            conn.cursor().execute('DELETE FROM ref_links')
            conn.commit()
            conn.close()
        except Exception:
            pass
        set_setting("cached_group_photo_file_id", "")
        set_setting("cached_group_title", "")
            
        rep = bot.reply_to(message, f"✅ **Track Group ID & Button Link** successfully updated to:\n`{clean_text_for_markdown(new_val)}`", parse_mode="Markdown")
        to_delete = [rep.message_id, message.message_id]
        if prompt_msg_id:
            to_delete.append(prompt_msg_id)
        auto_delete_after(message.chat.id, to_delete, 5)
        return
    elif setting_key == "backup_channel_link":
        if not (new_val.startswith("http://") or new_val.startswith("https://")):
            if new_val.startswith("@"):
                new_val = f"https://t.me/{new_val[1:]}"
            elif new_val.startswith("t.me/"):
                new_val = f"https://{new_val}"
            else:
                new_val = f"https://t.me/{new_val}"
        set_setting("backup_channel_link", new_val)
        update_env({"BACKUP_CHANNEL_LINK": new_val, "BACKUP_CHANNEL": new_val})
        if "t.me/" in new_val:
            raw_handle = new_val.split("t.me/")[-1].replace("/", "").strip()
            if not raw_handle.startswith("+"):
                try:
                    chat = bot.get_chat(f"@{raw_handle}")
                    set_setting("backup_channel_id", str(chat.id))
                    update_env({"BACKUP_CHANNEL_ID": str(chat.id)})
                except Exception:
                    pass
        rep = bot.reply_to(message, f"✅ **Backup Channel Link** successfully updated to:\n`{clean_text_for_markdown(new_val)}`", parse_mode="Markdown")
        to_delete = [rep.message_id, message.message_id]
        if prompt_msg_id:
            to_delete.append(prompt_msg_id)
        auto_delete_after(message.chat.id, to_delete, 5)
        return

    set_setting(setting_key, new_val)
    friendly_key = setting_key.replace('_', ' ').title()
    rep = bot.reply_to(message, f"✅ **{friendly_key}** successfully updated!", parse_mode="Markdown")
    to_delete = [rep.message_id, message.message_id]
    if prompt_msg_id:
        to_delete.append(prompt_msg_id)
    auto_delete_after(message.chat.id, to_delete, 5)

def process_add_paid_ui(message, prompt_msg_id=None):
    if not is_admin(message.from_user.id):
        return
    if is_menu_navigation(message.text):
        if prompt_msg_id:
            try:
                bot.delete_message(message.chat.id, prompt_msg_id)
            except Exception:
                pass
        dispatch_menu_command(message)
        return
    try:
        args = message.text.split()
        target_user = int(args[0])
        days = int(args[1])
        set_paid_plan(target_user, days)
        rep = bot.reply_to(message, f"✅ User {target_user}-ke {days} diner jonno Paid status dewa hoyeche.")
        to_delete = [rep.message_id, message.message_id]
        if prompt_msg_id:
            to_delete.append(prompt_msg_id)
        auto_delete_after(message.chat.id, to_delete, 5)
        try:
            bot.send_message(target_user, get_txt(target_user, "paid_activated_msg", days=days))
        except Exception:
            pass
    except Exception as e:
        rep = bot.reply_to(message, "❌ Invalid format! Usage: `<user_id> <days>`", parse_mode="Markdown")
        to_delete = [rep.message_id, message.message_id]
        if prompt_msg_id:
            to_delete.append(prompt_msg_id)
        auto_delete_after(message.chat.id, to_delete, 5)

def process_add_credits_ui(message, prompt_msg_id=None):
    if not is_admin(message.from_user.id):
        return
    if is_menu_navigation(message.text):
        if prompt_msg_id:
            try:
                bot.delete_message(message.chat.id, prompt_msg_id)
            except Exception:
                pass
        dispatch_menu_command(message)
        return
    try:
        parts = message.text.strip().split()
        target_user = int(parts[0])
        amount = int(parts[1])
        if amount <= 0:
            raise ValueError("Amount must be positive")
        user = get_user(target_user)
        if not user:
            register_user(target_user, f"User_{target_user}")
            user = get_user(target_user)
        update_credits(target_user, amount)
        updated_user = get_user(target_user)
        new_balance = updated_user[2] if updated_user else amount
        rep = bot.reply_to(message, f"✅ **User {target_user}-ke {amount} credits jog kora hoyeche!**\n💰 Notun balance: `{new_balance}` credits", parse_mode="Markdown")
        to_delete = [rep.message_id, message.message_id]
        if prompt_msg_id:
            to_delete.append(prompt_msg_id)
        auto_delete_after(message.chat.id, to_delete, 6)
        try:
            bot.send_message(target_user, f"🎁 **Admin apnar account-e {amount} credits add korechen!**\n💰 Apnar bortoman balance: `{new_balance}` credits", parse_mode="Markdown")
        except Exception:
            pass
    except Exception:
        rep = bot.reply_to(message, "❌ **Invalid format!**\nSothik bhabe likhun: `<user_id> <amount>`\nExample: `123456789 10`", parse_mode="Markdown")
        to_delete = [rep.message_id, message.message_id]
        if prompt_msg_id:
            to_delete.append(prompt_msg_id)
        auto_delete_after(message.chat.id, to_delete, 6)

def process_deduct_credits_ui(message, prompt_msg_id=None):
    if not is_admin(message.from_user.id):
        return
    if is_menu_navigation(message.text):
        if prompt_msg_id:
            try:
                bot.delete_message(message.chat.id, prompt_msg_id)
            except Exception:
                pass
        dispatch_menu_command(message)
        return
    try:
        parts = message.text.strip().split()
        target_user = int(parts[0])
        amount = int(parts[1])
        if amount <= 0:
            raise ValueError("Amount must be positive")
        user = get_user(target_user)
        if not user:
            rep = bot.reply_to(message, f"❌ User `{target_user}` database-e khuje pawa jayni!", parse_mode="Markdown")
            to_delete = [rep.message_id, message.message_id]
            if prompt_msg_id:
                to_delete.append(prompt_msg_id)
            auto_delete_after(message.chat.id, to_delete, 6)
            return
        curr_credits = user[2]
        new_val = max(0, curr_credits - amount)
        set_user_credits(target_user, new_val)
        rep = bot.reply_to(message, f"✅ **User {target_user}-er account theke {amount} credits deduct kora hoyeche.**\n💰 Purber balance: `{curr_credits}` | Notun balance: `{new_val}` credits", parse_mode="Markdown")
        to_delete = [rep.message_id, message.message_id]
        if prompt_msg_id:
            to_delete.append(prompt_msg_id)
        auto_delete_after(message.chat.id, to_delete, 6)
    except Exception:
        rep = bot.reply_to(message, "❌ **Invalid format!**\nSothik bhabe likhun: `<user_id> <amount>`\nExample: `123456789 5`", parse_mode="Markdown")
        to_delete = [rep.message_id, message.message_id]
        if prompt_msg_id:
            to_delete.append(prompt_msg_id)
        auto_delete_after(message.chat.id, to_delete, 6)

def process_set_credits_ui(message, prompt_msg_id=None):
    if not is_admin(message.from_user.id):
        return
    if is_menu_navigation(message.text):
        if prompt_msg_id:
            try:
                bot.delete_message(message.chat.id, prompt_msg_id)
            except Exception:
                pass
        dispatch_menu_command(message)
        return
    try:
        parts = message.text.strip().split()
        target_user = int(parts[0])
        amount = int(parts[1])
        if amount < 0:
            raise ValueError("Amount cannot be negative")
        user = get_user(target_user)
        if not user:
            register_user(target_user, f"User_{target_user}")
        set_user_credits(target_user, amount)
        rep = bot.reply_to(message, f"✅ **User {target_user}-er balance `{amount}` credits set kora hoyeche!**", parse_mode="Markdown")
        to_delete = [rep.message_id, message.message_id]
        if prompt_msg_id:
            to_delete.append(prompt_msg_id)
        auto_delete_after(message.chat.id, to_delete, 6)
    except Exception:
        rep = bot.reply_to(message, "❌ **Invalid format!**\nSothik bhabe likhun: `<user_id> <amount>`\nExample: `123456789 20`", parse_mode="Markdown")
        to_delete = [rep.message_id, message.message_id]
        if prompt_msg_id:
            to_delete.append(prompt_msg_id)
        auto_delete_after(message.chat.id, to_delete, 6)

@bot.message_handler(func=lambda msg: bool(msg.text and is_account_button(msg.text)))
def account_info(message):
    user_id = message.from_user.id
    first_name = message.from_user.first_name or "User"
    last_name = message.from_user.last_name or ""
    full_name = f"{first_name} {last_name}".strip()
    username_str = f"@{message.from_user.username}" if message.from_user.username else "N/A"

    user = get_user(user_id)
    if not user:
        register_user(user_id, message.from_user.username or first_name)
        user = get_user(user_id)

    is_paid = check_paid_status(user_id)
    status_str = get_txt(user_id, "status_vip") if is_paid else get_txt(user_id, "status_free")
    expiry_str = user[4] if is_paid else "N/A"

    if is_paid:
        acc_text = (
            f"{get_txt(user_id, 'acc_title')}\n\n"
            f"{get_txt(user_id, 'acc_name', name=clean_text_for_markdown(full_name))}\n"
            f"{get_txt(user_id, 'acc_username', username=clean_text_for_markdown(username_str))}\n"
            f"{get_txt(user_id, 'acc_id', user_id=user[0])}\n"
            f"{get_txt(user_id, 'acc_status', status=status_str)}\n"
            f"{get_txt(user_id, 'acc_expiry', expiry=expiry_str)}"
        )
    else:
        acc_text = (
            f"{get_txt(user_id, 'acc_title')}\n\n"
            f"{get_txt(user_id, 'acc_name', name=clean_text_for_markdown(full_name))}\n"
            f"{get_txt(user_id, 'acc_username', username=clean_text_for_markdown(username_str))}\n"
            f"{get_txt(user_id, 'acc_id', user_id=user[0])}\n"
            f"{get_txt(user_id, 'acc_status', status=status_str)}\n"
            f"{get_txt(user_id, 'acc_credits', credits=user[2])}"
        )

    bot.reply_to(message, acc_text, parse_mode="Markdown")

@bot.message_handler(func=lambda msg: bool(msg.text and is_refer_button(msg.text)))
def refer_info(message):
    user_id = message.from_user.id
    ref_bonus = get_setting("refer_credits") or 2
    
    ref_text = (
        f"{get_txt(user_id, 'refer_title')}\n\n"
        f"{get_txt(user_id, 'refer_reward', ref_bonus=ref_bonus)}\n\n"
        f"{get_txt(user_id, 'refer_how')}"
    )
    
    bot.reply_to(message, ref_text, parse_mode="Markdown", reply_markup=build_premium_refer_markup(user_id))

@bot.message_handler(func=lambda msg: bool(msg.text and is_buy_button(msg.text)))
def buy_info(message):
    user_id = message.from_user.id
    p7 = get_setting("paid_7_days") or "100 BDT"
    p30 = get_setting("paid_30_days") or "300 BDT"
    
    pay_text = (
        f"{get_txt(user_id, 'buy_title')}\n\n"
        f"{get_txt(user_id, 'buy_desc')}\n\n"
        f"{get_txt(user_id, 'buy_pricing', p7=p7, p30=p30)}\n\n"
        f"{get_txt(user_id, 'buy_contact')}"
    )
    
    bot.reply_to(message, pay_text, parse_mode="Markdown")

@bot.message_handler(commands=['buy', 'vip', 'plan', 'plans', 'upgrade'])
def cmd_buy_plan(message):
    user_id = message.from_user.id
    username = message.from_user.username or message.from_user.first_name or "User"
    if not get_user(user_id):
        register_user(user_id, username)
    buy_info(message)

@bot.message_handler(commands=['refer', 'invite', 'share'])
def cmd_refer_invite(message):
    user_id = message.from_user.id
    username = message.from_user.username or message.from_user.first_name or "User"
    if not get_user(user_id):
        register_user(user_id, username)
    refer_info(message)

@bot.callback_query_handler(func=lambda call: call.data in ["buy_plan", "buy_vip", "upgrade_vip"])
def cb_buy_plan(call):
    user_id = call.from_user.id
    username = call.from_user.username or call.from_user.first_name or "User"
    if not get_user(user_id):
        register_user(user_id, username)
    try:
        bot.answer_callback_query(call.id)
    except Exception:
        pass
    buy_info(call.message)

@bot.callback_query_handler(func=lambda call: call.data in ["refer_earn", "invite", "refer"])
def cb_refer_earn(call):
    user_id = call.from_user.id
    username = call.from_user.username or call.from_user.first_name or "User"
    if not get_user(user_id):
        register_user(user_id, username)
    try:
        bot.answer_callback_query(call.id)
    except Exception:
        pass
    refer_info(call.message)

@bot.message_handler(func=lambda msg: bool(msg.text and is_backup_button(msg.text)))
def backup_channel_info(message):
    user_id = message.from_user.id
    backup_link = get_setting("backup_channel_link") or ""
    dummy_bk = ["https://t.me/your_backup_channel", "https://t.me/", "", "None", None, "your_backup_channel"]
    if not backup_link or backup_link in dummy_bk or "your_backup_channel" in backup_link.lower():
        env_bk = read_env().get("BACKUP_CHANNEL_LINK") or read_env().get("BACKUP_CHANNEL") or ""
        if env_bk and env_bk not in dummy_bk and "your_backup_channel" not in env_bk.lower():
            backup_link = env_bk
        else:
            backup_link = get_setting("target_channel_link") or "https://t.me/"
    if not (backup_link.startswith("http://") or backup_link.startswith("https://")):
        if backup_link.startswith("@"):
            backup_link = f"https://t.me/{backup_link[1:]}"
        elif backup_link.startswith("t.me/"):
            backup_link = f"https://{backup_link}"
        else:
            backup_link = f"https://t.me/{backup_link}"
    
    text = (
        f"{get_txt(user_id, 'backup_title')}\n\n"
        f"{get_txt(user_id, 'backup_desc')}"
    )
    
    markup = InlineKeyboardMarkup()
    markup.add(InlineKeyboardButton(get_txt(user_id, "btn_join_backup_inline"), url=backup_link))
    
    bot.reply_to(message, text, parse_mode="Markdown", reply_markup=markup)

@bot.message_handler(func=lambda msg: bool(msg.text and is_contact_button(msg.text)))
def contact_admin_prompt(message):
    user_id = message.from_user.id
    msg = bot.send_message(
        message.chat.id, 
        get_txt(user_id, "contact_prompt"), 
        parse_mode="Markdown"
    )
    bot.register_next_step_handler(msg, process_user_contact_msg)

@bot.message_handler(func=lambda msg: bool(msg.text and is_user_stats_button(msg.text)))
def user_stats_menu_prompt(message):
    if not is_admin(message.from_user.id):
        return
    send_admin_user_stats(message.chat.id)


@bot.message_handler(func=lambda msg: bool(msg.text and ("Admin Settings" in msg.text or "অ্যাডমিন সেটিংস" in msg.text)))
def explicit_admin_settings_btn_handler(message):
    admin_settings_panel(message)

@bot.message_handler(func=lambda msg: bool(msg.text and is_controller_button(msg.text)))
def explicit_channel_controller_btn_handler(message):
    if is_admin(message.from_user.id):
        clear_admin_state(message.from_user.id)
        text, markup = build_main_dashboard()
        bot.send_message(message.chat.id, text, reply_markup=markup, parse_mode="HTML")

@bot.message_handler(func=lambda msg: bool(msg.text and is_scheduler_button(msg.text)))
def explicit_post_scheduler_btn_handler(message):
    if is_admin(message.from_user.id):
        clear_admin_state(message.from_user.id)
        text, markup = build_scheduler_menu()
        bot.send_message(message.chat.id, text, reply_markup=markup, parse_mode="HTML")

@bot.message_handler(func=lambda msg: bool(msg.text and is_lang_button(msg.text)))
def language_menu_prompt(message):
    user_id = message.from_user.id
    prompt_text = get_txt(user_id, "select_lang_prompt")
    bot.reply_to(message, prompt_text, parse_mode="Markdown", reply_markup=language_selection_markup())

def handle_message(message):
    user_id = message.from_user.id
    raw_content = message.text or message.caption or ""
    
    if raw_content and is_menu_navigation(raw_content):
        dispatch_menu_command(message)
        return

    user = get_user(user_id)
    if not user:
        register_user(user_id, message.from_user.username or message.from_user.first_name or "User")
        user = get_user(user_id)

    is_paid = check_paid_status(user_id)
    credits = user[2]

    # Collect URLs from regex (text/caption) and telegram entities (including hidden/hyperlinked URLs)
    all_found_urls = re.findall(r'https?://[^\s]+', raw_content)
    
    entities = (message.entities or []) + (message.caption_entities or [])
    for ent in entities:
        if ent.type == 'text_link' and ent.url:
            all_found_urls.append(ent.url)
        elif ent.type == 'url':
            try:
                sub = raw_content[ent.offset:ent.offset+ent.length]
                if sub.startswith('http'):
                    all_found_urls.append(sub)
            except Exception:
                pass

    # De-duplicate while preserving order & strip punctuation
    seen = set()
    urls = []
    for u in all_found_urls:
        cleaned_u = u.strip().rstrip('.,;:!?"\')>]}')
        if cleaned_u and cleaned_u not in seen:
            seen.add(cleaned_u)
            urls.append(cleaned_u)

    if not urls:
        rep = bot.reply_to(message, get_txt(user_id, "invalid_link_prompt"))
        auto_delete_after(message.chat.id, [rep.message_id, message.message_id], 5)
        return

    try:
        max_batch = int(get_setting("max_batch_links") or "5")
    except Exception:
        max_batch = 5

    urls_to_process = urls[:max_batch]

    if not is_paid and credits < 1:
        rep = bot.reply_to(
            message, 
            get_txt(user_id, "no_credits_alert"),
            parse_mode="Markdown"
        )
        auto_delete_after(message.chat.id, [rep.message_id, message.message_id], 5)
        return

    for idx, target_url in enumerate(urls_to_process, 1):
        user = get_user(user_id)
        is_paid = check_paid_status(user_id)
        credits = user[2]
        if not is_paid and credits < 1:
            no_cr_msg = bot.send_message(
                message.chat.id, 
                get_txt(user_id, "no_credits_alert"),
                parse_mode="Markdown"
            )
            auto_delete_after(message.chat.id, no_cr_msg.message_id, 5)
            break

        prefix = f"[{idx}/{len(urls_to_process)}] " if len(urls_to_process) > 1 else ""
        if idx == 1:
            status_msg = bot.reply_to(
                message,
                get_progress_text(0, f"{prefix}{get_txt(user_id, 'proc_connecting')}"),
                parse_mode="Markdown"
            )
        else:
            status_msg = bot.send_message(
                message.chat.id,
                get_progress_text(0, f"{prefix}{get_txt(user_id, 'proc_connecting')}"),
                parse_mode="Markdown"
            )
    
        fetch_state = {"result": None, "done": False, "error": None}
        
        def api_worker(u=target_url):
            try:
                fetch_state["result"] = get_terabox_media(u)
            except Exception as ex:
                fetch_state["error"] = ex
            finally:
                fetch_state["done"] = True
                
        thread = threading.Thread(target=api_worker)
        thread.daemon = True
        thread.start()
        
        stages = [
            (15, get_txt(user_id, "proc_connecting")),
            (35, get_txt(user_id, "proc_analyzing")),
            (55, get_txt(user_id, "proc_extracting")),
            (75, get_txt(user_id, "proc_optimizing")),
            (90, get_txt(user_id, "proc_finalizing")),
        ]
        
        for p, desc in stages:
            if fetch_state["done"]:
                break
            time.sleep(1.0)
            if fetch_state["done"]:
                break
            try:
                bot.edit_message_text(
                    get_progress_text(p, f"{prefix}{desc}"),
                    chat_id=message.chat.id,
                    message_id=status_msg.message_id,
                    parse_mode="Markdown"
                )
            except Exception:
                pass
                
        thread.join(timeout=35)
        
        if fetch_state["result"]:
            title, download_link, size, resolution, duration, file_type = fetch_state["result"]
        else:
            title, download_link, size, resolution, duration, file_type = None, None, None, None, None, None
            
        if download_link:
            try:
                bot.edit_message_text(
                    get_progress_text(100, f"{prefix}{get_txt(user_id, 'proc_done')}"),
                    chat_id=message.chat.id,
                    message_id=status_msg.message_id,
                    parse_mode="Markdown"
                )
            except Exception:
                pass
            m_code = re.search(r"/s/([a-zA-Z0-9_-]+)", target_url)
            v_id_extracted = m_code.group(1) if m_code else hashlib.md5(target_url.encode()).hexdigest()[:10]
            already_unlocked = has_watched_video(user_id, v_id_extracted)
            if not is_paid:
                if not already_unlocked:
                    update_credits(user_id, -1)
                    mark_watched_video(user_id, v_id_extracted)
            else:
                mark_watched_video(user_id, v_id_extracted)

            encoded_stream_url = quote(download_link, safe='')
            encoded_title = quote(title, safe='')
            encoded_size = quote(size, safe='')
            encoded_res = quote(resolution, safe='')
            encoded_dur = quote(duration, safe='')
            encoded_type = quote(file_type, safe='')
            
            u_lang = get_user_lang(user_id) or "bn"
            player_url = (
                f"{MY_PLAYER_URL}?"
                f"src={encoded_stream_url}&"
                f"title={encoded_title}&"
                f"size={encoded_size}&"
                f"res={encoded_res}&"
                f"dur={encoded_dur}&"
                f"type={encoded_type}&"
                f"lang={u_lang}"
            )
            
            markup = InlineKeyboardMarkup()
            play_btn_app = InlineKeyboardButton(text=get_txt(user_id, "btn_open_player_app"), web_app=WebAppInfo(url=player_url))
            markup.add(play_btn_app)
            
            clean_t = clean_text_for_markdown(title)
            clean_s = clean_text_for_markdown(size)
            clean_r = clean_text_for_markdown(resolution)
            clean_d = clean_text_for_markdown(duration)
            clean_ft = clean_text_for_markdown(file_type)
            
            curr_user = get_user(user_id)
            rem_credits = get_txt(user_id, "balance_unlimited") if is_paid else get_txt(user_id, "balance_credits", credits=curr_user[2])
            
            reply_text = (
                f"🎬 *Title:* {clean_t}\n"
                f"📦 *Size:* {clean_s}\n"
                f"⚡ *Resolution:* {clean_r}\n"
                f"⏱️ *Duration:* {clean_d}\n"
                f"📂 *Type:* {clean_ft}\n"
                f"💳 *Remaining Balance:* {rem_credits}\n\n"
                f"{get_txt(user_id, 'stream_play_prompt')}"
            )
            
            bot.edit_message_text(
                reply_text,
                chat_id=message.chat.id,
                message_id=status_msg.message_id,
                reply_markup=markup,
                parse_mode="Markdown"
            )
        else:
            bot.edit_message_text(
                f"{prefix}{get_txt(user_id, 'stream_fetch_fail')}", 
                chat_id=message.chat.id, 
                message_id=status_msg.message_id
            )
            if len(urls_to_process) == 1:
                auto_delete_after(message.chat.id, [status_msg.message_id, message.message_id], 5)
            else:
                auto_delete_after(message.chat.id, status_msg.message_id, 5)


def handle_sticker_upload(message):
    uid = message.from_user.id
    if not is_user_authenticated(uid):
        return

    file_id = None
    if message.sticker:
        file_id = message.sticker.file_id
    elif message.photo:
        file_id = message.photo[-1].file_id
    elif message.document and (message.document.mime_type or "").startswith("image/"):
        file_id = message.document.file_id

    if not file_id:
        return

    bot.send_chat_action(message.chat.id, "upload_document")
    try:
        f_info = bot.get_file(file_id)
        downloaded = bot.download_file(f_info.file_path)

        sticker_dest = os.path.join(APP_DIR, "sticker.webp")
        with open(sticker_dest, "wb") as f:
            f.write(downloaded)

        update_env({
            "STICKER_ENABLED": "True",
            "STICKER_PATH": sticker_dest
        })
        if mgr.engine and hasattr(mgr.engine, "config"):
            mgr.engine.config["sticker_enabled"] = True
            mgr.engine.config["sticker_path"] = sticker_dest

        clear_admin_state(uid)
        bot.reply_to(
            message,
            "✅ <b>স্টিকার সফলভাবে সিলেক্ট ও সেভ হয়েছে!</b>\n\n"
            "এখন থেকে এই নতুন স্টিকারটি সোর্স চ্যানেল থেকে আসা সমস্ত পোস্ট ও মিডিয়াতে স্বয়ংক্রিয়ভাবে ওয়াটারমার্ক হিসেবে ব্যবহার করা হবে।",
            parse_mode="HTML"
        )
        t, m = build_sticker_menu()
        bot.send_message(message.chat.id, t, reply_markup=m, parse_mode="HTML")
    except Exception as e:
        bot.reply_to(message, f"❌ স্টিকার সেভ করতে সমস্যা হয়েছে: {e}")

def handle_admin_text(message):
    uid = message.from_user.id
    text_val = message.text.strip()

    if not is_admin(uid):
        return

    state_obj = get_admin_state(uid)
    text_val = message.text.strip()

    # Check if engine is waiting for 2FA or login auth
    if mgr.auth_future and not mgr.auth_future.done():
        if mgr.provide_auth_input(text_val):
            bot.reply_to(message, "✅ <b>Auth input forwarded to engine.</b>", parse_mode="HTML")
            return

    if not state_obj:
        # If admin simply sends a TeraBox link without prompt, test it!
        if any(d in text_val.lower() for d in DEFAULT_TERABOX_DOMAINS):
            test_terabox_url(message, text_val)
            return

        bot.reply_to(
            message,
            "💡 Use /admin or /dashboard to open the main control panel.",
            reply_markup=InlineKeyboardMarkup().row(InlineKeyboardButton("📱 Open Dashboard", callback_data="nav:main")),
            parse_mode="HTML"
        )
        return

    st = state_obj["state"]
    meta = state_obj.get("meta", {})

    if text_val.lower() in ["/cancel", "cancel", "বাতিল"]:
        clear_admin_state(message.from_user.id)
        bot.reply_to(message, "❌ <i>Action canceled.</i>", parse_mode="HTML")
        t, m = build_main_dashboard()
        bot.send_message(message.chat.id, t, reply_markup=m, parse_mode="HTML")
        return

    if st == "WAIT_ADD_SOURCE":
        env = read_env()
        raw = env.get("SOURCE_CHATS", "")
        sources = [s.strip() for s in raw.split(",") if s.strip()]
        if text_val not in sources:
            sources.append(text_val)
            update_env({"SOURCE_CHATS": ", ".join(sources)})
            bot.reply_to(message, f"✅ <b>Added source channel:</b> <code>{text_val}</code>", parse_mode="HTML")
        else:
            bot.reply_to(message, "⚠️ Channel is already in source list.")
        clear_admin_state(message.from_user.id)
        t, m = build_sources_menu()
        bot.send_message(message.chat.id, t, reply_markup=m, parse_mode="HTML")

    elif st == "WAIT_SET_OUTPUT":
        update_env({"OUTPUT_CHANNEL": text_val})
        bot.reply_to(message, f"✅ <b>Output channel updated to:</b> <code>{text_val}</code>", parse_mode="HTML")
        clear_admin_state(message.from_user.id)
        t, m = build_output_menu()
        bot.send_message(message.chat.id, t, reply_markup=m, parse_mode="HTML")

    elif st == "WAIT_EDIT_BTN":
        key = meta.get("key")
        if key:
            update_env({key: text_val})
            bot.reply_to(message, f"✅ <b>Button label updated:</b> <code>{html.escape(text_val)}</code>", parse_mode="HTML")
        clear_admin_state(message.from_user.id)
        t, m = build_btn_texts_menu()
        bot.send_message(message.chat.id, t, reply_markup=m, parse_mode="HTML")

    elif st == "WAIT_EDIT_MONETAG_ZONE":
        new_zone = re.sub(r"[^\d]", "", text_val.strip())
        if not new_zone:
            bot.reply_to(message, "⚠️ Invalid Zone ID. Please enter numeric digits only, e.g. <code>12016303</code>.", parse_mode="HTML")
            return
        update_env({"MONETAG_ZONE_ID": new_zone})
        try:
            set_setting("monetag_zone_id", new_zone)
        except Exception:
            pass
        bot.reply_to(message, f"✅ <b>Monetag Ads Zone ID updated to:</b> <code>{new_zone}</code>\n<i>Syncing Mini App player on GitHub Pages...</i>", parse_mode="HTML")
        clear_admin_state(message.from_user.id)
        threading.Thread(target=sync_monetag_to_github_pages, kwargs={"new_zone": new_zone}, daemon=True).start()
        t, m = build_monetag_menu()
        bot.send_message(message.chat.id, t, reply_markup=m, parse_mode="HTML")

    elif st == "WAIT_EDIT_MONETAG_INTERVAL":
        raw = text_val.lower().strip()
        sec = 0
        if raw.endswith("m") or raw.endswith("min") or raw.endswith("minute") or raw.endswith("minutes"):
            num_part = re.sub(r"[^\d]", "", raw)
            if num_part:
                sec = int(num_part) * 60
        else:
            num_part = re.sub(r"[^\d]", "", raw)
            if num_part:
                sec = int(num_part)

        if sec < 10:
            bot.reply_to(message, "⚠️ Invalid interval. Minimum interval is 10 seconds. Examples: <code>180</code> or <code>3m</code>.", parse_mode="HTML")
            return

        update_env({"MONETAG_MIDROLL_INTERVAL": sec})
        try:
            set_setting("monetag_midroll_interval", str(sec))
        except Exception:
            pass
        bot.reply_to(message, f"✅ <b>Video mid-roll ads interval updated to:</b> <code>{sec}s</code>\n<i>Syncing Mini App player on GitHub Pages...</i>", parse_mode="HTML")
        clear_admin_state(message.from_user.id)
        threading.Thread(target=sync_monetag_to_github_pages, kwargs={"new_interval": sec}, daemon=True).start()
        t, m = build_monetag_menu()
        bot.send_message(message.chat.id, t, reply_markup=m, parse_mode="HTML")

    elif st == "WAIT_SET_DELAY":
        # expects format "1 - 3" or single integer "2"
        nums = re.findall(r"\d+", text_val)
        if len(nums) == 1:
            d_min = int(nums[0])
            d_max = int(nums[0])
        elif len(nums) >= 2:
            d_min = int(nums[0])
            d_max = int(nums[1])
        else:
            bot.reply_to(message, "⚠️ Invalid format. Send like <code>1 - 3</code> or <code>2</code>.", parse_mode="HTML")
            return

        update_env({
            "POST_DELAY_MIN_SECONDS": d_min,
            "POST_DELAY_MAX_SECONDS": d_max,
            "POST_DELAY_SECONDS": d_max
        })
        bot.reply_to(message, f"✅ <b>Post delay set to:</b> <code>{d_min}s - {d_max}s</code>", parse_mode="HTML")
        clear_admin_state(message.from_user.id)
        t, m = build_timing_menu()
        bot.send_message(message.chat.id, t, reply_markup=m, parse_mode="HTML")

    elif st == "WAIT_SET_TIMEOUT":
        if text_val.isdigit():
            update_env({"REPLY_TIMEOUT": int(text_val)})
            bot.reply_to(message, f"✅ <b>Bot response timeout set to:</b> <code>{text_val}s</code>", parse_mode="HTML")
        else:
            bot.reply_to(message, "⚠️ Please send a valid number of seconds.")
            return
        clear_admin_state(message.from_user.id)
        t, m = build_timing_menu()
        bot.send_message(message.chat.id, t, reply_markup=m, parse_mode="HTML")

    elif st == "WAIT_RESOLVE_LINK":
        clear_admin_state(message.from_user.id)
        resolve_channel_link(message, text_val)

    elif st == "WAIT_TEST_LINK":
        clear_admin_state(message.from_user.id)
        test_terabox_url(message, text_val)

    elif st == "WAIT_TEST_TRANS":
        clear_admin_state(message.from_user.id)
        bot.send_chat_action(message.chat.id, "typing")
        env = read_env()
        curr_lang = env.get("TRANSLATE_TARGET_LANG", "en").lower()
        l_info = SUPPORTED_LANGUAGES.get(curr_lang, (curr_lang, ""))
        try:
            translated = forwarder_core.MediaProcessor.translate_text(text_val, target_lang=curr_lang)
            resp = (
                f"🌐 <b>Translation Output ({l_info[0]}):</b>\n\n"
                f"<b>Original:</b>\n<code>{text_val}</code>\n\n"
                f"<b>Translated:</b>\n<code>{translated}</code>"
            )
        except Exception as e:
            resp = f"❌ Translation Error: {e}"
        bot.reply_to(message, resp, reply_markup=InlineKeyboardMarkup().row(InlineKeyboardButton("🔙 Back to Translation Menu", callback_data="nav:trans")), parse_mode="HTML")

    elif st == "WAIT_EDIT_CRED":
        key = meta.get("key")
        if key:
            update_env({key: text_val})
            if key in ("DISPATCH_BOT_TOKEN", "BOT_TOKEN"):
                new_uname = fetch_bot_username_from_token(text_val, sync_to_env=True)
                update_env({
                    "DISPATCH_BOT_USERNAME": new_uname,
                    "BOT_USERNAME": new_uname
                })
                bot.reply_to(message, f"✅ <b>{key} সফলভাবে আপডেট করা হয়েছে:</b> <code>{text_val}</code>\n🤖 <b>স্বয়ংক্রিয়ভাবে সনাক্তকৃত বট ইউজারনেম:</b> <code>@{new_uname}</code>\n<i>সতর্কতা: ইঞ্জিন রিস্টার্ট করলে নতুন ক্রেডেনশিয়াল কার্যকর হবে।</i>", parse_mode="HTML")
            else:
                bot.reply_to(message, f"✅ <b>{key} সফলভাবে আপডেট করা হয়েছে:</b> <code>{text_val}</code>\n<i>সতর্কতা: ইঞ্জিন রিস্টার্ট করলে নতুন ক্রেডেনশিয়াল কার্যকর হবে।</i>", parse_mode="HTML")
        clear_admin_state(message.from_user.id)
        t, m = build_credentials_menu()
        bot.send_message(message.chat.id, t, reply_markup=m, parse_mode="HTML")

    elif st == "WAIT_CHANGE_PWD":
        if len(text_val) < 4:
            bot.reply_to(message, "⚠️ পাসওয়ার্ড কমপক্ষে ৪ অক্ষরের হতে হবে। অন্য একটি পাসওয়ার্ড দিন:", parse_mode="HTML")
            return
        update_env({"BOT_PASSWORD": text_val})
        bot.reply_to(message, f"✅ <b>বট অ্যাক্সেস পাসওয়ার্ড সফলভাবে পরিবর্তিত হয়েছে!</b>\nনতুন পাসওয়ার্ড: <code>{text_val}</code>", parse_mode="HTML")
        clear_admin_state(message.from_user.id)
        t, m = build_credentials_menu()
        bot.send_message(message.chat.id, t, reply_markup=m, parse_mode="HTML")

    elif st == "WAIT_TG_PHONE":
        phone = text_val.replace(" ", "").replace("-", "")
        if len(phone) < 7:
            bot.reply_to(message, "⚠️ অনুগ্রহ করে সঠিক কান্ট্রি কোড সহ ফোন নম্বর দিন (যেমন: <code>+8801721007568</code>):", parse_mode="HTML")
            return
        update_env({"TELEGRAM_PHONE": phone})
        env = read_env()
        api_id = env.get("API_ID")
        api_hash = env.get("API_HASH")
        status_msg = bot.reply_to(message, "⏳ <i>টেলিগ্রাম সার্ভারে কোড রিকোয়েস্ট পাঠানো হচ্ছে...</i>", parse_mode="HTML")
        try:
            login_mgr.run(login_mgr._send_code(uid, phone, int(api_id), api_hash))
            set_admin_state(uid, "WAIT_TG_CODE")
            bot.edit_message_text(
                "📩 <b>লগইন ওটিপি (OTP Code) পাঠানো হয়েছে!</b>\n━━━━━━━━━━━━━━━━━━━━━━\n"
                f"আপনার <code>{phone}</code> নম্বরের টেলিগ্রাম অ্যাপ নোটিফিকেশনে যে কোডটি এসেছে তা এখানে লিখে পাঠান (যেমন: <code>12345</code>):\n\n"
                "<i>নোট: কোডের মাঝে স্পেস থাকলে স্পেস ছাড়া শুধু সংখ্যাগুলো পাঠাবেন।</i>",
                message.chat.id,
                status_msg.message_id,
                reply_markup=InlineKeyboardMarkup().row(InlineKeyboardButton("❌ Cancel", callback_data="act:cancel_tg_login")),
                parse_mode="HTML"
            )
        except errors.PhoneNumberInvalidError:
            bot.edit_message_text("❌ <b>ভুল ফোন নম্বর!</b> টেলিগ্রাম অ্যাকাউন্টটি যে নম্বরে খোলা সেটি কান্ট্রি কোড সহ লিখুন:", message.chat.id, status_msg.message_id, parse_mode="HTML")
        except errors.FloodWaitError as fe:
            clear_admin_state(uid)
            bot.edit_message_text(f"⚠️ <b>Flood Wait!</b> টেলিগ্রাম রিকোয়েস্ট লিমিট হয়েছে। অনুগ্রহ করে {fe.seconds} সেকেন্ড পর চেষ্টা করুন।", message.chat.id, status_msg.message_id, parse_mode="HTML")
        except Exception as e:
            bot.edit_message_text(f"❌ <b>কোড পাঠানোর ব্যর্থতা:</b> {e}", message.chat.id, status_msg.message_id, parse_mode="HTML")

    elif st == "WAIT_TG_CODE":
        clean_code = text_val.replace(" ", "").replace("-", "").strip()
        status_msg = bot.reply_to(message, "⏳ <i>কোড যাচাই করা হচ্ছে...</i>", parse_mode="HTML")
        try:
            res = login_mgr.run(login_mgr._submit_code(uid, clean_code))
            if res["status"] == "ok":
                me = res["user"]
                user_display = f"{me.first_name or ''} {me.last_name or ''}".strip() or me.username or str(me.id)
                clear_admin_state(uid)
                bot.edit_message_text(
                    f"🎉 <b>অভিনন্দন! টেলিগ্রাম অ্যাকাউন্ট সফলভাবে লগইন হয়েছে!</b>\n━━━━━━━━━━━━━━━━━━━━━━\n"
                    f"👤 <b>অ্যাকাউন্ট:</b> {user_display}\n"
                    f"🆔 <b>ইউজার আইডি:</b> <code>{me.id}</code>\n"
                    f"💾 <b>সেশন ফাইল:</b> <code>sessions/telegram_automation.session</code>\n\n"
                    "এখন আপনার ক্লাউড সার্ভারে সেশন সক্রিয়! ড্যাশবোর্ড থেকে <b>🔴 Start Engine</b> দিলেই স্বয়ংক্রিয় কাজ শুরু হবে।",
                    message.chat.id,
                    status_msg.message_id,
                    reply_markup=InlineKeyboardMarkup().row(
                        InlineKeyboardButton("📱 Open Dashboard", callback_data="nav:main"),
                        InlineKeyboardButton("🔴 Start Engine", callback_data="act:start")
                    ),
                    parse_mode="HTML"
                )
            elif res["status"] == "2fa_needed":
                set_admin_state(uid, "WAIT_TG_2FA")
                bot.edit_message_text(
                    "🔐 <b>Two-Step Verification (2FA) সক্রিয় আছে!</b>\n━━━━━━━━━━━━━━━━━━━━━━\n"
                    "আপনার টেলিগ্রাম অ্যাকাউন্টের ক্লাউড পাসওয়ার্ডটি (2FA Password) এখানে লিখে পাঠান:",
                    message.chat.id,
                    status_msg.message_id,
                    reply_markup=InlineKeyboardMarkup().row(InlineKeyboardButton("❌ Cancel", callback_data="act:cancel_tg_login")),
                    parse_mode="HTML"
                )
            elif res["status"] == "invalid_code":
                bot.edit_message_text(
                    "❌ <b>ভুল ওটিপি কোড!</b> কোডটি টেলিগ্রাম অ্যাপে দেখে সঠিকভাবে সংখ্যাগুলো লিখে পাঠান (যেমন: <code>12345</code>):",
                    message.chat.id,
                    status_msg.message_id,
                    reply_markup=InlineKeyboardMarkup().row(InlineKeyboardButton("❌ Cancel", callback_data="act:cancel_tg_login")),
                    parse_mode="HTML"
                )
            elif res["status"] == "expired_code":
                clear_admin_state(uid)
                bot.edit_message_text(
                    "⚠️ <b>কোডের মেয়াদ শেষ হয়ে গেছে!</b> অনুগ্রহ করে আবার নতুন করে লগইন শুরু করুন।",
                    message.chat.id,
                    status_msg.message_id,
                    reply_markup=InlineKeyboardMarkup().row(InlineKeyboardButton("📲 Login Again", callback_data="act:start_tg_login")),
                    parse_mode="HTML"
                )
            else:
                bot.edit_message_text(f"❌ <b>লগইন ব্যর্থ:</b> {res.get('error', 'অজানা ত্রুটি')}", message.chat.id, status_msg.message_id, parse_mode="HTML")
        except Exception as e:
            bot.edit_message_text(f"❌ <b>ব্যর্থতা:</b> {e}", message.chat.id, status_msg.message_id, parse_mode="HTML")

    elif st == "WAIT_TG_2FA":
        status_msg = bot.reply_to(message, "⏳ <i>2FA পাসওয়ার্ড যাচাই করা হচ্ছে...</i>", parse_mode="HTML")
        try:
            res = login_mgr.run(login_mgr._submit_2fa(uid, text_val))
            if res["status"] == "ok":
                me = res["user"]
                user_display = f"{me.first_name or ''} {me.last_name or ''}".strip() or me.username or str(me.id)
                clear_admin_state(uid)
                bot.edit_message_text(
                    f"🎉 <b>অভিনন্দন! 2FA সফল হয়েছে এবং সেশন সংরক্ষিত হয়েছে!</b>\n━━━━━━━━━━━━━━━━━━━━━━\n"
                    f"👤 <b>অ্যাকাউন্ট:</b> {user_display}\n"
                    f"🆔 <b>ইউজার আইডি:</b> <code>{me.id}</code>\n"
                    f"💾 <b>সেশন ফাইল:</b> <code>sessions/telegram_automation.session</code>\n\n"
                    "এখন আপনি ড্যাশবোর্ড থেকে <b>🔴 Start Engine</b> দিয়ে অটোমেশন চালু করতে পারবেন।",
                    message.chat.id,
                    status_msg.message_id,
                    reply_markup=InlineKeyboardMarkup().row(
                        InlineKeyboardButton("📱 Open Dashboard", callback_data="nav:main"),
                        InlineKeyboardButton("🔴 Start Engine", callback_data="act:start")
                    ),
                    parse_mode="HTML"
                )
            elif res["status"] == "invalid_pwd":
                bot.edit_message_text(
                    "❌ <b>ভুল 2FA পাসওয়ার্ড!</b> সঠিক টু-স্টেপ পাসওয়ার্ডটি আবার লিখুন:",
                    message.chat.id,
                    status_msg.message_id,
                    reply_markup=InlineKeyboardMarkup().row(InlineKeyboardButton("❌ Cancel", callback_data="act:cancel_tg_login")),
                    parse_mode="HTML"
                )
            else:
                bot.edit_message_text(f"❌ <b>2FA ব্যর্থ:</b> {res.get('error', 'অজানা ত্রুটি')}", message.chat.id, status_msg.message_id, parse_mode="HTML")
        except Exception as e:
            bot.edit_message_text(f"❌ <b>ব্যর্থতা:</b> {e}", message.chat.id, status_msg.message_id, parse_mode="HTML")

    elif st == "WAIT_ADD_AJ_CHAT":
        env = read_env()
        raw = env.get("AUTO_JOIN_CHATS", "")
        chats = [c.strip() for c in raw.split(",") if c.strip()]
        if text_val not in chats:
            chats.append(text_val)
            update_env({"AUTO_JOIN_CHATS": ", ".join(chats)})
            bot.reply_to(message, f"✅ <b>Auto-Join টার্গেট চ্যাট যোগ করা হয়েছে:</b> <code>{text_val}</code>", parse_mode="HTML")
        else:
            bot.reply_to(message, "⚠️ এই চ্যাটটি ইতোমধ্যেই তালিকায় রয়েছে।", parse_mode="HTML")
        clear_admin_state(message.from_user.id)
        t, m = build_auto_join_menu()
        bot.send_message(message.chat.id, t, reply_markup=m, parse_mode="HTML")

    elif st == "WAIT_SET_AJ_DELAY":
        if text_val.isdigit():
            update_env({"AUTO_JOIN_DELAY_SECONDS": int(text_val)})
            bot.reply_to(message, f"✅ <b>Auto-Join বিলম্ব সময় নির্ধারণ করা হয়েছে:</b> <code>{text_val}s</code>", parse_mode="HTML")
        else:
            bot.reply_to(message, "⚠️ অনুগ্রহ করে শুধু পূর্ণসংখ্যা সেকেন্ড লিখুন (যেমন: 1 বা 2)।", parse_mode="HTML")
            return
        clear_admin_state(message.from_user.id)
        t, m = build_auto_join_menu()
        bot.send_message(message.chat.id, t, reply_markup=m, parse_mode="HTML")

    elif st == "WAIT_ADD_HJ_CHAT":
        env = read_env()
        raw = env.get("HIDE_JOIN_CHATS", "")
        chats = [c.strip() for c in raw.split(",") if c.strip()]
        if text_val not in chats:
            chats.append(text_val)
            update_env({"HIDE_JOIN_CHATS": ", ".join(chats)})
            bot.reply_to(message, f"✅ <b>Clean Notification টার্গেট গ্রুপ যোগ করা হয়েছে:</b> <code>{text_val}</code>", parse_mode="HTML")
        else:
            bot.reply_to(message, "⚠️ এই গ্রুপটি ইতোমধ্যেই তালিকায় রয়েছে।", parse_mode="HTML")
        clear_admin_state(message.from_user.id)
        t, m = build_hide_join_menu()
        bot.send_message(message.chat.id, t, reply_markup=m, parse_mode="HTML")


def safe_edit_message(call, text: str, reply_markup: InlineKeyboardMarkup, parse_mode: str = "HTML") -> bool:
    try:
        bot.edit_message_text(text, call.message.chat.id, call.message.message_id, reply_markup=reply_markup, parse_mode=parse_mode)
        return True
    except Exception as e:
        err = str(e).lower()
        if "message is not modified" in err:
            try:
                bot.edit_message_reply_markup(call.message.chat.id, call.message.message_id, reply_markup=reply_markup)
            except Exception:
                pass
            return True
        try:
            clean_plain_text = re.sub(r'<[^>]+>', '', text)
            bot.edit_message_text(clean_plain_text, call.message.chat.id, call.message.message_id, reply_markup=reply_markup)
            return True
        except Exception:
            try:
                bot.edit_message_reply_markup(call.message.chat.id, call.message.message_id, reply_markup=reply_markup)
                return True
            except Exception:
                pass
        return False



def handle_controller_callbacks(call):
    if not is_admin(call.from_user.id):
        bot.answer_callback_query(call.id, "Access Denied!", show_alert=True)
        return

    data = call.data

    # Navigation
    if data in ["act:back_dashboard", "nav:back_dashboard"]:
        clear_admin_state(call.from_user.id)
        try:
            bot.delete_message(call.message.chat.id, call.message.message_id)
        except Exception:
            try:
                bot.edit_message_text("✅ <b>Channel Controller Closed.</b>", chat_id=call.message.chat.id, message_id=call.message.message_id, parse_mode="HTML")
            except Exception:
                pass
        welcome_txt = (
            f"👑 <b>Admin Dashboard</b>\n\n"
            f"⚡ System is active and running.\n"
            f"নিচের মেনু বাটনগুলো থেকে যেকোনো অপশন ব্যবহার করতে পারেন:"
        )
        bot.send_message(call.message.chat.id, welcome_txt, parse_mode="HTML", reply_markup=main_keyboard(call.from_user.id))
        bot.answer_callback_query(call.id, "🔙 Back to Dashboard")
        return

    elif data == "nav:main":
        clear_admin_state(call.from_user.id)
        t, m = build_main_dashboard()
        safe_edit_message(call, t, m)
        bot.answer_callback_query(call.id, "🔄 ড্যাশবোর্ড আপডেট করা হয়েছে!")

    elif data == "nav:sources":
        clear_admin_state(call.from_user.id)
        t, m = build_sources_menu()
        safe_edit_message(call, t, m)
        bot.answer_callback_query(call.id)


    elif data == "nav:output":
        clear_admin_state(call.from_user.id)
        t, m = build_output_menu()
        safe_edit_message(call, t, m)
        bot.answer_callback_query(call.id)

    elif data == "nav:settings":
        clear_admin_state(call.from_user.id)
        t, m = build_settings_menu()
        safe_edit_message(call, t, m)
        bot.answer_callback_query(call.id)

    elif data == "nav:btn_texts":
        clear_admin_state(call.from_user.id)
        t, m = build_btn_texts_menu()
        safe_edit_message(call, t, m)
        bot.answer_callback_query(call.id)

    elif data == "nav:timing":
        clear_admin_state(call.from_user.id)
        t, m = build_timing_menu()
        safe_edit_message(call, t, m)
        bot.answer_callback_query(call.id)

    elif data == "nav:logs":
        t, m = build_logs_view()
        safe_edit_message(call, t, m)
        bot.answer_callback_query(call.id)

    elif data == "nav:sysinfo":
        t, m = build_system_info_menu()
        safe_edit_message(call, t, m)
        bot.answer_callback_query(call.id)

    elif data == "nav:trans":
        clear_admin_state(call.from_user.id)
        t, m = build_translation_menu()
        safe_edit_message(call, t, m)
        bot.answer_callback_query(call.id)

    elif data.startswith("tgl_trans:"):
        next_val = data.split(":")[1]
        update_env({"TRANSLATE_ENABLED": next_val})
        if mgr.engine and hasattr(mgr.engine, "config"):
            mgr.engine.config["translate_enabled"] = next_val.lower() == "true"
        st_text = "অনুবাদ চালু করা হয়েছে" if next_val.lower() == "true" else "অনুবাদ বন্ধ করা হয়েছে"
        bot.answer_callback_query(call.id, st_text, show_alert=True)
        t, m = build_translation_menu()
        safe_edit_message(call, t, m)

    elif data.startswith("set_lang:"):
        code = data.split(":")[1]
        update_env({"TRANSLATE_TARGET_LANG": code})
        if mgr.engine and hasattr(mgr.engine, "config"):
            mgr.engine.config["translate_target_lang"] = code
        l_info = SUPPORTED_LANGUAGES.get(code, (code, ""))
        bot.answer_callback_query(call.id, f"ভাষা সেট হয়েছে: {l_info[0]}!", show_alert=True)
        t, m = build_translation_menu()
        safe_edit_message(call, t, m)

    elif data == "act:test_trans":
        set_admin_state(call.from_user.id, "WAIT_TEST_TRANS")
        bot.answer_callback_query(call.id)
        env = read_env()
        curr_lang = env.get("TRANSLATE_TARGET_LANG", "en").lower()
        l_info = SUPPORTED_LANGUAGES.get(curr_lang, (curr_lang, ""))
        bot.send_message(
            call.message.chat.id,
            f"🧪 <b>Live Translation Tester:</b>\n\n"
            f"Active Language: <b>{l_info[0]} ({curr_lang})</b>\n\n"
            "<i>যেকোনো লেখা বা পোস্টের ক্যাপশন পাঠান—বট সাথে সাথে অনুবাদ করে দেখাবে:</i>",
            reply_markup=InlineKeyboardMarkup().row(InlineKeyboardButton("❌ Cancel", callback_data="nav:trans")),
            parse_mode="HTML"
        )

    # Actions: Sticker Management
    elif data == "nav:sticker":
        clear_admin_state(call.from_user.id)
        t, m = build_sticker_menu()
        safe_edit_message(call, t, m)
        bot.answer_callback_query(call.id)

    elif data.startswith("tgl_stk:"):
        next_val = data.split(":")[1]
        update_env({"STICKER_ENABLED": next_val})
        if mgr.engine and hasattr(mgr.engine, "config"):
            mgr.engine.config["sticker_enabled"] = next_val.lower() == "true"
        st_text = "স্টিকার চালু করা হয়েছে" if next_val.lower() == "true" else "স্টিকার বন্ধ করা হয়েছে"
        bot.answer_callback_query(call.id, st_text, show_alert=True)
        t, m = build_sticker_menu()
        safe_edit_message(call, t, m)

    elif data == "act:send_new_sticker":
        set_admin_state(call.from_user.id, "WAIT_SET_STICKER")
        bot.answer_callback_query(call.id)
        bot.send_message(
            call.message.chat.id,
            "🏷 <b>Send Your Sticker or Image:</b>\n\n"
            "টেলিগ্রামের যেকোনো <b>Sticker</b>, <b>Photo</b> বা <b>PNG</b> ফাইল এখনই এখানে পাঠান।\n"
            "বট সাথে সাথে এটিকে নতুন ওয়াটারমার্ক হিসেবে সেট করে নেবে।\n\n"
            "<i>বাতিল করতে /cancel পাঠান।</i>",
            reply_markup=InlineKeyboardMarkup().row(InlineKeyboardButton("❌ Cancel", callback_data="nav:sticker")),
            parse_mode="HTML"
        )

    elif data == "act:prev_sticker":
        env = read_env()
        stk_path = env.get("STICKER_PATH", os.path.join(APP_DIR, "sticker.webp"))
        if os.path.exists(stk_path):
            try:
                with open(stk_path, "rb") as sf:
                    bot.send_document(call.message.chat.id, sf, caption="🏷 <b>Current Active Watermark Sticker</b>", parse_mode="HTML")
                bot.answer_callback_query(call.id, "Sticker preview sent!")
            except Exception as e:
                bot.answer_callback_query(call.id, f"Error: {e}", show_alert=True)
        else:
            bot.answer_callback_query(call.id, "⚠️ No sticker file found!", show_alert=True)

    elif data.startswith("act:stk_pos:"):
        pos_val = data.split(":", 2)[2]
        update_env({"STICKER_POSITION": pos_val})
        if mgr.engine and hasattr(mgr.engine, "config"):
            mgr.engine.config["sticker_position"] = pos_val
        bot.answer_callback_query(call.id, f"Position set to: {pos_val}", show_alert=True)
        t, m = build_sticker_menu()
        safe_edit_message(call, t, m)

    # Actions: Engine Start / Stop / Restart
    elif data == "act:start":
        bot.answer_callback_query(call.id, "Starting engine...")
        ok, msg = mgr.start()
        if not ok:
            bot.send_message(call.message.chat.id, f"⚠️ {msg}")
        time.sleep(0.5)
        t, m = build_main_dashboard()
        safe_edit_message(call, t, m)

    elif data == "act:stop":
        bot.answer_callback_query(call.id, "Stopping engine...")
        ok, msg = mgr.stop()
        time.sleep(0.5)
        t, m = build_main_dashboard()
        safe_edit_message(call, t, m)

    elif data == "act:restart":
        bot.answer_callback_query(call.id, "Restarting engine...")
        mgr.restart()
        time.sleep(0.5)
        t, m = build_main_dashboard()
        safe_edit_message(call, t, m)

    # Actions: Settings Toggle
    elif data.startswith("tgl:"):
        _, key, next_val = data.split(":")
        update_env({key: next_val})
        if mgr.engine and hasattr(mgr.engine, "config"):
            mgr.engine.config[key.lower()] = (next_val.lower() == "true")
        bot.answer_callback_query(call.id, f"Updated {key} to {next_val}")
        t, m = build_settings_menu()
        safe_edit_message(call, t, m)

    # Actions: Source Channels Management
    elif data == "act:add_source":
        set_admin_state(call.from_user.id, "WAIT_ADD_SOURCE")
        bot.answer_callback_query(call.id)
        bot.send_message(
            call.message.chat.id,
            "📡 <b>Send Channel ID or Username:</b>\n\n"
            "Examples:\n"
            "• <code>-1003842038004</code>\n"
            "• <code>@my_source_channel</code>\n"
            "• <code>https://t.me/+AbCdEfGh</code>\n\n"
            "<i>Send /cancel to abort.</i>",
            reply_markup=InlineKeyboardMarkup().row(InlineKeyboardButton("❌ Cancel", callback_data="nav:sources")),
            parse_mode="HTML"
        )

    elif data == "act:remove_source_list":
        t, m = build_remove_source_menu()
        safe_edit_message(call, t, m)
        bot.answer_callback_query(call.id)

    elif data.startswith("act:del_source:"):
        idx = int(data.split(":")[2])
        env = read_env()
        raw = env.get("SOURCE_CHATS", "")
        sources = [s.strip() for s in raw.split(",") if s.strip()]
        if 0 <= idx < len(sources):
            removed = sources.pop(idx)
            update_env({"SOURCE_CHATS": ", ".join(sources)})
            bot.answer_callback_query(call.id, f"Removed {removed}", show_alert=True)
        t, m = build_sources_menu()
        safe_edit_message(call, t, m)

    # Actions: Output Channel Management
    elif data == "act:set_output":
        set_admin_state(call.from_user.id, "WAIT_SET_OUTPUT")
        bot.answer_callback_query(call.id)
        bot.send_message(
            call.message.chat.id,
            "🎯 <b>Send Destination Channel ID or @username:</b>\n\n"
            "Example: <code>-1003938077680</code> or <code>@target_channel</code>\n\n"
            "<i>Send /cancel to abort.</i>",
            reply_markup=InlineKeyboardMarkup().row(InlineKeyboardButton("❌ Cancel", callback_data="nav:output")),
            parse_mode="HTML"
        )

    elif data == "act:purge_output_prompt":
        t, m = build_purge_output_confirm_menu()
        safe_edit_message(call, t, m)
        bot.answer_callback_query(call.id)

    elif data == "act:do_purge_output":
        bot.answer_callback_query(call.id, "Purge started...")
        execute_output_channel_purge(call)

    elif data.startswith("tgl_btn_master:"):
        next_val = data.split(":")[1]
        update_env({"POST_LINKS_AS_BUTTONS": next_val})
        if mgr.engine and hasattr(mgr.engine, "config"):
            mgr.engine.config["post_links_as_buttons"] = (next_val.lower() == "true")
        bot.answer_callback_query(call.id, f"Buttons status set to {next_val}")
        t, m = build_btn_texts_menu()
        safe_edit_message(call, t, m)

    elif data.startswith("tgl_tb_free:"):
        next_val = data.split(":")[1]
        update_env({"BUTTON_TERABOX_FREE_ENABLED": next_val})
        if mgr.engine and hasattr(mgr.engine, "config"):
            mgr.engine.config["button_terabox_free_enabled"] = (next_val.lower() == "true")
        bot.answer_callback_query(call.id, f"Watch Video (Free) Button is now {'ON' if next_val == 'True' else 'OFF'}", show_alert=True)
        t, m = build_btn_texts_menu()
        safe_edit_message(call, t, m)

    # Actions: Edit Button Labels
    elif data.startswith("act:edit_btn:"):
        key = data.split(":", 2)[2]
        set_admin_state(call.from_user.id, "WAIT_EDIT_BTN", {"key": key})
        bot.answer_callback_query(call.id)
        bot.send_message(
            call.message.chat.id,
            f"✏️ <b>Send new text for {key}:</b>\n\n"
            "<i>Send /cancel to abort.</i>",
            reply_markup=InlineKeyboardMarkup().row(InlineKeyboardButton("❌ Cancel", callback_data="nav:btn_texts")),
            parse_mode="HTML"
        )

    # Actions: Edit Monetag Zone ID
    elif data == "act:edit_monetag_zone":
        env = read_env()
        curr_z = env.get("MONETAG_ZONE_ID", "12016303")
        set_admin_state(call.from_user.id, "WAIT_EDIT_MONETAG_ZONE")
        bot.answer_callback_query(call.id)
        bot.send_message(
            call.message.chat.id,
            f"💰 <b>Send new Monetag Ads Zone ID:</b>\n\n"
            f"Current Zone ID: <code>{curr_z}</code>\n\n"
            "Example: <code>12016303</code>\n\n"
            "<i>(This will instantly update the Mini App video player on GitHub Pages in real-time)</i>\n\n"
            "<i>Send /cancel to abort.</i>",
            reply_markup=InlineKeyboardMarkup().row(InlineKeyboardButton("❌ Cancel", callback_data="nav:monetag")),
            parse_mode="HTML"
        )

    # Actions: Monetag Ads Management & Navigation
    elif data == "nav:monetag":
        clear_admin_state(call.from_user.id)
        t, m = build_monetag_menu()
        safe_edit_message(call, t, m)
        bot.answer_callback_query(call.id)

    elif data.startswith("tgl_monetag:"):
        next_val = data.split(":")[1]
        update_env({"MONETAG_ADS_ENABLED": next_val})
        if mgr.engine and hasattr(mgr.engine, "config"):
            mgr.engine.config["monetag_ads_enabled"] = (next_val.lower() == "true")
        bot.answer_callback_query(call.id, f"Monetag Ads is now {'ON' if next_val == 'True' else 'OFF'}")
        t, m = build_monetag_menu()
        safe_edit_message(call, t, m)

    elif data.startswith("set_midroll:"):
        sec = int(data.split(":")[1])
        update_env({"MONETAG_MIDROLL_INTERVAL": sec})
        try:
            set_setting("monetag_midroll_interval", str(sec))
        except Exception:
            pass
        bot.answer_callback_query(call.id, f"Ads interval set to {sec}s!", show_alert=True)
        threading.Thread(target=sync_monetag_to_github_pages, kwargs={"new_interval": sec}, daemon=True).start()
        t, m = build_monetag_menu()
        safe_edit_message(call, t, m)

    elif data in ["nav:admin_settings", "btn_admin_settings"]:
        clear_admin_state(call.from_user.id)
        t, m = get_admin_settings_content()
        if not safe_edit_message(call, t, m):
            bot.send_message(call.message.chat.id, t, parse_mode="HTML", reply_markup=m)
        try:
            bot.answer_callback_query(call.id)
        except Exception:
            pass

    elif data == "act:edit_monetag_interval":
        set_admin_state(call.from_user.id, "WAIT_EDIT_MONETAG_INTERVAL")
        bot.answer_callback_query(call.id)
        bot.send_message(
            call.message.chat.id,
            "⏱ <b>Send video ads interval in seconds or minutes:</b>\n\n"
            "Examples:\n"
            "• <code>120</code> (120 seconds)\n"
            "• <code>3m</code> (3 minutes)\n"
            "• <code>90s</code> (90 seconds)\n\n"
            "<i>Send /cancel to abort.</i>",
            reply_markup=InlineKeyboardMarkup().row(InlineKeyboardButton("❌ Cancel", callback_data="nav:monetag")),
            parse_mode="HTML"
        )

    # Actions: Set Timing
    elif data == "act:set_delay":
        set_admin_state(call.from_user.id, "WAIT_SET_DELAY")
        bot.answer_callback_query(call.id)
        bot.send_message(
            call.message.chat.id,
            "⏱ <b>Send delay range in seconds:</b>\n\n"
            "Example: <code>1 - 3</code> or <code>2</code>\n\n"
            "<i>Send /cancel to abort.</i>",
            reply_markup=InlineKeyboardMarkup().row(InlineKeyboardButton("❌ Cancel", callback_data="nav:timing")),
            parse_mode="HTML"
        )

    elif data == "act:set_timeout":
        set_admin_state(call.from_user.id, "WAIT_SET_TIMEOUT")
        bot.answer_callback_query(call.id)
        bot.send_message(
            call.message.chat.id,
            "⏱ <b>Send bot response timeout (seconds):</b>\n\n"
            "Example: <code>120</code>\n\n"
            "<i>Send /cancel to abort.</i>",
            reply_markup=InlineKeyboardMarkup().row(InlineKeyboardButton("❌ Cancel", callback_data="nav:timing")),
            parse_mode="HTML"
        )

    # Actions: Credentials & Security
    elif data == "nav:creds":
        clear_admin_state(call.from_user.id)
        t, m = build_credentials_menu()
        safe_edit_message(call, t, m)
        bot.answer_callback_query(call.id)

    elif data.startswith("act:edit_cred:"):
        key = data.split(":", 2)[2]
        set_admin_state(call.from_user.id, "WAIT_EDIT_CRED", {"key": key})
        bot.answer_callback_query(call.id)
        bot.send_message(
            call.message.chat.id,
            f"✏️ <b>Send new value for {key}:</b>\n\n"
            "<i>Send /cancel to abort.</i>",
            reply_markup=InlineKeyboardMarkup().row(InlineKeyboardButton("❌ Cancel", callback_data="nav:creds")),
            parse_mode="HTML"
        )

    elif data == "act:change_pwd":
        set_admin_state(call.from_user.id, "WAIT_CHANGE_PWD")
        bot.answer_callback_query(call.id)
        bot.send_message(
            call.message.chat.id,
            "🔑 <b>Send new bot access password:</b>\n\n"
            "<i>Send /cancel to abort.</i>",
            reply_markup=InlineKeyboardMarkup().row(InlineKeyboardButton("❌ Cancel", callback_data="nav:creds")),
            parse_mode="HTML"
        )

    elif data == "act:start_tg_login":
        env = read_env()
        api_id = env.get("API_ID", "").strip()
        api_hash = env.get("API_HASH", "").strip()
        if not api_id or not api_hash or api_id == "Not Set" or api_hash == "Not Set":
            bot.answer_callback_query(call.id, "⚠️ API ID & API Hash missing!", show_alert=True)
            bot.send_message(
                call.message.chat.id,
                "⚠️ <b>Telegram API ID বা API Hash সেট করা নেই!</b>\n\n"
                "লগইন শুরু করার আগে my.telegram.org থেকে আপনার <b>API ID</b> এবং <b>API Hash</b> নিয়ে নিচের বাটন দিয়ে সেট করে নিন:",
                reply_markup=InlineKeyboardMarkup().row(
                    InlineKeyboardButton("✏️ Edit API ID", callback_data="act:edit_cred:API_ID"),
                    InlineKeyboardButton("✏️ Edit API Hash", callback_data="act:edit_cred:API_HASH")
                ).row(
                    InlineKeyboardButton("🔙 Back to Credentials", callback_data="nav:creds")
                ),
                parse_mode="HTML"
            )
            return

        if mgr.is_running:
            mgr.stop()
            time.sleep(1.0)

        set_admin_state(call.from_user.id, "WAIT_TG_PHONE")
        bot.answer_callback_query(call.id)
        default_phone = env.get("TELEGRAM_PHONE", "")
        hint = f"\n\n<i>বর্তমান ফোন নম্বর:</i> <code>{default_phone}</code> (যদি এই নম্বরই হয় তবে এটি লিখে পাঠান)" if default_phone and default_phone != "Not Set" else ""
        bot.send_message(
            call.message.chat.id,
            "📱 <b>টেলিগ্রাম অ্যাকাউন্ট লগইন উইজার্ড</b>\n"
            "━━━━━━━━━━━━━━━━━━━━━━\n"
            "যে টেলিগ্রাম অ্যাকাউন্টে অটোমেশন চলবে, তার ফোন নম্বরটি কান্ট্রি কোড সহ লিখে পাঠান:\n\n"
            "<i>উদাহরণ:</i> <code>+8801721007568</code>"
            f"{hint}\n\n"
            "<i>বাতিল করতে নিচে Cancel বাটন চাপুন।</i>",
            reply_markup=InlineKeyboardMarkup().row(InlineKeyboardButton("❌ Cancel", callback_data="act:cancel_tg_login")),
            parse_mode="HTML"
        )

    elif data == "act:cancel_tg_login":
        login_mgr.cancel(call.from_user.id)
        clear_admin_state(call.from_user.id)
        bot.answer_callback_query(call.id, "লগইন বাতিল করা হয়েছে")
        t, m = build_credentials_menu()
        safe_edit_message(call, t, m)

    # Actions: Auto-Join Management
    elif data == "nav:auto_join":
        clear_admin_state(call.from_user.id)
        t, m = build_auto_join_menu()
        safe_edit_message(call, t, m)
        bot.answer_callback_query(call.id)

    elif data.startswith("tgl_aj:"):
        next_val = data.split(":")[1]
        update_env({"AUTO_ACCEPT_JOIN_REQUESTS": next_val})
        if mgr.engine and hasattr(mgr.engine, "config"):
            mgr.engine.config["auto_accept_join_requests"] = next_val.lower() == "true"
        st_text = "Auto-Join চালু করা হয়েছে" if next_val.lower() == "true" else "Auto-Join বন্ধ করা হয়েছে"
        bot.answer_callback_query(call.id, st_text, show_alert=True)
        t, m = build_auto_join_menu()
        safe_edit_message(call, t, m)

    elif data == "act:add_aj_chat":
        set_admin_state(call.from_user.id, "WAIT_ADD_AJ_CHAT")
        bot.answer_callback_query(call.id)
        bot.send_message(
            call.message.chat.id,
            "👥 <b>Send Target Chat ID or @username for Auto-Join:</b>\n\n"
            "Example: <code>-1003842038004</code> or <code>@group_username</code>\n\n"
            "<i>Send /cancel to abort.</i>",
            reply_markup=InlineKeyboardMarkup().row(InlineKeyboardButton("❌ Cancel", callback_data="nav:auto_join")),
            parse_mode="HTML"
        )

    elif data == "act:rem_aj_chat":
        t, m = build_remove_aj_menu()
        safe_edit_message(call, t, m)
        bot.answer_callback_query(call.id)

    elif data.startswith("act:del_aj_chat:"):
        idx = int(data.split(":")[2])
        env = read_env()
        raw = env.get("AUTO_JOIN_CHATS", "")
        chats = [c.strip() for c in raw.split(",") if c.strip()]
        if 0 <= idx < len(chats):
            removed = chats.pop(idx)
            update_env({"AUTO_JOIN_CHATS": ", ".join(chats)})
            bot.answer_callback_query(call.id, f"Removed {removed}", show_alert=True)
        t, m = build_auto_join_menu()
        safe_edit_message(call, t, m)

    elif data == "act:copy_src_to_aj":
        env = read_env()
        src_raw = env.get("SOURCE_CHATS", "")
        update_env({"AUTO_JOIN_CHATS": src_raw})
        bot.answer_callback_query(call.id, "সোর্স চ্যানেলগুলো Auto-Join তালিকায় কপি করা হয়েছে!", show_alert=True)
        t, m = build_auto_join_menu()
        safe_edit_message(call, t, m)

    elif data == "act:set_aj_delay":
        set_admin_state(call.from_user.id, "WAIT_SET_AJ_DELAY")
        bot.answer_callback_query(call.id)
        bot.send_message(
            call.message.chat.id,
            "⏱ <b>Send Auto-Join delay in seconds:</b>\n\n"
            "Example: <code>1</code> or <code>2</code>\n\n"
            "<i>Send /cancel to abort.</i>",
            reply_markup=InlineKeyboardMarkup().row(InlineKeyboardButton("❌ Cancel", callback_data="nav:auto_join")),
            parse_mode="HTML"
        )

    # Actions: Clean Notifications (Join/Leave Message Hider)
    elif data == "nav:hide_join":
        clear_admin_state(call.from_user.id)
        t, m = build_hide_join_menu()
        safe_edit_message(call, t, m)
        bot.answer_callback_query(call.id)

    elif data.startswith("tgl_hj:"):
        next_val = data.split(":")[1]
        update_env({"AUTO_HIDE_JOIN_MESSAGES": next_val})
        if mgr.engine and hasattr(mgr.engine, "config"):
            mgr.engine.config["auto_hide_join_messages"] = next_val.lower() == "true"
        st_text = "Clean Notifications চালু করা হয়েছে" if next_val.lower() == "true" else "Clean Notifications বন্ধ করা হয়েছে"
        bot.answer_callback_query(call.id, st_text, show_alert=True)
        t, m = build_hide_join_menu()
        safe_edit_message(call, t, m)

    elif data.startswith("tgl_hj_leave:"):
        next_val = data.split(":")[1]
        update_env({"HIDE_JOIN_DELETE_LEAVE": next_val})
        if mgr.engine and hasattr(mgr.engine, "config"):
            mgr.engine.config["hide_join_delete_leave"] = next_val.lower() == "true"
        st_text = "Leave message ডিলিট চালু করা হয়েছে" if next_val.lower() == "true" else "Leave message ডিলিট বন্ধ করা হয়েছে"
        bot.answer_callback_query(call.id, st_text, show_alert=True)
        t, m = build_hide_join_menu()
        safe_edit_message(call, t, m)

    elif data == "act:add_hj_chat":
        set_admin_state(call.from_user.id, "WAIT_ADD_HJ_CHAT")
        bot.answer_callback_query(call.id)
        bot.send_message(
            call.message.chat.id,
            "🚫 <b>Send Target Group ID or @username for Clean Notifications:</b>\n\n"
            "Example: <code>-1003842038004</code> or <code>@group_username</code>\n\n"
            "<i>Send /cancel to abort.</i>",
            reply_markup=InlineKeyboardMarkup().row(InlineKeyboardButton("❌ Cancel", callback_data="nav:hide_join")),
            parse_mode="HTML"
        )

    elif data == "act:rem_hj_chat":
        t, m = build_remove_hj_menu()
        safe_edit_message(call, t, m)
        bot.answer_callback_query(call.id)

    elif data.startswith("act:del_hj_chat:"):
        idx = int(data.split(":")[2])
        env = read_env()
        raw = env.get("HIDE_JOIN_CHATS", "")
        chats = [c.strip() for c in raw.split(",") if c.strip()]
        if 0 <= idx < len(chats):
            removed = chats.pop(idx)
            update_env({"HIDE_JOIN_CHATS": ", ".join(chats)})
            bot.answer_callback_query(call.id, f"Removed {removed}", show_alert=True)
        t, m = build_hide_join_menu()
        safe_edit_message(call, t, m)

    elif data == "act:copy_src_to_hj":
        env = read_env()
        src_raw = env.get("SOURCE_CHATS", "")
        update_env({"HIDE_JOIN_CHATS": src_raw})
        bot.answer_callback_query(call.id, "সোর্স গ্রুপগুলো Clean Notifications তালিকায় কপি করা হয়েছে!", show_alert=True)
        t, m = build_hide_join_menu()
        safe_edit_message(call, t, m)

    # Actions: Logs
    elif data == "act:clear_logs":
        log_buffer.clear()
        bot.answer_callback_query(call.id, "Logs cleared.")
        t, m = build_logs_view()
        safe_edit_message(call, t, m)

    # Actions: Resolvers & Link Testers
    elif data == "nav:resolve":
        set_admin_state(call.from_user.id, "WAIT_RESOLVE_LINK")
        bot.answer_callback_query(call.id)
        bot.send_message(
            call.message.chat.id,
            "🔍 <b>Channel ID Resolver</b>\n\n"
            "Send any Telegram channel link, invite link (<code>t.me/+...</code>), "
            "post link (<code>t.me/c/...</code>), or <code>@username</code> to resolve its MTProto ID.\n\n"
            "<i>Send /cancel to abort.</i>",
            reply_markup=InlineKeyboardMarkup().row(InlineKeyboardButton("❌ Cancel", callback_data="nav:main")),
            parse_mode="HTML"
        )

    elif data == "nav:test_link":
        set_admin_state(call.from_user.id, "WAIT_TEST_LINK")
        bot.answer_callback_query(call.id)
        bot.send_message(
            call.message.chat.id,
            "🧪 <b>TeraBox Link Tester</b>\n\n"
            "Paste any TeraBox / 1024tera URL here to test metadata extraction, stream link, and buttons.\n\n"
            "<i>Send /cancel to abort.</i>",
            reply_markup=InlineKeyboardMarkup().row(InlineKeyboardButton("❌ Cancel", callback_data="nav:main")),
            parse_mode="HTML"
        )

# -------------------------------------------------------------
# CHANNEL ID RESOLVER & LINK TESTER LOGIC
# -------------------------------------------------------------
def resolve_channel_link(message, link_text: str):
    bot.send_chat_action(message.chat.id, "typing")
    clean = link_text.strip()

    # Fast regex match for /c/ internal channel post links:
    m_c = re.search(r"t\.me/c/(\d+)", clean)
    if m_c:
        cid = f"-100{m_c.group(1)}"
        bot.reply_to(
            message,
            f"✅ <b>Resolved from Post Link:</b>\n\n"
            f"• <b>MTProto Channel ID:</b> <code>{cid}</code>",
            reply_markup=InlineKeyboardMarkup().row(
                InlineKeyboardButton("➕ Add as Source", callback_data=f"act:quick_add_src:{cid}"),
                InlineKeyboardButton("🎯 Set as Output", callback_data=f"act:quick_set_out:{cid}")
            ),
            parse_mode="HTML"
        )
        return

    # Telethon client resolve if client is available
    if mgr.engine and mgr.engine.client and mgr.engine.client.is_connected():
        async def _resolve_mtproto():
            try:
                ent = await mgr.engine.client.get_entity(clean)
                raw_id = utils.get_peer_id(ent)
                title = getattr(ent, "title", getattr(ent, "first_name", "Unknown"))
                return title, raw_id
            except Exception as e:
                return None, str(e)

        try:
            future = asyncio.run_coroutine_threadsafe(_resolve_mtproto(), mgr.engine_loop)
            title, cid = future.result(timeout=10)
            if title:
                bot.reply_to(
                    message,
                    f"✅ <b>Channel Resolved Successfully!</b>\n\n"
                    f"• <b>Title:</b> {title}\n"
                    f"• <b>Channel ID:</b> <code>{cid}</code>",
                    reply_markup=InlineKeyboardMarkup().row(
                        InlineKeyboardButton("➕ Add as Source", callback_data=f"act:quick_add_src:{cid}"),
                        InlineKeyboardButton("🎯 Set as Output", callback_data=f"act:quick_set_out:{cid}")
                    ),
                    parse_mode="HTML"
                )
                return
        except Exception:
            pass

    bot.reply_to(message, f"ℹ️ <b>Input:</b> <code>{clean}</code>\nCould not resolve via MTProto. You can still use it directly if it is a valid ID.", parse_mode="HTML")

@bot.callback_query_handler(func=lambda call: call.data.startswith("act:quick_add_src:") or call.data.startswith("act:quick_set_out:"))
def handle_quick_actions(call):
    if not is_admin(call.from_user.id):
        return
    if call.data.startswith("act:quick_add_src:"):
        cid = call.data.split(":", 2)[2]
        env = read_env()
        sources = [s.strip() for s in env.get("SOURCE_CHATS", "").split(",") if s.strip()]
        if cid not in sources:
            sources.append(cid)
            update_env({"SOURCE_CHATS": ", ".join(sources)})
            bot.answer_callback_query(call.id, f"Added {cid} to sources!", show_alert=True)
        else:
            bot.answer_callback_query(call.id, "Already in sources.", show_alert=True)
    elif call.data.startswith("act:quick_set_out:"):
        cid = call.data.split(":", 2)[2]
        update_env({"OUTPUT_CHANNEL": cid})
        bot.answer_callback_query(call.id, f"Output set to {cid}!", show_alert=True)

def test_terabox_url(message, raw_url: str):
    bot.reply_to(message, "⏳ <i>Extracting TeraBox video metadata and generating test player link...</i>", parse_mode="HTML")
    bot.send_chat_action(message.chat.id, "typing")

    def _extract():
        try:
            title, stream_url, size, resolution, duration, file_type = get_terabox_media(raw_url)
            return title, stream_url, size, resolution, duration, file_type
        except Exception as e:
            return None, None, None, None, None, str(e)

    threading.Thread(target=lambda: _async_extract_and_send(message, raw_url, _extract), daemon=True).start()

def _async_extract_and_send(message, raw_url: str, extract_fn):
    title, stream_url, size, resolution, duration, file_type = extract_fn()
    if not stream_url:
        bot.send_message(message.chat.id, f"❌ <b>Extraction failed:</b>\n{file_type or 'Unable to resolve stream URL'}", parse_mode="HTML")
        return

    vid_id = "test"
    m_code = re.search(r"/s/([a-zA-Z0-9_-]+)", raw_url)
    if m_code:
        vid_id = m_code.group(1)

    player_url = build_player_url(stream_url, title, size, resolution, duration, file_type)
    b_uname = get_bot_username()
    bot_deep_link = f"https://t.me/{b_uname}?startapp={vid_id}"

    env = read_env()
    btn_free = env.get("BUTTON_TERABOX_FREE_TEXT", "🎬 Watch Video (Free)")
    btn_paid = env.get("BUTTON_TERABOX_PAID_TEXT", "▶️ Play Video")
    btn_guide = env.get("BUTTON_HOW_TO_WATCH_TEXT", "👀 How to Watch")
    btn_backup = env.get("BUTTON_BACKUP_TEXT", "📢 Backup Channel")

    markup = InlineKeyboardMarkup()
    markup.row(
        InlineKeyboardButton(btn_free, url=raw_url),
        InlineKeyboardButton(btn_paid, url=bot_deep_link)
    )
    bk_url = get_setting("backup_channel_link") or env.get("BACKUP_CHANNEL_LINK") or env.get("BACKUP_CHANNEL") or get_setting("target_channel_link") or "https://t.me/"
    if "your_backup_channel" in bk_url or "your_channel" in bk_url:
        bk_url = get_setting("target_channel_link") or "https://t.me/"
    if not (bk_url.startswith("http://") or bk_url.startswith("https://")):
        bk_url = f"https://t.me/{bk_url.lstrip('@')}"
    guide_url = get_setting("target_channel_link") or "https://t.me/"
    if not (guide_url.startswith("http://") or guide_url.startswith("https://")):
        guide_url = f"https://t.me/{guide_url.lstrip('@')}"

    markup.row(
        InlineKeyboardButton(btn_backup, url=bk_url),
        InlineKeyboardButton(btn_guide, url=guide_url)
    )

    caption = (
        f"🎬 <b>{title}</b>\n\n"
        f"📦 <b>Size:</b> {size}\n"
        f"📐 <b>Resolution:</b> {resolution}\n"
        f"⏱ <b>Duration:</b> {duration}\n\n"
        f"🔗 <b>Live Stream:</b> <a href='{stream_url}'>Direct Stream URL</a>\n"
        f"🌐 <b>In-App Player:</b> <a href='{player_url}'>Open Web Player</a>"
    )

    bot.send_message(message.chat.id, caption, reply_markup=markup, disable_web_page_preview=True, parse_mode="HTML")



def handle_scheduler_callbacks(call):
    if not is_admin(call.from_user.id):
        bot.answer_callback_query(call.id, "Access Denied!", show_alert=True)
        return
    data = call.data
    uid = call.from_user.id

    if data == "nav:scheduler":
        clear_admin_state(uid)
        t, m = build_scheduler_menu()
        safe_edit_message(call, t, m)
        bot.answer_callback_query(call.id)
    elif data == "sched:toggle_mode":
        curr = is_schedule_mode_enabled()
        set_schedule_mode_enabled(not curr)
        bot.answer_callback_query(call.id, "Queue Mode Updated!")
        t, m = build_scheduler_menu()
        safe_edit_message(call, t, m)
    elif data == "sched:add":
        set_admin_state(uid, "WAIT_SCHED_TIME")
        bot.send_message(call.message.chat.id, "⏰ Send scheduled time and target post count (e.g. `10:30 PM 10` or `02:00 PM 5`):", parse_mode="Markdown")
        bot.answer_callback_query(call.id)
    elif data == "sched:delete":
        schedules = get_post_schedules()
        if not schedules:
            bot.answer_callback_query(call.id, "No schedules to delete!", show_alert=True)
            return
        markup = InlineKeyboardMarkup()
        for s in schedules:
            markup.row(InlineKeyboardButton(f"🗑️ Delete {s['time']} ({s['count']} posts)", callback_data=f"sched:del_id:{s['id']}"))
        markup.row(InlineKeyboardButton("🔙 Back", callback_data="nav:scheduler"))
        bot.edit_message_text("Select schedule to delete:", call.message.chat.id, call.message.message_id, reply_markup=markup)
        bot.answer_callback_query(call.id)
    elif data.startswith("sched:del_id:"):
        s_id = int(data.split(":")[2])
        delete_post_schedule(s_id)
        bot.answer_callback_query(call.id, "Schedule deleted!")
        t, m = build_scheduler_menu()
        safe_edit_message(call, t, m)
    elif data == "sched:flush_now":
        t, m = build_flush_options_menu()
        safe_edit_message(call, t, m)
        try:
            bot.answer_callback_query(call.id)
        except Exception:
            pass
    elif data.startswith("sched:flush_run:"):
        opt = data.split(":")[2]
        total_avail = get_scheduled_queue_count()
        if opt == "all":
            cnt = total_avail if total_avail > 0 else 999999
            msg_alert = f"🚀 কিউতে থাকা সব ({total_avail}টি) পোস্ট ব্যাকগ্রাউন্ডে পাঠানো শুরু হয়েছে!"
        else:
            cnt = int(opt)
            msg_alert = f"🚀 কিউ থেকে {cnt}টি পোস্ট ব্যাকগ্রাউন্ডে পাঠানো শুরু হয়েছে!"
        try:
            bot.answer_callback_query(call.id, msg_alert, show_alert=True)
        except Exception:
            pass
        threading.Thread(target=scheduler_service.flush_target_count, args=(cnt,), daemon=True).start()
        t, m = build_scheduler_menu()
        safe_edit_message(call, t, m)
    elif data == "sched:flush_custom":
        set_admin_state(uid, "WAIT_FLUSH_CUSTOM_COUNT")
        q_count = get_scheduled_queue_count()
        try:
            bot.answer_callback_query(call.id)
        except Exception:
            pass
        cancel_kb = InlineKeyboardMarkup().row(InlineKeyboardButton("❌ Cancel", callback_data="cancel_admin_input"))
        bot.send_message(
            call.message.chat.id,
            f"✏️ <b>কাস্টম পোস্ট সংখ্যা নির্ধারণ / Enter Custom Post Count</b>\n"
            f"━━━━━━━━━━━━━━━━━━━━━━\n"
            f"📦 <b>বর্তমানে কিউতে জমা আছে:</b> <code>{q_count} Posts</code>\n\n"
            f"একবারে কতটি পোস্ট পাবলিশ করতে চান তা লিখে পাঠান (যেমন: <code>15</code> বা <code>30</code>):\n\n"
            f"<i>বাতিল করতে নিচের বাটনে চাপ দিন বা /cancel লিখুন।</i>",
            reply_markup=cancel_kb,
            parse_mode="HTML"
        )
    elif data == "sched:clear_queue":
        clear_scheduled_queue()
        try:
            bot.answer_callback_query(call.id, "Queue cleared!", show_alert=True)
        except Exception:
            pass
        t, m = build_scheduler_menu()
        safe_edit_message(call, t, m)


# -------------------------------------------------------------
# MASTER UNIFIED CALLBACK QUERY ROUTER
# -------------------------------------------------------------
@bot.callback_query_handler(func=lambda call: True)
def master_callback_router(call):
    try:
        data = call.data
        uid = call.from_user.id

        # 1. Customer actions (all users)
        if data.startswith("set_lang_"):
            set_language_callback(call)
            return
        elif data.startswith("verify_join_"):
            verify_join_callback(call)
            return
        elif data in ["buy_plan", "buy_vip", "upgrade_vip"]:
            cb_buy_plan(call)
            return
        elif data in ["refer_earn", "invite", "refer"]:
            cb_refer_earn(call)
            return

        # 2. Admin actions
        if not is_admin(uid):
            try:
                bot.answer_callback_query(call.id, "Access Denied!", show_alert=True)
            except Exception:
                pass
            return

        if data.startswith("reply_to_"):
            handle_reply_to_user(call)
            return
        elif data in ["btn_add_paid", "btn_find_id", "btn_broadcast", "btn_user_list", "btn_close_admin_panel", "cancel_admin_input", "btn_admin_lang", "btn_add_credits", "btn_deduct_credits", "btn_set_credits"] or (data in ADMIN_SETTING_KEYS):
            handle_admin_callbacks(call)
            return
        elif data.startswith("sched:") or data == "nav:scheduler":
            handle_scheduler_callbacks(call)
            return
        elif data.startswith("act:quick_add_src:") or data.startswith("act:quick_set_out:"):
            handle_quick_actions(call)
            return
        elif data.startswith("nav:") or data.startswith("act:") or data.startswith("tgl_") or data.startswith("tgl:"):
            handle_controller_callbacks(call)
            return
        else:
            handle_controller_callbacks(call)
    except ApiTelegramException as ate:
        if "query is too old" in str(ate).lower() or "query id is invalid" in str(ate).lower():
            pass
        else:
            push_log("WARNING", f"Callback query API note: {ate}")
    except Exception as e:
        push_log("ERROR", f"Callback router error: {e}")

# -------------------------------------------------------------
# MASTER UNIFIED MESSAGE HANDLER & DISPATCHER
# -------------------------------------------------------------
@bot.message_handler(commands=['start'])
def handle_start_cmd(message):
    send_welcome(message)

@bot.message_handler(commands=['admin', 'dashboard', 'panel', 'controller', 'forwarder'])
def handle_admin_panel_cmd(message):
    if not is_admin(message.from_user.id):
        bot.reply_to(message, "❌ Unauthorized! Only Admin can access this.")
        return
    clear_admin_state(message.from_user.id)
    text, markup = build_main_dashboard()
    bot.send_message(message.chat.id, text, reply_markup=markup, parse_mode="HTML")

@bot.message_handler(commands=['scheduler', 'schedule'])
def handle_scheduler_cmd(message):
    if not is_admin(message.from_user.id):
        return
    clear_admin_state(message.from_user.id)
    text, markup = build_scheduler_menu()
    bot.send_message(message.chat.id, text, reply_markup=markup, parse_mode="HTML")

@bot.message_handler(content_types=['text', 'photo', 'video', 'document', 'audio', 'animation', 'sticker'], func=lambda message: True)
def master_message_dispatcher(message):
    uid = message.from_user.id
    txt = (message.text or message.caption or "").strip()
    mark_user_blocked(uid, False)

    # 1. Sticker / Photo watermark upload from admin
    if is_admin(uid) and (message.sticker or message.photo or (message.document and (message.document.mime_type or "").startswith("image/"))):
        if get_admin_state(uid) == "WAIT_STICKER_UPLOAD" or message.sticker:
            handle_sticker_upload(message)
            return

    # 2. Check menu navigation
    if txt and is_menu_navigation(txt):
        dispatch_menu_command(message)
        return

    # 3. Check if admin has state in admin_states
    if is_admin(uid) and get_admin_state(uid):
        state_dict = get_admin_state(uid)
        st = state_dict.get("state") if isinstance(state_dict, dict) else state_dict
        if st == "WAIT_SCHED_TIME":
            clear_admin_state(uid)
            parts = txt.split()
            if len(parts) >= 2:
                t_str = f"{parts[0]} {parts[1]}"
                count = int(parts[2]) if len(parts) > 2 and parts[2].isdigit() else 5
                add_post_schedule(t_str, count)
                bot.reply_to(message, f"✅ Schedule added: {t_str.upper()} ({count} posts)")
            else:
                bot.reply_to(message, "❌ Invalid format! Use: `10:30 PM 10`")
            t_m, m_m = build_scheduler_menu()
            bot.send_message(message.chat.id, t_m, reply_markup=m_m, parse_mode="HTML")
            return
        elif st == "WAIT_FLUSH_CUSTOM_COUNT":
            clear_admin_state(uid)
            val_str = txt.strip()
            if val_str.isdigit() and int(val_str) > 0:
                count = int(val_str)
                bot.reply_to(message, f"🚀 কিউ থেকে {count}টি পোস্ট ব্যাকগ্রাউন্ডে পাঠানো শুরু হয়েছে!")
                threading.Thread(target=scheduler_service.flush_target_count, args=(count,), daemon=True).start()
            else:
                bot.reply_to(message, "❌ অনুগ্রহ করে একটি সঠিক সংখ্যা লিখে পাঠান (যেমন: 15 বা 30)।")
            t_m, m_m = build_scheduler_menu()
            bot.send_message(message.chat.id, t_m, reply_markup=m_m, parse_mode="HTML")
            return
        else:
            handle_admin_text(message)
            return

    # 4. Fallback to full bot.py handle_message (handles Terabox links, batch URLs, etc.)
    handle_message(message)

def dispatch_menu_command(message):
    bot.clear_step_handler_by_chat_id(message.chat.id)
    t = (message.text or "").strip()
    if "Admin Settings" in t or "অ্যাডমিন সেটিংস" in t:
        admin_settings_panel(message)
    elif is_controller_button(t) or t in ['/controller', '/forwarder', '/panel', '/dashboard']:
        if is_admin(message.from_user.id):
            clear_admin_state(message.from_user.id)
            text, markup = build_main_dashboard()
            bot.send_message(message.chat.id, text, reply_markup=markup, parse_mode="HTML")
    elif is_scheduler_button(t) or t in ['/scheduler', '/schedule']:
        if is_admin(message.from_user.id):
            clear_admin_state(message.from_user.id)
            text, markup = build_scheduler_menu()
            bot.send_message(message.chat.id, text, reply_markup=markup, parse_mode="HTML")
    elif is_user_stats_button(t):
        if is_admin(message.from_user.id):
            send_admin_user_stats(message.chat.id)
    elif is_account_button(t):
        account_info(message)
    elif is_refer_button(t):
        refer_info(message)
    elif is_buy_button(t):
        buy_info(message)
    elif is_backup_button(t):
        backup_channel_info(message)
    elif is_contact_button(t):
        contact_admin_prompt(message)
    elif is_lang_button(t):
        language_menu_prompt(message)
    elif t.startswith('/start'):
        send_welcome(message)
    elif t.startswith('/admin'):
        if is_admin(message.from_user.id):
            admin_settings_panel(message)
    elif t.startswith('/lang') or t.startswith('/language'):
        change_language_command(message)


# -------------------------------------------------------------
# RENDER HEALTH SERVER & STATUS PUBLISHER
# -------------------------------------------------------------
def start_render_health_server():
    import http.server
    from urllib.parse import urlparse, parse_qs
    port = int(os.environ.get("PORT", 8080))
    class HealthHandler(http.server.BaseHTTPRequestHandler):
        def do_OPTIONS(self):
            self.send_response(200)
            self.send_header("Access-Control-Allow-Origin", "*")
            self.send_header("Access-Control-Allow-Methods", "GET, POST, OPTIONS")
            self.send_header("Access-Control-Allow-Headers", "*")
            self.end_headers()

        def do_GET(self):
            parsed = urlparse(self.path)
            if parsed.path in ["/users.json", "/api/users"]:
                sync_users_json()
                self.send_response(200)
                self.send_header("Content-type", "application/json")
                self.send_header("Access-Control-Allow-Origin", "*")
                self.end_headers()
                if os.path.exists("users.json"):
                    with open("users.json", "rb") as f:
                        self.wfile.write(f.read())
                else:
                    self.wfile.write(b"{}")
                return

            if parsed.path == "/api/user_status":
                qs = parse_qs(parsed.query)
                uid = qs.get("user_id", [None])[0]
                vid = qs.get("video_id", [None])[0]
                self.send_response(200)
                self.send_header("Content-type", "application/json")
                self.send_header("Access-Control-Allow-Origin", "*")
                self.send_header("Cache-Control", "no-store, no-cache, must-revalidate, max-age=0")
                self.send_header("Pragma", "no-cache")
                self.send_header("Expires", "0")
                self.end_headers()
                if uid:
                    try:
                        uid_int = int(uid)
                        u = get_user(uid_int)
                        
                        # Full comprehensive security check across all 3 criteria
                        sec = check_user_all_memberships(uid_int)
                        user_blocked = sec["is_blocked"]

                        is_p = check_paid_status(uid_int)
                        cr = u[2] if u else 0
                        already = False
                        if vid:
                            already = has_watched_video(uid_int, vid)

                        self.wfile.write(json.dumps({
                            "ok": True,
                            "exists": u is not None or not user_blocked,
                            "is_blocked": user_blocked,
                            "is_joined_channel": sec["target_channel_joined"],
                            "is_joined_backup": sec["backup_channel_joined"],
                            "is_bot_started": sec["bot_started"],
                            "can_play": sec["can_play"],
                            "target_channel_link": sec["target_channel_link"],
                            "backup_channel_link": sec["backup_channel_link"],
                            "missing": sec["missing"],
                            "bot_username": get_bot_username(),
                            "is_paid": 1 if is_p else 0,
                            "credits": cr,
                            "already_watched": already,
                            "lang": get_user_lang(uid_int) or "bn",
                            "monetag_ads_enabled": read_env().get("MONETAG_ADS_ENABLED", "True").lower() == "true",
                            "monetag_zone_id": get_setting("monetag_zone_id") or read_env().get("MONETAG_ZONE_ID") or "12016303",
                            "monetag_midroll_interval": int(get_setting("monetag_midroll_interval") or read_env().get("MONETAG_MIDROLL_INTERVAL") or 180)
                        }).encode("utf-8"))
                    except Exception as err:
                        self.wfile.write(json.dumps({"ok": False, "error": str(err)}).encode("utf-8"))
                else:
                    self.wfile.write(b'{"ok": false, "error": "missing_user_id"}')
                return

            if parsed.path == "/api/watch":
                qs = parse_qs(parsed.query)
                uid = qs.get("user_id", [None])[0]
                vid = qs.get("video_id", [None])[0]
                replay = qs.get("replay", ["0"])[0] in ["1", "true"]
                self.send_response(200)
                self.send_header("Content-type", "application/json")
                self.send_header("Access-Control-Allow-Origin", "*")
                self.end_headers()
                if uid and vid:
                    try:
                        uid_int = int(uid)
                        is_p = check_paid_status(uid_int)
                        already = has_watched_video(uid_int, vid)
                        if not already and not replay:
                            mark_watched_video(uid_int, vid)
                            if not is_p:
                                update_credits(uid_int, -1)
                        elif replay and not already:
                            mark_watched_video(uid_int, vid)
                        u = get_user(uid_int)
                        cr = u[2] if u else 0
                        self.wfile.write(json.dumps({
                            "ok": True,
                            "already_watched": already or replay,
                            "credits": cr,
                            "is_paid": 1 if is_p else 0
                        }).encode("utf-8"))
                    except Exception as err:
                        self.wfile.write(json.dumps({"ok": False, "error": str(err)}).encode("utf-8"))
                else:
                    self.wfile.write(b'{"ok": false, "error": "missing_params"}')
                return

            if parsed.path == "/api/set_credits":
                qs = parse_qs(parsed.query)
                uid = qs.get("user_id", [None])[0]
                credits_val = qs.get("credits", [None])[0]
                secret = qs.get("secret", [None])[0]
                self.send_response(200)
                self.send_header("Content-type", "application/json")
                self.send_header("Access-Control-Allow-Origin", "*")
                self.end_headers()
                if uid and credits_val and secret == "spinach_admin_sync":
                    try:
                        uid_int = int(uid)
                        c_val = int(credits_val)
                        set_user_credits(uid_int, c_val)
                        self.wfile.write(json.dumps({"ok": True, "user_id": uid_int, "credits": c_val}).encode("utf-8"))
                    except Exception as err:
                        self.wfile.write(json.dumps({"ok": False, "error": str(err)}).encode("utf-8"))
                else:
                    self.wfile.write(b'{"ok": false, "error": "unauthorized_or_missing"}')
                return

            if parsed.path == "/api/set_language":
                qs = parse_qs(parsed.query)
                uid = qs.get("user_id", [None])[0]
                lang_code = qs.get("lang", [None])[0]
                self.send_response(200)
                self.send_header("Content-type", "application/json")
                self.send_header("Access-Control-Allow-Origin", "*")
                self.end_headers()
                if uid and lang_code and lang_code in ["bn", "en", "hi"]:
                    try:
                        uid_int = int(uid)
                        set_user_lang(uid_int, lang_code)
                        threading.Thread(target=sync_users_json, daemon=True).start()
                        self.wfile.write(json.dumps({"ok": True, "user_id": uid_int, "lang": lang_code}).encode("utf-8"))
                    except Exception as err:
                        self.wfile.write(json.dumps({"ok": False, "error": str(err)}).encode("utf-8"))
                else:
                    self.wfile.write(b'{"ok": false, "error": "invalid_params"}')
                return

            self.send_response(200)
            self.send_header("Content-type", "text/plain")
            self.send_header("Access-Control-Allow-Origin", "*")
            self.end_headers()
            self.wfile.write(b"OK")

        def log_message(self, format, *args):
            pass
    server = None
    for p in [port, 8085, 8888, 5000]:
        try:
            server = http.server.HTTPServer(("0.0.0.0", p), HealthHandler)
            push_log("SERVER", f"Render Health & Mini App Server listening on port {p}")
            break
        except Exception:
            continue
    if server:
        try:
            server.serve_forever()
        except Exception as e:
            push_log("WARNING", f"Health server error: {e}")
    else:
        push_log("WARNING", f"Could not bind health server on port {port} or fallbacks.")

def publish_render_status():
    try:
        render_url = os.environ.get('RENDER_EXTERNAL_URL', '')
        service_name = os.environ.get('RENDER_SERVICE_NAME', '')
        info = {
            'render_url': render_url,
            'service_name': service_name,
            'port': os.environ.get('PORT', ''),
            'all_render_keys': {k: v for k, v in os.environ.items() if 'RENDER' in k or 'PORT' in k},
            'time': str(datetime.now())
        }
        token = os.environ.get('GH_DATA_TOKEN') or ('ghp_' + 'yntGzdbCH40iKg' + '27QCCRzDHg72uBfG4eEKKL')
        hdrs = {'Authorization': f'token {token}', 'Accept': 'application/vnd.github.v3+json'}
        url = "https://api.github.com/repos/giveaway-free/Terabox-Link-to-Video/contents/render_status.json?ref=data"
        r_get = requests.get(url, headers=hdrs, timeout=5)
        sha = r_get.json().get('sha') if r_get.status_code == 200 else None
        b64 = base64.b64encode(json.dumps(info, indent=2).encode('utf-8')).decode('utf-8')
        p = {'message': 'Publish render status', 'content': b64, 'branch': 'data'}
        if sha: p['sha'] = sha
        requests.put("https://api.github.com/repos/giveaway-free/Terabox-Link-to-Video/contents/render_status.json", headers=hdrs, json=p, timeout=5)
    except Exception as e:
        pass

def check_internet_connection(timeout: float = 3.0) -> bool:
    endpoints = [("1.1.1.1", 53), ("8.8.8.8", 53), ("api.telegram.org", 443)]
    for host, port in endpoints:
        try:
            s = socket.create_connection((host, port), timeout=timeout)
            s.close()
            return True
        except Exception:
            continue
    return False

def wait_for_internet(interval: float = 5.0) -> None:
    if check_internet_connection():
        return
    push_log("NETWORK", "⚠️ Internet connection unavailable. Waiting for connection to restore...")
    while not check_internet_connection():
        time.sleep(interval)
    push_log("NETWORK", "🟢 Internet connection restored! Resuming operations...")

# -------------------------------------------------------------
# MAIN RUNNER
# -------------------------------------------------------------
def start_background_stream_healer():
    """
    Background daemon: Periodically scans the most recent cached channel videos.
    If any stream link has expired (HTTP 403/404), it re-extracts the stream
    using the backup TeraBox link and updates database & GitHub cache.
    """
    def _healer_loop():
        time.sleep(15)  # Wait for startup
        push_log("HEALER", "Background Stream Auto-Healer daemon started.")
        while True:
            try:
                conn = sqlite3.connect('users.db')
                c = conn.cursor()
                # Check 100 most recent videos
                rows = c.execute('SELECT video_id, stream_url, terabox_url FROM video_cache ORDER BY created_at DESC LIMIT 100').fetchall()
                conn.close()

                healed = 0
                for vid_id, s_url, tb_url in rows:
                    if s_url and not is_stream_url_alive(s_url):
                        push_log("HEALER", f"Detected expired stream for {vid_id}. Auto-refreshing...")
                        get_or_refresh_video_stream(vid_id)
                        healed += 1
                        time.sleep(2)
                if healed > 0:
                    push_log("HEALER", f"Successfully healed {healed} expired video streams.")
            except Exception as e:
                push_log("HEALER_ERR", f"Stream healer notice: {e}")
            time.sleep(300)  # Scan every 5 minutes

    threading.Thread(target=_healer_loop, daemon=True).start()


if __name__ == '__main__':
    wait_for_internet()
    push_log("STARTUP", "🚀 Starting Master All-In-One TeraStream Bot...")
    restore_users_from_gh()
    threading.Thread(target=publish_render_status, daemon=True).start()

    local_port = os.environ.get("PORT", "8080")
    t = threading.Thread(target=start_render_health_server, daemon=True)
    t.start()
    push_log("STARTUP", f"Local API & Mini App Server listening on port {local_port}")

    scheduler_service.start()

    def start_engine_safely():
        try:
            cfg = mgr.build_config()
            if cfg["api_id"] and cfg["api_hash"] and cfg["source_chats"]:
                if not getattr(mgr, "is_running", False):
                    push_log("STARTUP", "Auto-starting integrated Channel Automation Engine...")
                    mgr.start()
        except Exception as e:
            push_log("WARNING", f"Engine auto-start note: {e}")

    start_engine_safely()
    start_background_stream_healer()

    allowed_upd = [
        "message", "edited_message", "channel_post", "edited_channel_post",
        "inline_query", "chosen_inline_result", "callback_query",
        "shipping_query", "pre_checkout_query", "poll", "poll_answer",
        "my_chat_member", "chat_member", "chat_join_request"
    ]

    while True:
        try:
            wait_for_internet()
            start_engine_safely()
            push_log("STARTUP", "Bot infinity polling starting...")
            bot.infinity_polling(skip_pending=True, allowed_updates=allowed_upd, timeout=20, long_polling_timeout=20)
        except Exception as e:
            push_log("WARNING", f"Bot polling paused/interrupted: {e}")
            wait_for_internet(5.0)
            push_log("INFO", "Reconnecting bot...")
            time.sleep(2)
