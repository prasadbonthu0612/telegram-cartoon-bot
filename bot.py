import os
import threading
import tempfile
import subprocess
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


async def download_latest_storage_video():

    storage_channel = await find_storage_channel()

    if storage_channel is None:
        raise RuntimeError(
            f'Could not find "{STORAGE_CHANNEL_NAME}".'
        )

    messages = await telethon_client.get_messages(
        storage_channel,
        limit=20
    )

    if not messages:
        raise RuntimeError(
            "No messages found in storage channel."
        )

    video_message = None

    for message in messages:

        if getattr(message, "video", None):
            video_message = message
            break

    if video_message is None:
        raise RuntimeError(
            "No video found in storage channel."
        )

    temp_dir = tempfile.gettempdir()

    input_path = os.path.join(
        temp_dir,
        f"cartoon_input_{video_message.id}.mp4"
    )

    print(
        f"Downloading storage video "
        f"message {video_message.id}"
    )

    downloaded_file = await telethon_client.download_media(
        video_message,
        file=input_path
    )

    if not downloaded_file:
        raise RuntimeError(
            "Video download returned no file."
        )

    return downloaded_file


def split_video(input_path):

    output_dir = tempfile.mkdtemp(
        prefix="cartoon_clips_"
    )

    output_pattern = os.path.join(
        output_dir,
        "clip_%03d.mp4"
    )

    command = [
        "ffmpeg",
        "-y",
        "-i",
        input_path,

        # Split every 40 seconds
        "-map",
        "0",

        "-c",
        "copy",

        "-f",
        "segment",

        "-segment_time",
        "40",

        "-reset_timestamps",
        "1",

        output_pattern,
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
        text=True,
    )

    if result.returncode != 0:

        print(
            "FFmpeg failed:"
        )

        print(
            result.stderr
        )

        raise RuntimeError(
            "FFmpeg failed to split the video."
        )

    clips = []

    for filename in sorted(
        os.listdir(output_dir)
    ):

        if filename.endswith(".mp4"):

            clips.append(
                os.path.join(
                    output_dir,
                    filename
                )
            )

    if not clips:
        raise RuntimeError(
            "FFmpeg completed but created no clips."
        )

    return output_dir, clips


async def split_storage_test(
    update: Update,
    context: ContextTypes.DEFAULT_TYPE
):

    input_path = None
    output_dir = None

    try:

        await update.message.reply_text(
            "📦 Opening Cartoon Clip Storage..."
        )

        await update.message.reply_text(
            "🔎 Finding latest video..."
        )

        input_path = await download_latest_storage_video()

        file_size = os.path.getsize(
            input_path
        )

        file_size_mb = (
            file_size / (1024 * 1024)
        )

        await update.message.reply_text(
            "✅ Video downloaded!\n\n"
            f"📦 Size: {file_size_mb:.2f} MB\n\n"
            "✂️ Starting FFmpeg...\n"
            "Splitting into 40-second clips..."
        )

        output_dir, clips = split_video(
            input_path
        )

        clip_lines = []

        for index, clip in enumerate(
            clips,
            start=1
        ):

            clip_size = os.path.getsize(
                clip
            )

            clip_size_mb = (
                clip_size / (1024 * 1024)
            )

            clip_lines.append(
                f"Clip {index}: "
                f"{clip_size_mb:.2f} MB"
            )

        result_text = (
            "🎉 FFMPEG TEST SUCCESSFUL!\n\n"
            f"🎬 Original: "
            f"{file_size_mb:.2f} MB\n"
            f"✂️ Clips created: {len(clips)}\n\n"
            + "\n".join(clip_lines)
            + "\n\n"
            "✅ 40-second splitting works."
        )

        # Keep Telegram message reasonably short
        if len(result_text) > 3500:
            result_text = (
                "🎉 FFMPEG TEST SUCCESSFUL!\n\n"
                f"🎬 Original: "
                f"{file_size_mb:.2f} MB\n"
                f"✂️ Clips created: {len(clips)}\n\n"
                "The clips were successfully created."
            )

        await update.message.reply_text(
            result_text
        )

        print(
            f"FFmpeg created {len(clips)} clips."
        )

        for clip in clips:
            print(
                f"Created: {clip}"
            )

    except Exception as e:

        print(
            f"FFMPEG TEST ERROR: "
            f"{type(e).__name__}: {str(e)}"
        )

        await update.message.reply_text(
            "❌ FFMPEG TEST FAILED\n\n"
            f"Error: {type(e).__name__}\n"
            f"Details: {str(e)}"
        )

    finally:

        # Delete original downloaded video
        if input_path:

            try:
                if os.path.exists(input_path):
                    os.remove(input_path)

                    print(
                        "Original temporary video deleted."
                    )

            except Exception as cleanup_error:

                print(
                    f"Input cleanup warning: "
                    f"{cleanup_error}"
                )

        # Delete generated clips
        if output_dir:

            try:

                if os.path.exists(output_dir):

                    for filename in os.listdir(
                        output_dir
                    ):

                        file_path = os.path.join(
                            output_dir,
                            filename
                        )

                        if os.path.isfile(
                            file_path
                        ):
                            os.remove(
                                file_path
                            )

                    os.rmdir(
                        output_dir
                    )

                    print(
                        "Temporary clips deleted."
                    )

            except Exception as cleanup_error:

                print(
                    f"Clip cleanup warning: "
                    f"{cleanup_error}"
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
            "split_storage_test",
            split_storage_test
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
