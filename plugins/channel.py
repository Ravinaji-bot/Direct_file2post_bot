import re
import logging
import asyncio
import uuid
from datetime import datetime
from collections import defaultdict
from plugins.Dreamxfutures.Imdbposter import get_movie_detailsx, fetch_image, get_movie_details, build_poster_from_telegram_thumb
from database.users_chats_db import db
from pyrogram import Client, filters, enums
from info import CHANNELS, MOVIE_UPDATE_CHANNEL, LINK_PREVIEW, ABOVE_PREVIEW, BAD_WORDS, TMDB_POSTER, MOVIE_POST_WATERMARK
from Script import script
from database.ia_filterdb import save_file, unpack_new_file_id
from pyrogram.types import InlineKeyboardMarkup, InlineKeyboardButton, LinkPreviewOptions
from utils import temp, get_size
from pymongo.errors import PyMongoError, DuplicateKeyError
from pyrogram.errors import MessageIdInvalid, MessageNotModified, FloodWait
from typing import Optional, Tuple

logger = logging.getLogger(__name__)

# Precomputed sets for faster lookups
IGNORE_WORDS = {
    "rarbg", "dub", "sub", "sample", "mkv", "aac", "combined",
    "action", "adventure", "animation", "biography", "comedy", "crime", 
    "documentary", "drama", "fantasy", "film-noir", "history", 
    "horror", "music", "musical", "mystery", "romance", "sci-fi", "sport", 
    "thriller", "war", "western", "hdcam", "hdtc", "camrip", "ts", "tc", 
    "telesync", "dvdscr", "dvdrip", "predvd", "webrip", "web-dl", "tvrip", 
    "hdtv", "web dl", "webdl", "bluray", "brrip", "bdrip", "360p", "480p", 
    "720p", "1080p", "2160p", "4k", "1440p", "540p", "240p", "140p", "hevc", 
    "hdrip", "hin", "hindi", "tam", "tamil", "kan", "kannada", "tel", "telugu", 
    "mal", "malayalam", "eng", "english", "pun", "punjabi", "ben", "bengali", 
    "mar", "marathi", "guj", "gujarati", "urd", "urdu", "kor", "korean", "jpn", 
    "japanese", "nf", "netflix", "sonyliv", "sony", "sliv", "amzn", "prime", 
    "primevideo", "hotstar", "zee5", "jio", "jhs", "aha", "hbo", "paramount", 
    "apple", "hoichoi", "sunnxt", "viki"
}|BAD_WORDS

# Constants
CAPTION_LANGUAGES = {
    "hin": "Hindi", "hindi": "Hindi",
    "tam": "Tamil", "tamil": "Tamil",
    "kan": "Kannada", "kannada": "Kannada",
    "tel": "Telugu", "telugu": "Telugu",
    "mal": "Malayalam", "malayalam": "Malayalam",
    "eng": "English", "english": "English",
    "pun": "Punjabi", "punjabi": "Punjabi",
    "ben": "Bengali", "bengali": "Bengali",
    "mar": "Marathi", "marathi": "Marathi",
    "guj": "Gujarati", "gujarati": "Gujarati",
    "urd": "Urdu", "urdu": "Urdu",
    "kor": "Korean", "korean": "Korean",
    "jpn": "Japanese", "japanese": "Japanese",
}

OTT_PLATFORMS = {
    "nf": "Netflix", "netflix": "Netflix",
    "sonyliv": "SonyLiv", "sony": "SonyLiv", "sliv": "SonyLiv",
    "amzn": "Amazon Prime Video", "prime": "Amazon Prime Video", "primevideo": "Amazon Prime Video",
    "hotstar": "Disney+ Hotstar", "zee5": "Zee5",
    "jio": "JioHotstar", "jhs": "JioHotstar",
    "aha": "Aha", "hbo": "HBO Max", "paramount": "Paramount+",
    "apple": "Apple TV+", "hoichoi": "Hoichoi", "sunnxt": "Sun NXT", "viki": "Viki"
}

STANDARD_GENRES = {
    'Action', 'Adventure', 'Animation', 'Biography', 'Comedy', 'Crime', 'Documentary',
    'Drama', 'Family', 'Fantasy', 'Film-Noir', 'History', 'Horror', 'Music',
    'Musical', 'Mystery', 'Romance', 'Sci-Fi', 'Sport', 'Thriller', 'War', 'Western'
}

# Precompiled regex patterns
CLEAN_PATTERN = re.compile(r'@[^ \n\r\t\.,:;!?()\[\]{}<>\\/"\'=_%]+|\bwww\.[^\s\]\)]+|\([\@^]+\)|\[[\@^]+\]')
NORMALIZE_PATTERN = re.compile(r"[._]+|[()\[\]{}:;'–!,.?_]")
QUALITY_PATTERN = re.compile(
    r"\b(?:HDCam|HDTC|CamRip|TS|TC|TeleSync|DVDScr|DVDRip|PreDVD|"
    r"WEBRip|WEB-DL|TVRip|HDTV|WEB DL|WebDl|BluRay|BRRip|BDRip|"
    r"360p|480p|720p|1080p|2160p|4K|1440p|540p|240p|140p|HEVC|HDRip)\b", 
    re.IGNORECASE
)
YEAR_PATTERN = re.compile(r"(?<![A-Za-z0-9])(?:19|20)\d{2}(?![A-Za-z0-9])")
RANGE_REGEX = re.compile(r'\bS(\d{1,2})[^\w\n\r]*E(?:p(?:isode)?)?0*(\d{1,2})\s*(?:to|-)\s*(?:E(?:p(?:isode)?)?)?0*(\d{1,2})',re.IGNORECASE)
SINGLE_REGEX = re.compile(r'\bS(\d{1,2})[^\w\n\r]*E(?:p(?:isode)?)?0*(\d{1,3})', re.IGNORECASE)
NAMED_REGEX = re.compile(r'Season\s*0*(\d{1,2})[\s\-,:]*Ep(?:isode)?\s*0*(\d{1,3})', re.IGNORECASE)
EP_ONLY_RANGE = re.compile(r'\b(?:EP|Episode)0*(\d{1,3})\s*-\s*0*(\d{1,3})\b',re.IGNORECASE)
# Fallback for whole-season-pack files that carry only a season marker with NO
# episode number at all, e.g. "Show Name S01" or "Show Name Season 02" (common
# when a full season is uploaded as one file instead of per-episode).
SEASON_ONLY_REGEX = re.compile(r'\bS0*(\d{1,2})\b|\bSeason\s*0*(\d{1,2})\b', re.IGNORECASE)


MEDIA_FILTER = filters.document | filters.video | filters.audio
locks = defaultdict(asyncio.Lock)
pending_updates = {}
error_tmdb = False

def clean_mentions_links(text: str) -> str:
    return CLEAN_PATTERN.sub("", text or "").strip()

def normalize(s: str) -> str:
    s = NORMALIZE_PATTERN.sub(" ", s)
    return re.sub(r"\s+", " ", s).strip()

def remove_ignored_words(text: str) -> str:
    IGNORE_WORDS_LOWER = {w.lower() for w in IGNORE_WORDS}
    return " ".join(word for word in text.split() if word.lower() not in IGNORE_WORDS_LOWER)

def get_qualities(text: str) -> str:
    qualities = QUALITY_PATTERN.findall(text)
    return ", ".join(qualities) if qualities else "N/A"

def extract_ott_platform(text: str) -> str:
    text = text.lower()
    platforms = {plat for key, plat in OTT_PLATFORMS.items() if key in text}
    return " | ".join(platforms) if platforms else "N/A"

def extract_season_episode(filename: str) -> Tuple[Optional[int], Optional[str]]:
    if m := EP_ONLY_RANGE.search(filename):
        return 1, f"{int(m.group(1))}-{int(m.group(2))}"
    for pattern in (RANGE_REGEX, SINGLE_REGEX, NAMED_REGEX):
        if m := pattern.search(filename):
            season = int(m.group(1))
            if pattern == RANGE_REGEX:
                ep = f"{m.group(2)}-{m.group(3)}"
            else:
                ep = m.group(2)
            return season, ep
    # No episode marker found - check if this is a whole-season-pack file
    # (e.g. "S01" or "Season 02" with no episode number). Treat it as that
    # season with no specific episode, instead of falling through to #MOVIE.
    if m := SEASON_ONLY_REGEX.search(filename):
        season = int(m.group(1) or m.group(2))
        return season, None
    return None, None

def schedule_update(bot, base_name, delay=5):
    if handle := pending_updates.get(base_name):
        if not handle.cancelled():
            handle.cancel()
    
    loop = asyncio.get_event_loop()
    pending_updates[base_name] = loop.call_later(
        delay,
        lambda: asyncio.create_task(update_movie_message(bot, base_name))
    )

def _series_group_key(base_name, season):
    """DB key for a series file: each season gets its own key (and therefore
    its own post) instead of every season of a show sharing one combined post."""
    if season is None:
        return base_name
    try:
        return f"{base_name} S{int(season):02d}"
    except (TypeError, ValueError):
        return f"{base_name} S{season}"

def extract_media_info(filename: str, caption: str):
    filename = normalize(clean_mentions_links(filename).title())
    caption_clean = clean_mentions_links(caption).lower() if caption else ""
    unified = f"{caption_clean} {filename.lower()}".strip()

    season = episode = year = None
    tag = "#MOVIE"
    processed_raw = base_raw = filename
    quality = get_qualities(caption_clean) or get_qualities(filename.lower()) or "N/A"
    ott_platform = extract_ott_platform(f"{filename} {caption_clean}")

    lang_keys = {k for k in CAPTION_LANGUAGES if k in caption_clean or k in filename.lower()}
    language = ", ".join(sorted({CAPTION_LANGUAGES[k] for k in lang_keys})) if lang_keys else "N/A"

    season, episode = extract_season_episode(filename)
    if season is not None:
        tag = "#SERIES"
        if m := (RANGE_REGEX.search(filename) or SINGLE_REGEX.search(filename) or NAMED_REGEX.search(filename) or EP_ONLY_RANGE.search(filename) or SEASON_ONLY_REGEX.search(filename)):
            match_str = m.group(0)
            start_idx = filename.lower().find(match_str.lower())
            end_idx = start_idx + len(match_str)
            processed_raw = filename[:end_idx]
            base_raw = filename[:start_idx]
            if year_match := YEAR_PATTERN.search(filename.lower()[end_idx:]):
                y = year_match.group(0)
                yi = filename.lower().find(y, end_idx)
                if yi != -1:
                    processed_raw = filename[:yi+4]
                    base_raw += f" {y}"
    else:
        if year_match := YEAR_PATTERN.search(unified):
            year = year_match.group(0)
            year_idx = filename.lower().find(year.lower())
            if year_idx != -1:
                processed_raw = filename[:year_idx + 4]
                base_raw = processed_raw
        else:
            if qual_match := QUALITY_PATTERN.search(unified):
                qual_str = qual_match.group(0)
                qual_idx = filename.lower().find(qual_str.lower())
                if qual_idx != -1:
                    processed_raw = filename[:qual_idx]
                    base_raw = processed_raw

    base_name = normalize(remove_ignored_words(normalize(base_raw)))
    if year and year not in base_name:
        base_name += f" {year}"

    if base_name.endswith(")"):
        base_name = re.sub(r"\s+\(\d{4}\)$", "", base_name)
        if year:
            base_name += f" {year}"

    # -------------------------
    # NEW: strip season/episode tokens from final base_name
    # -------------------------
    def _strip_season_episode_tokens(name: str) -> str:
        """
        Remove common season/episode markers from a title while preserving a trailing year.
        Examples removed: S01, s01e02, 1x02, season 1, ep 02, episode 2, part 1
        """
        if not name:
            return name

        # Preserve trailing year (e.g. "Title (2020)" or "Title 2020")
        year_match = re.search(r'\(?\b(19|20)\d{2}\b\)?\s*$', name)
        year_part = ""
        if year_match:
            year_part = year_match.group(0)
            name = name[:year_match.start()].strip()

        # Common patterns to remove
        patterns = [
            r'\bS\d{1,2}E\d{1,2}\b',     # S01E02
            r'\bS\d{1,2}\b',             # S01
            r'\bE\d{1,2}\b',             # E02
            r'\b\d{1,2}x\d{1,2}\b',      # 1x02
            r'\bSeason\s*\d{1,2}\b',     # Season 1
            r'\bEp(?:isode)?\.?\s*\d{1,3}\b',  # Ep02, Episode 2
            r'\bEpisode\s*\d{1,3}\b',
            r'\bPart\s*\d{1,2}\b'
        ]

        for p in patterns:
            name = re.sub(p, ' ', name, flags=re.IGNORECASE)

        # Remove leftover separators and extra whitespace
        name = re.sub(r'[_\.\-]+', ' ', name)     # underscores/dots/hyphens
        name = re.sub(r'\s+', ' ', name).strip()

        # Reattach year in canonical form if we removed it earlier
        if year_part:
            y = re.search(r'(19|20)\d{2}', year_part)
            if y:
                name = f"{name} {y.group(0)}"

        return name.strip()

    base_name = _strip_season_episode_tokens(base_name)
    # If stripping accidentally removed everything, fall back to a safer value
    if not base_name:
        base_name = normalize(remove_ignored_words(normalize(processed_raw))) or filename

    # Safety net: if the title ended up with more than one 4-digit year token
    # (e.g. "Bakaiti 2026 2025"), keep only the LAST one - it's the one placed
    # right next to the title by convention - and drop the earlier duplicate(s).
    year_tokens = list(re.finditer(r'\b(19|20)\d{2}\b', base_name))
    if len(year_tokens) > 1:
        keep = year_tokens[-1]
        cleaned = base_name[:year_tokens[0].start()] + base_name[keep.start():]
        base_name = re.sub(r'\s+', ' ', cleaned).strip()

    return {
        "processed": normalize(processed_raw),
        "base_name": base_name,
        "tag": tag,
        "season": season,
        "episode": episode,
        "year": year,
        "quality": quality,
        "ott_platform": ott_platform,
        "language": language
    }


@Client.on_message(filters.chat(CHANNELS) & MEDIA_FILTER)
async def media_handler(bot, message):
    media = next(
        (getattr(message, ft) for ft in ("document", "video", "audio")
         if getattr(message, ft, None)),
        None
    )
    if not media:
        return

    media.file_type = next(ft for ft in ("document", "video", "audio") if getattr(message, ft, None))
    media.caption = message.caption or ""
    success, info = await save_file(media)
    if not success:
        return

    try:
        enc_file_id, _ = unpack_new_file_id(media.file_id)
    except Exception:
        enc_file_id = None
    file_size = getattr(media, "file_size", 0) or 0

    # Telegram auto-generates a thumbnail for video files (and uploaders of
    # this kind of file very often set one manually on documents too). Keep
    # its file_id around as a last-resort poster source, in case TMDB/IMDb
    # have no poster/backdrop at all for this title.
    thumbs = getattr(media, "thumbs", None)
    thumb_file_id = thumbs[-1].file_id if thumbs else None

    try:
        if await db.movie_update_status(bot.me.id):
            await process_and_send_update(bot, media.file_name, media.caption, enc_file_id, file_size, thumb_file_id)
    except Exception:
        logger.exception("Error processing media")

async def process_and_send_update(bot, filename, caption, file_id=None, file_size=0, thumb_file_id=None):
    try:
        media_info = extract_media_info(filename, caption)
        base_name = media_info["base_name"]
        processed = media_info["processed"]

        # For series, group/post per season - each season gets its own DB
        # entry (and therefore its own post) instead of every season of a
        # show being merged into a single combined post.
        group_key = _series_group_key(base_name, media_info["season"]) if media_info["tag"] == "#SERIES" else base_name

        lock = locks[group_key]
        async with lock:
            await _process_with_lock(bot, filename, caption, media_info, base_name, group_key, processed, file_id, file_size, thumb_file_id)
    except PyMongoError as e:
        logger.error(f"Database error in process_and_send_update: {e}")
    except Exception as e:
        logger.exception(f"Processing failed in process_and_send_update: {e}")

async def _process_with_lock(bot, filename, caption, media_info, base_name, group_key, processed, file_id=None, file_size=0, thumb_file_id=None):
    if not hasattr(db, 'movie_updates'):
        db.movie_updates = db.db.movie_updates

    movie_doc = await db.movie_updates.find_one({"_id": group_key})
    error_tmdb=False
    file_data = {
        "filename": filename,
        "processed": processed,
        "quality": media_info["quality"],
        "language": media_info["language"],
        "ott_platform": media_info["ott_platform"],
        "timestamp": datetime.now(),
        "tag": media_info["tag"],
        "season": media_info["season"],
        "episode": media_info["episode"],
        "file_id": file_id,
        "file_size": file_size
    }

    if not movie_doc:
        if TMDB_POSTER:
            details = await get_movie_detailsx(base_name, season=media_info.get("season"), is_series=(media_info.get("season") is not None))
            if not details or details.get("error") or (not details.get("poster_url") and not details.get("backdrop_url")):
                error_tmdb=True
                logger.info("TMDB error switching to IMDB")
                details = await get_movie_details(base_name) or {}
        else:
            details = await get_movie_details(base_name) or {}

        raw_genres = details.get("genres", "N/A")
        if isinstance(raw_genres, str):
            genre_list = [g.strip() for g in raw_genres.split(",")]
            genres = ", ".join(g for g in genre_list if g in STANDARD_GENRES) or "N/A"
        else:
            genres = ", ".join(g for g in raw_genres if g in STANDARD_GENRES) or "N/A"
        # Posters should always come out landscape now (fetch_image/
        # build_poster_from_telegram_thumb both letterbox a portrait source
        # onto a landscape canvas), so always prefer a real backdrop when TMDB
        # has one - it's naturally landscape - falling back to the portrait
        # poster only if there's no backdrop at all.
        chosen_poster_url = (details.get("backdrop_url") if TMDB_POSTER and not error_tmdb else None) or details.get("poster_url")
        movie_doc = {
            "_id": group_key,
            "title": base_name,
            "files": [file_data],
            "poster_url": chosen_poster_url,
            "genres": genres,
            "rating": details.get("rating", "N/A"),
            "imdb_url": details.get("url", "")if not TMDB_POSTER or error_tmdb else details.get("tmdb_url"),
            "year": details.get("year") or media_info["year"],
            "tag": media_info["tag"],
            "ott_platform": media_info["ott_platform"],
            "message_id": None,
            "is_photo": False,
            "error_tmdb": error_tmdb,
            "is_backdrop": bool(details.get("backdrop_url")) if TMDB_POSTER and not error_tmdb else False,
            # No poster AND no backdrop from TMDB/IMDb at all -> remember this
            # file's own video thumbnail so send_movie_update() can build a
            # poster out of it instead of posting with no image.
            "fallback_thumb_file_id": thumb_file_id if not chosen_poster_url else None,
        }
        try:
            await db.movie_updates.insert_one(movie_doc)
            await send_movie_update(bot, group_key)
            movie_doc = await db.movie_updates.find_one({"_id": group_key})
        except DuplicateKeyError:
            movie_doc = await db.movie_updates.find_one({"_id": group_key})
            if movie_doc:
                if any(f["filename"] == filename for f in movie_doc["files"]):
                    return
                await db.movie_updates.update_one(
                    {"_id": group_key},
                    {"$push": {"files": file_data}}
                )
                movie_doc["files"].append(file_data)
                schedule_update(bot, group_key)
    else:
        if any(f["filename"] == filename for f in movie_doc["files"]):
            return
        await db.movie_updates.update_one(
            {"_id": group_key},
            {"$push": {"files": file_data}}
        )
        movie_doc["files"].append(file_data)
        schedule_update(bot, group_key)

async def send_movie_update(bot, base_name):
    max_retries = 3
    for attempt in range(max_retries):
        try:
            movie_doc = await db.movie_updates.find_one({"_id": base_name})
            if not movie_doc:
                return None

            fmt = await get_post_format(bot.me.id)
            text = build_post_caption(movie_doc, base_name, fmt)
            buttons = build_post_buttons(fmt)
            # Every poster we send is now built landscape (see
            # _to_landscape_canvas in Imdbposter.py) regardless of whether the
            # source was a TMDB backdrop, a TMDB poster, or a video thumbnail.
            size = (2560, 1440)

            poster_url = movie_doc.get("poster_url")
            fallback_thumb = movie_doc.get("fallback_thumb_file_id")

            resized_poster = None
            if poster_url and not LINK_PREVIEW:
                resized_poster = await fetch_image(poster_url, size)
            elif not poster_url and fallback_thumb:
                # No TMDB/IMDb poster or backdrop at all - use this file's own
                # video thumbnail as a last-resort poster instead of a
                # plain-text post.
                resized_poster = await build_poster_from_telegram_thumb(bot, fallback_thumb, size)

            if resized_poster:
                try:
                    msg = await bot.send_photo(
                        chat_id=MOVIE_UPDATE_CHANNEL,
                        photo=resized_poster,
                        caption=text,
                        reply_markup=buttons,
                        parse_mode=enums.ParseMode.HTML,
                        has_spoiler=fmt.get("spoiler", False)
                    )
                    is_photo = True
                except Exception as e:
                    # Photo captions are capped at 1024 chars by Telegram; if a
                    # post has many quality/episode lines it can exceed that,
                    # so fall back to a text message (4096 char limit) with the
                    # poster shown as a link preview instead of losing the post.
                    # (A video-thumbnail poster has no http URL, so it can only
                    # ever be a link preview when poster_url itself is set.)
                    if "CAPTION_TOO_LONG" in str(e).upper() or "too long" in str(e).lower():
                        text_content = f"<a href='{poster_url}'>&#8205;</a>{text}" if poster_url else text
                        msg = await bot.send_message(
                            chat_id=MOVIE_UPDATE_CHANNEL,
                            text=text_content,
                            reply_markup=buttons,
                            parse_mode=enums.ParseMode.HTML,
                            link_preview_options=LinkPreviewOptions(is_disabled=not bool(poster_url), show_above_text=ABOVE_PREVIEW)
                        )
                        is_photo = False
                    else:
                        raise
            else:
                if poster_url and LINK_PREVIEW:
                    text = f"<a href='{poster_url}'>&#8205;</a>{text}"
                send_params = {
                    "chat_id": MOVIE_UPDATE_CHANNEL,
                    "text": text,
                    "reply_markup": buttons,
                    "parse_mode": enums.ParseMode.HTML
                }
                if poster_url and LINK_PREVIEW:
                    send_params["link_preview_options"] = LinkPreviewOptions(is_disabled=False, show_above_text=ABOVE_PREVIEW)
                else:
                    send_params["link_preview_options"] = LinkPreviewOptions(is_disabled=not LINK_PREVIEW)
                msg = await bot.send_message(**send_params)
                is_photo = False

            await db.movie_updates.update_one(
                {"_id": base_name},
                {"$set": {"message_id": msg.id, "is_photo": is_photo}}
            )
            return msg
        except FloodWait as e:
            wait_time = e.value + 2
            await asyncio.sleep(wait_time)
        except Exception as e:
            logger.error(f"Failed to send movie update: {e}")
            break
    return None

async def update_movie_message(bot, base_name):
    try:
        movie_doc = await db.movie_updates.find_one({"_id": base_name})
        if not movie_doc:
            return

        fmt = await get_post_format(bot.me.id)
        text = build_post_caption(movie_doc, base_name, fmt)
        buttons = build_post_buttons(fmt)

        message_id = movie_doc.get("message_id")
        is_photo = movie_doc.get("is_photo", False)

        if not message_id:
            await send_movie_update(bot, base_name)
            return

        if movie_doc.get("poster_url") and LINK_PREVIEW and not is_photo:
            text = f"<a href='{movie_doc['poster_url']}'>&#8205;</a>{text}"

        try:
            if is_photo:
                await bot.edit_message_caption(
                    chat_id=MOVIE_UPDATE_CHANNEL,
                    message_id=message_id,
                    caption=text,
                    reply_markup=buttons,
                    parse_mode=enums.ParseMode.HTML
                )
            else:
                await bot.edit_message_text(
                    chat_id=MOVIE_UPDATE_CHANNEL,
                    message_id=message_id,
                    text=text,
                    reply_markup=buttons,
                    parse_mode=enums.ParseMode.HTML,
                    link_preview_options=LinkPreviewOptions(is_disabled=not LINK_PREVIEW, show_above_text=ABOVE_PREVIEW)
                )
            return
        except MessageNotModified:
            pass
        except MessageIdInvalid as e:
            logger.warning(f"Message update skipped due to error: {e}")
            pass
        except Exception:
            try:
                await bot.delete_messages(
                    chat_id=MOVIE_UPDATE_CHANNEL,
                    message_ids=message_id
                )
                await db.movie_updates.update_one(
                    {"_id": base_name},
                    {"$set": {"message_id": None, "is_photo": False}}
                )
            except Exception as e:
                logger.error(f"Error during message deletion/update in recovery: {e}")
                pass
            await send_movie_update(bot, base_name)
    except Exception as e:
        logger.error(f"Failed to update movie message for {base_name}: {e}")

DEFAULT_POST_FORMAT = {
    "bold": True,
    "watermark": MOVIE_POST_WATERMARK,
    "link_text": "Click Hare",
    "divider": "────•˚•── ✦ ──•˚•────",
    "title_emoji": "🎬",
    "layout": "twoline",  # "twoline" = "✧ quality :\nlink (size)", "compact" = "quality : link"
    "header_box": True,  # wraps the audio/genres/ott/quality lines (and watermark) in a Telegram quote-box
    "button_text": "",    # optional inline button under the post, e.g. "📢 Join Channel"
    "button_url": "",     # URL for that button; button is shown only if BOTH are set
    "spoiler": False,     # if True, the poster photo is sent as a spoiler (blurred until tapped)
}

_POST_FORMAT_KEYS = {
    "bold": "POST_BOLD",
    "watermark": "POST_WATERMARK",
    "link_text": "POST_LINK_TEXT",
    "divider": "POST_DIVIDER",
    "title_emoji": "POST_TITLE_EMOJI",
    "layout": "POST_LAYOUT",
    "header_box": "POST_HEADER_BOX",
    "button_text": "POST_BUTTON_TEXT",
    "button_url": "POST_BUTTON_URL",
    "spoiler": "POST_SPOILER",
}


async def get_post_format(bot_id):
    """Loads admin-customizable post-format settings (see plugins/post_format.py)
    from the database, falling back to DEFAULT_POST_FORMAT for anything not set."""
    fmt = dict(DEFAULT_POST_FORMAT)
    try:
        for key, db_key in _POST_FORMAT_KEYS.items():
            fmt[key] = await db.get_bot_setting(bot_id, db_key, DEFAULT_POST_FORMAT[key])
    except Exception:
        logger.exception("Failed to load post format settings, using defaults")
    return fmt


class _FileRef:
    """Tiny stand-in object exposing .file_id, matching what the 'allfiles'
    delivery flow in plugins/commands.py expects when bulk-sending a group
    of files under a single deep link."""
    __slots__ = ("file_id",)

    def __init__(self, file_id):
        self.file_id = file_id


_CODEC_TOKENS = {"hevc", "av1", "x264"}
_RES_TOKEN_RE = re.compile(r"^\d{3,4}p$|^4k$", re.IGNORECASE)


def _split_quality(qraw: str):
    """Splits a raw 'quality' string (e.g. 'WEB-DL, 720p, HEVC') into
    (source_tokens, resolution/codec_tokens). Source tokens (WEB-DL, BluRay,
    HDRip, ...) go in the header summary line; resolution/codec tokens
    (720p, HEVC, ...) are what each download line is labelled with."""
    if not qraw or qraw == "N/A":
        return set(), []
    source, res = set(), []
    for token in (t.strip() for t in qraw.split(",")):
        if not token:
            continue
        if _RES_TOKEN_RE.match(token) or token.lower() in _CODEC_TOKENS:
            res.append(token)
        else:
            source.add(token)
    return source, res


def _quality_sort_key(q: str):
    m = re.search(r"(\d{3,4})p", q or "")
    if m:
        return (0, int(m.group(1)), q)
    return (1, 0, q or "")


def _fmt_episode_label(ep) -> str:
    if not ep:
        return "Full Season"
    ep = str(ep)
    if "-" in ep:
        a, b = ep.split("-", 1)
        try:
            return f"E{int(a):02d}-E{int(b):02d}"
        except ValueError:
            return f"E{a}-E{b}"
    try:
        return f"E{int(ep):02d}"
    except ValueError:
        return f"E{ep}"


def _file_link(file_id: str) -> str:
    return f"https://t.me/{temp.U_NAME}?start=file_0_{file_id}"


def _group_link(file_ids) -> str:
    file_ids = [fid for fid in file_ids if fid]
    if not file_ids:
        return ""
    if len(file_ids) == 1:
        return _file_link(file_ids[0])
    key = uuid.uuid4().hex[:12]
    temp.GETALL[key] = [_FileRef(fid) for fid in file_ids]
    return f"https://t.me/{temp.U_NAME}?start=allfiles_0_{key}"


def build_post_buttons(fmt):
    """Returns an InlineKeyboardMarkup with a single admin-configured button
    (see /setbutton in plugins/post_format.py), or None if not configured."""
    text = (fmt or {}).get("button_text") or ""
    url = (fmt or {}).get("button_url") or ""
    if text and url:
        return InlineKeyboardMarkup([[InlineKeyboardButton(text, url=url)]])
    return None


def build_post_caption(movie_doc, base_name, fmt=None):
    """Builds the final auto-post caption. `fmt` (see get_post_format / the
    /postsettings admin command in plugins/post_format.py) controls bold,
    watermark, link text, divider and layout without touching this code."""
    fmt = fmt or DEFAULT_POST_FORMAT
    divider = fmt.get("divider", DEFAULT_POST_FORMAT["divider"])
    link_text = fmt.get("link_text", DEFAULT_POST_FORMAT["link_text"])
    title_emoji = fmt.get("title_emoji", DEFAULT_POST_FORMAT["title_emoji"])
    watermark = fmt.get("watermark", DEFAULT_POST_FORMAT["watermark"])
    layout = fmt.get("layout", DEFAULT_POST_FORMAT["layout"])
    bold = fmt.get("bold", DEFAULT_POST_FORMAT["bold"])
    header_box = fmt.get("header_box", DEFAULT_POST_FORMAT["header_box"])

    files = movie_doc.get("files", [])

    all_languages, all_ott = set(), set()
    seasons_present = set()
    for f in files:
        if f.get("language") and f["language"] != "N/A":
            all_languages.update(x.strip() for x in f["language"].split(",") if x.strip())
        if f.get("ott_platform") and f["ott_platform"] != "N/A":
            all_ott.update(x.strip() for x in f["ott_platform"].split("|") if x.strip())
        if f.get("tag") == "#SERIES" and f.get("season"):
            seasons_present.add(f["season"])

    is_series = bool(seasons_present) or any(f.get("tag") == "#SERIES" for f in files)
    genres = movie_doc.get("genres", "N/A")
    language_str = ", ".join(sorted(all_languages)) if all_languages else "N/A"
    ott_str = ", ".join(sorted(all_ott)) if all_ott else "N/A"

    year_val = str(movie_doc.get("year") or "").strip()
    title = str(movie_doc.get("title") or base_name).strip()
    # Strip a trailing 4-digit year token from the title (not just an exact
    # match of year_val) - a wrong/extra year picked up from the filename
    # (e.g. "Bakaiti 2026" when TMDB's real year is 2025) used to survive
    # this check and print twice ("Bakaiti 2026 2025"). Now we always drop
    # whatever year is baked into the title and re-add the canonical one below.
    # Guard: only strip if something is left afterwards, so a movie whose
    # actual title IS a year (e.g. "1917", "2012", "1984") is never emptied out.
    _stripped_title = re.sub(r"\s*\(?\b(19|20)\d{2}\b\)?\s*$", "", title).strip()
    if _stripped_title:
        title = _stripped_title

    source_tokens = set()
    for f in files:
        src, _ = _split_quality(f.get("quality") or "N/A")
        source_tokens.update(src)
    quality_str = ", ".join(sorted(source_tokens)) if source_tokens else "N/A"

    info_lines = [
        f"🔊 ᴀᴜᴅɪᴏ  : {language_str}",
        f"🎭 ɢᴇɴʀᴇs : {genres}",
        f"🍿 ᴏᴛᴛ : {ott_str}",
        f"🚀 ǫᴜᴀʟɪᴛʏ : {quality_str}",
    ]
    if header_box:
        info_block = "<blockquote>" + "\n".join(info_lines) + "</blockquote>"
    else:
        info_block = "\n".join(info_lines)

    lines = []

    if is_series:
        combined_tag = " #Combined" if any("combined" in (f.get("filename") or "").lower() for f in files) else ""
        header_parts = [title_emoji, title]
        if year_val:
            header_parts.append(year_val)
        if len(seasons_present) == 1:
            season_num = next(iter(seasons_present))
            try:
                header_parts.append(f"[Season {int(season_num):02d}]")
            except (TypeError, ValueError):
                header_parts.append(f"[Season {season_num}]")
        header_title = " ".join(header_parts) + combined_tag

        lines += [header_title, divider, info_block, divider, ""]

        groups = defaultdict(lambda: defaultdict(list))
        for f in files:
            if f.get("tag") != "#SERIES" or not f.get("file_id"):
                continue
            _, res_tokens = _split_quality(f.get("quality") or "N/A")
            qlabel = " ".join(res_tokens) if res_tokens else "Unknown"
            key = (f.get("season"), f.get("episode"))
            groups[qlabel][key].append(f)

        for qlabel in sorted(groups.keys(), key=_quality_sort_key):
            lines.append(f"✧  {qlabel} : ")
            season_eps = groups[qlabel]
            for (season, ep), flist in sorted(
                season_eps.items(),
                key=lambda kv: (int(kv[0][0]) if kv[0][0] else 0, str(kv[0][1] or ""))
            ):
                ep_label = _fmt_episode_label(ep)
                if len(seasons_present) > 1 and season:
                    try:
                        ep_label = f"S{int(season):02d} {ep_label}"
                    except (TypeError, ValueError):
                        ep_label = f"S{season} {ep_label}"
                file_ids = [x["file_id"] for x in flist]
                total_size = sum(x.get("file_size") or 0 for x in flist)
                link = _group_link(file_ids)
                size_str = f" ({get_size(total_size)})" if total_size else ""
                if link:
                    lines.append(f"{ep_label} : <a href='{link}'>{link_text}</a>{size_str}")
            lines.append("")
    else:
        header_title = f"{title_emoji} {title} {year_val}".strip()
        lines += [header_title, divider, info_block, divider, ""]

        movie_files = [f for f in files if f.get("tag") != "#SERIES" and f.get("file_id")]
        movie_files.sort(key=lambda f: _quality_sort_key(f.get("quality") or ""))
        for f in movie_files:
            _, res_tokens = _split_quality(f.get("quality") or "N/A")
            qlabel = " ".join(res_tokens) if res_tokens else "Unknown"
            link = _file_link(f["file_id"])
            size_str = f" ({get_size(f['file_size'])})" if f.get("file_size") else ""
            if layout == "compact":
                lines.append(f"{qlabel} : <a href='{link}'>{link_text}</a>{size_str}")
            else:
                lines.append(f"✧  {qlabel} : ")
                lines.append(f"<a href='{link}'>{link_text}</a>{size_str}")
                lines.append("")

    if watermark:
        wm_line = f"💢 ᴘᴏᴡᴇʀᴇᴅ ʙʏ : {watermark}"
        if header_box:
            wm_line = f"<blockquote>{wm_line}</blockquote>"
        lines.append(wm_line)

    caption = "\n".join(lines).strip()
    return f"<b>{caption}</b>" if bold else caption
