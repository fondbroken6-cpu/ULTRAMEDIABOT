
## 3. bot.py

```python
import asyncio
import logging
import os
import re
import shutil
import tempfile
import uuid
from pathlib import Path
from typing import Any
from urllib.parse import quote_plus

import aiohttp
import imageio_ffmpeg
import yt_dlp
from aiogram import Bot, Dispatcher, F
from aiogram.exceptions import TelegramBadRequest, TelegramConflictError
from aiogram.filters import CommandStart
from aiogram.types import (
    CallbackQuery,
    FSInputFile,
    InlineKeyboardButton,
    InlineKeyboardMarkup,
    Message,
    URLInputFile,
)
from dotenv import load_dotenv
from shazamio import Shazam


load_dotenv()

BOT_TOKEN = os.getenv("BOT_TOKEN")

if not BOT_TOKEN:
    raise RuntimeError("BOT_TOKEN environment variable topilmadi.")

BOT_NAME = "UltraMediaBot"
BOT_USERNAME = "@Ultraasave_bot"

DEEZER_API = "https://api.deezer.com/search"
LRCLIB_API = "https://lrclib.net/api/search"

COOKIES_FILE = Path(__file__).with_name("cookies.txt")

SEARCH_LIMIT = 10
MAX_LYRICS_LENGTH = 3500

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s | %(levelname)s | %(name)s | %(message)s",
)

logger = logging.getLogger(BOT_NAME)

bot = Bot(token=BOT_TOKEN)
dp = Dispatcher()
shazam = Shazam()

user_links: dict[int, str] = {}
search_sessions: dict[str, dict[str, Any]] = {}


def media_keyboard() -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup(
        inline_keyboard=[
            [
                InlineKeyboardButton(
                    text="📹 Video",
                    callback_data="media:video",
                ),
                InlineKeyboardButton(
                    text="🎵 MP3",
                    callback_data="media:mp3",
                ),
            ],
            [
                InlineKeyboardButton(
                    text="🎼 Identify Music",
                    callback_data="media:identify",
                )
            ],
        ]
    )


def search_keyboard(token: str, count: int) -> InlineKeyboardMarkup:
    buttons = [
        InlineKeyboardButton(
            text=str(index + 1),
            callback_data=f"song:{token}:{index}",
        )
        for index in range(count)
    ]

    rows = []

    if buttons[:5]:
        rows.append(buttons[:5])

    if buttons[5:10]:
        rows.append(buttons[5:10])

    return InlineKeyboardMarkup(inline_keyboard=rows)


def is_supported_link(text: str) -> bool:
    value = text.lower().strip()

    patterns = (
        r"https?://([^/]+\.)?tiktok\.com/",
        r"https?://([^/]+\.)?instagram\.com/(reel|reels|p)/",
        r"https?://([^/]+\.)?instagr\.am/",
        r"https?://([^/]+\.)?youtube\.com/shorts/",
        r"https?://youtu\.be/",
    )

    return any(
        re.search(pattern, value)
        for pattern in patterns
    )


def is_instagram_link(url: str) -> bool:
    value = url.lower()

    return (
        "instagram.com/" in value
        or "instagr.am/" in value
    )


def format_duration(value: Any) -> str:
    try:
        total = max(0, int(value or 0))
    except (TypeError, ValueError):
        return "--:--"

    minutes, seconds = divmod(total, 60)
    hours, minutes = divmod(minutes, 60)

    if hours:
        return f"{hours}:{minutes:02d}:{seconds:02d}"

    return f"{minutes}:{seconds:02d}"


def official_links(song: dict[str, Any]) -> str:
    query_text = (
        f"{song.get('artist', '')} "
        f"{song.get('title', '')}"
    ).strip()

    query = quote_plus(query_text)
    deezer_link = song.get("link") or "Topilmadi"

    return (
        f"🎧 Spotify: https://open.spotify.com/search/{query}\n"
        f"🍎 Apple Music: "
        f"https://music.apple.com/search?term={query}\n"
        f"▶️ YouTube: "
        f"https://www.youtube.com/results?search_query={query}\n"
        f"🎶 Deezer: {deezer_link}"
    )


def search_results_text(
    songs: list[dict[str, Any]],
) -> str:
    lines = [
        "🔎 Qidiruv natijalari",
        "",
    ]

    for index, song in enumerate(songs, start=1):
        lines.append(
            f"{index}. {song['artist']} — "
            f"{song['title']} "
            f"({format_duration(song.get('duration'))})"
        )

    lines.extend(
        [
            "",
            "Preview uchun pastdagi raqamni bosing.",
        ]
    )

    return "\n".join(lines)


def create_search_session(
    user_id: int,
    songs: list[dict[str, Any]],
) -> str:
    token = uuid.uuid4().hex[:10]

    search_sessions[token] = {
        "user_id": user_id,
        "songs": songs,
    }

    if len(search_sessions) > 1000:
        old_tokens = list(search_sessions)[:200]

        for old_token in old_tokens:
            search_sessions.pop(old_token, None)

    return token


def get_session_song(
    token: str,
    index: int,
    user_id: int,
) -> dict[str, Any] | None:
    session = search_sessions.get(token)

    if not session:
        return None

    if session.get("user_id") != user_id:
        return None

    songs = session.get("songs") or []

    if index < 0 or index >= len(songs):
        return None

    return songs[index]


def parse_song_callback(
    data: str | None,
) -> tuple[str, int] | None:
    try:
        _, token, raw_index = (
            data or ""
        ).split(":", maxsplit=2)

        return token, int(raw_index)

    except (TypeError, ValueError):
        return None


async def safe_edit(
    message: Message,
    text: str,
) -> None:
    try:
        await message.edit_text(text)

    except TelegramBadRequest:
        await message.answer(text)


def user_error(
    error: Exception,
    url: str,
    mode: str,
) -> str:
    text = str(error).lower()

    if "empty media response" in text:
        return (
            "❌ Instagram media bermadi. Cookies kerak "
            "yoki post yopiq bo‘lishi mumkin."
        )

    private_markers = (
        "private",
        "login required",
        "requested content is not available",
        "this content isn't available",
    )

    if is_instagram_link(url):
        if any(
            marker in text
            for marker in private_markers
        ):
            return (
                "❌ Instagram posti yopiq yoki "
                "mavjud emas."
            )

    blocked_markers = (
        "http error 403",
        "http error 429",
        "forbidden",
        "too many requests",
        "rate limit",
        "captcha",
    )

    if any(
        marker in text
        for marker in blocked_markers
    ):
        return (
            "❌ Platforma yuklash so‘rovini blokladi. "
            "Keyinroq urinib ko‘ring."
        )

    url_markers = (
        "no video formats found",
        "unable to extract",
        "unsupported url",
        "requested format is not available",
        "media fayl topilmadi",
    )

    if any(
        marker in text
        for marker in url_markers
    ):
        return (
            "❌ Media URL topilmadi yoki havola "
            "qo‘llab-quvvatlanmaydi."
        )

    large_markers = (
        "file is too big",
        "request entity too large",
        "too large",
        "error 413",
    )

    if any(
        marker in text
        for marker in large_markers
    ):
        return (
            "❌ Fayl Telegram orqali yuborish "
            "uchun juda katta."
        )

    if mode != "video":
        audio_markers = (
            "no audio",
            "audio stream",
            "bestaudio",
        )

        if any(
            marker in text
            for marker in audio_markers
        ):
            return "❌ Bu videoda audio topilmadi."

    messages = {
        "video": "❌ Video yuklashda xatolik bo‘ldi.",
        "mp3": "❌ MP3 tayyorlashda xatolik bo‘ldi.",
        "identify": (
            "❌ Musiqani aniqlash uchun audio olinmadi."
        ),
    }

    return messages.get(
        mode,
        "❌ Xatolik yuz berdi.",
    )


def base_ydl_options() -> dict[str, Any]:
    options: dict[str, Any] = {
        "quiet": True,
        "no_warnings": True,
        "noplaylist": True,
        "ignoreerrors": False,
        "retries": 3,
        "fragment_retries": 3,
        "socket_timeout": 30,
        "nocheckcertificate": True,
        "ffmpeg_location": (
            imageio_ffmpeg.get_ffmpeg_exe()
        ),
        "http_headers": {
            "User-Agent": (
                "Mozilla/5.0 "
                "(Windows NT 10.0; Win64; x64) "
                "AppleWebKit/537.36 "
                "(KHTML, like Gecko) "
                "Chrome/124.0.0.0 Safari/537.36"
            ),
            "Accept-Language": "en-US,en;q=0.9",
        },
    }

    if COOKIES_FILE.is_file():
        options["cookiefile"] = str(COOKIES_FILE)

        logger.info(
            "cookies.txt enabled | path=%s",
            COOKIES_FILE,
        )

    return options


def clear_folder(folder: str) -> None:
    path = Path(folder)

    if not path.exists():
        return

    for item in path.iterdir():
        try:
            if item.is_dir():
                shutil.rmtree(
                    item,
                    ignore_errors=True,
                )
            else:
                item.unlink(missing_ok=True)

        except Exception:
            logger.exception(
                "Temporary file cleanup failed | "
                "item=%s",
                item,
            )


def find_media_file(
    folder: str,
    suffix: str,
) -> str:
    files = [
        item
        for item in Path(folder).iterdir()
        if item.is_file()
        and item.suffix.lower() == suffix
        and not item.name.endswith(
            (".part", ".ytdl")
        )
    ]

    if not files:
        raise RuntimeError(
            f"{suffix} media fayl topilmadi."
        )

    files.sort(
        key=lambda item: item.stat().st_size,
        reverse=True,
    )

    return str(files[0])


def download_media(
    url: str,
    mode: str,
) -> tuple[str, str]:
    folder = tempfile.mkdtemp(
        prefix="ultramedia_"
    )

    options = base_ydl_options()

    if mode == "video":
        formats = [
            "best[ext=mp4]/best",
            "bestvideo+bestaudio/best",
            "best",
        ]

        options.update(
            {
                "outtmpl": os.path.join(
                    folder,
                    "video.%(ext)s",
                ),
                "merge_output_format": "mp4",
            }
        )

        expected_suffix = ".mp4"

    elif mode in {"mp3", "identify"}:
        formats = ["bestaudio/best"]

        options.update(
            {
                "outtmpl": os.path.join(
                    folder,
                    "audio.%(ext)s",
                ),
                "postprocessors": [
                    {
                        "key": "FFmpegExtractAudio",
                        "preferredcodec": "mp3",
                        "preferredquality": "192",
                    }
                ],
            }
        )

        expected_suffix = ".mp3"

    else:
        shutil.rmtree(
            folder,
            ignore_errors=True,
        )

        raise ValueError(
            "Noto‘g‘ri yuklash turi."
        )

    last_error: Exception | None = None

    try:
        for attempt, format_selector in enumerate(
            formats,
            start=1,
        ):
            options["format"] = format_selector

            try:
                logger.info(
                    "Download attempt | mode=%s | "
                    "attempt=%s/%s | format=%s | "
                    "url=%s",
                    mode,
                    attempt,
                    len(formats),
                    format_selector,
                    url,
                )

                with yt_dlp.YoutubeDL(options) as ydl:
                    info = ydl.extract_info(
                        url,
                        download=True,
                    )

                if not info:
                    raise RuntimeError(
                        "empty media response"
                    )

                path = find_media_file(
                    folder,
                    expected_suffix,
                )

                if mode in {"mp3", "identify"}:
                    target = os.path.join(
                        folder,
                        "audio.mp3",
                    )

                    if (
                        os.path.abspath(path)
                        != os.path.abspath(target)
                    ):
                        os.replace(path, target)

                    path = target

                if os.path.getsize(path) <= 0:
                    raise RuntimeError(
                        "Yuklangan media fayli bo‘sh."
                    )

                logger.info(
                    "Download complete | mode=%s | "
                    "size=%s | url=%s",
                    mode,
                    os.path.getsize(path),
                    url,
                )

                return path, folder

            except Exception as error:
                last_error = error

                logger.exception(
                    "Download attempt failed | "
                    "mode=%s | attempt=%s/%s | "
                    "url=%s | error=%r",
                    mode,
                    attempt,
                    len(formats),
                    url,
                    error,
                )

                clear_folder(folder)

        raise last_error or RuntimeError(
            "Media fayl topilmadi."
        )

    except Exception:
        shutil.rmtree(
            folder,
            ignore_errors=True,
        )

        raise


async def extract_sample(
    audio_path: str,
    folder: str,
    start_second: int,
    index: int,
) -> str:
    sample_path = os.path.join(
        folder,
        f"sample_{index}.mp3",
    )

    process = await asyncio.create_subprocess_exec(
        imageio_ffmpeg.get_ffmpeg_exe(),
        "-y",
        "-ss",
        str(start_second),
        "-i",
        audio_path,
        "-t",
        "20",
        "-vn",
        "-ac",
        "1",
        "-ar",
        "44100",
        "-b:a",
        "128k",
        sample_path,
        stdout=asyncio.subprocess.DEVNULL,
        stderr=asyncio.subprocess.PIPE,
    )

    _, stderr = await process.communicate()

    if (
        process.returncode != 0
        or not os.path.exists(sample_path)
    ):
        details = stderr.decode(
            "utf-8",
            errors="replace",
        )[-1000:]

        raise RuntimeError(
            f"Audio sample tayyorlanmadi: "
            f"{details}"
        )

    if os.path.getsize(sample_path) < 1000:
        raise RuntimeError(
            "Audio sample juda qisqa yoki bo‘sh."
        )

    return sample_path


def parse_shazam(
    result: dict[str, Any],
) -> dict[str, Any] | None:
    track = result.get("track") or {}

    if not track.get("title"):
        return None

    album = None

    for section in track.get("sections") or []:
        for item in section.get("metadata") or []:
            if (
                str(
                    item.get("title", "")
                ).lower()
                == "album"
            ):
                album = item.get("text")
                break

    images = track.get("images") or {}

    return {
        "title": track.get("title"),
        "artist": (
            track.get("subtitle")
            or "Noma’lum artist"
        ),
        "album": album,
        "cover": (
            images.get("coverarthq")
            or images.get("coverart")
        ),
        "shazam_link": track.get("url"),
    }


async def identify_song(
    audio_path: str,
    folder: str,
) -> dict[str, Any] | None:
    windows = [
        (0, "0–20"),
        (5, "5–25"),
        (10, "10–30"),
    ]

    for index, (
        start,
        label,
    ) in enumerate(windows, start=1):
        try:
            sample_path = await extract_sample(
                audio_path,
                folder,
                start,
                index,
            )

            logger.info(
                "Shazam attempt | window=%s",
                label,
            )

            raw_result = await shazam.recognize(
                sample_path
            )

            result = parse_shazam(raw_result)

            if result:
                logger.info(
                    "Shazam match | artist=%s | "
                    "title=%s | window=%s",
                    result["artist"],
                    result["title"],
                    label,
                )

                return result

        except Exception as error:
            logger.exception(
                "Shazam failed | window=%s | "
                "error=%r",
                label,
                error,
            )

    return None


async def search_deezer(
    query: str,
) -> list[dict[str, Any]]:
    timeout = aiohttp.ClientTimeout(total=20)

    async with aiohttp.ClientSession(
        timeout=timeout
    ) as session:
        async with session.get(
            DEEZER_API,
            params={
                "q": query,
                "limit": SEARCH_LIMIT,
            },
        ) as response:
            if response.status != 200:
                raise RuntimeError(
                    f"Deezer HTTP {response.status}"
                )

            data = await response.json(
                content_type=None
            )

    songs: list[dict[str, Any]] = []

    for item in (
        data.get("data") or []
    )[:SEARCH_LIMIT]:
        artist = item.get("artist") or {}
        album = item.get("album") or {}

        songs.append(
            {
                "title": (
                    item.get("title")
                    or "Noma’lum qo‘shiq"
                ),
                "artist": (
                    artist.get("name")
                    or "Noma’lum artist"
                ),
                "album": album.get("title"),
                "duration": item.get("duration"),
                "preview": item.get("preview"),
                "link": item.get("link"),
                "cover": (
                    album.get("cover_big")
                    or album.get("cover_medium")
                ),
            }
        )

    return songs


async def find_lyrics(
    title: str,
    artist: str,
) -> str | None:
    timeout = aiohttp.ClientTimeout(total=20)

    async with aiohttp.ClientSession(
        timeout=timeout
    ) as session:
        async with session.get(
            LRCLIB_API,
            params={
                "track_name": title,
                "artist_name": artist,
            },
        ) as response:
            if response.status != 200:
                logger.warning(
                    "LRCLIB failed | status=%s | "
                    "artist=%s | title=%s",
                    response.status,
                    artist,
                    title,
                )

                return None

            data = await response.json(
                content_type=None
            )

    if not isinstance(data, list):
        return None

    if not data:
        return None

    lyrics = (
        data[0].get("plainLyrics")
        or data[0].get("syncedLyrics")
    )

    if not lyrics:
        return None

    if len(lyrics) > MAX_LYRICS_LENGTH:
        lyrics = (
            lyrics[:MAX_LYRICS_LENGTH]
            + "\n\n..."
        )

    return lyrics


async def download_preview(
    url: str,
) -> tuple[str, str]:
    folder = tempfile.mkdtemp(
        prefix="ultramedia_preview_"
    )

    path = os.path.join(
        folder,
        "preview.mp3",
    )

    try:
        timeout = aiohttp.ClientTimeout(total=60)

        async with aiohttp.ClientSession(
            timeout=timeout
        ) as session:
            async with session.get(
                url
            ) as response:
                if response.status != 200:
                    raise RuntimeError(
                        "Deezer preview HTTP "
                        f"{response.status}"
                    )

                with open(path, "wb") as file:
                    async for chunk in (
                        response.content.iter_chunked(
                            128 * 1024
                        )
                    ):
                        file.write(chunk)

        if not os.path.exists(path):
            raise RuntimeError(
                "Preview fayli topilmadi."
            )

        if os.path.getsize(path) <= 0:
            raise RuntimeError(
                "Preview fayli bo‘sh."
            )

        return path, folder

    except Exception:
        shutil.rmtree(
            folder,
            ignore_errors=True,
        )

        raise


async def send_recognition(
    message: Message,
    song: dict[str, Any],
) -> None:
    text = (
        f"🎵 {song['title']}\n"
        f"👤 {song['artist']}\n"
        f"💿 "
        f"{song.get('album') or 'Topilmadi'}\n"
        f"🔗 "
        f"{song.get('shazam_link') or 'Shazam link topilmadi'}"
    )

    cover = song.get("cover")

    if cover:
        try:
            await message.answer_photo(
                photo=URLInputFile(cover),
                caption=text,
            )

            return

        except Exception as error:
            logger.warning(
                "Cover send failed | error=%r",
                error,
            )

    await message.answer(text)


@dp.message(CommandStart())
async def start_handler(
    message: Message,
) -> None:
    await message.answer(
        "🚀 UltraMediaBot\n\n"
        "📹 Video download\n"
        "🎵 MP3 extract\n"
        "🎼 Music identification\n"
        "🔎 Song search\n"
        "📝 Lyrics\n\n"
        "Media havolasi, qo‘shiq nomi "
        "yoki artist yuboring."
    )


@dp.message()
async def message_handler(
    message: Message,
) -> None:
    text = (message.text or "").strip()

    if not text:
        await message.answer(
            "Havola, qo‘shiq nomi yoki "
            "artist yuboring."
        )
        return

    if is_supported_link(text):
        user_links[message.from_user.id] = text

        logger.info(
            "Link accepted | user_id=%s | url=%s",
            message.from_user.id,
            text,
        )

        await message.answer(
            "✅ Havola qabul qilindi. "
            "Amalni tanlang:",
            reply_markup=media_keyboard(),
        )

        return

    if text.startswith(
        ("http://", "https://")
    ):
        await message.answer(
            "❌ Bu havola qo‘llab-quvvatlanmaydi."
        )
        return

    status = await message.answer(
        "🔎 Deezer orqali qidirilmoqda..."
    )

    try:
        songs = await search_deezer(text)

        if not songs:
            await safe_edit(
                status,
                "❌ Qo‘shiq topilmadi.",
            )
            return

        token = create_search_session(
            message.from_user.id,
            songs,
        )

        await status.edit_text(
            search_results_text(songs),
            reply_markup=search_keyboard(
                token,
                len(songs),
            ),
        )

    except Exception as error:
        logger.exception(
            "Song search failed | user_id=%s | "
            "query=%s | error=%r",
            message.from_user.id,
            text,
            error,
        )

        await safe_edit(
            status,
            "❌ Qo‘shiq qidirishda "
            "xatolik bo‘ldi.",
        )


async def process_media_callback(
    callback: CallbackQuery,
    mode: str,
) -> None:
    url = user_links.get(callback.from_user.id)
    folder: str | None = None
    status: Message | None = None

    try:
        if not url:
            await callback.message.answer(
                "❌ Havola topilmadi. "
                "Qaytadan yuboring."
            )
            return

        status_messages = {
            "video": "⏳ Video yuklanmoqda...",
            "mp3": "🎵 MP3 tayyorlanmoqda...",
            "identify": (
                "🎼 Musiqa aniqlanmoqda..."
            ),
        }

        status = await callback.message.answer(
            status_messages[mode]
        )

        path, folder = await asyncio.to_thread(
            download_media,
            url,
            mode,
        )

        if mode == "video":
            await callback.message.answer_video(
                video=FSInputFile(path),
                caption=(
                    "✅ Video tayyor!\n\n"
                    f"{BOT_USERNAME}"
                ),
            )

        elif mode == "mp3":
            await callback.message.answer_audio(
                audio=FSInputFile(
                    path,
                    filename="audio.mp3",
                ),
                caption=(
                    "✅ MP3 tayyor!\n\n"
                    f"{BOT_USERNAME}"
                ),
            )

        else:
            song = await identify_song(
                path,
                folder,
            )

            if not song:
                await safe_edit(
                    status,
                    "❌ Musiqa aniqlanmadi. "
                    "Audio shovqinli yoki "
                    "Shazam bazasida yo‘q.",
                )
                return

            await status.delete()
            status = None

            await send_recognition(
                callback.message,
                song,
            )

        if status:
            await status.delete()

    except Exception as error:
        logger.exception(
            "Media operation failed | "
            "mode=%s | user_id=%s | "
            "url=%s | error=%r",
            mode,
            callback.from_user.id,
            url,
            error,
        )

        text = user_error(
            error,
            url or "",
            mode,
        )

        if status:
            await safe_edit(status, text)
        else:
            await callback.message.answer(text)

    finally:
        if folder:
            shutil.rmtree(
                folder,
                ignore_errors=True,
            )


@dp.callback_query(F.data == "media:video")
async def video_callback(
    callback: CallbackQuery,
) -> None:
    try:
        await process_media_callback(
            callback,
            "video",
        )
    finally:
        await callback.answer()


@dp.callback_query(F.data == "media:mp3")
async def mp3_callback(
    callback: CallbackQuery,
) -> None:
    try:
        await process_media_callback(
            callback,
            "mp3",
        )
    finally:
        await callback.answer()


@dp.callback_query(F.data == "media:identify")
async def identify_callback(
    callback: CallbackQuery,
) -> None:
    try:
        await process_media_callback(
            callback,
            "identify",
        )
    finally:
        await callback.answer()


@dp.callback_query(F.data.startswith("song:"))
async def song_callback(
    callback: CallbackQuery,
) -> None:
    folder: str | None = None

    try:
        parsed = parse_song_callback(
            callback.data
        )

        if not parsed:
            await callback.message.answer(
                "❌ Noto‘g‘ri tanlov."
            )
            return

        token, index = parsed

        song = get_session_song(
            token,
            index,
            callback.from_user.id,
        )

        if not song:
            await callback.message.answer(
                "❌ Natija eskirgan. "
                "Qayta qidiring."
            )
            return

        preview_url = song.get("preview")

        if preview_url:
            path, folder = await download_preview(
                preview_url
            )

            caption = (
                "🎵 Deezer preview\n\n"
                f"🎵 {song['title']}\n"
                f"👤 {song['artist']}\n"
                f"💿 "
                f"{song.get('album') or 'Topilmadi'}\n"
                f"⏱ "
                f"{format_duration(song.get('duration'))}\n\n"
                f"{official_links(song)}\n\n"
                f"{BOT_USERNAME}"
            )

            await callback.message.answer_audio(
                audio=FSInputFile(
                    path,
                    filename="preview.mp3",
                ),
                caption=caption,
            )

        else:
            await callback.message.answer(
                "❌ Preview topilmadi.\n\n"
                f"{official_links(song)}"
            )

        lyrics = await find_lyrics(
            song["title"],
            song["artist"],
        )

        if lyrics:
            await callback.message.answer(
                f"📝 {song['artist']} — "
                f"{song['title']}\n\n"
                f"{lyrics}"
            )
        else:
            await callback.message.answer(
                "📝 Matn topilmadi."
            )

    except Exception as error:
        logger.exception(
            "Song selection failed | "
            "user_id=%s | data=%s | error=%r",
            callback.from_user.id,
            callback.data,
            error,
        )

        await callback.message.answer(
            "❌ Natijani yuborishda "
            "xatolik bo‘ldi."
        )

    finally:
        if folder:
            shutil.rmtree(
                folder,
                ignore_errors=True,
            )

        await callback.answer()


async def main() -> None:
    # TelegramConflictError oldini olish uchun
    # Render'da faqat bitta worker ishlating.
    await bot.delete_webhook(
        drop_pending_updates=True
    )

    logger.info(
        "%s ishga tushdi",
        BOT_NAME,
    )

    try:
        await dp.start_polling(bot)

    except TelegramConflictError:
        logger.exception(
            "Boshqa bot instance long polling "
            "qilmoqda."
        )
        raise

    finally:
        await bot.session.close()


if __name__ == "__main__":
    asyncio.run(main())
