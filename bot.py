import os
import re
import html
import time
import logging
import asyncio
from typing import Any, Dict, List

import requests
from telegram import (
    Update,
    InlineKeyboardMarkup,
    InlineKeyboardButton,
    InlineQueryResultArticle,
    InputTextMessageContent
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
# LOGGING AVANÇADO (Railway)
# =========================

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s | %(levelname)s | %(name)s | %(message)s"
)
logger = logging.getLogger("bot")

TOKEN = os.getenv("TELEGRAM_TOKEN")

session = requests.Session()

# =========================
# CACHE (TTL)
# =========================

CACHE: Dict[str, Dict] = {}
CACHE_TTL = 60  # segundos

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
# SANITIZE + TRADUÇÃO
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
                "q": text
            },
            timeout=4
        )
        data = r.json()
        return "[en: " + "".join(x[0] for x in data[0]) + "]"
    except:
        return "Unknown"

def sanitize(text: Any) -> str:
    if not text:
        return "Unknown"

    text = str(text)

    if not FORBIDDEN.search(text):
        return text

    return translate_sync(text)

def esc(text):
    return html.escape(sanitize(text))

# =========================
# DEEZER SEARCH + CACHE
# =========================

def deezer_search_sync(query: str):
    cache = get_cache(query)
    if cache:
        logger.info(f"CACHE HIT: {query}")
        return cache

    try:
        r = session.get(
            "https://api.deezer.com/search",
            params={"q": query},
            timeout=6
        )
        data = r.json().get("data", [])
        set_cache(query, data)
        logger.info(f"API HIT: {query}")
        return data
    except:
        return []

async def deezer_search(query: str):
    return await asyncio.to_thread(deezer_search_sync, query)

# =========================
# START
# =========================

async def start(update: Update, context: ContextTypes.DEFAULT_TYPE):
    await update.message.reply_text("Envie uma música ou use @seubot nome")

# =========================
# BUSCA NORMAL
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
# CLICK
# =========================

async def click(update: Update, context: ContextTypes.DEFAULT_TYPE):
    cb = update.callback_query
    await cb.answer()

    tracks = context.chat_data.get("tracks")

    if not tracks:
        await cb.answer("Refaça.", show_alert=True)
        return

    i = int(cb.data)

    t = tracks[i]

    caption = (
        f"🎹 {esc(cb.from_user.first_name)} está ouvindo...\n\n"
        f"🎧 <b>{esc(t['title'])}</b>\n"
        f"🎤 <i>{esc(t['artist']['name'])}</i>"
    )

    await cb.message.reply_photo(
        photo=t["album"]["cover_big"],
        caption=caption,
        parse_mode=ParseMode.HTML
    )

# =========================
# INLINE MODE
# =========================

async def inline_query(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = update.inline_query.query.strip()

    if not query:
        return

    tracks = await deezer_search(query)

    results = []

    for i, t in enumerate(tracks[:10]):
        title = sanitize(t["title"])
        artist = sanitize(t["artist"]["name"])

        text = f"🎧 {title} - {artist}"

        results.append(
            InlineQueryResultArticle(
                id=str(i),
                title=f"{title} — {artist}",
                input_message_content=InputTextMessageContent(text),
                description="Enviar música",
                thumb_url=t["album"]["cover_small"]
            )
        )

    await update.inline_query.answer(results, cache_time=1)

# =========================
# ERROR HANDLER
# =========================

async def error_handler(update: object, context: ContextTypes.DEFAULT_TYPE):
    logger.error("ERRO:", exc_info=context.error)

# =========================
# MAIN
# =========================

def main():
    if not TOKEN:
        raise RuntimeError("TELEGRAM_TOKEN não definido")

    app = Application.builder().token(TOKEN).build()

    app.add_handler(CommandHandler("start", start))
    app.add_handler(MessageHandler(filters.TEXT & ~filters.COMMAND, search_music))
    app.add_handler(CallbackQueryHandler(click))
    app.add_handler(InlineQueryHandler(inline_query))
    app.add_error_handler(error_handler)

    logger.info("BOT ONLINE 🚀")

    app.run_polling()

if __name__ == "__main__":
    main()
