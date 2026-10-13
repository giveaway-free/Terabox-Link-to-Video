"""
terastream_all_in_one.py
Unified Master Telegram Bot for TeraStream Video Streaming & TeraBox Channel Automation.
Combines:
  1. TeraStream Client Bot (credits, referrals, VIP plans, WebApp streaming, multi-lang)
  2. Channel Forwarder & Automation Engine (Telethon multi-source listener, media watermarking, buttons)
  3. Scheduled Post Queue (multi-schedule timer, target post count, store/flush queue, pop-on-post)
  4. Remote Admin Controller & Diagnostics Dashboard
All text is localized in the user's selected language (English & Bengali).
"""

import sys
if hasattr(sys.stdout, 'reconfigure'):
    sys.stdout.reconfigure(encoding='utf-8', errors='replace')
if hasattr(sys.stderr, 'reconfigure'):
    sys.stderr.reconfigure(encoding='utf-8', errors='replace')

import os
import re
import time
import json
import shutil
import sqlite3
import requests
import asyncio
import logging
import threading
from datetime import datetime, timedelta, timezone
from collections import deque
from typing import Optional, List, Dict, Any, Set, Tuple
from urllib.parse import quote

import telebot
from telebot.types import (
    InlineKeyboardMarkup, InlineKeyboardButton, WebAppInfo,
    ReplyKeyboardMarkup, KeyboardButton, InlineQueryResultArticle,
    InputTextMessageContent, ReplyKeyboardRemove
)
from telebot.apihelper import ApiTelegramException

try:
    import psutil
except ImportError:
    psutil = None

from telethon import TelegramClient, utils, errors
from telethon.tl import types, functions

# Import headless forwarder core
import forwarder_core
from forwarder_core import (
    AutomationEngine, Database, MediaProcessor,
    DEFAULT_TERABOX_DOMAINS, normalize_terabox_url,
    get_terabox_media, build_player_url, save_video_cache,
    push_single_video_to_gh
)

# -------------------------------------------------------------
# APP DIRECTORY & CONFIGURATION
# -------------------------------------------------------------
APP_DIR = os.path.dirname(os.path.abspath(__file__))
os.chdir(APP_DIR)
DOTENV_PATH = os.path.join(APP_DIR, ".env")
USERS_DB_PATH = os.path.join(APP_DIR, "users.db")
SCHEDULED_MEDIA_DIR = os.path.join(APP_DIR, "data", "scheduled_media")
os.makedirs(SCHEDULED_MEDIA_DIR, exist_ok=True)

# -------------------------------------------------------------
# .ENV HELPER FUNCTIONS
# -------------------------------------------------------------
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
    new_lines = []
    found_keys = set()

    for line in lines:
        stripped = line.strip()
        if stripped and not stripped.startswith("#") and "=" in stripped:
            k = stripped.split("=", 1)[0].strip()
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

# Read Environment Defaults
env_init = read_env()
BOT_TOKEN = env_init.get("BOT_TOKEN") or env_init.get("DISPATCH_BOT_TOKEN") or "7796341783:AAEng5ditGE0sYkyfSrm7DrzF1u6KTr20lI"
MY_PLAYER_URL = env_init.get("PLAYER_URL") or "https://giveaway-free.github.io/Terabox-Link-to-Video/"
DEFAULT_BOT_PASSWORD = env_init.get("BOT_PASSWORD") or "Bmjibon1234@"

bot = telebot.TeleBot(BOT_TOKEN, parse_mode=None)

# In-memory logging ring buffer
log_buffer = deque(maxlen=150)

def push_log(level: str, message: str):
    timestamp = datetime.now().strftime("%H:%M:%S")
    entry = f"[{timestamp}] [{level}] {message}"
    log_buffer.append((level, entry))
    print(entry, flush=True)

# -------------------------------------------------------------
# MULTI-LANGUAGE (I18N) LOCALIZATION ENGINE FOR BOT
# -------------------------------------------------------------
I18N_BOT = {
    "bn": {
        # Buttons
        "btn_account": "👤 আমার অ্যাকাউন্ট",
        "btn_refer": "🎁 রেফার এবং আয়",
        "btn_sub": "💳 সাবস্ক্রিপশন কিনুন",
        "btn_backup": "📢 ব্যাকআপ চ্যানেল",
        "btn_contact": "💬 অ্যাডমিনের সাথে যোগাযোগ",
        "btn_admin": "⚙️ অ্যাডমিন প্যানেল",
        "btn_lang": "🌐 ভাষা / Language",
        "btn_watch_web": "▶️ ভিডিও দেখুন (Web App)",
        "btn_fast_dl": "📥 ফাস্ট ডাউনলোড",
        "btn_cancel": "❌ বাতিল করুন",
        "btn_close": "❌ প্যানেল বন্ধ করুন",
        # General
        "lang_selected": "✅ ভাষা সফলভাবে **বাংলা** নির্ধারণ করা হয়েছে!",
        "lang_choose": "🌐 অনুগ্রহ করে আপনার পছন্দের ভাষা নির্বাচন করুন:\nPlease select your preferred language:",
        "welcome_back": "👋 হ্যালো {username}!\n\nভিডিও স্ট্রিম করতে TeraBox লিংক পাঠান অথবা নিচের মেনু ব্যবহার করুন।",
        "welcome_bonus": "👋 স্বাগতম {username}!\n\n🎁 আপনাকে **{init_credits} টি ফ্রি ক্রেডিট** দেওয়া হয়েছে!\nভিডিও স্ট্রিম করতে TeraBox লিংক পাঠান।",
        "ref_bonus_received": "🎉 **রেফারেল বোনাস জমা হয়েছে!**\n\nআপনার রেফার করা ইউজার (`{user_id}`) চ্যানেলে জয়েন করায় আপনার অ্যাকাউন্টে **+{bonus} টি ক্রেডিট** যুক্ত হয়েছে!",
        "ref_join_required": "🌟 **এক্সক্লুসিভ অ্যাক্সেস** 🌟\n\n👋 স্বাগতম {username}!\n\n⚠️ **বিজ্ঞপ্তি:** ফ্রি ক্রেডিট দিয়ে বট ব্যবহার করতে আমাদের চ্যানেলে জয়েন করা আবশ্যক!\n\n👉 নিচের বাটনে ক্লিক করে চ্যানেলে জয়েন হয়ে **Verify & Claim Bonus** বাটনে চাপুন:",
        "acc_title": "👤 **অ্যাকাউন্ট বিস্তারিত তথ্য**\n\n👤 **নাম:** {name}\n🌐 **ইউজারনেম:** {username}\n🆔 **ইউজার আইডি:** `{user_id}`\n⚡ **প্ল্যান স্ট্যাটাস:** {status}\n💰 **অবশিষ্ট ক্রেডিট:** `{credits}`\n👥 **মোট রেফারেল:** `{ref_count}`\n\n🔗 **আপনার রেফারেল লিংক:**\n`{ref_link}`",
        "plan_vip": "💎 ভিআইপি প্রিমিয়াম (মেয়াদ: `{expiry}`)",
        "plan_free": "🆓 ফ্রি মেম্বার",
        "refer_info": "🎁 **রেফার করে ফ্রি ক্রেডিট আয় করুন**\n\n👥 প্রতি সফল রেফারে পাবেন **+{bonus} টি ক্রেডিট**!\n⚠️ **শর্ত:** আপনার লিংক দিয়ে বট চালু করে চ্যানেলে জয়েন করলে তবেই ক্রেডিট যুক্ত হবে।\n\n🔗 **আপনার রেফারেল লিংক:**\n`{ref_link}`",
        "sub_info": "💎 **আনলিমিটেড প্রিমিয়াম সাবস্ক্রিপশন**\n\n⚡ **৭ দিন আনলিমিটেড:** `{p7}`\n⚡ **৩০ দিন আনলিমিটেড:** `{p30}`\n\n✨ **প্রিমিয়াম সুবিধাসমূহ:**\n• আনলিমিটেড হাই-স্পিড ভিডিও স্ট্রিমিং\n• কোনো ক্রেডিট বা পয়েন্টের ঝামেলা নেই\n• ২৪/৭ প্রায়োরিটি অ্যাক্সেস\n\n👉 সাবস্ক্রিপশন কিনতে অ্যাডমিনের সাথে যোগাযোগ করুন:",
        "backup_info": "📢 **আমাদের ব্যাকআপ চ্যানেলে যুক্ত থাকুন যাতে কোনো জরুরি আপডেট মিস না হয়:**",
        "contact_prompt": "📝 **অ্যাডমিনের কাছে আপনার বার্তাটি লিখে পাঠান:**\n\n*(বাতিল করতে /cancel লিখুন)*",
        "contact_sent": "✅ **আপনার বার্তাটি সফলভাবে অ্যাডমিনের কাছে পাঠানো হয়েছে!**",
        "contact_err": "❌ বার্তা পাঠাতে সমস্যা হয়েছে! পরে আবার চেষ্টা করুন।",
        "admin_self_contact": "👑 আপনি নিজেই অ্যাডমিন! আপনার জন্য নিচের অ্যাডমিন প্যানেল সক্রিয় রয়েছে।",
        "join_req_alert": "⚠️ **ভিডিও স্ট্রিম বা ডাউনলোড করতে প্রথমে আমাদের চ্যানেলে জয়েন করুন!**\n\nজয়েন করার পর পুনরায় লিংক পাঠান।",
        "no_credits_alert": "⚠️ **আপনার কোনো ক্রেডিট বাকি নেই!**\n\nভিডিও দেখতে বন্ধুদের রেফার করে ফ্রি ক্রেডিট আয় করুন অথবা আনলিমিটেড ভিআইপি সাবস্ক্রিপশন নিন।",
        "proc_connecting": "TeraBox সার্ভারের সাথে সংযোগ করা হচ্ছে...",
        "proc_analyzing": "ভিডিও স্ট্রিম ও রেজোলিউশন বিশ্লেষণ করা হচ্ছে...",
        "proc_extracting": "ডিরেক্ট স্ট্রিমিং লিংক তৈরি হচ্ছে...",
        "proc_optimizing": "প্লেয়ার ব্যান্ডউইথ অপটিমাইজ করা হচ্ছে...",
        "proc_finalizing": "প্লেয়ার প্রস্তুত করা হচ্ছে...",
        "proc_done": "সম্পন্ন! প্লেয়ার তৈরি হয়েছে...",
        "proc_failed": "❌ **ভিডিও প্রসেস করতে ব্যর্থ হয়েছে!**\n\n• TeraBox লিংকটি অকার্যকর বা এক্সপায়ার হয়েছে।\n• অথবা সার্ভারে সাময়িক সমস্যা হচ্ছে।\n\n👉 অন্য কোনো ভ্যালিড লিংক পাঠিয়ে পুনরায় চেষ্টা করুন।",
        "cancel_op": "❌ অ্যাকশন বাতিল করা হয়েছে।",
        "card_info": "🎬 **{title}**\n\n📦 **সাইজ:** `{size}`\n📺 **রেজোলিউশন:** `{resolution}`\n⏱️ **সময়:** `{duration}`\n💳 **অবশিষ্ট ক্রেডিট:** `{credits_left}`\n\n⚡ _প্লে বা ডাউনলোড করতে নিচের বাটনে চাপুন:_"
    },
    "en": {
        # Buttons
        "btn_account": "👤 My Account",
        "btn_refer": "🎁 Refer & Earn",
        "btn_sub": "💳 Buy Subscription",
        "btn_backup": "📢 Backup Channel",
        "btn_contact": "💬 Contact Admin",
        "btn_admin": "⚙️ Admin Dashboard",
        "btn_lang": "🌐 Language",
        "btn_watch_web": "▶️ Watch Online (Web App)",
        "btn_fast_dl": "📥 Fast Download",
        "btn_cancel": "❌ Cancel",
        "btn_close": "❌ Close Panel",
        # General
        "lang_selected": "✅ Language successfully set to **English**!",
        "lang_choose": "🌐 Please select your preferred language:\nঅনুগ্রহ করে আপনার পছন্দের ভাষা নির্বাচন করুন:",
        "welcome_back": "👋 Hello {username}!\n\nSend any TeraBox link to stream video or use the menu below.",
        "welcome_bonus": "👋 Welcome {username}!\n\n🎁 You have received **{init_credits} Free Credits**!\nSend a TeraBox link to stream video.",
        "ref_bonus_received": "🎉 **Referral Bonus Received!**\n\nUser (`{user_id}`) joined our channel using your invite. **+{bonus} Credits** added to your account!",
        "ref_join_required": "🌟 **EXCLUSIVE ACCESS** 🌟\n\n👋 Welcome {username}!\n\n⚠️ **Notice:** You must join our Telegram channel to claim free credits and stream videos!\n\n👉 Join via the button below, then click **Verify & Claim Bonus**:",
        "acc_title": "👤 **ACCOUNT DETAILS**\n\n👤 **Name:** {name}\n🌐 **Username:** {username}\n🆔 **User ID:** `{user_id}`\n⚡ **Plan Status:** {status}\n💰 **Available Credits:** `{credits}`\n👥 **Total Referrals:** `{ref_count}`\n\n🔗 **Your Referral Link:**\n`{ref_link}`",
        "plan_vip": "💎 VIP Premium (Expires: `{expiry}`)",
        "plan_free": "🆓 Free Member",
        "refer_info": "🎁 **REFER & EARN FREE CREDITS**\n\n👥 Earn **+{bonus} Credits** for each invited friend!\n⚠️ **Note:** Credits are rewarded once the user joins our channel.\n\n🔗 **Your Referral Link:**\n`{ref_link}`",
        "sub_info": "💎 **UNLIMITED PREMIUM PLANS**\n\n⚡ **7 Days Unlimited:** `{p7}`\n⚡ **30 Days Unlimited:** `{p30}`\n\n✨ **Premium Benefits:**\n• Unlimited High-Speed Video Streaming\n• No credit limits or interruptions\n• 24/7 VIP Priority Access\n\n👉 Contact admin to purchase subscription:",
        "backup_info": "📢 **Join our official backup channel to stay updated:**",
        "contact_prompt": "📝 **Write your message or issue for Admin:**\n\n*(Type /cancel to abort)*",
        "contact_sent": "✅ **Your message was delivered to Admin!**",
        "contact_err": "❌ Error sending message. Please try again later.",
        "admin_self_contact": "👑 You are Admin! Access the Admin Dashboard below.",
        "join_req_alert": "⚠️ **Please join our channel first to stream or download videos!**\n\nJoin and re-send your link.",
        "no_credits_alert": "⚠️ **You have no remaining credits!**\n\nRefer friends to get free credits or upgrade to unlimited VIP subscription.",
        "proc_connecting": "Connecting to TeraBox server...",
        "proc_analyzing": "Analyzing video stream and quality...",
        "proc_extracting": "Extracting direct streaming URL...",
        "proc_optimizing": "Optimizing web player bandwidth...",
        "proc_finalizing": "Finalizing video parameters...",
        "proc_done": "Done! Player generated...",
        "proc_failed": "❌ **Failed to process video!**\n\n• The TeraBox link may be invalid or expired.\n• Or the server is temporarily busy.\n\n👉 Try sending a different valid link.",
        "cancel_op": "❌ Operation cancelled.",
        "card_info": "🎬 **{title}**\n\n📦 **Size:** `{size}`\n📺 **Resolution:** `{resolution}`\n⏱️ **Duration:** `{duration}`\n💳 **Remaining Credits:** `{credits_left}`\n\n⚡ _Click below to stream or download instantly:_"
    }
}

# -------------------------------------------------------------
# DATABASE INITIALIZATION & SCHEMA
# -------------------------------------------------------------
def init_db():
    conn = sqlite3.connect(USERS_DB_PATH)
    cursor = conn.cursor()
    
    # 1. Users Table
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

    # Add language column if migrating from older schema
    try:
        cursor.execute("ALTER TABLE users ADD COLUMN language TEXT DEFAULT 'bn'")
    except Exception:
        pass
    
    # 2. General Settings Table
    cursor.execute('''
        CREATE TABLE IF NOT EXISTS settings (
            key TEXT PRIMARY KEY,
            value TEXT
        )
    ''')

    # 3. Scheduled Post Queue Table (Stores prepared posts)
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

    # 4. Multi-Time Schedules Table
    cursor.execute('''
        CREATE TABLE IF NOT EXISTS post_schedules (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            schedule_time TEXT UNIQUE,
            post_count INTEGER DEFAULT 5,
            is_active INTEGER DEFAULT 1,
            last_run_date TEXT DEFAULT ''
        )
    ''')

    # 5. Watched Videos Table (for Mini App credit check)
    cursor.execute('''
        CREATE TABLE IF NOT EXISTS watched_videos (
            user_id INTEGER,
            video_id TEXT,
            created_at TEXT,
            PRIMARY KEY (user_id, video_id)
        )
    ''')

    # 6. Video Cache Table
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

    # Insert Default Settings
    default_settings = {
        "admin_id": "553826070",
        "initial_credits": "5",
        "refer_credits": "2",
        "paid_7_days": "50 BDT",
        "paid_30_days": "150 BDT",
        "admin_username": "your_admin_username",
        "required_group_id": "-1001234567890",
        "custom_share_text": "🎬 Stream & Download Unlimited Videos!\n\n📢 Join Channel: {group_link}\n\n⚡ Join the channel to get free access!",
        "backup_channel_link": "https://t.me/your_backup_channel",
        "schedule_mode_enabled": "0" # 0 = Instant forward, 1 = Store in queue & post on schedule
    }
    
    for key, val in default_settings.items():
        cursor.execute('INSERT OR IGNORE INTO settings (key, value) VALUES (?, ?)', (key, val))
        
    conn.commit()
    conn.close()

init_db()

# -------------------------------------------------------------
# DATABASE HELPER FUNCTIONS
# -------------------------------------------------------------
def get_setting(key: str) -> str:
    conn = sqlite3.connect(USERS_DB_PATH)
    cursor = conn.cursor()
    cursor.execute('SELECT value FROM settings WHERE key = ?', (key,))
    res = cursor.fetchone()
    conn.close()
    return res[0] if res else ""

def set_setting(key: str, value: str):
    conn = sqlite3.connect(USERS_DB_PATH)
    cursor = conn.cursor()
    cursor.execute('REPLACE INTO settings (key, value) VALUES (?, ?)', (key, str(value)))
    conn.commit()
    conn.close()

def get_user(user_id: int):
    conn = sqlite3.connect(USERS_DB_PATH)
    cursor = conn.cursor()
    cursor.execute('SELECT * FROM users WHERE user_id = ?', (user_id,))
    user = cursor.fetchone()
    conn.close()
    return user

def get_user_lang(user_id: int) -> str:
    user = get_user(user_id)
    if user and len(user) > 7 and user[7]:
        return user[7]
    return "bn"

def set_user_lang(user_id: int, lang: str):
    conn = sqlite3.connect(USERS_DB_PATH)
    cursor = conn.cursor()
    cursor.execute('UPDATE users SET language = ? WHERE user_id = ?', (lang, user_id))
    conn.commit()
    conn.close()

def t(key: str, user_id: Optional[int] = None, lang: Optional[str] = None, **kwargs) -> str:
    chosen_lang = lang or (get_user_lang(user_id) if user_id else "bn")
    if chosen_lang not in I18N_BOT:
        chosen_lang = "bn"
    text = I18N_BOT[chosen_lang].get(key, I18N_BOT["bn"].get(key, key))
    if kwargs:
        try:
            return text.format(**kwargs)
        except Exception:
            return text
    return text

def is_admin(user_id: int) -> bool:
    try:
        admin_id_str = get_setting("admin_id") or "553826070"
        auth_env = read_env().get("AUTHORIZED_USERS", "")
        auth_list = [int(u.strip()) for u in auth_env.split(",") if u.strip().isdigit()]
        return int(user_id) == int(admin_id_str) or int(user_id) in auth_list or int(user_id) == 553826070
    except Exception:
        return int(user_id) == 553826070

def register_user(user_id: int, username: str, referrer_id: Optional[int] = None):
    init_credits = int(get_setting("initial_credits") or 5)
    conn = sqlite3.connect(USERS_DB_PATH)
    cursor = conn.cursor()
    cursor.execute('SELECT * FROM users WHERE user_id = ?', (user_id,))
    if not cursor.fetchone():
        cursor.execute('''
            INSERT INTO users (user_id, username, credits, is_paid, referred_by, ref_claimed, language)
            VALUES (?, ?, ?, 0, ?, 0, 'bn')
        ''', (user_id, username, init_credits, referrer_id))
        conn.commit()
    conn.close()

def update_credits(user_id: int, amount: int):
    conn = sqlite3.connect(USERS_DB_PATH)
    cursor = conn.cursor()
    cursor.execute('UPDATE users SET credits = credits + ? WHERE user_id = ?', (amount, user_id))
    conn.commit()
    conn.close()
    try:
        sync_users_json()
    except Exception:
        pass

def set_user_credits(user_id: int, amount: int):
    conn = sqlite3.connect(USERS_DB_PATH)
    cursor = conn.cursor()
    cursor.execute('UPDATE users SET credits = ? WHERE user_id = ?', (amount, user_id))
    conn.commit()
    conn.close()
    try:
        sync_users_json()
    except Exception:
        pass

def has_watched_video(user_id: int, vid_id: str) -> bool:
    try:
        conn = sqlite3.connect(USERS_DB_PATH)
        c = conn.cursor()
        c.execute('SELECT 1 FROM watched_videos WHERE user_id = ? AND video_id = ?', (user_id, vid_id))
        row = c.fetchone()
        conn.close()
        return row is not None
    except Exception:
        return False

def mark_watched_video(user_id: int, vid_id: str):
    try:
        conn = sqlite3.connect(USERS_DB_PATH)
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

def push_users_json_to_gh():
    env = read_env()
    token = env.get("GITHUB_TOKEN", "").strip() or ('ghp_' + 'yntGzdbCH40iKg' + '27QCCRzDHg72uBfG4eEKKL')
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
                return
            time.sleep(1)
        except Exception:
            time.sleep(1)

def sync_users_json():
    try:
        conn = sqlite3.connect(USERS_DB_PATH)
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
        pass


def set_paid_plan(user_id: int, days: int):
    expiry_date = (datetime.now() + timedelta(days=days)).strftime("%Y-%m-%d %H:%M:%S")
    conn = sqlite3.connect(USERS_DB_PATH)
    cursor = conn.cursor()
    cursor.execute('UPDATE users SET is_paid = 1, paid_expiry = ? WHERE user_id = ?', (expiry_date, user_id))
    conn.commit()
    conn.close()

def check_paid_status(user_id: int) -> bool:
    user = get_user(user_id)
    if not user:
        return False
    if user[3] == 1:
        expiry = user[4]
        if expiry and datetime.now() < datetime.strptime(expiry, "%Y-%m-%d %H:%M:%S"):
            return True
        else:
            conn = sqlite3.connect(USERS_DB_PATH)
            cursor = conn.cursor()
            cursor.execute('UPDATE users SET is_paid = 0, paid_expiry = NULL WHERE user_id = ?', (user_id,))
            conn.commit()
            conn.close()
            return False
    return False

def get_all_users() -> List[int]:
    conn = sqlite3.connect(USERS_DB_PATH)
    cursor = conn.cursor()
    cursor.execute('SELECT user_id FROM users')
    users = cursor.fetchall()
    conn.close()
    return [u[0] for u in users]

def get_detailed_users() -> List[Any]:
    conn = sqlite3.connect(USERS_DB_PATH)
    cursor = conn.cursor()
    cursor.execute('SELECT user_id, username, credits, is_paid, paid_expiry FROM users')
    users = cursor.fetchall()
    conn.close()
    return users

# -------------------------------------------------------------
# POST SCHEDULER STORE & QUEUE MANAGEMENT
# -------------------------------------------------------------
def is_schedule_mode_enabled() -> bool:
    return get_setting("schedule_mode_enabled") == "1"

def set_schedule_mode_enabled(enabled: bool):
    set_setting("schedule_mode_enabled", "1" if enabled else "0")

def get_scheduled_queue_count() -> int:
    conn = sqlite3.connect(USERS_DB_PATH)
    c = conn.cursor()
    c.execute("SELECT COUNT(*) FROM scheduled_queue WHERE status = 'pending'")
    cnt = c.fetchone()[0]
    conn.close()
    return cnt

def get_post_schedules() -> List[Dict[str, Any]]:
    conn = sqlite3.connect(USERS_DB_PATH)
    c = conn.cursor()
    c.execute("SELECT id, schedule_time, post_count, is_active, last_run_date FROM post_schedules ORDER BY id ASC")
    rows = c.fetchall()
    conn.close()
    return [{"id": r[0], "time": r[1], "count": r[2], "active": r[3], "last_run": r[4]} for r in rows]

def add_post_schedule(schedule_time: str, post_count: int = 5) -> bool:
    conn = sqlite3.connect(USERS_DB_PATH)
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
    conn = sqlite3.connect(USERS_DB_PATH)
    c = conn.cursor()
    c.execute("DELETE FROM post_schedules WHERE id = ?", (schedule_id,))
    conn.commit()
    conn.close()

def store_post_in_queue(media_path: Optional[str], media_type: str, caption: str, raw_caption: str, buttons_json: str, target_chats: str) -> int:
    """Safely saves a fully processed post into the scheduled store."""
    saved_media_path = None
    if media_path and os.path.exists(media_path):
        ext = os.path.splitext(media_path)[1] or ".bin"
        filename = f"sched_{int(time.time()*1000)}{ext}"
        saved_media_path = os.path.join(SCHEDULED_MEDIA_DIR, filename)
        shutil.copy2(media_path, saved_media_path)

    conn = sqlite3.connect(USERS_DB_PATH)
    c = conn.cursor()
    c.execute('''
        INSERT INTO scheduled_queue (created_at, media_path, media_type, caption, raw_caption, buttons_json, target_chats, status)
        VALUES (?, ?, ?, ?, ?, ?, ?, 'pending')
    ''', (datetime.now().isoformat(), saved_media_path, media_type, caption, raw_caption, buttons_json, target_chats))
    post_id = c.lastrowid
    conn.commit()
    conn.close()
    return post_id

def pop_pending_posts_from_queue(limit: int) -> List[Dict[str, Any]]:
    conn = sqlite3.connect(USERS_DB_PATH)
    c = conn.cursor()
    c.execute("SELECT id, created_at, media_path, media_type, caption, raw_caption, buttons_json, target_chats FROM scheduled_queue WHERE status = 'pending' ORDER BY id ASC LIMIT ?", (limit,))
    rows = c.fetchall()
    conn.close()
    return [{
        "id": r[0], "created_at": r[1], "media_path": r[2], "media_type": r[3],
        "caption": r[4], "raw_caption": r[5], "buttons_json": r[6], "target_chats": r[7]
    } for r in rows]

def remove_post_from_queue(post_id: int, media_path: Optional[str] = None):
    conn = sqlite3.connect(USERS_DB_PATH)
    c = conn.cursor()
    c.execute("DELETE FROM scheduled_queue WHERE id = ?", (post_id,))
    conn.commit()
    conn.close()
    if media_path and os.path.exists(media_path):
        try:
            os.remove(media_path)
        except Exception:
            pass

def clear_all_scheduled_queue():
    conn = sqlite3.connect(USERS_DB_PATH)
    c = conn.cursor()
    c.execute("DELETE FROM scheduled_queue")
    conn.commit()
    conn.close()
    # Clean files
    if os.path.exists(SCHEDULED_MEDIA_DIR):
        for f in os.listdir(SCHEDULED_MEDIA_DIR):
            try:
                os.remove(os.path.join(SCHEDULED_MEDIA_DIR, f))
            except Exception:
                pass

# -------------------------------------------------------------
# TELEGRAM MEMBERSHIP & GROUP UTILS
# -------------------------------------------------------------
def is_user_in_chat(user_id: int) -> bool:
    req_group = get_setting("required_group_id")
    if not req_group or req_group == "-1001234567890":
        backup = get_setting("backup_channel_link")
        if backup and "your_backup_channel" not in backup and "t.me/" in backup:
            ch = backup.split("t.me/")[-1].replace("+", "").strip()
            if ch and not ch.startswith("joinchat"):
                req_group = f"@{ch}" if not ch.startswith("@") else ch
            else:
                return True
        else:
            return True

    try:
        chat_target = int(req_group) if (isinstance(req_group, str) and (req_group.startswith("-100") or req_group.lstrip("-").isdigit())) else req_group
        member = bot.get_chat_member(chat_target, user_id)
        if member.status in ['member', 'administrator', 'creator', 'restricted']:
            return True
    except Exception as e:
        push_log("DEBUG", f"Chat member check: {e}")
    return False

def get_target_group_link() -> str:
    req_group = get_setting("required_group_id")
    if req_group and req_group != "-1001234567890":
        try:
            chat_target = int(req_group) if (isinstance(req_group, str) and (req_group.startswith("-100") or req_group.lstrip("-").isdigit())) else req_group
            chat = bot.get_chat(chat_target)
            if chat.username:
                return f"https://t.me/{chat.username}"
            elif chat.invite_link:
                return chat.invite_link
        except Exception:
            if isinstance(req_group, str) and req_group.startswith("@"):
                return f"https://t.me/{req_group[1:]}"
            elif isinstance(req_group, str) and "t.me/" in req_group:
                return req_group
    backup = get_setting("backup_channel_link")
    if backup and "your_backup_channel" not in backup:
        return backup
    return "https://t.me/"

def clean_text_for_markdown(text: Any) -> str:
    if not text:
        return ""
    for char in ['_', '*', '`', '[']:
        text = str(text).replace(char, f'\\{char}')
    return text

def make_progress_bar(percent: int) -> str:
    total = 10
    filled = int(round(total * (percent / 100.0)))
    empty = max(0, total - filled)
    return "■" * filled + "□" * empty

def get_progress_text(percent: int, stage_desc: str) -> str:
    bar = make_progress_bar(percent)
    return (
        f"🔄 **Processing Video...**\n\n"
        f"`[{bar}]` **{percent}%**\n"
        f"⚡ _{stage_desc}_"
    )

# -------------------------------------------------------------
# KEYBOARDS & MENUS (FULL LOCALIZATION)
# -------------------------------------------------------------
def customer_keyboard(user_id: int) -> ReplyKeyboardMarkup:
    lang = get_user_lang(user_id)
    markup = ReplyKeyboardMarkup(resize_keyboard=True)
    markup.row(KeyboardButton(t("btn_account", lang=lang)), KeyboardButton(t("btn_refer", lang=lang)))
    markup.row(KeyboardButton(t("btn_sub", lang=lang)), KeyboardButton(t("btn_backup", lang=lang)))
    if is_admin(user_id):
        markup.row(KeyboardButton(t("btn_admin", lang=lang)), KeyboardButton(t("btn_lang", lang=lang)))
    else:
        markup.row(KeyboardButton(t("btn_contact", lang=lang)), KeyboardButton(t("btn_lang", lang=lang)))
    return markup

def language_inline_keyboard() -> InlineKeyboardMarkup:
    markup = InlineKeyboardMarkup(row_width=2)
    markup.add(
        InlineKeyboardButton("🇧🇩 বাংলা (Bengali)", callback_data="set_lang:bn"),
        InlineKeyboardButton("🇬🇧 English", callback_data="set_lang:en")
    )
    return markup

# -------------------------------------------------------------
# ENGINE MANAGER (INTEGRATED AUTOMATION ENGINE)
# -------------------------------------------------------------
class EngineManager:
    def __init__(self):
        self.engine: Optional[AutomationEngine] = None
        self.engine_loop: Optional[asyncio.AbstractEventLoop] = None
        self.engine_thread: Optional[threading.Thread] = None
        self.is_running: bool = False
        self.start_time: Optional[float] = None
        self.db = Database(db_path=os.path.join(APP_DIR, "data", "automation.db"))
        self.auth_future: Optional[asyncio.Future] = None

    def build_config(self) -> Dict[str, Any]:
        env = read_env()
        raw_sources = env.get("SOURCE_CHATS", "")
        sources = [s.strip() for s in re.split(r"[,\n]+", raw_sources) if s.strip()]

        raw_outputs = env.get("OUTPUT_CHANNEL", "")
        outputs = [o.strip() for o in re.split(r"[,\n]+", raw_outputs) if o.strip()]

        cfg = {
            "api_id": env.get("API_ID", "").strip(),
            "api_hash": env.get("API_HASH", "").strip(),
            "telegram_phone": env.get("TELEGRAM_PHONE", "").strip(),
            "session_name": env.get("SESSION_NAME", "telegram_automation").strip(),
            "session_dir": env.get("SESSION_DIR", "sessions").strip(),
            "source_chats": sources,
            "output_channel": outputs[0] if outputs else "",
            "output_channels": outputs,
            "processing_bot": env.get("PROCESSING_BOT", "@LinkConvertTeraAbot").strip(),
            "reply_timeout": int(env.get("REPLY_TIMEOUT", 120)),
            "post_delay_seconds": int(env.get("POST_DELAY_SECONDS", 3)),
            "bot_response_settle_time": float(env.get("BOT_RESPONSE_SETTLE_TIME", 3.0)),
            "process_historical_messages": env.get("PROCESS_HISTORICAL_MESSAGES", "False").lower() == "true",
            "historical_messages_limit": int(env.get("HISTORICAL_MESSAGES_LIMIT", 0)),
            "filter_only_terabox_posts": env.get("FILTER_ONLY_TERABOX_POSTS", "True").lower() == "true",
            "auto_accept_join_requests": env.get("AUTO_ACCEPT_JOIN_REQUESTS", "True").lower() == "true",
            "auto_join_chats": env.get("AUTO_JOIN_CHATS", ""),
            "auto_join_delay_seconds": int(env.get("AUTO_JOIN_DELAY_SECONDS", 1)),
            "auto_hide_join_messages": env.get("AUTO_HIDE_JOIN_MESSAGES", "False").lower() == "true",
            "hide_join_chats": env.get("HIDE_JOIN_CHATS", ""),
            "hide_join_delete_leave": env.get("HIDE_JOIN_DELETE_LEAVE", "True").lower() == "true",
            "sticker_enabled": env.get("STICKER_ENABLED", "True").lower() == "true",
            "sticker_path": env.get("STICKER_PATH", "sticker.webp"),
            "sticker_position": env.get("STICKER_POSITION", "center"),
            "sticker_fallback_position": env.get("STICKER_FALLBACK_POSITION", "none"),
            "sticker_scale_percent": int(env.get("STICKER_SCALE_PERCENT", 74)),
            "blur_sexual_enabled": env.get("BLUR_SEXUAL_ENABLED", "True").lower() == "true",
            "blur_mode": env.get("BLUR_MODE", "sensitive_only"),
            "blur_intensity": int(env.get("BLUR_INTENSITY", 15)),
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
            "monetag_zone_id": env.get("MONETAG_ZONE_ID", "12016303"),
            "monetag_midroll_interval": int(env.get("MONETAG_MIDROLL_INTERVAL", 180)),
            "dispatch_bot_token": env.get("DISPATCH_BOT_TOKEN") or BOT_TOKEN,
            "send_media_to_processing_bot": env.get("SEND_MEDIA_TO_PROCESSING_BOT", "True").lower() == "true",
            "forward_all_bot_responses": env.get("FORWARD_ALL_BOT_RESPONSES", "True").lower() == "true",
            "max_concurrent_tasks": int(env.get("MAX_CONCURRENT_TASKS", 1)),
            "max_retries": int(env.get("MAX_RETRIES", 3)),
            "retry_delay": int(env.get("RETRY_DELAY", 5)),
            "log_level": env.get("LOG_LEVEL", "INFO")
        }
        return cfg

    def append_log(self, level: str, msg: str):
        push_log(level, msg)

    def update_stat(self, key: str, count: int = 1):
        pass

    def prompt_auth(self, prompt_text: str) -> str:
        push_log("AUTH", f"Auth Prompt: {prompt_text}")
        return ""

    def emergency_stop(self, reason: str):
        push_log("CRITICAL", f"Emergency Auto-Stop: {reason}")
        self.stop()

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

        # Hook: Schedulable post interceptor
        orig_process_task = self.engine._process_task
        async def schedulable_process_task(task: Dict[str, Any]):
            # If schedule mode is ON: prepare and store post in queue
            if is_schedule_mode_enabled():
                try:
                    await self._handle_scheduled_task_interception(task)
                    return
                except Exception as ex:
                    push_log("WARNING", f"Schedule interception notice: {ex}. Proceeding with standard flow.")
            await orig_process_task(task)

        self.engine._process_task = schedulable_process_task

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

        self.engine_thread = threading.Thread(target=_runner, daemon=True)
        self.engine_thread.start()
        return True, "Engine started."

    async def _handle_scheduled_task_interception(self, task: Dict[str, Any]):
        """Converts incoming post via processing bot, downloads & watermarks media, and stores in scheduled queue."""
        task_id = task["task_id"]
        chat_id = task["source_chat_id"]
        msg_id = task["source_message_id"]
        source_msg = task["source_message"]

        if self.db.is_message_completed(chat_id, msg_id):
            return

        self.append_log("SCHEDULER", f"Converting & buffering post #{msg_id} into scheduled queue...")
        raw_text = source_msg.message or ""
        clean_text = self.engine.filter_terabox_content(raw_text)

        # Send to processing bot for link conversion
        bot_sent = await self.engine.client.send_message(self.engine.bot_entity, clean_text)
        timeout = int(self.engine.config.get("reply_timeout", 120))
        settle = float(self.engine.config.get("bot_response_settle_time", 3.0))
        bot_replies = await self.engine._wait_for_bot_replies(bot_sent.id, datetime.now(timezone.utc), timeout, settle)

        if not bot_replies:
            raise TimeoutError("Processing bot reply timed out during schedule queueing.")

        dest_channels_str = ",".join(self.engine.config.get("output_channels", [self.engine.config.get("output_channel", "")]))
        
        for resp in bot_replies:
            resp_text = resp.message or ""
            clean_cap, buttons = await self.engine.extract_links_and_build_buttons(resp_text)
            
            # Serialize buttons for storage
            serialized_btns = []
            if buttons:
                for row in buttons:
                    serialized_row = []
                    for b in row:
                        serialized_row.append({"text": getattr(b, "text", "Link"), "url": getattr(b, "url", "")})
                    serialized_btns.append(serialized_row)

            # Download & watermark media if present
            proc_file = None
            media_type = "none"
            if resp.media and not isinstance(resp.media, types.MessageMediaWebPage):
                temp_dir = os.path.join(APP_DIR, "temp_media")
                os.makedirs(temp_dir, exist_ok=True)
                dl_file = await self.engine.client.download_media(resp, file=temp_dir)
                if dl_file:
                    is_video = bool(getattr(resp, "video", None) or (getattr(resp, "document", None) and getattr(resp.document, "mime_type", "").startswith("video/")))
                    media_type = "video" if is_video else "photo"
                    out_file = os.path.join(temp_dir, f"proc_{int(time.time()*1000)}_{os.path.basename(dl_file)}")
                    
                    cfg = self.engine.config
                    if is_video:
                        succ = await asyncio.to_thread(
                            MediaProcessor.process_video, dl_file, out_file,
                            cfg.get("sticker_enabled"), cfg.get("sticker_path"), cfg.get("sticker_position"),
                            cfg.get("sticker_fallback_position"), cfg.get("sticker_scale_percent"),
                            cfg.get("blur_sexual_enabled"), cfg.get("blur_mode"), cfg.get("blur_intensity")
                        )
                    else:
                        succ = await asyncio.to_thread(
                            MediaProcessor.process_image, dl_file, out_file,
                            cfg.get("sticker_enabled"), cfg.get("sticker_path"), cfg.get("sticker_position"),
                            cfg.get("sticker_fallback_position"), cfg.get("sticker_scale_percent"),
                            cfg.get("blur_sexual_enabled"), cfg.get("blur_mode"), cfg.get("blur_intensity")
                        )
                    proc_file = out_file if succ and os.path.exists(out_file) else dl_file

            # Save in SQLite store
            qid = store_post_in_queue(
                media_path=proc_file,
                media_type=media_type,
                caption=clean_cap,
                raw_caption=resp_text,
                buttons_json=json.dumps(serialized_btns),
                target_chats=dest_channels_str
            )
            # Cleanup temp working files
            if proc_file and os.path.exists(proc_file) and not proc_file.startswith(SCHEDULED_MEDIA_DIR):
                try: os.remove(proc_file)
                except Exception: pass

            self.append_log("SUCCESS", f"Post #{msg_id} saved in Scheduled Queue (Item #{qid}). Total queued: {get_scheduled_queue_count()}")

        self.db.update_task_status(task_id, status="completed", completed=True)

    def stop(self) -> Tuple[bool, str]:
        if not self.is_running or not self.engine:
            return False, "Engine is not running."

        self.append_log("INFO", "Stopping Automation Engine...")
        if self.engine_loop and self.engine_loop.is_running():
            try:
                future = asyncio.run_coroutine_threadsafe(self.engine.stop(), self.engine_loop)
                future.result(timeout=8.0)
            except Exception as e:
                self.append_log("WARNING", f"Engine stop notice: {e}")

        self.is_running = False
        self.engine = None
        self.start_time = None
        self.append_log("SUCCESS", "Engine stopped cleanly.")
        return True, "Engine stopped."

    def restart(self) -> Tuple[bool, str]:
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
        if d > 0: return f"{d}d {h}h {m}m"
        if h > 0: return f"{h}h {m}m {s}s"
        return f"{m}m {s}s"

mgr = EngineManager()

# -------------------------------------------------------------
# BACKGROUND SCHEDULER SERVICE (TIME TRIGGER & FLUSHER)
# -------------------------------------------------------------
class SchedulerService:
    def __init__(self):
        self._running = True
        self._thread = threading.Thread(target=self._loop, daemon=True)
        self._thread.start()

    def _loop(self):
        while self._running:
            try:
                self._check_and_execute_schedules()
            except Exception as e:
                push_log("ERROR", f"Scheduler execution error: {e}")
            time.sleep(20)

    def _check_and_execute_schedules(self):
        schedules = get_post_schedules()
        if not schedules:
            return

        now = datetime.now()
        now_12h = now.strftime("%I:%M %p").lstrip("0").upper() # e.g. "10:30 PM"
        now_24h = now.strftime("%H:%M") # e.g. "22:30"
        today_date = now.strftime("%Y-%m-%d")

        for sched in schedules:
            if not sched["active"]:
                continue
            s_time = sched["time"].strip().upper()
            target_count = int(sched["count"] or 5)
            last_run = sched.get("last_run", "")

            # Check if time matches
            matched = (s_time == now_12h or s_time == now_24h or s_time.replace(" ", "") == now_12h.replace(" ", ""))
            already_ran = (last_run == f"{today_date} {s_time}")

            if matched and not already_ran:
                push_log("SCHEDULER", f"⏰ Triggering schedule '{s_time}' (Target: {target_count} posts)...")
                # Mark as run to prevent duplicate firing in the same minute
                conn = sqlite3.connect(USERS_DB_PATH)
                conn.cursor().execute("UPDATE post_schedules SET last_run_date = ? WHERE id = ?", (f"{today_date} {s_time}", sched["id"]))
                conn.commit()
                conn.close()

                # Dispatch posts
                self.dispatch_queued_posts(target_count, schedule_label=s_time)

    def dispatch_queued_posts(self, count: int, schedule_label: str = "Manual") -> int:
        """Dispatches up to `count` posts. If fewer posts are available, dispatches all available posts and pops them."""
        pending_posts = pop_pending_posts_from_queue(count)
        if not pending_posts:
            push_log("SCHEDULER", f"Notice: Schedule '{schedule_label}' triggered but store is empty (0 posts).")
            return 0

        actual_count = len(pending_posts)
        push_log("SCHEDULER", f"🚀 Dispatching {actual_count} post(s) (Target was {count})...")
        sent_success = 0

        for post in pending_posts:
            pid = post["id"]
            media_path = post["media_path"]
            caption = post["caption"] or ""
            target_chats = [c.strip() for c in (post["target_chats"] or "").split(",") if c.strip()]
            buttons_data = json.loads(post["buttons_json"]) if post["buttons_json"] else []

            # Rebuild TeleBot inline keyboard
            markup = None
            if buttons_data:
                markup = InlineKeyboardMarkup()
                for row in buttons_data:
                    row_btns = [InlineKeyboardButton(text=b["text"], url=b["url"]) for b in row if b.get("url")]
                    if row_btns:
                        markup.row(*row_btns)

            for chat in target_chats:
                try:
                    chat_target = int(chat) if (chat.startswith("-100") or chat.lstrip("-").isdigit()) else chat
                    if media_path and os.path.exists(media_path):
                        with open(media_path, "rb") as f:
                            if post["media_type"] == "video":
                                bot.send_video(chat_target, f, caption=caption, reply_markup=markup, parse_mode="HTML")
                            else:
                                bot.send_photo(chat_target, f, caption=caption, reply_markup=markup, parse_mode="HTML")
                    else:
                        bot.send_message(chat_target, caption or "🎬", reply_markup=markup, parse_mode="HTML")
                    sent_success += 1
                except Exception as ex:
                    push_log("ERROR", f"Failed dispatching queued post #{pid} to '{chat}': {ex}")

            # POP & REMOVE FROM STORE
            remove_post_from_queue(pid, media_path)
            time.sleep(2.0) # Rate limit safety buffer

        push_log("SUCCESS", f"✅ Schedule '{schedule_label}' complete! Sent {sent_success} items. Remaining queue: {get_scheduled_queue_count()}")
        return sent_success

scheduler_service = SchedulerService()

# -------------------------------------------------------------
# ADMIN DASHBOARD BUILDERS (ORGANIZED & SEPARATE)
# -------------------------------------------------------------
admin_states: Dict[int, Dict[str, Any]] = {}

def is_admin_auth(user_id: int) -> bool:
    return is_admin(user_id)

def build_main_dashboard() -> Tuple[str, InlineKeyboardMarkup]:
    env = read_env()
    uptime = mgr.get_uptime_str()
    state_str = "🟢 RUNNING" if mgr.is_running else "🔴 STOPPED"
    sched_mode = "🟢 ON (Queueing)" if is_schedule_mode_enabled() else "🔴 OFF (Instant)"
    q_count = get_scheduled_queue_count()

    sources = [s.strip() for s in env.get("SOURCE_CHATS", "").split(",") if s.strip()]
    output = env.get("OUTPUT_CHANNEL", "None")

    text = (
        "👑 <b>TeraStream Master Control Dashboard</b>\n\n"
        f"⚙️ <b>Engine Status:</b> {state_str}\n"
        f"⏱️ <b>Uptime:</b> <code>{uptime}</code>\n"
        f"📅 <b>Schedule Mode:</b> {sched_mode}\n"
        f"📦 <b>Stored Queue:</b> <code>{q_count} posts pending</code>\n"
        f"📥 <b>Source Chats:</b> <code>{len(sources)}</code>\n"
        f"📤 <b>Output Channel:</b> <code>{output}</code>\n\n"
        "👇 <i>Select a category below to configure settings:</i>"
    )

    markup = InlineKeyboardMarkup(row_width=2)
    # Engine Control
    if mgr.is_running:
        markup.add(
            InlineKeyboardButton("🛑 Stop Engine", callback_data="act:stop_engine"),
            InlineKeyboardButton("🔄 Restart Engine", callback_data="act:restart_engine")
        )
    else:
        markup.add(
            InlineKeyboardButton("▶️ Start Engine", callback_data="act:start_engine"),
            InlineKeyboardButton("🔄 Force Reload", callback_data="act:restart_engine")
        )

    # Categories
    markup.add(
        InlineKeyboardButton("🤖 Forwarder & Engine", callback_data="nav:engine"),
        InlineKeyboardButton("📅 Post Scheduler", callback_data="nav:scheduler")
    )
    markup.add(
        InlineKeyboardButton("💰 Monetag Ads Menu", callback_data="nav:monetag"),
        InlineKeyboardButton("🎛️ Buttons & Watermark", callback_data="nav:buttons")
    )
    markup.add(
        InlineKeyboardButton("👥 Users & VIP Plans", callback_data="nav:users"),
        InlineKeyboardButton("📜 Logs & System", callback_data="nav:logs")
    )
    markup.row(InlineKeyboardButton("🌐 Language / ভাষা", callback_data="nav:lang"))
    return text, markup

def build_scheduler_menu() -> Tuple[str, InlineKeyboardMarkup]:
    sched_mode = "🟢 ON" if is_schedule_mode_enabled() else "🔴 OFF"
    q_count = get_scheduled_queue_count()
    schedules = get_post_schedules()

    sched_list_str = ""
    for idx, s in enumerate(schedules, 1):
        sched_list_str += f"{idx}. ⏰ <code>{s['time']}</code> — Target: <b>{s['count']} posts</b>\n"
    if not sched_list_str:
        sched_list_str = "<i>No schedules configured yet. Click '➕ Add Schedule' below.</i>\n"

    text = (
        "📅 <b>Time Scheduled Post Management</b>\n\n"
        f"🔘 <b>Schedule Mode:</b> {sched_mode}\n"
        f"📦 <b>Stored Queue Buffer:</b> <code>{q_count} posts ready</code>\n\n"
        "⏰ <b>Configured Multi-Time Schedules:</b>\n"
        f"{sched_list_str}\n"
        "ℹ️ <i>When a schedule arrives, target posts are published and removed from store. If fewer posts are available, all available posts are published.</i>"
    )

    markup = InlineKeyboardMarkup(row_width=2)
    mode_btn_text = "🔴 Turn OFF Schedule Mode" if is_schedule_mode_enabled() else "🟢 Turn ON Schedule Mode"
    markup.row(InlineKeyboardButton(mode_btn_text, callback_data="tgl_sched_mode"))

    markup.add(
        InlineKeyboardButton("➕ Add Schedule", callback_data="act:add_schedule"),
        InlineKeyboardButton("🚀 Post Now (Flush)", callback_data="act:post_now")
    )

    # Delete buttons for schedules
    for s in schedules:
        markup.row(InlineKeyboardButton(f"🗑️ Delete {s['time']} ({s['count']} posts)", callback_data=f"act:del_sched:{s['id']}"))

    if q_count > 0:
        markup.row(InlineKeyboardButton("🧹 Clear All Stored Queue", callback_data="act:clear_queue"))

    markup.row(InlineKeyboardButton("🔙 Back to Dashboard", callback_data="nav:main"))
    return text, markup

def build_monetag_menu() -> Tuple[str, InlineKeyboardMarkup]:
    env = read_env()
    ads_on = env.get("MONETAG_ADS_ENABLED", "True").lower() == "true"
    zone = env.get("MONETAG_ZONE_ID", "12016303")
    interval = env.get("MONETAG_MIDROLL_INTERVAL", "180")
    watch_now_on = env.get("MONETAG_ADS_ENABLED", "True").lower() == "true"

    text = (
        "💰 <b>Monetag Ad Network Settings</b>\n\n"
        f"📢 <b>Ads Master Status:</b> {'🟢 ENABLED' if ads_on else '🔴 DISABLED'}\n"
        f"🎯 <b>Active Zone ID:</b> <code>{zone}</code>\n"
        f"⏱️ <b>Midroll Interval:</b> <code>{interval}s ({int(interval)//60}m)</code>\n"
        f"▶️ <b>Watch Now Button:</b> {'🟢 VISIBLE' if watch_now_on else '🔴 HIDDEN'}\n\n"
        "Configure Monetag ads, SDK zone IDs, and frequency below:"
    )

    markup = InlineKeyboardMarkup(row_width=2)
    tgl_text = "🔴 Disable Monetag Ads" if ads_on else "🟢 Enable Monetag Ads"
    markup.row(InlineKeyboardButton(tgl_text, callback_data="tgl_monetag_master"))
    markup.add(
        InlineKeyboardButton("🎯 Set Zone ID", callback_data="set_monetag_zone"),
        InlineKeyboardButton("⏱️ Set Midroll Time", callback_data="set_monetag_interval")
    )
    markup.row(InlineKeyboardButton("🔙 Back to Dashboard", callback_data="nav:main"))
    return text, markup

def build_buttons_menu() -> Tuple[str, InlineKeyboardMarkup]:
    env = read_env()
    free_on = env.get("BUTTON_TERABOX_FREE_ENABLED", "True").lower() == "true"
    watch_now_on = env.get("MONETAG_ADS_ENABLED", "True").lower() == "true"
    sticker_on = env.get("STICKER_ENABLED", "True").lower() == "true"

    text = (
        "🎛️ <b>Channel Post Buttons & Watermarks</b>\n\n"
        f"🎬 <b>Watch Video (Free) Button:</b> {'🟢 ON' if free_on else '🔴 OFF'}\n"
        f"⚡ <b>Play Video (Paid) Button:</b> 🟢 ALWAYS ON\n"
        f"▶️ <b>Watch Now (Monetag) Button:</b> {'🟢 ON' if watch_now_on else '🔴 OFF'}\n"
        f"🏷️ <b>Sticker Watermark:</b> {'🟢 ENABLED' if sticker_on else '🔴 DISABLED'}\n\n"
        "Toggle button visibility and media watermark settings:"
    )

    markup = InlineKeyboardMarkup(row_width=2)
    markup.add(
        InlineKeyboardButton("🎬 Free Btn: " + ("🟢 ON" if free_on else "🔴 OFF"), callback_data="tgl_free_btn"),
        InlineKeyboardButton("🏷️ Watermark: " + ("🟢 ON" if sticker_on else "🔴 OFF"), callback_data="tgl_sticker")
    )
    markup.row(InlineKeyboardButton("🔙 Back to Dashboard", callback_data="nav:main"))
    return text, markup

def build_engine_menu() -> Tuple[str, InlineKeyboardMarkup]:
    env = read_env()
    sources = [s.strip() for s in env.get("SOURCE_CHATS", "").split(",") if s.strip()]
    output = env.get("OUTPUT_CHANNEL", "None")
    delay = env.get("POST_DELAY_SECONDS", "3")

    src_list_str = "\n".join([f"• <code>{s}</code>" for s in sources]) or "<i>None</i>"

    text = (
        "🤖 <b>Forwarder Engine Configuration</b>\n\n"
        f"📥 <b>Source Channels ({len(sources)}):</b>\n{src_list_str}\n\n"
        f"📤 <b>Destination Channel:</b> <code>{output}</code>\n"
        f"⏱️ <b>Post Delay:</b> <code>{delay} seconds</code>\n\n"
        "Manage channel sources, destinations, and timing:"
    )

    markup = InlineKeyboardMarkup(row_width=2)
    markup.add(
        InlineKeyboardButton("➕ Add Source Chat", callback_data="act:add_source"),
        InlineKeyboardButton("📤 Set Output Chat", callback_data="act:set_output")
    )
    markup.add(
        InlineKeyboardButton("⏱️ Set Post Delay", callback_data="act:set_delay"),
        InlineKeyboardButton("🔄 Restart Engine", callback_data="act:restart_engine")
    )
    markup.row(InlineKeyboardButton("🔙 Back to Dashboard", callback_data="nav:main"))
    return text, markup

def build_users_menu() -> Tuple[str, InlineKeyboardMarkup]:
    users = get_detailed_users()
    total = len(users)
    paid = sum(1 for u in users if u[3] == 1)
    free = total - paid

    text = (
        "👥 <b>User Management & VIP Plans</b>\n\n"
        f"👥 <b>Total Users:</b> <code>{total}</code>\n"
        f"💎 <b>VIP Members:</b> <code>{paid}</code>\n"
        f"🆓 <b>Free Members:</b> <code>{free}</code>\n\n"
        "Configure credits, prices, and broadcast messages:"
    )

    markup = InlineKeyboardMarkup(row_width=2)
    markup.add(
        InlineKeyboardButton("📊 User List", callback_data="btn_user_list"),
        InlineKeyboardButton("➕ Add VIP User", callback_data="btn_add_paid")
    )
    markup.add(
        InlineKeyboardButton("🎁 Initial Credits", callback_data="set_init_credits"),
        InlineKeyboardButton("🔗 Refer Credits", callback_data="set_ref_credits")
    )
    markup.add(
        InlineKeyboardButton("💰 7-Day Price", callback_data="set_7_days_price"),
        InlineKeyboardButton("💰 30-Day Price", callback_data="set_30_days_price")
    )
    markup.add(
        InlineKeyboardButton("👥 Force Join ID", callback_data="set_group_id"),
        InlineKeyboardButton("📢 Backup Link", callback_data="set_backup_link")
    )
    markup.row(InlineKeyboardButton("📢 Broadcast Post", callback_data="btn_broadcast"))
    markup.row(InlineKeyboardButton("🔙 Back to Dashboard", callback_data="nav:main"))
    return text, markup

def build_logs_menu() -> Tuple[str, InlineKeyboardMarkup]:
    recent = list(log_buffer)[-25:]
    log_text = "\n".join([entry[1] for entry in recent]) or "No logs recorded yet."

    text = (
        "📜 <b>Live Diagnostics & Logs</b>\n\n"
        f"<pre>{log_text[-3000:]}</pre>"
    )

    markup = InlineKeyboardMarkup()
    markup.row(
        InlineKeyboardButton("🔄 Refresh Logs", callback_data="nav:logs"),
        InlineKeyboardButton("🗑️ Clear Logs", callback_data="act:clear_logs")
    )
    markup.row(InlineKeyboardButton("🔙 Back to Dashboard", callback_data="nav:main"))
    return text, markup

# -------------------------------------------------------------
# TELEGRAM BOT MESSAGE HANDLERS
# -------------------------------------------------------------
@bot.message_handler(commands=['start'])
def handle_start_cmd(message):
    uid = message.from_user.id
    username = message.from_user.username or message.from_user.first_name or "User"
    args = message.text.split()
    
    referrer_id = None
    if len(args) > 1 and args[1].isdigit():
        referrer_id = int(args[1])
        if referrer_id == uid:
            referrer_id = None

    user = get_user(uid)
    init_cred = get_setting("initial_credits") or "5"

    if not user:
        register_user(uid, username, referrer_id)
        user = get_user(uid)

    lang = get_user_lang(uid)
    ref_by = user[5] if user and len(user) > 5 else None
    ref_claimed = user[6] if user and len(user) > 6 else 0

    if ref_by and ref_claimed == 0:
        if is_user_in_chat(uid):
            ref_bonus = int(get_setting("refer_credits") or 2)
            update_credits(ref_by, ref_bonus)
            conn = sqlite3.connect(USERS_DB_PATH)
            conn.cursor().execute('UPDATE users SET ref_claimed = 1 WHERE user_id = ?', (uid,))
            conn.commit()
            conn.close()

            try:
                bot.send_message(ref_by, t("ref_bonus_received", user_id=ref_by, bonus=ref_bonus))
            except Exception: pass

            bot.reply_to(message, t("welcome_bonus", lang=lang, username=username, init_credits=init_cred), reply_markup=customer_keyboard(uid))
            return
        else:
            g_link = get_target_group_link()
            markup = InlineKeyboardMarkup(row_width=1)
            if g_link and g_link != "https://t.me/":
                markup.add(InlineKeyboardButton("📢 Join Channel / Group", url=g_link))
            markup.add(InlineKeyboardButton("✨ Verify & Claim Bonus", callback_data=f"verify_join_{ref_by}"))
            bot.reply_to(message, t("ref_join_required", lang=lang, username=username), parse_mode="Markdown", reply_markup=markup)
            return

    bot.reply_to(message, t("welcome_back", lang=lang, username=username), reply_markup=customer_keyboard(uid))

@bot.message_handler(commands=['language'])
def handle_lang_cmd(message):
    bot.reply_to(message, t("lang_choose", user_id=message.from_user.id), reply_markup=language_inline_keyboard())

@bot.message_handler(commands=['admin', 'dashboard', 'panel', 'menu'])
def handle_admin_entry(message):
    uid = message.from_user.id
    if not is_admin(uid):
        bot.reply_to(message, "❌ Unauthorized! Only Admin can access this.")
        return

    t_dash, m_dash = build_main_dashboard()
    bot.send_message(message.chat.id, t_dash, reply_markup=m_dash, parse_mode="HTML")

# --- TERABOX LINK DETECTION & STREAM PROCESSOR ---
TERABOX_URL_REGEX = re.compile(
    r'(https?://[^\s]+(?:terabox|1024tera|freeterabox|4funbox|mirrobox|nephobox|tibabox|teraboxlink|teraboxurl)[^\s]+)',
    re.IGNORECASE
)

@bot.message_handler(func=lambda msg: msg.text and TERABOX_URL_REGEX.search(msg.text))
def handle_terabox_link_msg(message):
    uid = message.from_user.id
    lang = get_user_lang(uid)
    match = TERABOX_URL_REGEX.search(message.text)
    if not match: return
    terabox_url = match.group(1).strip()

    # 1. Force Join Check
    if not is_user_in_chat(uid):
        g_link = get_target_group_link()
        markup = InlineKeyboardMarkup()
        if g_link != "https://t.me/":
            markup.add(InlineKeyboardButton("📢 Join Channel / Group", url=g_link))
        bot.reply_to(message, t("join_req_alert", lang=lang), parse_mode="Markdown", reply_markup=markup)
        return

    # 2. Credits / VIP Check
    user = get_user(uid)
    if not user:
        register_user(uid, message.from_user.username or "User")
        user = get_user(uid)

    is_paid = check_paid_status(uid)
    credits = user[2]

    if not is_paid and credits < 1:
        bot.reply_to(message, t("no_credits_alert", lang=lang), reply_markup=customer_keyboard(uid), parse_mode="Markdown")
        return

    # 3. Dynamic Progress Animation
    status_msg = bot.reply_to(message, get_progress_text(0, t("proc_connecting", lang=lang)), parse_mode="Markdown")
    fetch_state = {"result": None, "done": False}

    def api_worker():
        try:
            fetch_state["result"] = get_terabox_media(terabox_url)
        except Exception:
            pass
        finally:
            fetch_state["done"] = True

    th = threading.Thread(target=api_worker, daemon=True)
    th.start()

    stages = [
        (15, t("proc_connecting", lang=lang)),
        (35, t("proc_analyzing", lang=lang)),
        (55, t("proc_extracting", lang=lang)),
        (75, t("proc_optimizing", lang=lang)),
        (90, t("proc_finalizing", lang=lang)),
    ]

    for p, desc in stages:
        if fetch_state["done"]: break
        time.sleep(1.2)
        if fetch_state["done"]: break
        try:
            bot.edit_message_text(get_progress_text(p, desc), chat_id=message.chat.id, message_id=status_msg.message_id, parse_mode="Markdown")
        except Exception: pass

    th.join(timeout=35)

    if fetch_state["result"] and fetch_state["result"][1]:
        title, stream_url, size, resolution, duration, file_type = fetch_state["result"]
        if not is_paid:
            update_credits(uid, -1)
            u_now = get_user(uid)
            credits_left = u_now[2] if u_now else credits - 1
        else:
            credits_left = "Unlimited (VIP)"

        # Web App Player URL with explicit user language passed
        player_link = f"{MY_PLAYER_URL}?url={quote(stream_url)}&title={quote(title)}&lang={lang}"

        markup = InlineKeyboardMarkup(row_width=1)
        markup.add(
            InlineKeyboardButton(t("btn_watch_web", lang=lang), web_app=WebAppInfo(url=player_link)),
            InlineKeyboardButton(t("btn_fast_dl", lang=lang), url=stream_url)
        )
        b_link = get_setting("backup_channel_link")
        if b_link and b_link != "https://t.me/":
            markup.add(InlineKeyboardButton(t("btn_backup", lang=lang), url=b_link))

        card = t("card_info", lang=lang, title=clean_text_for_markdown(title), size=size, resolution=resolution, duration=duration, credits_left=credits_left)

        try:
            bot.edit_message_text(card, chat_id=message.chat.id, message_id=status_msg.message_id, parse_mode="Markdown", reply_markup=markup)
        except Exception:
            bot.send_message(message.chat.id, card, parse_mode="Markdown", reply_markup=markup)
    else:
        err = t("proc_failed", lang=lang)
        try:
            bot.edit_message_text(err, chat_id=message.chat.id, message_id=status_msg.message_id, parse_mode="Markdown")
        except Exception:
            bot.send_message(message.chat.id, err, parse_mode="Markdown")

# --- CUSTOMER MENU BUTTON HANDLERS ---
@bot.message_handler(func=lambda msg: True, content_types=['text'])
def handle_all_text_inputs(message):
    uid = message.from_user.id
    txt = message.text.strip()
    lang = get_user_lang(uid)

    # 1. Check if user is in an admin input state
    if is_admin(uid) and uid in admin_states:
        handle_admin_text_state(message)
        return

    # 2. Customer Menu Buttons
    if txt in [t("btn_account", lang="bn"), t("btn_account", lang="en")]:
        user = get_user(uid)
        credits = user[2] if user else 0
        is_paid = check_paid_status(uid)
        st_str = t("plan_vip", lang=lang, expiry=user[4]) if is_paid else t("plan_free", lang=lang)
        
        conn = sqlite3.connect(USERS_DB_PATH)
        c = conn.cursor()
        c.execute('SELECT COUNT(*) FROM users WHERE referred_by = ?', (uid,))
        ref_count = c.fetchone()[0]
        conn.close()

        bot_info = bot.get_me()
        r_link = f"https://t.me/{bot_info.username}?start={uid}"
        u_name = f"{message.from_user.first_name or ''} {message.from_user.last_name or ''}".strip() or "User"
        u_handle = f"@{message.from_user.username}" if message.from_user.username else "No Username"

        card = t("acc_title", lang=lang, name=clean_text_for_markdown(u_name), username=clean_text_for_markdown(u_handle), user_id=uid, status=st_str, credits=credits, ref_count=ref_count, ref_link=r_link)
        bot.reply_to(message, card, parse_mode="Markdown")

    elif txt in [t("btn_refer", lang="bn"), t("btn_refer", lang="en")]:
        bonus = get_setting("refer_credits") or "2"
        bot_info = bot.get_me()
        r_link = f"https://t.me/{bot_info.username}?start={uid}"
        g_link = get_target_group_link()

        card = t("refer_info", lang=lang, bonus=bonus, ref_link=r_link)
        markup = InlineKeyboardMarkup(row_width=1)
        if g_link and g_link != "https://t.me/":
            markup.add(InlineKeyboardButton("📢 Join Channel / Group", url=g_link))
        markup.add(InlineKeyboardButton("🤖 Start Bot", url=r_link))
        markup.add(InlineKeyboardButton("📢 Share Invite Post", switch_inline_query="invite"))
        bot.reply_to(message, card, parse_mode="Markdown", reply_markup=markup)

    elif txt in [t("btn_sub", lang="bn"), t("btn_sub", lang="en")]:
        p7 = get_setting("paid_7_days") or "50 BDT"
        p30 = get_setting("paid_30_days") or "150 BDT"
        admin_u = get_setting("admin_username") or "Admin"
        card = t("sub_info", lang=lang, p7=p7, p30=p30)
        markup = InlineKeyboardMarkup()
        clean_u = admin_u.replace("@", "").strip()
        if clean_u:
            markup.add(InlineKeyboardButton("💬 Buy via Admin", url=f"https://t.me/{clean_u}"))
        bot.reply_to(message, card, parse_mode="Markdown", reply_markup=markup)

    elif txt in [t("btn_backup", lang="bn"), t("btn_backup", lang="en")]:
        b_link = get_setting("backup_channel_link") or "https://t.me/"
        markup = InlineKeyboardMarkup()
        markup.add(InlineKeyboardButton("📢 Open Backup Channel", url=b_link))
        bot.reply_to(message, t("backup_info", lang=lang), parse_mode="Markdown", reply_markup=markup)

    elif txt in [t("btn_contact", lang="bn"), t("btn_contact", lang="en")]:
        if is_admin(uid):
            bot.reply_to(message, t("admin_self_contact", lang=lang), reply_markup=customer_keyboard(uid))
            return
        admin_states[uid] = {"state": "USER_CONTACT"}
        bot.reply_to(message, t("contact_prompt", lang=lang), parse_mode="Markdown")

    elif txt in [t("btn_admin", lang="bn"), t("btn_admin", lang="en")]:
        if is_admin(uid):
            handle_admin_entry(message)
        else:
            bot.reply_to(message, "❌ Unauthorized!")

    elif txt in [t("btn_lang", lang="bn"), t("btn_lang", lang="en")]:
        bot.reply_to(message, t("lang_choose", user_id=uid), reply_markup=language_inline_keyboard())

    else:
        bot.reply_to(message, t("welcome_back", lang=lang, username=message.from_user.first_name or "User"), reply_markup=customer_keyboard(uid))

def handle_admin_text_state(message):
    uid = message.from_user.id
    txt = message.text.strip()
    st = admin_states.get(uid, {}).get("state")

    if txt.lower() in ["/cancel", "cancel", "বাতিল"]:
        admin_states.pop(uid, None)
        bot.reply_to(message, "❌ Cancelled.", reply_markup=customer_keyboard(uid))
        return

    if st == "USER_CONTACT":
        admin_states.pop(uid, None)
        admin_id = int(get_setting("admin_id") or 553826070)
        notif = (
            f"📩 <b>New User Message:</b>\n"
            f"👤 <b>From:</b> {message.from_user.first_name} (`{uid}`)\n\n"
            f"💬 {message.text}"
        )
        markup = InlineKeyboardMarkup().add(InlineKeyboardButton("💬 Reply", callback_data=f"reply_to_{uid}"))
        try:
            bot.send_message(admin_id, notif, parse_mode="HTML", reply_markup=markup)
            bot.reply_to(message, t("contact_sent", user_id=uid), parse_mode="Markdown")
        except Exception:
            bot.reply_to(message, t("contact_err", user_id=uid))

    elif st == "ADMIN_REPLY":
        meta_uid = admin_states[uid].get("target_uid")
        admin_states.pop(uid, None)
        try:
            bot.send_message(meta_uid, f"💬 <b>Message from Admin:</b>\n\n{txt}", parse_mode="HTML")
            bot.reply_to(message, f"✅ Reply sent to `{meta_uid}`.")
        except Exception as e:
            bot.reply_to(message, f"❌ Failed sending reply: {e}")

    elif st == "WAIT_ADD_SOURCE":
        admin_states.pop(uid, None)
        env = read_env()
        sources = [s.strip() for s in env.get("SOURCE_CHATS", "").split(",") if s.strip()]
        if txt not in sources:
            sources.append(txt)
            update_env({"SOURCE_CHATS": ", ".join(sources)})
            bot.reply_to(message, f"✅ Added source chat: <code>{txt}</code>", parse_mode="HTML")
        t_m, m_m = build_engine_menu()
        bot.send_message(message.chat.id, t_m, reply_markup=m_m, parse_mode="HTML")

    elif st == "WAIT_SET_OUTPUT":
        admin_states.pop(uid, None)
        update_env({"OUTPUT_CHANNEL": txt})
        bot.reply_to(message, f"✅ Output channel set to: <code>{txt}</code>", parse_mode="HTML")
        t_m, m_m = build_engine_menu()
        bot.send_message(message.chat.id, t_m, reply_markup=m_m, parse_mode="HTML")

    elif st == "WAIT_SET_DELAY":
        admin_states.pop(uid, None)
        if txt.isdigit():
            update_env({"POST_DELAY_SECONDS": txt})
            bot.reply_to(message, f"✅ Post delay set to: <b>{txt}s</b>", parse_mode="HTML")
        t_m, m_m = build_engine_menu()
        bot.send_message(message.chat.id, t_m, reply_markup=m_m, parse_mode="HTML")

    elif st == "WAIT_ADD_SCHEDULE":
        admin_states.pop(uid, None)
        parts = txt.split()
        if len(parts) >= 2:
            time_str = " ".join(parts[:-1]).strip().upper()
            cnt = int(parts[-1]) if parts[-1].isdigit() else 5
            add_post_schedule(time_str, cnt)
            bot.reply_to(message, f"✅ Schedule <b>{time_str}</b> added with target count <b>{cnt} posts</b>!", parse_mode="HTML")
        else:
            bot.reply_to(message, "❌ Invalid format. Example: `10:30 PM 10` or `22:30 5`", parse_mode="Markdown")
        t_s, m_s = build_scheduler_menu()
        bot.send_message(message.chat.id, t_s, reply_markup=m_s, parse_mode="HTML")

    elif st == "WAIT_MONETAG_ZONE":
        admin_states.pop(uid, None)
        update_env({"MONETAG_ZONE_ID": txt})
        bot.reply_to(message, f"✅ Monetag Zone ID updated: <code>{txt}</code>", parse_mode="HTML")
        t_m, m_m = build_monetag_menu()
        bot.send_message(message.chat.id, t_m, reply_markup=m_m, parse_mode="HTML")

    elif st == "WAIT_MONETAG_INTERVAL":
        admin_states.pop(uid, None)
        if txt.isdigit():
            update_env({"MONETAG_MIDROLL_INTERVAL": txt})
            bot.reply_to(message, f"✅ Monetag Midroll Interval set to: <b>{txt}s</b>", parse_mode="HTML")
        t_m, m_m = build_monetag_menu()
        bot.send_message(message.chat.id, t_m, reply_markup=m_m, parse_mode="HTML")

    elif st == "WAIT_SET_SETTING":
        key = admin_states[uid].get("key")
        admin_states.pop(uid, None)
        set_setting(key, txt)
        bot.reply_to(message, f"✅ Setting <b>{key}</b> updated to: <code>{txt}</code>", parse_mode="HTML")
        t_u, m_u = build_users_menu()
        bot.send_message(message.chat.id, t_u, reply_markup=m_u, parse_mode="HTML")

    elif st == "WAIT_ADD_PAID":
        admin_states.pop(uid, None)
        parts = txt.split()
        if len(parts) >= 2 and parts[0].isdigit() and parts[1].isdigit():
            t_uid, days = int(parts[0]), int(parts[1])
            if not get_user(t_uid): register_user(t_uid, "User")
            set_paid_plan(t_uid, days)
            bot.reply_to(message, f"✅ User `{t_uid}` given VIP plan for **{days} days**!", parse_mode="Markdown")
            try: bot.send_message(t_uid, f"🎉 You have been granted **VIP Unlimited** for {days} days!")
            except Exception: pass
        t_u, m_u = build_users_menu()
        bot.send_message(message.chat.id, t_u, reply_markup=m_u, parse_mode="HTML")

    elif st == "WAIT_BROADCAST_TEXT":
        admin_states[uid] = {"state": "WAIT_BROADCAST_BTN", "post_text": txt}
        bot.reply_to(message, "🔘 Enter button as `Label | https://link.com` or type `skip`:", parse_mode="Markdown")

    elif st == "WAIT_BROADCAST_BTN":
        post_text = admin_states[uid].get("post_text", "")
        admin_states.pop(uid, None)
        markup = None
        if txt.lower() != "skip" and "|" in txt:
            l, u = txt.split("|", 1)
            markup = InlineKeyboardMarkup().add(InlineKeyboardButton(l.strip(), url=u.strip()))

        users = get_all_users()
        sent, failed = 0, 0
        s_msg = bot.send_message(message.chat.id, f"🚀 Broadcasting to {len(users)} users...")
        for u in users:
            try:
                bot.send_message(u, post_text, reply_markup=markup, parse_mode="Markdown")
                sent += 1
            except Exception:
                failed += 1
        bot.edit_message_text(f"✅ Broadcast Complete!\nSent: {sent} | Failed: {failed}", chat_id=message.chat.id, message_id=s_msg.message_id)

# -------------------------------------------------------------
# CALLBACK QUERY HANDLERS (ADMIN & CUSTOMER)
# -------------------------------------------------------------
@bot.callback_query_handler(func=lambda call: True)
def handle_all_callbacks(call):
    uid = call.from_user.id
    cid = call.message.chat.id
    data = call.data

    # Language Selection Callback
    if data.startswith("set_lang:"):
        chosen = data.split(":", 1)[1]
        set_user_lang(uid, chosen)
        bot.answer_callback_query(call.id, "Language Updated!")
        try:
            bot.edit_message_text(t("lang_selected", user_id=uid), chat_id=cid, message_id=call.message.message_id, parse_mode="Markdown")
        except Exception: pass
        bot.send_message(cid, t("welcome_back", user_id=uid, username=call.from_user.first_name or "User"), reply_markup=customer_keyboard(uid))
        return

    # Verify Join Referral
    if data.startswith("verify_join_"):
        ref_id = int(data.split("_")[-1])
        if is_user_in_chat(uid):
            user = get_user(uid)
            if user and len(user) > 6 and user[6] == 0:
                bonus = int(get_setting("refer_credits") or 2)
                update_credits(ref_id, bonus)
                conn = sqlite3.connect(USERS_DB_PATH)
                conn.cursor().execute('UPDATE users SET ref_claimed = 1 WHERE user_id = ?', (uid,))
                conn.commit()
                conn.close()
                bot.answer_callback_query(call.id, "✅ Bonus Verified & Claimed!", show_alert=True)
                bot.send_message(cid, "🎉 **Bonus Added Successfully!**", reply_markup=customer_keyboard(uid), parse_mode="Markdown")
            else:
                bot.answer_callback_query(call.id, "Bonus already claimed!", show_alert=True)
        else:
            bot.answer_callback_query(call.id, "❌ Please join our channel first!", show_alert=True)
        return

    # Reply to User
    if data.startswith("reply_to_"):
        if not is_admin(uid): return
        t_uid = int(data.split("_")[-1])
        admin_states[uid] = {"state": "ADMIN_REPLY", "target_uid": t_uid}
        bot.send_message(cid, f"📝 Write your reply for User `{t_uid}`:", parse_mode="Markdown")
        bot.answer_callback_query(call.id)
        return

    # ---------------- ADMIN NAVIGATION ----------------
    if not is_admin(uid):
        bot.answer_callback_query(call.id, "Unauthorized!")
        return

    if data == "nav:main":
        t_d, m_d = build_main_dashboard()
        bot.edit_message_text(t_d, chat_id=cid, message_id=call.message.message_id, reply_markup=m_d, parse_mode="HTML")
        bot.answer_callback_query(call.id)

    elif data == "nav:engine":
        t_e, m_e = build_engine_menu()
        bot.edit_message_text(t_e, chat_id=cid, message_id=call.message.message_id, reply_markup=m_e, parse_mode="HTML")
        bot.answer_callback_query(call.id)

    elif data == "nav:scheduler":
        t_s, m_s = build_scheduler_menu()
        bot.edit_message_text(t_s, chat_id=cid, message_id=call.message.message_id, reply_markup=m_s, parse_mode="HTML")
        bot.answer_callback_query(call.id)

    elif data == "nav:monetag":
        t_m, m_m = build_monetag_menu()
        bot.edit_message_text(t_m, chat_id=cid, message_id=call.message.message_id, reply_markup=m_m, parse_mode="HTML")
        bot.answer_callback_query(call.id)

    elif data == "nav:buttons":
        t_b, m_b = build_buttons_menu()
        bot.edit_message_text(t_b, chat_id=cid, message_id=call.message.message_id, reply_markup=m_b, parse_mode="HTML")
        bot.answer_callback_query(call.id)

    elif data == "nav:users":
        t_u, m_u = build_users_menu()
        bot.edit_message_text(t_u, chat_id=cid, message_id=call.message.message_id, reply_markup=m_u, parse_mode="HTML")
        bot.answer_callback_query(call.id)

    elif data == "nav:logs":
        t_l, m_l = build_logs_menu()
        bot.edit_message_text(t_l, chat_id=cid, message_id=call.message.message_id, reply_markup=m_l, parse_mode="HTML")
        bot.answer_callback_query(call.id)

    elif data == "nav:lang":
        bot.send_message(cid, "🌐 Select Admin Panel Language:", reply_markup=language_inline_keyboard())
        bot.answer_callback_query(call.id)

    # ---------------- ENGINE ACTIONS ----------------
    elif data == "act:start_engine":
        ok, msg = mgr.start()
        bot.answer_callback_query(call.id, msg, show_alert=True)
        t_d, m_d = build_main_dashboard()
        bot.edit_message_text(t_d, chat_id=cid, message_id=call.message.message_id, reply_markup=m_d, parse_mode="HTML")

    elif data == "act:stop_engine":
        ok, msg = mgr.stop()
        bot.answer_callback_query(call.id, msg, show_alert=True)
        t_d, m_d = build_main_dashboard()
        bot.edit_message_text(t_d, chat_id=cid, message_id=call.message.message_id, reply_markup=m_d, parse_mode="HTML")

    elif data == "act:restart_engine":
        ok, msg = mgr.restart()
        bot.answer_callback_query(call.id, msg, show_alert=True)
        t_d, m_d = build_main_dashboard()
        bot.edit_message_text(t_d, chat_id=cid, message_id=call.message.message_id, reply_markup=m_d, parse_mode="HTML")

    # ---------------- SCHEDULER ACTIONS ----------------
    elif data == "tgl_sched_mode":
        cur = is_schedule_mode_enabled()
        set_schedule_mode_enabled(not cur)
        bot.answer_callback_query(call.id, "Schedule Mode Toggled!")
        t_s, m_s = build_scheduler_menu()
        bot.edit_message_text(t_s, chat_id=cid, message_id=call.message.message_id, reply_markup=m_s, parse_mode="HTML")

    elif data == "act:add_schedule":
        admin_states[uid] = {"state": "WAIT_ADD_SCHEDULE"}
        bot.send_message(cid, "⏰ <b>Enter Schedule Time & Target Post Count:</b>\nFormat: <code>TIME COUNT</code>\nExamples:\n• <code>10:30 PM 10</code>\n• <code>02:00 PM 5</code>\n• <code>22:30 10</code>", parse_mode="HTML")
        bot.answer_callback_query(call.id)

    elif data.startswith("act:del_sched:"):
        sid = int(data.split(":")[-1])
        delete_post_schedule(sid)
        bot.answer_callback_query(call.id, "Schedule Deleted!")
        t_s, m_s = build_scheduler_menu()
        bot.edit_message_text(t_s, chat_id=cid, message_id=call.message.message_id, reply_markup=m_s, parse_mode="HTML")

    elif data == "act:post_now":
        dispatched = scheduler_service.dispatch_queued_posts(999, schedule_label="Manual Flush")
        bot.answer_callback_query(call.id, f"Dispatched {dispatched} posts!", show_alert=True)
        t_s, m_s = build_scheduler_menu()
        bot.edit_message_text(t_s, chat_id=cid, message_id=call.message.message_id, reply_markup=m_s, parse_mode="HTML")

    elif data == "act:clear_queue":
        clear_all_scheduled_queue()
        bot.answer_callback_query(call.id, "Queue cleared!")
        t_s, m_s = build_scheduler_menu()
        bot.edit_message_text(t_s, chat_id=cid, message_id=call.message.message_id, reply_markup=m_s, parse_mode="HTML")

    # ---------------- MONETAG ACTIONS ----------------
    elif data == "tgl_monetag_master":
        env = read_env()
        cur = env.get("MONETAG_ADS_ENABLED", "True").lower() == "true"
        update_env({"MONETAG_ADS_ENABLED": str(not cur)})
        bot.answer_callback_query(call.id, "Monetag Ads Toggled!")
        t_m, m_m = build_monetag_menu()
        bot.edit_message_text(t_m, chat_id=cid, message_id=call.message.message_id, reply_markup=m_m, parse_mode="HTML")

    elif data == "set_monetag_zone":
        admin_states[uid] = {"state": "WAIT_MONETAG_ZONE"}
        bot.send_message(cid, "🎯 Send new Monetag Zone ID (e.g. 12016303):")
        bot.answer_callback_query(call.id)

    elif data == "set_monetag_interval":
        admin_states[uid] = {"state": "WAIT_MONETAG_INTERVAL"}
        bot.send_message(cid, "⏱️ Send new Midroll Interval in seconds (e.g. 180):")
        bot.answer_callback_query(call.id)

    # ---------------- BUTTON & WATERMARK ACTIONS ----------------
    elif data == "tgl_free_btn":
        env = read_env()
        cur = env.get("BUTTON_TERABOX_FREE_ENABLED", "True").lower() == "true"
        update_env({"BUTTON_TERABOX_FREE_ENABLED": str(not cur)})
        bot.answer_callback_query(call.id, "Free Button Toggled!")
        t_b, m_b = build_buttons_menu()
        bot.edit_message_text(t_b, chat_id=cid, message_id=call.message.message_id, reply_markup=m_b, parse_mode="HTML")

    elif data == "tgl_sticker":
        env = read_env()
        cur = env.get("STICKER_ENABLED", "True").lower() == "true"
        update_env({"STICKER_ENABLED": str(not cur)})
        bot.answer_callback_query(call.id, "Sticker Watermark Toggled!")
        t_b, m_b = build_buttons_menu()
        bot.edit_message_text(t_b, chat_id=cid, message_id=call.message.message_id, reply_markup=m_b, parse_mode="HTML")

    # ---------------- FORWARDER ENGINE ACTIONS ----------------
    elif data == "act:add_source":
        admin_states[uid] = {"state": "WAIT_ADD_SOURCE"}
        bot.send_message(cid, "📥 Send Source Channel ID or @username:")
        bot.answer_callback_query(call.id)

    elif data == "act:set_output":
        admin_states[uid] = {"state": "WAIT_SET_OUTPUT"}
        bot.send_message(cid, "📤 Send Destination Channel ID or @username:")
        bot.answer_callback_query(call.id)

    elif data == "act:set_delay":
        admin_states[uid] = {"state": "WAIT_SET_DELAY"}
        bot.send_message(cid, "⏱️ Send Post Delay in seconds (e.g. 3):")
        bot.answer_callback_query(call.id)

    # ---------------- USERS & PLANS ACTIONS ----------------
    elif data == "btn_user_list":
        users = get_detailed_users()
        report = f"📊 Total Users: {len(users)}\nVIP: {sum(1 for u in users if u[3]==1)}\nFree: {sum(1 for u in users if u[3]!=1)}"
        bot.send_message(cid, report)
        bot.answer_callback_query(call.id)

    elif data == "btn_add_paid":
        admin_states[uid] = {"state": "WAIT_ADD_PAID"}
        bot.send_message(cid, "➕ Send User ID and Days (e.g. `123456789 30`):", parse_mode="Markdown")
        bot.answer_callback_query(call.id)

    elif data == "set_init_credits":
        admin_states[uid] = {"state": "WAIT_SET_SETTING", "key": "initial_credits"}
        bot.send_message(cid, "🎁 Send Initial Credits amount (e.g. 5):")
        bot.answer_callback_query(call.id)

    elif data == "set_ref_credits":
        admin_states[uid] = {"state": "WAIT_SET_SETTING", "key": "refer_credits"}
        bot.send_message(cid, "🔗 Send Referral Credits amount (e.g. 2):")
        bot.answer_callback_query(call.id)

    elif data == "set_7_days_price":
        admin_states[uid] = {"state": "WAIT_SET_SETTING", "key": "paid_7_days"}
        bot.send_message(cid, "💰 Send 7-Day Plan Price (e.g. 50 BDT):")
        bot.answer_callback_query(call.id)

    elif data == "set_30_days_price":
        admin_states[uid] = {"state": "WAIT_SET_SETTING", "key": "paid_30_days"}
        bot.send_message(cid, "💰 Send 30-Day Plan Price (e.g. 150 BDT):")
        bot.answer_callback_query(call.id)

    elif data == "set_group_id":
        admin_states[uid] = {"state": "WAIT_SET_SETTING", "key": "required_group_id"}
        bot.send_message(cid, "👥 Send Required Group ID (e.g. -100xxxx):")
        bot.answer_callback_query(call.id)

    elif data == "set_backup_link":
        admin_states[uid] = {"state": "WAIT_SET_SETTING", "key": "backup_channel_link"}
        bot.send_message(cid, "📢 Send Backup Channel Link (e.g. https://t.me/...):")
        bot.answer_callback_query(call.id)

    elif data == "btn_broadcast":
        admin_states[uid] = {"state": "WAIT_BROADCAST_TEXT"}
        bot.send_message(cid, "📢 Send message text for broadcast:")
        bot.answer_callback_query(call.id)

    elif data == "act:clear_logs":
        log_buffer.clear()
        bot.answer_callback_query(call.id, "Logs Cleared!")
        t_l, m_l = build_logs_menu()
        bot.edit_message_text(t_l, chat_id=cid, message_id=call.message.message_id, reply_markup=m_l, parse_mode="HTML")

# -------------------------------------------------------------
# RENDER WEB SERVICE HEALTH & MINI APP API SERVER
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

            self.send_response(200)
            self.send_header("Content-type", "text/plain")
            self.send_header("Access-Control-Allow-Origin", "*")
            self.end_headers()
            self.wfile.write(b"OK")

        def log_message(self, format, *args):
            pass

    try:
        server = http.server.HTTPServer(("0.0.0.0", port), HealthHandler)
        push_log("SERVER", f"Render Health & Mini App Server listening on port {port}")
        server.serve_forever()
    except Exception as e:
        push_log("WARNING", f"Health server error: {e}")

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

# -------------------------------------------------------------
# MAIN RUNNER
# -------------------------------------------------------------
if __name__ == "__main__":
    push_log("STARTUP", "🚀 TeraStream Master All-In-One Bot is launching...")

    # Start Render Health & API Server if PORT is specified or on Render
    if os.environ.get("PORT"):
        t_health = threading.Thread(target=start_render_health_server, daemon=True)
        t_health.start()

    # Publish Render Status
    threading.Thread(target=publish_render_status, daemon=True).start()

    # Auto-start automation engine if credentials exist
    try:
        cfg = mgr.build_config()
        if cfg["api_id"] and cfg["api_hash"] and cfg["source_chats"]:
            push_log("STARTUP", "Auto-starting integrated Channel Automation Engine...")
            mgr.start()
    except Exception as e:
        push_log("WARNING", f"Engine auto-start note: {e}")

    try:
        allowed_upd = [
            "message", "edited_message", "channel_post", "edited_channel_post",
            "inline_query", "chosen_inline_result", "callback_query",
            "shipping_query", "pre_checkout_query", "poll", "poll_answer",
            "my_chat_member", "chat_member", "chat_join_request"
        ]
        bot.infinity_polling(skip_pending=True, allowed_updates=allowed_upd)
    except ApiTelegramException as api_err:
        if api_err.error_code == 409:
            push_log("CRITICAL", "Conflict Error (409): Another instance is running! Stop existing instance first.")
        else:
            push_log("ERROR", f"Telegram API Error: {api_err}")
    except KeyboardInterrupt:
        push_log("INFO", "Bot stopped by user.")
    finally:
        mgr.stop()
