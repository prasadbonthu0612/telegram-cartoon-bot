import os
import threading
import tempfile
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
            f"Account: {name}"
        )

    except Exception as e:
        await update.message.reply_text(
            f"❌ Telethon connection failed:\n"
            f"{type(e).__name__}: {str(e)}"
        )


async def handle_video(
    update: Update,
    context: ContextTypes.DEFAULT_TYPE
):
    if not update.message or not update.message.video:
        return

    await update.message.reply_text(
        "📥 Video received!\n\n"
        "🔎 Finding the video through Telethon..."
    )

    try:
        # Get the bot's username using the Bot API
        bot_info = await context.bot.get_me()
        bot_username = bot_info.username

        if not bot_username:
            raise RuntimeError(
                "Could not determine bot username."
            )

        print(
            f"Bot username: @{bot_username}"
        )

        # Find the bot conversation through Telethon
        telethon_bot = await telethon_client.get_entity(
            f"@{bot_username}"
        )

        print(
            f"Telethon bot entity found: "
            f"{telethon_bot.id}"
        )

        # Get recent messages from the bot conversation
        recent_messages = await telethon_client.get_messages(
            telethon_bot,
            limit=20
        )

        if not recent_messages:
            raise RuntimeError(
                "Telethon found no messages in the bot conversation."
            )

        bot_message_id = update.message.message_id

        print(
            f"Bot API message ID: {bot_message_id}"
        )

        # First try to find the exact message ID
        telethon_message = None

        for message in recent_messages:
            if message.id == bot_message_id:
                telethon_message = message
                break

        # If IDs don't match, find the most recent incoming
        # video message from the user.
        if telethon_message is None:
            print(
                "Exact message ID was not found. "
                "Searching recent messages for video..."
            )

            for message in recent_messages:
                if (
                    message.media
                    and getattr(message, "video", None)
                    and not message.out
                ):
                    telethon_message = message
                    break

        if telethon_message is None:
            raise RuntimeError(
                "Telethon could not find the video "
                "in the bot conversation."
            )

        print(
            f"Found Telethon message ID: "
            f"{telethon_message.id}"
        )

        await update.message.reply_text(
            "✅ Video found!\n\n"
            "⬇️ Downloading through Telethon..."
        )

        # Temporary download location
        temp_dir = tempfile.gettempdir()

        download_path = os.path.join(
            temp_dir,
            f"cartoon_test_{telethon_message.id}.mp4"
        )

        print(
            f"Downloading to: {download_path}"
        )

        # Download using Telethon
        downloaded_file = await telethon_client.download_media(
            telethon_message,
            file=download_path
        )

        if not downloaded_file:
            raise RuntimeError(
                "Telethon returned no downloaded file."
            )

        # Get downloaded file size
        file_size = os.path.getsize(
            downloaded_file
        )

        file_size_mb = file_size / (
            1024 * 1024
        )

        print(
            f"Video downloaded successfully: "
            f"{file_size_mb:.2f} MB"
        )

        await update.message.reply_text(
            "✅ VIDEO DOWNLOAD SUCCESSFUL!\n\n"
            f"📦 Size: {file_size_mb:.2f} MB\n"
            "📁 Temporary file created successfully.\n\n"
            "🎉 Telethon can now download videos."
        )

        # Delete temporary test file
        try:
            os.remove(downloaded_file)

            print(
                "Temporary test file deleted."
            )

        except Exception as cleanup_error:
            print(
                f"Cleanup warning: "
                f"{cleanup_error}"
            )

    except Exception as e:

        print(
            f"Video download failed: "
            f"{type(e).__name__}: {str(e)}"
        )

        await update.message.reply_text(
            "❌ Video download failed.\n\n"
            f"Error: {type(e).__name__}\n"
            f"Details: {str(e)}"
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

    # Start health server for Render
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
