import os
import threading
from http.server import BaseHTTPRequestHandler, HTTPServer

from telegram import Update
from telegram.ext import (
    Application,
    CommandHandler,
    MessageHandler,
    ContextTypes,
    filters,
)

from telethon import TelegramClient
from telethon.sessions import StringSession


BOT_TOKEN = os.getenv("BOT_TOKEN")
API_ID = int(os.getenv("API_ID", "0"))
API_HASH = os.getenv("API_HASH")
TELEGRAM_SESSION = os.getenv("TELEGRAM_SESSION")

PORT = int(os.getenv("PORT", "10000"))

telethon_client = None


class HealthHandler(BaseHTTPRequestHandler):

    def do_GET(self):
        if self.path == "/health":
            self.send_response(200)
            self.send_header("Content-Type", "text/plain")
            self.end_headers()
            self.wfile.write(b"Bot is healthy")
        else:
            self.send_response(404)
            self.end_headers()

    def log_message(self, format, *args):
        return


def start_health_server():
    server = HTTPServer(
        ("0.0.0.0", PORT),
        HealthHandler
    )
    server.serve_forever()


async def start(
    update: Update,
    context: ContextTypes.DEFAULT_TYPE
):
    await update.message.reply_text(
        "👋 Hello!\n\n"
        "I'm your Cartoon Instagram Bot.\n\n"
        "Telegram connection is working."
    )


async def test_telegram(
    update: Update,
    context: ContextTypes.DEFAULT_TYPE
):
    try:
        me = await telethon_client.get_me()

        name = " ".join(
            part
            for part in [me.first_name, me.last_name]
            if part
        )

        await update.message.reply_text(
            f"✅ Telethon connected!\n\n"
            f"Account: {name}\n"
            f"User ID: {me.id}"
        )

    except Exception as e:
        await update.message.reply_text(
            f"❌ Telethon connection failed:\n"
            f"{type(e).__name__}: {str(e)}"
        )


async def debug_telethon(
    update: Update,
    context: ContextTypes.DEFAULT_TYPE
):
    try:
        # Get bot information through Bot API
        bot_info = await context.bot.get_me()

        bot_username = bot_info.username
        bot_id = bot_info.id

        if not bot_username:
            raise RuntimeError(
                "Bot username could not be determined."
            )

        print(
            f"DEBUG: Bot username = @{bot_username}"
        )

        print(
            f"DEBUG: Bot ID = {bot_id}"
        )

        # Get our Telethon account
        me = await telethon_client.get_me()

        print(
            f"DEBUG: Telethon account = "
            f"{me.first_name} ({me.id})"
        )

        # Find the bot through Telethon
        bot_entity = await telethon_client.get_entity(
            f"@{bot_username}"
        )

        print(
            f"DEBUG: Telethon bot entity ID = "
            f"{bot_entity.id}"
        )

        # Read recent messages from the bot conversation
        messages = await telethon_client.get_messages(
            bot_entity,
            limit=20
        )

        if not messages:
            await update.message.reply_text(
                "⚠️ Telethon found the bot, "
                "but there are no messages in the conversation."
            )
            return

        lines = [
            "🔎 TELETHON DEBUG",
            "",
            f"Bot: @{bot_username}",
            f"Bot ID: {bot_id}",
            f"Messages found: {len(messages)}",
            "",
            "Recent messages:"
        ]

        for msg in messages:

            has_media = bool(msg.media)

            media_type = "none"

            if getattr(msg, "video", None):
                media_type = "VIDEO"

            elif msg.media:
                media_type = type(
                    msg.media
                ).__name__

            direction = (
                "OUT"
                if msg.out
                else "IN"
            )

            line = (
                f"ID={msg.id} | "
                f"{direction} | "
                f"media={media_type}"
            )

            lines.append(line)

            print(
                f"DEBUG MESSAGE: {line}"
            )

        result = "\n".join(lines)

        # Telegram messages have a practical length limit,
        # so keep the diagnostic response short.
        if len(result) > 3500:
            result = result[:3500]

        await update.message.reply_text(
            result
        )

    except Exception as e:

        print(
            f"DEBUG ERROR: "
            f"{type(e).__name__}: {str(e)}"
        )

        await update.message.reply_text(
            "❌ Telethon debug failed.\n\n"
            f"Error: {type(e).__name__}\n"
            f"Details: {str(e)}"
        )


async def handle_video(
    update: Update,
    context: ContextTypes.DEFAULT_TYPE
):
    if not update.message or not update.message.video:
        return

    await update.message.reply_text(
        "📥 Video received!\n\n"
        "This is currently diagnostic mode.\n"
        "Use /debug_telethon to inspect the conversation."
    )


def main():

    global telethon_client

    if not BOT_TOKEN:
        raise RuntimeError(
            "BOT_TOKEN is missing."
        )

    if not API_ID:
        raise RuntimeError(
            "API_ID is missing."
        )

    if not API_HASH:
        raise RuntimeError(
            "API_HASH is missing."
        )

    if not TELEGRAM_SESSION:
        raise RuntimeError(
            "TELEGRAM_SESSION is missing."
        )

    # Start Render health server
    health_thread = threading.Thread(
        target=start_health_server,
        daemon=True,
    )

    health_thread.start()

    # Start Telethon
    telethon_client = TelegramClient(
        StringSession(TELEGRAM_SESSION),
        API_ID,
        API_HASH,
    )

    telethon_client.start()

    print(
        "Telethon connected successfully."
    )

    # Start Bot API
    app = Application.builder().token(
        BOT_TOKEN
    ).build()

    app.add_handler(
        CommandHandler(
            "start",
            start
        )
    )

    app.add_handler(
        CommandHandler(
            "test_telegram",
            test_telegram
        )
    )

    app.add_handler(
        CommandHandler(
            "debug_telethon",
            debug_telethon
        )
    )

    app.add_handler(
        MessageHandler(
            filters.VIDEO,
            handle_video,
        )
    )

    print(
        f"Bot is running. "
        f"Health server listening on port {PORT}."
    )

    app.run_polling()


if __name__ == "__main__":
    main()
