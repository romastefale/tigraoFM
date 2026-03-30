import os
import re
import html
import time
import logging
import asyncio
from typing import Any, Dict, Optional

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
    ChosenInlineResultHandler,
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

# =========================
# REDIS INIT
# =========================

redis_client: Optional[redis.Redis] = None

if REDIS_URL:
    try:
        redis_client = redis.Redis.from_url(
            REDIS_URL,
            decode_responses=True,
            socket_timeout=5,
            socket_connect_timeout=5,
            health_check_interval=30,
        )
        redis_client.ping()
        logger.info("Redis conectado ✅")
    except Exception as e:
        logger.warning("Redis OFF: %s", e)
        redis_client = None
else:
    logger.warning("REDIS_URL não definida")

# =========================
# CACHE
# =========================

CACHE: Dict[str, Dict[str, Any]] = {}
CACHE_TTL = 60


def get_cache(key: str):
    data = CACHE.get(key)
    if not data:
        return None

    if time.time() - data["time"] > CACHE_TTL:
        del CACHE[key]
        return None

    return data["value"]


def set_cache(key: str, value):
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


def esc(text: Any) -> str:
    return html.escape(sanitize(text))

# =========================
# REDIS COUNTER
# =========================

def register_play(user_id: int, track_id: int) -> int:
    if not redis_client:
        return 0

    try:
        return int(redis_client.incr(f"plays:{user_id}:{track_id}"))
    except Exception as e:
        logger.error("Erro Redis INCR: %s", e)
        return 0


def get_user_stats(user_id: int):
    if not redis_client:
        return []

    data = []

    try:
        for k in redis_client.scan_iter(match=f"plays:{user_id}:*"):
            try:
                count = int(redis_client.get(k) or 0)
                track_id = k.split(":")[-1]
                data.append((track_id, count))
            except:
                continue
    except Exception as e:
        logger.error("Erro Redis SCAN: %s", e)
        return []

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
            timeout=8
        )
        r.raise_for_status()
        data = r.json().get("data", [])
        set_cache(query, data)
        return data
    except Exception as e:
        logger.error("Erro Deezer: %s", e)
        return []


async def deezer_search(query: str):
    return await asyncio.to_thread(deezer_search_sync, query)

# =========================
# START
# =========================

async def start(update: Update, context: ContextTypes.DEFAULT_TYPE):
    await update.message.reply_text("Envie uma música ou use @seubot nome")

# =========================
# CHAT SEARCH
# =========================

async def search_music(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = update.message.text.strip()
    tracks = await deezer_search(query)

    if not tracks:
        await update.message.reply_text("Nada encontrado.")
        return

    track_map = {}
    keyboard = []

    for t in tracks[:5]:
        track_id = str(t["id"])
        track_map[track_id] = t

        keyboard.append([
            InlineKeyboardButton(
                f"{sanitize(t['title'])} — {sanitize(t['artist']['name'])}",
                callback_data=f"play:{track_id}"
            )
        ])

    context.chat_data["track_map"] = track_map

    await update.message.reply_text(
        "Escolha:",
        reply_markup=InlineKeyboardMarkup(keyboard)
    )

# =========================
# CLICK
# =========================

async def click(update: Update, context: ContextTypes.DEFAULT_TYPE):
    cb = update.callback_query
    await cb.answer()

    track_id = cb.data.split(":")[1]
    t = context.chat_data.get("track_map", {}).get(track_id)

    if not t:
        await cb.answer("Refaça a busca", show_alert=True)
        return

    count = register_play(cb.from_user.id, int(track_id))

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
        result_id = f"{t['id']}"

        results.append(
            InlineQueryResultPhoto(
                id=result_id,
                photo_url=t["album"]["cover_big"],
                thumbnail_url=t["album"]["cover_small"],
                caption=(
                    f"🎹 {esc(update.inline_query.from_user.first_name)} está ouvindo...\n"
                    f"🎧 <b>{esc(t['title'])}</b>\n"
                    f"🎤 <i>{esc(t['artist']['name'])}</i>\n"
                    f"<i>🔁 0 Plays </i>"
                ),
                parse_mode=ParseMode.HTML
            )
        )

    await update.inline_query.answer(results, cache_time=2, is_personal=True)

# =========================
# INLINE SELECT
# =========================

async def chosen_inline(update: Update, context: ContextTypes.DEFAULT_TYPE):
    data = update.chosen_inline_result

    track_id = int(data.result_id)
    user_id = data.from_user.id

    count = register_play(user_id, track_id)

    logger.info(f"INLINE PLAY | user={user_id} track={track_id} total={count}")

# =========================
# STATS
# =========================

async def stats(update: Update, context: ContextTypes.DEFAULT_TYPE):
    data = get_user_stats(update.message.from_user.id)

    if not data:
        await update.message.reply_text("Nenhuma música ainda.")
        return

    text = "📊 Suas mais ouvidas:\n\n"

    for i, (track_id, count) in enumerate(data, 1):
        text += f"{i}. ID {track_id} — {count} plays\n"

    await update.message.reply_text(text)

# =========================
# MAIN
# =========================

def main():
    app = Application.builder().token(TOKEN).build()

    app.add_handler(CommandHandler("start", start))
    app.add_handler(CommandHandler("stats", stats))
    app.add_handler(MessageHandler(filters.TEXT & ~filters.COMMAND, search_music))
    app.add_handler(CallbackQueryHandler(click))
    app.add_handler(InlineQueryHandler(inline_query))
    app.add_handler(ChosenInlineResultHandler(chosen_inline))

    logger.info("BOT ONLINE 🚀")
    app.run_polling()

if __name__ == "__main__":
    main()
