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
    /repost <title>      - deletes the old post for that title in the
                            update channel (if it still exists) and sends a
                            brand-new one, no manual deleting required
"""
import re as _re
import logging
from pyrogram import Client, filters, enums
from pyrogram.types import Message, InlineKeyboardMarkup, InlineKeyboardButton, CallbackQuery
from info import ADMINS, MOVIE_UPDATE_CHANNEL
from database.users_chats_db import db
from plugins.channel import DEFAULT_POST_FORMAT, _POST_FORMAT_KEYS, get_post_format, send_movie_update

logger = logging.getLogger(__name__)


def _settings_text(fmt: dict) -> str:
    bold_status = "Bold ✅" if fmt["bold"] else "Bold ❌"
    layout_status = "2-line (✧ style)" if fmt["layout"] == "twoline" else "1-line (compact)"
    box_status = "ON ✅ (quote-box)" if fmt.get("header_box") else "OFF ❌ (plain lines)"
    links_box_status = "ON ✅ (quote-box)" if fmt.get("links_box") else "OFF ❌ (plain lines)"
    watermark_box_status = "ON ✅ (quote-box)" if fmt.get("watermark_box") else "OFF ❌ (plain line)"
    if fmt.get("button_text") and fmt.get("button_url"):
        button_status = f"{fmt['button_text']} → {fmt['button_url']}"
    else:
        button_status = "Not set"
    return (
        "<b>🛠️ Auto-Post Format Settings</b>\n\n"
        f"<b>Bold text:</b> {bold_status}\n"
        f"<b>Layout:</b> {layout_status}\n"
        f"<b>Header box:</b> {box_status}\n"
        f"<b>Links box:</b> {links_box_status}\n"
        f"<b>Watermark box:</b> {watermark_box_status}\n"
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
                "Links box: ON ✅" if fmt.get("links_box") else "Links box: OFF ❌",
                callback_data="pfmt_linksbox"
            )
        ],
        [
            InlineKeyboardButton(
                "Watermark box: ON ✅" if fmt.get("watermark_box") else "Watermark box: OFF ❌",
                callback_data="pfmt_watermarkbox"
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
    elif action == "linksbox":
        current = await get_post_format(bot_id)
        await db.update_bot_setting(bot_id, _POST_FORMAT_KEYS["links_box"], not current.get("links_box"))
        await query.answer("Links box toggled!")
    elif action == "watermarkbox":
        current = await get_post_format(bot_id)
        await db.update_bot_setting(bot_id, _POST_FORMAT_KEYS["watermark_box"], not current.get("watermark_box"))
        await query.answer("Watermark box toggled!")
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


@Client.on_message(filters.command("repost") & filters.user(ADMINS))
async def repost_cmd(bot: Client, message: Message):
    """Force a fresh, brand-new post for a title that was already posted to
    MOVIE_UPDATE_CHANNEL. Deletes the old channel message itself (no need to
    do it by hand) and sends a new one, using build_post_caption / the
    current /postsettings just like a normal auto-post."""
    if len(message.command) < 2:
        await message.reply_text(
            "Usage: <code>/repost movie or series title</code>\n\n"
            "This finds that title's existing post in the update channel, "
            "deletes it (if it's still there), and sends a brand-new post "
            "for it — no need to delete anything by hand.",
            parse_mode=enums.ParseMode.HTML
        )
        return

    query = message.text.split(None, 1)[1].strip()
    if not hasattr(db, 'movie_updates'):
        db.movie_updates = db.db.movie_updates

    cursor = db.movie_updates.find({"_id": {"$regex": _re.escape(query), "$options": "i"}})
    matches = [doc async for doc in cursor]

    if not matches:
        await message.reply_text("No matching title found for that update-channel post.")
        return

    if len(matches) > 1:
        titles = "\n".join(f"• {m['_id']}" for m in matches[:15])
        await message.reply_text(
            f"Multiple matches found, please be more specific:\n\n{titles}"
        )
        return

    movie_doc = matches[0]
    base_name = movie_doc["_id"]
    old_message_id = movie_doc.get("message_id")

    if old_message_id:
        try:
            await bot.delete_messages(chat_id=MOVIE_UPDATE_CHANNEL, message_ids=old_message_id)
        except Exception as e:
            logger.warning(f"Could not delete old post for {base_name} (may already be gone): {e}")

    await db.movie_updates.update_one(
        {"_id": base_name},
        {"$set": {"message_id": None, "is_photo": False}}
    )

    status = await message.reply_text(f"⏳ Re-posting <b>{base_name}</b> ...", parse_mode=enums.ParseMode.HTML)
    msg = await send_movie_update(bot, base_name)
    if msg:
        await status.edit_text(f"✅ Re-posted: <b>{base_name}</b>", parse_mode=enums.ParseMode.HTML)
    else:
        await status.edit_text(f"⚠️ Could not re-post <b>{base_name}</b>. Check the bot logs.", parse_mode=enums.ParseMode.HTML)
