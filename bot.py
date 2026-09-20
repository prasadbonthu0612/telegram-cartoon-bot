import os
import threading
import json
import re
import shutil
import subprocess
import tempfile

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

# ============================================================
# TELEGRAM STORAGE MARKERS
# ============================================================

QUEUE_MARKER = "[QUEUE_MANIFEST]"
CONFIG_MARKER = "[BOT_CONFIG]"
TITLE_REQUEST_MARKER = "[TITLE_REQUEST]"
PROCESSING_MARKER = "[PROCESSING]"

CLIP_MARKER = "[CLIP]"


# ============================================================
# GLOBALS
# ============================================================

telethon_client = None
bot_application = None

admin_chat_id = None
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
# SAVE ADMIN CHAT ID
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

    config_text = (
        f"{CONFIG_MARKER}\n"
        f"{json.dumps({'admin_chat_id': chat_id})}"
    )

    await telethon_client.send_message(
        storage_channel,
        config_text
    )


# ============================================================
# LOAD ADMIN CHAT ID
# ============================================================

async def load_admin_chat_id():

    global admin_chat_id

    if admin_chat_id:
        return admin_chat_id

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
# START
# ============================================================

async def start(
    update: Update,
    context: ContextTypes.DEFAULT_TYPE
):

    try:

        chat_id = update.effective_chat.id

        await save_admin_chat_id(
            chat_id
        )

        await update.message.reply_text(
            "👋 Hello!\n\n"
            "✅ Your Telegram account is connected.\n\n"
            "🎬 Upload the ORIGINAL video "
            "directly to Cartoon Clip Storage.\n\n"
            "I'll detect it automatically and "
            "ask for the title."
        )

    except Exception as e:

        await update.message.reply_text(
            "❌ Could not save configuration.\n\n"
            f"{type(e).__name__}: {str(e)}"
        )


# ============================================================
# TELEGRAM CONNECTION TEST
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
            "❌ Telethon connection failed:\n"
            f"{type(e).__name__}: {str(e)}"
        )


# ============================================================
# TITLE REQUEST
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


async def delete_title_request():

    storage_channel = (
        await find_storage_channel()
    )

    if storage_channel is None:
        return

    messages = await telethon_client.get_messages(
        storage_channel,
        limit=100
    )

    for message in messages:

        text = message.message or ""

        if text.startswith(
            TITLE_REQUEST_MARKER
        ):

            await telethon_client.delete_messages(
                storage_channel,
                message.id
            )

            return


# ============================================================
# PROCESSING STATE
# ============================================================

async def save_processing_state(
    video_message_id,
    title
):

    storage_channel = (
        await find_storage_channel()
    )

    if storage_channel is None:
        return

    state = {
        "video_message_id": video_message_id,
        "title": title,
        "started_at": datetime.now(
            timezone.utc
        ).isoformat()
    }

    text = (
        f"{PROCESSING_MARKER}\n"
        f"{json.dumps(state, indent=2)}"
    )

    messages = await telethon_client.get_messages(
        storage_channel,
        limit=100
    )

    for message in messages:

        existing = message.message or ""

        if existing.startswith(
            PROCESSING_MARKER
        ):

            await telethon_client.edit_message(
                storage_channel,
                message.id,
                text
            )

            return

    await telethon_client.send_message(
        storage_channel,
        text
    )


async def delete_processing_state():

    storage_channel = (
        await find_storage_channel()
    )

    if storage_channel is None:
        return

    messages = await telethon_client.get_messages(
        storage_channel,
        limit=100
    )

    for message in messages:

        text = message.message or ""

        if text.startswith(
            PROCESSING_MARKER
        ):

            await telethon_client.delete_messages(
                storage_channel,
                message.id
            )

            return


# ============================================================
# SAFE FILENAME
# ============================================================

def safe_filename(text):

    text = text.strip()

    text = re.sub(
        r'[<>:"/\\|?*\x00-\x1F]',
        "",
        text
    )

    text = re.sub(
        r"\s+",
        " ",
        text
    )

    if not text:
        text = "Video"

    return text[:150]


# ============================================================
# SPLIT VIDEO
# ============================================================

def split_video(
    input_path,
    output_directory
):

    os.makedirs(
        output_directory,
        exist_ok=True
    )

    output_pattern = os.path.join(
        output_directory,
        "part_%03d.mp4"
    )

    command = [
        "ffmpeg",
        "-y",

        "-i",
        input_path,

        # Video
        "-map",
        "0:v:0",

        # Audio if present
        "-map",
        "0:a:0?",

        # Re-encode so segments can be
        # cleanly cut around ~40 seconds.
        "-c:v",
        "libx264",

        "-preset",
        "veryfast",

        "-crf",
        "23",

        "-c:a",
        "aac",

        "-b:a",
        "128k",

        "-movflags",
        "+faststart",

        "-f",
        "segment",

        "-segment_time",
        "40",

        "-reset_timestamps",
        "1",

        "-segment_format",
        "mp4",

        output_pattern
    ]

    print(
        "Running FFmpeg:"
    )

    print(
        " ".join(command)
    )

    result = subprocess.run(
        command,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        text=True
    )

    if result.returncode != 0:

        print(
            "FFmpeg ERROR:"
        )

        print(
            result.stderr
        )

        raise RuntimeError(
            "FFmpeg failed to split the video."
        )

    clips = []

    for filename in os.listdir(
        output_directory
    ):

        if not filename.endswith(
            ".mp4"
        ):
            continue

        if not filename.startswith(
            "part_"
        ):
            continue

        clips.append(
            os.path.join(
                output_directory,
                filename
            )
        )

    clips.sort()

    if not clips:

        raise RuntimeError(
            "FFmpeg completed but produced no clips."
        )

    return clips


# ============================================================
# CREATE QUEUE MANIFEST
# ============================================================

async def create_automatic_queue(
    storage_channel,
    title,
    original_message_id,
    uploaded_clips
):

    now = datetime.now(
        timezone.utc
    ).isoformat()

    queue_id = (
        f"queue_{now}"
    )

    queue = {
        "queue_id": queue_id,
        "title": title,
        "created_at": now,
        "status": "PENDING",
        "original_telegram_message_id":
            original_message_id,
        "total_clips":
            len(uploaded_clips),
        "next_clip_index": 1,
        "clips": []
    }

    for index, message in enumerate(
        uploaded_clips,
        start=1
    ):

        queue["clips"].append({

            "index": index,

            "telegram_message_id":
                message.id,

            "filename":
                message.file.name
                if message.file
                and message.file.name
                else f"Part {index}.mp4",

            "status":
                "PENDING",

            "instagram_status":
                "NOT_POSTED",

            "instagram_media_id":
                None,

            "posted_at":
                None,

            "deleted_from_telegram":
                False
        })

    manifest_json = json.dumps(
        queue,
        indent=2,
        ensure_ascii=False
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

    return queue, manifest_message


# ============================================================
# PROCESS ORIGINAL VIDEO
# ============================================================

async def process_original_video(
    video_message_id,
    title,
    admin_chat_id
):

    storage_channel = (
        await find_storage_channel()
    )

    if storage_channel is None:

        raise RuntimeError(
            f'Could not find "{STORAGE_CHANNEL_NAME}".'
        )

    await save_processing_state(
        video_message_id,
        title
    )

    temp_directory = tempfile.mkdtemp(
        prefix="cartoon_bot_"
    )

    try:

        # ----------------------------------------------------
        # Find original
        # ----------------------------------------------------

        print(
            f"Looking for original video "
            f"message ID {video_message_id}"
        )

        original_message = (
            await telethon_client.get_messages(
                storage_channel,
                ids=video_message_id
            )
        )

        if not original_message:

            raise RuntimeError(
                "Original video message could not be found."
            )

        if not original_message.video:

            raise RuntimeError(
                "The stored message is not a video."
            )

        # ----------------------------------------------------
        # Download original
        # ----------------------------------------------------

        original_path = os.path.join(
            temp_directory,
            "original.mp4"
        )

        await bot_application.bot.send_message(
            chat_id=admin_chat_id,
            text=(
                "⏳ PROCESSING STARTED\n\n"
                f"🎬 {title}\n\n"
                "📥 Downloading original video..."
            )
        )

        print(
            "Downloading original..."
        )

        downloaded_path = (
            await telethon_client.download_media(
                original_message,
                file=original_path
            )
        )

        if not downloaded_path:
            raise RuntimeError(
                "Failed to download original video."
            )

        # ----------------------------------------------------
        # Split
        # ----------------------------------------------------

        clips_directory = os.path.join(
            temp_directory,
            "clips"
        )

        await bot_application.bot.send_message(
            chat_id=admin_chat_id,
            text=(
                "✂️ Splitting video into "
                "approximately 40-second parts..."
            )
        )

        clip_paths = split_video(
            original_path,
            clips_directory
        )

        print(
            f"FFmpeg created {len(clip_paths)} clips."
        )

        await bot_application.bot.send_message(
            chat_id=admin_chat_id,
            text=(
                f"✂️ Split complete!\n\n"
                f"🎬 Total parts: {len(clip_paths)}\n\n"
                "📤 Uploading clips to Telegram..."
            )
        )

        # ----------------------------------------------------
        # Upload clips
        # ----------------------------------------------------

        safe_title = safe_filename(
            title
        )

        uploaded_messages = []

        for index, clip_path in enumerate(
            clip_paths,
            start=1
        ):

            filename = (
                f"{safe_title} "
                f"Part {index}.mp4"
            )

            final_path = os.path.join(
                clips_directory,
                filename
            )

            # Rename temporary FFmpeg output.
            os.rename(
                clip_path,
                final_path
            )

            caption = (
                f"{CLIP_MARKER}\n"
                f"Title: {title}\n"
                f"Part: {index}/{len(clip_paths)}"
            )

            print(
                f"Uploading {filename}..."
            )

            uploaded_message = (
                await telethon_client.send_file(
                    storage_channel,
                    final_path,
                    caption=caption,
                    force_document=False,
                    supports_streaming=True
                )
            )

            # send_file can return a single Message
            # or a list depending on the input.
            if isinstance(
                uploaded_message,
                list
            ):

                if not uploaded_message:
                    raise RuntimeError(
                        f"Upload returned no message "
                        f"for Part {index}."
                    )

                uploaded_message = (
                    uploaded_message[0]
                )

            uploaded_messages.append(
                uploaded_message
            )

            print(
                f"Uploaded Part {index}: "
                f"Telegram message ID "
                f"{uploaded_message.id}"
            )

        # ----------------------------------------------------
        # SAFETY CHECK
        # ----------------------------------------------------

        if len(uploaded_messages) != len(
            clip_paths
        ):

            raise RuntimeError(
                "Not all clips were uploaded. "
                "Original will NOT be deleted."
            )

        # ----------------------------------------------------
        # Create persistent queue
        # ----------------------------------------------------

        queue, manifest_message = (
            await create_automatic_queue(
                storage_channel,
                title,
                video_message_id,
                uploaded_messages
            )
        )

        print(
            f"Queue created: "
            f"{queue['queue_id']}"
        )

        print(
            f"Manifest message ID: "
            f"{manifest_message.id}"
        )

        # ----------------------------------------------------
        # ONLY NOW delete original
        # ----------------------------------------------------

        print(
            "All clips successfully uploaded."
        )

        print(
            "Deleting original video..."
        )

        await telethon_client.delete_messages(
            storage_channel,
            video_message_id
        )

        print(
            "Original video deleted."
        )

        # ----------------------------------------------------
        # Cleanup state
        # ----------------------------------------------------

        await delete_title_request()
        await delete_processing_state()

        # ----------------------------------------------------
        # Notify user
        # ----------------------------------------------------

        await bot_application.bot.send_message(
            chat_id=admin_chat_id,
            text=(
                "✅ VIDEO PROCESSING COMPLETE!\n\n"
                f"🎬 {title}\n\n"
                f"✂️ Parts created: "
                f"{len(uploaded_messages)}\n"
                "📤 All parts uploaded to Telegram\n"
                "🗑️ Original video deleted\n\n"
                "📋 Instagram queue created.\n\n"
                "⏳ Waiting for the Instagram "
                "automation to publish the clips."
            )
        )

    except Exception as e:

        print(
            "❌ VIDEO PROCESSING FAILED"
        )

        print(
            f"{type(e).__name__}: {str(e)}"
        )

        # IMPORTANT:
        # We deliberately DO NOT delete the original.
        # It remains available for recovery.

        await bot_application.bot.send_message(
            chat_id=admin_chat_id,
            text=(
                "❌ VIDEO PROCESSING FAILED\n\n"
                f"🎬 {title}\n\n"
                f"Error: {type(e).__name__}\n"
                f"{str(e)}\n\n"
                "⚠️ The original video was NOT deleted.\n"
                "Your video is still safe in "
                "Cartoon Clip Storage."
            )
        )

    finally:

        # Delete temporary Render files.
        try:

            shutil.rmtree(
                temp_directory,
                ignore_errors=True
            )

        except Exception:
            pass


# ============================================================
# CHANNEL VIDEO DETECTOR
# ============================================================

async def channel_video_handler(event):

    global pending_video_message_id

    try:

        message = event.message

        if not message.video:
            return

        caption = message.message or ""

        # Ignore generated clips.
        if caption.startswith(
            CLIP_MARKER
        ):
            return

        # Ignore queue manifests.
        if caption.startswith(
            QUEUE_MARKER
        ):
            return

        # Ignore configuration.
        if caption.startswith(
            CONFIG_MARKER
        ):
            return

        # Ignore title state.
        if caption.startswith(
            TITLE_REQUEST_MARKER
        ):
            return

        # Ignore processing state.
        if caption.startswith(
            PROCESSING_MARKER
        ):
            return

        print(
            "🎬 NEW ORIGINAL VIDEO DETECTED"
        )

        print(
            f"Telegram message ID: {message.id}"
        )

        chat_id = await load_admin_chat_id()

        if not chat_id:

            print(
                "⚠️ Admin chat ID not found."
            )

            print(
                "Send /start to the bot first."
            )

            return

        await save_title_request(
            message.id
        )

        pending_video_message_id = (
            message.id
        )

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


# ============================================================
# SEND TITLE QUESTION
# ============================================================

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

    if title.startswith("/"):
        return

    chat_id = update.effective_chat.id

    saved_chat_id = (
        await load_admin_chat_id()
    )

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
        "🚀 Starting automatic processing..."
    )

    # Start processing.
    #
    # We await it here so the job is tracked by
    # the Telegram bot event loop.
    await process_original_video(
        video_message_id,
        title,
        chat_id
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
        "Please upload the ORIGINAL video "
        "directly into Cartoon Clip Storage."
    )


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
    # Health server
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
            "queue",
            show_queue
        )
    )

    bot_application.add_handler(
        MessageHandler(
            filters.VIDEO,
            handle_video
        )
    )

    bot_application.add_handler(
        MessageHandler(
            filters.TEXT
            & ~filters.COMMAND,
            handle_title
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
    # Start
    # --------------------------------------------------------

    print(
        f"Bot is running. "
        f"Health server listening on port {PORT}."
    )

    bot_application.run_polling(
        close_loop=False
    )


# ============================================================
# ENTRY POINT
# ============================================================

if __name__ == "__main__":
    main()
