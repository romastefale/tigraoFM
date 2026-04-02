# =========================
# MAIN
# =========================

def main():
    if not TOKEN:
        raise RuntimeError("TELEGRAM_TOKEN não definido")

    # A correção: post_init e post_shutdown devem estar no builder
    app = (
        Application.builder()
        .token(TOKEN)
        .post_init(post_init)      # Adicionado aqui
        .post_shutdown(post_shutdown)  # Adicionado aqui
        .build()
    )

    app.add_handler(CommandHandler("start", start))
    app.add_handler(CommandHandler("play", play))
    app.add_handler(CommandHandler("story", story_command))
    app.add_handler(CommandHandler("charts", stats))
    app.add_handler(CommandHandler("top", top))
    app.add_handler(CommandHandler("log", log_cmd))

    app.add_handler(MessageHandler(filters.TEXT & ~filters.COMMAND & filters.REPLY & StoryReplyFilter(), story_reply_handler))
    app.add_handler(MessageHandler(filters.TEXT & ~filters.COMMAND & filters.ChatType.GROUPS, group_handler))
    app.add_handler(MessageHandler(filters.TEXT & ~filters.COMMAND & filters.ChatType.PRIVATE, search_music))

    app.add_handler(CallbackQueryHandler(story_theme_callback, pattern=r"^story_theme:"))
    app.add_handler(CallbackQueryHandler(click, pattern=r"^play:"))
    
    app.add_handler(InlineQueryHandler(inline_query))
    app.add_handler(ChosenInlineResultHandler(chosen_inline))
    app.add_error_handler(error_handler)

    logger.info("BOT ONLINE 🚀")
    
    # Removidos os argumentos do run_polling
    app.run_polling()
