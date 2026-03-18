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
WEBHOOK_SECRET = os.getenv("WEBHOOK_SECRET", TOKEN[:20] if TOKEN else None)

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
# BUSCA NA API (sync, runs in thread)
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

            if r.status_code >= 500:
                logger.warning(f"Deezer server error {r.status_code} (attempt {attempt + 1}/3)")
                if attempt < 2:
                    time.sleep(1)
                continue

            if r.status_code != 200:
                logger.warning(f"Deezer returned status {r.status_code}")
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

        except requests.exceptions.Timeout:
            logger.warning(f"Deezer timeout (attempt {attempt + 1}/3)")
            if attempt < 2:
                time.sleep(1)
            continue

        except Exception as e:
            logger.error(f"Deezer search error: {e}")
            return []

    return []


async def search_deezer(query, index=0):
    loop = asyncio.get_event_loop()
    return await loop.run_in_executor(_executor, _search_deezer_sync, query, index)


# =========================
# GLOBAL ERROR HANDLER
# =========================

async def error_handler(update: object, context: ContextTypes.DEFAULT_TYPE):
    error = context.error

    if isinstance(error, telegram.error.RetryAfter):
        logger.warning(f"Rate limited by Telegram. Retrying after {error.retry_after}s")
        await asyncio.sleep(error.retry_after)

    elif isinstance(error, telegram.error.TimedOut):
        logger.warning("Telegram request timed out — will retry automatically")

    elif isinstance(error, telegram.error.NetworkError):
        logger.warning(f"Network error: {error} — will retry automatically")

    elif isinstance(error, telegram.error.Forbidden):
        logger.warning(f"Forbidden: bot was blocked or chat deleted — {error}")

    elif isinstance(error, telegram.error.BadRequest):
        logger.error(f"Bad request sent to Telegram: {error}")

    else:
        logger.error(f"Unhandled error: {error}", exc_info=context.error)


# =========================
# SAFE SEND HELPERS
# =========================

async def safe_reply_text(target, text, **kwargs):
    try:
        await target.reply_text(text, **kwargs)
    except telegram.error.RetryAfter as e:
        await asyncio.sleep(e.retry_after)
        try:
            await target.reply_text(text, **kwargs)
        except Exception as e2:
            logger.error(f"safe_reply_text retry failed: {e2}")
    except telegram.error.Forbidden:
        logger.warning("Could not send message: user blocked the bot")
    except Exception as e:
        logger.error(f"safe_reply_text error: {e}")


async def safe_reply_photo(target, photo, caption, parse_mode):
    try:
        await target.reply_photo(photo=photo, caption=caption, parse_mode=parse_mode)
    except telegram.error.RetryAfter as e:
        await asyncio.sleep(e.retry_after)
        try:
            await target.reply_photo(photo=photo, caption=caption, parse_mode=parse_mode)
        except Exception as e2:
            logger.error(f"safe_reply_photo retry failed: {e2}")
    except telegram.error.Forbidden:
        logger.warning("Could not send photo: user blocked the bot")
    except Exception as e:
        logger.error(f"safe_reply_photo error: {e}")


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
            cover = track["album"]["cover_big"]

            results.append(

                InlineQueryResultPhoto(
                    id=str(i),
                    photo_url=cover,
                    thumbnail_url=cover,

                    title=f"{track['title']} — {track['artist']['name']}",
                    description="Tap to share",

                    caption=f"_{user_name} is listening to..._\n\n*{title}*\n_{artist}_\n\n♫ Now Playing",
                    parse_mode="Markdown"
                )
            )

        except Exception as e:
            logger.warning(f"Skipping inline track {i}: {e}")
            continue

    try:
        await update.inline_query.answer(results, cache_time=5)
    except telegram.error.BadRequest as e:
        logger.error(f"Inline answer bad request: {e}")
    except Exception as e:
        logger.error(f"Inline answer error: {e}")


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
        await safe_reply_text(update.message, "Please send a search term first.")
        return

    tracks = await search_deezer(query, offset)

    if not tracks:
        await safe_reply_text(update.message, "No results found.")
        return

    context.user_data["tracks"] = tracks

    keyboard = []

    for i, track in enumerate(tracks[:10]):
        try:
            title = track["title"]
            artist = track["artist"]["name"]

            keyboard.append([
                InlineKeyboardButton(
                    f"{title} — {artist}",
                    callback_data=f"track_{i}"
                )
            ])
        except Exception as e:
            logger.warning(f"Skipping keyboard track {i}: {e}")
            continue

    keyboard.append([
        InlineKeyboardButton(
            "Load more",
            callback_data="more"
        )
    ])

    await safe_reply_text(
        update.message,
        "♪ Select the song:",
        reply_markup=InlineKeyboardMarkup(keyboard)
    )


# =========================
# MAIS RESULTADOS
# =========================

async def more_results(update: Update, context: ContextTypes.DEFAULT_TYPE):

    cb_query = update.callback_query

    try:
        await cb_query.answer()
    except Exception:
        pass

    search_query = context.user_data.get("query")
    if not search_query:
        await safe_reply_text(cb_query.message, "Session expired. Please search again.")
        return

    context.user_data["offset"] = context.user_data.get("offset", 0) + 10

    tracks = await search_deezer(
        search_query,
        context.user_data["offset"]
    )

    if not tracks:
        await safe_reply_text(cb_query.message, "No more results.")
        return

    context.user_data["tracks"] = tracks

    keyboard = []

    for i, track in enumerate(tracks[:10]):
        try:
            title = track["title"]
            artist = track["artist"]["name"]

            keyboard.append([
                InlineKeyboardButton(
                    f"{title} — {artist}",
                    callback_data=f"track_{i}"
                )
            ])
        except Exception as e:
            logger.warning(f"Skipping more_results track {i}: {e}")
            continue

    keyboard.append([
        InlineKeyboardButton(
            "Load more",
            callback_data="more"
        )
    ])

    await safe_reply_text(
        cb_query.message,
        "More results:",
        reply_markup=InlineKeyboardMarkup(keyboard)
    )


# =========================
# ESCOLHER MÚSICA
# =========================

async def select_track(update: Update, context: ContextTypes.DEFAULT_TYPE):

    cb_query = update.callback_query

    try:
        await cb_query.answer()
    except Exception:
        pass

    try:
        index = int(cb_query.data.split("_")[1])
    except (IndexError, ValueError):
        await safe_reply_text(cb_query.message, "Invalid selection. Please search again.")
        return

    tracks = context.user_data.get("tracks")

    if not tracks:
        await safe_reply_text(cb_query.message, "Session expired. Please search again.")
        return

    if index < 0 or index >= len(tracks):
        await safe_reply_text(cb_query.message, "Track no longer available. Please search again.")
        return

    track = tracks[index]
    title = None
    artist = None
    cover = None
    user_name = None

    try:
        title = escape_markdown(track["title"])
        artist = escape_markdown(track["artist"]["name"])
        cover = track["album"]["cover_big"]
        user_name = escape_markdown(cb_query.from_user.first_name)
    except (KeyError, Exception) as e:
        logger.error(f"select_track data error: {e}")
        await safe_reply_text(cb_query.message, "Could not load track. Please try again.")
        return

    await safe_reply_photo(
        cb_query.message,
        photo=cover,
        caption=f"_{user_name} is listening to..._\n\n*{title}*\n_{artist}_\n\n♫ Now Playing",
        parse_mode="Markdown"
    )


# =========================
# MAIN
# =========================

def main():

    app = (
        Application.builder()
        .token(TOKEN)
        .connect_timeout(30)
        .read_timeout(30)
        .write_timeout(30)
        .pool_timeout(30)
        .build()
    )

    app.add_error_handler(error_handler)

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
        logger.info(f"Starting in webhook mode on port {PORT}")
        app.run_webhook(
            listen="0.0.0.0",
            port=PORT,
            url_path=TOKEN,
            webhook_url=f"{WEBHOOK_URL}/{TOKEN}",
            secret_token=WEBHOOK_SECRET,
            drop_pending_updates=True,
        )
    else:
        logger.info("Starting in polling mode")
        app.run_polling(drop_pending_updates=True)


if __name__ == "__main__":
    main()
