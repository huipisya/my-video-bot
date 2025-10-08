import os
import tempfile
import asyncio
import logging
import re
import hashlib
import json
import time
from pathlib import Path
from typing import Optional, Tuple, List
import aiohttp
from aiogram import Bot, Dispatcher, types, F
from aiogram.filters import Command
from aiogram.fsm.state import State, StatesGroup
from aiogram.fsm.context import FSMContext
from aiogram.types import ReplyKeyboardMarkup, KeyboardButton, FSInputFile
from aiogram.exceptions import TelegramBadRequest
from aiogram.webhook.aiohttp_server import SimpleRequestHandler, setup_application
from dotenv import load_dotenv
import yt_dlp
import instaloader
import pickle
import sys

sys.stdout.reconfigure(encoding='utf-8')

# === 🧰 ЛОГИРОВАНИЕ ===
logging.basicConfig(
    level=logging.INFO,
    format='%(asctime)s - %(levelname)s - %(message)s'
)
logger = logging.getLogger(__name__)

# === 🔐 ТОКЕН И WEBHOOK ===
load_dotenv()
BOT_TOKEN = os.getenv("BOT_TOKEN")
WEBHOOK_HOST = os.getenv("WEBHOOK_HOST")

if not BOT_TOKEN:
    raise ValueError("❌ BOT_TOKEN не найден в переменных окружения")

WEBHOOK_PATH = "/"
WEBHOOK_URL = f"{WEBHOOK_HOST}{WEBHOOK_PATH}" if WEBHOOK_HOST else None

bot = Bot(token=BOT_TOKEN)
dp = Dispatcher()

# === 🧠 ХРАНИЛИЩЕ НАСТРОЕК ===
user_settings = {}
RATE_LIMIT_DELAY = {}  # {user_id: last_request_time}

# === 🎨 СОСТОЯНИЯ FSM ===
class VideoStates(StatesGroup):
    choosing_quality = State()

# === 📺 КАЧЕСТВА ВИДЕО ===
QUALITY_FORMATS = {
    "best": 'bestvideo[ext=mp4]+bestaudio[ext=m4a]/best[ext=mp4]/best',
    "1080p": 'bestvideo[ext=mp4][height<=1080]+bestaudio[ext=m4a]/best[height<=1080][ext=mp4]/best',
    "720p": 'bestvideo[ext=mp4][height<=720]+bestaudio[ext=m4a]/best[height<=720][ext=mp4]/best',
    "480p": 'best[height<=480][ext=mp4]/best[ext=mp4]/best',
    "360p": 'best[height<=360][ext=mp4]/best[ext=mp4]/best'
}

# === 🧹 АВТООЧИСТКА ФАЙЛОВ ===
def cleanup_file(file_path: str):
    """Удаляет файл сразу после использования"""
    try:
        if file_path and os.path.exists(file_path):
            os.remove(file_path)
            logger.info(f"🗑️ Удалён файл: {Path(file_path).name}")
    except Exception as e:
        logger.warning(f"⚠️ Не удалось удалить {file_path}: {e}")

def cleanup_files(files: List[str]):
    """Удаляет список файлов"""
    for file_path in files:
        cleanup_file(file_path)

# === 🛠 ВСПОМОГАТЕЛЬНЫЕ ФУНКЦИИ ===
async def check_rate_limit(user_id: int):
    """Ограничение: 1 запрос в 3 секунды"""
    now = time.time()
    last_time = RATE_LIMIT_DELAY.get(user_id, 0)
    if now - last_time < 3:
        delay = 3 - (now - last_time)
        logger.info(f"⏱️ Rate limit для user {user_id}: ждём {delay:.1f}с")
        await asyncio.sleep(delay)
    RATE_LIMIT_DELAY[user_id] = time.time()

def get_quality_setting(user_id: int) -> str:
    return user_settings.get(user_id, "best")

def get_ydl_opts(quality: str = "best") -> dict:
    opts = {
        'format': QUALITY_FORMATS.get(quality, QUALITY_FORMATS["best"]),
        'merge_output_format': 'mp4',
        'noplaylist': True,
        'outtmpl': os.path.join(tempfile.gettempdir(), '%(id)s.%(ext)s'),
        'quiet': True,
        'no_warnings': True,
        'http_headers': {
            'User-Agent': 'Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36'
        },
    }
    proxy = os.getenv("PROXY_URL")
    if proxy:
        opts['proxy'] = proxy
    return opts

def is_valid_url(url: str) -> bool:
    regex = re.compile(
        r'^(https?://)?(www\.)?'
        r'(youtube\.com|youtu\.be|tiktok\.com|instagram\.com|vm\.tiktok\.com|vt\.tiktok\.com)',
        re.IGNORECASE
    )
    return bool(re.match(regex, url))

def detect_platform(url: str) -> str:
    url_lower = url.lower()
    if 'youtube.com' in url_lower or 'youtu.be' in url_lower:
        return 'youtube'
    elif 'tiktok.com' in url_lower:
        return 'tiktok'
    elif 'instagram.com' in url_lower:
        return 'instagram'
    return 'unknown'

async def download_file(url: str, save_path: str, timeout: int = 60) -> bool:
    try:
        async with aiohttp.ClientSession(timeout=aiohttp.ClientTimeout(total=timeout)) as session:
            async with session.get(url) as resp:
                if resp.status == 200:
                    with open(save_path, 'wb') as f:
                        async for chunk in resp.content.iter_chunked(8192):
                            f.write(chunk)
                    return True
    except Exception as e:
        logger.error(f"Ошибка скачивания файла {url}: {e}")
    return False

# === 📥 INSTAGRAM: УЛУЧШЕННЫЕ МЕТОДЫ ===

async def download_instagram_embedder(url: str, shortcode: str) -> Tuple[Optional[str], Optional[List[str]], Optional[str]]:
    """Метод через публичный эмбед Instagram"""
    try:
        logger.info("🔄 Instagram: попытка через Embed...")
        embed_url = f"https://www.instagram.com/p/{shortcode}/embed/captioned/"
        
        async with aiohttp.ClientSession(timeout=aiohttp.ClientTimeout(total=30)) as session:
            headers = {
                'User-Agent': 'Mozilla/5.0 (iPhone; CPU iPhone OS 16_0 like Mac OS X) AppleWebKit/605.1.15',
                'Accept': 'text/html,application/xhtml+xml',
                'Accept-Language': 'en-US,en;q=0.9',
                'Referer': 'https://www.instagram.com/',
            }
            
            async with session.get(embed_url, headers=headers) as resp:
                if resp.status == 200:
                    html = await resp.text()
                    
                    # Ищем video_url в HTML
                    video_match = re.search(r'"video_url":"([^"]+)"', html)
                    if video_match:
                        video_url = video_match.group(1).replace('\\u0026', '&')
                        temp_path = os.path.join(tempfile.gettempdir(), f"insta_embed_{shortcode}.mp4")
                        if await download_file(video_url, temp_path):
                            return (temp_path, None, None)
                    
                    # Ищем display_url для фото
                    image_match = re.search(r'"display_url":"([^"]+)"', html)
                    if image_match:
                        image_url = image_match.group(1).replace('\\u0026', '&')
                        photo_path = os.path.join(tempfile.gettempdir(), f"insta_embed_{shortcode}.jpg")
                        if await download_file(image_url, photo_path):
                            return (None, [photo_path], "📸 Instagram")
    except Exception as e:
        logger.error(f"❌ Instagram Embed: {e}")
    return None, None, None

async def download_instagram_oembed(url: str) -> Tuple[Optional[str], Optional[List[str]], Optional[str]]:
    """Метод через официальный oEmbed API"""
    try:
        logger.info("🔄 Instagram: попытка через oEmbed...")
        oembed_url = f"https://api.instagram.com/oembed/?url={url}"
        
        async with aiohttp.ClientSession(timeout=aiohttp.ClientTimeout(total=20)) as session:
            async with session.get(oembed_url) as resp:
                if resp.status == 200:
                    data = await resp.json()
                    thumbnail_url = data.get('thumbnail_url')
                    
                    if thumbnail_url:
                        # Получаем HTML страницы для видео
                        async with session.get(url, headers={
                            'User-Agent': 'Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36'
                        }) as page_resp:
                            if page_resp.status == 200:
                                html = await page_resp.text()
                                video_match = re.search(r'"video_url":"([^"]+)"', html)
                                if video_match:
                                    video_url = video_match.group(1).replace('\\/', '/')
                                    shortcode = re.search(r'/(?:p|reel)/([^/]+)', url).group(1)
                                    temp_path = os.path.join(tempfile.gettempdir(), f"insta_oembed_{shortcode}.mp4")
                                    if await download_file(video_url, temp_path):
                                        return (temp_path, None, None)
    except Exception as e:
        logger.error(f"❌ Instagram oEmbed: {e}")
    return None, None, None

async def download_instagram_ytdlp(url: str, quality: str) -> Tuple[Optional[str], Optional[List[str]], Optional[str]]:
    try:
        logger.info("🔄 Instagram: попытка через yt-dlp...")
        ydl_opts = {
            'format': 'best',
            'noplaylist': True,
            'outtmpl': os.path.join(tempfile.gettempdir(), '%(id)s.%(ext)s'),
            'quiet': True,
            'no_warnings': True,
            'http_headers': {
                'User-Agent': 'Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36'
            },
        }
        with yt_dlp.YoutubeDL(ydl_opts) as ydl:
            info = ydl.extract_info(url, download=True)
            temp_file = ydl.prepare_filename(info)
            if temp_file and os.path.exists(temp_file):
                return (temp_file, None, None)
    except Exception as e:
        logger.error(f"❌ Instagram yt-dlp: {e}")
    return None, None, None

async def download_instagram_instaloader(url: str, shortcode: str) -> Tuple[Optional[str], Optional[List[str]], Optional[str]]:
    try:
        logger.info("🔄 Instagram: попытка через Instaloader...")
        L = instaloader.Instaloader(
            download_videos=True,
            download_pictures=True,
            download_geotags=False,
            download_comments=False,
            save_metadata=False,
            quiet=True
        )
        post = instaloader.Post.from_shortcode(L.context, shortcode)

        if post.is_video:
            video_url = post.video_url
            temp_path = os.path.join(tempfile.gettempdir(), f"insta_{shortcode}.mp4")
            if await download_file(video_url, temp_path):
                return (temp_path, None, None)
        else:
            photos = []
            description = post.caption or "Без описания"
            if post.typename == "GraphSidecar":
                for i, node in enumerate(post.get_sidecar_nodes()):
                    if node.is_video or i >= 10:
                        continue
                    photo_path = os.path.join(tempfile.gettempdir(), f"insta_{shortcode}_{i}.jpg")
                    if await download_file(node.display_url, photo_path):
                        photos.append(photo_path)
            else:
                photo_path = os.path.join(tempfile.gettempdir(), f"insta_{shortcode}.jpg")
                if await download_file(post.url, photo_path):
                    photos.append(photo_path)
            if photos:
                return (None, photos, description)
    except Exception as e:
        logger.error(f"❌ Instagram Instaloader: {e}")
    return None, None, None

async def download_instagram_api(url: str, shortcode: str) -> Tuple[Optional[str], Optional[List[str]], Optional[str]]:
    try:
        logger.info("🔄 Instagram: попытка через API...")
        api_url = f"https://www.instagram.com/p/{shortcode}/?__a=1&__d=dis"
        async with aiohttp.ClientSession() as session:
            headers = {'User-Agent': 'Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36'}
            async with session.get(api_url, headers=headers) as resp:
                if resp.status == 200:
                    data = await resp.json()
                    media = data.get('graphql', {}).get('shortcode_media', {})
                    if media.get('is_video'):
                        video_url = media.get('video_url')
                        if video_url:
                            temp_path = os.path.join(tempfile.gettempdir(), f"insta_api_{shortcode}.mp4")
                            if await download_file(video_url, temp_path):
                                return (temp_path, None, None)
                    else:
                        photos = []
                        edges = media.get('edge_sidecar_to_children', {}).get('edges', [])
                        if edges:
                            for i, edge in enumerate(edges[:10]):
                                node = edge.get('node', {})
                                img_url = node.get('display_url')
                                if img_url:
                                    photo_path = os.path.join(tempfile.gettempdir(), f"insta_api_{shortcode}_{i}.jpg")
                                    if await download_file(img_url, photo_path):
                                        photos.append(photo_path)
                        else:
                            img_url = media.get('display_url')
                            if img_url:
                                photo_path = os.path.join(tempfile.gettempdir(), f"insta_api_{shortcode}.jpg")
                                if await download_file(img_url, photo_path):
                                    photos.append(photo_path)
                        if photos:
                            description = media.get('edge_media_to_caption', {}).get('edges', [{}])[0].get('node', {}).get('text', 'Без описания')
                            return (None, photos, description)
    except Exception as e:
        logger.error(f"❌ Instagram API: {e}")
    return None, None, None

# --- 🆕 НОВЫЙ МЕТОД ДЛЯ REELS ---
async def download_instagram_reels_ytdlp(url: str, quality: str) -> Tuple[Optional[str], Optional[List[str]], Optional[str]]:
    """Надежный метод для скачивания Reels через yt-dlp с эмуляцией мобильного устройства."""
    try:
        logger.info("🔄 Instagram Reels: попытка через yt-dlp (мобильный режим)...")

        # Попробуем получить shortcode из /share/ URL
        share_match = re.search(r'/share/([^/]+)', url)
        if share_match:
            shortcode = share_match.group(1)
            # Составляем правильный URL для рилса
            reel_url = f"https://www.instagram.com/reel/{shortcode}/"
            logger.info(f"🔄 Переведен URL /share/ в /reel/: {reel_url}")
        else:
            # Если это не /share/, используем исходный URL
            reel_url = url

        ydl_opts = {
            'format': 'best',
            'noplaylist': True,
            'outtmpl': os.path.join(tempfile.gettempdir(), '%(id)s.%(ext)s'),
            'quiet': True,
            'no_warnings': True,
            'http_headers': {
                'User-Agent': 'Mozilla/5.0 (iPhone; CPU iPhone OS 16_0 like Mac OS X) AppleWebKit/605.1.15 (KHTML, like Gecko) Version/16.0 Mobile/15E148 Safari/604.1',
                'Referer': 'https://www.instagram.com/',
                'Origin': 'https://www.instagram.com',
                'Accept-Language': 'en-US,en;q=0.9',
            },
            # Добавим опцию для работы с Reels
            'extractor_args': {
                'instagram': {
                    'skip_download': False
                }
            }
        }

        # Если есть cookies.txt, используем их
        cookies_file = Path("cookies.txt")
        if cookies_file.exists():
            ydl_opts['cookiefile'] = str(cookies_file)
            logger.info("✅ Используются куки из cookies.txt")

        with yt_dlp.YoutubeDL(ydl_opts) as ydl:
            info = ydl.extract_info(reel_url, download=True)
            temp_file = ydl.prepare_filename(info)
            if temp_file and os.path.exists(temp_file):
                return (temp_file, None, None)

    except Exception as e:
        logger.error(f"❌ Instagram Reels yt-dlp: {e}")
    return None, None, None

# --- 🆕 ИСПРАВЛЕННАЯ ФУНКЦИЯ download_instagram ---
async def download_instagram(url: str, quality: str = "best") -> Tuple[Optional[str], Optional[List[str]], Optional[str]]:
    # Исправленное извлечение shortcode (учитывает /share/)
    shortcode_match = re.search(r'/(?:p|reel|share)/([^/]+)', url)
    if not shortcode_match:
        return None, None, "❌ Не удалось извлечь shortcode из URL"
    shortcode = shortcode_match.group(1)

    # Определяем тип контента по URL
    is_reel = '/reel/' in url.lower() or '/share/' in url.lower()

    methods = []

    # Для Reels: сначала пробуем специальный метод
    if is_reel:
        methods.append(lambda: download_instagram_reels_ytdlp(url, quality))

    # Затем остальные методы
    methods.extend([
        lambda: download_instagram_embedder(url, shortcode),
        lambda: download_instagram_oembed(url),
        lambda: download_instagram_ytdlp(url, quality),
        lambda: download_instagram_instaloader(url, shortcode),
        lambda: download_instagram_api(url, shortcode)
    ])

    for method in methods:
        result = await method()
        if result and (result[0] or result[1]):
            return result

    return None, None, "❌ Не удалось скачать контент из Instagram всеми методами"

# === 📤 TIKTOK ФОТО ===
async def download_tiktok_photos(url: str) -> Tuple[Optional[List[str]], str]:
    try:
        clean_url = url.split('?')[0]
        headers = {
            'User-Agent': 'Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36',
            'Accept': 'text/html,application/xhtml+xml,application/xml;q=0.9,image/webp,*/*;q=0.8',
            'Accept-Language': 'en-US,en;q=0.5',
            'Referer': 'https://www.tiktok.com/',
            'DNT': '1',
        }

        async with aiohttp.ClientSession(timeout=aiohttp.ClientTimeout(total=30)) as session:
            async with session.get(clean_url, headers=headers, allow_redirects=True) as resp:
                if resp.status != 200:
                    return None, f"❌ TikTok вернул статус {resp.status}"
                html = await resp.text()

        json_match = re.search(r'<script id="__UNIVERSAL_DATA_FOR_REHYDRATION__" type="application/json">({.*})</script>', html)
        if not json_match:
            return None, "❌ Не найден JSON-блок с данными"

        try:
            data = json.loads(json_match.group(1))
            item_info = data.get('__DEFAULT_SCOPE__', {}).get('webapp.photo.detail', {}).get('itemInfo', {})
            image_post = item_info.get('imagePost', {})
            images = image_post.get('images', [])
        except Exception as e:
            logger.error(f"Ошибка парсинга JSON: {e}")
            return None, "❌ Не удалось распарсить данные фото"

        if not images:
            return None, "❌ Фото не найдены в данных"

        photos = []
        for i, img in enumerate(images[:10]):
            img_url = img.get('imageURL', {}).get('urlList', [])
            if not img_url:
                continue
            url_to_download = img_url[0]
            photo_path = os.path.join(tempfile.gettempdir(), f"tiktok_photo_{i}.jpg")
            if await download_file(url_to_download, photo_path, timeout=15):
                photos.append(photo_path)

        if photos:
            return (photos, "📸 Фото из TikTok")
        else:
            return None, "❌ Не удалось скачать ни одного фото"

    except asyncio.TimeoutError:
        return None, "❌ Таймаут при загрузке страницы TikTok"
    except Exception as e:
        logger.error(f"❌ TikTok фото ошибка: {e}")
        return None, f"❌ Ошибка: {str(e)[:100]}"

# === 📤 СКАЧИВАНИЕ ВИДЕО ===
async def download_video_ytdlp(url: str, quality: str) -> Optional[str]:
    try:
        logger.info("🔄 Видео: попытка через yt-dlp...")
        ydl_opts = get_ydl_opts(quality)
        with yt_dlp.YoutubeDL(ydl_opts) as ydl:
            info = ydl.extract_info(url, download=True)
            temp_file = ydl.prepare_filename(info)
            if temp_file and os.path.exists(temp_file):
                return temp_file
    except Exception as e:
        logger.error(f"❌ yt-dlp стандартный: {e}")
    return None

async def download_video_ytdlp_cookies(url: str, quality: str) -> Optional[str]:
    try:
        logger.info("🔄 Видео: попытка через yt-dlp с cookies...")
        cookies_file = Path("cookies.txt")
        if not cookies_file.exists():
            return None
        ydl_opts = get_ydl_opts(quality)
        ydl_opts['cookiefile'] = str(cookies_file)
        with yt_dlp.YoutubeDL(ydl_opts) as ydl:
            info = ydl.extract_info(url, download=True)
            temp_file = ydl.prepare_filename(info)
            if temp_file and os.path.exists(temp_file):
                return temp_file
    except Exception as e:
        logger.error(f"❌ yt-dlp с cookies: {e}")
    return None

async def download_video_ytdlp_alt(url: str) -> Optional[str]:
    try:
        logger.info("🔄 Видео: попытка через yt-dlp (альтернативный формат)...")
        ydl_opts = {
            'format': 'best',
            'outtmpl': os.path.join(tempfile.gettempdir(), '%(id)s.%(ext)s'),
            'quiet': True,
            'no_warnings': True
        }
        with yt_dlp.YoutubeDL(ydl_opts) as ydl:
            info = ydl.extract_info(url, download=True)
            temp_file = ydl.prepare_filename(info)
            if temp_file and os.path.exists(temp_file):
                return temp_file
    except Exception as e:
        logger.error(f"❌ yt-dlp альтернативный: {e}")
    return None

async def download_video(url: str, quality: str = "best") -> Optional[str]:
    methods = [
        lambda: download_video_ytdlp(url, quality),
        lambda: download_video_ytdlp_cookies(url, quality),
        lambda: download_video_ytdlp_alt(url)
    ]
    for method in methods:
        result = await method()
        if result:
            return result
    return None

# === 📤 ОТПРАВКА ===
async def send_photos_with_caption(chat_id: int, photos: List[str], caption: str) -> bool:
    if not photos:
        return False
    try:
        if len(photos) == 1:
            await bot.send_photo(chat_id=chat_id, photo=FSInputFile(photos[0]), caption=caption)
        else:
            media_group = [
                types.InputMediaPhoto(media=FSInputFile(photo), caption=caption if i == 0 else None)
                for i, photo in enumerate(photos[:10])
            ]
            await bot.send_media_group(chat_id=chat_id, media=media_group)
        logger.info(f"✅ Отправлено {len(photos)} фото в Telegram")
        return True
    except Exception as e:
        logger.error(f"Ошибка отправки фото: {e}")
        return False

# === 📤 ЗАГРУЗКА НА ФАЙЛООБМЕННИКИ ===
async def upload_to_filebin(file_path: str) -> Optional[str]:
    try:
        logger.info("🔄 Загрузка на filebin.net...")
        async with aiohttp.ClientSession(timeout=aiohttp.ClientTimeout(total=120)) as session:
            with open(file_path, 'rb') as f:
                data = aiohttp.FormData()
                data.add_field('file', f, filename=Path(file_path).name)
                async with session.post('https://filebin.net/', data=data, params={'expiry': '3d'}) as resp:
                    if resp.status == 200:
                        text = await resp.text()
                        lines = text.strip().split('\n')
                        if lines and lines[0].strip().startswith('http') and 'filebin.net' in lines[0]:
                            logger.info("✅ Загружено на filebin.net")
                            return lines[0].strip()
    except Exception as e:
        logger.error(f"❌ filebin.net: {e}")
    return None

async def upload_to_gofile(file_path: str) -> Optional[str]:
    try:
        logger.info("🔄 Загрузка на gofile.io...")
        async with aiohttp.ClientSession(timeout=aiohttp.ClientTimeout(total=300)) as session:
            async with session.get('https://api.gofile.io/servers') as resp:
                if resp.status != 200:
                    return None
                server_data = await resp.json()
                if not server_data.get('data', {}).get('servers'):
                    return None
                server = server_data['data']['servers'][0]['name']
            with open(file_path, 'rb') as f:
                data = aiohttp.FormData()
                data.add_field('file', f, filename=Path(file_path).name)
                upload_url = f'https://{server}.gofile.io/contents/uploadfile'
                async with session.post(upload_url, data=data) as upload_resp:
                    if upload_resp.status == 200:
                        result = await upload_resp.json()
                        if result.get('status') == 'ok':
                            return result['data']['downloadPage']
    except Exception as e:
        logger.error(f"❌ gofile.io: {e}")
    return None

async def send_video_or_link(chat_id: int, file_path: str, caption: str = "") -> bool:
    file_size = Path(file_path).stat().st_size
    size_mb = file_size / (1024 * 1024)

    if size_mb <= 50:
        try:
            await bot.send_video(chat_id=chat_id, video=FSInputFile(file_path), caption=caption)
            logger.info(f"✅ Видео ({size_mb:.1f} МБ) отправлено в Telegram")
            return True
        except TelegramBadRequest as e:
            logger.error(f"Ошибка отправки видео: {e}")

    uploaders = [
        ('gofile.io', upload_to_gofile),
        ('filebin.net', upload_to_filebin),
    ]

    for name, uploader in uploaders:
        link = await uploader(file_path)
        if link:
            await bot.send_message(
                chat_id=chat_id,
                text=f"📦 Файл ({size_mb:.1f} МБ) загружен на {name}\n\n📥 Скачать: {link}\n\n⏱️ Ссылка действительна несколько дней"
            )
            return True

    await bot.send_message(
        chat_id=chat_id,
        text=f"❌ Файл слишком большой ({size_mb:.1f} МБ).\nВсе сервисы загрузки недоступны."
    )
    return False

# === 🧭 КЛАВИАТУРЫ ===
def main_keyboard() -> ReplyKeyboardMarkup:
    return ReplyKeyboardMarkup(
        keyboard=[[KeyboardButton(text="⚙️ Настройки")]],
        resize_keyboard=True
    )

def settings_keyboard() -> ReplyKeyboardMarkup:
    return ReplyKeyboardMarkup(
        keyboard=[
            [KeyboardButton(text="🌟 Лучшее")],
            [KeyboardButton(text="🎬 1080p"), KeyboardButton(text="📺 720p")],
            [KeyboardButton(text="⚡ 480p"), KeyboardButton(text="📱 360p")],
            [KeyboardButton(text="◀️ Назад")]
        ],
        resize_keyboard=True
    )

# === 🚀 КОМАНДЫ ===
@dp.message(Command("start"))
async def cmd_start(message: types.Message, state: FSMContext):
    await state.clear()
    welcome_text = (
        "🎬 <b>Добро пожаловать в VideoBot!</b>\n\n"
        "Я могу скачать видео с:\n"
        "• YouTube\n"
        "• TikTok\n"
        "• Instagram\n\n"
        "📲 Просто отправь мне ссылку!"
    )
    await message.answer(welcome_text, reply_markup=main_keyboard(), parse_mode="HTML")

@dp.message(F.text == "⚙️ Настройки")
async def settings_menu(message: types.Message, state: FSMContext):
    await state.set_state(VideoStates.choosing_quality)
    current = get_quality_setting(message.from_user.id)
    await message.answer(
        f"⚙️ Текущее качество: <b>{current.upper()}</b>\n\nВыберите новое:",
        reply_markup=settings_keyboard(),
        parse_mode="HTML"
    )

@dp.message(VideoStates.choosing_quality, F.text.in_([
    "🌟 Лучшее", "🎬 1080p", "📺 720p", "⚡ 480p", "📱 360p"
]))
async def set_quality(message: types.Message, state: FSMContext):
    quality_map = {
        "🌟 Лучшее": "best",
        "🎬 1080p": "1080p",
        "📺 720p": "720p",
        "⚡ 480p": "480p",
        "📱 360p": "360p"
    }
    user_settings[message.from_user.id] = quality_map[message.text]
    await message.answer(
        f"✅ Установлено: <b>{message.text}</b>",
        reply_markup=main_keyboard(),
        parse_mode="HTML"
    )
    await state.clear()

@dp.message(VideoStates.choosing_quality, F.text == "◀️ Назад")
async def back_to_main(message: types.Message, state: FSMContext):
    await state.clear()
    await message.answer("🏠 Главное меню", reply_markup=main_keyboard())

# === 📥 ОБРАБОТКА ССЫЛОК ===
@dp.message(F.text)
async def handle_link(message: types.Message):
    url = message.text.strip()
    if not is_valid_url(url):
        await message.answer("⚠️ Отправьте корректную ссылку на YouTube, TikTok или Instagram")
        return

    # ✅ RATE LIMITING
    await check_rate_limit(message.from_user.id)

    platform = detect_platform(url)
    status_msg = await message.answer(f"⏳ Обрабатываю {platform.upper()}...")
    user_quality = get_quality_setting(message.from_user.id)
    temp_file = None
    temp_photos = []

    try:
        if platform == 'instagram':
            temp_file, photos, description = await download_instagram(url, user_quality)
            if description and "❌" in description:
                await status_msg.edit_text(description)
                return
            if photos:
                temp_photos = photos
                await status_msg.delete()
                success = await send_photos_with_caption(message.chat.id, photos, description)
                # 🧹 АВТООЧИСТКА после отправки
                cleanup_files(photos)
                return

        elif platform == 'tiktok':
            if '/photo/' in url.lower():
                photos, description = await download_tiktok_photos(url)
                await status_msg.delete()
                if photos:
                    temp_photos = photos
                    success = await send_photos_with_caption(message.chat.id, photos, description)
                    # 🧹 АВТООЧИСТКА после отправки
                    cleanup_files(photos)
                else:
                    await message.answer(description)
                return

        temp_file = await download_video(url, user_quality)
        if not temp_file or not os.path.exists(temp_file):
            await status_msg.edit_text("❌ Не удалось скачать видео всеми доступными методами")
            return

        await status_msg.edit_text("📤 Отправляю...")
        await send_video_or_link(message.chat.id, temp_file, caption="🎥 Готово!")
        await status_msg.delete()
        
        # 🧹 АВТООЧИСТКА после отправки
        cleanup_file(temp_file)

    except Exception as e:
        error_msg = f"❌ Ошибка: {str(e)}"
        logger.error(error_msg)
        try:
            await status_msg.edit_text(error_msg)
        except:
            pass
    
    finally:
        # 🧹 ФИНАЛЬНАЯ ОЧИСТКА (на случай ошибок)
        if temp_file:
            cleanup_file(temp_file)
        if temp_photos:
            cleanup_files(temp_photos)

# === 🚀 ЗАПУСК: ГИБКИЙ РЕЖИМ ===
async def main():
    logger.info("🚀 Запуск бота...")
    
    if WEBHOOK_HOST:
        # === Режим Webhook (для Railway) ===
        from aiogram.webhook.aiohttp_server import SimpleRequestHandler
        import aiohttp.web

        WEBHOOK_PATH = "/"
        WEBHOOK_URL = f"{WEBHOOK_HOST}{WEBHOOK_PATH}"

        await bot.set_webhook(WEBHOOK_URL)
        logger.info(f"✅ Webhook установлен на {WEBHOOK_URL}")

        app = aiohttp.web.Application()
        webhook_requests_handler = SimpleRequestHandler(dispatcher=dp, bot=bot)
        webhook_requests_handler.register(app, path=WEBHOOK_PATH)

        port = int(os.getenv("PORT", 8000))
        runner = aiohttp.web.AppRunner(app)
        await runner.setup()
        site = aiohttp.web.TCPSite(runner, host="0.0.0.0", port=port)
        await site.start()
        logger.info(f"📡 Webhook-сервер запущен на порту {port}")

        try:
            while True:
                await asyncio.sleep(3600)
        except (KeyboardInterrupt, SystemExit):
            logger.info("🛑 Бот остановлен")
            await bot.delete_webhook(drop_pending_updates=True)
            await runner.cleanup()
            await bot.session.close()
    else:
        # === Режим Long Polling (для локального запуска) ===
        logger.info("🔄 Запуск в режиме long polling (локально)")
        await dp.start_polling(bot, skip_updates=True)

if __name__ == "__main__":
    asyncio.run(main())