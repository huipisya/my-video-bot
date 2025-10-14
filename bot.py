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
from playwright.async_api import async_playwright, Browser, BrowserContext, Page
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
    # Проверяем cookies_bot1/2/3 (Instagram)
    for i in range(1, 4):
        env_var = f"COOKIES_BOT{i}"
        cookies_content = os.getenv(env_var)
        if cookies_content:
            cookies_file = Path(f"cookies_bot{i}")
            with open(cookies_file, 'w', encoding='utf-8') as f:
                f.write(cookies_content)
            logger.info(f"✅ Создан cookies_bot{i}")
            cookies_created += 1
    # Проверяем YouTube cookies
    youtube_cookies = os.getenv("COOKIES_YOUTUBE")
    if youtube_cookies:
        cookies_file = Path("cookies_youtube.txt")
        with open(cookies_file, 'w', encoding='utf-8') as f:
            # Добавляем заголовок если его нет
            if not youtube_cookies.strip().startswith('#'):
                f.write("# Netscape HTTP Cookie File\n")
            f.write(youtube_cookies)
        logger.info(f"✅ Создан cookies_youtube.txt")
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
# --- НОВОЕ ---
# Переменные для Instagram Playwright
IG_BROWSER: Optional[Browser] = None
IG_CONTEXT: Optional[BrowserContext] = None
IG_PLAYWRIGHT_READY = False
# --- НОВОЕ ---
# Определение функции init_instagram_playwright
async def init_instagram_playwright():
    """
    Инициализирует браузер и контекст Playwright для Instagram.
    Загружает cookies из файла (если существует) и применяет их к контексту.
    """
    global IG_BROWSER, IG_CONTEXT, IG_PLAYWRIGHT_READY
    logger.info("🌐 Инициализация Instagram Playwright...")
    try:
        # Запускаем Playwright
        pw = await async_playwright().start()
        IG_BROWSER = await pw.chromium.launch(
            headless=True, # Установите False, если нужно видеть окно браузера для отладки
            # args=["--no-sandbox", "--disable-dev-shm-usage"] # Опционально для серверов
        )
        # Загружаем cookies из файла, если он существует
        cookies_to_load = []
        cookie_file_path = Path("cookies_playwright.json") # Имя файла для cookies от Playwright
        if cookie_file_path.exists():
            logger.info(f"🍪 Загружаем cookies из {cookie_file_path.name}")
            try:
                with open(cookie_file_path, 'r', encoding='utf-8') as f:
                    cookies_to_load = json.load(f)
            except json.JSONDecodeError:
                logger.warning(f"⚠️ Ошибка чтения JSON из {cookie_file_path.name}, пропускаем загрузку cookies.")
        else:
            logger.info(f"🍪 Файл {cookie_file_path.name} не найден, запуск без предзагруженных cookies.")
        # Создаём контекст с загруженными cookies
        IG_CONTEXT = await IG_BROWSER.new_context(
            # viewport={'width': 1920, 'height': 1080}, # Опционально: размер окна
            user_agent='Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/91.0.4472.124 Safari/537.36'
        )
        if cookies_to_load:
            await IG_CONTEXT.add_cookies(cookies_to_load)
            logger.info(f"✅ Загружено {len(cookies_to_load)} cookies в контекст Playwright.")
        IG_PLAYWRIGHT_READY = True
        logger.info("✅ Instagram Playwright инициализирован.")
    except Exception as e:
        logger.error(f"❌ Ошибка инициализации Instagram Playwright: {e}")
        IG_PLAYWRIGHT_READY = False
        # Опционально: попытаться закрыть браузер, если он был открыт
        if IG_BROWSER:
            await IG_BROWSER.close()
            IG_BROWSER = None
        if IG_CONTEXT:
            await IG_CONTEXT.close()
            IG_CONTEXT = None

# --- НОВОЕ (YouTube Playwright) ---
# Переменные для YouTube Playwright
YT_BROWSER: Optional[Browser] = None
YT_CONTEXT: Optional[BrowserContext] = None
YT_PLAYWRIGHT_READY = False

async def init_youtube_playwright():
    """
    Инициализирует браузер и контекст Playwright для YouTube.
    Загружает cookies из файла cookies_youtube.txt (если существует) и применяет их к контексту.
    """
    global YT_BROWSER, YT_CONTEXT, YT_PLAYWRIGHT_READY
    logger.info("🌐 Инициализация YouTube Playwright...")
    try:
        # Запускаем Playwright (создаём новый экземпляр для YT, чтобы изолировать от IG)
        pw = await async_playwright().start()
        YT_BROWSER = await pw.chromium.launch(
            headless=True,
            # args=["--no-sandbox", "--disable-dev-shm-usage"] # Опционально
        )
        # Загружаем cookies из файла, если он существует
        cookies_to_load = []
        cookie_file_path = Path("cookies_youtube.txt")
        if cookie_file_path.exists():
            logger.info(f"🍪 Загружаем YouTube cookies из {cookie_file_path.name}")
            try:
                with open(cookie_file_path, 'r', encoding='utf-8') as f:
                    lines = f.readlines()
                    # Пропускаем заголовок Netscape
                    for line in lines:
                        if line.startswith('#') or not line.strip():
                            continue
                        parts = line.strip().split('\t')
                        if len(parts) >= 7:
                            # Преобразуем строку куки в формат Playwright
                            cookies_to_load.append({
                                "name": parts[5],
                                "value": parts[6],
                                "domain": parts[0],
                                "path": parts[2],
                                "expires": int(parts[4]) if parts[4].isdigit() else None,
                                "httpOnly": bool(int(parts[3])),
                                "secure": bool(int(parts[1]))
                            })
            except Exception as e:
                logger.warning(f"⚠️ Ошибка чтения/парсинга {cookie_file_path.name} для Playwright: {e}")
        else:
            logger.info(f"🍪 Файл {cookie_file_path.name} не найден для Playwright.")

        # Создаём контекст с загруженными cookies
        YT_CONTEXT = await YT_BROWSER.new_context(
            user_agent='Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/91.0.4472.124 Safari/537.36'
        )
        if cookies_to_load:
            await YT_CONTEXT.add_cookies(cookies_to_load)
            logger.info(f"✅ Загружено {len(cookies_to_load)} cookies в контекст YouTube Playwright.")

        YT_PLAYWRIGHT_READY = True
        logger.info("✅ YouTube Playwright инициализирован.")
        # ВАЖНО: Не закрываем pw здесь, оставляем для использования функцией download_youtube_with_playwright
        # Также не закрываем YT_BROWSER и YT_CONTEXT, они нужны для скачивания
        # Их нужно будет закрыть в main() при завершении работы бота

    except Exception as e:
        logger.error(f"❌ Ошибка инициализации YouTube Playwright: {e}")
        YT_PLAYWRIGHT_READY = False
        if YT_BROWSER:
            await YT_BROWSER.close()
            YT_BROWSER = None
        if YT_CONTEXT:
            await YT_CONTEXT.close()
            YT_CONTEXT = None
# --- /НОВОЕ (YouTube Playwright) ---

# === 🎨 СОСТОЯНИЯ FSM ===
class VideoStates(StatesGroup):
    choosing_quality = State()
# === 📺 КАЧЕСТВА ВИДЕО ===
QUALITY_FORMATS = {
    "best": 'bestvideo[ext=mp4]+bestaudio[ext=m4a]/bestvideo+bestaudio/best',
    "1080p": 'bestvideo[ext=mp4][height<=1920][width<=1920]+bestaudio[ext=m4a]/bestvideo[height<=1920]+bestaudio/best',
    "720p": 'bestvideo[ext=mp4][height<=1280][width<=1280]+bestaudio[ext=m4a]/bestvideo[height<=1280]+bestaudio/best',
    "480p": 'bestvideo[ext=mp4][height<=854][width<=854]+bestaudio[ext=m4a]/bestvideo[height<=854]+bestaudio/best',
    "360p": 'bestvideo[ext=mp4][height<=640][width<=640]+bestaudio[ext=m4a]/bestvideo[height<=640]+bestaudio/best'
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
def get_ydl_opts(quality: str = "best", use_youtube_cookies: bool = True) -> dict: # Добавлен параметр use_youtube_cookies
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
    # YouTube cookies (теперь условно)
    if use_youtube_cookies and Path("cookies_youtube.txt").exists(): # Проверяем флаг И существование файла
        opts['cookiefile'] = 'cookies_youtube.txt'
        logger.info("🍪 Используем YouTube cookies (если применимо)")
    elif use_youtube_cookies:
         logger.debug("🍪 YouTube cookies не найдены или не требуются") # Опционально: лог, если флаг true, но файла нет
    # Proxy
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
# === 📸 INSTAGRAM MODULE (ПОЛНОСТЬЮ РАБОЧАЯ ВЕРСИЯ) ===
import random
# User-Agent pool для Instagram
USER_AGENTS_INSTAGRAM = [
    'Instagram 269.0.0.18.75 Android (30/11; 420dpi; 1080x2265; OnePlus; ONEPLUS A6000; OnePlus6; qcom; en_US; 314665256)',
    'Instagram 275.0.0.27.98 Android (31/12; 480dpi; 1080x2400; Samsung; SM-G998B; p3q; exynos2100; en_US)',
    'Instagram 280.0.0.33.109 Android (32/13; 560dpi; 1440x3120; Google; Pixel 7 Pro; cheetah; tensor; en_US)',
]
async def extract_instagram_shortcode(url: str) -> Optional[Tuple[str, bool]]:
    """Извлекает shortcode из URL Instagram"""
    url_lower = url.lower()
    is_reel = '/reel/' in url_lower or '/reels/' in url_lower
    match = re.search(r'/(?:p|reel|reels|tv)/([A-Za-z0-9_-]+)', url)
    if match:
        shortcode = match.group(1).split('?')[0]
        logger.info(f"📌 Shortcode: {shortcode} ({'REEL' if is_reel else 'POST'})")
        return (shortcode, is_reel)
    if '/share/' in url or 'instagram.com/s/' in url_lower:
        try:
            headers = {
                'User-Agent': 'Mozilla/5.0 (iPhone; CPU iPhone OS 17_0 like Mac OS X) AppleWebKit/605.1.15',
                'Accept': 'text/html,application/xhtml+xml,application/xml;q=0.9,*/*;q=0.8',
            }
            async with aiohttp.ClientSession(timeout=aiohttp.ClientTimeout(total=15)) as session:
                async with session.get(url, headers=headers, allow_redirects=True) as resp:
                    final_url = str(resp.url)
                    final_url_lower = final_url.lower()
                    is_reel = '/reel/' in final_url_lower or '/reels/' in final_url_lower
                    match = re.search(r'/(?:p|reel|reels|tv)/([A-Za-z0-9_-]+)', final_url)
                    if match:
                        shortcode = match.group(1).split('?')[0]
                        logger.info(f"✅ Shortcode: {shortcode}")
                        return (shortcode, is_reel)
        except Exception as e:
            logger.error(f"❌ Резолв share: {e}")
    return None
def load_cookies_from_file(cookies_file: Path) -> Dict[str, str]:
    """Загружает cookies из Netscape формата"""
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
        required = ['sessionid', 'ds_user_id']
        if all(key in cookies for key in required):
            logger.debug(f"✅ {len(cookies)} cookies из {cookies_file.name}")
            return cookies
        else:
            logger.warning(f"⚠️ {cookies_file.name}: нет sessionid/ds_user_id")
    except Exception as e:
        logger.error(f"❌ Ошибка чтения {cookies_file.name}: {e}")
    return {}
async def download_file_ig(url: str, save_path: str, timeout: int = 60) -> bool:
    """Скачивает файл для Instagram"""
    try:
        async with aiohttp.ClientSession(timeout=aiohttp.ClientTimeout(total=timeout)) as session:
            async with session.get(url) as resp:
                if resp.status == 200:
                    with open(save_path, 'wb') as f:
                        async for chunk in resp.content.iter_chunked(8192):
                            f.write(chunk)
                    return True
                else:
                    logger.warning(f"HTTP {resp.status} для {url[:50]}")
    except Exception as e:
        logger.error(f"Ошибка скачивания: {e}")
    return False
def select_video_quality(versions: List[Dict], quality: str) -> Optional[str]:
    """Выбирает URL видео нужного качества"""
    if not versions:
        return None
    sorted_versions = sorted(versions, key=lambda x: x.get('height', 0), reverse=True)
    if quality == "best":
        return sorted_versions[0].get('url')
    quality_map = {"1080p": 1080, "720p": 720, "480p": 480, "360p": 360}
    target_height = quality_map.get(quality, 1080)
    for version in sorted_versions:
        if version.get('height', 0) <= target_height:
            return version.get('url')
    return sorted_versions[-1].get('url')
def select_image_quality(candidates: List[Dict], quality: str) -> Optional[str]:
    """Выбирает URL фото нужного качества"""
    if not candidates:
        return None
    sorted_candidates = sorted(candidates, key=lambda x: x.get('width', 0), reverse=True)
    if quality == "best":
        return sorted_candidates[0].get('url')
    quality_map = {"1080p": 1080, "720p": 720, "480p": 480, "360p": 360}
    target_width = quality_map.get(quality, 1080)
    for candidate in sorted_candidates:
        if candidate.get('width', 0) <= target_width:
            return candidate.get('url')
    return sorted_candidates[-1].get('url')
async def download_instagram_mobile_api(
    shortcode: str, 
    quality: str = "best", 
    cookies_dict: Optional[Dict[str, str]] = None
) -> Tuple[Optional[str], Optional[List[str]], Optional[str], Optional[str]]:
    """
    Mobile API с поддержкой качества
    Returns: (video_path, photos_list, description, error_code)
    """
    try:
        api_url = f"https://www.instagram.com/api/v1/media/{shortcode}/info/"
        headers = {
            'User-Agent': random.choice(USER_AGENTS_INSTAGRAM),
            'Accept': '*/*',
            'Accept-Language': 'en-US',
            'X-IG-App-ID': '567067343352427',
            'X-IG-Device-ID': str(uuid.uuid4()),
        }
        async with aiohttp.ClientSession(timeout=aiohttp.ClientTimeout(total=30)) as session:
            async with session.get(api_url, headers=headers, cookies=cookies_dict or {}) as resp:
                status = resp.status
                if status == 404:
                    logger.warning("Mobile API: 404")
                    return None, None, None, '404'
                if status == 403:
                    logger.info("Mobile API: 403 (приватный)")
                    return None, None, None, '403'
                if status != 200:
                    logger.warning(f"Mobile API: статус {status}")
                    return None, None, None, 'other'
                try:
                    data = await resp.json()
                except:
                    logger.error("Mobile API: ошибка парсинга JSON")
                    return None, None, None, 'other'
                items = data.get('items', [])
                if not items:
                    logger.warning("Mobile API: нет items")
                    return None, None, None, 'other'
                media = items[0]
                media_type = media.get('media_type', 0)
                # Описание
                caption = media.get('caption')
                description = caption.get('text', '📸 Instagram') if caption else '📸 Instagram'
                if len(description) > 200:
                    description = description[:200] + '...'
                prefix = "ig_auth" if cookies_dict else "ig_pub"
                # ВИДЕО (media_type = 2)
                if media_type == 2:
                    video_versions = media.get('video_versions', [])
                    video_url = select_video_quality(video_versions, quality)
                    if video_url:
                        video_path = os.path.join(tempfile.gettempdir(), f"{prefix}_{shortcode}.mp4")
                        if await download_file_ig(video_url, video_path, timeout=120):
                            auth_tag = " + cookies" if cookies_dict else ""
                            logger.info(f"✅ Mobile API: видео {quality}{auth_tag}")
                            return (video_path, None, description, None)
                    else:
                        logger.warning("Mobile API: нет video_versions")
                # КАРУСЕЛЬ (media_type = 8)
                elif media_type == 8:
                    carousel_media = media.get('carousel_media', [])
                    all_media = []
                    for idx, item in enumerate(carousel_media):
                        item_type = item.get('media_type', 0)
                        if item_type == 2:  # Видео
                            video_versions = item.get('video_versions', [])
                            video_url = select_video_quality(video_versions, quality)
                            if video_url:
                                video_path = os.path.join(tempfile.gettempdir(), f"{prefix}_{shortcode}_v{idx}.mp4")
                                if await download_file_ig(video_url, video_path, timeout=120):
                                    all_media.append(video_path)
                        elif item_type == 1:  # Фото
                            img_candidates = item.get('image_versions2', {}).get('candidates', [])
                            img_url = select_image_quality(img_candidates, quality)
                            if img_url:
                                photo_path = os.path.join(tempfile.gettempdir(), f"{prefix}_{shortcode}_p{idx}.jpg")
                                if await download_file_ig(img_url, photo_path, timeout=60):
                                    all_media.append(photo_path)
                    if all_media:
                        auth_tag = " + cookies" if cookies_dict else ""
                        logger.info(f"✅ Mobile API: карусель ({len(all_media)} файлов){auth_tag}")
                        return (None, all_media, description, None)
                # ФОТО (media_type = 1)
                elif media_type == 1:
                    img_candidates = media.get('image_versions2', {}).get('candidates', [])
                    img_url = select_image_quality(img_candidates, quality)
                    if img_url:
                        photo_path = os.path.join(tempfile.gettempdir(), f"{prefix}_{shortcode}.jpg")
                        if await download_file_ig(img_url, photo_path, timeout=60):
                            auth_tag = " + cookies" if cookies_dict else ""
                            logger.info(f"✅ Mobile API: фото {quality}{auth_tag}")
                            return (None, [photo_path], description, None)
                    else:
                        logger.warning("Mobile API: нет image candidates")
                else:
                    logger.warning(f"Mobile API: неизвестный media_type={media_type}")
    except Exception as e:
        logger.error(f"❌ Mobile API: {e}")
    return None, None, None, 'other'
async def download_instagram_yt_dlp(
    url: str, 
    quality: str = "best", 
    cookies_file: Optional[Path] = None
) -> Tuple[Optional[str], Optional[List[str]], Optional[str]]:
    """yt-dlp с упрощенными форматами для Instagram"""
    try:
        # Instagram часто имеет только один формат
        format_str = 'best'
        ydl_opts = {
            'format': format_str,
            'merge_output_format': 'mp4',
            'noplaylist': False,
            'outtmpl': os.path.join(tempfile.gettempdir(), 'ig_ytdlp_%(id)s_%(autonumber)s.%(ext)s'),
            'quiet': True,
            'no_warnings': True,
            'socket_timeout': 120,
        }
        if cookies_file and cookies_file.exists():
            ydl_opts['cookiefile'] = str(cookies_file)
            logger.info(f"🍪 yt-dlp: {cookies_file.name}")
        with yt_dlp.YoutubeDL(ydl_opts) as ydl:
            info = ydl.extract_info(url, download=True)
            description = info.get('description', '📸 Instagram')
            if description and len(description) > 200:
                description = description[:200] + '...'
            # Карусель
            if info.get('_type') == 'playlist':
                downloaded_files = []
                for entry in info.get('entries', []):
                    file_path = ydl.prepare_filename(entry)
                    if file_path and os.path.exists(file_path):
                        downloaded_files.append(file_path)
                if downloaded_files:
                    logger.info(f"✅ yt-dlp: карусель ({len(downloaded_files)} файлов)")
                    return (None, downloaded_files, description)
            # Одиночный файл
            else:
                temp_file = ydl.prepare_filename(info)
                if temp_file and os.path.exists(temp_file):
                    ext = Path(temp_file).suffix.lower()
                    if ext in ['.mp4', '.mov']:
                        logger.info(f"✅ yt-dlp: видео")
                        return (temp_file, None, description)
                    elif ext in ['.jpg', '.jpeg', '.png', '.webp']:
                        logger.info(f"✅ yt-dlp: фото")
                        return (None, [temp_file], description)
    except Exception as e:
        logger.debug(f"yt-dlp: {str(e)[:100]}")
    return None, None, None
async def download_instagram(
    url: str, 
    quality: str = "best", 
    user_id: Optional[int] = None
) -> Tuple[Optional[str], Optional[List[str]], Optional[str]]:
    """
    Главная функция скачивания Instagram
    Стратегия:
    1. Mobile API публичный
    2. Mobile API + cookies
    3. yt-dlp + cookies
    4. yt-dlp публичный
    """
    result = await extract_instagram_shortcode(url)
    if not result:
        return None, None, "❌ Некорректная ссылка на Instagram"
    shortcode, is_reel = result
    # ШАГ 1: MOBILE API ПУБЛИЧНЫЙ
    logger.info(f"🔄 [1/4] Mobile API (качество={quality})...")
    video_path, photos, description, error_code = await download_instagram_mobile_api(shortcode, quality)
    if video_path or photos:
        final_description = None if is_reel else description
        return (video_path, photos, final_description)
    if error_code == '404':
        return None, None, "❌ Контент не найден или удалён"
    # ШАГ 2-4: С COOKIES
    if error_code in ['403', 'other']:
        logger.info("🔐 Используем cookies...")
        cookies_files = []
        if Path("cookies.txt").exists():
            cookies_files.append(Path("cookies.txt"))
        for i in range(1, 4):
            cookies_file = Path(f"cookies_bot{i}")
            if cookies_file.exists():
                cookies_files.append(cookies_file)
        if cookies_files:
            for idx, cookies_file in enumerate(cookies_files, 1):
                # Шаг 2: Mobile API + cookies
                logger.info(f"🔄 [2/4] ({idx}/{len(cookies_files)}) Mobile API + {cookies_file.name}...")
                cookies_dict = load_cookies_from_file(cookies_file)
                if cookies_dict:
                    video_path, photos, description, _ = await download_instagram_mobile_api(
                        shortcode, quality, cookies_dict
                    )
                    if video_path or photos:
                        final_description = None if is_reel else description
                        return (video_path, photos, final_description)
                # Шаг 3: yt-dlp + cookies
                logger.info(f"🔄 [3/4] ({idx}/{len(cookies_files)}) yt-dlp + {cookies_file.name}...")
                video_path, photos, description = await download_instagram_yt_dlp(url, quality, cookies_file)
                if video_path or photos:
                    final_description = None if is_reel else description
                    return (video_path, photos, final_description)
        # ШАГ 4: YT-DLP ПУБЛИЧНЫЙ
        logger.info(f"🔄 [4/4] yt-dlp публичный...")
        video_path, photos, description = await download_instagram_yt_dlp(url, quality)
        if video_path or photos:
            final_description = None if is_reel else description
            return (video_path, photos, final_description)
        return None, None, (
            "❌ Не удалось скачать контент\n"
            "Возможные причины:\n"
            "• Приватный аккаунт\n"
            "• Cookies устарели\n"
            "• Контент удалён\n"
            "• Rate-limit от Instagram\n"
            "💡 Попробуйте через несколько минут"
        )
    return None, None, "❌ Неизвестная ошибка"
async def send_instagram_content(
    chat_id: int, 
    video_path: Optional[str], 
    photos: Optional[List[str]], 
    description: Optional[str]
) -> bool:
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
        # Карусель/фото
        elif photos:
            total = len(photos)
            logger.info(f"📤 Отправка {total} файлов...")
            for batch_start in range(0, total, 10):
                batch = photos[batch_start:batch_start + 10]
                media_group = []
                for idx, media_path in enumerate(batch):
                    if not os.path.exists(media_path):
                        continue
                    ext = Path(media_path).suffix.lower()
                    caption = None
                    if batch_start == 0 and idx == 0 and description:
                        caption = description[:1024]
                    if ext in ['.mp4', '.mov']:
                        media_group.append(InputMediaVideo(
                            media=FSInputFile(media_path), 
                            caption=caption
                        ))
                    else:
                        media_group.append(InputMediaPhoto(
                            media=FSInputFile(media_path), 
                            caption=caption
                        ))
                if media_group:
                    if len(media_group) == 1:
                        item = media_group[0]
                        if isinstance(item, InputMediaPhoto):
                            await bot.send_photo(
                                chat_id=chat_id, 
                                photo=item.media, 
                                caption=item.caption
                            )
                        else:
                            await bot.send_video(
                                chat_id=chat_id, 
                                video=item.media, 
                                caption=item.caption
                            )
                    else:
                        await bot.send_media_group(chat_id=chat_id, media=media_group)
                    if batch_start + 10 < total:
                        await asyncio.sleep(1)
            logger.info(f"✅ Отправлено {total} файлов")
            return True
    except Exception as e:
        logger.error(f"❌ Ошибка отправки: {e}")
        return False
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

# === 📤 ОБНОВЛЕНАЯ ФУНКЦИЯ СКАЧИВАНИЯ ВИДЕО (YouTube Playwright) ===
async def download_video(url: str, quality: str = "best", platform: str = "youtube") -> Tuple[Optional[str], Optional[str]]: # Возвращает (file_path, error_message)
    """Скачивание видео (YouTube, TikTok) через yt-dlp"""
    try:
        logger.info(f"🔄 Скачивание видео с {platform.upper()} (качество={quality})...")
        # Решаем, использовать ли YouTube cookies. TikTok - нет.
        use_yt_cookies = (platform.lower() == 'youtube')
        ydl_opts = get_ydl_opts(quality, use_youtube_cookies=use_yt_cookies) # Передаем флаг

        with yt_dlp.YoutubeDL(ydl_opts) as ydl:
            info = ydl.extract_info(url, download=True)
            temp_file = ydl.prepare_filename(info)
            if temp_file and os.path.exists(temp_file):
                logger.info(f"✅ Видео скачано: {Path(temp_file).name}")
                return temp_file, None # Успешно
    except yt_dlp.DownloadError as e:
        error_str = str(e)
        logger.error(f"❌ yt-dlp ({platform}): {error_str}")
        # Проверяем, связана ли ошибка с аутентификацией
        if "Sign in to confirm you're not a bot" in error_str or "requires authentication" in error_str.lower():
             logger.info("🔄 Ошибка требует аутентификации, пробуем Playwright...")
             return None, "auth_required" # Специальный код ошибки
        else:
             return None, error_str # Другая ошибка
    except Exception as e:
        logger.error(f"❌ Неизвестная ошибка при скачивании {platform}: {e}")
        return None, str(e) # Неизвестная ошибка
    return None, "Неизвестная ошибка"

# --- НОВОЕ (YouTube Playwright) ---
async def download_youtube_with_playwright(url: str, quality: str = "best") -> Optional[str]:
    """
    Использует Playwright для открытия YouTube, затем yt-dlp для скачивания,
    надеясь, что браузер помог обойти проверки.
    """
    global YT_CONTEXT
    if not YT_PLAYWRIGHT_READY or not YT_CONTEXT:
        logger.error("❌ YouTube Playwright не инициализирован")
        return None

    page: Optional[Page] = None
    temp_cookies_file = None # Инициализируем переменную для временного файла куки
    try:
        logger.info(f"🌐 Открываю YouTube в Playwright для {url}")
        page = await YT_CONTEXT.new_page()
        # Устанавливаем таймауты
        page.set_default_timeout(60000) # 60 секунд
        page.set_default_navigation_timeout(60000)

        # Навигация к URL
        await page.goto(url, wait_until='networkidle')
        logger.info("🌐 Страница загружена")

        # Ждём появления элементов, указывающих на видео
        await page.wait_for_selector('video, #player', timeout=10000)
        logger.info("✅ Видео элемент найден")

        # Проверим, не появилась ли страница с ошибкой или подтверждением
        # (Это базовая проверка, можно улучшить)
        page_title = await page.title()
        if "Sign in" in page_title or "not a bot" in page.content():
             logger.warning("⚠️ Обнаружена страница аутентификации или подтверждения в Playwright")
             # Попробуем всё равно использовать yt-dlp, но с куки из контекста
             # Извлекаем куки из контекста
             cookies = await YT_CONTEXT.cookies()
             logger.info(f"🍪 Извлечено {len(cookies)} куки из Playwright контекста")
             # yt-dlp может использовать куки из памяти, но проще сохранить во временный файл
             import tempfile # Импортируем внутри функции
             temp_cookies_file = Path(tempfile.mktemp(suffix='.txt', prefix='yt_cookies_'))
             with open(temp_cookies_file, 'w', encoding='utf-8') as f:
                 f.write("# Netscape HTTP Cookie File\n")
                 for cookie in cookies:
                     # Формат: domain flag path secure expiration name value
                     # Упрощаем формат, yt-dlp должен понять
                     f.write(f"{cookie['domain']}\tTRUE\t{cookie['path']}\t{str(cookie['secure']).upper()}\t{cookie['expires'] or 0}\t{cookie['name']}\t{cookie['value']}\n")

             # Используем временный файл с куки для yt-dlp
             ydl_opts = get_ydl_opts(quality, use_youtube_cookies=False) # Не используем глобальный файл
             ydl_opts['cookiefile'] = str(temp_cookies_file) # Используем временный файл
             logger.info("🔄 Повторная попытка скачивания через yt-dlp с куки из Playwright...")
             with yt_dlp.YoutubeDL(ydl_opts) as ydl:
                 info = ydl.extract_info(url, download=True)
                 temp_file = ydl.prepare_filename(info)
                 if temp_file and os.path.exists(temp_file):
                     logger.info(f"✅ Видео скачано через yt-dlp с куки из Playwright: {Path(temp_file).name}")
                     # Удаляем временный файл куки
                     # temp_cookies_file.unlink(missing_ok=True) # Удалим в finally
                     return temp_file
                 else:
                     logger.error("❌ yt-dlp не создал файл после использования куки из Playwright")
             # Удаляем временный файл куки, если что-то пошло не так
             # temp_cookies_file.unlink(missing_ok=True) # Удалим в finally
             return None

        # Если страница нормально загрузилась без подтверждений
        # Попробуем использовать yt-dlp с куки из контекста Playwright
        # Это может сработать, если браузер помог "ожить" куки
        cookies = await YT_CONTEXT.cookies()
        logger.info(f"🍪 Извлечено {len(cookies)} куки из Playwright контекста (альтернативный способ)")
        import tempfile # Импортируем внутри функции
        temp_cookies_file = Path(tempfile.mktemp(suffix='.txt', prefix='yt_cookies_alt_'))
        with open(temp_cookies_file, 'w', encoding='utf-8') as f:
             f.write("# Netscape HTTP Cookie File\n")
             for cookie in cookies:
                 f.write(f"{cookie['domain']}\tTRUE\t{cookie['path']}\t{str(cookie['secure']).upper()}\t{cookie['expires'] or 0}\t{cookie['name']}\t{cookie['value']}\n")

        ydl_opts = get_ydl_opts(quality, use_youtube_cookies=False)
        ydl_opts['cookiefile'] = str(temp_cookies_file)
        logger.info("🔄 Повторная попытка скачивания через yt-dlp с куки из Playwright (альтернативный способ)...")
        with yt_dlp.YoutubeDL(ydl_opts) as ydl:
             info = ydl.extract_info(url, download=True)
             temp_file = ydl.prepare_filename(info)
             if temp_file and os.path.exists(temp_file):
                 logger.info(f"✅ Видео скачано через yt-dlp с куки из Playwright (альт): {Path(temp_file).name}")
                 # temp_cookies_file.unlink(missing_ok=True) # Удалим в finally
                 return temp_file
             else:
                 logger.error("❌ yt-dlp не создал файл (альт. способ)")
        # temp_cookies_file.unlink(missing_ok=True) # Удалим в finally
        return None


    except Exception as e:
        logger.error(f"❌ Ошибка в download_youtube_with_playwright: {e}")
    finally:
        if page:
            await page.close()
        # Удаляем временный файл куки, если он был создан
        if temp_cookies_file and temp_cookies_file.exists():
            temp_cookies_file.unlink(missing_ok=True)
    return None
# --- /НОВОЕ (YouTube Playwright) ---


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
        "🎬 <b>Добро пожаловать в VideoBot!</b>\n"
        "Я могу скачать видео с:\n"
        "• YouTube\n"
        "• TikTok\n"
        "• Instagram (посты, reels, карусели)\n"
        "📲 Просто отправь мне ссылку!\n"
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
    temp_file = None
    temp_photos = []
    status_msg_deleted = False
    async def safe_edit_status(text: str):
        """Безопасное редактирование статусного сообщения"""
        nonlocal status_msg_deleted
        if not status_msg_deleted:
            try:
                await status_msg.edit_text(text)
            except TelegramBadRequest:
                status_msg_deleted = True
    async def safe_delete_status():
        """Безопасное удаление статусного сообщения"""
        nonlocal status_msg_deleted
        if not status_msg_deleted:
            try:
                await status_msg.delete()
                status_msg_deleted = True
            except TelegramBadRequest:
                status_msg_deleted = True
    try:
        # === INSTAGRAM ===
        if platform == 'instagram':
            video_path, photos, description = await download_instagram(url, user_quality, message.from_user.id)
            # Ошибка
            if description and "❌" in description:
                await safe_edit_status(description)
                return
            # Успех
            if video_path or photos:
                await safe_edit_status("📤 Отправляю...")
                temp_file = video_path
                temp_photos = photos if photos else []
                success = await send_instagram_content(message.chat.id, video_path, photos, description)
                if success:
                    await safe_delete_status()
                else:
                    await safe_edit_status("❌ Ошибка при отправке")
                # Очистка
                if temp_file:
                    cleanup_file(temp_file)
                if temp_photos:
                    cleanup_files(temp_photos)
                return
            # Не удалось скачать
            await safe_edit_status("❌ Не удалось скачать контент")
            return
        # === TIKTOK ФОТО ===
        elif platform == 'tiktok' and '/photo/' in url.lower():
            photos, description = await download_tiktok_photos(url)
            await safe_delete_status()
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
        # temp_file = await download_video(url, user_quality, platform) # Закомментировано или удалено
        temp_file, error_msg = await download_video(url, user_quality, platform) # Теперь возвращает (file_path, error_msg)
        # Если первая попытка не удалась из-за аутентификации и это YouTube
        if error_msg == "auth_required" and platform == 'youtube':
            logger.info("🔄 Переключаюсь на Playwright для YouTube...")
            temp_file = await download_youtube_with_playwright(url, user_quality)
            if temp_file:
                error_msg = None # Сбросим ошибку, если Playwright сработал
            else:
                error_msg = "❌ Не удалось скачать через Playwright"

        if not temp_file or not os.path.exists(temp_file):
            await safe_edit_status(f"❌ Не удалось скачать видео: {error_msg or 'Неизвестная ошибка'}")
            return

        await safe_edit_status("📤 Отправляю...")
        await send_video_or_message(message.chat.id, temp_file)
        await safe_delete_status()
        cleanup_file(temp_file)
    except Exception as e:
        error_msg = f"❌ Ошибка: {str(e)}"
        logger.error(error_msg)
        await safe_edit_status(error_msg)
    finally:
        if temp_file:
            cleanup_file(temp_file)
        if temp_photos:
            cleanup_files(temp_photos)
# === 🚀 ЗАПУСК ===
async def main():
    logger.info("🚀 Запуск бота...")
    # --- НОВОЕ ---
    # Инициализируем Playwright для Instagram
    await init_instagram_playwright()
    # Инициализируем Playwright для YouTube
    await init_youtube_playwright()
    # --- /НОВОЕ ---
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
            # --- НОВОЕ ---
            # Закрываем браузер Playwright при завершении
            if IG_BROWSER:
                logger.info("🛑 Закрываю браузер Instagram Playwright...")
                await IG_BROWSER.close()
            # Закрываем браузер YouTube Playwright
            if YT_BROWSER:
                logger.info("🛑 Закрываю браузер YouTube Playwright...")
                await YT_BROWSER.close()
            # --- /НОВОЕ ---
            await bot.session.close()
    else:
        logger.info("🔄 Запускаю в рэжиме long polling")
        await dp.start_polling(bot, skip_updates=True) 
if __name__ == "__main__":
    asyncio.run(main())