import os
import re
import asyncio
import logging
import requests
import html

from concurrent.futures import ThreadPoolExecutor
from telegram import (
    Update,
    InlineQueryResultPhoto,
    InlineKeyboardMarkup,
    InlineKeyboardButton
)
from telegram.ext import (
    Application,
    InlineQueryHandler,
    MessageHandler,
    CallbackQueryHandler,
    ContextTypes,
    filters
)

logging.basicConfig(level=logging.INFO)
logger = logging.getLogger(__name__)

TOKEN = os.getenv("TELEGRAM_TOKEN")
WEBHOOK_URL = os.getenv("WEBHOOK_URL")
PORT = int(os.getenv("PORT", 8443))

session = requests.Session()
_executor = ThreadPoolExecutor(max_workers=4)

cache = {}
CACHE_MAX_SIZE = 300


# =========================
# UTIL
# =========================

def escape_html(text):
    return html.escape(str(text)) if text else ""


def sanitize_text(text):
    return text or "Unknown"


def evict_cache():
    if len(cache) >= CACHE_MAX_SIZE:
        for k in list(cache.keys())[:50]:
            del cache[k]


# =========================
# DEEZER
# =========================

def _search_deezer_sync(query, index=0):
    query = re.sub(r"\s+", " ", query).strip()
    key = f"{query}_{index}"

    if key in cache:
        return cache[key]

    try:
        r = session.get(
            "https://api.deezer.com/search",
            params={"q": query, "index": index},
            timeout=5
        )

        if r.status_code != 200:
            return []

        data = r.json().get("data", [])

        evict_cache()
        cache[key] = data

        return data

    except Exception as e:
        logger.error(f"Erro Deezer: {e}")
        return []


async def search_deezer(query, index=0):
    loop = asyncio.get_running_loop()
    return await loop.run_in_executor(_executor, _search_deezer_sync, query, index)


# =========================
# INLINE
# =========================

async def inline_query(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = update.inline_query.query
    if not query:
        return

    tracks = await search_deezer(query)

    user = update.inline_query.from_user
    user_name = escape_html(user.first_name if user else "Alguém")

    results = []

    for i, track in enumerate(tracks[:10]):
        try:
            title = escape_html(sanitize_text(track["title"]))
            artist = escape_html(sanitize_text(track["artist"]["name"]))
            album = escape_html(sanitize_text(track["album"]["title"]))
            cover = track["album"]["cover_big"]

            results.append(
                InlineQueryResultPhoto(
                    id=str(i),
                    photo_url=cover,
                    thumbnail_url=cover,
                    title=f"{track['title']} — {track['artist']['name']}",
                    description="Compartilhar música",
                    caption=(
                        f"<a href='tg://emoji?id=5388632425314140043'>🎧</a> {user_name} está ouvindo...<br><br>"
                        f"<a href='tg://emoji?id=5463107823946717464'>🎵</a> <b>{title}</b> - <i>{album}</i> — <i>{artist}</i>"
                    ),
                    parse_mode="HTML"
                )
            )
        except Exception as e:
            logger.warning(f"Erro inline item: {e}")
            continue

    await update.inline_query.answer(results, cache_time=5)


# =========================
# BUSCA CHAT
# =========================

async def search_music(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = update.message.text

    context.user_data["query"] = query
    context.user_data["offset"] = 0

    await send_results(update.message, context)


async def send_results(message, context):
    query = context.user_data.get("query")
    offset = context.user_data.get("offset", 0)

    tracks = await search_deezer(query, offset)

    if not tracks:
        await message.reply_text("Nenhum resultado.")
        return

    context.user_data["tracks"] = tracks

    keyboard = []
    for i, track in enumerate(tracks[:10]):
        title = sanitize_text(track["title"])
        artist = sanitize_text(track["artist"]["name"])

        keyboard.append([
            InlineKeyboardButton(
                f"{title} — {artist}",
                callback_data=f"track_{i}"
            )
        ])

    keyboard.append([
        InlineKeyboardButton("Mais", callback_data="more")
    ])

    await message.reply_text(
        "🔎 Escolha uma música:",
        reply_markup=InlineKeyboardMarkup(keyboard)
    )


# =========================
# MAIS
# =========================

async def more_results(update: Update, context: ContextTypes.DEFAULT_TYPE):
    cb = update.callback_query
    await cb.answer()

    context.user_data["offset"] = context.user_data.get("offset", 0) + 10

    await send_results(cb.message, context)


# =========================
# SELECT TRACK (CORRIGIDO)
# =========================

async def select_track(update: Update, context: ContextTypes.DEFAULT_TYPE):
    cb = update.callback_query
    await cb.answer()

    try:
        index = int(cb.data.split("_")[1])
    except:
        await cb.answer("Erro.", show_alert=True)
        return

    tracks = context.user_data.get("tracks")

    # 🔥 CORREÇÃO PRINCIPAL
    if not tracks or index >= len(tracks):
        await cb.answer("Resultado expirado. Busque novamente.", show_alert=True)
        return

    track = tracks[index]

    try:
        title = escape_html(sanitize_text(track["title"]))
        artist = escape_html(sanitize_text(track["artist"]["name"]))
        album = escape_html(sanitize_text(track["album"]["title"]))
        cover = track["album"]["cover_big"]

        user_name = escape_html(cb.from_user.first_name)

        await cb.message.reply_photo(
            photo=cover,
            caption=(
                f"<a href='tg://emoji?id=5388632425314140043'>🎧</a> {user_name} está ouvindo...<br><br>"
                f"<a href='tg://emoji?id=5463107823946717464'>🎵</a> <b>{title}</b> - <i>{album}</i> — <i>{artist}</i>"
            ),
            parse_mode="HTML"
        )

    except Exception as e:
        logger.error(f"Erro ao enviar música: {e}")
        await cb.answer("Erro ao enviar música.", show_alert=True)


# =========================
# MAIN
# =========================

def main():
    app = Application.builder().token(TOKEN).build()

    app.add_handler(InlineQueryHandler(inline_query))
    app.add_handler(MessageHandler(filters.TEXT & ~filters.COMMAND, search_music))
    app.add_handler(CallbackQueryHandler(more_results, pattern="^more$"))
    app.add_handler(CallbackQueryHandler(select_track, pattern=r"^track_\d+$"))

    if WEBHOOK_URL:
        app.run_webhook(
            listen="0.0.0.0",
            port=PORT,
            url_path=TOKEN,
            webhook_url=f"{WEBHOOK_URL}/{TOKEN}",
            drop_pending_updates=True,
        )
    else:
        app.run_polling(drop_pending_updates=True)


if __name__ == "__main__":
    main()