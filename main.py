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
from telegram.constants import ParseMode
from telegram.ext import (
    Application,
    InlineQueryHandler,
    MessageHandler,
    CallbackQueryHandler,
    CommandHandler,
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

ADMIN_ID_RAW = os.getenv("ADMIN_ID")
try:
    ADMIN_ID = int(ADMIN_ID_RAW) if ADMIN_ID_RAW else None
except ValueError:
    logger.warning("Invalid ADMIN_ID value, bot will run without admin restriction.")
    ADMIN_ID = None

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


# =========================
# FUNÇÕES DE HIGIENIZAÇÃO (NOVAS REGRAS)
# =========================

def sanitize_text(text):
    """
    Verifica se há alfabetos proibidos. Tenta traduzir para o inglês.
    Se falhar, omite (remove) os caracteres proibidos.
    """
    if not text:
        return text

    text = str(text)

    # Regex cobrindo blocos Unicode do Árabe, Cirílico, Chinês, Hindi (Devanagari) e Bengali
    forbidden_pattern = re.compile(
        r'['
        r'\u0600-\u06FF\u0750-\u077F\u08A0-\u08FF\uFB50-\uFDFF\uFE70-\uFEFF'  # Árabe
        r'\u0400-\u04FF\u0500-\u052F\u2DE0-\u2DFF\uA640-\uA69F'               # Cirílico
        r'\u4E00-\u9FFF\u3400-\u4DBF'                                         # Chinês
        r'\u0900-\u097F'                                                      # Hindi (Devanagari)
        r'\u0980-\u09FF'                                                      # Bengali
        r']'
    )

    # Se não encontrar nenhum caractere proibido, retorna o texto original rapidamente
    if not forbidden_pattern.search(text):
        return text

    # 1. Tentar traduzir para o Inglês usando API gratuita do Google
    try:
        url = "https://translate.googleapis.com/translate_a/single"
        params = {
            "client": "gtx",
            "sl": "auto",
            "tl": "en",
            "dt": "t",
            "q": text
        }
        response = session.get(url, params=params, timeout=3)
        if response.status_code == 200:
            data = response.json()
            translated_text = "".join([sentence[0] for sentence in data[0]])

            # Se a tradução não contiver mais os caracteres proibidos, deu sucesso
            if not forbidden_pattern.search(translated_text):
                return translated_text
    except Exception as e:
        logger.warning(f"Falha na tradução automática, aplicando omissão: {e}")

    # 2. Se a tradução falhar ou não resolver, omitir os caracteres proibidos
    sanitized = forbidden_pattern.sub("", text)
    sanitized = re.sub(r'\s+', ' ', sanitized).strip()  # Limpa espaços duplos

    return sanitized if sanitized else "Unknown"


def escape_markdown(text):
    return re.sub(r"([_*`\[])", r"\\\1", str(text))


def evict_cache():
    if len(cache) >= CACHE_MAX_SIZE:
        oldest_keys = list(cache.keys())[:100]
        for k in oldest_keys:
            del cache[k]


def is_admin(user_id: int | None) -> bool:
    return ADMIN_ID is not None and user_id == ADMIN_ID


async def send_log_prompt(update: Update, context: ContextTypes.DEFAULT_TYPE):
    await update.effective_message.reply_text(
        "📝Qual texto de <i>Update</i> você deseja enviar?",
        parse_mode=ParseMode.HTML
    )


# =========================
# COMANDO /LOG
# =========================

async def start_log(update: Update, context: ContextTypes.DEFAULT_TYPE):
    user_id = update.effective_user.id if update.effective_user else None

    if not is_admin(user_id):
        await update.effective_message.reply_text("Sem permissão.")
        return

    context.user_data["awaiting_log"] = True
    await send_log_prompt(update, context)


async def handle_log_input(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not context.user_data.get("awaiting_log"):
        return

    user_id = update.effective_user.id if update.effective_user else None
    if not is_admin(user_id):
        return

    msg = update.effective_message
    if not msg:
        return

    try:
        # Reenvia exatamente o que foi enviado, preservando texto, formatação e anexos
        await context.bot.copy_message(
            chat_id=msg.chat_id,
            from_chat_id=msg.chat_id,
            message_id=msg.message_id
        )
    except Exception as e:
        logger.exception(f"Falha ao copiar mensagem no /log: {e}")
        await msg.reply_text("Falha ao reproduzir a mensagem.")
        context.user_data["awaiting_log"] = False
        return

    keyboard = InlineKeyboardMarkup([
        [
            InlineKeyboardButton("🆗Correto?", callback_data="log_ok"),
            InlineKeyboardButton("✏️Editar...", callback_data="log_edit")
        ]
    ])

    await msg.reply_text(
        "🆗Correto?",
        reply_markup=keyboard
    )

    # Aguarda a confirmação; se editar, o fluxo volta ao início
    context.user_data["awaiting_log"] = False


async def handle_log_callback(update: Update, context: ContextTypes.DEFAULT_TYPE):
    cb_query = update.callback_query
    if not cb_query:
        return

    user_id = cb_query.from_user.id if cb_query.from_user else None
    if not is_admin(user_id):
        await cb_query.answer("Sem permissão.", show_alert=True)
        return

    data = cb_query.data

    if data == "log_ok":
        context.user_data.pop("awaiting_log", None)
        await cb_query.answer("Concluído.")
        try:
            await cb_query.edit_message_reply_markup(reply_markup=None)
        except Exception:
            pass
        return

    if data == "log_edit":
        context.user_data["awaiting_log"] = True
        await cb_query.answer("Envie novamente o texto.")
        try:
            await cb_query.edit_message_reply_markup(reply_markup=None)
        except Exception:
            pass
        await cb_query.message.reply_text(
            "📝Qual texto de <i>Update</i> você deseja enviar?",
            parse_mode=ParseMode.HTML
        )
        return


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
    # Higieniza e depois escapa o nome do usuário
    user_name = escape_markdown(sanitize_text(user.first_name if user else "Someone"))

    results = []

    for i, track in enumerate(tracks[:10]):

        try:
            # Higieniza e escapa os dados da música
            title = escape_markdown(sanitize_text(track["title"]))
            artist = escape_markdown(sanitize_text(track["artist"]["name"]))
            album = escape_markdown(sanitize_text(track["album"]["title"]))
            cover = track["album"]["cover_big"]

            results.append(

                InlineQueryResultPhoto(
                    id=str(i),
                    photo_url=cover,
                    thumbnail_url=cover,

                    title=f"{sanitize_text(track['title'])} — {sanitize_text(track['artist']['name'])}",
                    description="♪ Share this song",

                    caption=(
                        f"♫ {user_name} is listening to...\n\n"
                        f"♬ *{title}* - _{album}_ — _{artist}_"
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
    if context.user_data.get("awaiting_log"):
        return

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

        # Higieniza os botões
        title = sanitize_text(track["title"])
        artist = sanitize_text(track["artist"]["name"])

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

        # Higieniza os botões do "Load more"
        title = sanitize_text(track["title"])
        artist = sanitize_text(track["artist"]["name"])

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

    # Higieniza e escapa os dados finais da música selecionada
    title = escape_markdown(sanitize_text(track["title"]))
    artist = escape_markdown(sanitize_text(track["artist"]["name"]))
    album = escape_markdown(sanitize_text(track["album"]["title"]))
    cover = track["album"]["cover_big"]

    # Higieniza o nome do usuário
    user_name = escape_markdown(sanitize_text(cb_query.from_user.first_name))

    await cb_query.message.reply_photo(
        photo=cover,
        caption=(
            f"♫ {user_name} is listening to...\n\n"
            f"♬ *{title}* - _{album} — {artist}_"
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

    app.add_handler(CommandHandler("log", start_log))
    app.add_handler(InlineQueryHandler(inline_query))
    app.add_handler(CallbackQueryHandler(handle_log_callback, pattern=r"^log_(ok|edit)$"))

    app.add_handler(
        MessageHandler(filters.TEXT & ~filters.COMMAND, search_music)
    )

    app.add_handler(
        MessageHandler(filters.ALL & ~filters.COMMAND, handle_log_input),
        group=1
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
