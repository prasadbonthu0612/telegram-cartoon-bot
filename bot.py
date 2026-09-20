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
    server = HTTPServer(("0.0.0.0", PORT), HealthHandler)
    server.serve_forever()


async def start(update: Update, context: ContextTypes.DEFAULT_TYPE):
    await update.message.reply_text(
        "👋 Hello!\n\n"
        "I'm your Cartoon Instagram Bot.\n\n"
        "Telegram connection is being initialized."
    )


async def handle_video(update: Update, context: ContextTypes.DEFAULT_TYPE):
    await update.message.reply_text(
        "📥 Video received!\n\n"
        "Video processing will be added next."
    )


def main():
    if not BOT_TOKEN:
        raise RuntimeError("BOT_TOKEN is missing.")

    if not API_ID:
        raise RuntimeError("API_ID is missing.")

    if not API_HASH:
        raise RuntimeError("API_HASH is missing.")

    if not TELEGRAM_SESSION:
        raise RuntimeError("TELEGRAM_SESSION is missing.")

    health_thread = threading.Thread(
        target=start_health_server,
        daemon=True,
    )
    health_thread.start()

    telethon_client = TelegramClient(
        StringSession(TELEGRAM_SESSION),
        API_ID,
        API_HASH,
    )

    telethon_client.start()

    print("Telethon connected successfully.")

    app = Application.builder().token(BOT_TOKEN).build()

    app.add_handler(CommandHandler("start", start))

    app.add_handler(
        MessageHandler(
            filters.VIDEO,
            handle_video,
        )
    )

    print(f"Bot is running. Health server listening on port {PORT}.")

    app.run_polling()


if __name__ == "__main__":
    main()
