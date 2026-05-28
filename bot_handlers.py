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
import google_calendar_service

PRIORITY_EMOJI = {"high": "🔴", "medium": "🟡", "low": "🟢"}
PRIORITY_LABEL = {"high": "Високий", "medium": "Середній", "low": "Низький"}


def _esc(text: str) -> str:
    if not text:
        return ""
    for ch in r"_*[]()~`>#+-=|{}.!":
        text = text.replace(ch, f"\\{ch}")
    return text


def _format_tasks(tasks: list[dict], transcript: str) -> str:
    lines = [
        f"📝 *Транскрипт:*\n_{_esc(transcript)}_\n",
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


def _confirmation_keyboard(session_key: str) -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup([
        [
            InlineKeyboardButton("✅ Підтвердити", callback_data=f"confirm:{session_key}"),
            InlineKeyboardButton("📅 + Calendar", callback_data=f"calendar:{session_key}"),
        ],
        [
            InlineKeyboardButton("✏️ Уточнити", callback_data=f"clarify:{session_key}"),
            InlineKeyboardButton("❌ Скасувати", callback_data=f"cancel:{session_key}"),
        ],
    ])


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
    tmp_path = None

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
        if tmp_path:
            Path(tmp_path).unlink(missing_ok=True)
        await msg.edit_text(f"❌ Помилка: {e}")


async def _finalize_calendar(message, context: ContextTypes.DEFAULT_TYPE, flow: dict):
    """Create Calendar events and Tasks for all collected dates."""
    tasks_with_dates = [
        {"task": flow["tasks"][i], "date": flow["dates"][i]}
        for i in range(len(flow["tasks"]))
        if i in flow["dates"]
    ]

    skipped = len(flow["tasks"]) - len(tasks_with_dates)

    try:
        links = await google_calendar_service.add_to_calendar(tasks_with_dates)
        text = f"📅 Додано до Google Calendar та Tasks: *{len(links)}* задач\\(и\\)\\!"
        if skipped:
            text += f"\nПропущено \\(без дати\\): {skipped}"
        await message.reply_text(text, parse_mode="MarkdownV2")
    except FileNotFoundError:
        await message.reply_text(
            "❌ Файл `credentials\\.json` не знайдено\\.\n"
            "Налаштуйте Google Calendar API \\(інструкція в README\\)\\.",
            parse_mode="MarkdownV2",
        )
    except Exception as e:
        await message.reply_text(f"❌ Помилка Calendar: {_esc(str(e))}", parse_mode="MarkdownV2")
    finally:
        context.user_data.pop("calendar_flow", None)


async def _ask_next_date(message, flow: dict):
    """Ask user for the next missing deadline."""
    queue = flow["no_deadline_queue"]
    idx = flow["current_queue_idx"]
    task_title = _esc(flow["tasks"][queue[idx]]["title"])
    total = len(queue)
    current = idx + 1
    await message.reply_text(
        f"📅 Задача {current}/{total}: *{task_title}*\n"
        "Вкажіть дату \\(наприклад: `5 червня`, `завтра`, `2024\\-06\\-05`\\)\n"
        "або напишіть `пропустити`\\.",
        parse_mode="MarkdownV2",
    )


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
        await query.edit_message_text(
            f"✅ Збережено {count} задач\\(и\\)\\!", parse_mode="MarkdownV2"
        )

    elif action == "calendar":
        tasks = pending["tasks"]

        # Save to DB
        vm_id = database.save_voice_message(pending["message_id"], pending["transcript"])
        database.save_tasks(vm_id, tasks)
        del context.user_data["pending"][session_key]

        await query.edit_message_text(
            "✅ Збережено в БД\\. Парсю дати для Calendar…", parse_mode="MarkdownV2"
        )

        flow = {
            "tasks": tasks,
            "dates": {},
            "no_deadline_queue": [],
            "current_queue_idx": 0,
        }

        # Parse deadlines that are already mentioned
        for i, task in enumerate(tasks):
            if task.get("deadline"):
                try:
                    date = await google_calendar_service.parse_deadline_to_date(
                        task["deadline"], gemini_service._model
                    )
                    flow["dates"][i] = date
                except Exception:
                    flow["no_deadline_queue"].append(i)
            else:
                flow["no_deadline_queue"].append(i)

        context.user_data["calendar_flow"] = flow

        if flow["no_deadline_queue"]:
            await _ask_next_date(query.message, flow)
        else:
            await _finalize_calendar(query.message, context, flow)

    elif action == "clarify":
        context.user_data["clarify_session"] = session_key
        await query.edit_message_text("✏️ Напишіть уточнення або виправлення:")

    elif action == "cancel":
        del context.user_data["pending"][session_key]
        await query.edit_message_text("❌ Скасовано. Задачі не збережено.")


async def handle_text(update: Update, context: ContextTypes.DEFAULT_TYPE):
    # Clarification flow
    if "clarify_session" in context.user_data:
        session_key = context.user_data.pop("clarify_session")
        pending = context.user_data.get("pending", {}).get(session_key)
        if not pending:
            await update.message.reply_text("❌ Сесія не знайдена.")
            return

        msg = await update.message.reply_text("🔄 Оновлюю задачі…")
        try:
            result = await gemini_service.reprocess_with_clarification(
                pending["transcript"], pending["tasks"], update.message.text
            )
            tasks = result.get("tasks", [])
            context.user_data["pending"][session_key]["tasks"] = tasks
            await msg.edit_text(
                _format_tasks(tasks, pending["transcript"]),
                parse_mode="MarkdownV2",
                reply_markup=_confirmation_keyboard(session_key),
            )
        except Exception as e:
            context.user_data.setdefault("clarify_session", session_key)  # restore on error
            await msg.edit_text(f"❌ Помилка: {e}")
        return

    # Calendar date collection flow
    if "calendar_flow" in context.user_data:
        flow = context.user_data["calendar_flow"]
        queue = flow["no_deadline_queue"]
        idx = flow["current_queue_idx"]

        if idx >= len(queue):
            context.user_data.pop("calendar_flow", None)
            return

        user_input = update.message.text.strip().lower()

        if user_input != "пропустити":
            try:
                date = await google_calendar_service.parse_deadline_to_date(
                    update.message.text, gemini_service._model
                )
                flow["dates"][queue[idx]] = date
            except Exception:
                await update.message.reply_text(
                    "❌ Не вдалося розпізнати дату\\. Спробуйте ще раз\n"
                    "\\(наприклад: `5 червня`, `2024\\-06\\-05`\\)\\.",
                    parse_mode="MarkdownV2",
                )
                return

        flow["current_queue_idx"] += 1

        if flow["current_queue_idx"] < len(queue):
            await _ask_next_date(update.message, flow)
        else:
            await _finalize_calendar(update.message, context, flow)


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
