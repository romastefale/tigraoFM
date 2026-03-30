import os
import re
import html
import time
import json
import logging
import asyncio
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

session = requests.Session()

# =========================
# LOGGING
# =========================

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s | %(levelname)s | %(name)s | %(message)s"
)
logger = logging.getLogger("bot")

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
        data = r.json()
        translated = "".join(part[0] for part in data[0])
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
# HELPERS DE LAYOUT
# =========================

def build_caption(title: Any, artist: Any, plays: int, user_first_name: Optional[str] = None) -> str:
    header = ""
    if user_first_name:
        header = f"🎹 {esc(user_first_name)} está ouvindo...\n"

    return (
        f"{header}"
        f"🎧 <b>{esc(title)}</b>\n"
        f"🎤 <i>{esc(artist)}</i>\n"
        f"<i>🔁 {plays} Plays </i>"
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

    meta = build_track_meta(track)
    try:
        redis_client.hset(f"trackmeta:{track_id}", mapping=meta)
    except Exception as e:
        logger.warning("Falha ao salvar trackmeta %s: %s", track_id, e)

def get_play_count(user_id: int, track_id: int) -> int:
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

async def fetch_track_meta(track_id: str) -> Dict[str, str]:
    if redis_client:
        try:
            meta = redis_client.hgetall(f"trackmeta:{track_id}")
            if meta and meta.get("title"):
                return meta
        except Exception:
            pass

    track = await deezer_track(track_id)
    if track and track.get("id"):
        remember_track(track)
        if redis_client:
            try:
                meta = redis_client.hgetall(f"trackmeta:{track_id}")
                if meta and meta.get("title"):
                    return meta
            except Exception:
                pass

    return {
        "title": f"Track {track_id}",
        "artist": "Unknown",
        "cover_big": "",
        "cover_small": "",
    }

# =========================
# DEEZER
# =========================

async def deezer_search(query: str):
    query = (query or "").strip()
    if not query:
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

async def deezer_track(track_id: str):
    try:
        r = await asyncio.to_thread(
            session.get,
            f"https://api.deezer.com/track/{track_id}",
            timeout=6,
        )
        r.raise_for_status()
        return r.json()
    except Exception as e:
        logger.warning("Erro Deezer track %s: %s", track_id, e)
        return None

# =========================
# START / HELP
# =========================

async def start(update: Update, context: ContextTypes.DEFAULT_TYPE):
    text = (
        f"🎶 <b>{BOT_DISPLAY_NAME}</b>\n"
        f"🎧 Digite o nome de uma música ou use <code>{BOT_USERNAME} nome</code>\n\n"
        f"📌 Comandos:\n"
        f"/stats — suas músicas mais ouvidas\n"
        f"/top — ranking global"
    )
    await update.message.reply_text(text, parse_mode=ParseMode.HTML)

# =========================
# BUSCA NORMAL
# =========================

async def search_music(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = (update.message.text or "").strip()
    if not query:
        await update.message.reply_text("🎤 Digite o nome de uma música.")
        return

    tracks = await deezer_search(query)

    if not tracks:
        await update.message.reply_text("🔎 Nada encontrado.")
        return

    context.chat_data["tracks"] = {}
    keyboard = []

    for t in tracks[:5]:
        try:
            track_id = str(t["id"])
            context.chat_data["tracks"][track_id] = t
            remember_track(t)

            title = sanitize(t.get("title"))
            artist = sanitize((t.get("artist") or {}).get("name"))

            keyboard.append([
                InlineKeyboardButton(
                    f"🎵 {title} — {artist}",
                    callback_data=f"play:{track_id}"
                )
            ])
        except Exception as e:
            logger.warning("Erro montando botão: %s", e)

    await update.message.reply_text(
        "🎧 Escolha:",
        reply_markup=InlineKeyboardMarkup(keyboard)
    )

# =========================
# CLICK DO CHAT
# =========================

async def click(update: Update, context: ContextTypes.DEFAULT_TYPE):
    cb = update.callback_query
    await cb.answer()

    try:
        track_id = cb.data.split(":", 1)[1]
    except Exception:
        await cb.answer("⚠️ Ação inválida.", show_alert=True)
        return

    tracks = context.chat_data.get("tracks") or {}
    t = tracks.get(track_id)

    if not t:
        await cb.answer("🔄 Refaça a busca.", show_alert=True)
        return

    count = register_play(cb.from_user.id, t)

    caption = build_caption(
        title=t.get("title"),
        artist=(t.get("artist") or {}).get("name"),
        plays=count,
        user_first_name=cb.from_user.first_name,
    )

    await cb.message.reply_photo(
        photo=(t.get("album") or {}).get("cover_big"),
        caption=caption,
        parse_mode=ParseMode.HTML
    )

# =========================
# INLINE MODE
# =========================

async def inline_query(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = (update.inline_query.query or "").strip()
    if not query:
        return

    user = update.inline_query.from_user
    tracks = await deezer_search(query)

    results = []

    for t in tracks[:10]:
        try:
            track_id = str(t["id"])
            title = sanitize(t.get("title"))
            artist = sanitize((t.get("artist") or {}).get("name"))
            cover_big = (t.get("album") or {}).get("cover_big")
            cover_small = (t.get("album") or {}).get("cover_small")

            if not cover_big:
                continue

            remember_track(t)
            current_count = get_play_count(user.id, int(track_id))

            results.append(
                InlineQueryResultPhoto(
                    id=f"track:{track_id}",
                    photo_url=cover_big,
                    thumbnail_url=cover_small or cover_big,
                    caption=build_caption(
                        title=title,
                        artist=artist,
                        plays=current_count,
                        user_first_name=user.first_name,
                    ),
                    parse_mode=ParseMode.HTML,
                    title=f"{title} — {artist}",
                    description=f"{artist} • {current_count} Plays"
                )
            )
        except Exception as e:
            logger.warning("Erro inline item: %s", e)

    await update.inline_query.answer(
        results,
        cache_time=2,
        is_personal=True
    )

# =========================
# CHOSEN INLINE RESULT
# =========================

async def chosen_inline(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not redis_client:
        return

    try:
        result_id = update.chosen_inline_result.result_id or ""
        if not result_id.startswith("track:"):
            return

        track_id = result_id.split(":", 1)[1]
        user_id = update.chosen_inline_result.from_user.id

        track = await deezer_track(track_id)
        if not track:
            meta = await fetch_track_meta(track_id)
            track = {
                "id": track_id,
                "title": meta.get("title", "Unknown"),
                "artist": {"name": meta.get("artist", "Unknown")},
                "album": {
                    "cover_big": meta.get("cover_big", ""),
                    "cover_small": meta.get("cover_small", ""),
                }
            }

        register_play(user_id, track)
    except Exception as e:
        logger.warning("Erro no chosen_inline: %s", e)

# =========================
# STATS / TOP
# =========================

async def stats(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not redis_client:
        await update.message.reply_text("⚠️ Redis indisponível.")
        return

    user_id = update.effective_user.id
    entries: List[Tuple[str, float]] = redis_client.zrevrange(
        f"top:user:{user_id}",
        0,
        9,
        withscores=True
    )

    if not entries:
        await update.message.reply_text("🎧 Você ainda não ouviu músicas.")
        return

    metas = await asyncio.gather(*(fetch_track_meta(track_id) for track_id, _ in entries))

    lines = [
        f"📊 <b>Suas mais ouvidas no {BOT_DISPLAY_NAME}</b>",
        ""
    ]

    for i, ((track_id, score), meta) in enumerate(zip(entries, metas), 1):
        title = sanitize(meta.get("title") or f"Track {track_id}")
        artist = sanitize(meta.get("artist") or "Unknown")

        lines.append(f"{i}. 🎧 <b>{esc(title)}</b>")
        lines.append(f"   🎤 <i>{esc(artist)}</i>")
        lines.append(f"   <i>🔁 {int(score)} Plays </i>")
        lines.append("")

    await update.message.reply_text(
        "\n".join(lines).strip(),
        parse_mode=ParseMode.HTML,
        disable_web_page_preview=True
    )

async def top(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not redis_client:
        await update.message.reply_text("⚠️ Redis indisponível.")
        return

    entries: List[Tuple[str, float]] = redis_client.zrevrange(
        "top:tracks",
        0,
        9,
        withscores=True
    )

    if not entries:
        await update.message.reply_text("🎧 Ainda não há plays registrados.")
        return

    metas = await asyncio.gather(*(fetch_track_meta(track_id) for track_id, _ in entries))

    lines = [
        f"📈 <b>Top global do {BOT_DISPLAY_NAME}</b>",
        ""
    ]

    for i, ((track_id, score), meta) in enumerate(zip(entries, metas), 1):
        title = sanitize(meta.get("title") or f"Track {track_id}")
        artist = sanitize(meta.get("artist") or "Unknown")

        lines.append(f"{i}. 🎧 <b>{esc(title)}</b>")
        lines.append(f"   🎤 <i>{esc(artist)}</i>")
        lines.append(f"   <i>🔁 {int(score)} Plays </i>")
        lines.append("")

    await update.message.reply_text(
        "\n".join(lines).strip(),
        parse_mode=ParseMode.HTML,
        disable_web_page_preview=True
    )

# =========================
# ERROS
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
    app.add_handler(CommandHandler("stats", stats))
    app.add_handler(CommandHandler("top", top))
    app.add_handler(MessageHandler(filters.TEXT & ~filters.COMMAND, search_music))
    app.add_handler(CallbackQueryHandler(click))
    app.add_handler(InlineQueryHandler(inline_query))
    app.add_handler(ChosenInlineResultHandler(chosen_inline))
    app.add_error_handler(error_handler)

    logger.info("BOT ONLINE 🚀")
    app.run_polling()

if __name__ == "__main__":
    main()