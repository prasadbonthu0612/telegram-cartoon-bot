import os
import asyncio
import threading
import json
import re
import shutil
import subprocess
import tempfile
import time
import uuid
import urllib.parse
import urllib.request

from datetime import datetime, timezone
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

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

# Instagram / Meta
INSTAGRAM_ACCESS_TOKEN = os.getenv("INSTAGRAM_ACCESS_TOKEN")
INSTAGRAM_USER_ID = os.getenv("INSTAGRAM_USER_ID")
INSTAGRAM_API_VERSION = os.getenv("INSTAGRAM_API_VERSION", "v25.0")
INSTAGRAM_API_BASE = f"https://graph.instagram.com/{INSTAGRAM_API_VERSION}"

# Public URL used by Instagram to fetch temporary Reel videos.
# Set this in Render to your public Render URL, for example:
# https://telegram-cartoon-bot-if57.onrender.com
PUBLIC_BASE_URL = os.getenv("PUBLIC_BASE_URL", "").rstrip("/")

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

# Temporary public media registry.
# token -> {"path": local_file_path, "content_type": "video/mp4"}
public_media_files = {}
public_media_lock = threading.Lock()


# ============================================================
# HEALTH SERVER
# ============================================================

class HealthHandler(BaseHTTPRequestHandler):

    def _send_common_headers(self, content_type="text/plain"):
        self.send_header("Content-Type", content_type)
        self.send_header("Cache-Control", "no-store")

    def do_HEAD(self):
        if self.path == "/health":
            self.send_response(200)
            self._send_common_headers()
            self.end_headers()
            return

        token = self.path.split("?", 1)[0].removeprefix("/media/")
        if token and token != self.path and self._media_exists(token):
            file_path = self._get_media_path(token)
            if file_path:
                try:
                    size = os.path.getsize(file_path)
                    self.send_response(200)
                    self._send_common_headers("video/mp4")
                    self.send_header("Content-Length", str(size))
                    self.send_header("Accept-Ranges", "bytes")
                    self.end_headers()
                    return
                except OSError:
                    pass

        self.send_response(404)
        self.end_headers()

    def do_GET(self):
        request_path = self.path.split("?", 1)[0]

        if request_path == "/health":
            self.send_response(200)
            self._send_common_headers()
            self.end_headers()
            self.wfile.write(b"Bot is healthy")
            return

        if request_path.startswith("/media/"):
            token = request_path[len("/media/"):]
            self.serve_media(token)
            return

        self.send_response(404)
        self.end_headers()

    def _media_exists(self, token):
        with public_media_lock:
            entry = public_media_files.get(token)
            return bool(entry and os.path.isfile(entry["path"]))

    def _get_media_path(self, token):
        with public_media_lock:
            entry = public_media_files.get(token)
            if not entry:
                return None
            return entry["path"]

    def serve_media(self, token):
        file_path = self._get_media_path(token)

        if not file_path or not os.path.isfile(file_path):
            self.send_response(404)
            self.end_headers()
            return

        try:
            file_size = os.path.getsize(file_path)
            range_header = self.headers.get("Range")

            start_byte = 0
            end_byte = file_size - 1
            status = 200

            if range_header and range_header.startswith("bytes="):
                value = range_header[6:].split(",", 1)[0].strip()

                if "-" in value:
                    left, right = value.split("-", 1)

                    if left:
                        start_byte = int(left)
                    if right:
                        end_byte = int(right)
                    elif left:
                        # RFC 7233: open-ended range.
                        end_byte = file_size - 1

                    if not left:
                        # Suffix range: bytes=-N
                        suffix_length = int(right)
                        suffix_length = min(suffix_length, file_size)
                        start_byte = file_size - suffix_length
                        end_byte = file_size - 1

                    if (
                        start_byte < 0
                        or start_byte >= file_size
                        or end_byte < start_byte
                    ):
                        self.send_response(416)
                        self.send_header(
                            "Content-Range",
                            f"bytes */{file_size}"
                        )
                        self.end_headers()
                        return

                    end_byte = min(end_byte, file_size - 1)
                    status = 206

            content_length = end_byte - start_byte + 1

            self.send_response(status)
            self._send_common_headers("video/mp4")
            self.send_header("Content-Length", str(content_length))
            self.send_header("Accept-Ranges", "bytes")

            if status == 206:
                self.send_header(
                    "Content-Range",
                    f"bytes {start_byte}-{end_byte}/{file_size}"
                )

            self.end_headers()

            with open(file_path, "rb") as media_file:
                media_file.seek(start_byte)
                remaining = content_length

                while remaining > 0:
                    chunk = media_file.read(
                        min(1024 * 1024, remaining)
                    )
                    if not chunk:
                        break

                    self.wfile.write(chunk)
                    remaining -= len(chunk)

        except (BrokenPipeError, ConnectionResetError):
            # Instagram/client disconnected before reading the whole file.
            pass
        except Exception as e:
            print(
                f"❌ Public media serving error: "
                f"{type(e).__name__}: {str(e)}"
            )

    def log_message(self, format, *args):
        return


def start_health_server():

    server = ThreadingHTTPServer(
        ("0.0.0.0", PORT),
        HealthHandler
    )

    print(
        f"Public HTTP server listening on port {PORT}."
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
# INSTAGRAM API
# ============================================================

def instagram_api_get(path, params=None):
    """
    Make a GET request to the current Instagram API using the
    Instagram Login access token stored in Render environment variables.
    """
    if not INSTAGRAM_ACCESS_TOKEN:
        raise RuntimeError("INSTAGRAM_ACCESS_TOKEN is missing.")

    if not INSTAGRAM_USER_ID:
        raise RuntimeError("INSTAGRAM_USER_ID is missing.")

    params = dict(params or {})
    params["access_token"] = INSTAGRAM_ACCESS_TOKEN

    url = f"{INSTAGRAM_API_BASE}/{path.lstrip('/')}"
    url += "?" + urllib.parse.urlencode(params)

    request = urllib.request.Request(
        url,
        headers={
            "User-Agent": "Cartoon-Instagram-Bot/1.0"
        },
        method="GET",
    )

    try:
        with urllib.request.urlopen(request, timeout=30) as response:
            body = response.read().decode("utf-8")
            return response.status, json.loads(body)

    except urllib.error.HTTPError as e:
        body = e.read().decode("utf-8", errors="replace")

        try:
            data = json.loads(body)
        except Exception:
            data = {"raw": body}

        raise RuntimeError(
            f"Instagram API HTTP {e.code}: {json.dumps(data, ensure_ascii=False)}"
        )

    except Exception as e:
        raise RuntimeError(
            f"Instagram API request failed: {type(e).__name__}: {str(e)}"
        )


def instagram_api_post(path, params=None):
    """
    Make a form-encoded POST request to the Instagram Graph API.
    The access token is stored only in Render environment variables.
    """
    if not INSTAGRAM_ACCESS_TOKEN:
        raise RuntimeError("INSTAGRAM_ACCESS_TOKEN is missing.")

    if not INSTAGRAM_USER_ID:
        raise RuntimeError("INSTAGRAM_USER_ID is missing.")

    params = dict(params or {})
    params["access_token"] = INSTAGRAM_ACCESS_TOKEN

    url = f"{INSTAGRAM_API_BASE}/{path.lstrip('/')}"

    body = urllib.parse.urlencode(params).encode("utf-8")

    request = urllib.request.Request(
        url,
        data=body,
        headers={
            "User-Agent": "Cartoon-Instagram-Bot/1.0",
            "Content-Type": "application/x-www-form-urlencoded",
        },
        method="POST",
    )

    try:
        with urllib.request.urlopen(request, timeout=60) as response:
            response_body = response.read().decode("utf-8")
            return response.status, json.loads(response_body)

    except urllib.error.HTTPError as e:
        body_text = e.read().decode("utf-8", errors="replace")

        try:
            data = json.loads(body_text)
        except Exception:
            data = {"raw": body_text}

        raise RuntimeError(
            f"Instagram API HTTP {e.code}: "
            f"{json.dumps(data, ensure_ascii=False)}"
        )

    except Exception as e:
        raise RuntimeError(
            f"Instagram API POST failed: "
            f"{type(e).__name__}: {str(e)}"
        )


def register_public_media(file_path):
    """
    Register one local MP4 so Instagram can fetch it over HTTPS.
    Returns (token, public_url).
    """
    if not PUBLIC_BASE_URL:
        raise RuntimeError(
            "PUBLIC_BASE_URL is missing in Render."
        )

    if not os.path.isfile(file_path):
        raise RuntimeError(
            f"Media file does not exist: {file_path}"
        )

    token = uuid.uuid4().hex

    with public_media_lock:
        public_media_files[token] = {
            "path": file_path,
            "content_type": "video/mp4",
        }

    public_url = f"{PUBLIC_BASE_URL}/media/{token}"

    print(f"🌐 Temporary public media URL created: {public_url}")

    return token, public_url


def unregister_public_media(token):
    if not token:
        return

    with public_media_lock:
        public_media_files.pop(token, None)

    print(f"🗑️ Temporary public media URL removed: {token}")


def publish_reel_from_file(file_path, title, part_index, total_parts):
    """
    Upload one local video to Instagram as a Reel using the
    URL-based Instagram publishing flow.

    Important:
    Instagram fetches the video from the temporary public URL.
    The Telegram clip is NOT deleted by this function.
    """
    token = None

    try:
        token, public_url = register_public_media(file_path)

        caption = (
            f"{title}\n\n"
            f"Part {part_index}/{total_parts}"
        )

        print(
            f"📤 Creating Instagram Reel container "
            f"for Part {part_index}/{total_parts}..."
        )

        _, container_data = instagram_api_post(
            f"{INSTAGRAM_USER_ID}/media",
            {
                "media_type": "REELS",
                "video_url": public_url,
                "caption": caption,
                "share_to_feed": "true",
            }
        )

        creation_id = container_data.get("id")

        if not creation_id:
            raise RuntimeError(
                "Instagram did not return a creation/container ID.\n"
                f"Response: {json.dumps(container_data, ensure_ascii=False)}"
            )

        print(
            f"✅ Instagram container created: {creation_id}"
        )

        # Instagram needs time to download/transcode the video.
        # Poll until the container is ready.
        max_attempts = 60
        poll_seconds = 5

        for attempt in range(1, max_attempts + 1):

            time.sleep(poll_seconds)

            _, status_data = instagram_api_get(
                creation_id,
                {
                    "fields": "status_code,status"
                }
            )

            status_code = (
                status_data.get("status_code")
                or status_data.get("status")
                or ""
            )

            print(
                f"📡 Instagram processing status "
                f"{attempt}/{max_attempts}: {status_code}"
            )

            if status_code == "FINISHED":
                break

            if status_code in {
                "ERROR",
                "EXPIRED",
                "FAILED",
            }:
                raise RuntimeError(
                    "Instagram video processing failed.\n"
                    f"Status: {json.dumps(status_data, ensure_ascii=False)}"
                )

        else:
            raise RuntimeError(
                "Instagram video processing timed out after "
                f"{max_attempts * poll_seconds} seconds."
            )

        print(
            f"🚀 Publishing Instagram Reel: {creation_id}"
        )

        _, publish_data = instagram_api_post(
            f"{INSTAGRAM_USER_ID}/media_publish",
            {
                "creation_id": creation_id
            }
        )

        media_id = publish_data.get("id")

        if not media_id:
            raise RuntimeError(
                "Instagram did not return a published media ID.\n"
                f"Response: {json.dumps(publish_data, ensure_ascii=False)}"
            )

        print(
            f"🎉 Instagram Reel published successfully: {media_id}"
        )

        return media_id

    finally:
        # The file itself is deleted by the caller's temp-directory
        # cleanup. The public route disappears immediately.
        unregister_public_media(token)


async def test_instagram(
    update: Update,
    context: ContextTypes.DEFAULT_TYPE
):
    """
    Verify the Instagram access token and Instagram User ID.
    The token itself is never sent to Telegram.
    """
    try:
        if not INSTAGRAM_ACCESS_TOKEN:
            await update.message.reply_text(
                "❌ INSTAGRAM_ACCESS_TOKEN is missing in Render."
            )
            return

        if not INSTAGRAM_USER_ID:
            await update.message.reply_text(
                "❌ INSTAGRAM_USER_ID is missing in Render."
            )
            return

        await update.message.reply_text(
            "🔎 Checking Instagram API connection..."
        )

        status_code, data = instagram_api_get(
            INSTAGRAM_USER_ID,
            {
                "fields": "id,username"
            }
        )

        instagram_id = data.get("id")
        username = data.get("username")

        await update.message.reply_text(
            "✅ INSTAGRAM API CONNECTION WORKS!\n\n"
            f"Username: @{username}\n"
            f"Instagram User ID: {instagram_id}\n"
            f"API version: {INSTAGRAM_API_VERSION}\n"
            f"HTTP status: {status_code}\n\n"
            "🔐 Access token was NOT displayed."
        )

    except Exception as e:
        await update.message.reply_text(
            "❌ INSTAGRAM API TEST FAILED\n\n"
            f"Error: {type(e).__name__}\n"
            f"{str(e)}"
        )


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

            # FIX:
            # Generate the filename ourselves instead
            # of relying on Telegram's reported filename.
            "filename":
                f"{safe_filename(title)} Part {index}.mp4",

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
# INSTAGRAM QUEUE PUBLISHER
# ============================================================

async def find_queue_manifests():
    storage_channel = await find_storage_channel()

    if storage_channel is None:
        return []

    messages = await telethon_client.get_messages(
        storage_channel,
        limit=200
    )

    manifests = []

    for message in messages:
        text = message.message or ""

        if not text.startswith(QUEUE_MARKER):
            continue

        try:
            json_text = text.split("\n\n", 2)[-1]
            queue = json.loads(json_text)

            manifests.append(
                {
                    "message": message,
                    "queue": queue,
                }
            )

        except Exception as e:
            print(
                f"⚠️ Could not parse queue manifest "
                f"{message.id}: {type(e).__name__}: {str(e)}"
            )

    return manifests


async def save_queue_manifest(manifest_message, queue):
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

    storage_channel = await find_storage_channel()

    if storage_channel is None:
        raise RuntimeError(
            f'Could not find "{STORAGE_CHANNEL_NAME}".'
        )

    await telethon_client.edit_message(
        storage_channel,
        manifest_message.id,
        manifest_text
    )


async def publish_one_queue_clip(
    manifest_message,
    queue,
    clip
):
    storage_channel = await find_storage_channel()

    if storage_channel is None:
        raise RuntimeError(
            f'Could not find "{STORAGE_CHANNEL_NAME}".'
        )

    clip_index = clip.get("index")
    telegram_message_id = clip.get("telegram_message_id")
    title = queue.get("title", "Cartoon")

    if not telegram_message_id:
        raise RuntimeError(
            f"Clip {clip_index} has no Telegram message ID."
        )

    print(
        f"📦 Loading Telegram clip {clip_index} "
        f"(message {telegram_message_id})..."
    )

    message = await telethon_client.get_messages(
        storage_channel,
        ids=telegram_message_id
    )

    if not message:
        raise RuntimeError(
            f"Telegram clip message {telegram_message_id} "
            f"could not be found."
        )

    if not message.video:
        raise RuntimeError(
            f"Telegram message {telegram_message_id} "
            f"is not a video."
        )

    temp_directory = tempfile.mkdtemp(
        prefix="instagram_publish_"
    )

    try:
        safe_name = safe_filename(
            clip.get("filename")
            or f"{title} Part {clip_index}.mp4"
        )

        if not safe_name.lower().endswith(".mp4"):
            safe_name += ".mp4"

        local_path = os.path.join(
            temp_directory,
            safe_name
        )

        print(
            f"📥 Downloading Telegram clip "
            f"{clip_index}/{queue.get('total_clips')}..."
        )

        downloaded = await telethon_client.download_media(
            message,
            file=local_path
        )

        if not downloaded:
            raise RuntimeError(
                f"Failed to download Telegram clip {clip_index}."
            )

        media_id = await asyncio.to_thread(
            publish_reel_from_file,
            local_path,
            title,
            clip_index,
            queue.get("total_clips", 1)
        )

        # Instagram has confirmed publication. Mark it as published
        # BEFORE attempting Telegram cleanup, so a Telegram deletion
        # problem can never cause a duplicate Instagram post.
        clip["status"] = "PUBLISHED"
        clip["instagram_status"] = "PUBLISHED"
        clip["instagram_media_id"] = media_id
        clip["posted_at"] = datetime.now(
            timezone.utc
        ).isoformat()

        try:
            # ONLY now delete the Telegram clip.
            await telethon_client.delete_messages(
                storage_channel,
                telegram_message_id
            )
            clip["deleted_from_telegram"] = True

        except Exception as delete_error:
            # Publication succeeded, so do not retry Instagram.
            clip["deleted_from_telegram"] = False

            print(
                "⚠️ Instagram publication succeeded but Telegram "
                "clip deletion failed:\n"
                f"{type(delete_error).__name__}: {str(delete_error)}"
            )

        return media_id

    except Exception:
        # Keep the Telegram clip for retry when Instagram publication
        # itself fails.
        clip["status"] = "FAILED"
        clip["instagram_status"] = "FAILED"
        raise

    finally:
        shutil.rmtree(
            temp_directory,
            ignore_errors=True
        )


async def process_pending_queues():
    """
    Scan Telegram queue manifests and publish one pending/failed
    clip at a time.

    A failed clip remains in Telegram and can be retried on the
    next scan.
    """
    if not INSTAGRAM_ACCESS_TOKEN or not INSTAGRAM_USER_ID:
        print(
            "⚠️ Instagram credentials are missing. "
            "Queue publisher is disabled."
        )
        return

    if not PUBLIC_BASE_URL:
        print(
            "⚠️ PUBLIC_BASE_URL is missing. "
            "Queue publisher is disabled."
        )
        return

    try:
        manifests = await find_queue_manifests()

        for item in manifests:
            manifest_message = item["message"]
            queue = item["queue"]

            queue_status = queue.get("status", "PENDING")

            if queue_status == "COMPLETED":
                continue

            clips = queue.get("clips", [])

            # Find the first clip that has not been successfully
            # published yet.
            target_clip = None

            for clip in clips:
                if clip.get("instagram_status") == "PUBLISHED":
                    continue

                target_clip = clip
                break

            if target_clip is None:
                queue["status"] = "COMPLETED"
                queue["next_clip_index"] = (
                    queue.get("total_clips", len(clips)) + 1
                )

                await save_queue_manifest(
                    manifest_message,
                    queue
                )

                if admin_chat_id:
                    await bot_application.bot.send_message(
                        chat_id=admin_chat_id,
                        text=(
                            "🎉 INSTAGRAM QUEUE COMPLETE!\n\n"
                            f"🎬 {queue.get('title', 'Cartoon')}\n"
                            f"📤 Published: {queue.get('total_clips', len(clips))}\n"
                            "🗑️ Successfully published clips "
                            "were removed from Telegram."
                        )
                    )

                continue

            clip_index = target_clip.get("index")

            print(
                f"🚀 Starting Instagram queue item "
                f"{queue.get('queue_id')} "
                f"Part {clip_index}."
            )

            target_clip["status"] = "PUBLISHING"
            target_clip["instagram_status"] = "PUBLISHING"

            await save_queue_manifest(
                manifest_message,
                queue
            )

            try:
                media_id = await publish_one_queue_clip(
                    manifest_message,
                    queue,
                    target_clip
                )

                queue["next_clip_index"] = (
                    int(clip_index) + 1
                )

                # Check whether all clips are now published.
                all_published = all(
                    clip.get("instagram_status") == "PUBLISHED"
                    for clip in clips
                )

                if all_published:
                    queue["status"] = "COMPLETED"

                await save_queue_manifest(
                    manifest_message,
                    queue
                )

                if admin_chat_id:
                    await bot_application.bot.send_message(
                        chat_id=admin_chat_id,
                        text=(
                            "✅ INSTAGRAM REEL PUBLISHED!\n\n"
                            f"🎬 {queue.get('title', 'Cartoon')}\n"
                            f"📌 Part: {clip_index}/{queue.get('total_clips', len(clips))}\n"
                            f"🆔 Instagram Media ID: {media_id}\n\n"
                            + (
                                "🗑️ Telegram clip deleted after successful publishing."
                                if target_clip.get("deleted_from_telegram")
                                else "⚠️ Instagram published successfully, but Telegram cleanup failed."
                            )
                        )
                    )

                # Publish only one clip per scan. This avoids
                # hammering the API and makes failures isolated.
                return

            except Exception as e:
                target_clip["status"] = "FAILED"
                target_clip["instagram_status"] = "FAILED"

                await save_queue_manifest(
                    manifest_message,
                    queue
                )

                print(
                    "❌ Instagram publishing failed.\n"
                    f"{type(e).__name__}: {str(e)}"
                )

                if admin_chat_id:
                    await bot_application.bot.send_message(
                        chat_id=admin_chat_id,
                        text=(
                            "❌ INSTAGRAM REEL PUBLISH FAILED\n\n"
                            f"🎬 {queue.get('title', 'Cartoon')}\n"
                            f"📌 Part: {clip_index}/{queue.get('total_clips', len(clips))}\n"
                            f"Error: {type(e).__name__}\n"
                            f"{str(e)}\n\n"
                            "⚠️ The Telegram clip was NOT deleted.\n"
                            "It will remain available for retry."
                        )
                    )

                return

    except Exception as e:
        print(
            "❌ Queue publisher scan failed:\n"
            f"{type(e).__name__}: {str(e)}"
        )


async def instagram_queue_loop():
    """
    Background loop for the Instagram queue.

    It checks Telegram for pending queue manifests every minute.
    """
    await asyncio.sleep(15)

    while True:
        try:
            await process_pending_queues()
        except Exception as e:
            print(
                "❌ Instagram queue loop error:\n"
                f"{type(e).__name__}: {str(e)}"
            )

        await asyncio.sleep(60)



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

async def publish_queue_now(
    update: Update,
    context: ContextTypes.DEFAULT_TYPE
):
    chat_id = update.effective_chat.id
    saved_chat_id = await load_admin_chat_id()

    if saved_chat_id != chat_id:
        return

    await update.message.reply_text(
        "🚀 Starting Instagram queue check..."
    )

    await process_pending_queues()

    await update.message.reply_text(
        "✅ Instagram queue check finished."
    )



async def test_public_url(
    update: Update,
    context: ContextTypes.DEFAULT_TYPE
):
    chat_id = update.effective_chat.id
    saved_chat_id = await load_admin_chat_id()

    if saved_chat_id != chat_id:
        return

    if not PUBLIC_BASE_URL:
        await update.message.reply_text(
            "❌ PUBLIC_BASE_URL is missing in Render."
        )
        return

    try:
        url = f"{PUBLIC_BASE_URL}/health"

        request = urllib.request.Request(
            url,
            headers={
                "User-Agent": "Cartoon-Instagram-Bot/1.0"
            },
            method="GET"
        )

        with urllib.request.urlopen(
            request,
            timeout=30
        ) as response:
            body = response.read().decode("utf-8", errors="replace")

        await update.message.reply_text(
            "✅ PUBLIC URL WORKS!\n\n"
            f"URL: {PUBLIC_BASE_URL}\n"
            f"HTTP status: {response.status}\n"
            f"Response: {body}"
        )

    except Exception as e:
        await update.message.reply_text(
            "❌ PUBLIC URL TEST FAILED\n\n"
            f"{type(e).__name__}: {str(e)}"
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

async def application_post_init(application):
    """
    Start the Instagram queue worker after python-telegram-bot
    has initialized its asyncio event loop.
    """
    application.create_task(
        instagram_queue_loop(),
        update=None
    )


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

    # Instagram credentials are intentionally checked by
    # /test_instagram so the existing Telegram pipeline can
    # still start while Instagram setup is being verified.

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
        .post_init(application_post_init)
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
            "test_instagram",
            test_instagram
        )
    )

    bot_application.add_handler(
        CommandHandler(
            "queue",
            show_queue
        )
    )

    bot_application.add_handler(
        CommandHandler(
            "publish_queue",
            publish_queue_now
        )
    )

    bot_application.add_handler(
        CommandHandler(
            "test_public_url",
            test_public_url
        )
    )

    bot_application.add_handler(
        CommandHandler(
            "publish_queue",
            publish_queue_now
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
