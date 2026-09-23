"""
Admin commands to customize the auto-post caption format (see
plugins/channel.py -> build_post_caption / get_post_format) WITHOUT
touching any code or redeploying. Settings are stored per-bot in the
existing `bot_settings` collection (db.get_bot_setting/update_bot_setting),
the same mechanism already used for MOVIE_UPDATE_NOTIFICATION/MAINTENANCE.

Commands (admin only):
    /postsettings        - shows current settings with toggle buttons
    /setwatermark <text> - sets the "💢 ᴘᴏᴡᴇʀᴇᴅ ʙʏ :" line (HTML allowed)
    /setlinktext <text>  - sets the clickable link text (default: Click Hare)
    /setdivider <text>   - sets the ──── divider line
    /resetpostformat     - resets everything back to defaults
"""
import logging
from pyrogram import Client, filters, enums
from pyrogram.types import Message, InlineKeyboardMarkup, InlineKeyboardButton, CallbackQuery
from info import ADMINS
from database.users_chats_db import db
from plugins.channel import DEFAULT_POST_FORMAT, _POST_FORMAT_KEYS, get_post_format

logger = logging.getLogger(__name__)


def _settings_text(fmt: dict) -> str:
    bold_status = "Bold ✅" if fmt["bold"] else "Bold ❌"
    layout_status = "2-line (✧ style)" if fmt["layout"] == "twoline" else "1-line (compact)"
    box_status = "ON ✅ (quote-box)" if fmt.get("header_box") else "OFF ❌ (plain lines)"
    spoiler_status = "ON ✅ (blurred)" if fmt.get("spoiler") else "OFF ❌"
    if fmt.get("button_text") and fmt.get("button_url"):
        button_status = f"{fmt['button_text']} → {fmt['button_url']}"
    else:
        button_status = "Not set"
    return (
        "<b>🛠️ Auto-Post Format Settings</b>\n\n"
        f"<b>Bold text:</b> {bold_status}\n"
        f"<b>Layout:</b> {layout_status}\n"
        f"<b>Header box:</b> {box_status}\n"
        f"<b>Poster spoiler:</b> {spoiler_status}\n"
        f"<b>Link text:</b> {fmt['link_text']}\n"
        f"<b>Divider:</b> <code>{fmt['divider']}</code>\n"
        f"<b>Title emoji:</b> {fmt['title_emoji']}\n"
        f"<b>Watermark:</b> {fmt['watermark']}\n"
        f"<b>Inline button:</b> {button_status}\n\n"
        "Use the buttons below to toggle, or these commands to change text:\n"
        "<code>/setwatermark your text here</code>\n"
        "<code>/setlinktext Click Here</code>\n"
        "<code>/setdivider ──────────</code>\n"
        "<code>/setbutton Join Channel | https://t.me/yourchannel</code>\n"
        "<code>/removebutton</code>\n"
        "<code>/resetpostformat</code> — reset everything to default"
    )


def _settings_buttons(fmt: dict) -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup([
        [
            InlineKeyboardButton(
                "Bold: ON ✅" if fmt["bold"] else "Bold: OFF ❌",
                callback_data="pfmt_bold"
            )
        ],
        [
            InlineKeyboardButton(
                "Layout: 2-line ✧" if fmt["layout"] == "twoline" else "Layout: 1-line",
                callback_data="pfmt_layout"
            )
        ],
        [
            InlineKeyboardButton(
                "Header box: ON ✅" if fmt.get("header_box") else "Header box: OFF ❌",
                callback_data="pfmt_headerbox"
            )
        ],
        [
            InlineKeyboardButton(
                "Poster spoiler: ON ✅" if fmt.get("spoiler") else "Poster spoiler: OFF ❌",
                callback_data="pfmt_spoiler"
            )
        ],
        [InlineKeyboardButton("🔄 Refresh", callback_data="pfmt_refresh")]
    ])


@Client.on_message(filters.command("postsettings") & filters.user(ADMINS))
async def post_settings_cmd(bot: Client, message: Message):
    fmt = await get_post_format(bot.me.id)
    await message.reply_text(
        text=_settings_text(fmt),
        reply_markup=_settings_buttons(fmt),
        parse_mode=enums.ParseMode.HTML
    )


@Client.on_callback_query(filters.regex(r"^pfmt_") & filters.user(ADMINS))
async def post_settings_callback(bot: Client, query: CallbackQuery):
    bot_id = bot.me.id
    action = query.data.split("_", 1)[1]

    if action == "bold":
        current = await get_post_format(bot_id)
        await db.update_bot_setting(bot_id, _POST_FORMAT_KEYS["bold"], not current["bold"])
        await query.answer("Bold toggled!")
    elif action == "layout":
        current = await get_post_format(bot_id)
        new_layout = "compact" if current["layout"] == "twoline" else "twoline"
        await db.update_bot_setting(bot_id, _POST_FORMAT_KEYS["layout"], new_layout)
        await query.answer("Layout toggled!")
    elif action == "headerbox":
        current = await get_post_format(bot_id)
        await db.update_bot_setting(bot_id, _POST_FORMAT_KEYS["header_box"], not current.get("header_box"))
        await query.answer("Header box toggled!")
    elif action == "spoiler":
        current = await get_post_format(bot_id)
        await db.update_bot_setting(bot_id, _POST_FORMAT_KEYS["spoiler"], not current.get("spoiler"))
        await query.answer("Poster spoiler toggled!")
    elif action == "refresh":
        await query.answer("Refreshed")

    fmt = await get_post_format(bot_id)
    try:
        await query.message.edit_text(
            text=_settings_text(fmt),
            reply_markup=_settings_buttons(fmt),
            parse_mode=enums.ParseMode.HTML
        )
    except Exception:
        pass


@Client.on_message(filters.command("setwatermark") & filters.user(ADMINS))
async def set_watermark_cmd(bot: Client, message: Message):
    if len(message.command) < 2:
        await message.reply_text(
            "Usage: <code>/setwatermark your text or HTML link here</code>\n\n"
            "Example:\n<code>/setwatermark &lt;a href=\"https://t.me/yourchannel\"&gt;Your Channel&lt;/a&gt; 🤞</code>",
            parse_mode=enums.ParseMode.HTML
        )
        return
    value = message.text.split(None, 1)[1]
    await db.update_bot_setting(bot.me.id, _POST_FORMAT_KEYS["watermark"], value)
    await message.reply_text(f"✅ Watermark updated:\n{value}", parse_mode=enums.ParseMode.HTML)


@Client.on_message(filters.command("setlinktext") & filters.user(ADMINS))
async def set_link_text_cmd(bot: Client, message: Message):
    if len(message.command) < 2:
        await message.reply_text("Usage: <code>/setlinktext Click Here</code>", parse_mode=enums.ParseMode.HTML)
        return
    value = message.text.split(None, 1)[1]
    await db.update_bot_setting(bot.me.id, _POST_FORMAT_KEYS["link_text"], value)
    await message.reply_text(f"✅ Link text updated to: {value}")


@Client.on_message(filters.command("setdivider") & filters.user(ADMINS))
async def set_divider_cmd(bot: Client, message: Message):
    if len(message.command) < 2:
        await message.reply_text("Usage: <code>/setdivider ──────────</code>", parse_mode=enums.ParseMode.HTML)
        return
    value = message.text.split(None, 1)[1]
    await db.update_bot_setting(bot.me.id, _POST_FORMAT_KEYS["divider"], value)
    await message.reply_text(f"✅ Divider updated to:\n{value}")


@Client.on_message(filters.command("setbutton") & filters.user(ADMINS))
async def set_button_cmd(bot: Client, message: Message):
    if len(message.command) < 2 or "|" not in message.text.split(None, 1)[1]:
        await message.reply_text(
            "Usage: <code>/setbutton Button Text | https://t.me/yourchannel</code>\n\n"
            "This adds one inline button under every auto-post.\n"
            "Use <code>/removebutton</code> to remove it.",
            parse_mode=enums.ParseMode.HTML
        )
        return
    raw = message.text.split(None, 1)[1]
    text, url = (part.strip() for part in raw.split("|", 1))
    if not text or not url:
        await message.reply_text("Both button text and URL are required.")
        return
    if not (url.startswith("http://") or url.startswith("https://") or url.startswith("t.me/") or url.startswith("tg://")):
        await message.reply_text("⚠️ URL looks invalid — it should start with https://, http://, or t.me/")
        return
    await db.update_bot_setting(bot.me.id, _POST_FORMAT_KEYS["button_text"], text)
    await db.update_bot_setting(bot.me.id, _POST_FORMAT_KEYS["button_url"], url)
    await message.reply_text(f"✅ Button set:\n<b>{text}</b> → {url}", parse_mode=enums.ParseMode.HTML)


@Client.on_message(filters.command("removebutton") & filters.user(ADMINS))
async def remove_button_cmd(bot: Client, message: Message):
    bot_id = bot.me.id
    await db.update_bot_setting(bot_id, _POST_FORMAT_KEYS["button_text"], "")
    await db.update_bot_setting(bot_id, _POST_FORMAT_KEYS["button_url"], "")
    await message.reply_text("✅ Inline button removed from auto-posts.")


@Client.on_message(filters.command("resetpostformat") & filters.user(ADMINS))
async def reset_post_format_cmd(bot: Client, message: Message):
    bot_id = bot.me.id
    for key, db_key in _POST_FORMAT_KEYS.items():
        await db.update_bot_setting(bot_id, db_key, DEFAULT_POST_FORMAT[key])
    await message.reply_text("✅ Post format reset to defaults.")
