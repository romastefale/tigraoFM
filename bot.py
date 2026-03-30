import os
import re
import html
import json
import logging
import asyncio
from datetime import datetime
from typing import Any, Dict, Optional, List, Tuple

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
# CONFIG
# =========================

BOT_USERNAME = "@tigraoFMbot"
BOT_DISPLAY_NAME = "Tigrão FM"

TOKEN = os.getenv("TELEGRAM_TOKEN")
REDIS_URL = os.getenv("REDIS_URL")
ADMIN_ID = int(os.getenv("ADMIN_ID", "0"))
BACKUP_PATH = os.getenv("BACKUP_PATH", "/tmp")

session = requests.Session()

# =========================
# LOG BUFFER (/log)
# =========================

LOG_BUFFER: List[str] = []
LOG_LIMIT = 200

class BufferHandler(logging.Handler):
    def emit(self, record):
        msg = self.format(record)
        LOG_BUFFER.append(msg)
        if len(LOG_BUFFER) > LOG_LIMIT:
            LOG_BUFFER.pop(0)

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s | %(levelname)s | %(message)s"
)

logger = logging.getLogger("bot")
logger.addHandler(BufferHandler())

# =========================
# REDIS
# =========================

redis_client: Optional[redis.Redis] = None

def connect_redis():
    global redis_client
    if not REDIS_URL:
        return

    try:
        redis_client = redis.Redis.from_url(REDIS_URL, decode_responses=True)
        redis_client.ping()
        logger.info("Redis conectado ✅")
    except Exception as e:
        logger.error(f"Redis erro: {e}")
        redis_client = None

connect_redis()

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
    if FORBIDDEN.search(text):
        return "[en: translated]"
    return text

def esc(text: Any) -> str:
    return html.escape(sanitize(text))

# =========================
# DEEZER
# =========================

async def deezer_search(query: str):
    try:
        r = await asyncio.to_thread(
            session.get,
            "https://api.deezer.com/search",
            params={"q": query},
            timeout=5
        )
        return r.json().get("data", [])
    except:
        return []

async def deezer_track(track_id: str):
    try:
        r = await asyncio.to_thread(
            session.get,
            f"https://api.deezer.com/track/{track_id}",
            timeout=5
        )
        return r.json()
    except:
        return None

# =========================
# REDIS PLAY
# =========================

def register_play(user_id: int, track_id: str):
    if not redis_client:
        return 0
    pipe = redis_client.pipeline()
    pipe.incr(f"plays:{user_id}:{track_id}")
    pipe.zincrby(f"top:user:{user_id}", 1, track_id)
    pipe.zincrby("top:tracks", 1, track_id)
    result = pipe.execute()
    return int(result[0])

# =========================
# BOT
# =========================

async def start(update: Update, context: ContextTypes.DEFAULT_TYPE):
    await update.message.reply_text("🎧 Envie o nome de uma música")

async def search_music(update: Update, context: ContextTypes.DEFAULT_TYPE):
    tracks = await deezer_search(update.message.text)

    keyboard = []
    for t in tracks[:5]:
        keyboard.append([
            InlineKeyboardButton(
                t["title"],
                callback_data=f"play:{t['id']}"
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
    track = await deezer_track(track_id)

    plays = register_play(cb.from_user.id, track_id)

    await cb.message.reply_photo(
        photo=track["album"]["cover_big"],
        caption=f"{track['title']} - {track['artist']['name']}\n🔁 {plays} plays"
    )

# =========================
# /LOG
# =========================

async def log_cmd(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if update.effective_user.id != ADMIN_ID:
        return

    logs = "\n".join(LOG_BUFFER[-30:])
    await update.message.reply_text(f"<pre>{logs}</pre>", parse_mode="HTML")

# =========================
# BACKUP REDIS
# =========================

async def backup_task():
    while True:
        await asyncio.sleep(86400)  # 24h

        if not redis_client:
            continue

        try:
            data = {}
            for key in redis_client.scan_iter("*"):
                data[key] = redis_client.get(key)

            path = f"{BACKUP_PATH}/backup_{datetime.now().date()}.json"
            with open(path, "w") as f:
                json.dump(data, f)

            logger.info(f"Backup salvo em {path}")

        except Exception as e:
            logger.error(f"Erro backup: {e}")

# =========================
# EXPORT STATS
# =========================

async def export_stats_task():
    while True:
        await asyncio.sleep(86400)

        if not redis_client:
            continue

        try:
            top = redis_client.zrevrange("top:tracks", 0, 50, withscores=True)

            path = f"{BACKUP_PATH}/stats_{datetime.now().date()}.json"
            with open(path, "w") as f:
                json.dump(top, f)

            logger.info("Stats exportado")

        except Exception as e:
            logger.error(f"Erro export stats: {e}")

# =========================
# MONITOR REDIS
# =========================

async def monitor_redis():
    while True:
        await asyncio.sleep(3600)

        try:
            if redis_client:
                redis_client.ping()
            else:
                connect_redis()
        except Exception as e:
            logger.error(f"Redis caiu: {e}")
            connect_redis()

# =========================
# MAIN
# =========================

def main():
    app = Application.builder().token(TOKEN).build()

    app.add_handler(CommandHandler("start", start))
    app.add_handler(CommandHandler("log", log_cmd))
    app.add_handler(MessageHandler(filters.TEXT, search_music))
    app.add_handler(CallbackQueryHandler(click))

    loop = asyncio.get_event_loop()
    loop.create_task(backup_task())
    loop.create_task(export_stats_task())
    loop.create_task(monitor_redis())

    logger.info("BOT ONLINE 🚀")
    app.run_polling()

if __name__ == "__main__":
    main()
