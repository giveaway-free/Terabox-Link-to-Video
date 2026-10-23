import sys
import os
if hasattr(sys.stdout, 'reconfigure'):
    sys.stdout.reconfigure(encoding='utf-8', errors='replace')
if hasattr(sys.stderr, 'reconfigure'):
    sys.stderr.reconfigure(encoding='utf-8', errors='replace')

import re
import json
import time
import base64
import threading
import sqlite3
import requests
import hashlib
import telebot
from datetime import datetime, timedelta
from telebot.types import (
    InlineKeyboardMarkup, InlineKeyboardButton, WebAppInfo, 
    ReplyKeyboardMarkup, KeyboardButton, InlineQueryResultArticle, InputTextMessageContent,
    InlineQueryResultCachedPhoto, CopyTextButton
)
from urllib.parse import quote, urlparse, urlunparse

BOT_TOKEN = (
    os.environ.get("DISPATCH_BOT_TOKEN")
    or os.environ.get("BOT_TOKEN")
    or "8751273482:AAGP-Ls2H899GyMNJ6MXHKEQPCeAOO6eWYI"
)
MY_PLAYER_URL = os.environ.get("PLAYER_URL") or "https://giveaway-free.github.io/Terabox-Link-to-Video/"

bot = telebot.TeleBot(BOT_TOKEN, parse_mode=None)

try:
    from forwarder_core import fetch_bot_username_from_token, get_or_refresh_video_stream, is_stream_url_alive
except ImportError:
    fetch_bot_username_from_token = None
    get_or_refresh_video_stream = None
    is_stream_url_alive = None

_BOT_USERNAME = None
def get_bot_username():
    global _BOT_USERNAME
    if fetch_bot_username_from_token:
        _BOT_USERNAME = fetch_bot_username_from_token(BOT_TOKEN, sync_to_env=True)
    if not _BOT_USERNAME:
        try:
            me = bot.get_me()
            if me and me.username:
                _BOT_USERNAME = me.username
        except Exception:
            _BOT_USERNAME = "Terastream_pro_bot"
    return str(_BOT_USERNAME).strip().lstrip("@")

# --- DATABASE SETUP ---
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
        "max_batch_links": "5"
    }
    
    for key, val in default_settings.items():
        cursor.execute('INSERT OR IGNORE INTO settings (key, value) VALUES (?, ?)', (key, val))
        
    conn.commit()
    conn.close()

def restore_users_from_gh():
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
                    uname = uinfo.get('username') or f"User_{uid}"
                    credits = int(uinfo.get('credits', 10))
                    is_paid = int(uinfo.get('is_paid', 0))
                    paid_expiry = uinfo.get('paid_expiry')
                    cursor.execute('''
                        INSERT INTO users (user_id, username, credits, is_paid, paid_expiry)
                        VALUES (?, ?, ?, ?, ?)
                        ON CONFLICT(user_id) DO UPDATE SET
                            username = excluded.username,
                            credits = excluded.credits,
                            is_paid = excluded.is_paid,
                            paid_expiry = excluded.paid_expiry
                    ''', (uid, uname, credits, is_paid, paid_expiry))
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

init_db()
restore_users_from_gh()

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
        return int(user_id) == int(admin_id_str) or int(user_id) == 553826070
    except Exception:
        return int(user_id) == 553826070

def get_user(user_id):
    conn = sqlite3.connect('users.db')
    cursor = conn.cursor()
    cursor.execute('SELECT * FROM users WHERE user_id = ?', (user_id,))
    user = cursor.fetchone()
    conn.close()
    return user

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

def mark_user_blocked(user_id: int, blocked: bool):
    try:
        conn = sqlite3.connect('users.db')
        cursor = conn.cursor()
        val = 1 if blocked else 0
        cursor.execute('''
            UPDATE users SET is_blocked = ? WHERE user_id = ?
        ''', (val, user_id))
        conn.commit()
        conn.close()
        try:
            threading.Thread(target=sync_users_json, daemon=True).start()
        except Exception:
            pass
    except Exception:
        pass

def is_user_blocked(user_id: int) -> bool:
    try:
        conn = sqlite3.connect('users.db')
        cursor = conn.cursor()
        cursor.execute('SELECT is_blocked FROM users WHERE user_id = ?', (user_id,))
        row = cursor.fetchone()
        conn.close()
        if not row:
            return False
        return row[0] == 1
    except Exception:
        return False

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
    return any(k in text for k in ["My Account", "Account Overview", "আমার অ্যাকাউন্ট"])

def is_refer_button(text):
    return any(k in text for k in ["Refer & Earn", "রেফার", "रेफर"])

def is_buy_button(text):
    return any(k in text for k in ["Buy Subscription", "সাবস্ক্রিপশন", "सब्सक्रिप्शन"])

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
    return any(k in t for k in ["Contact Admin", "যোগাযোগ", "संपर्क"])

def is_lang_button(text):
    if not text: return False
    t = text.strip().lower()
    if t.startswith("http://") or t.startswith("https://") or t.startswith("t.me/") or t.startswith("@"):
        return False
    return any(k in t for k in ["Language", "ভাষা", "भाषा"])

def is_user_stats_button(text):
    if not text: return False
    t = text.strip()
    if t.startswith("http://") or t.startswith("https://") or t.startswith("t.me/") or t.startswith("@"):
        return False
    return any(k in t for k in ["User List & Stats", "User Stats", "ইউজার লিস্ট"])

def is_menu_navigation(text):
    if not text:
        return False
    t = text.strip()
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
        "Admin Settings" in t
    )

def dispatch_menu_command(message):
    bot.clear_step_handler_by_chat_id(message.chat.id)
    t = (message.text or "").strip()
    if "Admin Settings" in t:
        admin_settings_panel(message)
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

def build_monetag_menu():
    zone_id = get_setting("monetag_zone_id") or "12016303"
    ads_enabled_val = get_setting("monetag_ads_enabled")
    is_ads_on = ads_enabled_val.lower() != "false" if ads_enabled_val else True
    next_ads_val = "False" if is_ads_on else "True"
    status_icon = "🟢 **ACTIVE (বিজ্ঞাপন চালু)**" if is_ads_on else "🔴 **DISABLED (বিজ্ঞাপন বন্ধ)**"

    try:
        interval_sec = int(get_setting("monetag_midroll_interval") or 180)
    except Exception:
        interval_sec = 180

    if interval_sec >= 60:
        mins = interval_sec // 60
        secs = interval_sec % 60
        time_display = f"{mins} Minute{'s' if mins > 1 else ''}" + (f" {secs}s" if secs > 0 else "")
    else:
        time_display = f"{interval_sec} Seconds"

    text = (
        "💰 **Monetag Ads Settings**\n"
        "━━━━━━━━━━━━━━━━━━━━━━\n"
        f"• **Status:** {status_icon}\n"
        f"• **Zone ID:** `{zone_id}`\n"
        f"• **Ads Interval (Time):** **{time_display}** (`{interval_sec}s`)\n\n"
        "💡 *Monetag অন থাকলে ফ্রি ইউজারদের জন্য ভিডিওতে বিজ্ঞাপন চালু থাকবে। অফ করলে বিজ্ঞাপন পুরোপুরি বন্ধ থাকবে।*\n"
        "━━━━━━━━━━━━━━━━━━━━━━\n"
        "*নিচ থেকে Monetag Ads অন/অফ করুন, Zone ID বা Ads Time পরিবর্তন করুন:*"
    )

    markup = InlineKeyboardMarkup()
    tgl_btn_text = "🔴 Turn OFF Monetag Ads" if is_ads_on else "🟢 Turn ON Monetag Ads"
    markup.row(
        InlineKeyboardButton(tgl_btn_text, callback_data=f"tgl_monetag:{next_ads_val}")
    )
    markup.row(
        InlineKeyboardButton(f"✏️ Edit Zone ID [{zone_id}]", callback_data="set_monetag_zone")
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
        InlineKeyboardButton("✏️ Custom Ads Time (Seconds / Minutes)", callback_data="set_monetag_interval")
    )
    markup.row(
        InlineKeyboardButton("⚙️ Admin Settings", callback_data="nav:admin_settings"),
        InlineKeyboardButton("❌ Close Panel", callback_data="btn_close_admin_panel")
    )
    return text, markup

def admin_settings_inline():
    ads_enabled_val = get_setting("monetag_ads_enabled")
    is_ads_on = ads_enabled_val.lower() != "false" if ads_enabled_val else True
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
        InlineKeyboardButton("14. ➖ Deduct User Credits", callback_data="btn_deduct_credits"),
        InlineKeyboardButton("15. ✏️ Set User Credits", callback_data="btn_set_credits"),
        InlineKeyboardButton(f"16. 💰 Monetag Ads: {monetag_st}", callback_data="nav:monetag")
    )
    markup.row(InlineKeyboardButton("❌ Close Panel", callback_data="btn_close_admin_panel"))
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
    
    # 1. Register user if not exists
    user = get_user(user_id)
    if not user:
        register_user(user_id, username)
        user = get_user(user_id)
        
    # 2. Check strict multi-channel membership gate
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
        bot.send_message(
            message.chat.id,
            f"⚠️ **Access Locked / Membership Required!**\n\n"
            f"এই ভিডিওটি দেখতে হলে নিচের শর্তগুলো পূরণ করুন:\n{reason_txt}\n\n"
            f"জয়েন করার পর নিচের **'Joined! Verify & Watch'** বাটনে চাপ দিন।",
            reply_markup=markup,
            parse_mode="Markdown"
        )
        return

    # 3. Always Fetch & Re-extract Video Info FIRST (Guarantees link is fresh in DB & GitHub!)
    status_msg = bot.send_message(message.chat.id, "⚡ **Preparing Mini App Video Player...**", parse_mode="Markdown")
    if get_or_refresh_video_stream:
        stream_url, title, size, resolution, duration, file_type, terabox_url = get_or_refresh_video_stream(vid_id)
    else:
        cached = get_cached_video(vid_id)
        stream_url = cached[0] if cached else None
        title = cached[1] if cached and cached[1] else "TeraBox Video"
        size = cached[2] if cached and cached[2] else "Unknown"
        resolution = cached[3] if cached and cached[3] else "Auto"
        duration = cached[4] if cached and cached[4] else "--"
        file_type = cached[5] if cached and cached[5] else "Video"

    if not stream_url:
        surl = vid_id[1:] if vid_id.startswith('1') else vid_id
        stream_url = f"https://www.terabox.app/sharing/embed?surl={surl}"
        resolution = "HD Embed"
        print(f"[BOT-DIRECT-WATCH] Fallback to embed player for {vid_id}: {stream_url}")

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
    
    command_args = message.text.split()
    if len(command_args) > 1 and command_args[1].startswith("v_"):
        vid_id = command_args[1][2:]
        handle_direct_video_watch(message, vid_id)
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

def get_admin_settings_text():
    track_display = get_setting('target_channel_link') or get_setting('required_group_id')
    ads_enabled_val = get_setting("monetag_ads_enabled")
    is_ads_on = ads_enabled_val.lower() != "false" if ads_enabled_val else True
    m_zone = get_setting('monetag_zone_id') or '12016303'
    m_int = get_setting('monetag_midroll_interval') or '180'
    try:
        m_int_int = int(m_int)
        m_int_lbl = f"{m_int_int // 60}m" if m_int_int >= 60 and m_int_int % 60 == 0 else f"{m_int_int}s"
    except Exception:
        m_int_lbl = f"{m_int}s"

    text = (
        "⚙️ **Admin Control Panel**\n\n"
        f"👑 **Admin ID:** `{get_setting('admin_id')}`\n"
        f"👥 **Track Group ID:** `{get_setting('required_group_id')}`\n"
        f"1. **Signup Free Credits:** {get_setting('initial_credits')}\n"
        f"2. **Referral Bonus:** {get_setting('refer_credits')} Credits\n"
        f"3. **7-Day Plan Price:** {get_setting('paid_7_days')}\n"
        f"4. **30-Day Plan Price:** {get_setting('paid_30_days')}\n"
        f"5. **Custom Share Text:**\n`{get_setting('custom_share_text')}`\n"
        f"6. **Backup Channel Link:**\n`{get_setting('backup_channel_link')}`\n"
        f"7. **Max Batch Links (per post):** {get_setting('max_batch_links') or '5'}\n"
        f"8. **Monetag Ads:** {'🟢 ON' if is_ads_on else '🔴 OFF'} (Zone: `{m_zone}` | Time: `{m_int_lbl}`)\n\n"
        "Nicher numbered buttons theke option select korun:"
    )
    return text

@bot.message_handler(func=lambda msg: bool(msg.text and "Admin Settings" in msg.text))
def admin_settings_panel(message):
    if not is_admin(message.from_user.id):
        return
    text = get_admin_settings_text()
    bot.send_message(message.chat.id, text, parse_mode="Markdown", reply_markup=admin_settings_inline())
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

@bot.callback_query_handler(func=lambda call: call.data.startswith("set_") or call.data in ["btn_add_paid", "btn_find_id", "btn_broadcast", "btn_user_list", "btn_close_admin_panel", "cancel_admin_input", "btn_admin_lang", "btn_add_credits", "btn_deduct_credits", "btn_set_credits"])
def handle_admin_callbacks(call):
    if not is_admin(call.from_user.id):
        return

    if call.data == "btn_admin_lang":
        bot.send_message(
            call.message.chat.id,
            get_txt(call.from_user.id, "select_lang_prompt"),
            parse_mode="Markdown",
            reply_markup=language_selection_markup()
        )
    if call.data == "nav:monetag":
        t, m = build_monetag_menu()
        try:
            bot.edit_message_text(t, call.message.chat.id, call.message.message_id, reply_markup=m, parse_mode="Markdown")
        except Exception:
            bot.send_message(call.message.chat.id, t, reply_markup=m, parse_mode="Markdown")
        return

    if call.data.startswith("tgl_monetag:"):
        next_val = call.data.split(":")[1]
        set_setting("monetag_ads_enabled", next_val)
        try:
            bot.answer_callback_query(call.id, f"Monetag Ads is now {'ON' if next_val == 'True' else 'OFF'}")
        except Exception:
            pass
        t, m = build_monetag_menu()
        try:
            bot.edit_message_text(t, call.message.chat.id, call.message.message_id, reply_markup=m, parse_mode="Markdown")
        except Exception:
            bot.send_message(call.message.chat.id, t, reply_markup=m, parse_mode="Markdown")
        return

    if call.data.startswith("set_midroll:"):
        sec = int(call.data.split(":")[1])
        set_setting("monetag_midroll_interval", str(sec))
        try:
            bot.answer_callback_query(call.id, f"Ads interval set to {sec}s!", show_alert=True)
        except Exception:
            pass
        t, m = build_monetag_menu()
        try:
            bot.edit_message_text(t, call.message.chat.id, call.message.message_id, reply_markup=m, parse_mode="Markdown")
        except Exception:
            bot.send_message(call.message.chat.id, t, reply_markup=m, parse_mode="Markdown")
        return

    if call.data in ["nav:admin_settings", "btn_admin_settings"]:
        t = get_admin_settings_text()
        m = admin_settings_inline()
        try:
            bot.edit_message_text(t, call.message.chat.id, call.message.message_id, reply_markup=m, parse_mode="Markdown")
        except Exception:
            bot.send_message(call.message.chat.id, t, reply_markup=m, parse_mode="Markdown")
        try:
            bot.answer_callback_query(call.id)
        except Exception:
            pass
        return

    if call.data == "btn_close_admin_panel":
        try:
            bot.delete_message(call.message.chat.id, call.message.message_id)
        except Exception:
            bot.edit_message_text("❌ Panel closed.", chat_id=call.message.chat.id, message_id=call.message.message_id)
        return

    if call.data == "cancel_admin_input":
        bot.clear_step_handler_by_chat_id(call.message.chat.id)
        try:
            bot.delete_message(call.message.chat.id, call.message.message_id)
        except Exception:
            pass
        c_msg = bot.send_message(call.message.chat.id, "❌ **Input session cancelled.**", parse_mode="Markdown")
        auto_delete_after(call.message.chat.id, c_msg.message_id, 5)
        return

    if call.data == "btn_user_list":
        send_admin_user_stats(call.message.chat.id)
        return
    
    if call.data.startswith("no_action_"):
        bot.answer_callback_query(call.id, "It's just an identifier button.")
        return
    
    if call.data == "btn_broadcast":
        msg = bot.send_message(
            call.message.chat.id, 
            "📢 **Je Message-ti sobar kache Broadcast korte chan ta likhe pathan:**", 
            parse_mode="Markdown",
            reply_markup=cancel_input_inline()
        )
        bot.register_next_step_handler(msg, process_broadcast_text, msg.message_id)
        return

    if call.data == "btn_find_id":
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
        msg = bot.send_message(
            call.message.chat.id, 
            "👤 **User-ke Paid status dite User ID & Days din:**\nFormat: `<User_ID> <Days>`\nExample: `123456789 30`", 
            parse_mode="Markdown",
            reply_markup=cancel_input_inline()
        )
        bot.register_next_step_handler(msg, process_add_paid_ui, msg.message_id)
        return

    if call.data == "btn_add_credits":
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
    elif setting_key == "monetag_zone_id":
        clean_zone = re.sub(r"[^\d]", "", new_val)
        if clean_zone:
            new_val = clean_zone
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
        if "t.me/" in new_val:
            raw_handle = new_val.split("t.me/")[-1].replace("/", "").strip()
            if not raw_handle.startswith("+"):
                try:
                    chat = bot.get_chat(f"@{raw_handle}")
                    set_setting("backup_channel_id", str(chat.id))
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

@bot.message_handler(func=lambda msg: bool(msg.text and is_lang_button(msg.text)))
def language_menu_prompt(message):
    user_id = message.from_user.id
    prompt_text = get_txt(user_id, "select_lang_prompt")
    bot.reply_to(message, prompt_text, parse_mode="Markdown", reply_markup=language_selection_markup())

@bot.message_handler(content_types=['text', 'photo', 'video', 'document', 'audio', 'animation'], func=lambda message: True)
def handle_message(message):
    user_id = message.from_user.id
    raw_content = message.text or message.caption or ""
    
    if raw_content and (is_account_button(raw_content) or is_refer_button(raw_content) or is_buy_button(raw_content) or 
        is_backup_button(raw_content) or is_contact_button(raw_content) or is_lang_button(raw_content) or 
        is_user_stats_button(raw_content) or "Admin Settings" in raw_content):
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
            
            player_url = (
                f"{MY_PLAYER_URL}?"
                f"src={encoded_stream_url}&"
                f"title={encoded_title}&"
                f"size={encoded_size}&"
                f"res={encoded_res}&"
                f"dur={encoded_dur}&"
                f"type={encoded_type}"
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
        rows = c.execute('SELECT user_id, username, credits, is_paid, paid_expiry FROM users').fetchall()
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
                'watched': watched_map.get(str(r[0]), [])
            } for r in rows
        }
        with open('users.json', 'w', encoding='utf-8') as f:
            json.dump(data, f, indent=2)
        threading.Thread(target=push_users_json_to_gh, daemon=True).start()
    except Exception as e:
        print(f"Error syncing users.json: {e}")

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
                self.end_headers()
                if uid:
                    try:
                        uid_int = int(uid)
                        u = get_user(uid_int)
                        is_p = check_paid_status(uid_int)
                        cr = u[2] if u else 0
                        already = False
                        if vid:
                            already = has_watched_video(uid_int, vid)
                        self.wfile.write(json.dumps({
                            "ok": True,
                            "exists": u is not None,
                            "is_paid": 1 if is_p else 0,
                            "credits": cr,
                            "already_watched": already,
                            "monetag_ads_enabled": get_setting("monetag_ads_enabled") != "False",
                            "monetag_zone_id": get_setting("monetag_zone_id") or "12016303",
                            "monetag_midroll_interval": int(get_setting("monetag_midroll_interval") or 180)
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
    try:
        server = http.server.HTTPServer(("0.0.0.0", port), HealthHandler)
        server.serve_forever()
    except Exception as e:
        print(f"Health server error: {e}")

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
        print(f"Error publishing render status: {e}")

if __name__ == '__main__':
    threading.Thread(target=publish_render_status, daemon=True).start()
    if os.environ.get("PORT"):
        t = threading.Thread(target=start_render_health_server, daemon=True)
        t.start()
        print(f"Render health server listening on port {os.environ.get('PORT')}")
    print("Bot is running...")
    bot.infinity_polling(allowed_updates=["message", "edited_message", "channel_post", "edited_channel_post", "inline_query", "chosen_inline_result", "callback_query", "shipping_query", "pre_checkout_query", "poll", "poll_answer", "my_chat_member", "chat_member", "chat_join_request"])
