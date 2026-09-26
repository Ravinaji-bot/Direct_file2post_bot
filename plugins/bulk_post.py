"""
Bulk-post OLD files (already saved in the database) to MOVIE_UPDATE_CHANNEL,
newest year first (2026, 2025, 2024 ... and titles with no year at the very end).

It reuses the exact same title parsing (extract_media_info), poster fetch
(TMDB -> IMDb fallback), caption builder and sender that plugins/channel.py uses
for live posts, so the bulk posts look identical to the normal auto-posts.

Commands (admin only):
    /bulkpost scan                 - step 1: read ALL saved files and build the queue (run once)
    /bulkpost start [count] [gap]  - step 2: start posting. count = how many posts (0/none = all),
                                     gap = seconds between posts (default 4, minimum 2)
    /bulkpost stop                 - stop after the current post
    /bulkpost status               - queue numbers, running state, next year to be posted
    /bulkpost noposter on|off      - allow/skip movies for which no poster was found (default: skip)
    /bulkpost retry                - put failed + no-poster items back in the queue
    /bulkpost clear confirm        - delete the queue (the posts already sent are NOT touched)

Notes:
    * The queue lives in the `bulk_post_queue` collection, one document per movie/series,
      so it survives restarts. After a restart just send /bulkpost start again.
    * Movies that already have a post (in `movie_updates`) are not posted twice; any old
      files missing from that post are merged into it instead.
    * Posts go to MOVIE_UPDATE_CHANNEL - point it at a test channel first.
"""
import asyncio
import html
import logging
import re
from datetime import datetime

from pymongo import UpdateOne
from pymongo.errors import DuplicateKeyError
from pyrogram import Client, filters
from pyrogram.types import Message

from database.ia_filterdb import Media, Media2
from database.users_chats_db import db
from info import ADMINS, MULTIPLE_DB, TMDB_POSTER
from plugins.channel import (
    extract_media_info,
    send_movie_update,
    update_movie_message,
    STANDARD_GENRES,
    _series_group_key,
)
from plugins.Dreamxfutures.Imdbposter import get_movie_detailsx, get_movie_details

logger = logging.getLogger(__name__)

YEAR_TAIL = re.compile(r"(?<!\d)((?:19|20)\d{2})\s*$")
BATCH = 1000
DEFAULT_GAP = 4
NOPOSTER_KEY = "BULK_ALLOW_NOPOSTER"
PROGRESS_EVERY = 100          # send a progress message every N posts
SCAN_PROGRESS_EVERY = 25000   # ... and every N files while scanning
SORT = [("year", -1), ("_id", 1)]   # newest year first, year 0 (unknown) last

_state = {"scan": None, "post": None, "stop": False}


def _queue():
    return db.db.bulk_post_queue


def _running(key):
    task = _state.get(key)
    return bool(task and not task.done())


async def _say(bot, chat_id, text):
    try:
        await bot.send_message(chat_id, text)
    except Exception:
        logger.exception("bulkpost: could not send progress message")


# --------------------------------------------------------------------------- scan
async def _scan(bot, chat_id):
    q = _queue()
    seen = errors = 0
    ops = []
    max_year = datetime.now().year + 1
    try:
        await q.create_index([("status", 1), ("year", -1), ("_id", 1)])
        sources = [Media.collection]
        if MULTIPLE_DB:
            sources.append(Media2.collection)

        for col in sources:
            cursor = col.find({}, {"file_name": 1, "caption": 1, "file_size": 1, "cover": 1})
            async for doc in cursor:
                seen += 1
                try:
                    info = extract_media_info(doc.get("file_name") or "", doc.get("caption") or "")
                    base = info["base_name"]
                    if not base:
                        raise ValueError("empty base_name")
                except Exception:
                    errors += 1
                    continue

                # For series, queue each season separately so it later becomes
                # its own post instead of every season being merged into one.
                group_key = _series_group_key(base, info["season"]) if info["tag"] == "#SERIES" else base

                m = YEAR_TAIL.search(base)
                year = int(m.group(1)) if m else 0
                if year > max_year:      # e.g. "Blade Runner 2049" is not a 2049 release
                    year = 0

                file_data = {
                    "filename": doc.get("file_name"),
                    "processed": info["processed"],
                    "quality": info["quality"],
                    "language": info["language"],
                    "ott_platform": info["ott_platform"],
                    "timestamp": datetime.now(),
                    "tag": info["tag"],
                    "season": info["season"],
                    "episode": info["episode"],
                    "file_id": doc["_id"],
                    "file_size": doc.get("file_size") or 0,
                    # This file's own Telegram cover/thumbnail, already saved at
                    # index time (COVERX) - reused as a last-resort poster if
                    # TMDB/IMDb have nothing for this title at all.
                    "cover": doc.get("cover"),
                }
                ops.append(UpdateOne(
                    {"_id": group_key},
                    {
                        "$setOnInsert": {
                            "title": base,
                            "year": year,
                            "status": "pending",
                            "tag": info["tag"],
                            "ott_platform": info["ott_platform"],
                        },
                        "$push": {"files": file_data},
                    },
                    upsert=True,
                ))
                if len(ops) >= BATCH:
                    await q.bulk_write(ops, ordered=True)
                    ops = []
                if seen % SCAN_PROGRESS_EVERY == 0:
                    await _say(bot, chat_id, f"🔎 Scan running... {seen} files read")

        if ops:
            await q.bulk_write(ops, ordered=True)

        movies = await q.count_documents({})
        years = await q.aggregate([
            {"$group": {"_id": "$year", "n": {"$sum": 1}}},
            {"$sort": {"_id": -1}},
            {"$limit": 8},
        ]).to_list(8)
        year_lines = "\n".join(
            f"{y['_id'] if y['_id'] else 'No year'} : {y['n']}" for y in years
        )
        await _say(
            bot, chat_id,
            f"✅ Scan finished\nFiles read: {seen}\nSkipped (unreadable): {errors}\n"
            f"Movies/series queued: {movies}\n\nTop of the queue:\n{year_lines}\n\n"
            "Test first: /bulkpost start 20"
        )
    except Exception as e:
        logger.exception("bulkpost scan failed")
        await _say(bot, chat_id, f"❌ Scan failed: {e}")


# --------------------------------------------------------------------------- post
async def _post_one(bot, qdoc, allow_noposter):
    """Returns one of: done, skipped, noposter, failed."""
    group_key = qdoc["_id"]
    # Old queue entries (scanned before this change) won't have a "title"
    # field - fall back to the key itself, which was the plain title back then.
    title_base = qdoc.get("title") or group_key
    files = qdoc.get("files", [])
    mu = db.db.movie_updates

    existing = await mu.find_one({"_id": group_key})
    if existing and not existing.get("files"):
        await mu.delete_one({"_id": group_key})      # empty stub, replace it
        existing = None

    if existing:
        have = {f.get("file_id") for f in existing["files"]}
        new_files = [f for f in files if f["file_id"] not in have]
        if new_files:
            await mu.update_one({"_id": group_key}, {"$push": {"files": {"$each": new_files}}})
        if existing.get("message_id"):
            if new_files:
                await update_movie_message(bot, group_key)
            return "skipped"
        msg = await send_movie_update(bot, group_key)
        return "done" if msg else "failed"

    error_tmdb = False
    details = {}
    # First file's season tells us definitively whether this is a series
    # (matches the same detection channel.py already uses for live uploads) -
    # restricts TMDB search to the right media type and fetches the right
    # season's own poster/year instead of the show's overall one.
    season_num = next((f.get("season") for f in files if f.get("season") is not None), None)
    is_series_hint = (qdoc.get("tag") == "#SERIES") or (season_num is not None)
    try:
        if TMDB_POSTER:
            details = await get_movie_detailsx(title_base, season=season_num, is_series=is_series_hint)
            if not details or details.get("error") or (
                not details.get("poster_url") and not details.get("backdrop_url")
            ):
                error_tmdb = True
                details = await get_movie_details(title_base) or {}
        else:
            details = await get_movie_details(title_base) or {}
    except Exception:
        logger.exception("bulkpost: poster lookup failed for %s", title_base)
        error_tmdb = True
        details = {}

    # Posters always come out landscape now (fetch_image letterboxes a
    # portrait source onto a landscape canvas - see Imdbposter.py), so prefer
    # a real backdrop when TMDB has one since it's naturally landscape,
    # falling back to the portrait poster only if there's no backdrop at all.
    poster = (details.get("backdrop_url") if TMDB_POSTER and not error_tmdb else None) or details.get("poster_url")
    # No poster/backdrop anywhere -> fall back to one of these old files' own
    # saved cover/thumbnail instead of skipping/posting with no image.
    fallback_thumb = None if poster else next((f.get("cover") for f in files if f.get("cover")), None)
    if not poster and not fallback_thumb and not allow_noposter:
        return "noposter"

    raw_genres = details.get("genres", "N/A")
    if isinstance(raw_genres, str):
        raw_genres = [g.strip() for g in raw_genres.split(",")]
    genres = ", ".join(g for g in raw_genres if g in STANDARD_GENRES) or "N/A"

    movie_doc = {
        "_id": group_key,
        "title": title_base,
        "files": files,
        "poster_url": poster,
        "genres": genres,
        "rating": details.get("rating", "N/A"),
        "imdb_url": details.get("url", "") if not TMDB_POSTER or error_tmdb else details.get("tmdb_url"),
        "year": details.get("year") or (str(qdoc["year"]) if qdoc.get("year") else None),
        "tag": qdoc.get("tag", "#MOVIE"),
        "ott_platform": qdoc.get("ott_platform", "N/A"),
        "message_id": None,
        "is_photo": False,
        "error_tmdb": error_tmdb,
        "is_backdrop": bool(details.get("backdrop_url")),
        "fallback_thumb_file_id": fallback_thumb,
    }
    try:
        await mu.insert_one(movie_doc)
    except DuplicateKeyError:
        return "skipped"

    msg = await send_movie_update(bot, group_key)
    return "done" if msg else "failed"


async def _post_worker(bot, chat_id, limit, gap):
    q = _queue()
    counts = {"done": 0, "skipped": 0, "noposter": 0, "failed": 0}
    allow_noposter = bool(await db.get_bot_setting(bot.me.id, NOPOSTER_KEY, False))
    reason = "queue finished"
    try:
        while True:
            if _state["stop"]:
                reason = "stopped by you"
                break
            if limit and counts["done"] >= limit:
                reason = f"limit of {limit} posts reached"
                break

            qdoc = await q.find_one({"status": "pending"}, sort=SORT)
            if not qdoc:
                break

            try:
                status = await _post_one(bot, qdoc, allow_noposter)
            except Exception:
                logger.exception("bulkpost: failed on %s", qdoc.get("_id"))
                status = "failed"

            await q.update_one(
                {"_id": qdoc["_id"]},
                {"$set": {"status": status, "updated_at": datetime.now()}},
            )
            counts[status] += 1

            if status == "done":
                if counts["done"] % PROGRESS_EVERY == 0:
                    await _say(
                        bot, chat_id,
                        f"📤 {counts['done']} posted (now at year {qdoc.get('year') or 'No year'}) | "
                        f"no poster: {counts['noposter']} | failed: {counts['failed']}"
                    )
                await asyncio.sleep(gap)
            elif status == "failed":
                await asyncio.sleep(1)
    except Exception as e:
        logger.exception("bulkpost worker crashed")
        reason = f"error: {e}"
    finally:
        _state["stop"] = False
        await _say(
            bot, chat_id,
            f"🏁 Bulk posting ended ({reason})\nPosted: {counts['done']}\n"
            f"Already posted/merged: {counts['skipped']}\nNo poster (skipped): {counts['noposter']}\n"
            f"Failed: {counts['failed']}"
        )


# --------------------------------------------------------------------------- command
async def _status_text():
    q = _queue()
    lines = []
    for st in ("pending", "done", "skipped", "noposter", "failed"):
        lines.append(f"{st}: {await q.count_documents({'status': st})}")
    nxt = await q.find_one({"status": "pending"}, sort=SORT, projection={"year": 1})
    if nxt:
        lines.append(f"\nNext up: {html.escape(str(nxt['_id']))} (year {nxt.get('year') or 'No year'})")
    lines.append(f"\nScanning: {'yes' if _running('scan') else 'no'}")
    lines.append(f"Posting: {'yes' if _running('post') else 'no'}")
    return "📊 Bulk post queue\n" + "\n".join(lines)


@Client.on_message(filters.command("bulkpost") & filters.user(ADMINS))
async def bulkpost_cmd(bot: Client, message: Message):
    args = message.command[1:]
    sub = args[0].lower() if args else ""
    chat_id = message.chat.id
    q = _queue()

    if sub == "scan":
        if _running("scan") or _running("post"):
            return await message.reply_text("Scan or posting is already running.")
        if await q.count_documents({}) > 0:
            return await message.reply_text(
                "A queue already exists. Use /bulkpost status to see it, or "
                "/bulkpost clear confirm and then scan again."
            )
        await message.reply_text("🔎 Scan started. Reading all saved files, this can take a few minutes...")
        _state["scan"] = asyncio.create_task(_scan(bot, chat_id))

    elif sub == "start":
        if _running("scan"):
            return await message.reply_text("Scan is still running, wait for it to finish.")
        if _running("post"):
            return await message.reply_text("Posting is already running. /bulkpost stop to stop it.")
        if await q.count_documents({"status": "pending"}) == 0:
            return await message.reply_text("Nothing pending. Run /bulkpost scan first (or /bulkpost retry).")
        nums = [int(a) for a in args[1:] if a.isdigit()]
        limit = nums[0] if nums else 0
        gap = max(2, nums[1]) if len(nums) > 1 else DEFAULT_GAP
        _state["stop"] = False
        _state["post"] = asyncio.create_task(_post_worker(bot, chat_id, limit, gap))
        await message.reply_text(
            f"▶️ Posting started: {limit or 'all'} posts, {gap}s gap, newest year first.\n"
            "/bulkpost stop to stop, /bulkpost status to check."
        )

    elif sub == "stop":
        if not _running("post"):
            return await message.reply_text("Posting is not running.")
        _state["stop"] = True
        await message.reply_text("⏹ Stopping after the current post...")

    elif sub == "status":
        await message.reply_text(await _status_text())

    elif sub == "noposter":
        if len(args) < 2 or args[1].lower() not in ("on", "off"):
            cur = await db.get_bot_setting(bot.me.id, NOPOSTER_KEY, False)
            return await message.reply_text(
                f"Posts without poster: {'ALLOWED' if cur else 'SKIPPED'}\n"
                "Use /bulkpost noposter on  or  /bulkpost noposter off"
            )
        on = args[1].lower() == "on"
        await db.update_bot_setting(bot.me.id, NOPOSTER_KEY, on)
        await message.reply_text(
            "✅ Movies with no poster will now be posted as text." if on
            else "✅ Movies with no poster will be skipped."
        )

    elif sub == "retry":
        if _running("post"):
            return await message.reply_text("Stop posting first.")
        res = await q.update_many(
            {"status": {"$in": ["failed", "noposter"]}},
            {"$set": {"status": "pending"}},
        )
        await message.reply_text(f"🔁 {res.modified_count} items moved back to pending.")

    elif sub == "clear":
        if _running("scan") or _running("post"):
            return await message.reply_text("Stop scan/posting first.")
        if len(args) < 2 or args[1].lower() != "confirm":
            return await message.reply_text("This deletes the whole queue. Send /bulkpost clear confirm to proceed.")
        await q.drop()
        await message.reply_text("🗑 Queue deleted. Already sent posts are untouched.")

    else:
        await message.reply_text(
            "Bulk post (old files, newest year first)\n\n"
            "/bulkpost scan\n"
            "/bulkpost start [count] [gap_seconds]\n"
            "/bulkpost stop\n"
            "/bulkpost status\n"
            "/bulkpost noposter on|off\n"
            "/bulkpost retry\n"
            "/bulkpost clear confirm"
)
