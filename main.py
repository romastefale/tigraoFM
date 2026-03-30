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

# =========================
# CONFIGURAÇÕES BÁSICAS
# =========================
logging.basicConfig(
    format="%(asctime)s - %(name)s - %(levelname)s - %(message)s", 
    level=logging.INFO
)
logger = logging.getLogger(__name__)

TOKEN = os.getenv("TELEGRAM_TOKEN")
WEBHOOK_URL = os.getenv("WEBHOOK_URL", "").rstrip("/") 
PORT = int(os.getenv("PORT", 8443))

session = requests.Session()
_executor = ThreadPoolExecutor(max_workers=4)

cache = {}
CACHE_MAX_SIZE = 300

# =========================
# FUNÇÕES DE UTILIDADE
# =========================
def escape_html(text):
    return html.escape(str(text)) if text else ""

def sanitize_text(text):
    return text or "Desconhecido"

def evict_cache():
    if len(cache) >= CACHE_MAX_SIZE:
        for k in list(cache.keys())[:50]:
            del cache[k]

def build_caption(title, artist, album, user_name):
    return (
        f"🎧 {user_name} está ouvindo...\n\n"
        f"🎵 <b>{title}</b> - <i>{album}</i> — <i>{artist}</i>"
    )

# =========================
# INTEGRAÇÃO DEEZER
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
# MODO INLINE (@seu_bot query)
# =========================
async def inline_query(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = update.inline_query.query
    if not query:
        return

    tracks = await search_deezer(query)
    user_name = escape_html(update.inline_query.from_user.first_name or "Alguém")
    results = []

    for i, track in enumerate(tracks[:10]):
        try:
            title = escape_html(sanitize_text(track.get("title")))
            artist = escape_html(sanitize_text(track.get("artist", {}).get("name")))
            album = escape_html(sanitize_text(track.get("album", {}).get("title")))
            cover = track.get("album", {}).get("cover_big", "")
            track_id = str(track.get("id", i))

            caption_text = build_caption(title, artist, album, user_name)

            results.append(
                InlineQueryResultPhoto(
                    id=track_id,
                    photo_url=cover,
                    thumbnail_url=cover,
                    title=f"{title} — {artist}",
                    description=album,
                    caption=caption_text,
                    parse_mode="HTML"
                )
            )
        except Exception as e:
            logger.warning(f"Erro item inline: {e}")
            continue

    await update.inline_query.answer(results, cache_time=5)

# =========================
# MODO CHAT (Mensagem Direta)
# =========================
async def search_music(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not update.message or not update.message.text:
        return

    query = update.message.text
    context.user_data["query"] = query
    context.user_data["offset"] = 0

    await send_results(update.message, context, is_edit=False)

async def send_results(message, context, is_edit=False):
    query = context.user_data.get("query")
    offset = context.user_data.get("offset", 0)

    if not query:
        text = "Sessão expirada. Por favor, digite o nome da música novamente."
        if is_edit:
            await message.edit_text(text)
        else:
            await message.reply_text(text)
        return

    tracks = await search_deezer(query, offset)

    if not tracks:
        text = "Nenhum resultado encontrado."
        if is_edit:
            await message.edit_text(text)
        else:
            await message.reply_text(text)
        return

    context.user_data["tracks"] = tracks

    keyboard = []
    for i, track in enumerate(tracks[:10]):
        title = sanitize_text(track.get("title"))
        artist = sanitize_text(track.get("artist", {}).get("name"))
        keyboard.append([InlineKeyboardButton(f"{title} — {artist}", callback_data=f"track_{i}")])

    if len(tracks) == 10:
        keyboard.append([InlineKeyboardButton("Mais", callback_data="more")])

    reply_markup = InlineKeyboardMarkup(keyboard)
    text = "🔎 Escolha uma música:"

    if is_edit:
        await message.edit_text(text, reply_markup=reply_markup)
    else:
        await message.reply_text(text, reply_markup=reply_markup)

# =========================
# CALLBACKS (Botões)
# =========================
async def more_results(update: Update, context: ContextTypes.DEFAULT_TYPE):
    cb = update.callback_query
    await cb.answer()

    context.user_data["offset"] = context.user_data.get("offset", 0) + 10
    await send_results(cb.message, context, is_edit=True)

async def select_track(update: Update, context: ContextTypes.DEFAULT_TYPE):
    cb = update.callback_query
    
    try:
        index = int(cb.data.split("_")[1])
    except ValueError:
        await cb.answer("Erro no botão.", show_alert=True)
        return

    tracks = context.user_data.get("tracks")

    if not tracks or index >= len(tracks):
        await cb.answer("Busca expirada. Digite o nome da música novamente.", show_alert=True)
        return

    track = tracks[index]

    try:
        title = escape_html(sanitize_text(track.get("title")))
        artist = escape_html(sanitize_text(track.get("artist", {}).get("name")))
        album = escape_html(sanitize_text(track.get("album", {}).get("title")))
        cover = track.get("album", {}).get("cover_big", "")
        user_name = escape_html(cb.from_user.first_name)

        caption_text = build_caption(title, artist, album, user_name)

        await cb.answer()
        await cb.message.reply_photo(
            photo=cover,
            caption=caption_text,
            parse_mode="HTML"
        )
    except Exception as e:
        logger.error(f"Erro ao enviar música: {e}")
        await cb.answer("Falha ao enviar a música.", show_alert=True)

# =========================
# MAIN / INICIALIZAÇÃO
# =========================
def main():
    if not TOKEN:
        logger.error("TELEGRAM_TOKEN não configurado!")
        return

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
        logger.error("WEBHOOK_URL não configurada. Fechando...")

if __name__ == "__main__":
    main()
