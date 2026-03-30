import os
import re
import html
import time
import json
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
ADMIN_ID = os.getenv("ADMIN_ID")

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
        )
        redis_client.ping()
        logger.info("Redis conectado ✅")
    except Exception as e:
        logger.warning("Redis OFF: %s", e)
        redis_client = None
else:
    logger.warning("REDIS_URL não definida")

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
    return int(redis_client.incr(f"plays:{user_id}:{track_id}"))

def get_user_stats(user_id: int):
    if not redis_client:
        return []

    data = []
    for k in redis_client.scan_iter(match=f"plays:{user_id}:*"):
        count = int(redis_client.get(k) or 0)
        track_id = k.split(":")[-1]
        data.append((track_id, count))

    data.sort(key=lambda x: x[1], reverse=True)
    return data[:10]

# =========================
# DEEZER
# =========================

async def deezer_search(query: str):
    try:
        r = await asyncio.to_thread(
            session.get,
            "https://api.deezer.com/search",
            params={"q": query},
        )
        return r.json().get("data", [])
    except:
        return []

async def deezer_track(track_id: str):
    try:
        r = await asyncio.to_thread(
            session.get,
            f"https://api.deezer.com/track/{track_id}"
        )
        return r.json()
    except:
        return None

# =========================
# BACKUP DIÁRIO
# =========================

async def backup_redis(context: ContextTypes.DEFAULT_TYPE):
    if not redis_client or not ADMIN_ID:
        return

    try:
        data = {}
        for key in redis_client.scan_iter("plays:*"):
            data[key] = int(redis_client.get(key) or 0)

        filename = "backup.json"

        with open(filename, "w") as f:
            json.dump(data, f)

        await context.bot.send_document(
            chat_id=int(ADMIN_ID),
            document=open(filename, "rb"),
            caption="📦 Backup diário Redis"
        )

        os.remove(filename)

    except Exception as e:
        await context.bot.send_message(
            chat_id=int(ADMIN_ID),
            text=f"🚨 ERRO BACKUP:\n{e}"
        )

# =========================
# EXPORT STATS
# =========================

async def export_stats(context: ContextTypes.DEFAULT_TYPE):
    if not redis_client or not ADMIN_ID:
        return

    try:
        stats = {}

        for key in redis_client.scan_iter("plays:*"):
            _, user_id, track_id = key.split(":")
            count = int(redis_client.get(key) or 0)

            stats.setdefault(user_id, {})
            stats[user_id][track_id] = count

        filename = "stats.json"

        with open(filename, "w") as f:
            json.dump(stats, f, indent=2)

        await context.bot.send_document(
            chat_id=int(ADMIN_ID),
            document=open(filename, "rb"),
            caption="📊 Stats diário"
        )

        os.remove(filename)

    except Exception as e:
        await context.bot.send_message(
            chat_id=int(ADMIN_ID),
            text=f"🚨 ERRO STATS:\n{e}"
        )

# =========================
# MONITOR REDIS (1h)
# =========================

async def monitor_redis(context: ContextTypes.DEFAULT_TYPE):
    try:
        if not redis_client:
            raise Exception("Redis não conectado")
        redis_client.ping()
    except Exception as e:
        if ADMIN_ID:
            await context.bot.send_message(
                chat_id=int(ADMIN_ID),
                text=f"🚨 Redis OFFLINE!\n{e}"
            )

# =========================
# BOT
# =========================

async def start(update: Update, context: ContextTypes.DEFAULT_TYPE):
    await update.message.reply_text("Envie música ou use @bot")

async def search_music(update: Update, context: ContextTypes.DEFAULT_TYPE):
    tracks = await deezer_search(update.message.text)

    keyboard = []
    context.chat_data["tracks"] = {}

    for t in tracks[:5]:
        track_id = str(t["id"])
        context.chat_data["tracks"][track_id] = t

        keyboard.append([
            InlineKeyboardButton(
                f"{t['title']} — {t['artist']['name']}",
                callback_data=f"play:{track_id}"
            )
        ])

    await update.message.reply_text(
        "Escolha:",
        reply_markup=InlineKeyboardMarkup(keyboard)
    )

async def click(update: Update, context: ContextTypes.DEFAULT_TYPE):
    cb = update.callback_query
    await cb.answer()

    track_id = cb.data.split(":")[1]
    t = context.chat_data["tracks"][track_id]

    count = register_play(cb.from_user.id, int(track_id))

    await cb.message.reply_photo(
        photo=t["album"]["cover_big"],
        caption=f"{t['title']} — {t['artist']['name']}\n🔁 {count} Plays"
    )

async def inline_query(update: Update, context: ContextTypes.DEFAULT_TYPE):
    tracks = await deezer_search(update.inline_query.query)

    results = []
    for t in tracks[:10]:
        results.append(
            InlineQueryResultPhoto(
                id=str(t["id"]),
                photo_url=t["album"]["cover_big"],
                thumbnail_url=t["album"]["cover_small"],
                caption=f"{t['title']} — {t['artist']['name']}\n🔁 0 Plays"
            )
        )

    await update.inline_query.answer(results)

async def chosen_inline(update: Update, context: ContextTypes.DEFAULT_TYPE):
    register_play(
        update.chosen_inline_result.from_user.id,
        int(update.chosen_inline_result.result_id)
    )

async def stats(update: Update, context: ContextTypes.DEFAULT_TYPE):
    user = update.effective_user
    data = get_user_stats(user.id)

    text = "📊 Suas mais ouvidas:\n\n"

    for i, (track_id, count) in enumerate(data, 1):
        track = await deezer_track(track_id)

        if track:
            text += f"{i}. 🎧 {track['title']} — {track['artist']['name']}\n   🔁 {count} Plays\n\n"

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

    job_queue = app.job_queue

    # 🔥 AGENDAMENTOS
    job_queue.run_repeating(monitor_redis, interval=3600, first=60)
    job_queue.run_repeating(backup_redis, interval=86400, first=120)
    job_queue.run_repeating(export_stats, interval=86400, first=180)

    logger.info("BOT ONLINE 🚀")
    app.run_polling()

if __name__ == "__main__":
    main()
