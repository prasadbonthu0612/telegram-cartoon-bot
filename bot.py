import os
import threading
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

from telethon import TelegramClient, events
from telethon.sessions import StringSession


# ============================================================
# ENVIRONMENT VARIABLES
# ============================================================

BOT_TOKEN = os.getenv("BOT_TOKEN")
API_ID = int(os.getenv("API_ID", "0"))
API_HASH = os.getenv("API_HASH")
TELEGRAM_SESSION = os.getenv("TELEGRAM_SESSION")

PORT = int(os.getenv("PORT", "10000"))

STORAGE_CHANNEL_NAME = "Cartoon Clip Storage"

QUEUE_MARKER = "[QUEUE_MANIFEST]"
CONFIG_MARKER = "[BOT_CONFIG]"
TITLE_REQUEST_MARKER = "[TITLE_REQUEST]"

telethon_client = None

# User's Telegram chat ID.
# It gets saved persistently inside the Telegram storage channel.
admin_chat_id = None

# Current video waiting for a title.
pending_video_message_id = None


# ============================================================
# HEALTH SERVER
# ============================================================

class HealthHandler(BaseHTTPRequestHandler):

    def do_GET(self):

        if self.path == "/health":

            self.send_response(200)
            self.send_header(
                "Content-Type",
                "text/plain"
            )
            self.end_headers()

            self.wfile.write(
                b"Bot is healthy"
            )

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


# ============================================================
# FIND STORAGE CHANNEL
# ============================================================

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


# ============================================================
# SAVE / LOAD ADMIN CHAT ID
# ============================================================

async def save_admin_chat_id(chat_id):

    global admin_chat_id

    admin_chat_id = chat_id

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

    # Check whether configuration already exists.
    for message in messages:

        text = message.message or ""

        if text.startswith(CONFIG_MARKER):

            try:

                config = json.loads(
                    text.split("\n", 1)[1]
                )

                old_chat_id = config.get(
                    "admin_chat_id"
                )

                if old_chat_id == chat_id:
                    return

                new_text = (
                    f"{CONFIG_MARKER}\n"
                    f"{json.dumps({'admin_chat_id': chat_id})}"
                )

                await telethon_client.edit_message(
                    storage_channel,
                    message.id,
                    new_text
                )

                return

            except Exception:
                pass

    # No configuration exists.
    config_text = (
        f"{CONFIG_MARKER}\n"
        f"{json.dumps({'admin_chat_id': chat_id})}"
    )

    await telethon_client.send_message(
        storage_channel,
        config_text
    )


async def load_admin_chat_id():

    global admin_chat_id

    storage_channel = (
        await find_storage_channel()
    )

    if storage_channel is None:
        return None

    messages = await telethon_client.get_messages(
        storage_channel,
        limit=100
    )

    for message in messages:

        text = message.message or ""

        if not text.startswith(CONFIG_MARKER):
            continue

        try:

            config = json.loads(
                text.split("\n", 1)[1]
            )

            admin_chat_id = config.get(
                "admin_chat_id"
            )

            return admin_chat_id

        except Exception:
            continue

    return None


# ============================================================
# START COMMAND
# ============================================================

async def start(
    update: Update,
    context: ContextTypes.DEFAULT_TYPE
):

    global admin_chat_id

    try:

        chat_id = update.effective_chat.id

        await save_admin_chat_id(
            chat_id
        )

        await update.message.reply_text(
            "👋 Hello!\n\n"
            "✅ Your Telegram account is connected.\n\n"
            "🎬 Now upload the ORIGINAL video "
            "to Cartoon Clip Storage.\n\n"
            "I'll automatically detect it and "
            "ask you for the title."
        )

    except Exception as e:

        await update.message.reply_text(
            "❌ Could not save your Telegram "
            "configuration.\n\n"
            f"{type(e).__name__}: {str(e)}"
        )


# ============================================================
# TELETHON TEST
# ============================================================

async def test_telegram(
    update: Update,
    context: ContextTypes.DEFAULT_TYPE
):

    try:

        me = await telethon_client.get_me()

        name = " ".join(
            part
            for part in [
                me.first_name,
                me.last_name
            ]
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


# ============================================================
# TITLE REQUEST STORAGE
# ============================================================

async def save_title_request(
    video_message_id
):

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

    request_text = (
        f"{TITLE_REQUEST_MARKER}\n"
        f"{json.dumps({'video_message_id': video_message_id})}"
    )

    # Update existing request if present.
    for message in messages:

        text = message.message or ""

        if text.startswith(
            TITLE_REQUEST_MARKER
        ):

            await telethon_client.edit_message(
                storage_channel,
                message.id,
                request_text
            )

            return

    # Otherwise create one.
    await telethon_client.send_message(
        storage_channel,
        request_text
    )


async def load_title_request():

    storage_channel = (
        await find_storage_channel()
    )

    if storage_channel is None:
        return None

    messages = await telethon_client.get_messages(
        storage_channel,
        limit=100
    )

    for message in messages:

        text = message.message or ""

        if not text.startswith(
            TITLE_REQUEST_MARKER
        ):
            continue

        try:

            data = json.loads(
                text.split("\n", 1)[1]
            )

            return data.get(
                "video_message_id"
            )

        except Exception:
            continue

    return None


# ============================================================
# TELEGRAM CHANNEL VIDEO DETECTOR
# ============================================================

async def channel_video_handler(event):

    global pending_video_message_id

    try:

        message = event.message

        # Only process videos.
        if not message.video:
            return

        caption = message.message or ""

        # Ignore generated clips.
        if caption.startswith("[CLIP]"):
            return

        # Ignore queue manifests.
        if caption.startswith(QUEUE_MARKER):
            return

        # Ignore bot configuration messages.
        if caption.startswith(CONFIG_MARKER):
            return

        # Ignore our title-request state message.
        if caption.startswith(TITLE_REQUEST_MARKER):
            return

        print(
            f"🎬 New video detected in storage."
        )

        print(
            f"Telegram message ID: {message.id}"
        )

        # Load the user's Telegram chat ID.
        chat_id = await load_admin_chat_id()

        if not chat_id:

            print(
                "⚠️ No admin chat ID is saved."
            )

            print(
                "Open the bot and send /start first."
            )

            return

        # Save the pending video persistently.
        await save_title_request(
            message.id
        )

        pending_video_message_id = message.id

        # Ask the user for the title.
        await send_title_question(
            chat_id,
            message.id
        )

    except Exception as e:

        print(
            "❌ CHANNEL VIDEO HANDLER ERROR:"
        )

        print(
            f"{type(e).__name__}: {str(e)}"
        )


async def send_title_question(
    chat_id,
    video_message_id
):

    try:

        await bot_application.bot.send_message(
            chat_id=chat_id,
            text=(
                "🎬 NEW VIDEO DETECTED!\n\n"
                f"Telegram video ID: {video_message_id}\n\n"
                "✏️ What is the title of this video?\n\n"
                "Example:\n"
                "Doraemon Episode 25"
            )
        )

    except Exception as e:

        print(
            "❌ Could not send title question:"
        )

        print(
            f"{type(e).__name__}: {str(e)}"
        )


# ============================================================
# TITLE RESPONSE
# ============================================================

async def handle_title(
    update: Update,
    context: ContextTypes.DEFAULT_TYPE
):

    global pending_video_message_id

    if not update.message:
        return

    title = (
        update.message.text or ""
    ).strip()

    if not title:
        return

    # Don't treat commands as titles.
    if title.startswith("/"):
        return

    chat_id = update.effective_chat.id

    saved_chat_id = await load_admin_chat_id()

    if saved_chat_id != chat_id:
        return

    video_message_id = (
        await load_title_request()
    )

    if not video_message_id:

        await update.message.reply_text(
            "ℹ️ I don't have a video waiting "
            "for a title."
        )

        return

    pending_video_message_id = (
        video_message_id
    )

    print(
        "🎬 TITLE RECEIVED"
    )

    print(
        f"Title: {title}"
    )

    print(
        f"Video message ID: "
        f"{video_message_id}"
    )

    await update.message.reply_text(
        "✅ Title received!\n\n"
        f"🎬 {title}\n\n"
        "⏳ Video processing will be added "
        "in the next step."
    )

    # For now, we only test the title flow.
    # Splitting will be added next.


# ============================================================
# EXISTING QUEUE TEST
# ============================================================

async def create_queue_manifest(
    update: Update,
    context: ContextTypes.DEFAULT_TYPE
):

    await update.message.reply_text(
        "ℹ️ The old manual queue test is "
        "still available, but we'll replace "
        "it with automatic queue creation."
    )


# ============================================================
# SHOW QUEUE
# ============================================================

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


# ============================================================
# BOT VIDEO HANDLER
# ============================================================

async def handle_video(
    update: Update,
    context: ContextTypes.DEFAULT_TYPE
):

    if not update.message:
        return

    if not update.message.video:
        return

    await update.message.reply_text(
        "📥 Video received by the bot.\n\n"
        "For automatic processing, upload "
        "the ORIGINAL video directly into "
        "Cartoon Clip Storage."
    )


# ============================================================
# GLOBAL BOT APPLICATION
# ============================================================

bot_application = None


# ============================================================
# MAIN
# ============================================================

def main():

    global telethon_client
    global bot_application

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

    # --------------------------------------------------------
    # Render health server
    # --------------------------------------------------------

    health_thread = threading.Thread(
        target=start_health_server,
        daemon=True,
    )

    health_thread.start()

    # --------------------------------------------------------
    # Telethon
    # --------------------------------------------------------

    telethon_client = TelegramClient(
        StringSession(TELEGRAM_SESSION),
        API_ID,
        API_HASH,
    )

    telethon_client.start()

    print(
        "Telethon connected successfully."
    )

    # --------------------------------------------------------
    # Telegram Bot API
    # --------------------------------------------------------

    bot_application = (
        Application.builder()
        .token(BOT_TOKEN)
        .build()
    )

    bot_application.add_handler(
        CommandHandler(
            "start",
            start
        )
    )

    bot_application.add_handler(
        CommandHandler(
            "test_telegram",
            test_telegram
        )
    )

    bot_application.add_handler(
        CommandHandler(
            "create_queue_test",
            create_queue_manifest
        )
    )

    bot_application.add_handler(
        CommandHandler(
            "queue",
            show_queue
        )
    )

    bot_application.add_handler(
        MessageHandler(
            filters.VIDEO,
            handle_video,
        )
    )

    bot_application.add_handler(
        MessageHandler(
            filters.TEXT
            & ~filters.COMMAND,
            handle_title,
        )
    )

    # --------------------------------------------------------
    # Telethon channel watcher
    # --------------------------------------------------------

    @telethon_client.on(
        events.NewMessage()
    )
    async def new_telegram_message(event):

        try:

            chat = await event.get_chat()

            title = getattr(
                chat,
                "title",
                None
            )

            if title != STORAGE_CHANNEL_NAME:
                return

            await channel_video_handler(
                event
            )

        except Exception as e:

            print(
                "❌ Telegram event error:"
            )

            print(
                f"{type(e).__name__}: {str(e)}"
            )

    # --------------------------------------------------------
    # Start bot
    # --------------------------------------------------------

    print(
        f"Bot is running. "
        f"Health server listening on port {PORT}."
    )

    bot_application.run_polling(
        close_loop=False
    )


if __name__ == "__main__":
    main()
