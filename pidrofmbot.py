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
# Alteração 1: Removendo os ":" do TOKEN para o Telegram não rejeitar o Webhook
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
    except telegram.
