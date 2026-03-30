# =========================
# IMPORTS
# =========================
import os
import re
import html
import json
import logging
import asyncio
from datetime import datetime
from pathlib import Path
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
# LOGGING
# =========================

LOG_BUFFER: List[str] = []
LOG_LIMIT = 300

class BufferHandler(logging.Handler):
    def emit(self, record):
        try:
            msg = self.format(record)
            LOG_BUFFER.append(msg)
            if len(LOG_BUFFER) > LOG_LIMIT:
                del LOG_BUFFER[0:len(LOG_BUFFER) - LOG_LIMIT]
        except Exception:
            pass

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s | %(levelname)s | %(name)s | %(message)s"
)

logger = logging.getLogger("bot")
buffer_handler = BufferHandler()
buffer_handler.setFormatter(logging.Formatter("%(asctime)s | %(levelname)s | %(name)s | %(message)s"))
logger.addHandler(buffer_handler)

# =========================
# REDIS INIT
# =========================

redis_client: Optional[redis.Redis] = None

def connect_redis() -> None:
    global redis_client
    if not REDIS_URL:
        logger.warning("REDIS_URL não definida")
        redis_client = None
        return

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

connect_redis()

# =========================
# SANITIZE / TRADUÇÃO
# =========================

FORBIDDEN = re.compile(
    r'[\u0600-\u06FF\u0400-\u04FF\u4E00-\u9FFF\u0900-\u097F\u0980-\u09FF]'
)

def translate_sync(text: str) -> str:
    try:
        r = session.get(
            "https://translate.googleapis.com/translate_a/single",
            params={
                "client": "gtx",
                "sl": "auto",
                "tl": "en",
                "dt": "t",
                "q": text,
            },
            timeout=4,
        )
        r.raise_for_status()
        data = r.json()
        translated = "".join(part[0] for part in data[0] if part and part[0])
        return f"[en: {translated}]"
    except Exception:
        return "Unknown"

def sanitize(text: Any) -> str:
    if text is None:
        return "Unknown"
    text = str(text).strip()
    if not text:
        return "Unknown"
    if FORBIDDEN.search(text):
        return translate_sync(text)
    return text

def esc(text: Any) -> str:
    return html.escape(sanitize(text))

# =========================
# HELPERS
# =========================

def build_caption(title: Any, artist: Any, plays: int, user_first_name: Optional[str] = None) -> str:
    header = ""
    if user_first_name:
        header = f"🎹 {esc(user_first_name)} está ouvindo...\n"

    return (
        f"{header}"
        f"🎧 <b>{esc(title)}</b>\n"
        f"🎤 <i>{esc(artist)}</i>\n"
        f"<i>🔁 {plays} Plays</i>"
    )

def build_track_meta(track: Dict[str, Any]) -> Dict[str, str]:
    return {
        "title": str(track.get("title") or "Unknown"),
        "artist": str((track.get("artist") or {}).get("name") or "Unknown"),
        "cover_big": str((track.get("album") or {}).get("cover_big") or ""),
        "cover_small": str((track.get("album") or {}).get("cover_small") or ""),
    }

def remember_track(track: Dict[str, Any]) -> None:
    if not redis_client or not track:
        return
    track_id = str(track.get("id") or "")
    if not track_id:
        return
    try:
        redis_client.hset(f"trackmeta:{track_id}", mapping=build_track_meta(track))
    except Exception as e:
        logger.warning("Falha ao salvar trackmeta %s: %s", track_id, e)

def get_play_count(user_id: int, track_id: Any) -> int:
    if not redis_client:
        return 0
    try:
        value = redis_client.get(f"plays:{user_id}:{track_id}")
        return int(value) if value else 0
    except Exception:
        return 0

def register_play(user_id: int, track: Dict[str, Any]) -> int:
    if not redis_client or not track:
        return 0

    track_id = str(track.get("id") or "")
    if not track_id:
        return 0

    remember_track(track)

    try:
        pipe = redis_client.pipeline()
        pipe.incr(f"plays:{user_id}:{track_id}")
        pipe.zincrby(f"top:user:{user_id}", 1, track_id)
        pipe.zincrby("top:tracks", 1, track_id)
        result = pipe.execute()
        return int(result[0] or 0)
    except Exception as e:
        logger.warning("Falha ao registrar play: %s", e)
        return 0

# =========================
# DEEZER
# =========================

async def deezer_search(query: str):
    if not query.strip():
        return []
    try:
        r = await asyncio.to_thread(
            session.get,
            "https://api.deezer.com/search",
            params={"q": query},
            timeout=6,
        )
        r.raise_for_status()
        return r.json().get("data", [])
    except Exception as e:
        logger.warning("Erro Deezer search: %s", e)
        return []

# =========================
# /PLAY (NOVO)
# =========================

async def play(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = " ".join(context.args).strip()

    if not query:
        await update.message.reply_text("🎤 Use: /play nome da música")
        return

    tracks = await deezer_search(query)

    if not tracks:
        await update.message.reply_text("🔎 Nada encontrado.")
        return

    keyboard = []

    for t in tracks[:5]:
        track_id = str(t["id"])
        remember_track(t)

        title = sanitize(t.get("title"))
        artist = sanitize((t.get("artist") or {}).get("name"))

        keyboard.append([
            InlineKeyboardButton(
                f"🎵 {title} — {artist}",
                callback_data=f"play:{track_id}"
            )
        ])

    await update.message.reply_text(
        "🎧 Escolha:",
        reply_markup=InlineKeyboardMarkup(keyboard)
    )

# =========================
# MAIN
# =========================

def main():
    app = Application.builder().token(TOKEN).build()

    app.add_handler(CommandHandler("start", start))
    app.add_handler(CommandHandler("play", play))  # NOVO
    app.add_handler(CommandHandler("charts", stats))
    app.add_handler(CommandHandler("top", top))
    app.add_handler(CommandHandler("log", log_cmd))

    # REMOVIDO: MessageHandler de texto

    app.add_handler(CallbackQueryHandler(click))
    app.add_handler(InlineQueryHandler(inline_query))
    app.add_handler(ChosenInlineResultHandler(chosen_inline))

    logger.info("BOT ONLINE 🚀")
    app.run_polling()

if __name__ == "__main__":
    main()