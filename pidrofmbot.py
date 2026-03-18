import os
import re
import requests

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

TOKEN = os.getenv("TELEGRAM_TOKEN")

if not TOKEN:
    raise ValueError("Configure TELEGRAM_TOKEN nas variáveis do Render")

session = requests.Session()
cache = {}

# =========================
# RANKING INTELIGENTE
# =========================

def score_track(track, query):

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


# =========================
# BUSCA NA API
# =========================

def search_deezer(query, index=0):

    query = re.sub(r"[-_]+", " ", query)
    query = re.sub(r"\s+", " ", query).strip()

    cache_key = f"{query}_{index}"

    if cache_key in cache:
        return cache[cache_key]

    try:

        r = session.get(
            "https://api.deezer.com/search",
            params={"q": query, "index": index},
            timeout=3
        )

        if r.status_code != 200:
            return []

        tracks = r.json().get("data", [])

        tracks = sorted(
            tracks,
            key=lambda t: score_track(t, query),
            reverse=True
        )

        cache[cache_key] = tracks

        return tracks

    except:
        return []


# =========================
# INLINE MODE
# =========================

async def inline_query(update: Update, context: ContextTypes.DEFAULT_TYPE):

    query = update.inline_query.query

    if not query:
        return

    tracks = search_deezer(query)

    user = update.inline_query.from_user
    user_name = user.first_name if user else "Someone"

    results = []

    for i, track in enumerate(tracks[:10]):

        try:

            title = track["title"]
            artist = track["artist"]["name"]
            cover = track["album"]["cover_big"]

            results.append(

                InlineQueryResultPhoto(
                    id=str(i),
                    photo_url=cover,
                    thumbnail_url=cover,

                    title=f"{title} — {artist}",
                    description="Tap to share",

                    caption=f"_{user_name} is listening to..._\n\n*{title}*\n_{artist}_\n\n♫ Now Playing",
                    parse_mode="Markdown"
                )
            )

        except:
            continue

    try:
        await update.inline_query.answer(results, cache_time=5)
    except:
        pass


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

    query = context.user_data["query"]
    offset = context.user_data["offset"]

    tracks = search_deezer(query, offset)

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
        "Select the song:",
        reply_markup=InlineKeyboardMarkup(keyboard)
    )


# =========================
# MAIS RESULTADOS
# =========================

async def more_results(update: Update, context: ContextTypes.DEFAULT_TYPE):

    query = update.callback_query

    context.user_data["offset"] += 10

    await query.answer()

    tracks = search_deezer(
        context.user_data["query"],
        context.user_data["offset"]
    )

    if not tracks:
        await query.message.reply_text("No more results.")
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

    await query.message.reply_text(
        "More results:",
        reply_markup=InlineKeyboardMarkup(keyboard)
    )


# =========================
# ESCOLHER MÚSICA
# =========================

async def select_track(update: Update, context: ContextTypes.DEFAULT_TYPE):

    query = update.callback_query

    index = int(query.data.split("_")[1])

    tracks = context.user_data.get("tracks")

    if not tracks:
        return

    track = tracks[index]

    title = track["title"]
    artist = track["artist"]["name"]
    cover = track["album"]["cover_big"]

    user_name = query.from_user.first_name

    await query.message.reply_photo(
        photo=cover,
        caption=f"_{user_name} is listening to..._\n\n*{title}*\n_{artist}_\n\n♫ Now Playing",
        parse_mode="Markdown"
    )

    await query.answer()


# =========================
# MAIN
# =========================

def main():

    app = Application.builder().token(TOKEN).build()

    app.add_handler(InlineQueryHandler(inline_query))

    app.add_handler(
        MessageHandler(filters.TEXT & ~filters.COMMAND, search_music)
    )

    app.add_handler(
        CallbackQueryHandler(more_results, pattern="more")
    )

    app.add_handler(
        CallbackQueryHandler(select_track, pattern="track_")
    )

    print("Bot rodando...")

    app.run_polling()


if __name__ == "__main__":
    main()
