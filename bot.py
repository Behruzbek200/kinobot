#!/usr/bin/env python3
# -*- coding: utf-8 -*-

"""
MovieSearchBot - Kino qidiruv boti (TO'LIQ TUZATILGAN VERSIYA)
"""

import os
import sqlite3
import logging
import random
import string
import secrets
from datetime import datetime, timedelta
from functools import wraps
from typing import Dict, List, Tuple, Any

import telebot
from telebot.types import (
    InlineKeyboardMarkup, InlineKeyboardButton,
    CallbackQuery, Message, Update
)
from flask import Flask, request, abort

# ---------------------------- CONFIGURATION ---------------------------------
TOKEN = os.environ.get("BOT_TOKEN")
if not TOKEN:
    raise ValueError("BOT_TOKEN environment variable not set")

ADMIN_IDS = os.environ.get("ADMIN_IDS", "")
RENDER_EXTERNAL_URL = os.environ.get("RENDER_EXTERNAL_URL", "https://your-app.onrender.com")
WEBHOOK_PATH = f"/webhook/{TOKEN}"
WEBHOOK_URL = f"{RENDER_EXTERNAL_URL}{WEBHOOK_PATH}"

DB_NAME = "movies.db"

logging.basicConfig(
    format="%(asctime)s - %(name)s - %(levelname)s - %(message)s",
    level=logging.INFO
)
logger = logging.getLogger(__name__)

app = Flask(__name__)
bot = telebot.TeleBot(TOKEN, parse_mode="HTML")

# In-memory states
user_states: Dict[int, Dict[str, Any]] = {}
user_search_results: Dict[int, List[Tuple]] = {}
user_current_page: Dict[int, int] = {}

# ---------------------------------------------------------------
# Database functions
# ---------------------------------------------------------------
def init_db():
    conn = sqlite3.connect(DB_NAME)
    cur = conn.cursor()
    cur.execute("""
        CREATE TABLE IF NOT EXISTS movies (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            name TEXT NOT NULL,
            year INTEGER,
            genre TEXT,
            rating REAL,
            description TEXT,
            poster_url TEXT,
            duration TEXT,
            director TEXT,
            actors TEXT,
            code TEXT UNIQUE,
            created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
        )
    """)
    cur.execute("""
        CREATE TABLE IF NOT EXISTS users (
            user_id INTEGER PRIMARY KEY,
            username TEXT,
            first_name TEXT,
            is_admin INTEGER DEFAULT 0,
            joined_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
            last_active TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
            last_sub_check TIMESTAMP DEFAULT CURRENT_TIMESTAMP
        )
    """)
    cur.execute("""
        CREATE TABLE IF NOT EXISTS user_favorites (
            user_id INTEGER,
            movie_id INTEGER,
            added_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
            PRIMARY KEY (user_id, movie_id)
        )
    """)
    cur.execute("""
        CREATE TABLE IF NOT EXISTS required_channels (
            channel_username TEXT PRIMARY KEY
        )
    """)
    cur.execute("""
        CREATE TABLE IF NOT EXISTS search_stats (
            search_term TEXT PRIMARY KEY,
            count INTEGER DEFAULT 1
        )
    """)
    cur.execute("""
        CREATE TABLE IF NOT EXISTS bot_config (
            key TEXT PRIMARY KEY,
            value TEXT
        )
    """)
    # Migrate old tables - add code column if not exists
    try:
        cur.execute("ALTER TABLE movies ADD COLUMN code TEXT UNIQUE")
    except sqlite3.OperationalError:
        pass
    conn.commit()
    conn.close()
    logger.info("Database initialized.")

def execute_query(query: str, params: tuple = (), fetch_one=False, fetch_all=False):
    conn = sqlite3.connect(DB_NAME)
    cur = conn.cursor()
    cur.execute(query, params)
    result = None
    if fetch_one:
        result = cur.fetchone()
    elif fetch_all:
        result = cur.fetchall()
    conn.commit()
    conn.close()
    return result

def get_config(key: str) -> str:
    res = execute_query("SELECT value FROM bot_config WHERE key = ?", (key,), fetch_one=True)
    return res[0] if res else None

def set_config(key: str, value: str):
    execute_query("INSERT OR REPLACE INTO bot_config (key, value) VALUES (?, ?)", (key, value))

def generate_movie_code() -> str:
    while True:
        code = "MOV_" + ''.join(secrets.choice(string.ascii_uppercase + string.digits) for _ in range(6))
        existing = execute_query("SELECT id FROM movies WHERE code = ?", (code,), fetch_one=True)
        if not existing:
            return code

def register_user(user_id: int, username: str, first_name: str):
    execute_query("""
        INSERT INTO users (user_id, username, first_name, last_active)
        VALUES (?, ?, ?, CURRENT_TIMESTAMP)
        ON CONFLICT(user_id) DO UPDATE SET
            username = excluded.username,
            first_name = excluded.first_name,
            last_active = CURRENT_TIMESTAMP
    """, (user_id, username, first_name))

def is_admin(user_id: int) -> bool:
    res = execute_query("SELECT is_admin FROM users WHERE user_id = ?", (user_id,), fetch_one=True)
    return res is not None and res[0] == 1

def get_all_admins() -> List[int]:
    rows = execute_query("SELECT user_id FROM users WHERE is_admin = 1", fetch_all=True)
    return [row[0] for row in rows] if rows else []

def add_admin(user_id: int) -> bool:
    try:
        execute_query("UPDATE users SET is_admin = 1 WHERE user_id = ?", (user_id,))
        return True
    except Exception:
        return False

def remove_admin(user_id: int) -> bool:
    try:
        execute_query("UPDATE users SET is_admin = 0 WHERE user_id = ?", (user_id,))
        return True
    except Exception:
        return False

def get_required_channels() -> List[str]:
    rows = execute_query("SELECT channel_username FROM required_channels", fetch_all=True)
    return [row[0] for row in rows] if rows else []

def add_required_channel(channel: str):
    execute_query("INSERT OR IGNORE INTO required_channels (channel_username) VALUES (?)", (channel.strip('@'),))

def remove_required_channel(channel: str):
    execute_query("DELETE FROM required_channels WHERE channel_username = ?", (channel.strip('@'),))

def check_user_subscription(user_id: int) -> Tuple[bool, List[str]]:
    channels = get_required_channels()
    if not channels:
        return True, []
    not_subscribed = []
    for ch in channels:
        try:
            chat_member = bot.get_chat_member(f"@{ch}", user_id)
            if chat_member.status in ["left", "kicked"]:
                not_subscribed.append(ch)
        except Exception as e:
            logger.error(f"Error checking subscription for channel {ch}: {e}")
            not_subscribed.append(ch)
    return len(not_subscribed) == 0, not_subscribed

def update_last_sub_check(user_id: int):
    execute_query("UPDATE users SET last_sub_check = CURRENT_TIMESTAMP WHERE user_id = ?", (user_id,))

def ensure_subscription(user_id: int, chat_id: int) -> bool:
    row = execute_query("SELECT last_sub_check FROM users WHERE user_id = ?", (user_id,), fetch_one=True)
    need_check = True
    if row:
        last_check = datetime.fromisoformat(row[0])
        if datetime.now() - last_check < timedelta(hours=24):
            need_check = False
    if need_check:
        ok, not_subbed = check_user_subscription(user_id)
        update_last_sub_check(user_id)
        if not ok:
            markup = InlineKeyboardMarkup()
            for ch in not_subbed:
                markup.add(InlineKeyboardButton(f"📢 {ch}", url=f"https://t.me/{ch}"))
            markup.add(InlineKeyboardButton("✅ Tekshirish", callback_data="check_subscription"))
            bot.send_message(
                chat_id,
                "❌ Botdan foydalanish uchun quyidagi kanallarga a'zo bo'ling:\n" +
                "\n".join(f"@{ch}" for ch in not_subbed),
                reply_markup=markup
            )
            return False
    return True

def subscription_required(handler):
    @wraps(handler)
    def wrapper(message: Message):
        if ensure_subscription(message.from_user.id, message.chat.id):
            return handler(message)
    return wrapper

# ---------------------------- Movie CRUD ---------------------------------
def add_movie(name, year, genre, rating, description, poster_url, duration, director, actors):
    code = generate_movie_code()
    logger.info(f"Adding movie: {name}, code: {code}")
    execute_query("""
        INSERT INTO movies (name, year, genre, rating, description, poster_url, duration, director, actors, code)
        VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
    """, (name, year, genre, rating, description, poster_url, duration, director, actors, code))
    movie_id = execute_query("SELECT last_insert_rowid()", fetch_one=True)[0]
    return movie_id, code

def update_movie(movie_id, field, value):
    allowed = ["name", "year", "genre", "rating", "description", "poster_url", "duration", "director", "actors"]
    if field not in allowed:
        return False
    execute_query(f"UPDATE movies SET {field} = ? WHERE id = ?", (value, movie_id))
    return True

def delete_movie(movie_id):
    execute_query("DELETE FROM movies WHERE id = ?", (movie_id,))
    execute_query("DELETE FROM user_favorites WHERE movie_id = ?", (movie_id,))

def get_movie_by_id(movie_id):
    return execute_query("SELECT * FROM movies WHERE id = ?", (movie_id,), fetch_one=True)

def get_movie_by_code(code: str):
    return execute_query("SELECT * FROM movies WHERE code = ?", (code,), fetch_one=True)

def search_movies(query_term: str):
    term = f"%{query_term}%"
    rows = execute_query("SELECT * FROM movies WHERE name LIKE ? OR code LIKE ? ORDER BY name", (term, term), fetch_all=True)
    return rows if rows else []

def get_movies_by_filter(genre=None, year=None, rating=None):
    sql = "SELECT * FROM movies WHERE 1=1"
    params = []
    if genre:
        sql += " AND genre LIKE ?"
        params.append(f"%{genre}%")
    if year:
        sql += " AND year = ?"
        params.append(year)
    if rating:
        sql += " AND rating >= ?"
        params.append(rating)
    sql += " ORDER BY rating DESC"
    return execute_query(sql, tuple(params), fetch_all=True) or []

def get_random_movie():
    return execute_query("SELECT * FROM movies ORDER BY RANDOM() LIMIT 1", fetch_one=True)

def get_movie_count() -> int:
    res = execute_query("SELECT COUNT(*) FROM movies", fetch_one=True)
    return res[0] if res else 0

def get_user_count() -> int:
    res = execute_query("SELECT COUNT(*) FROM users", fetch_one=True)
    return res[0] if res else 0

def get_most_searched_term():
    res = execute_query("SELECT search_term, count FROM search_stats ORDER BY count DESC LIMIT 1", fetch_one=True)
    return res[0] if res else "Ma'lumot yo'q"

def increment_search_term(term: str):
    execute_query("INSERT INTO search_stats (search_term, count) VALUES (?, 1) ON CONFLICT(search_term) DO UPDATE SET count = count + 1", (term,))

def get_recent_movies_count(days=7):
    res = execute_query("SELECT COUNT(*) FROM movies WHERE created_at >= datetime('now', ?)", (f"-{days} days",), fetch_one=True)
    return res[0] if res else 0

def get_favorites(user_id: int):
    rows = execute_query("""
        SELECT m.* FROM movies m
        JOIN user_favorites f ON m.id = f.movie_id
        WHERE f.user_id = ?
        ORDER BY f.added_at DESC
    """, (user_id,), fetch_all=True)
    return rows if rows else []

def add_favorite(user_id: int, movie_id: int):
    execute_query("INSERT OR IGNORE INTO user_favorites (user_id, movie_id) VALUES (?, ?)", (user_id, movie_id))

def remove_favorite(user_id: int, movie_id: int):
    execute_query("DELETE FROM user_favorites WHERE user_id = ? AND movie_id = ?", (user_id, movie_id))

def format_movie_info(movie) -> str:
    if not movie:
        return "Kino ma'lumotlari topilmadi."
    name = movie[1] if len(movie) > 1 else "Noma'lum"
    year = movie[2] if len(movie) > 2 else "?"
    rating = movie[4] if len(movie) > 4 else "0"
    genre = movie[3] if len(movie) > 3 else "Noma'lum"
    duration = movie[7] if len(movie) > 7 else "?"
    director = movie[8] if len(movie) > 8 else "Noma'lum"
    actors = movie[9] if len(movie) > 9 else "Noma'lum"
    desc = movie[5] if len(movie) > 5 else "Tavsif yo'q"
    code = movie[10] if len(movie) > 10 else "Kod yo'q"
    text = f"🎬 <b>{name}</b> ({year})\n"
    text += f"⭐ Reyting: {rating}/10\n"
    text += f"🎭 Janr: {genre}\n"
    text += f"⏱ Davomiyligi: {duration}\n"
    text += f"🎥 Rejissyor: {director}\n"
    text += f"🌟 Aktyorlar: {actors}\n"
    text += f"🔑 Kino kodi: <code>{code}</code>\n"
    text += f"📖 Tavsif:\n{desc}\n"
    return text

def send_movie_info(chat_id, movie, show_fav_button=True, user_id=None):
    if not movie:
        bot.send_message(chat_id, "❌ Kino topilmadi!")
        return
    movie_id = movie[0]
    caption = format_movie_info(movie)
    markup = None
    if show_fav_button and user_id:
        fav_check = execute_query("SELECT 1 FROM user_favorites WHERE user_id = ? AND movie_id = ?", (user_id, movie_id), fetch_one=True)
        fav_text = "❤️ Sevimlilarga qo'shish" if not fav_check else "❌ Sevimlilardan o'chirish"
        fav_cb = f"fav_{movie_id}" if not fav_check else f"unfav_{movie_id}"
        markup = InlineKeyboardMarkup()
        markup.add(InlineKeyboardButton(fav_text, callback_data=fav_cb))
        markup.add(InlineKeyboardButton("🔙 Orqaga", callback_data="back_to_list"))
    poster_url = movie[6] if len(movie) > 6 else None
    if poster_url and poster_url.startswith("http"):
        try:
            bot.send_photo(chat_id, poster_url, caption=caption, reply_markup=markup, parse_mode="HTML")
        except Exception as e:
            logger.error(f"Failed to send photo: {e}")
            bot.send_message(chat_id, caption, reply_markup=markup, parse_mode="HTML")
    else:
        bot.send_message(chat_id, caption, reply_markup=markup, parse_mode="HTML")

def build_search_keyboard(user_id, page=0, per_page=5):
    results = user_search_results.get(user_id, [])
    if not results:
        return InlineKeyboardMarkup().add(InlineKeyboardButton("🏠 Asosiy menyu", callback_data="main_menu"))
    total = len(results)
    pages = (total + per_page - 1) // per_page
    start = page * per_page
    end = start + per_page
    movies_page = results[start:end]
    markup = InlineKeyboardMarkup(row_width=1)
    for m in movies_page:
        movie_id = m[0]
        movie_name = m[1]
        markup.add(InlineKeyboardButton(f"🎬 {movie_name}", callback_data=f"movie_{movie_id}"))
    nav_buttons = []
    if page > 0:
        nav_buttons.append(InlineKeyboardButton("⬅️ Oldingi", callback_data=f"page_{page-1}"))
    if page + 1 < pages:
        nav_buttons.append(InlineKeyboardButton("Keyingi ➡️", callback_data=f"page_{page+1}"))
    if nav_buttons:
        markup.row(*nav_buttons)
    markup.add(InlineKeyboardButton("🏠 Asosiy menyu", callback_data="main_menu"))
    return markup

def post_movie_to_channel(movie):
    channel_id = get_config("output_channel")
    if not channel_id:
        logger.warning("Output channel not configured")
        return
    try:
        channel_id = int(channel_id)
    except ValueError:
        pass
    movie_id = movie[0]
    movie_name = movie[1]
    caption = format_movie_info(movie)
    markup = InlineKeyboardMarkup()
    markup.add(InlineKeyboardButton("✏️ Kinoni tahrirlash", callback_data=f"edit_movie_{movie_id}"))
    poster_url = movie[6] if len(movie) > 6 else None
    try:
        if poster_url and poster_url.startswith("http"):
            bot.send_photo(channel_id, poster_url, caption=caption, reply_markup=markup, parse_mode="HTML")
        else:
            bot.send_message(channel_id, caption, reply_markup=markup, parse_mode="HTML")
        logger.info(f"Movie '{movie_name}' posted to channel {channel_id}")
    except Exception as e:
        logger.error(f"Failed to post to channel: {e}")

# ---------------------------- Handlers ---------------------------------
def show_main_menu(chat_id):
    markup = InlineKeyboardMarkup(row_width=2)
    markup.add(
        InlineKeyboardButton("🔍 Kino qidirish", callback_data="search"),
        InlineKeyboardButton("🎲 Tavsiya", callback_data="recommend"),
        InlineKeyboardButton("📊 Statistika", callback_data="stats_user"),
        InlineKeyboardButton("❤️ Sevimlilar", callback_data="favorites"),
        InlineKeyboardButton("🎭 Filtr", callback_data="filter_menu"),
    )
    if is_admin(chat_id):
        markup.add(InlineKeyboardButton("⚙️ Admin panel", callback_data="admin_panel"))
    bot.send_message(chat_id, "🏠 <b>Asosiy menyu</b>", reply_markup=markup, parse_mode="HTML")

def show_admin_panel(chat_id):
    markup = InlineKeyboardMarkup(row_width=2)
    markup.add(
        InlineKeyboardButton("➕ Kino qo'shish", callback_data="admin_add_movie"),
        InlineKeyboardButton("✏️ Kino tahrirlash", callback_data="admin_edit_movie"),
        InlineKeyboardButton("🗑 Kino o'chirish", callback_data="admin_del_movie"),
        InlineKeyboardButton("👥 Admin qo'shish", callback_data="admin_add_admin"),
        InlineKeyboardButton("❌ Admin o'chirish", callback_data="admin_remove_admin"),
        InlineKeyboardButton("📊 Statistika", callback_data="admin_stats"),
        InlineKeyboardButton("📢 Majburiy kanallar", callback_data="admin_channels"),
        InlineKeyboardButton("📤 Chiqish kanali", callback_data="admin_output_channel"),
        InlineKeyboardButton("📢 Reklama", callback_data="admin_broadcast"),
        InlineKeyboardButton("🔙 Chiqish", callback_data="main_menu"),
    )
    bot.send_message(chat_id, "⚙️ <b>Admin panel</b>", reply_markup=markup, parse_mode="HTML")

@bot.message_handler(commands=['start'])
def start_command(message: Message):
    user = message.from_user
    register_user(user.id, user.username, user.first_name)
    if ensure_subscription(user.id, message.chat.id):
        show_main_menu(message.chat.id)

@bot.message_handler(commands=['admin'])
def admin_command(message: Message):
    if not is_admin(message.from_user.id):
        bot.reply_to(message, "❌ Siz admin emassiz!")
        return
    show_admin_panel(message.chat.id)

@bot.message_handler(func=lambda message: True, content_types=['text'])
@subscription_required
def handle_text(message: Message):
    text = message.text.strip()
    if not text:
        return
    
    # Check if it's a movie code
    if text.startswith("MOV_"):
        movie = get_movie_by_code(text)
        if movie:
            send_movie_info(message.chat.id, movie, show_fav_button=True, user_id=message.from_user.id)
            increment_search_term(text)
            return
    
    # Normal search
    increment_search_term(text)
    results = search_movies(text)
    if not results:
        bot.reply_to(message, "❌ Hech qanday kino topilmadi.\n\n💡 Maslahat: Kino kodini yoki nomini yuboring.")
        return
    
    user_id = message.from_user.id
    user_search_results[user_id] = results
    user_current_page[user_id] = 0
    markup = build_search_keyboard(user_id, 0)
    bot.send_message(message.chat.id, f"🔍 <b>\"{text}\" bo'yicha {len(results)} ta natija:</b>", reply_markup=markup, parse_mode="HTML")

@bot.callback_query_handler(func=lambda call: True)
def handle_callback(call: CallbackQuery):
    user_id = call.from_user.id
    chat_id = call.message.chat.id
    data = call.data

    if not ensure_subscription(user_id, chat_id):
        return

    # Subscription check
    if data == "check_subscription":
        ok, _ = check_user_subscription(user_id)
        update_last_sub_check(user_id)
        if ok:
            bot.edit_message_text("✅ A'zolik tasdiqlandi!", chat_id, call.message.message_id)
            show_main_menu(chat_id)
        else:
            bot.answer_callback_query(call.id, "Hali ham a'zo emassiz!", show_alert=True)
        return

    elif data == "main_menu":
        show_main_menu(chat_id)
        return

    elif data == "search":
        bot.send_message(chat_id, "🔍 Kino nomini yoki kodini yuboring (masalan: MOV_ABC123):")
        return

    elif data == "recommend":
        movie = get_random_movie()
        if movie:
            send_movie_info(chat_id, movie, show_fav_button=True, user_id=user_id)
        else:
            bot.send_message(chat_id, "❌ Hozircha hech qanday kino yo'q.\n\nAdmin panelga o'tib kino qo'shing.")
        return

    elif data == "stats_user":
        total_movies = get_movie_count()
        total_users = get_user_count()
        most_searched = get_most_searched_term()
        recent_7 = get_recent_movies_count(7)
        text = f"📊 <b>Bot statistikasi</b>\n\n🎬 Jami kinolar: {total_movies}\n👥 Jami foydalanuvchilar: {total_users}\n🔎 Eng ko'p qidirilgan: {most_searched}\n🆕 Oxirgi 7 kunda qo'shilgan kinolar: {recent_7}"
        bot.send_message(chat_id, text, parse_mode="HTML")
        return

    elif data == "favorites":
        favs = get_favorites(user_id)
        if not favs:
            bot.send_message(chat_id, "❤️ Sevimlilar ro'yxati bo'sh.\n\nKinoni ko'rib, ❤️ tugmasini bosing.")
            return
        user_search_results[user_id] = favs
        user_current_page[user_id] = 0
        markup = build_search_keyboard(user_id, 0)
        bot.send_message(chat_id, "⭐ <b>Sevimli kinolar</b>", reply_markup=markup, parse_mode="HTML")
        return

    elif data == "filter_menu":
        markup = InlineKeyboardMarkup(row_width=2)
        markup.add(
            InlineKeyboardButton("🎭 Janr", callback_data="filter_genre"),
            InlineKeyboardButton("📅 Yil", callback_data="filter_year"),
            InlineKeyboardButton("⭐ Reyting", callback_data="filter_rating"),
            InlineKeyboardButton("🔙 Orqaga", callback_data="main_menu"),
        )
        bot.edit_message_text("🎛 <b>Qidiruv filtri</b>", chat_id, call.message.message_id, reply_markup=markup, parse_mode="HTML")
        return

    elif data.startswith("filter_"):
        filter_type = data.split("_")[1]
        if filter_type == "genre":
            msg = bot.send_message(chat_id, "🎭 Janr nomini yozing (masalan: Drama, Komediya):")
            bot.register_next_step_handler(msg, process_filter_genre, user_id)
        elif filter_type == "year":
            msg = bot.send_message(chat_id, "📅 Yilni raqamda yozing (masalan: 2020):")
            bot.register_next_step_handler(msg, process_filter_year, user_id)
        elif filter_type == "rating":
            msg = bot.send_message(chat_id, "⭐ Minimal reytingni yozing (0-10 oralig'ida):")
            bot.register_next_step_handler(msg, process_filter_rating, user_id)
        return

    elif data.startswith("movie_"):
        movie_id = int(data.split("_")[1])
        movie = get_movie_by_id(movie_id)
        if movie:
            send_movie_info(chat_id, movie, show_fav_button=True, user_id=user_id)
        else:
            bot.answer_callback_query(call.id, "Kino topilmadi!")
        return

    elif data.startswith("fav_"):
        movie_id = int(data.split("_")[1])
        add_favorite(user_id, movie_id)
        bot.answer_callback_query(call.id, "✅ Sevimlilarga qo'shildi!")
        movie = get_movie_by_id(movie_id)
        if movie:
            send_movie_info(chat_id, movie, show_fav_button=True, user_id=user_id)
        return

    elif data.startswith("unfav_"):
        movie_id = int(data.split("_")[1])
        remove_favorite(user_id, movie_id)
        bot.answer_callback_query(call.id, "❌ Sevimlilardan o'chirildi!")
        movie = get_movie_by_id(movie_id)
        if movie:
            send_movie_info(chat_id, movie, show_fav_button=True, user_id=user_id)
        return

    elif data.startswith("page_"):
        page = int(data.split("_")[1])
        user_current_page[user_id] = page
        markup = build_search_keyboard(user_id, page)
        bot.edit_message_reply_markup(chat_id, call.message.message_id, reply_markup=markup)
        return

    elif data == "admin_panel":
        if not is_admin(user_id):
            bot.answer_callback_query(call.id, "Ruxsat yo'q!")
            return
        show_admin_panel(chat_id)
        return

    elif data == "admin_add_movie":
        if not is_admin(user_id):
            return
        user_states[user_id] = {"action": "add_movie", "step": "name"}
        bot.send_message(chat_id, "➕ <b>Yangi kino qo'shish</b>\n\nKino nomini yuboring:", parse_mode="HTML")
        return

    elif data == "admin_edit_movie":
        if not is_admin(user_id):
            return
        bot.send_message(chat_id, "✏️ Tahrirlash uchun kino ID sini yuboring:\n\n(ID ni bilish uchun /start -> Kino qidirish -> kinoni toping va ID ga qarang)")
        bot.register_next_step_handler(call.message, admin_edit_movie_select)
        return

    elif data == "admin_del_movie":
        if not is_admin(user_id):
            return
        bot.send_message(chat_id, "🗑 O'chirish uchun kino ID sini yuboring:")
        bot.register_next_step_handler(call.message, admin_delete_movie)
        return

    elif data == "admin_add_admin":
        if not is_admin(user_id):
            return
        bot.send_message(chat_id, "👥 Admin qo'shish uchun foydalanuvchi ID sini yuboring:\n\n(ID ni bilish uchun @userinfobot dan foydalaning)")
        bot.register_next_step_handler(call.message, admin_add_admin_by_id)
        return

    elif data == "admin_remove_admin":
        if not is_admin(user_id):
            return
        admins = get_all_admins()
        if not admins:
            bot.send_message(chat_id, "Hech qanday admin yo'q.")
            return
        markup = InlineKeyboardMarkup()
        for aid in admins:
            if aid != user_id:
                user_info = execute_query("SELECT first_name FROM users WHERE user_id = ?", (aid,), fetch_one=True)
                name = user_info[0] if user_info else str(aid)
                markup.add(InlineKeyboardButton(f"❌ {name}", callback_data=f"remove_admin_{aid}"))
        markup.add(InlineKeyboardButton("🔙 Orqaga", callback_data="admin_panel"))
        bot.edit_message_text("Admin o'chirish: kimni olib tashlamoqchisiz?", chat_id, call.message.message_id, reply_markup=markup)
        return

    elif data.startswith("remove_admin_"):
        target_id = int(data.split("_")[2])
        if target_id == user_id:
            bot.answer_callback_query(call.id, "O'zingizni o'chira olmaysiz!")
            return
        remove_admin(target_id)
        bot.answer_callback_query(call.id, "Admin o'chirildi!")
        show_admin_panel(chat_id)
        return

    elif data == "admin_stats":
        if not is_admin(user_id):
            return
        total_users = get_user_count()
        total_movies = get_movie_count()
        daily = execute_query("SELECT COUNT(*) FROM users WHERE last_active >= datetime('now', '-1 day')", fetch_one=True)[0]
        weekly = execute_query("SELECT COUNT(*) FROM users WHERE last_active >= datetime('now', '-7 days')", fetch_one=True)[0]
        monthly = execute_query("SELECT COUNT(*) FROM users WHERE last_active >= datetime('now', '-30 days')", fetch_one=True)[0]
        active_users = execute_query("SELECT user_id, first_name, last_active FROM users WHERE last_active >= datetime('now', '-7 days') ORDER BY last_active DESC LIMIT 5", fetch_all=True)
        active_text = ""
        for i, u in enumerate(active_users, 1):
            name = u[1] or str(u[0])
            active_text += f"{i}. {name}\n"
        top_searched = execute_query("SELECT search_term, count FROM search_stats ORDER BY count DESC LIMIT 5", fetch_all=True)
        top_text = "\n".join(f"{t[0]}: {t[1]} marta" for t in top_searched) if top_searched else "Ma'lumot yo'q"
        text = f"📊 <b>Batafsil statistika</b>\n\n👥 Jami foydalanuvchilar: {total_users}\n🎬 Kinolar: {total_movies}\n📅 Kunlik faol: {daily}\n📆 Haftalik faol: {weekly}\n📆 Oylik faol: {monthly}\n\n🏆 Eng faol foydalanuvchilar (oxirgi 7 kun):\n{active_text}\n\n🔍 Eng ko'p qidirilgan so'zlar:\n{top_text}"
        bot.send_message(chat_id, text, parse_mode="HTML")
        return

    elif data == "admin_channels":
        if not is_admin(user_id):
            return
        channels = get_required_channels()
        ch_list = "\n".join(f"📢 @{ch}" for ch in channels) if channels else "❌ Hech qanday kanal yo'q"
        markup = InlineKeyboardMarkup(row_width=2)
        markup.add(
            InlineKeyboardButton("➕ Kanal qo'shish", callback_data="admin_add_channel"),
            InlineKeyboardButton("➖ Kanal o'chirish", callback_data="admin_remove_channel"),
            InlineKeyboardButton("🔙 Orqaga", callback_data="admin_panel"),
        )
        bot.edit_message_text(f"⚙️ <b>Majburiy kanallar</b>\n\n{ch_list}\n\nFoydalanuvchilar ushbu kanallarga a'zo bo'lishi shart.", chat_id, call.message.message_id, reply_markup=markup, parse_mode="HTML")
        return

    elif data == "admin_add_channel":
        if not is_admin(user_id):
            return
        bot.send_message(chat_id, "➕ Majburiy kanal username ni yuboring (masalan: @mychannel):")
        bot.register_next_step_handler(call.message, admin_add_channel_step)
        return

    elif data == "admin_remove_channel":
        if not is_admin(user_id):
            return
        channels = get_required_channels()
        if not channels:
            bot.send_message(chat_id, "Hech qanday kanal yo'q.")
            return
        markup = InlineKeyboardMarkup()
        for ch in channels:
            markup.add(InlineKeyboardButton(f"❌ @{ch}", callback_data=f"remove_ch_{ch}"))
        markup.add(InlineKeyboardButton("🔙 Orqaga", callback_data="admin_channels"))
        bot.edit_message_text("O'chirish uchun kanalni tanlang:", chat_id, call.message.message_id, reply_markup=markup)
        return

    elif data.startswith("remove_ch_"):
        ch = data.split("_")[2]
        remove_required_channel(ch)
        bot.answer_callback_query(call.id, f"✅ @{ch} o'chirildi!")
        handle_callback(CallbackQuery(id="", from_user=call.from_user, message=call.message, data="admin_channels"))
        return

    elif data == "admin_output_channel":
        if not is_admin(user_id):
            return
        current = get_config("output_channel") or "❌ Sozlanmagan"
        markup = InlineKeyboardMarkup(row_width=1)
        markup.add(InlineKeyboardButton("📤 Chiqish kanalini sozlash", callback_data="admin_set_output_channel"))
        markup.add(Inline
