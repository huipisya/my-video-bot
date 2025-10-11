import os
import tempfile
import asyncio
import logging
import uuid
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
import sys
sys.stdout.reconfigure(encoding='utf-8')
IG_DEVICE_ID = str(uuid.uuid4())
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
    """Получает установленное качество для пользователя."""
    return user_settings.get(user_id, "720p")

def set_quality_setting(user_id: int, quality: str):
    """Сохраняет качество для пользователя."""
    user_settings[user_id] = quality
    logger.info(f"💾 Качество '{quality}' сохранено для user {user_id}")

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

# === 📥 ОПТИМИЗИРОВАННОЕ СКАЧИВАНИЕ С INSTAGRAM ===

async def extract_instagram_shortcode(url: str) -> Optional[str]:
    """
    Извлекает shortcode из URL Instagram
    Поддерживает все форматы: /p/, /reel/, /reels/, /tv/, /share/
    """
    # Сначала пробуем стандартные форматы
    match = re.search(r'/(?:p|reel|reels|tv)/([A-Za-z0-9_-]+)', url)
    if match:
        shortcode = match.group(1)
        logger.info(f"📌 Извлечён shortcode: {shortcode}")
        return shortcode
    
    # Для /share/ нужно резолвить через yt-dlp
    if '/share/' in url:
        logger.info("🔄 Обнаружен /share/ формат, резолвим через yt-dlp...")
        try:
            ydl_opts = {
                'quiet': True,
                'no_warnings': True,
                'extract_flat': True,
                'socket_timeout': 10,
            }
            with yt_dlp.YoutubeDL(ydl_opts) as ydl:
                info = ydl.extract_info(url, download=False)
                webpage_url = info.get('webpage_url', '')
                if webpage_url:
                    match = re.search(r'/(?:p|reel|reels|tv)/([A-Za-z0-9_-]+)', webpage_url)
                    if match:
                        shortcode = match.group(1)
                        logger.info(f"📌 Извлечён shortcode из /share/: {shortcode}")
                        return shortcode
        except Exception as e:
            logger.warning(f"⚠️ Не удалось резолвить /share/ через yt-dlp: {e}")
    
    logger.warning("⚠️ Не удалось извлечь shortcode из URL")
    return None


async def download_instagram_ytdlp(url: str, quality: str, cookies_file: Optional[Path] = None) -> Optional[str]:
    """
    Основной метод через yt-dlp с поддержкой cookies
    Работает в 90% случаев, включая 18+ контент при наличии cookies
    """
    try:
        logger.info("🔄 Instagram: yt-dlp...")
        
        ydl_opts = {
            'format': 'best',
            'merge_output_format': 'mp4',
            'noplaylist': True,
            'outtmpl': os.path.join(tempfile.gettempdir(), '%(id)s.%(ext)s'),
            'quiet': False,
            'no_warnings': False,
            'socket_timeout': 30,
            'retries': 3,
            'fragment_retries': 3,
            'http_headers': {
                'User-Agent': 'Mozilla/5.0 (iPhone; CPU iPhone OS 16_6 like Mac OS X) AppleWebKit/605.1.15',
                'Accept': '*/*',
                'Accept-Language': 'en-US,en;q=0.9',
                'Accept-Encoding': 'gzip, deflate, br',
                'Origin': 'https://www.instagram.com',
                'Referer': 'https://www.instagram.com/',
            },
            'geo_bypass': True,
            'age_limit': None,
        }
        
        # Добавляем cookies если есть
        if cookies_file and cookies_file.exists():
            ydl_opts['cookiefile'] = str(cookies_file)
            logger.info(f"✅ Используются cookies: {cookies_file.name}")
        
        # Добавляем прокси если есть
        proxy = os.getenv("PROXY_URL")
        if proxy:
            ydl_opts['proxy'] = proxy
            logger.debug(f"✅ Используется прокси")
        
        with yt_dlp.YoutubeDL(ydl_opts) as ydl:
            info = ydl.extract_info(url, download=True)
            temp_file = ydl.prepare_filename(info)
            
            if temp_file and os.path.exists(temp_file):
                file_size = os.path.getsize(temp_file) / (1024 * 1024)
                logger.info(f"✅ Видео скачано через yt-dlp ({file_size:.1f} МБ)")
                return temp_file
                
    except yt_dlp.utils.DownloadError as e:
        error_str = str(e).lower()
        
        # Обработка специфичных ошибок
        if 'inappropriate' in error_str or 'unavailable for certain audiences' in error_str:
            logger.warning("⚠️ yt-dlp: контент 18+ (inappropriate content)")
            return None
        elif 'login required' in error_str or 'rate-limit' in error_str:
            logger.warning("⚠️ yt-dlp: требуется авторизация или rate-limit")
            return None
        
        logger.error(f"❌ yt-dlp error: {e}")
    except Exception as e:
        logger.error(f"❌ yt-dlp exception: {e}")
    
    return None


async def download_instagram_mobile_api(shortcode: str, cookies_dict: Optional[dict] = None) -> Tuple[Optional[str], Optional[List[str]], Optional[str]]:
    """
    Запасной метод через мобильное API Instagram
    Работает для публичного контента и с cookies для 18+
    """
    try:
        logger.info("🔄 Instagram: Mobile API...")
        
        api_url = f"https://www.instagram.com/api/v1/media/{shortcode}/info/"
        
        headers = {
            'User-Agent': 'Instagram 269.0.0.18.75 Android (30/11; 420dpi; 1080x2265; OnePlus; ONEPLUS A6000; OnePlus6; qcom; en_US; 314665256)',
            'Accept': '*/*',
            'Accept-Language': 'en-US',
            'X-IG-App-ID': '567067343352427',
            'X-IG-Device-ID': str(uuid.uuid4()),
            'X-IG-Android-ID': str(uuid.uuid4()),
            'Origin': 'https://www.instagram.com',
            'Referer': f'https://www.instagram.com/p/{shortcode}/',
        }
        
        async with aiohttp.ClientSession(timeout=aiohttp.ClientTimeout(total=20)) as session:
            async with session.get(api_url, headers=headers, cookies=cookies_dict or {}) as resp:
                if resp.status == 404:
                    logger.warning("Mobile API: контент не найден (404)")
                    return None, None, None
                    
                if resp.status == 403:
                    logger.warning("Mobile API: доступ запрещен (403) - возможно 18+ или приватный")
                    return None, None, None
                    
                if resp.status != 200:
                    logger.warning(f"Mobile API: статус {resp.status}")
                    return None, None, None
                
                try:
                    data = await resp.json()
                except:
                    return None, None, None
                
                items = data.get('items', [])
                if not items:
                    return None, None, None
                
                media = items[0]
                media_type = media.get('media_type', 0)
                
                # Видео (type 2)
                if media_type == 2:
                    video_versions = media.get('video_versions', [])
                    if video_versions:
                        video_url = video_versions[0].get('url')
                        if video_url:
                            video_path = os.path.join(tempfile.gettempdir(), f"ig_{shortcode}.mp4")
                            if await download_file(video_url, video_path, timeout=30):
                                logger.info("✅ Видео скачано (Mobile API)")
                                return (video_path, None, None)
                
                # Карусель (type 8)
                elif media_type == 8:
                    carousel_media = media.get('carousel_media', [])
                    photos = []
                    videos = []
                    
                    for idx, item in enumerate(carousel_media[:10]):
                        item_type = item.get('media_type', 0)
                        
                        if item_type == 2:  # Видео в карусели
                            video_versions = item.get('video_versions', [])
                            if video_versions:
                                video_url = video_versions[0].get('url')
                                if video_url:
                                    videos.append(video_url)
                                    
                        elif item_type == 1:  # Фото в карусели
                            img_candidates = item.get('image_versions2', {}).get('candidates', [])
                            if img_candidates:
                                img_url = img_candidates[0].get('url')
                                if img_url:
                                    photo_path = os.path.join(tempfile.gettempdir(), f"ig_{shortcode}_{idx}.jpg")
                                    if await download_file(img_url, photo_path, timeout=15):
                                        photos.append(photo_path)
                    
                    # Приоритет видео
                    if videos:
                        video_path = os.path.join(tempfile.gettempdir(), f"ig_{shortcode}.mp4")
                        if await download_file(videos[0], video_path, timeout=30):
                            logger.info("✅ Видео из карусели (Mobile API)")
                            return (video_path, None, None)
                    
                    if photos:
                        caption = media.get('caption', {})
                        description = caption.get('text', "📸 Instagram") if caption else "📸 Instagram"
                        logger.info(f"✅ {len(photos)} фото из карусели (Mobile API)")
                        return (None, photos, description)
                
                # Фото (type 1)
                elif media_type == 1:
                    img_candidates = media.get('image_versions2', {}).get('candidates', [])
                    if img_candidates:
                        img_url = img_candidates[0].get('url')
                        if img_url:
                            photo_path = os.path.join(tempfile.gettempdir(), f"ig_{shortcode}.jpg")
                            if await download_file(img_url, photo_path, timeout=15):
                                caption = media.get('caption', {})
                                description = caption.get('text', "📸 Instagram") if caption else "📸 Instagram"
                                logger.info("✅ Фото скачано (Mobile API)")
                                return (None, [photo_path], description)
                                
    except asyncio.TimeoutError:
        logger.warning("⏱️ Mobile API timeout")
    except Exception as e:
        logger.error(f"❌ Mobile API error: {e}")
    
    return None, None, None


def load_cookies_from_file(cookies_file: Path) -> dict:
    """Загружает cookies из файла в формате Netscape"""
    cookies = {}
    try:
        with open(cookies_file, 'r') as f:
            for line in f:
                if line.startswith('#') or not line.strip():
                    continue
                try:
                    parts = line.strip().split('\t')
                    if len(parts) >= 7:
                        cookies[parts[5]] = parts[6]
                except:
                    continue
        logger.debug(f"✅ Загружено {len(cookies)} cookies из {cookies_file.name}")
    except Exception as e:
        logger.error(f"❌ Ошибка чтения cookies: {e}")
    return cookies


async def download_instagram(url: str, quality: str = "best", user_id: Optional[int] = None) -> Tuple[Optional[str], Optional[List[str]], Optional[str]]:
    """
    Главная функция скачивания Instagram контента
    
    Стратегия:
    1. yt-dlp без cookies (работает для публичного контента)
    2. yt-dlp с глобальными cookies (для ограниченного контента)
    3. yt-dlp с пользовательскими cookies (для 18+ контента)
    4. Mobile API как запасной вариант
    
    Args:
        url: Ссылка на Instagram (включая /share/)
        quality: Качество видео (не используется, т.к. yt-dlp берет best)
        user_id: ID пользователя для персональных cookies
    
    Returns:
        (video_path, photos_list, description) или (None, None, error_message)
    """
    
    # Извлекаем shortcode (поддерживает /share/)
    shortcode = await extract_instagram_shortcode(url)
    if not shortcode:
        return None, None, "❌ Некорректная ссылка на Instagram. Отправьте ссылку вида:\n• instagram.com/p/...\n• instagram.com/reel/...\n• instagram.com/share/..."
    
    logger.info(f"📌 Instagram shortcode: {shortcode}")
    
    # Проверяем наличие cookies ПЕРЕД началом загрузки
    global_cookies = Path("cookies.txt")
    user_cookies = Path(f"session/{user_id}_instagram_cookies.txt") if user_id else None
    
    has_global_cookies = global_cookies.exists()
    has_user_cookies = user_cookies and user_cookies.exists()
    
    # === МЕТОД 1: yt-dlp БЕЗ cookies (публичный контент) ===
    logger.info("🔄 Попытка 1/4: yt-dlp без cookies")
    result = await download_instagram_ytdlp(url, quality)
    if result and os.path.exists(result):
        logger.info("✅ Успешно скачано (публичный доступ)")
        return (result, None, None)
    
    # === МЕТОД 2: yt-dlp С ГЛОБАЛЬНЫМИ cookies ===
    if has_global_cookies:
        logger.info("🔄 Попытка 2/4: yt-dlp с глобальными cookies")
        result = await download_instagram_ytdlp(url, quality, global_cookies)
        if result and os.path.exists(result):
            logger.info("✅ Успешно скачано (глобальные cookies)")
            return (result, None, None)
    else:
        logger.info("⏭️ Пропуск попытки 2/4: глобальные cookies отсутствуют")
    
    # === МЕТОД 3: yt-dlp С ПОЛЬЗОВАТЕЛЬСКИМИ cookies (для 18+) ===
    if has_user_cookies:
        logger.info(f"🔄 Попытка 3/4: yt-dlp с cookies пользователя {user_id}")
        result = await download_instagram_ytdlp(url, quality, user_cookies)
        if result and os.path.exists(result):
            logger.info(f"✅ Успешно скачано (cookies пользователя {user_id})")
            return (result, None, None)
    else:
        logger.info(f"⏭️ Пропуск попытки 3/4: cookies пользователя отсутствуют")
    
    # === МЕТОД 4: Mobile API как запасной вариант ===
    logger.info("🔄 Попытка 4/4: Mobile API")
    
    # Пробуем с пользовательскими cookies если есть
    cookies_dict = None
    if has_user_cookies:
        cookies_dict = load_cookies_from_file(user_cookies)
    
    # Если нет пользовательских, пробуем глобальные
    if not cookies_dict and has_global_cookies:
        cookies_dict = load_cookies_from_file(global_cookies)
    
    video_path, photos, description = await download_instagram_mobile_api(shortcode, cookies_dict)
    
    if video_path or photos:
        logger.info("✅ Успешно скачано (Mobile API)")
        return (video_path, photos, description)
    
    # === ВСЕ МЕТОДЫ НЕ СРАБОТАЛИ ===
    logger.error("❌ Все методы не сработали")
    
    # Формируем детальное сообщение об ошибке
    if not has_global_cookies and not has_user_cookies:
        error_msg = (
            "❌ <b>Не удалось скачать контент</b>\n\n"
            "Скорее всего это контент с возрастными ограничениями (18+).\n\n"
            "<b>Для скачивания 18+ контента:</b>\n"
            "1. Войдите в Instagram через браузер\n"
            "2. Экспортируйте cookies в формате Netscape\n"
            "3. Отправьте файл администратору бота\n\n"
            "<i>Обычный контент (без 18+) скачивается без cookies</i>"
        )
    else:
        error_msg = (
            "❌ <b>Не удалось скачать контент</b>\n\n"
            "<b>Возможные причины:</b>\n"
            "• Контент удален или недоступен\n"
            "• Приватный аккаунт\n"
            "• Instagram временно заблокировал доступ\n"
            "• Неверная ссылка\n\n"
            "<b>Попробуйте:</b>\n"
            "1. Убедиться что аккаунт публичный\n"
            "2. Скопировать ссылку заново\n"
            "3. Подождать несколько минут"
        )
    
    return None, None, error_msg

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
                text=f"📦 Файл ({size_mb:.1f} МБ) загружен на {name}\n"
                     f"📥 Скачать: {link}\n"
                     f"⏱️ Ссылка действительна несколько дней"
            )
            return True

    await bot.send_message(
        chat_id=chat_id,
        text=f"❌ Файл слишком большой ({size_mb:.1f} МБ).\n"
             f"Все сервисы загрузки недоступны."
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
    current_quality = get_quality_setting(message.from_user.id)
    welcome_text = (
        "🎬 <b>Добро пожаловать в VideoBot!</b>\n"
        "Я могу скачать видео с:\n"
        "• YouTube\n"
        "• TikTok\n"
        "• Instagram\n"
        "📲 Просто отправь мне ссылку!\n\n"
        f"⚙️ Текущее качество: <b>{current_quality.upper()}</b>"
    )
    await message.answer(welcome_text, reply_markup=main_keyboard(), parse_mode="HTML")

@dp.message(F.text == "⚙️ Настройки")
async def settings_menu(message: types.Message, state: FSMContext):
    await state.set_state(VideoStates.choosing_quality)
    current = get_quality_setting(message.from_user.id)
    await message.answer(
        f"⚙️ Текущее качество: <b>{current.upper()}</b>\nВыберите новое:",
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
    set_quality_setting(message.from_user.id, quality_map[message.text])
    await message.answer(
        f"Установлено: <b>{message.text}</b>",
        reply_markup=main_keyboard(),
        parse_mode="HTML"
    )
    await state.clear()

@dp.message(VideoStates.choosing_quality, F.text == "◀️ Назад")
async def back_to_main(message: types.Message, state: FSMContext):
    await state.clear()
    current_quality = get_quality_setting(message.from_user.id)
    await message.answer(
        f"🏠 Главное меню\n"
        f"⚙️ Текущее качество: <b>{current_quality.upper()}</b>",
        reply_markup=main_keyboard(),
        parse_mode="HTML"
    )

# === 🔗 ОБРАБОТЧИК ССЫЛОК ===
@dp.message(
    F.text &
    ~F.text.startswith("/") &
    ~F.text.in_([
        "⚙️ Настройки",
        "🌟 Лучшее", "🎬 1080p", "📺 720p", "⚡ 480p", "📱 360p", "◀️ Назад"
    ])
)
async def handle_link(message: types.Message, state: FSMContext):
    if await state.get_state() is not None:
        return
    
    url = message.text.strip()
    if not is_valid_url(url):
        await message.answer("⚠️ Отправьте корректную ссылку на YouTube, TikTok или Instagram")
        return
    
    await check_rate_limit(message.from_user.id)
    platform = detect_platform(url)
    status_msg = await message.answer(f"⏳ Обрабатываю {platform.upper()}...")
    user_quality = get_quality_setting(message.from_user.id)
    user_id = message.from_user.id
    temp_file = None
    temp_photos = []
    
    try:
        if platform == 'instagram':
            temp_file, photos, description = await download_instagram(url, user_quality, user_id)
            
            if description and "❌" in description:
                await status_msg.edit_text(description, parse_mode="HTML")
                return
            if photos:
                temp_photos = photos
                await status_msg.delete()
                success = await send_photos_with_caption(message.chat.id, photos, description)
                cleanup_files(photos)
                return
            if temp_file and os.path.exists(temp_file):
                await status_msg.edit_text("📤 Отправляю...")
                await send_video_or_link(message.chat.id, temp_file)
                await status_msg.delete()
                cleanup_file(temp_file)
                return
            if not temp_file and not photos:
                error_detail = description if description else "❌ Не удалось скачать контент"
                await status_msg.edit_text(error_detail, parse_mode="HTML")
                return
        
        elif platform == 'tiktok':
            if '/photo/' in url.lower():
                photos, description = await download_tiktok_photos(url)
                await status_msg.delete()
                if photos:
                    temp_photos = photos
                    success = await send_photos_with_caption(message.chat.id, photos, description)
                    cleanup_files(photos)
                else:
                    await message.answer(description)
                return
        
        temp_file = await download_video(url, user_quality)
        if not temp_file or not os.path.exists(temp_file):
            await status_msg.edit_text("❌ Не удалось скачать видео всеми доступными методами")
            return
        
        await status_msg.edit_text("📤 Отправляю...")
        await send_video_or_link(message.chat.id, temp_file)
        await status_msg.delete()
        cleanup_file(temp_file)
    
    except Exception as e:
        error_msg = f"❌ Ошибка: {str(e)}"
        logger.error(error_msg)
        try:
            await status_msg.edit_text(error_msg)
        except:
            pass
    finally:
        if temp_file:
            cleanup_file(temp_file)
        if temp_photos:
            cleanup_files(temp_photos)

# === 🚀 ЗАПУСК: ГИБКИЙ РЕЖИМ ===
async def main():
    logger.info("🚀 Запуск бота...")
    if WEBHOOK_HOST:
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
        logger.info("🔄 Запуск в режиме long polling (локально)")
        await dp.start_polling(bot, skip_updates=True)

if __name__ == "__main__":
    asyncio.run(main())