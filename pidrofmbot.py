import os
import re
import time
import asyncio
import logging
import requests
import telegram.error

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

logging.basicConfig(
    format="%(asctime)s - %(name)s - %(levelname)s - %(message)s",
    level=logging.INFO
)
logger = logging.getLogger(__name__)

TOKEN = os.getenv("TELEGRAM_TOKEN")
WEBHOOK_URL = os.getenv("WEBHOOK_URL")
WEBHOOK_SECRET = os.getenv("WEBHOOK_SECRET", TOKEN.replace(":", "")[:20] if TOKEN else None)

try:
    PORT = int(os.getenv("PORT", 8443))
except ValueError:
    logger.warning("Invalid PORT value, defaulting to 8443")
    PORT = 8443

if not TOKEN:
    raise ValueError("Configure TELEGRAM_TOKEN nas variáveis do Render")

session = requests.Session()
cache = {}
CACHE_MAX_SIZE = 500
_executor = ThreadPoolExecutor(max_workers=4)


def escape_markdown(text):
    return re.sub(r"([_*`\[])", r"\\\1", str(text))


def evict_cache():
    if len(cache) >= CACHE_MAX_SIZE:
        oldest_keys = list(cache.keys())[:100]
        for k in oldest_keys:
            del cache[k]


# =========================
# RANKING INTELIGENTE
# =========================

def score_track(track, query):
    try:
        title = track["title"].lower()
        artist = track["artist"]["name"].lower()
        q = query.lower()

        score = 0

        if q in f"{title} {artist}":
            score += 100

        if q in title:
            score += 60

        if q in artist:
            score += 40

        if title.startswith(q):
            score += 30

        return score
    except (KeyError, AttributeError):
        return 0


# =========================
# BUSCA NA API
# =========================

def _search_deezer_sync(query, index=0):

    query = re.sub(r"[-_]+", " ", query)
    query = re.sub(r"\s+", " ", query).strip()

    cache_key = f"{query}_{index}"

    if cache_key in cache:
        return cache[cache_key]

    for attempt in range(3):
        try:
            r = session.get(
                "https://api.deezer.com/search",
                params={"q": query, "index": index},
                timeout=5
            )

            if r.status_code != 200:
                return []

            tracks = r.json().get("data", [])

            tracks = sorted(
                tracks,
                key=lambda t: score_track(t, query),
                reverse=True
            )

            evict_cache()
            cache[cache_key] = tracks

            return tracks

        except Exception:
            time.sleep(1)

    return []


async def search_deezer(query, index=0):
    loop = asyncio.get_event_loop()
    return await loop.run_in_executor(_executor, _search_deezer_sync, query, index)


# =========================
# INLINE MODE
# =========================

async def inline_query(update: Update, context: ContextTypes.DEFAULT_TYPE):

    query = update.inline_query.query

    if not query:
        return

    tracks = await search_deezer(query)

    user = update.inline_query.from_user
    user_name = escape_markdown(user.first_name if user else "Someone")

    results = []

    for i, track in enumerate(tracks[:10]):

        try:

            title = escape_markdown(track["title"])
            artist = escape_markdown(track["artist"]["name"])
            album = escape_markdown(track["album"]["title"])
            cover = track["album"]["cover_big"]

            results.append(

                InlineQueryResultPhoto(
                    id=str(i),
                    photo_url=cover,
                    thumbnail_url=cover,

                    title=f"{track['title']} — {track['artist']['name']}",
                    description="♪ Share this song",

                    caption=(
                        f"♬ {user_name} is listening to...\n\n"
                        f"♫ *{title}* - _{album}_ — _{artist}_"
                    ),
                    parse_mode="Markdown"
                )
            )

        except Exception:
            continue

    await update.inline_query.answer(results, cache_time=5)


# =========================
# BUSCA NO CHAT
# =========================

async def search_music(update: Update, context: ContextTypes.DEFAULT_TYPE):

    query = update.message.text

    context.user_data["query"] = query
    context.user_data["offset"] = 0

    await send_results(update, context)


# =========================
# ENVIAR RESULTADOS
# =========================

async def send_results(update, context):

    query = context.user_data.get("query")
    offset = context.user_data.get("offset", 0)

    if not query:
        return

    tracks = await search_deezer(query, offset)

    if not tracks:
        await update.message.reply_text("No results found.")
        return

    context.user_data["tracks"] = tracks

    keyboard = []

    for i, track in enumerate(tracks[:10]):

        title = track["title"]
        artist = track["artist"]["name"]

        keyboard.append([
            InlineKeyboardButton(
                f"{title} — {artist}",
                callback_data=f"track_{i}"
            )
        ])

    keyboard.append([
        InlineKeyboardButton(
            "Load more",
            callback_data="more"
        )
    ])

    await update.message.reply_text(
        "♪ Search song...",
        reply_markup=InlineKeyboardMarkup(keyboard)
    )


# =========================
# MAIS RESULTADOS
# =========================

async def more_results(update: Update, context: ContextTypes.DEFAULT_TYPE):

    cb_query = update.callback_query
    await cb_query.answer()

    search_query = context.user_data.get("query")

    context.user_data["offset"] = context.user_data.get("offset", 0) + 10

    tracks = await search_deezer(
        search_query,
        context.user_data["offset"]
    )

    context.user_data["tracks"] = tracks

    keyboard = []

    for i, track in enumerate(tracks[:10]):

        title = track["title"]
        artist = track["artist"]["name"]

        keyboard.append([
            InlineKeyboardButton(
                f"{title} — {artist}",
                callback_data=f"track_{i}"
            )
        ])

    keyboard.append([
        InlineKeyboardButton(
            "Load more",
            callback_data="more"
        )
    ])

    await cb_query.message.reply_text(
        "♪ Search song...",
        reply_markup=InlineKeyboardMarkup(keyboard)
    )


# =========================
# ESCOLHER MÚSICA
# =========================

async def select_track(update: Update, context: ContextTypes.DEFAULT_TYPE):

    cb_query = update.callback_query
    await cb_query.answer()

    index = int(cb_query.data.split("_")[1])
    tracks = context.user_data.get("tracks")

    track = tracks[index]

    title = escape_markdown(track["title"])
    artist = escape_markdown(track["artist"]["name"])
    album = escape_markdown(track["album"]["title"])
    cover = track["album"]["cover_big"]

    user_name = escape_markdown(cb_query.from_user.first_name)

    await cb_query.message.reply_photo(
        photo=cover,
        caption=(
            f"♬ {user_name} is listening to...\n\n"
            f"♫ *{title}* ({album}) — _{artist}_"
        ),
        parse_mode="Markdown"
    )


# =========================
# MAIN
# =========================

def main():

    app = (
        Application.builder()
        .token(TOKEN)
        .build()
    )

    app.add_handler(InlineQueryHandler(inline_query))

    app.add_handler(
        MessageHandler(filters.TEXT & ~filters.COMMAND, search_music)
    )

    app.add_handler(
        CallbackQueryHandler(more_results, pattern="^more$")
    )

    app.add_handler(
        CallbackQueryHandler(select_track, pattern=r"^track_\d+$")
    )

    if WEBHOOK_URL:
        app.run_webhook(
            listen="0.0.0.0",
            port=PORT,
            url_path=TOKEN,
            webhook_url=f"{WEBHOOK_URL}/{TOKEN}",
            secret_token=WEBHOOK_SECRET,
            drop_pending_updates=True,
        )
    else:
        app.run_polling(drop_pending_updates=True)


if __name__ == "__main__":
    main()
