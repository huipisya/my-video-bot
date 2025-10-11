import os
import tempfile
import asyncio
import logging
import uuid
import re
import json
import time
from pathlib import Path
from typing import Optional, Tuple, List, Dict
import aiohttp
from aiogram import Bot, Dispatcher, types, F
from aiogram.filters import Command
from aiogram.fsm.state import State, StatesGroup
from aiogram.fsm.context import FSMContext
from aiogram.types import ReplyKeyboardMarkup, KeyboardButton, FSInputFile, InputMediaPhoto, InputMediaVideo
from aiogram.exceptions import TelegramBadRequest
from dotenv import load_dotenv
import yt_dlp
import sys

sys.stdout.reconfigure(encoding='utf-8')

# === 🧰 ЛОГИРОВАНИЕ ===
logging.basicConfig(
    level=logging.INFO,
    format='%(asctime)s - %(levelname)s - %(message)s'
)
logger = logging.getLogger(__name__)

# === 🔐 ЗАГРУЗКА ПЕРЕМЕННЫХ ОКРУЖЕНИЯ ===
load_dotenv()

# === 📁 СОЗДАНИЕ COOKIES ИЗ ПЕРЕМЕННЫХ ОКРУЖЕНИЯ ===
def init_cookies_from_env():
    """Создаёт файлы cookies из переменных окружения Railway"""
    cookies_created = 0
    
    # Проверяем cookies.txt (глобальные)
    cookies_txt_content = os.getenv("COOKIES_TXT")
    if cookies_txt_content:
        cookies_file = Path("cookies.txt")
        with open(cookies_file, 'w', encoding='utf-8') as f:
            f.write(cookies_txt_content)
        logger.info(f"✅ Создан cookies.txt")
        cookies_created += 1
    
    # Проверяем cookies_bot1/2/3
    for i in range(1, 4):
        env_var = f"COOKIES_BOT{i}"
        cookies_content = os.getenv(env_var)
        if cookies_content:
            cookies_file = Path(f"cookies_bot{i}")
            with open(cookies_file, 'w', encoding='utf-8') as f:
                f.write(cookies_content)
            logger.info(f"✅ Создан cookies_bot{i}")
            cookies_created += 1
    
    if cookies_created == 0:
        logger.warning("⚠️ Не найдено cookies в переменных окружения")
    else:
        logger.info(f"✅ Создано {cookies_created} файлов cookies")

# Инициализируем cookies
init_cookies_from_env()

# === 🔐 ТОКЕН И WEBHOOK ===
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
RATE_LIMIT_DELAY = {}

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
    """Удаляет файл"""
    try:
        if file_path and os.path.exists(file_path):
            os.remove(file_path)
            logger.debug(f"🗑️ Удалён файл: {Path(file_path).name}")
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
        await asyncio.sleep(delay)
    RATE_LIMIT_DELAY[user_id] = time.time()

def get_quality_setting(user_id: int) -> str:
    return user_settings.get(user_id, "720p")

def set_quality_setting(user_id: int, quality: str):
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

# === 📸 НОВЫЙ РАЗДЕЛ INSTAGRAM ===

async def extract_instagram_shortcode(url: str) -> Optional[str]:
    """Извлекает shortcode из URL Instagram"""
    match = re.search(r'/(?:p|reel|reels|tv)/([A-Za-z0-9_-]+)', url)
    if match:
        shortcode = match.group(1)
        logger.debug(f"Извлечён shortcode: {shortcode}")
        return shortcode
    
    if '/share/' in url:
        try:
            async with aiohttp.ClientSession(timeout=aiohttp.ClientTimeout(total=10)) as session:
                async with session.get(url, allow_redirects=True) as resp:
                    final_url = str(resp.url)
                    match = re.search(r'/(?:p|reel|reels|tv)/([A-Za-z0-9_-]+)', final_url)
                    if match:
                        shortcode = match.group(1)
                        logger.debug(f"Извлечён shortcode из /share/: {shortcode}")
                        return shortcode
        except Exception as e:
            logger.warning(f"Не удалось резолвить /share/: {e}")
    
    return None


def load_cookies_from_file(cookies_file: Path) -> Dict[str, str]:
    """Загружает cookies из файла"""
    cookies = {}
    try:
        with open(cookies_file, 'r', encoding='utf-8') as f:
            for line in f:
                if line.startswith('#') or not line.strip():
                    continue
                try:
                    parts = line.strip().split('\t')
                    if len(parts) >= 7:
                        cookies[parts[5]] = parts[6]
                except:
                    continue
        logger.debug(f"Загружено {len(cookies)} cookies")
    except Exception as e:
        logger.error(f"Ошибка чтения cookies: {e}")
    return cookies


async def download_instagram_mobile_public(shortcode: str) -> Tuple[Optional[str], Optional[List[str]], Optional[str], Optional[str]]:
    """
    Публичное скачивание через Mobile API
    Returns: (video_path, photos_list, description, error_code)
    """
    try:
        api_url = f"https://www.instagram.com/api/v1/media/{shortcode}/info/"
        
        headers = {
            'User-Agent': 'Instagram 269.0.0.18.75 Android (30/11; 420dpi; 1080x2265; OnePlus; ONEPLUS A6000; OnePlus6; qcom; en_US; 314665256)',
            'Accept': '*/*',
            'Accept-Language': 'en-US',
            'X-IG-App-ID': '567067343352427',
            'X-IG-Device-ID': str(uuid.uuid4()),
        }
        
        async with aiohttp.ClientSession(timeout=aiohttp.ClientTimeout(total=15)) as session:
            async with session.get(api_url, headers=headers) as resp:
                if resp.status == 404:
                    logger.warning("Mobile API: контент не найден")
                    return None, None, None, '404'
                
                if resp.status == 403:
                    logger.info("Mobile API: требуется авторизация")
                    return None, None, None, '403'
                
                if resp.status != 200:
                    logger.warning(f"Mobile API: статус {resp.status}")
                    return None, None, None, 'other'
                
                try:
                    data = await resp.json()
                except:
                    return None, None, None, 'other'
                
                items = data.get('items', [])
                if not items:
                    return None, None, None, 'other'
                
                media = items[0]
                media_type = media.get('media_type', 0)
                
                caption = media.get('caption')
                description = caption.get('text', '📸 Instagram') if caption else '📸 Instagram'
                description = description[:200] + '...' if len(description) > 200 else description
                
                # ВИДЕО
                if media_type == 2:
                    video_versions = media.get('video_versions', [])
                    if video_versions:
                        video_url = video_versions[0].get('url')
                        if video_url:
                            video_path = os.path.join(tempfile.gettempdir(), f"ig_{shortcode}.mp4")
                            if await download_file(video_url, video_path, timeout=30):
                                logger.info("✅ Видео скачано (публично)")
                                return (video_path, None, None, None)
                
                # КАРУСЕЛЬ
                elif media_type == 8:
                    carousel_media = media.get('carousel_media', [])
                    all_media = []
                    
                    for idx, item in enumerate(carousel_media[:10]):
                        item_type = item.get('media_type', 0)
                        
                        if item_type == 2:  # Видео
                            video_versions = item.get('video_versions', [])
                            if video_versions:
                                video_url = video_versions[0].get('url')
                                if video_url:
                                    video_path = os.path.join(tempfile.gettempdir(), f"ig_{shortcode}_v{idx}.mp4")
                                    if await download_file(video_url, video_path, timeout=30):
                                        all_media.append(video_path)
                        
                        elif item_type == 1:  # Фото
                            img_candidates = item.get('image_versions2', {}).get('candidates', [])
                            if img_candidates:
                                img_url = img_candidates[0].get('url')
                                if img_url:
                                    photo_path = os.path.join(tempfile.gettempdir(), f"ig_{shortcode}_p{idx}.jpg")
                                    if await download_file(img_url, photo_path, timeout=15):
                                        all_media.append(photo_path)
                    
                    if all_media:
                        logger.info(f"✅ Карусель: {len(all_media)} элементов")
                        return (None, all_media, description, None)
                
                # ФОТО
                elif media_type == 1:
                    img_candidates = media.get('image_versions2', {}).get('candidates', [])
                    if img_candidates:
                        img_url = img_candidates[0].get('url')
                        if img_url:
                            photo_path = os.path.join(tempfile.gettempdir(), f"ig_{shortcode}.jpg")
                            if await download_file(img_url, photo_path, timeout=15):
                                logger.info("✅ Фото скачано (публично)")
                                return (None, [photo_path], description, None)
    
    except Exception as e:
        logger.error(f"Mobile API error: {e}")
    
    return None, None, None, 'other'


async def download_instagram_mobile_cookies(shortcode: str, cookies_dict: Dict[str, str]) -> Tuple[Optional[str], Optional[List[str]], Optional[str]]:
    """Скачивание с авторизацией"""
    try:
        api_url = f"https://www.instagram.com/api/v1/media/{shortcode}/info/"
        
        headers = {
            'User-Agent': 'Instagram 269.0.0.18.75 Android (30/11; 420dpi; 1080x2265; OnePlus; ONEPLUS A6000; OnePlus6; qcom; en_US; 314665256)',
            'Accept': '*/*',
            'X-IG-App-ID': '567067343352427',
            'X-IG-Device-ID': str(uuid.uuid4()),
        }
        
        async with aiohttp.ClientSession(timeout=aiohttp.ClientTimeout(total=15)) as session:
            async with session.get(api_url, headers=headers, cookies=cookies_dict) as resp:
                if resp.status != 200:
                    return None, None, None
                
                data = await resp.json()
                items = data.get('items', [])
                if not items:
                    return None, None, None
                
                media = items[0]
                media_type = media.get('media_type', 0)
                
                caption = media.get('caption')
                description = caption.get('text', '📸 Instagram') if caption else '📸 Instagram'
                description = description[:200] + '...' if len(description) > 200 else description
                
                # ВИДЕО
                if media_type == 2:
                    video_versions = media.get('video_versions', [])
                    if video_versions:
                        video_url = video_versions[0].get('url')
                        if video_url:
                            video_path = os.path.join(tempfile.gettempdir(), f"ig_auth_{shortcode}.mp4")
                            if await download_file(video_url, video_path, timeout=30):
                                logger.info("✅ Видео (с cookies)")
                                return (video_path, None, None)
                
                # КАРУСЕЛЬ
                elif media_type == 8:
                    carousel_media = media.get('carousel_media', [])
                    all_media = []
                    
                    for idx, item in enumerate(carousel_media[:10]):
                        item_type = item.get('media_type', 0)
                        
                        if item_type == 2:
                            video_versions = item.get('video_versions', [])
                            if video_versions:
                                video_url = video_versions[0].get('url')
                                if video_url:
                                    video_path = os.path.join(tempfile.gettempdir(), f"ig_auth_{shortcode}_v{idx}.mp4")
                                    if await download_file(video_url, video_path, timeout=30):
                                        all_media.append(video_path)
                        
                        elif item_type == 1:
                            img_candidates = item.get('image_versions2', {}).get('candidates', [])
                            if img_candidates:
                                img_url = img_candidates[0].get('url')
                                if img_url:
                                    photo_path = os.path.join(tempfile.gettempdir(), f"ig_auth_{shortcode}_p{idx}.jpg")
                                    if await download_file(img_url, photo_path, timeout=15):
                                        all_media.append(photo_path)
                    
                    if all_media:
                        logger.info(f"✅ Карусель (cookies): {len(all_media)} элементов")
                        return (None, all_media, description)
                
                # ФОТО
                elif media_type == 1:
                    img_candidates = media.get('image_versions2', {}).get('candidates', [])
                    if img_candidates:
                        img_url = img_candidates[0].get('url')
                        if img_url:
                            photo_path = os.path.join(tempfile.gettempdir(), f"ig_auth_{shortcode}.jpg")
                            if await download_file(img_url, photo_path, timeout=15):
                                logger.info("✅ Фото (cookies)")
                                return (None, [photo_path], description)
    
    except Exception as e:
        logger.error(f"Mobile API (cookies) error: {e}")
    
    return None, None, None


async def download_instagram_ytdlp_cookies(url: str, cookies_file: Path) -> Optional[str]:
    """yt-dlp с cookies (резервный метод)"""
    try:
        ydl_opts = {
            'format': 'best',
            'merge_output_format': 'mp4',
            'noplaylist': True,
            'outtmpl': os.path.join(tempfile.gettempdir(), 'ig_ytdlp_%(id)s.%(ext)s'),
            'quiet': True,
            'no_warnings': True,
            'cookiefile': str(cookies_file),
        }
        
        with yt_dlp.YoutubeDL(ydl_opts) as ydl:
            info = ydl.extract_info(url, download=True)
            temp_file = ydl.prepare_filename(info)
            
            if temp_file and os.path.exists(temp_file):
                logger.info("✅ yt-dlp + cookies")
                return temp_file
    
    except Exception as e:
        logger.error(f"yt-dlp error: {e}")
    
    return None


async def download_instagram(url: str, quality: str = "best", user_id: Optional[int] = None) -> Tuple[Optional[str], Optional[List[str]], Optional[str]]:
    """
    Главная функция скачивания Instagram
    Логика: публично → cookies при ошибке → yt-dlp
    """
    shortcode = await extract_instagram_shortcode(url)
    if not shortcode:
        return None, None, "❌ Некорректная ссылка на Instagram"
    
    logger.info(f"📌 Instagram: {shortcode}")
    
    # ШАГ 1: ПУБЛИЧНОЕ СКАЧИВАНИЕ
    logger.info("🔄 Публичное скачивание...")
    video_path, photos, description, error_code = await download_instagram_mobile_public(shortcode)
    
    if video_path or photos:
        return (video_path, photos, description)
    
    if error_code == '404':
        return None, None, "❌ Контент не найден или удалён"
    
    # ШАГ 2: ТРЕБУЕТСЯ АВТОРИЗАЦИЯ
    if error_code == '403' or error_code == 'other':
        logger.info("🔐 Используем cookies...")
        
        cookies_files = []
        if Path("cookies.txt").exists():
            cookies_files.append(Path("cookies.txt"))
        for i in range(1, 4):
            cookies_file = Path(f"cookies_bot{i}")
            if cookies_file.exists():
                cookies_files.append(cookies_file)
        
        if not cookies_files:
            return None, None, (
                "❌ Контент защищён (18+ или приватный)\n\n"
                "Для скачивания необходимы cookies."
            )
        
        # Пробуем каждый cookies
        for cookies_file in cookies_files:
            logger.info(f"🔄 {cookies_file.name}...")
            cookies_dict = load_cookies_from_file(cookies_file)
            
            if not cookies_dict:
                continue
            
            video_path, photos, description = await download_instagram_mobile_cookies(shortcode, cookies_dict)
            if video_path or photos:
                return (video_path, photos, description)
        
        # ШАГ 3: YT-DLP
        logger.info("🔄 Резервный метод: yt-dlp...")
        for cookies_file in cookies_files:
            result = await download_instagram_ytdlp_cookies(url, cookies_file)
            if result:
                return (result, None, None)
        
        return None, None, (
            "❌ Не удалось скачать\n\n"
            "Возможные причины:\n"
            "• Приватный аккаунт\n"
            "• Контент удалён\n"
            "• Временная блокировка"
        )
    
    return None, None, "❌ Ошибка при скачивании"


async def send_instagram_content(chat_id: int, video_path: Optional[str], photos: Optional[List[str]], description: Optional[str]):
    """Отправка Instagram контента"""
    try:
        # Одиночное видео
        if video_path and not photos:
            file_size_mb = os.path.getsize(video_path) / (1024 * 1024)
            if file_size_mb <= 50:
                await bot.send_video(
                    chat_id=chat_id,
                    video=FSInputFile(video_path),
                    caption=description[:1024] if description else None
                )
                return True
            else:
                await bot.send_message(
                    chat_id=chat_id,
                    text=f"📦 Видео слишком большое ({file_size_mb:.1f} МБ)"
                )
                return False
        
        # Карусель
        elif photos and len(photos) > 1:
            media_group = []
            
            for idx, media_path in enumerate(photos[:10]):
                ext = Path(media_path).suffix.lower()
                
                if ext in ['.mp4', '.mov']:
                    media_group.append(
                        InputMediaVideo(
                            media=FSInputFile(media_path),
                            caption=description[:1024] if idx == 0 and description else None
                        )
                    )
                else:
                    media_group.append(
                        InputMediaPhoto(
                            media=FSInputFile(media_path),
                            caption=description[:1024] if idx == 0 and description else None
                        )
                    )
            
            await bot.send_media_group(chat_id=chat_id, media=media_group)
            return True
        
        # Одиночное фото
        elif photos and len(photos) == 1:
            await bot.send_photo(
                chat_id=chat_id,
                photo=FSInputFile(photos[0]),
                caption=description[:1024] if description else None
            )
            return True
    
    except Exception as e:
        logger.error(f"Ошибка отправки: {e}")
        return False

# === 📤 TIKTOK ФОТО ===
async def download_tiktok_photos(url: str) -> Tuple[Optional[List[str]], str]:
    try:
        clean_url = url.split('?')[0]
        headers = {
            'User-Agent': 'Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36',
            'Accept': 'text/html,application/xhtml+xml,application/xml;q=0.9,image/webp,*/*;q=0.8',
            'Referer': 'https://www.tiktok.com/',
        }
        async with aiohttp.ClientSession(timeout=aiohttp.ClientTimeout(total=30)) as session:
            async with session.get(clean_url, headers=headers, allow_redirects=True) as resp:
                if resp.status != 200:
                    return None, f"❌ TikTok вернул статус {resp.status}"
                html = await resp.text()

        json_match = re.search(r'<script id="__UNIVERSAL_DATA_FOR_REHYDRATION__" type="application/json">({.*})</script>', html)
        if not json_match:
            return None, "❌ Не найден JSON-блок"

        try:
            data = json.loads(json_match.group(1))
            item_info = data.get('__DEFAULT_SCOPE__', {}).get('webapp.photo.detail', {}).get('itemInfo', {})
            image_post = item_info.get('imagePost', {})
            images = image_post.get('images', [])
        except Exception as e:
            return None, "❌ Ошибка парсинга"

        if not images:
            return None, "❌ Фото не найдены"

        photos = []
        for i, img in enumerate(images[:10]):
            img_url = img.get('imageURL', {}).get('urlList', [])
            if not img_url:
                continue
            photo_path = os.path.join(tempfile.gettempdir(), f"tiktok_photo_{i}.jpg")
            if await download_file(img_url[0], photo_path, timeout=15):
                photos.append(photo_path)

        if photos:
            return (photos, "📸 TikTok")
        else:
            return None, "❌ Не удалось скачать фото"
    except Exception as e:
        return None, f"❌ Ошибка: {str(e)[:100]}"

# === 📤 СКАЧИВАНИЕ ВИДЕО (YouTube, TikTok) ===
async def download_video(url: str, quality: str = "best") -> Optional[str]:
    try:
        logger.info("🔄 Скачивание видео через yt-dlp...")
        ydl_opts = get_ydl_opts(quality)
        with yt_dlp.YoutubeDL(ydl_opts) as ydl:
            info = ydl.extract_info(url, download=True)
            temp_file = ydl.prepare_filename(info)
            if temp_file and os.path.exists(temp_file):
                return temp_file
    except Exception as e:
        logger.error(f"❌ yt-dlp: {e}")
    return None

# === 📤 ОТПРАВКА ВИДЕО ===
async def send_video_or_message(chat_id: int, file_path: str, caption: str = "") -> bool:
    file_size = Path(file_path).stat().st_size
    size_mb = file_size / (1024 * 1024)

    if size_mb <= 50:
        try:
            await bot.send_video(chat_id=chat_id, video=FSInputFile(file_path), caption=caption)
            logger.info(f"✅ Видео отправлено ({size_mb:.1f} МБ)")
            return True
        except Exception as e:
            logger.error(f"Ошибка отправки: {e}")

    await bot.send_message(
        chat_id=chat_id,
        text=f"❌ Видео слишком большое ({size_mb:.1f} МБ)"
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
        "🎬 <b>Добро пожаловать в VideoBot!</b>\n\n"
        "Я могу скачать видео с:\n"
        "• YouTube\n"
        "• TikTok\n"
        "• Instagram (посты, reels, карусели)\n\n"
        "📲 Просто отправь мне ссылку!\n\n"
        f"⚙️ Текущее качество: <b>{current_quality.upper()}</b>"
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
    set_quality_setting(message.from_user.id, quality_map[message.text])
    await message.answer(
        f"✅ Установлено: <b>{message.text}</b>",
        reply_markup=main_keyboard(),
        parse_mode="HTML"
    )
    await state.clear()

@dp.message(VideoStates.choosing_quality, F.text == "◀️ Назад")
async def back_to_main(message: types.Message, state: FSMContext):
    await state.clear()
    current_quality = get_quality_setting(message.from_user.id)
    await message.answer(
        f"🏠 Главное меню\n\n"
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
    
    temp_file = None
    temp_photos = []
    
    try:
        # === INSTAGRAM ===
        if platform == 'instagram':
            video_path, photos, description = await download_instagram(url, user_quality, message.from_user.id)
            
            # Ошибка
            if description and "❌" in description:
                await status_msg.edit_text(description, parse_mode="HTML")
                return
            
            # Успех
            if video_path or photos:
                await status_msg.edit_text("📤 Отправляю...")
                temp_file = video_path
                temp_photos = photos if photos else []
                
                success = await send_instagram_content(message.chat.id, video_path, photos, description)
                
                if success:
                    await status_msg.delete()
                else:
                    await status_msg.edit_text("❌ Ошибка при отправке")
                
                # Очистка
                if temp_file:
                    cleanup_file(temp_file)
                if temp_photos:
                    cleanup_files(temp_photos)
                return
            
            # Не удалось скачать
            await status_msg.edit_text("❌ Не удалось скачать контент")
            return
        
        # === TIKTOK ФОТО ===
        elif platform == 'tiktok' and '/photo/' in url.lower():
            photos, description = await download_tiktok_photos(url)
            await status_msg.delete()
            
            if photos:
                temp_photos = photos
                media_group = [
                    InputMediaPhoto(
                        media=FSInputFile(photo),
                        caption=description if i == 0 else None
                    )
                    for i, photo in enumerate(photos[:10])
                ]
                await bot.send_media_group(chat_id=message.chat.id, media=media_group)
                cleanup_files(photos)
            else:
                await message.answer(description)
            return
        
        # === YOUTUBE / TIKTOK ВИДЕО ===
        temp_file = await download_video(url, user_quality)
        
        if not temp_file or not os.path.exists(temp_file):
            await status_msg.edit_text("❌ Не удалось скачать видео")
            return
        
        await status_msg.edit_text("📤 Отправляю...")
        await send_video_or_message(message.chat.id, temp_file)
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

# === 🚀 ЗАПУСК ===
async def main():
    logger.info("🚀 Запуск бота...")
    
    if WEBHOOK_HOST:
        from aiogram.webhook.aiohttp_server import SimpleRequestHandler
        import aiohttp.web
        
        await bot.set_webhook(WEBHOOK_URL)
        logger.info(f"✅ Webhook установлен: {WEBHOOK_URL}")

        app = aiohttp.web.Application()
        webhook_requests_handler = SimpleRequestHandler(dispatcher=dp, bot=bot)
        webhook_requests_handler.register(app, path=WEBHOOK_PATH)

        port = int(os.getenv("PORT", 8000))
        runner = aiohttp.web.AppRunner(app)
        await runner.setup()
        site = aiohttp.web.TCPSite(runner, host="0.0.0.0", port=port)
        await site.start()

        logger.info(f"📡 Webhook-сервер на порту {port}")
        
        try:
            while True:
                await asyncio.sleep(3600)
        except (KeyboardInterrupt, SystemExit):
            logger.info("🛑 Бот остановлен")
            await bot.delete_webhook(drop_pending_updates=True)
            await runner.cleanup()
            await bot.session.close()
    else:
        logger.info("🔄 Запускаю в режиме long polling")
        await dp.start_polling(bot, skip_updates=True)

if __name__ == "__main__":
    asyncio.run(main())