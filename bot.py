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
    raise RuntimeError("BOT_TOKEN muhit o'zgaruvchisi topilmadi.")

BOT_NAME = "UltraMediaBot"
BOT_USERNAME = "@Ultraasave_bot"

DEEZER_API_URL = "https://api.deezer.com/search"
LRCLIB_API_URL = "https://lrclib.net/api/search"
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
user_modes: dict[int, str] = {}
search_sessions: dict[str, dict[str, Any]] = {}


def main_menu() -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup(
        inline_keyboard=[
            [
                InlineKeyboardButton(
                    text="🎥 Video yuklab olish",
                    callback_data="menu:video",
                )
            ],
            [
                InlineKeyboardButton(
                    text="🎵 MP3 ajratish",
                    callback_data="menu:mp3",
                )
            ],
            [
                InlineKeyboardButton(
                    text="🎼 Musiqani aniqlash",
                    callback_data="menu:identify",
                )
            ],
            [
                InlineKeyboardButton(
                    text="🔍 Qo'shiq qidirish",
                    callback_data="menu:search",
                )
            ],
            [
                InlineKeyboardButton(
                    text="📝 Qo'shiq matni",
                    callback_data="menu:lyrics",
                )
            ],
        ]
    )


def media_menu() -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup(
        inline_keyboard=[
            [
                InlineKeyboardButton(
                    text="🎥 Video yuklab olish",
                    callback_data="media:video",
                )
            ],
            [
                InlineKeyboardButton(
                    text="🎵 MP3 ajratish",
                    callback_data="media:mp3",
                )
            ],
            [
                InlineKeyboardButton(
                    text="🎼 Musiqani aniqlash",
                    callback_data="media:identify",
                )
            ],
        ]
    )


def search_results_menu(
    token: str,
    count: int,
) -> InlineKeyboardMarkup:
    buttons = [
        InlineKeyboardButton(
            text=str(index + 1),
            callback_data=f"song:{token}:{index}",
        )
        for index in range(count)
    ]

    rows: list[list[InlineKeyboardButton]] = []

    if buttons[:5]:
        rows.append(buttons[:5])

    if buttons[5:10]:
        rows.append(buttons[5:10])

    return InlineKeyboardMarkup(inline_keyboard=rows)


def is_supported_link(text: str) -> bool:
    value = text.strip().lower()

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
        total_seconds = max(0, int(value or 0))
    except (TypeError, ValueError):
        return "Noma'lum"

    minutes, seconds = divmod(total_seconds, 60)
    hours, minutes = divmod(minutes, 60)

    if hours:
        return f"{hours}:{minutes:02d}:{seconds:02d}"

    return f"{minutes}:{seconds:02d}"


def official_links(song: dict[str, Any]) -> str:
    title = str(song.get("title") or "")
    artist = str(song.get("artist") or "")
    query = quote_plus(f"{artist} {title}".strip())
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
        "🔍 Qo'shiq qidirish natijalari:",
        "",
    ]

    for index, song in enumerate(songs, start=1):
        lines.append(
            f"{index}. {song['artist']} — {song['title']} "
            f"({format_duration(song.get('duration'))})"
        )

    lines.extend(
        [
            "",
            "Tinglash uchun natija raqamini bosing.",
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


async def safe_callback_answer(
    callback: CallbackQuery,
    text: str | None = None,
) -> None:
    try:
        await callback.answer(text)

    except TelegramBadRequest:
        pass


def user_error_message(
    error: Exception,
    url: str,
    mode: str,
) -> str:
    error_text = str(error).lower()

    if "empty media response" in error_text:
        return (
            "❌ Instagram media bermadi. Cookies kerak "
            "yoki post yopiq bo'lishi mumkin."
        )

    private_markers = (
        "private",
        "login required",
        "requested content is not available",
        "this content isn't available",
        "content is unavailable",
        "not available",
    )

    if is_instagram_link(url):
        if any(
            marker in error_text
            for marker in private_markers
        ):
            return (
                "❌ Ushbu post yopiq hisobga tegishli "
                "yoki mavjud emas."
            )

    cookies_markers = (
        "cookies",
        "cookie",
        "login_required",
        "authentication required",
    )

    if is_instagram_link(url):
        if any(
            marker in error_text
            for marker in cookies_markers
        ):
            return (
                "❌ Instagram ushbu media uchun kirishni "
                "chekladi. cookies.txt fayli kerak bo'lishi mumkin."
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
        marker in error_text
        for marker in blocked_markers
    ):
        return (
            "❌ Platforma yuklash so'rovini vaqtincha "
            "blokladi. Keyinroq qayta urinib ko'ring."
        )

    media_markers = (
        "no video formats found",
        "unable to extract",
        "unsupported url",
        "requested format is not available",
        "media fayl topilmadi",
    )

    if any(
        marker in error_text
        for marker in media_markers
    ):
        if is_instagram_link(url):
            return "❌ Instagram videosi topilmadi."

        return (
            "❌ Media topilmadi yoki havola "
            "qo'llab-quvvatlanmaydi."
        )

    large_file_markers = (
        "file is too big",
        "request entity too large",
        "too large",
        "error 413",
    )

    if any(
        marker in error_text
        for marker in large_file_markers
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
            "audio fayl topilmadi",
        )

        if any(
            marker in error_text
            for marker in audio_markers
        ):
            return "❌ Ushbu videoda audio topilmadi."

    messages = {
        "video": "❌ Video yuklab olinmadi.",
        "mp3": "❌ MP3 ajratib bo'lmadi.",
        "identify": "❌ Musiqani aniqlash uchun audio olinmadi.",
    }

    return messages.get(
        mode,
        "❌ Serverda xatolik yuz berdi.",
    )


def ytdlp_base_options() -> dict[str, Any]:
    options: dict[str, Any] = {
        "quiet": True,
        "no_warnings": True,
        "noplaylist": True,
        "ignoreerrors": False,
        "retries": 5,
        "fragment_retries": 5,
        "extractor_retries": 3,
        "file_access_retries": 3,
        "socket_timeout": 30,
        "nocheckcertificate": True,
        "geo_bypass": True,
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
            "Accept": (
                "text/html,application/xhtml+xml,"
                "application/xml;q=0.9,image/avif,"
                "image/webp,*/*;q=0.8"
            ),
            "Accept-Language": "en-US,en;q=0.9",
        },
    }

    if COOKIES_FILE.is_file():
        options["cookiefile"] = str(COOKIES_FILE)

        logger.info(
            "cookies.txt ishlatilmoqda | manzil=%s",
            COOKIES_FILE,
        )

    return options


def clear_temporary_folder(folder: str) -> None:
    folder_path = Path(folder)

    if not folder_path.exists():
        return

    for item in folder_path.iterdir():
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
                "Vaqtinchalik fayl o'chirilmadi | fayl=%s",
                item,
            )


def find_downloaded_file(
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

    options = ytdlp_base_options()

    if mode == "video":
        formats = [
            "best[ext=mp4]/best",
            "bestvideo[ext=mp4]+bestaudio[ext=m4a]/best",
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
        formats = [
            "bestaudio[ext=m4a]/bestaudio/best",
        ]

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
            "Noto'g'ri yuklash turi."
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
                    "Yuklash boshlandi | tur=%s | "
                    "urinish=%s/%s | format=%s | havola=%s",
                    mode,
                    attempt,
                    len(formats),
                    format_selector,
                    url,
                )

                with yt_dlp.YoutubeDL(options) as ydl:
                    information = ydl.extract_info(
                        url,
                        download=True,
                    )

                if not information:
                    raise RuntimeError(
                        "empty media response"
                    )

                path = find_downloaded_file(
                    folder,
                    expected_suffix,
                )

                if mode in {"mp3", "identify"}:
                    audio_path = os.path.join(
                        folder,
                        "audio.mp3",
                    )

                    if (
                        os.path.abspath(path)
                        != os.path.abspath(audio_path)
                    ):
                        os.replace(
                            path,
                            audio_path,
                        )

                    path = audio_path

                file_size = os.path.getsize(path)

                if file_size <= 0:
                    raise RuntimeError(
                        "Yuklangan media fayli bo'sh."
                    )

                logger.info(
                    "Yuklash tugadi | tur=%s | "
                    "hajm=%s | havola=%s",
                    mode,
                    file_size,
                    url,
                )

                return path, folder

            except Exception as error:
                last_error = error

                logger.exception(
                    "Yuklash urinishi muvaffaqiyatsiz | "
                    "tur=%s | urinish=%s/%s | "
                    "havola=%s | xato=%r",
                    mode,
                    attempt,
                    len(formats),
                    url,
                    error,
                )

                clear_temporary_folder(folder)

        raise last_error or RuntimeError(
            "Media fayl topilmadi."
        )

    except Exception:
        shutil.rmtree(
            folder,
            ignore_errors=True,
        )

        raise


async def extract_audio_sample(
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
        technical_error = stderr.decode(
            "utf-8",
            errors="replace",
        )[-1500:]

        raise RuntimeError(
            "Audio namunasi tayyorlanmadi: "
            f"{technical_error}"
        )

    if os.path.getsize(sample_path) < 1000:
        raise RuntimeError(
            "Audio namunasi juda qisqa yoki bo'sh."
        )

    return sample_path


def parse_shazam_result(
    result: dict[str, Any],
) -> dict[str, Any] | None:
    track = result.get("track") or {}

    title = track.get("title")

    if not title:
        return None

    album = None

    for section in track.get("sections") or []:
        for item in section.get("metadata") or []:
            metadata_title = str(
                item.get("title", "")
            ).lower()

            if metadata_title == "album":
                album = item.get("text")
                break

    images = track.get("images") or {}

    return {
        "title": title,
        "artist": (
            track.get("subtitle")
            or "Noma'lum ijrochi"
        ),
        "album": album,
        "cover": (
            images.get("coverarthq")
            or images.get("coverart")
        ),
        "shazam_link": track.get("url"),
    }


async def identify_music(
    audio_path: str,
    folder: str,
) -> dict[str, Any] | None:
    sample_windows = [
        (0, "0-20 soniya"),
        (5, "5-25 soniya"),
        (10, "10-30 soniya"),
    ]

    for index, (
        start_second,
        window_name,
    ) in enumerate(sample_windows, start=1):
        try:
            sample_path = await extract_audio_sample(
                audio_path,
                folder,
                start_second,
                index,
            )

            logger.info(
                "Shazam tekshiruvi | oraliq=%s",
                window_name,
            )

            raw_result = await shazam.recognize(
                sample_path
            )

            result = parse_shazam_result(
                raw_result
            )

            if result:
                logger.info(
                    "Musiqa aniqlandi | ijrochi=%s | "
                    "nomi=%s | oraliq=%s",
                    result["artist"],
                    result["title"],
                    window_name,
                )

                return result

        except Exception as error:
            logger.exception(
                "Shazam tekshiruvida xato | "
                "oraliq=%s | xato=%r",
                window_name,
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
            DEEZER_API_URL,
            params={
                "q": query,
                "limit": SEARCH_LIMIT,
            },
        ) as response:
            if response.status != 200:
                raise RuntimeError(
                    f"Deezer javob kodi: {response.status}"
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
                    or "Noma'lum qo'shiq"
                ),
                "artist": (
                    artist.get("name")
                    or "Noma'lum ijrochi"
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


async def request_lyrics(
    parameters: dict[str, str],
) -> list[dict[str, Any]]:
    timeout = aiohttp.ClientTimeout(total=20)

    async with aiohttp.ClientSession(
        timeout=timeout
    ) as session:
        async with session.get(
            LRCLIB_API_URL,
            params=parameters,
        ) as response:
            if response.status != 200:
                logger.warning(
                    "LRCLIB xato javobi | kod=%s | "
                    "so'rov=%s",
                    response.status,
                    parameters,
                )

                return []

            data = await response.json(
                content_type=None
            )

    if not isinstance(data, list):
        return []

    return data


def prepare_lyrics(
    results: list[dict[str, Any]],
) -> str | None:
    if not results:
        return None

    lyrics = (
        results[0].get("plainLyrics")
        or results[0].get("syncedLyrics")
    )

    if not lyrics:
        return None

    if len(lyrics) > MAX_LYRICS_LENGTH:
        lyrics = (
            lyrics[:MAX_LYRICS_LENGTH]
            + "\n\n..."
        )

    return lyrics


async def find_song_lyrics(
    title: str,
    artist: str,
) -> str | None:
    results = await request_lyrics(
        {
            "track_name": title,
            "artist_name": artist,
        }
    )

    return prepare_lyrics(results)


async def search_lyrics_by_query(
    query: str,
) -> tuple[str, str, str] | None:
    results = await request_lyrics(
        {"q": query}
    )

    lyrics = prepare_lyrics(results)

    if not lyrics or not results:
        return None

    item = results[0]

    return (
        item.get("trackName") or "Noma'lum qo'shiq",
        item.get("artistName") or "Noma'lum ijrochi",
        lyrics,
    )


async def download_preview(
    preview_url: str,
) -> tuple[str, str]:
    folder = tempfile.mkdtemp(
        prefix="ultramedia_preview_"
    )

    preview_path = os.path.join(
        folder,
        "preview.mp3",
    )

    try:
        timeout = aiohttp.ClientTimeout(total=60)

        async with aiohttp.ClientSession(
            timeout=timeout
        ) as session:
            async with session.get(
                preview_url
            ) as response:
                if response.status != 200:
                    raise RuntimeError(
                        "Deezer namuna audiosi olinmadi. "
                        f"Javob kodi: {response.status}"
                    )

                with open(
                    preview_path,
                    "wb",
                ) as output_file:
                    async for chunk in (
                        response.content.iter_chunked(
                            128 * 1024
                        )
                    ):
                        output_file.write(chunk)

        if not os.path.exists(preview_path):
            raise RuntimeError(
                "Namuna audio fayli topilmadi."
            )

        if os.path.getsize(preview_path) <= 0:
            raise RuntimeError(
                "Namuna audio fayli bo'sh."
            )

        return preview_path, folder

    except Exception:
        shutil.rmtree(
            folder,
            ignore_errors=True,
        )

        raise


async def send_identified_music(
    message: Message,
    song: dict[str, Any],
) -> None:
    text = (
        f"🎵 Nomi: {song['title']}\n"
        f"👤 Ijrochi: {song['artist']}\n"
        f"💿 Albom: "
        f"{song.get('album') or 'Topilmadi'}\n"
        f"🔗 Shazam: "
        f"{song.get('shazam_link') or 'Topilmadi'}"
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
                "Muqova yuborilmadi | xato=%r",
                error,
            )

    await message.answer(text)


@dp.message(CommandStart())
async def start_handler(
    message: Message,
) -> None:
    user_modes.pop(message.from_user.id, None)

    await message.answer(
        "🚀 UltraMediaBot\n\n"
        "Kerakli bo'limni tanlang:",
        reply_markup=main_menu(),
    )


@dp.callback_query(F.data.startswith("menu:"))
async def main_menu_handler(
    callback: CallbackQuery,
) -> None:
    action = (callback.data or "").split(
        ":",
        maxsplit=1,
    )[-1]

    messages = {
        "video": (
            "🎥 TikTok, Instagram yoki YouTube Shorts "
            "havolasini yuboring."
        ),
        "mp3": (
            "🎵 MP3 ajratish uchun media "
            "havolasini yuboring."
        ),
        "identify": (
            "🎼 Musiqani aniqlash uchun video "
            "havolasini yuboring."
        ),
        "search": (
            "🔍 Qo'shiq nomi yoki ijrochi "
            "nomini yozing."
        ),
        "lyrics": (
            "📝 Qo'shiq nomi va ijrochini yozing."
        ),
    }

    if action in {"video", "mp3", "identify"}:
        user_modes[callback.from_user.id] = action

    elif action == "search":
        user_modes[callback.from_user.id] = "search"

    elif action == "lyrics":
        user_modes[callback.from_user.id] = "lyrics"

    else:
        await callback.message.answer(
            "❌ Noto'g'ri tanlov."
        )
        await safe_callback_answer(callback)
        return

    await callback.message.answer(
        messages[action]
    )

    await safe_callback_answer(callback)


@dp.message()
async def message_handler(
    message: Message,
) -> None:
    text = (message.text or "").strip()
    user_id = message.from_user.id

    if not text:
        await message.answer(
            "❌ Xabar bo'sh. Havola yoki matn yuboring."
        )
        return

    if is_supported_link(text):
        user_links[user_id] = text

        logger.info(
            "Havola qabul qilindi | "
            "foydalanuvchi=%s | havola=%s",
            user_id,
            text,
        )

        await message.answer(
            "✅ Havola qabul qilindi. "
            "Kerakli amalni tanlang:",
            reply_markup=media_menu(),
        )

        return

    if text.startswith(
        ("http://", "https://")
    ):
        await message.answer(
            "❌ Havola noto'g'ri yoki "
            "qo'llab-quvvatlanmaydi."
        )
        return

    mode = user_modes.get(
        user_id,
        "search",
    )

    if mode == "lyrics":
        status = await message.answer(
            "📝 Qo'shiq matni qidirilmoqda..."
        )

        try:
            result = await search_lyrics_by_query(
                text
            )

            if not result:
                await safe_edit(
                    status,
                    "❌ Qo'shiq matni topilmadi.",
                )
                return

            title, artist, lyrics = result

            await safe_edit(
                status,
                f"📝 {artist} — {title}\n\n"
                f"{lyrics}",
            )

        except Exception as error:
            logger.exception(
                "Matn qidirishda xato | "
                "foydalanuvchi=%s | so'rov=%s | "
                "xato=%r",
                user_id,
                text,
                error,
            )

            await safe_edit(
                status,
                "❌ Qo'shiq matnini qidirishda "
                "server xatosi yuz berdi.",
            )

        return

    status = await message.answer(
        "🔍 Qo'shiq qidirilmoqda..."
    )

    try:
        songs = await search_deezer(text)

        if not songs:
            await safe_edit(
                status,
                "❌ Qo'shiq topilmadi.",
            )
            return

        token = create_search_session(
            user_id,
            songs,
        )

        await status.edit_text(
            search_results_text(songs),
            reply_markup=search_results_menu(
                token,
                len(songs),
            ),
        )

    except Exception as error:
        logger.exception(
            "Qo'shiq qidirishda xato | "
            "foydalanuvchi=%s | so'rov=%s | "
            "xato=%r",
            user_id,
            text,
            error,
        )

        await safe_edit(
            status,
            "❌ Qo'shiq qidirishda "
            "server xatosi yuz berdi.",
        )


async def process_media_action(
    callback: CallbackQuery,
    mode: str,
) -> None:
    url = user_links.get(
        callback.from_user.id
    )

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
            "video": (
                "⏳ Video yuklab olinmoqda..."
            ),
            "mp3": (
                "⏳ MP3 ajratilmoqda..."
            ),
            "identify": (
                "⏳ Musiqa aniqlanmoqda..."
            ),
        }

        status = await callback.message.answer(
            status_messages[mode]
        )

        media_path, folder = await asyncio.to_thread(
            download_media,
            url,
            mode,
        )

        if mode == "video":
            await callback.message.answer_video(
                video=FSInputFile(media_path),
                caption=(
                    "✅ Video tayyor.\n\n"
                    f"{BOT_USERNAME}"
                ),
            )

        elif mode == "mp3":
            await callback.message.answer_audio(
                audio=FSInputFile(
                    media_path,
                    filename="audio.mp3",
                ),
                caption=(
                    "✅ MP3 tayyor.\n\n"
                    f"{BOT_USERNAME}"
                ),
            )

        else:
            song = await identify_music(
                media_path,
                folder,
            )

            if not song:
                await safe_edit(
                    status,
                    "❌ Musiqa aniqlanmadi. "
                    "Audio shovqinli yoki "
                    "Shazam bazasida mavjud emas.",
                )
                return

            await status.delete()
            status = None

            await send_identified_music(
                callback.message,
                song,
            )

        if status:
            await status.delete()

    except Exception as error:
        logger.exception(
            "Media amalida xato | tur=%s | "
            "foydalanuvchi=%s | havola=%s | "
            "xato=%r",
            mode,
            callback.from_user.id,
            url,
            error,
        )

        text = user_error_message(
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
        await process_media_action(
            callback,
            "video",
        )

    finally:
        await safe_callback_answer(callback)


@dp.callback_query(F.data == "media:mp3")
async def mp3_callback(
    callback: CallbackQuery,
) -> None:
    try:
        await process_media_action(
            callback,
            "mp3",
        )

    finally:
        await safe_callback_answer(callback)


@dp.callback_query(F.data == "media:identify")
async def identify_callback(
    callback: CallbackQuery,
) -> None:
    try:
        await process_media_action(
            callback,
            "identify",
        )

    finally:
        await safe_callback_answer(callback)


@dp.callback_query(F.data.startswith("song:"))
async def song_result_callback(
    callback: CallbackQuery,
) -> None:
    folder: str | None = None

    try:
        parsed = parse_song_callback(
            callback.data
        )

        if not parsed:
            await callback.message.answer(
                "❌ Noto'g'ri tanlov."
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
                "❌ Qidiruv natijasi eskirgan. "
                "Qaytadan qidiring."
            )
            return

        preview_url = song.get("preview")

        if preview_url:
            preview_path, folder = (
                await download_preview(
                    preview_url
                )
            )

            caption = (
                "🎵 Namuna audio\n\n"
                f"🎵 Nomi: {song['title']}\n"
                f"👤 Ijrochi: {song['artist']}\n"
                f"💿 Albom: "
                f"{song.get('album') or 'Topilmadi'}\n"
                f"⏱ Davomiyligi: "
                f"{format_duration(song.get('duration'))}\n\n"
                f"{official_links(song)}\n\n"
                f"{BOT_USERNAME}"
            )

            await callback.message.answer_audio(
                audio=FSInputFile(
                    preview_path,
                    filename="preview.mp3",
                ),
                caption=caption,
            )

        else:
            await callback.message.answer(
                "❌ Ushbu qo'shiq uchun "
                "namuna audio topilmadi.\n\n"
                f"{official_links(song)}"
            )

        lyrics = await find_song_lyrics(
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
                "❌ Qo'shiq matni topilmadi."
            )

    except Exception as error:
        logger.exception(
            "Natijani yuborishda xato | "
            "foydalanuvchi=%s | ma'lumot=%s | "
            "xato=%r",
            callback.from_user.id,
            callback.data,
            error,
        )

        await callback.message.answer(
            "❌ Natijani yuborishda "
            "server xatosi yuz berdi."
        )

    finally:
        if folder:
            shutil.rmtree(
                folder,
                ignore_errors=True,
            )

        await safe_callback_answer(callback)


async def main() -> None:
    # Bir token bilan faqat bitta Render ishchisi ishlashi kerak.
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
            "Boshqa bot nusxasi ayni token "
            "bilan ishlab turibdi."
        )

        raise

    finally:
        await bot.session.close()


if __name__ == "__main__":
    asyncio.run(main())
