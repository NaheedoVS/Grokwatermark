import os
import asyncio
import logging
from io import BytesIO
from hachoir.parser import createParser
from hachoir.metadata import extractMetadata
from pyrogram import Client, filters, types
from pyrogram.types import Message, CallbackQuery, InlineKeyboardMarkup, InlineKeyboardButton
from pyrogram.errors import FloodWait, UserNotParticipant

from configs import API_ID, API_HASH, BOT_TOKEN, OWNER_ID, LOG_CHANNEL, UPDATES_CHANNEL, PRESET
from db import get_user_data, set_user_data, get_text_data, set_text_data

logging.basicConfig(level=logging.INFO)
logger = logging.getLogger(__name__)

app = Client("WatermarkBot", api_id=API_ID, api_hash=API_HASH, bot_token=BOT_TOKEN)

# Simple state for pending inputs (user_id: state)
pending_states = {}

# Position map for overlays (x:y expressions safe for ffmpeg)
POSITIONS = {
    "tl": "10:10",
    "tc": "(w-text_w)/2:10",
    "tr": "main_w-text_w-10:10",
    "ml": "10:(h-text_h)/2",
    "mc": "(w-text_w)/2:(h-text_h)/2",
    "mr": "main_w-text_w-10:(h-text_h)/2",
    "bl": "10:main_h-text_h-10",
    "bc": "(w-text_w)/2:main_h-text_h-10",
    "br": "main_w-text_w-10:main_h-text_h-10",
}


@app.on_message(
    filters.video |
    (filters.document & filters.file_extension(["mp4", "mkv", "mov", "webm"]))
)
async def start_cmd(client: Client, message: Message):
    user_id = message.from_user.id
    await set_user_data(user_id, {"position": "mc", "size": 50})  # Default
    await set_text_data(user_id, {"text": "", "color": "white", "size": 24, "use": False})
    kb = InlineKeyboardMarkup([[InlineKeyboardButton("📤 Add Watermark", callback_data="add_wm")]])
    await message.reply("Welcome! Upload a video to add watermark.", reply_markup=kb)


@app.on_message(filters.video | (filters.document & filters.mime_type("video/*")))
async def process_video(client: Client, message: Message):
    user_id = message.from_user.id
    if not await check_sub(user_id):
        return

    msg = await message.reply("Processing video...")

    file_path = await message.download()
    try:
        # --- Get basic video metadata (guarding None) ---
        parser = createParser(file_path)
        metadata = extractMetadata(parser) if parser else None
        width = 1280
        height = 720
        try:
            if metadata:
                if metadata.has("width"):
                    width = int(metadata.get("width"))
                if metadata.has("height"):
                    height = int(metadata.get("height"))
        except Exception:
            # fallback to defaults
            logger.debug("Could not read width/height from metadata, using defaults")

        # Get user settings
        data = await get_user_data(user_id) or {}
        text_data = await get_text_data(user_id) or {}
        pos_key = data.get("position", "mc")
        position = POSITIONS.get(pos_key, POSITIONS["mc"])
        img_size = int(data.get("size", 50))

        wm_path = await get_user_watermark(user_id)  # Implemented elsewhere; None if not set
        text = text_data.get("text", "")
        color = text_data.get("color", "white")
        t_size = int(text_data.get("size", 24))
        use_text = bool(text_data.get("use", False))

        output_path = f"watermarked_{message.message_id}.mp4"

        # --- Build ffmpeg command ---
        # Inputs: main video always, image watermark optional
        cmd = ["ffmpeg", "-y", "-i", file_path]
        if wm_path:
            cmd.extend(["-i", wm_path])

        filters = []
        main_label = "[0:v]"

        # Text drawtext
        if use_text and text:
            # Prepare safe text for ffmpeg (escape single quotes)
            safe_text = text.replace("'", r"\'")
            # We use x/y from 'position' if it was a simple literal (e.g., "10:10"),
            # otherwise for drawtext we'll center using expressions in POSITIONS map.
            # If position contains ':' we assume it's x:y already. Extract x,y.
            if ":" in position:
                x_expr, y_expr = position.split(":", 1)
            else:
                x_expr, y_expr = position, "10"

            draw = (
                f"drawtext=fontfile=/usr/share/fonts/truetype/dejavu/DejaVuSans.ttf:"
                f"text='{safe_text}':"
                f"fontcolor={color}:"
                f"fontsize={t_size}:"
                f"x={x_expr}:"
                f"y={y_expr}"
            )
            # create labeled filter
            filters.append(f"{main_label}{draw}[base]")
            main_label = "[base]"

        # Image watermark handling
        final_label = main_label
        if wm_path:
            # scale watermark to percentage of video dimensions
            wm_w = max(1, int(width * img_size / 100))
            wm_h = max(1, int(height * img_size / 100))
            filters.append(f"[1:v]scale={wm_w}:{wm_h}[wm]")
            # overlay using the position expression
            filters.append(f"{main_label}[wm]overlay={position}[v]")
            final_label = "[v]"

        # Compose filter_complex if we have any filters
        if filters:
            filter_complex = ";".join(filters)
            cmd.extend(["-filter_complex", filter_complex, "-map", final_label, "-map", "0:a?", "-c:v", "libx264", "-preset", PRESET, "-c:a", "copy", output_path])
        else:
            # No filters: simple copy or re-encode
            cmd.extend(["-map", "0:v", "-map", "0:a?", "-c:v", "libx264", "-preset", PRESET, "-c:a", "copy", output_path])

        logger.info("Running ffmpeg: %s", " ".join(cmd))

        # Run ffmpeg asynchronously and capture stderr for diagnostics
        process = await asyncio.create_subprocess_exec(*cmd, stdout=asyncio.subprocess.PIPE, stderr=asyncio.subprocess.PIPE)
        stdout, stderr = await process.communicate()

        if process.returncode == 0:
            await msg.edit("Processing complete, uploading...")
            await client.send_video(message.chat.id, output_path)
            await msg.delete()
        else:
            logger.error("ffmpeg failed: %s", stderr.decode(errors="ignore"))
            await msg.edit("Error processing video. Check logs for details.")

    except Exception as e:
        logger.exception("Error in process_video: %s", e)
        await msg.edit("Unexpected error while processing.")
    finally:
        # Clean up temp files
        try:
            if os.path.exists(file_path):
                os.remove(file_path)
        except Exception:
            logger.debug("Could not remove input file")
        try:
            if os.path.exists(output_path):
                os.remove(output_path)
        except Exception:
            logger.debug("Could not remove output file")


@app.on_callback_query(filters.regex("settings"))
async def settings_cb(client: Client, callback: CallbackQuery):
    user_id = callback.from_user.id
    text_data = await get_text_data(user_id) or {}
    status = "Enabled" if text_data.get("use", False) else "Disabled"
    kb = InlineKeyboardMarkup([
        [InlineKeyboardButton("📝 Set Text", callback_data="set_text")],
        [InlineKeyboardButton("🎨 Set Color", callback_data="set_color")],
        [InlineKeyboardButton("📏 Set Size", callback_data="set_size")],
        [InlineKeyboardButton(f"Text Overlay: {status}", callback_data="toggle_text")],
        [InlineKeyboardButton("🔙 Back", callback_data="back")]
    ])
    await callback.edit_message_text("**Text Watermark Settings:**", reply_markup=kb)


@app.on_callback_query(filters.regex("set_text"))
async def set_text_cb(client: Client, callback: CallbackQuery):
    pending_states[callback.from_user.id] = "waiting_text"
    await callback.answer("Send your watermark text:", show_alert=True)


@app.on_callback_query(filters.regex("set_color"))
async def set_color_cb(client: Client, callback: CallbackQuery):
    kb = InlineKeyboardMarkup([
        [InlineKeyboardButton("White", callback_data="color_white"), InlineKeyboardButton("Black", callback_data="color_black")],
        [InlineKeyboardButton("Red", callback_data="color_red"), InlineKeyboardButton("Blue", callback_data="color_blue")],
        [InlineKeyboardButton("Custom Hex", callback_data="color_custom")],
        [InlineKeyboardButton("🔙 Back", callback_data="settings")]
    ])
    await callback.edit_message_text("Choose color:", reply_markup=kb)


@app.on_callback_query(filters.regex(r"color_(white|black|red|blue)"))
async def color_simple_cb(client: Client, callback: CallbackQuery):
    color_map = {"white": "white", "black": "black", "red": "#FF0000", "blue": "#0000FF"}
    color = color_map[callback.data.split("_")[1]]
    user_id = callback.from_user.id
    data = await get_text_data(user_id) or {}
    await set_text_data(user_id, {**data, "color": color})
    await callback.answer(f"Color set to {color}")
    await settings_cb(client, callback)


@app.on_callback_query(filters.regex("color_custom"))
async def color_custom_cb(client: Client, callback: CallbackQuery):
    pending_states[callback.from_user.id] = "waiting_color"
    await callback.answer("Send hex color (e.g., #FF0000):", show_alert=True)


@app.on_callback_query(filters.regex("set_size"))
async def set_size_cb(client: Client, callback: CallbackQuery):
    kb = InlineKeyboardMarkup([
        [InlineKeyboardButton("Small (20px)", callback_data="size_20"), InlineKeyboardButton("Medium (30px)", callback_data="size_30")],
        [InlineKeyboardButton("Large (40px)", callback_data="size_40"), InlineKeyboardButton("Custom", callback_data="size_custom")],
        [InlineKeyboardButton("🔙 Back", callback_data="settings")]
    ])
    await callback.edit_message_text("Choose size:", reply_markup=kb)


@app.on_callback_query(filters.regex(r"size_(\d+)"))
async def size_cb(client: Client, callback: CallbackQuery):
    size = int(callback.data.split("_")[1])
    user_id = callback.from_user.id
    data = await get_text_data(user_id) or {}
    await set_text_data(user_id, {**data, "size": size})
    await callback.answer(f"Size set to {size}px")
    await settings_cb(client, callback)


@app.on_callback_query(filters.regex("size_custom"))
async def size_custom_cb(client: Client, callback: CallbackQuery):
    pending_states[callback.from_user.id] = "waiting_size"
    await callback.answer("Send size in px (e.g., 24):", show_alert=True)


@app.on_callback_query(filters.regex("toggle_text"))
async def toggle_text_cb(client: Client, callback: CallbackQuery):
    user_id = callback.from_user.id
    data = await get_text_data(user_id) or {}
    data["use"] = not data.get("use", False)
    await set_text_data(user_id, data)
    status = "Enabled" if data["use"] else "Disabled"
    await callback.answer(f"Text overlay {status}")
    await settings_cb(client, callback)


@app.on_message(filters.text & ~filters.command(["start", "settings"]))
async def handle_text_input(client: Client, message: Message):
    user_id = message.from_user.id
    if user_id not in pending_states:
        return
    state = pending_states[user_id]
    data = await get_text_data(user_id) or {}
    if state == "waiting_text":
        data["text"] = message.text
        await message.reply("Text set!")
    elif state == "waiting_color":
        if message.text.startswith("#") and len(message.text) == 7:
            data["color"] = message.text
            await message.reply("Color set!")
        else:
            await message.reply("Invalid hex. Try again.")
            return
    elif state == "waiting_size":
        try:
            size = int(message.text)
            data["size"] = size
            await message.reply(f"Size set to {size}px!")
        except ValueError:
            await message.reply("Invalid number. Try again.")
            return
    await set_text_data(user_id, data)
    del pending_states[user_id]
    await message.reply("/settings to view.")


# Helper: Check subscription
async def check_sub(user_id: int) -> bool:
    if not UPDATES_CHANNEL:
        return True
    try:
        await app.get_chat_member(int(UPDATES_CHANNEL), user_id)
        return True
    except UserNotParticipant:
        await app.send_message(user_id, "Subscribe first!")
        return False


# Placeholder for get_user_watermark (implement save wm upload handler)
async def get_user_watermark(user_id: int):
    # Fetch from DB or temp folder
    return None  # Update as needed


@app.on_message(filters.command("settings"))
async def settings_cmd(client: Client, message: Message):
    # Reuse callback handler by crafting a minimal CallbackQuery-like object
    fake_cb = types.CallbackQuery(id=0, from_user=message.from_user, chat_instance=None, data="settings")
    await settings_cb(client, fake_cb)


if __name__ == "__main__":
    app.run()
            
