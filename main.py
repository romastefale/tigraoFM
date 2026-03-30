import os
import asyncio
import logging
import requests
import html
import re

from telegram import Update, InlineKeyboardMarkup, InlineKeyboardButton
from telegram.ext import (
    Application,
    MessageHandler,
    CallbackQueryHandler,
    ContextTypes,
    filters
)

logging.basicConfig(level=logging.INFO)

TOKEN = os.getenv("TELEGRAM_TOKEN")

session = requests.Session()


# =========================
# SANITIZE COM TRADUÇÃO
# =========================

def sanitize_text(text):
    if not text:
        return "Unknown"

    text = str(text)

    forbidden = re.compile(
        r'[\u0600-\u06FF\u0400-\u04FF\u4E00-\u9FFF\u0900-\u097F\u0980-\u09FF]'
    )

    if not forbidden.search(text):
        return text

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
            timeout=3
        )

        data = r.json()
        translated = "".join(x[0] for x in data[0])

        return f"[en: {translated}]"

    except:
        return "Unknown"


def esc(text):
    return html.escape(sanitize_text(text))


# =========================
# BUSCA DEEZER
# =========================

def search(query):
    r = session.get("https://api.deezer.com/search", params={"q": query})
    return r.json().get("data", [])


# =========================
# BUSCA
# =========================

async def search_music(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = update.message.text

    tracks = search(query)

    if not tracks:
        await update.message.reply_text("Nada encontrado.")
        return

    context.user_data["tracks"] = tracks

    keyboard = []
    for i, t in enumerate(tracks[:5]):
        keyboard.append([
            InlineKeyboardButton(
                f"{sanitize_text(t['title'])} — {sanitize_text(t['artist']['name'])}",
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

    tracks = context.user_data.get("tracks")

    if not tracks:
        await cb.answer("Refaça a busca.", show_alert=True)
        return

    i = int(cb.data)

    if i >= len(tracks):
        await cb.answer("Expirou.", show_alert=True)
        return

    t = tracks[i]

    caption = (
        f"<a href='tg://emoji?id=5388632425314140043'>🎧</a> "
        f"{esc(cb.from_user.first_name)} está ouvindo...<br><br>"
        f"<a href='tg://emoji?id=5463107823946717464'>🎵</a> "
        f"<b>{esc(t['title'])}</b> - <i>{esc(t['album']['title'])}</i> — <i>{esc(t['artist']['name'])}</i>"
    )

    await cb.message.reply_photo(
        photo=t["album"]["cover_big"],
        caption=caption,
        parse_mode="HTML"
    )


# =========================
# MAIN
# =========================

def main():
    app = Application.builder().token(TOKEN).build()

    app.add_handler(MessageHandler(filters.TEXT, search_music))
    app.add_handler(CallbackQueryHandler(click))

    app.run_polling()


if __name__ == "__main__":
    main()