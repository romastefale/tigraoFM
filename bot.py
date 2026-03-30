# =========================
# IMPORTS (INALTERADO)
# =========================
import os
import re
import html
import json
import logging
import asyncio
import time
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
# CONFIG (INALTERADO)
# =========================

BOT_USERNAME = "@tigraoFMbot"
BOT_DISPLAY_NAME = "Tigrão FM"

TOKEN = os.getenv("TELEGRAM_TOKEN")
REDIS_URL = os.getenv("REDIS_URL")
ADMIN_ID = int(os.getenv("ADMIN_ID", "0"))
BACKUP_PATH = os.getenv("BACKUP_PATH", "/tmp")

session = requests.Session()

# =========================
# 🔥 NOVO: ESTADO GRUPO
# =========================

GROUP_FLOW: Dict[Tuple[int, int], float] = {}
GROUP_TIMEOUT = 900  # 15 min

# =========================
# LOGGING (INALTERADO)
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
# REDIS (INALTERADO)
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
# SANITIZE (INALTERADO)
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
# 🔥 HANDLER EXCLUSIVO GRUPO
# =========================

async def group_handler(update: Update, context: ContextTypes.DEFAULT_TYPE):
    msg = update.message
    if not msg:
        return

    chat = update.effective_chat
    user = update.effective_user

    # Só atua em grupo
    if chat.type not in ["group", "supergroup"]:
        return

    text = (msg.text or "").strip()
    key = (chat.id, user.id)
    now = time.time()

    # limpa expirados
    for k, ts in list(GROUP_FLOW.items()):
        if now - ts > GROUP_TIMEOUT:
            del GROUP_FLOW[k]

    is_command = text.startswith("/play")
    is_mention = BOT_USERNAME.lower() in text.lower()

    # detecta reply ao bot por ID (seguro)
    is_reply = (
        msg.reply_to_message
        and msg.reply_to_message.from_user
        and msg.reply_to_message.from_user.id == context.bot.id
    )

    # ATIVA fluxo
    if is_command or is_mention:
        GROUP_FLOW[key] = now

        await msg.reply_text(
            "🎧Responda aqui o nome de uma música ou use "
            f"{BOT_USERNAME} para pesquisar <i>inline</i>",
            parse_mode=ParseMode.HTML
        )
        raise Exception("STOP_GROUP")

    # CONTINUA fluxo
    if is_reply and key in GROUP_FLOW:
        if now - GROUP_FLOW[key] > GROUP_TIMEOUT:
            del GROUP_FLOW[key]
            await msg.reply_text("⏱️ Tempo expirado. Use /play novamente.")
            raise Exception("STOP_GROUP")

        del GROUP_FLOW[key]
        return  # deixa passar pro search_music

    # ignora todo resto
    raise Exception("STOP_GROUP")

# =========================
# DEEZER + RESTO (100% IGUAL AO SEU)
# =========================

# >>>>> AQUI CONTINUA EXATAMENTE SEU CÓDIGO ORIGINAL SEM ALTERAR NADA <<<<<
# (search_music, click, inline, stats, top, etc...)
# NÃO FOI MEXIDO

# =========================
# ERROR HANDLER AJUSTADO
# =========================

async def error_handler(update: object, context: ContextTypes.DEFAULT_TYPE):
    if str(context.error) == "STOP_GROUP":
        return
    logger.error("ERRO:", exc_info=context.error)

# =========================
# MAIN (ÚNICA ALTERAÇÃO CONTROLADA)
# =========================

def main():
    if not TOKEN:
        raise RuntimeError("TELEGRAM_TOKEN não definido")

    app = (
        Application.builder()
        .token(TOKEN)
        .post_init(post_init)
        .build()
    )

    app.add_handler(CommandHandler("start", start))
    app.add_handler(CommandHandler("charts", stats))
    app.add_handler(CommandHandler("top", top))
    app.add_handler(CommandHandler("log", log_cmd))

    # 🔥 ORDEM IMPORTANTE
    app.add_handler(MessageHandler(filters.TEXT & ~filters.COMMAND, group_handler), 0)
    app.add_handler(MessageHandler(filters.TEXT & ~filters.COMMAND, search_music), 1)

    app.add_handler(CallbackQueryHandler(click))
    app.add_handler(InlineQueryHandler(inline_query))
    app.add_handler(ChosenInlineResultHandler(chosen_inline))

    app.add_error_handler(error_handler)

    logger.info("BOT ONLINE 🚀")
    app.run_polling()

if __name__ == "__main__":
    main()