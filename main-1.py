import asyncio
import csv
import io
import logging
import os
import random
import secrets
from datetime import datetime, timezone
from typing import Optional

import aiohttp
import asyncpg
from aiogram import Bot, Dispatcher, F, Router
from aiogram.client.default import DefaultBotProperties
from aiogram.enums import ParseMode
from aiogram.exceptions import TelegramBadRequest, TelegramForbiddenError
from aiogram.filters import Command, CommandStart
from aiogram.fsm.context import FSMContext
from aiogram.fsm.state import State, StatesGroup
from aiogram.types import (
    BufferedInputFile, CallbackQuery, InlineKeyboardButton, InlineKeyboardMarkup,
    KeyboardButton, Message, ReplyKeyboardMarkup
)
from aiohttp import web

# =========================
# Configuration (Render Environment Variables)
# =========================
BOT_TOKEN = os.getenv("BOT_TOKEN", "").strip()
DATABASE_URL = os.getenv("DATABASE_URL", "").strip()
ADMIN_IDS_RAW = os.getenv("ADMIN_IDS", "").strip()
TMDB_API_KEY = os.getenv("TMDB_API_KEY", "").strip()

ADMIN_IDS = set()
for value in ADMIN_IDS_RAW.replace(";", ",").split(","):
    value = value.strip()
    if value.isdigit():
        ADMIN_IDS.add(int(value))

if not BOT_TOKEN:
    raise RuntimeError("BOT_TOKEN environment variable kiritilmagan.")
if not DATABASE_URL:
    raise RuntimeError("DATABASE_URL environment variable kiritilmagan.")
if not ADMIN_IDS:
    raise RuntimeError("ADMIN_IDS ichiga kamida bitta Telegram ID kiriting.")

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s | %(levelname)s | %(name)s | %(message)s",
)
logger = logging.getLogger("kino-bot")

router = Router()
dp = Dispatcher()
dp.include_router(router)
pool: Optional[asyncpg.Pool] = None
bot_username = "kino_bot"


# =========================
# States
# =========================
class AddMovie(StatesGroup):
    title = State()
    language = State()
    access_type = State()
    price = State()


class AdminFlow(StatesGroup):
    movie_search = State()
    edit_value = State()
    replacement_video = State()
    paid_text = State()
    broadcast_text = State()
    required_channel = State()
    grant_user = State()
    grant_movie = State()
    revoke_user = State()
    revoke_movie = State()
    price_movie = State()
    price_value = State()
    block_user = State()
    add_admin = State()
    remove_admin = State()


# =========================
# UI
# =========================
LANGUAGES = {
    "uz": "🇺🇿 O‘zbek tilidagi kinolar",
    "ru": "🇷🇺 Rus tilidagi kinolar",
    "en": "🇬🇧 Ingliz tilidagi kinolar",
}
ACCESS_LABELS = {"free": "🆓 Bepul", "paid": "💎 Pullik"}

def is_admin(user_id: int) -> bool:
    return user_id in ADMIN_IDS

async def is_db_admin(user_id: int) -> bool:
    if is_admin(user_id):
        return True
    row = await pool.fetchrow("SELECT user_id FROM admins WHERE user_id=$1", user_id)
    return row is not None

def main_keyboard(admin: bool = False) -> ReplyKeyboardMarkup:
    rows = [
        [KeyboardButton(text="🔎 Kino qidirish"), KeyboardButton(text="🌐 Til kategoriyalari")],
        [KeyboardButton(text="🔥 Mashhur kinolar"), KeyboardButton(text="🆕 Yangi kinolar")],
        [KeyboardButton(text="🎲 Tasodifiy kino"), KeyboardButton(text="⭐ Sevimlilarim")],
        [KeyboardButton(text="💎 Pullik tizim"), KeyboardButton(text="🆘 Yordam")],
    ]
    if admin:
        rows.append([KeyboardButton(text="⚙️ Admin panel")])
    return ReplyKeyboardMarkup(keyboard=rows, resize_keyboard=True)

def inline(rows):
    return InlineKeyboardMarkup(inline_keyboard=rows)

def admin_keyboard():
    return inline([
        [InlineKeyboardButton(text="➕ Kino qo‘shish", callback_data="admin:add_help"),
         InlineKeyboardButton(text="🎬 Kinolarni boshqarish", callback_data="admin:movies")],
        [InlineKeyboardButton(text="💎 Pullik tizim", callback_data="admin:paid"),
         InlineKeyboardButton(text="👥 Ruxsat berish", callback_data="admin:grant")],
        [InlineKeyboardButton(text="🚫 Ruxsatni bekor qilish", callback_data="admin:revoke"),
         InlineKeyboardButton(text="📢 Ommaviy xabar", callback_data="admin:broadcast")],
        [InlineKeyboardButton(text="📊 Statistika", callback_data="admin:stats"),
         InlineKeyboardButton(text="📋 Amallar tarixi", callback_data="admin:logs")],
        [InlineKeyboardButton(text="🛡 Kontent himoyasi", callback_data="admin:protection"),
         InlineKeyboardButton(text="📣 Majburiy obuna", callback_data="admin:channels")],
        [InlineKeyboardButton(text="👤 Adminlar", callback_data="admin:admins"),
         InlineKeyboardButton(text="💾 Zaxira nusxa", callback_data="admin:backup")],
        [InlineKeyboardButton(text="🚫 Foydalanuvchini bloklash/ochish", callback_data="admin:block_user")],
    ])

def language_keyboard(prefix="lang"):
    return inline([
        [InlineKeyboardButton(text=LANGUAGES["uz"], callback_data=f"{prefix}:uz")],
        [InlineKeyboardButton(text=LANGUAGES["ru"], callback_data=f"{prefix}:ru")],
        [InlineKeyboardButton(text=LANGUAGES["en"], callback_data=f"{prefix}:en")],
    ])

def access_keyboard(prefix="access"):
    return inline([[
        InlineKeyboardButton(text="🆓 Bepul", callback_data=f"{prefix}:free"),
        InlineKeyboardButton(text="💎 Pullik", callback_data=f"{prefix}:paid"),
        InlineKeyboardButton(text="❌ Bekor qilish", callback_data=f"{prefix}:cancel"),
    ]])

async def log_action(admin_id: int, action: str, details: str = ""):
    try:
        await pool.execute(
            "INSERT INTO action_logs(admin_id, action, details) VALUES($1,$2,$3)",
            admin_id, action[:100], details[:1000]
        )
    except Exception:
        logger.exception("Amallar tarixiga yozishda xato")

async def setting(key: str, default: str = "") -> str:
    row = await pool.fetchrow("SELECT value FROM settings WHERE key=$1", key)
    return row["value"] if row else default

async def set_setting(key: str, value: str):
    await pool.execute(
        """INSERT INTO settings(key,value) VALUES($1,$2)
           ON CONFLICT(key) DO UPDATE SET value=EXCLUDED.value""",
        key, value
    )

async def is_protected() -> bool:
    return await setting("protect_content", "0") == "1"

async def append_username() -> bool:
    return await setting("append_username", "0") == "1"

async def user_has_access(user_id: int, movie_id: int) -> bool:
    if await is_db_admin(user_id):
        return True
    row = await pool.fetchrow(
        """SELECT 1 FROM movie_access
           WHERE user_id=$1 AND movie_id=$2
             AND (expires_at IS NULL OR expires_at > NOW())""",
        user_id, movie_id
    )
    return row is not None

async def add_user(user_id: int, username: Optional[str], full_name: str):
    await pool.execute(
        """INSERT INTO users(user_id, username, full_name, last_seen)
           VALUES($1,$2,$3,NOW())
           ON CONFLICT(user_id) DO UPDATE SET username=EXCLUDED.username,
           full_name=EXCLUDED.full_name,last_seen=NOW()""",
        user_id, username, full_name[:200]
    )

async def tmdb_metadata(title: str) -> dict:
    """TMDb metadata is optional. Missing results never block adding a movie."""
    if not TMDB_API_KEY:
        return {}
    url = "https://api.themoviedb.org/3/search/movie"
    params = {"api_key": TMDB_API_KEY, "query": title, "language": "en-US", "include_adult": "false"}
    timeout = aiohttp.ClientTimeout(total=8)
    try:
        async with aiohttp.ClientSession(timeout=timeout) as session:
            async with session.get(url, params=params) as resp:
                if resp.status != 200:
                    return {}
                data = await resp.json()
            results = data.get("results") or []
            if not results:
                return {}
            # Pick closest title match if available; otherwise first result.
            target = title.casefold().strip()
            result = next((r for r in results if (r.get("title") or "").casefold().strip() == target), results[0])
            movie_id = result.get("id")
            details = {}
            if movie_id:
                async with session.get(
                    f"https://api.themoviedb.org/3/movie/{movie_id}",
                    params={"api_key": TMDB_API_KEY, "language": "en-US"},
                ) as resp:
                    if resp.status == 200:
                        details = await resp.json()
            genres = details.get("genres") or []
            countries = details.get("production_countries") or []
            release_date = details.get("release_date") or result.get("release_date") or ""
            return {
                "year": release_date[:4] if len(release_date) >= 4 else "",
                "genre": ", ".join(g.get("name", "") for g in genres if g.get("name")),
                "country": ", ".join(c.get("name", "") for c in countries if c.get("name")),
                "description": details.get("overview") or result.get("overview") or "",
            }
    except Exception:
        logger.exception("TMDb metadata qidiruvida xato")
        return {}

async def new_movie_code() -> str:
    for _ in range(30):
        code = str(secrets.randbelow(900000) + 100000)
        exists = await pool.fetchval("SELECT 1 FROM movies WHERE code=$1", code)
        if not exists:
            return code
    return secrets.token_hex(4).upper()

async def get_movie(movie_id: int):
    return await pool.fetchrow("SELECT * FROM movies WHERE id=$1", movie_id)

async def show_movie(message: Message, movie, requester_id: int):
    if movie["access_type"] == "paid" and not await user_has_access(requester_id, movie["id"]):
        await message.answer(
            f"💎 <b>{escape_html(movie['title'])}</b>\n"
            f"Bu kino pullik.\n"
            f"💰 Narxi: {movie['price'] or 'Kelishiladi'}\n\n"
            "Kino ochilishi uchun admin bilan bog‘laning.",
            reply_markup=inline([[InlineKeyboardButton(text="🆘 Admin bilan bog‘lanish", callback_data="help:contact")]])
        )
        return
    views = int(movie["views"] or 0) + 1
    await pool.execute("UPDATE movies SET views=$1 WHERE id=$2", views, movie["id"])
    caption_lines = [
        f"🎬 <b>{escape_html(movie['title'])}</b>",
        f"🔢 Kod: <code>{movie['code']}</code>",
        f"🌐 Til: {LANGUAGES.get(movie['language'], movie['language'])}",
    ]
    if movie["year"]:
        caption_lines.append(f"📅 Yil: {escape_html(movie['year'])}")
    if movie["genre"]:
        caption_lines.append(f"🎭 Janr: {escape_html(movie['genre'])}")
    if movie["country"]:
        caption_lines.append(f"🌍 Davlat: {escape_html(movie['country'])}")
    if movie["description"]:
        caption_lines.append(f"\n📝 {escape_html(movie['description'][:700])}")
    if movie["access_type"] == "paid":
        caption_lines.append("\n💎 Pullik kino")
    if await append_username():
        caption_lines.append(f"\n📲 @{bot_username}")
    kb = inline([
        [InlineKeyboardButton(text="⭐ Sevimlilarga qo‘shish", callback_data=f"fav:{movie['id']}"),
         InlineKeyboardButton(text="🔗 Ulashish", callback_data=f"share:{movie['code']}")],
        [InlineKeyboardButton(text="⭐ 1", callback_data=f"rate:{movie['id']}:1"),
         InlineKeyboardButton(text="⭐ 2", callback_data=f"rate:{movie['id']}:2"),
         InlineKeyboardButton(text="⭐ 3", callback_data=f"rate:{movie['id']}:3"),
         InlineKeyboardButton(text="⭐ 4", callback_data=f"rate:{movie['id']}:4"),
         InlineKeyboardButton(text="⭐ 5", callback_data=f"rate:{movie['id']}:5")],
    ])
    await message.answer_video(
        movie["file_id"],
        caption="\n".join(caption_lines)[:1020],
        protect_content=await is_protected(),
        reply_markup=kb
    )

def escape_html(value) -> str:
    if value is None:
        return ""
    s = str(value)
    return s.replace("&", "&amp;").replace("<", "&lt;").replace(">", "&gt;")

async def movie_results(message: Message, rows, title: str = "🎬 Kinolar"):
    if not rows:
        await message.answer("😕 Kino topilmadi. Nomini yoki kodini tekshirib qayta urinib ko‘ring.")
        return
    buttons = []
    for m in rows[:20]:
        access = "💎 " if m["access_type"] == "paid" else ""
        buttons.append([InlineKeyboardButton(
            text=f"{access}{m['title'][:45]} · {m['code']}",
            callback_data=f"movie:{m['id']}"
        )])
    await message.answer(title, reply_markup=inline(buttons))

async def check_subscription(user_id: int) -> tuple[bool, list[str]]:
    channels = await pool.fetch("SELECT channel FROM required_channels ORDER BY channel")
    missing = []
    for row in channels:
        channel = row["channel"]
        try:
            member = await bot.get_chat_member(channel, user_id)
            if member.status in ("left", "kicked"):
                missing.append(channel)
        except Exception:
            # If the channel is misconfigured or the bot lacks access, log it.
            logger.warning("Majburiy obuna tekshiruvi bajarilmadi: %s", channel, exc_info=True)
            missing.append(channel)
    return (not missing, missing)

def subscription_keyboard(channels):
    rows = []
    for channel in channels:
        label = channel.lstrip("@")
        url = f"https://t.me/{label}" if channel.startswith("@") else None
        if url:
            rows.append([InlineKeyboardButton(text=f"➕ {label} kanaliga obuna", url=url)])
    rows.append([InlineKeyboardButton(text="✅ Tekshirish", callback_data="sub:check")])
    return inline(rows)

# =========================
# Database
# =========================
async def init_db():
    global pool
    pool = await asyncpg.create_pool(DATABASE_URL, min_size=1, max_size=5, command_timeout=30)
    async with pool.acquire() as conn:
        await conn.execute("""
        CREATE TABLE IF NOT EXISTS users(
            user_id BIGINT PRIMARY KEY, username TEXT, full_name TEXT,
            created_at TIMESTAMPTZ NOT NULL DEFAULT NOW(), last_seen TIMESTAMPTZ NOT NULL DEFAULT NOW(),
            blocked BOOLEAN NOT NULL DEFAULT FALSE
        );
        CREATE TABLE IF NOT EXISTS admins(user_id BIGINT PRIMARY KEY, added_at TIMESTAMPTZ NOT NULL DEFAULT NOW());
        CREATE TABLE IF NOT EXISTS movies(
            id BIGSERIAL PRIMARY KEY, code TEXT UNIQUE NOT NULL, title TEXT NOT NULL,
            file_id TEXT NOT NULL, language TEXT NOT NULL DEFAULT 'uz',
            year TEXT NOT NULL DEFAULT '', genre TEXT NOT NULL DEFAULT '',
            country TEXT NOT NULL DEFAULT '', description TEXT NOT NULL DEFAULT '',
            access_type TEXT NOT NULL DEFAULT 'free', price TEXT NOT NULL DEFAULT '',
            views BIGINT NOT NULL DEFAULT 0, created_at TIMESTAMPTZ NOT NULL DEFAULT NOW(),
            created_by BIGINT
        );
        CREATE INDEX IF NOT EXISTS movies_title_idx ON movies USING gin (to_tsvector('simple', title));
        CREATE INDEX IF NOT EXISTS movies_language_idx ON movies(language);
        CREATE TABLE IF NOT EXISTS movie_access(
            user_id BIGINT NOT NULL, movie_id BIGINT NOT NULL REFERENCES movies(id) ON DELETE CASCADE,
            granted_by BIGINT, granted_at TIMESTAMPTZ NOT NULL DEFAULT NOW(),
            expires_at TIMESTAMPTZ, PRIMARY KEY(user_id,movie_id)
        );
        CREATE TABLE IF NOT EXISTS favorites(
            user_id BIGINT NOT NULL, movie_id BIGINT NOT NULL REFERENCES movies(id) ON DELETE CASCADE,
            created_at TIMESTAMPTZ NOT NULL DEFAULT NOW(), PRIMARY KEY(user_id,movie_id)
        );
        CREATE TABLE IF NOT EXISTS ratings(
            user_id BIGINT NOT NULL, movie_id BIGINT NOT NULL REFERENCES movies(id) ON DELETE CASCADE,
            rating SMALLINT NOT NULL CHECK(rating BETWEEN 1 AND 5),
            created_at TIMESTAMPTZ NOT NULL DEFAULT NOW(), PRIMARY KEY(user_id,movie_id)
        );
        CREATE TABLE IF NOT EXISTS settings(key TEXT PRIMARY KEY, value TEXT NOT NULL);
        CREATE TABLE IF NOT EXISTS required_channels(channel TEXT PRIMARY KEY, added_at TIMESTAMPTZ NOT NULL DEFAULT NOW());
        CREATE TABLE IF NOT EXISTS action_logs(
            id BIGSERIAL PRIMARY KEY, admin_id BIGINT, action TEXT, details TEXT,
            created_at TIMESTAMPTZ NOT NULL DEFAULT NOW()
        );
        """)
        for admin_id in ADMIN_IDS:
            await conn.execute("INSERT INTO admins(user_id) VALUES($1) ON CONFLICT DO NOTHING", admin_id)
        await conn.execute(
            "INSERT INTO settings(key,value) VALUES('paid_message',$1) ON CONFLICT DO NOTHING",
            "💎 Pullik kinolar haqida ma’lumot olish uchun admin bilan bog‘laning. Narx va to‘lov tartibi kelishiladi."
        )
        await conn.execute("INSERT INTO settings(key,value) VALUES('protect_content','0') ON CONFLICT DO NOTHING")
        await conn.execute("INSERT INTO settings(key,value) VALUES('append_username','0') ON CONFLICT DO NOTHING")
        logger.info("PostgreSQL bazasi tayyor.")

# =========================
# Start and user navigation
# =========================
@router.message(CommandStart())
async def start(message: Message, state: FSMContext, command: CommandStart):
    await state.clear()
    await add_user(message.from_user.id, message.from_user.username, message.from_user.full_name)
    if await pool.fetchval("SELECT blocked FROM users WHERE user_id=$1", message.from_user.id):
        await message.answer("Botdan foydalanishingiz cheklangan.")
        return
    ok, missing = await check_subscription(message.from_user.id)
    if not ok:
        await message.answer("Botdan foydalanish uchun avval quyidagi kanallarga obuna bo‘ling:", reply_markup=subscription_keyboard(missing))
        return
    payload = command.args
    await message.answer(
        "🎬 <b>Kino botiga xush kelibsiz!</b>\n\n"
        "Kinoni nomi yoki kodi orqali qidiring, til kategoriyasini tanlang yoki mashhur kinolarni ko‘ring.",
        reply_markup=main_keyboard(await is_db_admin(message.from_user.id))
    )
    if payload and payload.startswith("movie_"):
        code = payload[6:]
        movie = await pool.fetchrow("SELECT * FROM movies WHERE code=$1", code)
        if movie:
            await show_movie(message, movie, message.from_user.id)

@router.callback_query(F.data == "sub:check")
async def sub_check(call: CallbackQuery):
    ok, missing = await check_subscription(call.from_user.id)
    if ok:
        await call.message.answer("✅ Obuna tasdiqlandi. Endi botdan foydalanishingiz mumkin.", reply_markup=main_keyboard(await is_db_admin(call.from_user.id)))
    else:
        await call.answer("Hali barcha kanallarga obuna bo‘lmagansiz.", show_alert=True)
        await call.message.edit_reply_markup(reply_markup=subscription_keyboard(missing))

@router.message(F.text == "🌐 Til kategoriyalari")
async def categories(message: Message):
    await message.answer("Kerakli til kategoriyasini tanlang:", reply_markup=language_keyboard("browse"))

@router.callback_query(F.data.startswith("browse:"))
async def browse_language(call: CallbackQuery):
    lang = call.data.split(":")[1]
    rows = await pool.fetch("SELECT * FROM movies WHERE language=$1 ORDER BY created_at DESC LIMIT 20", lang)
    await call.answer()
    await movie_results(call.message, rows, LANGUAGES.get(lang, "Kinolar"))

@router.message(F.text == "🔎 Kino qidirish")
async def search_prompt(message: Message):
    await message.answer("🔎 Kino nomini yoki 6 xonali kodini yuboring.")

@router.message(F.text == "🔥 Mashhur kinolar")
async def popular(message: Message):
    rows = await pool.fetch("SELECT * FROM movies ORDER BY views DESC, created_at DESC LIMIT 15")
    await movie_results(message, rows, "🔥 Eng ko‘p ko‘rilgan kinolar")

@router.message(F.text == "🆕 Yangi kinolar")
async def latest(message: Message):
    rows = await pool.fetch("SELECT * FROM movies ORDER BY created_at DESC LIMIT 15")
    await movie_results(message, rows, "🆕 Yangi qo‘shilgan kinolar")

@router.message(F.text == "🎲 Tasodifiy kino")
async def random_movie(message: Message):
    movie = await pool.fetchrow("SELECT * FROM movies WHERE access_type='free' ORDER BY RANDOM() LIMIT 1")
    if not movie:
        await message.answer("Hozircha tavsiya qilish uchun kino yo‘q.")
        return
    await show_movie(message, movie, message.from_user.id)

@router.message(F.text == "⭐ Sevimlilarim")
async def favorites(message: Message):
    rows = await pool.fetch(
        """SELECT m.* FROM favorites f JOIN movies m ON m.id=f.movie_id
           WHERE f.user_id=$1 ORDER BY f.created_at DESC LIMIT 20""", message.from_user.id
    )
    await movie_results(message, rows, "⭐ Sevimli kinolaringiz")

@router.message(F.text == "💎 Pullik tizim")
async def paid_info(message: Message):
    text = await setting("paid_message", "💎 Pullik kinolar haqida admin bilan bog‘laning.")
    await message.answer(text[:4000], parse_mode=None, reply_markup=inline([
        [InlineKeyboardButton(text="🆘 Admin bilan bog‘lanish", callback_data="help:contact")]
    ]))

@router.message(F.text == "🆘 Yordam")
async def help_message(message: Message):
    await message.answer(
        "🆘 <b>Yordam</b>\n\n"
        "• Kino nomi yoki kodini yuborib qidiring.\n"
        "• Til kategoriyasidan kerakli bo‘limni tanlang.\n"
        "• Pullik kino uchun admin ruxsati kerak.\n"
        "• Muammo bo‘lsa, quyidagi tugmani bosing.",
        reply_markup=inline([[InlineKeyboardButton(text="✉️ Admin bilan bog‘lanish", callback_data="help:contact")]])
    )

@router.callback_query(F.data == "help:contact")
async def contact_admin(call: CallbackQuery):
    await call.answer()
    admins = set(ADMIN_IDS)
    rows = await pool.fetch("SELECT user_id FROM admins")
    admins.update(r["user_id"] for r in rows)
    await call.message.answer("✉️ Admin bilan bog‘lanish uchun Telegram orqali quyidagi ID’lardan biriga yozing:\n" +
                              "\n".join(f"• <code>{a}</code>" for a in sorted(admins)))

@router.callback_query(F.data.startswith("movie:"))
async def movie_callback(call: CallbackQuery):
    movie_id = int(call.data.split(":")[1])
    movie = await get_movie(movie_id)
    await call.answer()
    if movie:
        await show_movie(call.message, movie, call.from_user.id)
    else:
        await call.message.answer("Bu kino topilmadi.")

@router.callback_query(F.data.startswith("fav:"))
async def favorite_toggle(call: CallbackQuery):
    movie_id = int(call.data.split(":")[1])
    exists = await pool.fetchval("SELECT 1 FROM favorites WHERE user_id=$1 AND movie_id=$2", call.from_user.id, movie_id)
    if exists:
        await pool.execute("DELETE FROM favorites WHERE user_id=$1 AND movie_id=$2", call.from_user.id, movie_id)
        await call.answer("Sevimlilardan olib tashlandi.")
    else:
        await pool.execute("INSERT INTO favorites(user_id,movie_id) VALUES($1,$2) ON CONFLICT DO NOTHING", call.from_user.id, movie_id)
        await call.answer("Sevimlilarga qo‘shildi.")

@router.callback_query(F.data.startswith("rate:"))
async def rate_movie(call: CallbackQuery):
    _, movie_id, rating = call.data.split(":")
    await pool.execute(
        """INSERT INTO ratings(user_id,movie_id,rating) VALUES($1,$2,$3)
           ON CONFLICT(user_id,movie_id) DO UPDATE SET rating=EXCLUDED.rating,created_at=NOW()""",
        call.from_user.id, int(movie_id), int(rating)
    )
    await call.answer(f"Bahoyingiz saqlandi: {rating}/5 ⭐")

@router.callback_query(F.data.startswith("share:"))
async def share_movie(call: CallbackQuery):
    code = call.data.split(":")[1]
    await call.answer()
    await call.message.answer(f"🔗 Kino havolasi:\nhttps://t.me/{bot_username}?start=movie_{code}")

# =========================
# Admin panel navigation
# =========================
@router.message(F.text == "⚙️ Admin panel")
async def admin_panel(message: Message):
    if not await is_db_admin(message.from_user.id):
        return
    await message.answer("⚙️ <b>Admin panel</b>\nKerakli bo‘limni tanlang:", reply_markup=admin_keyboard())

@router.callback_query(F.data.startswith("admin:"))
async def admin_actions(call: CallbackQuery, state: FSMContext):
    if not await is_db_admin(call.from_user.id):
        await call.answer("Ruxsat yo‘q.", show_alert=True)
        return
    action = call.data.split(":")[1]
    await call.answer()
    if action == "add_help":
        await state.clear()
        await call.message.answer("➕ Kino qo‘shish uchun botga kino videosini yuboring. Keyin bot kino nomi, tili va bepul/pullik turini so‘raydi.")
    elif action == "movies":
        await state.set_state(AdminFlow.movie_search)
        await call.message.answer("Boshqarish uchun kino nomi yoki kodini yuboring.")
    elif action == "paid":
        await call.message.answer(
            "💎 <b>Pullik tizim</b>\n"
            "Pullik kinolarni boshqarish, xabarni tahrirlash va narxlarni sozlash.",
            reply_markup=inline([
                [InlineKeyboardButton(text="📝 Pullik tizim xabarini tahrirlash", callback_data="paid:edit_text")],
                [InlineKeyboardButton(text="🎬 Pullik kinolar ro‘yxati", callback_data="paid:list")],
                [InlineKeyboardButton(text="💰 Kino narxini o‘zgartirish", callback_data="paid:set_price")],
                [InlineKeyboardButton(text="⬅️ Admin panel", callback_data="admin:home")],
            ])
        )
    elif action == "grant":
        await state.set_state(AdminFlow.grant_user)
        await call.message.answer("Ruxsat beriladigan foydalanuvchining Telegram ID raqamini yuboring.")
    elif action == "revoke":
        await state.set_state(AdminFlow.revoke_user)
        await call.message.answer("Ruxsati bekor qilinadigan foydalanuvchining Telegram ID raqamini yuboring.")
    elif action == "broadcast":
        await state.set_state(AdminFlow.broadcast_text)
        await call.message.answer("Yuboriladigan xabar matnini yuboring. Bekor qilish uchun /cancel.")
    elif action == "stats":
        users = await pool.fetchval("SELECT COUNT(*) FROM users")
        active = await pool.fetchval("SELECT COUNT(*) FROM users WHERE last_seen > NOW()-INTERVAL '7 days'")
        movies = await pool.fetchval("SELECT COUNT(*) FROM movies")
        free = await pool.fetchval("SELECT COUNT(*) FROM movies WHERE access_type='free'")
        paid = await pool.fetchval("SELECT COUNT(*) FROM movies WHERE access_type='paid'")
        views = await pool.fetchval("SELECT COALESCE(SUM(views),0) FROM movies")
        await call.message.answer(
            f"📊 <b>Statistika</b>\n\n👥 Foydalanuvchilar: {users}\n"
            f"🟢 Oxirgi 7 kunda faol: {active}\n🎬 Jami kinolar: {movies}\n"
            f"🆓 Bepul: {free}\n💎 Pullik: {paid}\n👁 Jami ko‘rishlar: {views}"
        )
    elif action == "logs":
        rows = await pool.fetch("SELECT * FROM action_logs ORDER BY id DESC LIMIT 10")
        if not rows:
            await call.message.answer("Hozircha amallar tarixi bo‘sh.")
        else:
            text = "📋 <b>Oxirgi amallar</b>\n\n" + "\n".join(
                f"#{r['id']} | admin {r['admin_id']} | {escape_html(r['action'])} | {escape_html(r['details'])}"
                for r in rows
            )
            await call.message.answer(text[:4000])
    elif action == "protection":
        protected = await is_protected()
        username_on = await append_username()
        await call.message.answer(
            "🛡 <b>Kontent himoyasi</b>\nHar bir sozlama mustaqil yoqiladi yoki o‘chiriladi.",
            reply_markup=inline([
                [InlineKeyboardButton(text=f"{'🟢' if protected else '⚪'} Telegram kontent himoyasi", callback_data="protect:toggle")],
                [InlineKeyboardButton(text=f"{'🟢' if username_on else '⚪'} Kino postiga bot username qo‘shish", callback_data="username:toggle")],
                [InlineKeyboardButton(text="⬅️ Admin panel", callback_data="admin:home")],
            ])
        )
    elif action == "channels":
        channels = await pool.fetch("SELECT channel FROM required_channels ORDER BY channel")
        txt = "📣 <b>Majburiy obuna kanallari</b>\n\n"
        txt += "\n".join("• " + escape_html(r["channel"]) for r in channels) if channels else "Hozircha kanal qo‘shilmagan."
        await call.message.answer(txt, reply_markup=inline([
            [InlineKeyboardButton(text="➕ Kanal qo‘shish", callback_data="channels:add")],
            [InlineKeyboardButton(text="➖ Kanalni o‘chirish", callback_data="channels:remove")],
            [InlineKeyboardButton(text="⬅️ Admin panel", callback_data="admin:home")],
        ]))
    elif action == "admins":
        rows = await pool.fetch("SELECT user_id FROM admins ORDER BY user_id")
        txt = "👤 <b>Adminlar</b>\n\n" + "\n".join(f"• <code>{r['user_id']}</code>" for r in rows)
        await call.message.answer(txt, reply_markup=inline([
            [InlineKeyboardButton(text="➕ Admin qo‘shish", callback_data="admins:add")],
            [InlineKeyboardButton(text="➖ Adminni olib tashlash", callback_data="admins:remove")],
            [InlineKeyboardButton(text="⬅️ Admin panel", callback_data="admin:home")],
        ]))
    elif action == "backup":
        await send_backup(call.message)
    elif action == "block_user":
        await state.set_state(AdminFlow.block_user)
        await call.message.answer("Bloklash yoki blokdan chiqarish uchun foydalanuvchining Telegram ID raqamini yuboring.")
    elif action == "home":
        await call.message.answer("⚙️ Admin panel", reply_markup=admin_keyboard())

@router.callback_query(F.data.startswith("protect:"))
async def toggle_protect(call: CallbackQuery):
    if not await is_db_admin(call.from_user.id):
        await call.answer("Ruxsat yo‘q.", show_alert=True); return
    current = await is_protected()
    await set_setting("protect_content", "0" if current else "1")
    await log_action(call.from_user.id, "content_protection", "enabled" if not current else "disabled")
    await call.answer("Sozlama yangilandi.")
    await call.message.edit_reply_markup(reply_markup=inline([
        [InlineKeyboardButton(text=f"{'🟢' if not current else '⚪'} Telegram kontent himoyasi", callback_data="protect:toggle")],
        [InlineKeyboardButton(text=f"{'🟢' if await append_username() else '⚪'} Kino postiga bot username qo‘shish", callback_data="username:toggle")],
        [InlineKeyboardButton(text="⬅️ Admin panel", callback_data="admin:home")],
    ]))

@router.callback_query(F.data.startswith("username:"))
async def toggle_username(call: CallbackQuery):
    if not await is_db_admin(call.from_user.id):
        await call.answer("Ruxsat yo‘q.", show_alert=True); return
    current = await append_username()
    await set_setting("append_username", "0" if current else "1")
    await log_action(call.from_user.id, "append_username", "enabled" if not current else "disabled")
    await call.answer("Sozlama yangilandi.")
    await call.message.edit_reply_markup(reply_markup=inline([
        [InlineKeyboardButton(text=f"{'🟢' if await is_protected() else '⚪'} Telegram kontent himoyasi", callback_data="protect:toggle")],
        [InlineKeyboardButton(text=f"{'🟢' if not current else '⚪'} Kino postiga bot username qo‘shish", callback_data="username:toggle")],
        [InlineKeyboardButton(text="⬅️ Admin panel", callback_data="admin:home")],
    ]))

# =========================
# Adding movies: video -> title -> language -> free/paid -> price if paid
# =========================
@router.message(F.video)
async def receive_admin_video(message: Message, state: FSMContext):
    if not await is_db_admin(message.from_user.id):
        return
    current = await state.get_state()
    if current == AdminFlow.replacement_video.state:
        data = await state.get_data()
        movie_id = data.get("edit_movie_id")
        if movie_id:
            await pool.execute("UPDATE movies SET file_id=$1 WHERE id=$2", message.video.file_id, movie_id)
            await log_action(message.from_user.id, "replace_video", str(movie_id))
            await state.clear()
            await message.answer("✅ Kino videosi yangilandi.", reply_markup=admin_keyboard())
            return
    await state.clear()
    await state.update_data(file_id=message.video.file_id)
    await state.set_state(AddMovie.title)
    await message.answer("🎬 Video qabul qilindi. Endi kino nomini yuboring.")

@router.message(AddMovie.title, F.text)
async def add_title(message: Message, state: FSMContext):
    title = message.text.strip()
    if len(title) < 1 or len(title) > 200:
        await message.answer("Kino nomi 1–200 ta belgi bo‘lsin. Qayta yuboring.")
        return
    metadata = await tmdb_metadata(title)
    await state.update_data(title=title, metadata=metadata)
    await state.set_state(AddMovie.language)
    found = "Ma’lumotlar topildi." if metadata else "Qo‘shimcha ma’lumotlar topilmadi; kino baribir qo‘shiladi."
    await message.answer(f"🔎 {found}\nEndi kino tilini tanlang:", reply_markup=language_keyboard("addlang"))

@router.callback_query(F.data.startswith("addlang:"))
async def add_language(call: CallbackQuery, state: FSMContext):
    if not await is_db_admin(call.from_user.id) or await state.get_state() != AddMovie.language.state:
        await call.answer("Bu amal hozir mavjud emas.", show_alert=True); return
    lang = call.data.split(":")[1]
    if lang not in LANGUAGES:
        await call.answer("Til noto‘g‘ri."); return
    await state.update_data(language=lang)
    await state.set_state(AddMovie.access_type)
    await call.answer()
    await call.message.answer("Bu kino bepulmi yoki pullikmi?", reply_markup=access_keyboard("addaccess"))

@router.callback_query(F.data.startswith("addaccess:"))
async def add_access_type(call: CallbackQuery, state: FSMContext):
    if not await is_db_admin(call.from_user.id) or await state.get_state() != AddMovie.access_type.state:
        await call.answer("Bu amal hozir mavjud emas.", show_alert=True); return
    choice = call.data.split(":")[1]
    if choice == "cancel":
        await state.clear()
        await call.answer("Bekor qilindi.")
        await call.message.answer("Kino qo‘shish bekor qilindi.")
        return
    if choice not in ("free", "paid"):
        await call.answer("Noto‘g‘ri tanlov."); return
    await state.update_data(access_type=choice)
    if choice == "paid":
        await state.set_state(AddMovie.price)
        await call.answer()
        await call.message.answer("💰 Narxni yuboring (masalan: 10 000 so‘m) yoki kelishilgan deb yozing.")
        return
    await call.answer()
    await finish_add_movie(call.message, state, call.from_user.id, "")

@router.message(AddMovie.price, F.text)
async def add_price(message: Message, state: FSMContext):
    price = message.text.strip()
    if len(price) > 100:
        await message.answer("Narx matni juda uzun. Qayta yuboring.")
        return
    await finish_add_movie(message, state, message.from_user.id, price)

async def finish_add_movie(message: Message, state: FSMContext, admin_id: int, price: str):
    data = await state.get_data()
    meta = data.get("metadata") or {}
    code = await new_movie_code()
    row = await pool.fetchrow(
        """INSERT INTO movies(code,title,file_id,language,year,genre,country,description,
           access_type,price,created_by)
           VALUES($1,$2,$3,$4,$5,$6,$7,$8,$9,$10,$11) RETURNING id""",
        code, data["title"], data["file_id"], data["language"],
        meta.get("year", ""), meta.get("genre", ""), meta.get("country", ""),
        meta.get("description", ""), data.get("access_type", "free"), price, admin_id
    )
    await log_action(admin_id, "add_movie", f"{data['title']} | {code} | {data['access_type']}")
    await state.clear()
    await message.answer(
        f"✅ <b>Kino muvaffaqiyatli qo‘shildi!</b>\n\n"
        f"🎬 Nomi: {escape_html(data['title'])}\n🔢 Kodi: <code>{code}</code>\n"
        f"🌐 Tili: {LANGUAGES[data['language']]}\n"
        f"💳 Turi: {ACCESS_LABELS[data['access_type']]}\n"
        f"📅 Yili: {escape_html(meta.get('year') or 'Topilmadi')}\n"
        f"🎭 Janri: {escape_html(meta.get('genre') or 'Topilmadi')}\n"
        f"🌍 Davlati: {escape_html(meta.get('country') or 'Topilmadi')}",
        reply_markup=admin_keyboard()
    )

# =========================
# Admin movie management
# =========================
@router.message(AdminFlow.movie_search, F.text)
async def admin_movie_search(message: Message, state: FSMContext):
    query = message.text.strip()
    rows = await pool.fetch(
        """SELECT * FROM movies WHERE code=$1 OR title ILIKE $2
           ORDER BY created_at DESC LIMIT 20""", query, f"%{query}%"
    )
    if not rows:
        await message.answer("Kino topilmadi. Boshqa nom yoki kod bilan urinib ko‘ring. /cancel")
        return
    buttons = [[InlineKeyboardButton(text=f"{r['title'][:40]} · {r['code']}", callback_data=f"manage:{r['id']}")] for r in rows]
    await message.answer("Tahrirlamoqchi bo‘lgan kinoni tanlang:", reply_markup=inline(buttons))
    await state.clear()

@router.callback_query(F.data.startswith("manage:"))
async def manage_movie(call: CallbackQuery):
    if not await is_db_admin(call.from_user.id):
        await call.answer("Ruxsat yo‘q.", show_alert=True); return
    movie = await get_movie(int(call.data.split(":")[1]))
    if not movie:
        await call.answer("Kino topilmadi.", show_alert=True); return
    await call.answer()
    await call.message.answer(
        f"🎬 <b>{escape_html(movie['title'])}</b> · <code>{movie['code']}</code>\n"
        f"Til: {LANGUAGES.get(movie['language'])}\nTur: {ACCESS_LABELS.get(movie['access_type'])}\n"
        f"Yil: {escape_html(movie['year']) or '—'} | Janr: {escape_html(movie['genre']) or '—'}",
        reply_markup=inline([
            [InlineKeyboardButton(text="✏️ Nomini o‘zgartirish", callback_data=f"edit:{movie['id']}:title"),
             InlineKeyboardButton(text="📅 Yil", callback_data=f"edit:{movie['id']}:year")],
            [InlineKeyboardButton(text="🎭 Janr", callback_data=f"edit:{movie['id']}:genre"),
             InlineKeyboardButton(text="🌍 Davlat", callback_data=f"edit:{movie['id']}:country")],
            [InlineKeyboardButton(text="📝 Tavsif", callback_data=f"edit:{movie['id']}:description"),
             InlineKeyboardButton(text="🌐 Til", callback_data=f"edit:{movie['id']}:language")],
            [InlineKeyboardButton(text="🆓/💎 Bepul-pullikni almashtirish", callback_data=f"edit:{movie['id']}:access_type")],
            [InlineKeyboardButton(text="💰 Narxni o‘zgartirish", callback_data=f"edit:{movie['id']}:price"),
             InlineKeyboardButton(text="🎞 Videoni almashtirish", callback_data=f"replace:{movie['id']}")],
            [InlineKeyboardButton(text="🗑 O‘chirish", callback_data=f"delete:{movie['id']}")],
        ])
    )

@router.callback_query(F.data.startswith("edit:"))
async def edit_movie_start(call: CallbackQuery, state: FSMContext):
    if not await is_db_admin(call.from_user.id):
        await call.answer("Ruxsat yo‘q.", show_alert=True); return
    _, movie_id, field = call.data.split(":")
    if field == "access_type":
        movie = await get_movie(int(movie_id))
        new_type = "paid" if movie["access_type"] == "free" else "free"
        await pool.execute("UPDATE movies SET access_type=$1 WHERE id=$2", new_type, int(movie_id))
        await log_action(call.from_user.id, "change_access_type", f"{movie_id} -> {new_type}")
        await call.answer(f"Endi: {ACCESS_LABELS[new_type]}")
        await call.message.answer(f"✅ Kino turi o‘zgardi: {ACCESS_LABELS[new_type]}")
        return
    if field == "language":
        await state.update_data(edit_movie_id=int(movie_id), edit_field=field)
        await state.set_state(AdminFlow.edit_value)
        await call.answer()
        await call.message.answer("Yangi tilni tanlang:", reply_markup=language_keyboard("editlang"))
        return
    await state.update_data(edit_movie_id=int(movie_id), edit_field=field)
    await state.set_state(AdminFlow.edit_value)
    await call.answer()
    prompts = {
        "title": "Yangi kino nomini yuboring.",
        "year": "Yangi yilni yuboring (yoki bo‘sh qiymat uchun — yozing).",
        "genre": "Yangi janrni yuboring.",
        "country": "Yangi davlatni yuboring.",
        "description": "Yangi tavsifni yuboring.",
        "price": "Yangi narxni yuboring.",
    }
    await call.message.answer(prompts.get(field, "Yangi qiymatni yuboring."))

@router.callback_query(F.data.startswith("editlang:"))
async def edit_language_save(call: CallbackQuery, state: FSMContext):
    if not await is_db_admin(call.from_user.id):
        await call.answer("Ruxsat yo‘q.", show_alert=True); return
    data = await state.get_data()
    if await state.get_state() != AdminFlow.edit_value.state or data.get("edit_field") != "language":
        await call.answer("Bu amal hozir mavjud emas.", show_alert=True); return
    lang = call.data.split(":")[1]
    if lang not in LANGUAGES:
        await call.answer("Til noto‘g‘ri."); return
    await pool.execute("UPDATE movies SET language=$1 WHERE id=$2", lang, data["edit_movie_id"])
    await log_action(call.from_user.id, "edit_movie_language", f"{data['edit_movie_id']} -> {lang}")
    await state.clear()
    await call.answer("Til yangilandi.")
    await call.message.answer("✅ Kino tili o‘zgartirildi.")

@router.message(AdminFlow.edit_value, F.text)
async def edit_movie_save(message: Message, state: FSMContext):
    data = await state.get_data()
    field = data.get("edit_field")
    movie_id = data.get("edit_movie_id")
    allowed = {"title", "year", "genre", "country", "description", "price"}
    if field not in allowed:
        await message.answer("Maydon noto‘g‘ri. /cancel")
        await state.clear()
        return
    value = "" if message.text.strip() == "—" else message.text.strip()
    max_len = 2000 if field == "description" else 200
    if len(value) > max_len:
        await message.answer(f"Qiymat {max_len} belgidan oshmasin.")
        return
    await pool.execute(f"UPDATE movies SET {field}=$1 WHERE id=$2", value, movie_id)
    await log_action(message.from_user.id, "edit_movie_" + field, f"{movie_id}")
    await state.clear()
    await message.answer("✅ Kino ma’lumoti yangilandi. Kino kodi o‘zgarmadi.", reply_markup=admin_keyboard())

@router.callback_query(F.data.startswith("replace:"))
async def replace_movie_start(call: CallbackQuery, state: FSMContext):
    if not await is_db_admin(call.from_user.id):
        await call.answer("Ruxsat yo‘q.", show_alert=True); return
    movie_id = int(call.data.split(":")[1])
    await state.update_data(edit_movie_id=movie_id)
    await state.set_state(AdminFlow.replacement_video)
    await call.answer()
    await call.message.answer("🎞 Yangi videoni yuboring. Kino kodi o‘zgarmaydi.")

@router.callback_query(F.data.startswith("delete:"))
async def delete_movie_confirm(call: CallbackQuery):
    if not await is_db_admin(call.from_user.id):
        await call.answer("Ruxsat yo‘q.", show_alert=True); return
    movie_id = int(call.data.split(":")[1])
    await call.answer()
    await call.message.answer("⚠️ Bu kinoni butunlay o‘chirishni tasdiqlaysizmi?", reply_markup=inline([
        [InlineKeyboardButton(text="🗑 Ha, o‘chir", callback_data=f"delete_yes:{movie_id}"),
         InlineKeyboardButton(text="❌ Yo‘q", callback_data="delete_no")]
    ]))

@router.callback_query(F.data.startswith("delete_yes:"))
async def delete_movie_yes(call: CallbackQuery):
    if not await is_db_admin(call.from_user.id):
        await call.answer("Ruxsat yo‘q.", show_alert=True); return
    movie_id = int(call.data.split(":")[1])
    title = await pool.fetchval("SELECT title FROM movies WHERE id=$1", movie_id)
    await pool.execute("DELETE FROM movies WHERE id=$1", movie_id)
    await log_action(call.from_user.id, "delete_movie", f"{movie_id} {title}")
    await call.answer("Kino o‘chirildi.")
    await call.message.answer("🗑 Kino o‘chirildi.")

@router.callback_query(F.data == "delete_no")
async def delete_movie_no(call: CallbackQuery):
    await call.answer("Bekor qilindi.")
    await call.message.answer("O‘chirish bekor qilindi.")

# =========================
# Paid movie administration and access grants
# =========================
@router.callback_query(F.data == "paid:edit_text")
async def paid_text_start(call: CallbackQuery, state: FSMContext):
    if not await is_db_admin(call.from_user.id):
        await call.answer("Ruxsat yo‘q.", show_alert=True); return
    await state.set_state(AdminFlow.paid_text)
    await call.answer()
    await call.message.answer("Foydalanuvchilarga ko‘rinadigan pullik tizim xabarini yuboring.")

@router.message(AdminFlow.paid_text, F.text)
async def paid_text_save(message: Message, state: FSMContext):
    await set_setting("paid_message", message.text[:4000])
    await log_action(message.from_user.id, "edit_paid_message", "updated")
    await state.clear()
    await message.answer("✅ Pullik tizim xabari yangilandi.", reply_markup=admin_keyboard())

@router.callback_query(F.data == "paid:list")
async def paid_list(call: CallbackQuery):
    if not await is_db_admin(call.from_user.id):
        await call.answer("Ruxsat yo‘q.", show_alert=True); return
    rows = await pool.fetch("SELECT * FROM movies WHERE access_type='paid' ORDER BY created_at DESC LIMIT 30")
    await call.answer()
    await movie_results(call.message, rows, "💎 Pullik kinolar")

@router.callback_query(F.data == "paid:set_price")
async def set_price_start(call: CallbackQuery, state: FSMContext):
    if not await is_db_admin(call.from_user.id):
        await call.answer("Ruxsat yo‘q.", show_alert=True); return
    await state.set_state(AdminFlow.price_movie)
    await call.answer()
    await call.message.answer("Narxini o‘zgartirmoqchi bo‘lgan kinoning kodini yuboring.")

@router.message(AdminFlow.price_movie, F.text)
async def set_price_movie(message: Message, state: FSMContext):
    movie = await pool.fetchrow("SELECT * FROM movies WHERE code=$1", message.text.strip())
    if not movie:
        await message.answer("Kino kodi topilmadi. Qayta yuboring.")
        return
    await state.update_data(price_movie_id=movie["id"])
    await state.set_state(AdminFlow.price_value)
    await message.answer(f"“{escape_html(movie['title'])}” uchun yangi narxni yuboring.")

@router.message(AdminFlow.price_value, F.text)
async def set_price_value(message: Message, state: FSMContext):
    data = await state.get_data()
    await pool.execute("UPDATE movies SET price=$1,access_type='paid' WHERE id=$2", message.text.strip()[:100], data["price_movie_id"])
    await log_action(message.from_user.id, "set_movie_price", str(data["price_movie_id"]))
    await state.clear()
    await message.answer("✅ Narx saqlandi. Kino pullik sifatida belgilandi.", reply_markup=admin_keyboard())

@router.message(AdminFlow.grant_user, F.text)
async def grant_user(message: Message, state: FSMContext):
    if not message.text.strip().isdigit():
        await message.answer("Telegram ID faqat raqam bo‘lishi kerak.")
        return
    await state.update_data(target_user=int(message.text.strip()))
    await state.set_state(AdminFlow.grant_movie)
    await message.answer("Ruxsat beriladigan kinoning kodini yuboring.")

@router.message(AdminFlow.grant_movie, F.text)
async def grant_movie(message: Message, state: FSMContext):
    data = await state.get_data()
    movie = await pool.fetchrow("SELECT * FROM movies WHERE code=$1", message.text.strip())
    if not movie:
        await message.answer("Kino kodi topilmadi. Qayta yuboring.")
        return
    user_id = data["target_user"]
    await pool.execute(
        """INSERT INTO movie_access(user_id,movie_id,granted_by)
           VALUES($1,$2,$3) ON CONFLICT(user_id,movie_id)
           DO UPDATE SET granted_by=EXCLUDED.granted_by,granted_at=NOW(),expires_at=NULL""",
        user_id, movie["id"], message.from_user.id
    )
    await log_action(message.from_user.id, "grant_movie_access", f"user={user_id}, movie={movie['code']}")
    await state.clear()
    await message.answer(f"✅ {user_id} foydalanuvchiga “{escape_html(movie['title'])}” uchun ruxsat berildi.", reply_markup=admin_keyboard())
    try:
        await bot.send_message(user_id, f"✅ Sizga “{escape_html(movie['title'])}” kinoni ko‘rish uchun ruxsat berildi. Kod: <code>{movie['code']}</code>")
    except Exception:
        pass

@router.message(AdminFlow.revoke_user, F.text)
async def revoke_user(message: Message, state: FSMContext):
    if not message.text.strip().isdigit():
        await message.answer("Telegram ID faqat raqam bo‘lishi kerak.")
        return
    await state.update_data(target_user=int(message.text.strip()))
    await state.set_state(AdminFlow.revoke_movie)
    await message.answer("Ruxsati bekor qilinadigan kinoning kodini yuboring.")

@router.message(AdminFlow.revoke_movie, F.text)
async def revoke_movie(message: Message, state: FSMContext):
    data = await state.get_data()
    movie = await pool.fetchrow("SELECT * FROM movies WHERE code=$1", message.text.strip())
    if not movie:
        await message.answer("Kino kodi topilmadi. Qayta yuboring.")
        return
    await pool.execute("DELETE FROM movie_access WHERE user_id=$1 AND movie_id=$2", data["target_user"], movie["id"])
    await log_action(message.from_user.id, "revoke_movie_access", f"user={data['target_user']}, movie={movie['code']}")
    await state.clear()
    await message.answer("✅ Kino uchun ruxsat bekor qilindi.", reply_markup=admin_keyboard())

# =========================
# Required subscription, broadcast, admins, backup
# =========================
@router.callback_query(F.data == "channels:add")
async def channel_add_start(call: CallbackQuery, state: FSMContext):
    if not await is_db_admin(call.from_user.id):
        await call.answer("Ruxsat yo‘q.", show_alert=True); return
    await state.set_state(AdminFlow.required_channel)
    await call.answer()
    await call.message.answer("Majburiy obuna kanalini yuboring, masalan: @kanal_nomi. Bot kanal admini bo‘lishi kerak.")

@router.message(AdminFlow.required_channel, F.text)
async def channel_add_save(message: Message, state: FSMContext):
    channel = message.text.strip()
    if not channel.startswith("@") and not channel.lstrip("-").isdigit():
        await message.answer("Kanalni @username yoki raqamli chat ID ko‘rinishida yuboring.")
        return
    await pool.execute("INSERT INTO required_channels(channel) VALUES($1) ON CONFLICT DO NOTHING", channel)
    await log_action(message.from_user.id, "add_required_channel", channel)
    await state.clear()
    await message.answer(f"✅ Majburiy obuna kanali qo‘shildi: {escape_html(channel)}", reply_markup=admin_keyboard())

@router.callback_query(F.data == "channels:remove")
async def channel_remove_start(call: CallbackQuery):
    if not await is_db_admin(call.from_user.id):
        await call.answer("Ruxsat yo‘q.", show_alert=True); return
    rows = await pool.fetch("SELECT channel FROM required_channels ORDER BY channel")
    if not rows:
        await call.answer("O‘chirish uchun kanal yo‘q.", show_alert=True); return
    await call.answer()
    await call.message.answer("O‘chirmoqchi bo‘lgan kanalni tanlang:", reply_markup=inline([
        [InlineKeyboardButton(text=r["channel"], callback_data="channel_del:" + r["channel"])] for r in rows
    ]))

@router.callback_query(F.data.startswith("channel_del:"))
async def channel_remove(call: CallbackQuery):
    if not await is_db_admin(call.from_user.id):
        await call.answer("Ruxsat yo‘q.", show_alert=True); return
    channel = call.data[len("channel_del:"):]
    await pool.execute("DELETE FROM required_channels WHERE channel=$1", channel)
    await log_action(call.from_user.id, "remove_required_channel", channel)
    await call.answer("Kanal o‘chirildi.")
    await call.message.answer(f"✅ {escape_html(channel)} majburiy obunadan olib tashlandi.")

@router.message(AdminFlow.broadcast_text, F.text)
async def broadcast_send(message: Message, state: FSMContext):
    text = message.text[:4000]
    users = await pool.fetch("SELECT user_id FROM users WHERE blocked=FALSE")
    sent = failed = 0
    status = await message.answer(f"📢 Xabar yuborilmoqda: 0/{len(users)}")
    for index, row in enumerate(users, start=1):
        try:
            await bot.send_message(row["user_id"], text, parse_mode=None)
            sent += 1
        except (TelegramForbiddenError, TelegramBadRequest):
            failed += 1
        except Exception:
            failed += 1
        if index % 25 == 0:
            try:
                await status.edit_text(f"📢 Xabar yuborilmoqda: {index}/{len(users)}")
            except Exception:
                pass
        await asyncio.sleep(0.04)
    await state.clear()
    await log_action(message.from_user.id, "broadcast", f"sent={sent}, failed={failed}")
    await status.edit_text(f"✅ Ommaviy xabar tugadi.\nYuborildi: {sent}\nXato: {failed}")

@router.message(AdminFlow.block_user, F.text)
async def block_user_toggle(message: Message, state: FSMContext):
    if not await is_db_admin(message.from_user.id):
        await state.clear()
        return
    if not message.text.strip().isdigit():
        await message.answer("Telegram ID faqat raqam bo‘lishi kerak.")
        return
    user_id = int(message.text.strip())
    row = await pool.fetchrow("SELECT blocked FROM users WHERE user_id=$1", user_id)
    if not row:
        await message.answer("Bu foydalanuvchi bazada topilmadi. Avval botni /start orqali ishga tushirgan bo‘lishi mumkin.")
        await state.clear()
        return
    new_value = not row["blocked"]
    await pool.execute("UPDATE users SET blocked=$1 WHERE user_id=$2", new_value, user_id)
    await log_action(message.from_user.id, "block_user" if new_value else "unblock_user", str(user_id))
    await state.clear()
    await message.answer(
        f"✅ Foydalanuvchi {user_id} {'bloklandi' if new_value else 'blokdan chiqarildi'}.",
        reply_markup=admin_keyboard()
    )

@router.callback_query(F.data == "admins:add")
async def add_admin_start(call: CallbackQuery, state: FSMContext):
    if not is_admin(call.from_user.id):
        await call.answer("Faqat asosiy admin (ADMIN_IDS) admin qo‘sha oladi.", show_alert=True); return
    await state.set_state(AdminFlow.add_admin)
    await call.answer()
    await call.message.answer("Yangi adminning Telegram ID raqamini yuboring.")

@router.message(AdminFlow.add_admin, F.text)
async def add_admin_save(message: Message, state: FSMContext):
    if not is_admin(message.from_user.id):
        await state.clear(); return
    if not message.text.strip().isdigit():
        await message.answer("ID faqat raqam bo‘lishi kerak."); return
    uid = int(message.text.strip())
    await pool.execute("INSERT INTO admins(user_id) VALUES($1) ON CONFLICT DO NOTHING", uid)
    await log_action(message.from_user.id, "add_admin", str(uid))
    await state.clear()
    await message.answer(f"✅ {uid} adminlar ro‘yxatiga qo‘shildi.", reply_markup=admin_keyboard())

@router.callback_query(F.data == "admins:remove")
async def remove_admin_start(call: CallbackQuery, state: FSMContext):
    if not is_admin(call.from_user.id):
        await call.answer("Faqat asosiy admin (ADMIN_IDS) adminni olib tashlay oladi.", show_alert=True); return
    await state.set_state(AdminFlow.remove_admin)
    await call.answer()
    await call.message.answer("Olib tashlanadigan adminning Telegram ID raqamini yuboring. ADMIN_IDS ichidagi asosiy adminlarni bu usul bilan olib tashlab bo‘lmaydi.")

@router.message(AdminFlow.remove_admin, F.text)
async def remove_admin_save(message: Message, state: FSMContext):
    if not is_admin(message.from_user.id):
        await state.clear(); return
    if not message.text.strip().isdigit():
        await message.answer("ID faqat raqam bo‘lishi kerak."); return
    uid = int(message.text.strip())
    if uid in ADMIN_IDS:
        await message.answer("Bu ID Render’dagi ADMIN_IDS ichida asosiy admin sifatida belgilangan.")
        await state.clear()
        return
    await pool.execute("DELETE FROM admins WHERE user_id=$1", uid)
    await log_action(message.from_user.id, "remove_admin", str(uid))
    await state.clear()
    await message.answer(f"✅ {uid} adminlar ro‘yxatidan olib tashlandi.", reply_markup=admin_keyboard())

async def send_backup(message: Message):
    """CSV export backup for key catalog/user tables; DB credentials are never exported."""
    movies = await pool.fetch("SELECT code,title,language,year,genre,country,description,access_type,price,views,created_at FROM movies ORDER BY id")
    users = await pool.fetch("SELECT user_id,username,full_name,created_at,last_seen,blocked FROM users ORDER BY user_id")
    output = io.StringIO()
    writer = csv.writer(output)
    writer.writerow(["MOVIES"])
    writer.writerow(["code","title","language","year","genre","country","description","access_type","price","views","created_at"])
    for row in movies:
        writer.writerow([row[k] for k in ["code","title","language","year","genre","country","description","access_type","price","views","created_at"]])
    writer.writerow([])
    writer.writerow(["USERS"])
    writer.writerow(["user_id","username","full_name","created_at","last_seen","blocked"])
    for row in users:
        writer.writerow([row[k] for k in ["user_id","username","full_name","created_at","last_seen","blocked"]])
    file = BufferedInputFile(output.getvalue().encode("utf-8-sig"), filename="kinobot_backup.csv")
    await message.answer_document(file, caption="💾 Kino va foydalanuvchilar jadvalining CSV zaxira nusxasi.")
    await log_action(message.from_user.id, "backup_csv", f"movies={len(movies)}, users={len(users)}")

# =========================
# General search, blocking and cancellation
# =========================
@router.message(Command("cancel"))
async def cancel_flow(message: Message, state: FSMContext):
    await state.clear()
    await message.answer("❌ Amal bekor qilindi.", reply_markup=main_keyboard(await is_db_admin(message.from_user.id)))

@router.message(F.text)
async def general_text(message: Message, state: FSMContext):
    if not message.from_user:
        return
    # All explicit admin/user flows above have higher priority and are handled first.
    current_state = await state.get_state()
    if current_state:
        return
    if message.text.startswith("/"):
        return
    if not await is_db_admin(message.from_user.id):
        await add_user(message.from_user.id, message.from_user.username, message.from_user.full_name)
        blocked = await pool.fetchval("SELECT blocked FROM users WHERE user_id=$1", message.from_user.id)
        if blocked:
            await message.answer("Botdan foydalanishingiz cheklangan.")
            return
        ok, missing = await check_subscription(message.from_user.id)
        if not ok:
            await message.answer("Botdan foydalanish uchun avval quyidagi kanallarga obuna bo‘ling:", reply_markup=subscription_keyboard(missing))
            return
    query = message.text.strip()
    if not query:
        return
    if len(query) > 200:
        query = query[:200]
    if query.isdigit():
        rows = await pool.fetch("SELECT * FROM movies WHERE code=$1 LIMIT 10", query)
    else:
        rows = await pool.fetch(
            """SELECT * FROM movies WHERE title ILIKE $1
               ORDER BY CASE WHEN LOWER(title)=LOWER($2) THEN 0 ELSE 1 END, created_at DESC LIMIT 20""",
            f"%{query}%", query
        )
    await movie_results(message, rows, f"🔎 “{escape_html(query)}” bo‘yicha natijalar")

# =========================
# Runtime
# =========================
async def health_handler(request):
    return web.Response(text="Kino bot ishlayapti.", status=200)

async def start_health_server():
    # Render Web Service port tekshiruvini o‘tkazishi uchun kichik health endpoint.
    app = web.Application()
    app.router.add_get("/", health_handler)
    app.router.add_get("/health", health_handler)
    runner = web.AppRunner(app)
    await runner.setup()
    port = int(os.getenv("PORT", "10000"))
    site = web.TCPSite(runner, "0.0.0.0", port)
    await site.start()
    logger.info("Health server 0.0.0.0:%s portida ishga tushdi.", port)
    return runner

async def main():
    global bot, bot_username
    await init_db()
    bot = Bot(BOT_TOKEN, default=DefaultBotProperties(parse_mode=ParseMode.HTML))
    me = await bot.get_me()
    bot_username = me.username or "kino_bot"
    web_runner = await start_health_server()
    logger.info("Bot ishga tushdi: @%s", bot_username)
    try:
        await dp.start_polling(bot, allowed_updates=dp.resolve_used_update_types())
    finally:
        await web_runner.cleanup()
        await bot.session.close()
        if pool:
            await pool.close()

if __name__ == "__main__":
    asyncio.run(main())
