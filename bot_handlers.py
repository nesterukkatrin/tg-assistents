import os
import tempfile
from pathlib import Path

from telegram import InlineKeyboardButton, InlineKeyboardMarkup, Update
from telegram.ext import (
    Application,
    CallbackQueryHandler,
    CommandHandler,
    ContextTypes,
    MessageHandler,
    filters,
)

import database
import gemini_service

PRIORITY_EMOJI = {"high": "🔴", "medium": "🟡", "low": "🟢"}
PRIORITY_LABEL = {"high": "Високий", "medium": "Середній", "low": "Низький"}


def _format_tasks(tasks: list[dict], transcript: str) -> str:
    lines = [
        f"📝 *Транскрипт:*\n_{transcript}_\n",
        f"*Знайдено задач: {len(tasks)}*\n",
    ]
    for i, t in enumerate(tasks, 1):
        priority = t.get("priority", "medium")
        lines.append(f"*{i}\\. {_esc(t['title'])}*")
        if t.get("description") and t["description"] != t["title"]:
            lines.append(f"   📋 {_esc(t['description'])}")
        if t.get("responsible"):
            lines.append(f"   👤 {_esc(t['responsible'])}")
        if t.get("deadline"):
            lines.append(f"   📅 {_esc(t['deadline'])}")
        lines.append(f"   {PRIORITY_EMOJI[priority]} {PRIORITY_LABEL[priority]}")
        lines.append("")
    return "\n".join(lines)


def _esc(text: str) -> str:
    """Escape MarkdownV2 special characters."""
    for ch in r"_*[]()~`>#+-=|{}.!":
        text = text.replace(ch, f"\\{ch}")
    return text


def _confirmation_keyboard(session_key: str) -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup([[
        InlineKeyboardButton("✅ Підтвердити", callback_data=f"confirm:{session_key}"),
        InlineKeyboardButton("✏️ Уточнити", callback_data=f"clarify:{session_key}"),
        InlineKeyboardButton("❌ Скасувати", callback_data=f"cancel:{session_key}"),
    ]])


async def cmd_start(update: Update, context: ContextTypes.DEFAULT_TYPE):
    await update.message.reply_text(
        "👋 Привіт\\! Я асистент для фіксації задач\\.\n\n"
        "Надішліть голосове повідомлення — розпізнаю задачі, "
        "відповідальних та дедлайни\\.\n\n"
        "Команди:\n"
        "/tasks — переглянути всі задачі",
        parse_mode="MarkdownV2",
    )


async def cmd_tasks(update: Update, context: ContextTypes.DEFAULT_TYPE):
    tasks = database.get_all_tasks()
    if not tasks:
        await update.message.reply_text("Задач поки немає.")
        return

    lines = [f"📋 *Всі задачі \\({len(tasks)}\\):*\n"]
    for t in tasks:
        emoji = PRIORITY_EMOJI.get(t["priority"], "🟡")
        status = "✅" if t["status"] == "done" else "⏳"
        lines.append(f"{status} {emoji} *{_esc(t['title'])}*")
        if t.get("responsible"):
            lines.append(f"   👤 {_esc(t['responsible'])}")
        if t.get("deadline"):
            lines.append(f"   📅 {_esc(t['deadline'])}")
        lines.append(f"   🆔 \\#{t['id']}")
        lines.append("")

    await update.message.reply_text("\n".join(lines), parse_mode="MarkdownV2")


async def handle_voice(update: Update, context: ContextTypes.DEFAULT_TYPE):
    msg = await update.message.reply_text("🎙️ Обробляю голосове повідомлення…")

    try:
        file = await context.bot.get_file(update.message.voice.file_id)

        with tempfile.NamedTemporaryFile(suffix=".ogg", delete=False) as tmp:
            tmp_path = tmp.name

        await file.download_to_drive(tmp_path)

        result = await gemini_service.process_voice_message(tmp_path)
        Path(tmp_path).unlink(missing_ok=True)

        transcript = result.get("transcript", "")
        tasks = result.get("tasks", [])

        if not tasks:
            await msg.edit_text(
                f"📝 Транскрипт:\n_{_esc(transcript)}_\n\nЗадач не знайдено\\. Спробуйте ще раз\\.",
                parse_mode="MarkdownV2",
            )
            return

        session_key = str(update.message.message_id)
        context.user_data.setdefault("pending", {})[session_key] = {
            "tasks": tasks,
            "transcript": transcript,
            "message_id": update.message.message_id,
        }

        await msg.edit_text(
            _format_tasks(tasks, transcript),
            parse_mode="MarkdownV2",
            reply_markup=_confirmation_keyboard(session_key),
        )

    except Exception as e:
        Path(tmp_path).unlink(missing_ok=True) if "tmp_path" in locals() else None
        await msg.edit_text(f"❌ Помилка: {e}")


async def handle_callback(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    await query.answer()

    action, session_key = query.data.split(":", 1)
    pending = context.user_data.get("pending", {}).get(session_key)

    if not pending:
        await query.edit_message_text("❌ Сесія застаріла. Надішліть нове голосове повідомлення.")
        return

    if action == "confirm":
        vm_id = database.save_voice_message(pending["message_id"], pending["transcript"])
        count = len(database.save_tasks(vm_id, pending["tasks"]))
        del context.user_data["pending"][session_key]
        await query.edit_message_text(f"✅ Збережено {count} задач\\(и\\)\\!", parse_mode="MarkdownV2")

    elif action == "clarify":
        context.user_data["clarify_session"] = session_key
        await query.edit_message_text("✏️ Напишіть уточнення або виправлення:")

    elif action == "cancel":
        del context.user_data["pending"][session_key]
        await query.edit_message_text("❌ Скасовано. Задачі не збережено.")


async def handle_text(update: Update, context: ContextTypes.DEFAULT_TYPE):
    session_key = context.user_data.get("clarify_session")
    if not session_key:
        return

    pending = context.user_data.get("pending", {}).get(session_key)
    if not pending:
        await update.message.reply_text("❌ Сесія не знайдена.")
        context.user_data.pop("clarify_session", None)
        return

    msg = await update.message.reply_text("🔄 Оновлюю задачі…")

    try:
        result = await gemini_service.reprocess_with_clarification(
            pending["transcript"], pending["tasks"], update.message.text
        )
        tasks = result.get("tasks", [])
        context.user_data["pending"][session_key]["tasks"] = tasks
        context.user_data.pop("clarify_session", None)

        await msg.edit_text(
            _format_tasks(tasks, pending["transcript"]),
            parse_mode="MarkdownV2",
            reply_markup=_confirmation_keyboard(session_key),
        )

    except Exception as e:
        await msg.edit_text(f"❌ Помилка: {e}")


def create_application() -> Application:
    database.init_db()
    gemini_service.init_gemini(os.getenv("GEMINI_API_KEY"))

    allowed_id = int(os.getenv("TELEGRAM_USER_ID"))
    user_filter = filters.User(user_id=allowed_id)

    app = Application.builder().token(os.getenv("TELEGRAM_BOT_TOKEN")).build()
    app.add_handler(CommandHandler("start", cmd_start, filters=user_filter))
    app.add_handler(CommandHandler("tasks", cmd_tasks, filters=user_filter))
    app.add_handler(MessageHandler(filters.VOICE & user_filter, handle_voice))
    app.add_handler(CallbackQueryHandler(handle_callback))
    app.add_handler(MessageHandler(filters.TEXT & ~filters.COMMAND & user_filter, handle_text))

    return app
