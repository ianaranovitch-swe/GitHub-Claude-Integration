#!/usr/bin/env python3
"""
Telegram-бот: генерирует HTML-лендинг по GitHub-репозиториям пользователя.
Диалог: username → публичные repos → опционально приватные через токен.
"""

import asyncio
import logging
import os
import re
from datetime import datetime
from pathlib import Path

from dotenv import load_dotenv
from telegram import InlineKeyboardButton, InlineKeyboardMarkup, Update
from telegram.ext import (
    Application,
    CallbackQueryHandler,
    CommandHandler,
    ContextTypes,
    ConversationHandler,
    MessageHandler,
    filters,
)

from generate_bot_descriptions import (
    JobConfig,
    count_public_repos,
    run_generation_job,
    validate_github_token,
    validate_github_username,
)

load_dotenv(Path(__file__).resolve().parent / ".env")

TELEGRAM_BOT_TOKEN = os.environ.get("TELEGRAM_BOT_TOKEN", "")
OUTPUT_BASE_DIR = Path(os.environ.get("BOT_OUTPUT_DIR", "output/telegram"))

# Состояния диалога
ASK_USERNAME, ASK_PRIVATE, WAIT_TOKEN = range(3)

# Callback-data для inline-кнопок
CB_PRIVATE_YES = "private_yes"
CB_PRIVATE_NO = "private_no"

GITHUB_USERNAME_PATTERN = re.compile(r"^[a-zA-Z0-9](?:[a-zA-Z0-9-]{0,37}[a-zA-Z0-9])?$")
GITHUB_TOKEN_PATTERN = re.compile(r"^(ghp_[A-Za-z0-9]{20,}|github_pat_[A-Za-z0-9_]{20,})$")

logging.basicConfig(
    format="%(asctime)s - %(name)s - %(levelname)s - %(message)s",
    level=logging.INFO,
)
logging.getLogger("httpx").setLevel(logging.WARNING)
logger = logging.getLogger(__name__)


def normalize_username(text: str) -> str:
    """Убирает @ и пробелы из GitHub-логина."""
    return text.strip().lstrip("@")


def user_output_dir(user_id: int, username: str) -> Path:
    """Папка результатов для одного Telegram-пользователя."""
    stamp = datetime.now().strftime("%Y%m%d_%H%M%S")
    safe_user = re.sub(r"[^\w.-]", "_", username)
    return OUTPUT_BASE_DIR / str(user_id) / f"{safe_user}_{stamp}"


async def start(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    """Приветствие и запрос GitHub username."""
    context.user_data.clear()
    await update.message.reply_text(
        "Hej! Jag skapar en HTML-landningssida med beskrivningar av dina GitHub-repos.\n\n"
        "Steg 1: Skicka ditt **GitHub-användarnamn** (t.ex. `ianaranovitch-swe`).\n\n"
        "Jag börjar med **publika repos**. Sedan kan du välja att inkludera privata repos "
        "med en GitHub-token.\n\n"
        "/cancel — avbryt",
        parse_mode="Markdown",
    )
    return ASK_USERNAME


async def receive_username(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    """Принимает GitHub username и спрашивает про приватные repos."""
    username = normalize_username(update.message.text or "")
    if not GITHUB_USERNAME_PATTERN.match(username):
        await update.message.reply_text(
            "Ogiltigt användarnamn. Skicka ett giltigt GitHub-namn, t.ex. `ditt-namn`.",
            parse_mode="Markdown",
        )
        return ASK_USERNAME

    status = await update.message.reply_text("⏳ Kontrollerar GitHub-konto...")
    if not validate_github_username(username):
        await status.edit_text(
            f"Kunde inte hitta GitHub-användaren `@{username}`.\n"
            "Kontrollera stavningen och försök igen.",
            parse_mode="Markdown",
        )
        return ASK_USERNAME

    public_count = count_public_repos(username)
    context.user_data["github_username"] = username
    context.user_data["public_count"] = public_count

    keyboard = InlineKeyboardMarkup(
        [
            [
                InlineKeyboardButton("Ja, inkludera privata", callback_data=CB_PRIVATE_YES),
                InlineKeyboardButton("Nej, bara publika", callback_data=CB_PRIVATE_NO),
            ]
        ]
    )

    await status.edit_text(
        f"✅ Hittade GitHub-konto `@{username}`\n"
        f"📦 Publika repos: **{public_count}**\n\n"
        "Vill du att jag analyserar dina **privata** GitHub-repos också?\n"
        "Då behöver du en Personal Access Token (classic) med rättigheten **repo**.\n\n"
        "Token sparas **inte** — används bara under denna körning.",
        parse_mode="Markdown",
        reply_markup=keyboard,
    )
    return ASK_PRIVATE


async def private_choice(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    """Обрабатывает выбор: только публичные или нужен токен."""
    query = update.callback_query
    await query.answer()

    if query.data == CB_PRIVATE_NO:
        await query.edit_message_text(
            f"👍 OK! Jag genererar landningssida för **publika repos** på `@{context.user_data['github_username']}`.",
            parse_mode="Markdown",
        )
        await run_job_for_user(query.message.chat_id, context, github_token="")
        return ConversationHandler.END

    await query.edit_message_text(
        "Skicka din **GitHub Personal Access Token** (classic).\n\n"
        "⚠️ Skicka token i ett **privat chattmeddelande** till denna bot.\n"
        "Meddelandet med token **raderas** direkt efter mottagning.\n\n"
        "Token måste tillhöra samma konto som användarnamnet du angav.\n"
        "/cancel — avbryt",
        parse_mode="Markdown",
    )
    return WAIT_TOKEN


async def receive_token(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    """Принимает GitHub-токен, проверяет и запускает генерацию."""
    token = (update.message.text or "").strip()
    username = context.user_data.get("github_username", "")

    # Удаляем сообщение с токеном из чата
    try:
        await update.message.delete()
    except Exception as err:
        logger.warning("Kunde inte radera token-meddelande: %s", err)

    if not GITHUB_TOKEN_PATTERN.match(token):
        await update.effective_chat.send_message(
            "Ogiltig token-format. Skicka en GitHub Personal Access Token som börjar med `ghp_` "
            "eller `github_pat_`.",
            parse_mode="Markdown",
        )
        return WAIT_TOKEN

    status = await update.effective_chat.send_message("⏳ Verifierar token...")
    valid, error = validate_github_token(username, token)
    if not valid:
        await status.edit_text(f"❌ {error}\n\nFörsök igen eller skriv /cancel.")
        return WAIT_TOKEN

    await status.edit_text(
        f"✅ Token OK för `@{username}`.\n"
        "Jag hämtar publika **och** privata repos...",
        parse_mode="Markdown",
    )
    await run_job_for_user(update.effective_chat.id, context, github_token=token)
    return ConversationHandler.END


async def run_job_for_user(chat_id: int, context: ContextTypes.DEFAULT_TYPE, github_token: str):
    """Запускает генерацию и отправляет файлы пользователю."""
    if context.user_data.get("job_running"):
        await context.bot.send_message(chat_id, "⏳ En körning pågår redan. Vänta tills den är klar.")
        return

    username = context.user_data.get("github_username")
    if not username:
        await context.bot.send_message(chat_id, "Sessionen saknar användarnamn. Skriv /start igen.")
        return

    context.user_data["job_running"] = True
    output_dir = user_output_dir(chat_id, username)
    status_message = await context.bot.send_message(
        chat_id,
        f"🚀 Startar generering för `@{username}`...\n"
        f"{'🔓 Inkluderar privata repos' if github_token else '🌐 Bara publika repos'}",
        parse_mode="Markdown",
    )

    progress_lines: list[str] = []

    def on_progress(message: str):
        progress_lines.append(message)
        if len(progress_lines) > 6:
            progress_lines.pop(0)

    job = JobConfig(
        github_username=username,
        github_token=github_token,
        repo_filter="all",
        output_dir=output_dir,
        use_claude_html=True,
        fresh_pricing=True,
    )

    try:
        output, json_path, html_path = await asyncio.to_thread(
            run_generation_job,
            job,
            on_progress,
        )

        await status_message.edit_text(
            f"✅ Klart för `@{username}`!\n"
            f"📊 {output.get('total_bots', 0)} repos analyserade.\n"
            "Skickar filer...",
            parse_mode="Markdown",
        )

        with open(html_path, "rb") as html_file:
            await context.bot.send_document(
                chat_id,
                document=html_file,
                filename="index.html",
                caption="🌐 Din HTML-landningssida — öppna i webbläsaren.",
            )
        with open(json_path, "rb") as json_file:
            await context.bot.send_document(
                chat_id,
                document=json_file,
                filename="bot_descriptions.json",
                caption="📄 JSON-data med alla beskrivningar och priser.",
            )

        await context.bot.send_message(
            chat_id,
            "Vill du skapa en ny sida? Skriv /start",
        )
    except Exception as err:
        logger.exception("Generering misslyckades")
        await status_message.edit_text(
            f"❌ Något gick fel:\n`{err}`\n\nFörsök igen med /start",
            parse_mode="Markdown",
        )
    finally:
        context.user_data["job_running"] = False
        context.user_data.pop("github_token", None)


async def cancel(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    """Отмена диалога."""
    context.user_data.clear()
    await update.message.reply_text("Avbrutet. Skriv /start när du vill börja om.")
    return ConversationHandler.END


def build_application() -> Application:
    """Собирает Telegram Application."""
    if not TELEGRAM_BOT_TOKEN:
        raise RuntimeError("TELEGRAM_BOT_TOKEN saknas i .env")

    application = Application.builder().token(TELEGRAM_BOT_TOKEN).build()

    conv_handler = ConversationHandler(
        entry_points=[CommandHandler("start", start)],
        states={
            ASK_USERNAME: [
                MessageHandler(filters.TEXT & ~filters.COMMAND, receive_username),
            ],
            ASK_PRIVATE: [
                CallbackQueryHandler(private_choice, pattern=f"^({CB_PRIVATE_YES}|{CB_PRIVATE_NO})$"),
            ],
            WAIT_TOKEN: [
                MessageHandler(filters.TEXT & ~filters.COMMAND, receive_token),
            ],
        },
        fallbacks=[CommandHandler("cancel", cancel)],
        allow_reentry=True,
    )

    application.add_handler(conv_handler)
    application.add_handler(CommandHandler("help", start))
    return application


def main():
    """Запуск бота."""
    print("🤖 Telegram Landing Generator Bot")
    print("=" * 50)
    app = build_application()
    print("✅ Bot körs — tryck Ctrl+C för att stoppa")
    app.run_polling(allowed_updates=Update.ALL_TYPES)


if __name__ == "__main__":
    main()
