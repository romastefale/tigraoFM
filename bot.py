import os
import re
import html
import time
import logging
import asyncio
from typing import Any, Dict

import requests
import redis

from telegram import (
    Update,
    InlineKeyboardMarkup,
    InlineKeyboardButton,
    InlineQueryResultPhoto,
)
from telegram.constants import ParseMode
from telegram.ext import (
    Application,
    MessageHandler,
    CallbackQueryHandler,
    InlineQueryHandler,
    CommandHandler,
    ContextTypes,
    filters,
)

# =========================
# LOGGING
# =========================

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s | %(levelname)s | %(name)s | %(message)s"
)
logger = logging.getLogger("bot")

TOKEN = os.getenv("TELEGRAM_TOKEN")
REDIS_URL = os.getenv("REDIS_URL")

session = requests.Session()

redis_client = redis.Redis.from_url(
    REDIS_URL,
    decode_responses=True
) if REDIS_URL else None

# =========================
# CACHE
# =========================

CACHE: Dict[str, Dict] = {}
CACHE_TTL = 60

def get_cache(key):
    data = CACHE.get(key)
    if not data:
        return None

    if time.time() - data["time"] > CACHE_TTL:
        del CACHE[key]
        return None

    return data["value"]

def set_cache(key, value):
    CACHE[key] = {
        "value": value,
        "time": time.time()
    }

# =========================
# SANITIZE
# =========================

FORBIDDEN = re.compile(
    r'[\u0600-\u06FF\u0400-\u04FF\u4E00-\u9FFF\u0900-\u097F\u0980-\u09FF]'
)

def sanitize(text: Any) -> str:
    if not text:
        return "Unknown"
    text = str(text)
    if not FORBIDDEN.search(text):
        return text
    return "Unknown"

def esc(text):
    return html.escape(sanitize(text))

# =========================
# REDIS COUNTER
# =========================

def register_play(user_id: int, track_id: int) -> int:
    if not redis_client:
        return 0

    key = f"plays:{user_id}:{track_id}"
    count = redis_client.incr(key)
    return count

def get_user_stats(user_id: int):
    if not redis_client:
        return []

    keys = redis_client.keys(f"plays:{user_id}:*")
    data = []

    for k in keys:
        count = int(redis_client.get(k))
        track_id = k.split(":")[-1]
        data.append((track_id, count))

    data.sort(key=lambda x: x[1], reverse=True)
    return data[:10]

# =========================
# DEEZER
# =========================

def deezer_search_sync(query: str):
    cache = get_cache(query)
    if cache:
        return cache

    try:
        r = session.get(
            "https://api.deezer.com/search",
            params={"q": query},
            timeout=6
        )
        data = r.json().get("data", [])
        set_cache(query, data)
        return data
    except:
        return []

async def deezer_search(query: str):
    return await asyncio.to_thread(deezer_search_sync, query)

# =========================
# START
# =========================

async def start(update: Update, context: ContextTypes.DEFAULT_TYPE):
    await update.message.reply_text("🔍Envie uma música ou use @tigraofm nome")

# =========================
# SEARCH CHAT
# =========================

async def search_music(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = update.message.text.strip()
    tracks = await deezer_search(query)

    if not tracks:
        await update.message.reply_text("Nada encontrado.")
        return

    context.chat_data["tracks"] = tracks

    keyboard = []
    for i, t in enumerate(tracks[:5]):
        keyboard.append([
            InlineKeyboardButton(
                f"{sanitize(t['title'])} — {sanitize(t['artist']['name'])}",
                callback_data=str(i)
            )
        ])

    await update.message.reply_text(
        "Escolha:",
        reply_markup=InlineKeyboardMarkup(keyboard)
    )

# =========================
# CLICK CHAT
# =========================

async def click(update: Update, context: ContextTypes.DEFAULT_TYPE):
    cb = update.callback_query
    await cb.answer()

    tracks = context.chat_data.get("tracks")

    if not tracks:
        await cb.answer("Refaça.", show_alert=True)
        return

    t = tracks[int(cb.data)]

    count = register_play(cb.from_user.id, t["id"])

    caption = (
        f"🎹 {esc(cb.from_user.first_name)} está ouvindo...\n"
        f"🎧 <b>{esc(t['title'])}</b>\n"
        f"🎤 <i>{esc(t['artist']['name'])}</i>\n"
        f"<i>🔁 {count} Plays </i>"
    )

    await cb.message.reply_photo(
        photo=t["album"]["cover_big"],
        caption=caption,
        parse_mode=ParseMode.HTML
    )

# =========================
# INLINE
# =========================

async def inline_query(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = (update.inline_query.query or "").strip()

    if not query:
        return

    tracks = await deezer_search(query)
    results = []

    for i, t in enumerate(tracks[:10]):
        try:
            title = sanitize(t["title"])
            artist = sanitize(t["artist"]["name"])
            cover_big = t["album"]["cover_big"]
            cover_small = t["album"]["cover_small"]

            if not cover_big:
                continue

            user = update.inline_query.from_user
            count = register_play(user.id, t["id"])

            caption = (
                f"🎹 {esc(user.first_name)} está ouvindo...\n"
                f"🎧 <b>{esc(title)}</b>\n"
                f"🎤 <i>{esc(artist)}</i>\n"
                f"<i>🔁 {count} Plays </i>"
            )

            results.append(
                InlineQueryResultPhoto(
                    id=f"{i}_{t['id']}_{int(time.time())}",
                    photo_url=cover_big,
                    thumbnail_url=cover_small or cover_big,
                    caption=caption,
                    parse_mode=ParseMode.HTML,
                    title=f"{title} — {artist}",
                    description="Enviar música"
                )
            )

        except Exception as e:
            logger.error(e)

    await update.inline_query.answer(
        results,
        cache_time=2,
        is_personal=True
    )

# =========================
# STATS
# =========================

async def stats(update: Update, context: ContextTypes.DEFAULT_TYPE):
    user_id = update.message.from_user.id
    data = get_user_stats(user_id)

    if not data:
        await update.message.reply_text("Nenhuma música ainda.")
        return

    text = "📊 Suas mais ouvidas:\n\n"

    for i, (track_id, count) in enumerate(data, 1):
        text += f"{i}. ID {track_id} — {count} plays\n"

    await update.message.reply_text(text)

# =========================
# ERROR
# =========================

async def error_handler(update: object, context: ContextTypes.DEFAULT_TYPE):
    logger.error("Erro:", exc_info=context.error)

# =========================
# MAIN
# =========================

def main():
    if not TOKEN:
        raise RuntimeError("TELEGRAM_TOKEN não definido")

    app = Application.builder().token(TOKEN).build()

    app.add_handler(CommandHandler("start", start))
    app.add_handler(CommandHandler("stats", stats))
    app.add_handler(MessageHandler(filters.TEXT & ~filters.COMMAND, search_music))
    app.add_handler(CallbackQueryHandler(click))
    app.add_handler(InlineQueryHandler(inline_query))
    app.add_error_handler(error_handler)

    logger.info("BOT ONLINE 🚀")
    app.run_polling()

if __name__ == "__main__":
    main()
