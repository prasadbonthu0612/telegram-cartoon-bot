import os
import threading
import tempfile
import subprocess
import shutil
import json
from datetime import datetime, timezone
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

QUEUE_MARKER = "[QUEUE_MANIFEST]"

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


async def find_storage_channel():

    dialogs = await telethon_client.get_dialogs(
        limit=None
    )

    for dialog in dialogs:

        entity = dialog.entity

        title = getattr(
            entity,
            "title",
            None
        )

        if title == STORAGE_CHANNEL_NAME:
            return entity

    return None


async def get_generated_clips(
    storage_channel
):

    messages = await telethon_client.get_messages(
        storage_channel,
        limit=100
    )

    clips = []

    for message in messages:

        caption = message.message or ""

        if not caption.startswith("[CLIP]"):
            continue

        if not getattr(message, "video", None):
            continue

        clips.append(message)

    # Oldest generated clip first
    clips.sort(
        key=lambda message: message.id
    )

    return clips


async def create_queue_manifest(
    update: Update,
    context: ContextTypes.DEFAULT_TYPE
):

    try:

        await update.message.reply_text(
            "📋 Creating persistent queue..."
        )

        storage_channel = (
            await find_storage_channel()
        )

        if storage_channel is None:
            raise RuntimeError(
                f'Could not find "{STORAGE_CHANNEL_NAME}".'
            )

        clips = await get_generated_clips(
            storage_channel
        )

        if not clips:
            raise RuntimeError(
                "No generated [CLIP] videos "
                "were found in storage."
            )

        now = datetime.now(
            timezone.utc
        ).isoformat()

        queue = {
            "queue_id": f"queue_{now}",
            "created_at": now,
            "status": "PENDING",
            "total_clips": len(clips),
            "next_clip_index": 1,
            "clips": []
        }

        for index, message in enumerate(
            clips,
            start=1
        ):

            queue["clips"].append({
                "index": index,
                "telegram_message_id": message.id,
                "status": "PENDING",
                "instagram_status": "NOT_POSTED",
                "posted_at": None
            })

        manifest_json = json.dumps(
            queue,
            indent=2
        )

        manifest_text = (
            f"{QUEUE_MARKER}\n\n"
            f"Cartoon Instagram Bot Queue\n\n"
            f"{manifest_json}"
        )

        manifest_message = (
            await telethon_client.send_message(
                storage_channel,
                manifest_text
            )
        )

        await update.message.reply_text(
            "🎉 QUEUE CREATED SUCCESSFULLY!\n\n"
            f"📋 Queue ID:\n"
            f"{queue['queue_id']}\n\n"
            f"🎬 Total clips: "
            f"{queue['total_clips']}\n"
            f"▶️ Next clip: "
            f"{queue['next_clip_index']}\n"
            f"📊 Status: "
            f"{queue['status']}\n\n"
            f"📨 Manifest message ID: "
            f"{manifest_message.id}\n\n"
            "✅ Queue state is now stored "
            "persistently in Telegram."
        )

        print(
            "Queue manifest created."
        )

        print(
            f"Manifest message ID: "
            f"{manifest_message.id}"
        )

        print(
            manifest_json
        )

    except Exception as e:

        print(
            f"QUEUE CREATION ERROR: "
            f"{type(e).__name__}: {str(e)}"
        )

        await update.message.reply_text(
            "❌ QUEUE CREATION FAILED\n\n"
            f"Error: {type(e).__name__}\n"
            f"Details: {str(e)}"
        )


async def show_queue(
    update: Update,
    context: ContextTypes.DEFAULT_TYPE
):

    try:

        storage_channel = (
            await find_storage_channel()
        )

        if storage_channel is None:
            raise RuntimeError(
                f'Could not find "{STORAGE_CHANNEL_NAME}".'
            )

        messages = await telethon_client.get_messages(
            storage_channel,
            limit=100
        )

        manifest = None

        for message in messages:

            text = message.message or ""

            if text.startswith(
                QUEUE_MARKER
            ):

                manifest = message
                break

        if manifest is None:

            await update.message.reply_text(
                "📋 No queue manifest exists yet."
            )

            return

        text = manifest.message

        # Keep Telegram response below practical limits
        if len(text) > 3500:
            text = text[:3500]

        await update.message.reply_text(
            "📋 CURRENT QUEUE\n\n"
            + text
        )

    except Exception as e:

        await update.message.reply_text(
            "❌ Could not read queue.\n\n"
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

    # Render health server
    health_thread = threading.Thread(
        target=start_health_server,
        daemon=True,
    )

    health_thread.start()

    # Telethon
    telethon_client = TelegramClient(
        StringSession(TELEGRAM_SESSION),
        API_ID,
        API_HASH,
    )

    telethon_client.start()

    print(
        "Telethon connected successfully."
    )

    # Telegram Bot API
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
            "create_queue_test",
            create_queue_manifest
        )
    )

    app.add_handler(
        CommandHandler(
            "queue",
            show_queue
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
