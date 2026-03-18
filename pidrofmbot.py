import os
import requests
from telegram import Update, InlineQueryResultPhoto
from telegram.ext import Application, InlineQueryHandler, ContextTypes

# pega o token da variável de ambiente
TOKEN = os.getenv("TELEGRAM_TOKEN")

if not TOKEN:
    raise ValueError("TELEGRAM_TOKEN environment variable not set")


async def inline_query(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = update.inline_query.query

    if not query:
        return

    r = requests.get(f"https://api.deezer.com/search?q={query}").json()

    results = []
    user_name = update.inline_query.from_user.first_name

    for i, track in enumerate(r["data"][:5]):
        title = track["title"]
        artist = track["artist"]["name"]
        cover = track["album"]["cover_big"]

        results.append(
            InlineQueryResultPhoto(
                id=str(i),
                photo_url=cover,
                thumbnail_url=cover,
                title=title,
                description=f"{artist} • Tap to confirm",
                caption=f"_{user_name} is listening to..._\n\n♫ Playing: {title}\n★ Artist: {artist}",
                parse_mode="Markdown"
            )
        )

    await update.inline_query.answer(results)


def main():
    app = Application.builder().token(TOKEN).build()

    app.add_handler(InlineQueryHandler(inline_query))

    print("Bot running...")

    app.run_polling()


if __name__ == "__main__":
    main()