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

STORAGE_CHANNEL_NAME = "Cartoon Clip Storage"

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


async def debug_storage(
    update: Update,
    context: ContextTypes.DEFAULT_TYPE
):
    try:
        await update.message.reply_text(
            "🔎 Searching for Cartoon Clip Storage..."
        )

        storage_channel = None

        # Get all dialogs available to the Telethon account
        dialogs = await telethon_client.get_dialogs(
            limit=None
        )

        print(
            f"DEBUG: Telethon found {len(dialogs)} dialogs."
        )

        for dialog in dialogs:

            entity = dialog.entity

            title = getattr(
                entity,
                "title",
                None
            )

            if title == STORAGE_CHANNEL_NAME:
                storage_channel = entity
                break

        if storage_channel is None:
            raise RuntimeError(
                f'Could not find the channel '
                f'"{STORAGE_CHANNEL_NAME}".'
            )

        channel_id = storage_channel.id

        print(
            f"DEBUG: Storage channel found."
        )
        print(
            f"DEBUG: Channel ID = {channel_id}"
        )

        # Read recent messages
        messages = await telethon_client.get_messages(
            storage_channel,
            limit=20
        )

        if not messages:
            await update.message.reply_text(
                "⚠️ Channel found, "
                "but no messages were found."
            )
            return

        lines = [
            "📦 STORAGE CHANNEL DEBUG",
            "",
            f"Channel: {STORAGE_CHANNEL_NAME}",
            f"Channel ID: {channel_id}",
            f"Messages found: {len(messages)}",
            "",
            "Recent messages:"
        ]

        for message in messages:

            media_type = "none"

            if getattr(message, "video", None):
                media_type = "VIDEO"

            elif getattr(message, "document", None):
                media_type = "DOCUMENT"

            elif message.media:
                media_type = type(
                    message.media
                ).__name__

            direction = (
                "OUT"
                if message.out
                else "IN"
            )

            line = (
                f"ID={message.id} | "
                f"{direction} | "
                f"media={media_type}"
            )

            lines.append(line)

            print(
                f"DEBUG STORAGE MESSAGE: {line}"
            )

        result = "\n".join(lines)

        if len(result) > 3500:
            result = result[:3500]

        await update.message.reply_text(
            result
        )

    except Exception as e:

        print(
            f"STORAGE DEBUG ERROR: "
            f"{type(e).__name__}: {str(e)}"
        )

        await update.message.reply_text(
            "❌ Storage diagnostic failed.\n\n"
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
        "Video processing will be added next."
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

    # Start Telegram Bot API
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
            "debug_storage",
            debug_storage
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
