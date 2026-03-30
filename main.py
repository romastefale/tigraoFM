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
        # Copia EXATAMENTE a mensagem (texto, mídia, formatação, etc.)
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

    # IMPORTANTE: trava input até decisão do usuário
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
