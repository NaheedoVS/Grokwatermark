import os
import asyncio
import subprocess
import logging
from io import BytesIO
from hachoir.parser import createParser
from hachoir.metadata import extractMetadata
from pyrogram import Client, filters, types
from pyrogram.types import Message, CallbackQuery, InlineKeyboardMarkup, InlineKeyboardButton, InputMediaVideo
from pyrogram.errors import FloodWait, UserNotParticipant
import motor.motor_asyncio

from configs import API_ID, API_HASH, BOT_TOKEN, OWNER_ID, LOG_CHANNEL, UPDATES_CHANNEL, PRESET, STREAMTAPE_API_USERNAME, STREAMTAPE_API_PASS
from db import get_user_data, set_user_data, get_text_data, set_text_data

logging.basicConfig(level=logging.INFO)
logger = logging.getLogger(__name__)

app = Client("WatermarkBot", api_id=API_ID, api_hash=API_HASH, bot_token=BOT_TOKEN)

# Simple state for pending inputs (user_id: state)
pending_states = {}

# Position map for overlays (9 positions)
POSITIONS = {
    "tl": "10:10", "tc": "w/2-text_w/2:10", "tr": "main_w-text_w-10:10",
    "ml": "10:h/2-text_h/2", "mc": "(w-text_w)/2:(h-text_h)/2", "mr": "(main_w-text_w-10):h/2-text_h/2",
    "bl": "10:main_h-text_h-10", "bc": "w/2-text_w/2:main_h-text_h-10", "br": "main_w-text_w-10:main_h-text_h-10"
}

@app.on_message(filters.command("start"))
async def start_cmd(client: Client, message: Message):
    user_id = message.from_user.id
    await set_user_data(user_id, {"position": "mc", "size": 50})  # Default
    await set_text_data(user_id, {"text": "", "color": "white", "size": 24, "use": False})
    kb = InlineKeyboardMarkup([[InlineKeyboardButton("📤 Add Watermark", callback_data="add_wm")]])
    await message.reply("Welcome! Upload a video to add watermark.", reply_markup=kb)

@app.on_message(filters.video | filters.document & filters.mime_type("video/*"))
async def process_video(client: Client, message: Message):
    user_id = message.from_user.id
    if not await check_sub(user_id):
        return
    msg = await message.reply("Processing video...")
    file_path = await message.download()
    
    # Get video info
    metadata = extractMetadata(createParser(file_path))
    width = metadata.get("width", 1280)
    height = metadata.get("height", 720)
    
    # Get user settings
    data = await get_user_data(user_id)
    text_data = await get_text_data(user_id)
    position = POSITIONS.get(data.get("position", "mc"), "mc")
    img_size = data.get("size", 50)
    
    wm_path = await get_user_watermark(user_id)  # Assume helper fetches saved wm
    text = text_data.get("text", "")
    color = text_data.get("color", "white")
    t_size = text_data.get("size", 24)
    use_text = text_data.get("use", False)
    
    output_path = f"watermarked_{message.id}.mp4"
    
    # Build FFmpeg command
    cmd = ["ffmpeg", "-y", "-i", file_path]
    filter_complex = []
    
    if use_text and text:
        x, y = position.split(":") if ":" in position else (position, "10")  # Simplified
        drawtext = f"drawtext=fontfile=/usr/share/fonts/truetype/dejavu/DejaVuSans.ttf:text='{text.replace('\'', '\\\'')}':fontcolor={color}:fontsize={t_size}:x={x}:y={y}"
        filter_complex.append(f"[0:v]{drawtext}[base]")
        current_input = "[base]"
    else:
        current_input = "[0:v]"
    
    if wm_path:
        scale = f"scale={int(width * img_size / 100)}:{int(height * img_size / 100)}[wm]"
        overlay = f"{current_input}[wm]overlay={position}[v]"
        filter_complex.append(f"-i {wm_path} -filter_complex '{scale};{overlay}'")
        cmd.extend(["-i", wm_path, "-filter_complex", filter_complex[-1]])
    else:
        if filter_complex:
            cmd.extend(["-vf", filter_complex[0].split("]")[1]])  # Just drawtext if no img
    
    cmd.extend(["-preset", PRESET, "-c:a", "copy", output_path])
    
    # Run with progress
    process = await asyncio.create_subprocess_exec(
        *cmd, stdout=asyncio.subprocess.PIPE, stderr=asyncio.subprocess.PIPE
    )
    await process.wait()
    
    if process.returncode == 0:
        await msg.edit("Upload complete!")
        await client.send_video(message.chat.id, output_path)
    else:
        await msg.edit("Error processing video.")
    
    os.remove(file_path)
    if os.path.exists(output_path):
        os.remove(output_path)

@app.on_callback_query(filters.regex("settings"))
async def settings_cb(client: Client, callback: CallbackQuery):
    user_id = callback.from_user.id
    text_data = await get_text_data(user_id)
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
    data = await get_text_data(user_id)
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
    data = await get_text_data(user_id)
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
    data = await get_text_data(user_id)
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
    data = await get_text_data(user_id)
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
        await app.get_chat_member(UPDATES_CHANNEL, user_id)
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
    await settings_cb(client, types.CallbackQuery(message.chat.id, message.message_id, None, None, "settings", None))

if __name__ == "__main__":
    app.run()
