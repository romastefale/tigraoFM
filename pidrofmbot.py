import os
from telegram import Update
from telegram.ext import ApplicationBuilder, CommandHandler, ContextTypes

TOKEN = os.getenv("TOKEN")

app = ApplicationBuilder().token(TOKEN).build()


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
                title=f"{title}",
                description=f"{artist} • Tap to confirm",
                caption=f"_{user_name} is listening to..._\n\n♫ Playing: {title}\n★ Artist: {artist}",
                parse_mode="Markdown"
            )
        )

    await update.inline_query.answer(results)


def main():
    app = Application.builder().token(TOKEN).build()

    app.add_handler(InlineQueryHandler(inline_query))

    print("Bot rodando...")

    app.run_polling()


if __name__ == "__main__":
    main()
