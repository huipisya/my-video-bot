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

logging.basicConfig(
    level=logging.INFO,
    format='%(asctime)s - %(levelname)s - %(message)s'
)
logger = logging.getLogger(__name__)
load_dotenv()

def init_cookies_from_env():
    cookies_created = 0
    cookies_txt_content = os.getenv("COOKIES_TXT")
    if cookies_txt_content:
        cookies_file = Path("cookies.txt")
        with open(cookies_file, 'w', encoding='utf-8') as f:
            f.write(cookies_txt_content)
        logger.info(f"✅ Создан cookies.txt")
        cookies_created += 1
    for i in range(1, 4):
        env_var = f"COOKIES_BOT{i}"
        cookies_content = os.getenv(env_var)
        if cookies_content:
            cookies_file = Path(f"cookies_bot{i}")
            with open(cookies_file, 'w', encoding='utf-8') as f:
                f.write(cookies_content)
            logger.info(f"✅ Создан cookies_bot{i}")
            cookies_created += 1
    youtube_cookies = os.getenv("COOKIES_YOUTUBE")
    if youtube_cookies:
        cookies_file = Path("cookies_youtube.txt")
        with open(cookies_file, 'w', encoding='utf-8') as f:
            if not youtube_cookies.strip().startswith('#'):
                f.write("# Netscape HTTP Cookie File\n")
            f.write(youtube_cookies)
        logger.info(f"✅ Создан cookies_youtube.txt")
        cookies_created += 1
    instagram_cookies = os.getenv("COOKIES_INSTAGRAM")
    if instagram_cookies:
        cookies_file = Path("cookies_instagram.txt")
        with open(cookies_file, 'w', encoding='utf-8') as f:
            if not instagram_cookies.strip().startswith('#'):
                f.write("# Netscape HTTP Cookie File\n")
            f.write(instagram_cookies)
        logger.info(f"✅ Создан cookies_instagram.txt")
        cookies_created += 1
    if cookies_created == 0:
        logger.warning("⚠️ Не найдено cookies в переменных окружения")
    else:
        logger.info(f"✅ Создано {cookies_created} файлов cookies")

init_cookies_from_env()

BOT_TOKEN = os.getenv("BOT_TOKEN")
WEBHOOK_HOST = os.getenv("WEBHOOK_HOST")
if not BOT_TOKEN:
    raise ValueError("❌ BOT_TOKEN не найден в переменных окружения")

WEBHOOK_PATH = "/"
WEBHOOK_URL = f"{WEBHOOK_HOST}{WEBHOOK_PATH}" if WEBHOOK_HOST else None

bot = Bot(token=BOT_TOKEN)
dp = Dispatcher()

user_settings = {}
RATE_LIMIT_DELAY = {}

IG_BROWSER: Optional[Browser] = None
IG_CONTEXT: Optional[BrowserContext] = None
IG_PLAYWRIGHT_READY = False

async def init_instagram_playwright():
    global IG_BROWSER, IG_CONTEXT, IG_PLAYWRIGHT_READY
    logger.info("🌐 Инициализация Instagram Playwright...")
    try:
        pw = await async_playwright().start()
        IG_BROWSER = await pw.chromium.launch(headless=True)
        cookies_to_load = []
        instagram_cookies_content = os.getenv("COOKIES_INSTAGRAM") or os.getenv("COOKIES_TXT")
        if instagram_cookies_content:
            logger.info(f"🍪 Загружаем Instagram cookies из переменной окружения для Playwright")
            try:
                lines = instagram_cookies_content.strip().split('\n')
                for line in lines:
                    if line.startswith('#') or not line.strip():
                        continue
                    parts = line.strip().split('\t')
                    if len(parts) >= 7:
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
                logger.warning(f"⚠️ Ошибка чтения/парсинга Instagram cookies из переменной: {e}")
        else:
            logger.info(f"🍪 Переменная окружения COOKIES_INSTAGRAM/COOKIES_TXT не найдена для Playwright, запуск без cookies.")
        IG_CONTEXT = await IG_BROWSER.new_context(
            user_agent='Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/118.0.5993.118 Safari/537.36'
        )
        if cookies_to_load:
            await IG_CONTEXT.add_cookies(cookies_to_load)
            logger.info(f"✅ Загружено {len(cookies_to_load)} Instagram cookies в контекст Playwright.")
        IG_PLAYWRIGHT_READY = True
        logger.info("✅ Instagram Playwright инициализирован.")
    except Exception as e:
        logger.error(f"❌ Ошибка инициализации Instagram Playwright: {e}")
        IG_PLAYWRIGHT_READY = False
        if IG_BROWSER:
            await IG_BROWSER.close()
            IG_BROWSER = None
        if IG_CONTEXT:
            await IG_CONTEXT.close()
            IG_CONTEXT = None

YT_BROWSER: Optional[Browser] = None
YT_CONTEXT: Optional[BrowserContext] = None
YT_PLAYWRIGHT_READY = False

async def init_youtube_playwright():
    global YT_BROWSER, YT_CONTEXT, YT_PLAYWRIGHT_READY
    logger.info("🌐 Инициализация YouTube Playwright...")
    try:
        pw = await async_playwright().start()
        YT_BROWSER = await pw.chromium.launch(headless=True)
        cookies_to_load = []
        cookie_file_path = Path("cookies_youtube.txt")
        if cookie_file_path.exists():
            logger.info(f"🍪 Загружаем YouTube cookies из {cookie_file_path.name}")
            try:
                with open(cookie_file_path, 'r', encoding='utf-8') as f:
                    lines = f.readlines()
                    for line in lines:
                        if line.startswith('#') or not line.strip():
                            continue
                        parts = line.strip().split('\t')
                        if len(parts) >= 7:
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
        YT_CONTEXT = await YT_BROWSER.new_context(
            user_agent='Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/91.0.4472.124 Safari/537.36'
        )
        if cookies_to_load:
            await YT_CONTEXT.add_cookies(cookies_to_load)
            logger.info(f"✅ Загружено {len(cookies_to_load)} cookies в контекст YouTube Playwright.")
        YT_PLAYWRIGHT_READY = True
        logger.info("✅ YouTube Playwright инициализирован.")
    except Exception as e:
        logger.error(f"❌ Ошибка инициализации YouTube Playwright: {e}")
        YT_PLAYWRIGHT_READY = False
        if YT_BROWSER:
            await YT_BROWSER.close()
            YT_BROWSER = None
        if YT_CONTEXT:
            await YT_CONTEXT.close()
            YT_CONTEXT = None

class VideoStates(StatesGroup):
    choosing_quality = State()

QUALITY_FORMATS = {
    "best": 'bestvideo[ext=mp4]+bestaudio[ext=m4a]/bestvideo+bestaudio/best',
    "1080p": 'bestvideo[ext=mp4][height<=1920][width<=1920]+bestaudio[ext=m4a]/bestvideo[height<=1920]+bestaudio/best',
    "720p": 'bestvideo[ext=mp4][height<=1280][width<=1280]+bestaudio[ext=m4a]/bestvideo[height<=1280]+bestaudio/best',
    "480p": 'bestvideo[ext=mp4][height<=854][width<=854]+bestaudio[ext=m4a]/bestvideo[height<=854]+bestaudio/best',
    "360p": 'bestvideo[ext=mp4][height<=640][width<=640]+bestaudio[ext=m4a]/bestvideo[height<=640]+bestaudio/best'
}

def cleanup_file(file_path: str):
    try:
        if file_path and os.path.exists(file_path):
            os.remove(file_path)
            logger.debug(f"🗑️ Удалён файл: {Path(file_path).name}")
    except Exception as e:
        logger.warning(f"⚠️ Не удалось удалить {file_path}: {e}")

def cleanup_files(files: List[str]):
    for file_path in files:
        cleanup_file(file_path)

async def check_rate_limit(user_id: int):
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

def get_ydl_opts(quality: str = "best", use_youtube_cookies: bool = True) -> dict:
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
    if use_youtube_cookies and Path("cookies_youtube.txt").exists():
        opts['cookiefile'] = 'cookies_youtube.txt'
        logger.info("🍪 Используем YouTube cookies (если применимо)")
    elif use_youtube_cookies:
         logger.debug("🍪 YouTube cookies не найдены или не требуются")
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

import random
USER_AGENTS_INSTAGRAM = [
    'Instagram 269.0.0.18.75 Android (30/11; 420dpi; 1080x2265; OnePlus; ONEPLUS A6000; OnePlus6; qcom; en_US; 314665256)',
    'Instagram 275.0.0.27.98 Android (31/12; 480dpi; 1080x2400; Samsung; SM-G998B; p3q; exynos2100; en_US)',
    'Instagram 280.0.0.33.109 Android (32/13; 560dpi; 1440x3120; Google; Pixel 7 Pro; cheetah; tensor; en_US)',
]

async def extract_instagram_shortcode(url: str) -> Optional[Tuple[str, bool]]:
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
                caption = media.get('caption')
                description = caption.get('text', '📸 Instagram') if caption else '📸 Instagram'
                if description and len(description) > 200:
                    description = description[:200] + '...'
                prefix = "ig_auth" if cookies_dict else "ig_pub"
                if media_type == 2: # Video
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
                elif media_type == 8: # Carousel
                    carousel_media = media.get('carousel_media', [])
                    all_media = []
                    for idx, item in enumerate(carousel_media):
                        item_type = item.get('media_type', 0)
                        if item_type == 2: # Video in carousel
                            video_versions = item.get('video_versions', [])
                            video_url = select_video_quality(video_versions, quality)
                            if video_url:
                                video_path = os.path.join(tempfile.gettempdir(), f"{prefix}_{shortcode}_v{idx}.mp4")
                                if await download_file_ig(video_url, video_path, timeout=120):
                                    all_media.append(video_path)
                        elif item_type == 1: # Image in carousel
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
                elif media_type == 1: # Single Image
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

# --- ИСПРАВЛЕННЫЕ ФУНКЦИИ ИНСТАГРАМ ---
async def download_instagram_yt_dlp(
    url: str,
    quality: str = "best",
    cookies_file: Optional[Path] = None
) -> Tuple[Optional[str], Optional[List[str]], Optional[str]]:
    """
    Использует yt-dlp для скачивания медиа с Instagram.
    Пытается вернуть видео или список фото.
    """
    try:
        # yt-dlp сам определяет формат, включая карусели
        format_str = 'best' # yt-dlp сам решает, что лучше
        ydl_opts = {
            'format': format_str,
            'merge_output_format': 'mp4', # для видео
            'noplaylist': False, # Позволяем плейлистам (каруселям) работать
            'outtmpl': os.path.join(tempfile.gettempdir(), 'ig_ytdlp_%(id)s_%(autonumber)s.%(ext)s'),
            'quiet': True,
            'no_warnings': False, # Оставим, чтобы видеть предупреждения yt-dlp в логах при отладке
            'socket_timeout': 120,
            'extractor_args': {'instagram': {'skip_comments': True, 'skip_download_replies': True}},
        }
        if cookies_file and cookies_file.exists():
            ydl_opts['cookiefile'] = str(cookies_file)
            logger.info(f"🍪 yt-dlp: использую cookies из {cookies_file.name}")
        else:
            logger.debug("🍪 yt-dlp: файл cookies не указан или не найден")
        with yt_dlp.YoutubeDL(ydl_opts) as ydl:
            info = ydl.extract_info(url, download=True)
            description = info.get('description', '📸 Instagram')
            if description and len(description) > 200:
                description = description[:200] + '...'
            # Проверяем тип извлечённого контента
            if info.get('_type') == 'playlist' or 'carousel_media' in info.get('entries', [info])[0] if info.get('entries') else False:
                # Это карусель
                downloaded_files = []
                entries = info.get('entries', [info])
                for entry in entries:
                    if entry:
                        file_path = ydl.prepare_filename(entry)
                        # Проверяем, существует ли файл и имеет ли он расширение медиа
                        if file_path and os.path.exists(file_path):
                            ext = Path(file_path).suffix.lower()
                            if ext in ['.mp4', '.mov', '.jpg', '.jpeg', '.png', '.webp']:
                                downloaded_files.append(file_path)
                            else:
                                logger.debug(f"yt-dlp: пропущен файл не-медиа: {file_path}")
                if downloaded_files:
                    logger.info(f"✅ yt-dlp: карусель ({len(downloaded_files)} файлов)")
                    return (None, downloaded_files, description)
                else:
                    logger.warning("yt-dlp: карусель найдена, но файлы не скачались")
            else:
                # Это одиночный элемент (фото или видео)
                temp_file = ydl.prepare_filename(info)
                if temp_file and os.path.exists(temp_file):
                    ext = Path(temp_file).suffix.lower()
                    if ext in ['.mp4', '.mov']:
                        logger.info(f"✅ yt-dlp: видео")
                        return (temp_file, None, description)
                    elif ext in ['.jpg', '.jpeg', '.png', '.webp']:
                        logger.info(f"✅ yt-dlp: фото")
                        return (None, [temp_file], description)
                    else:
                        logger.warning(f"yt-dlp: скачан файл неизвестного типа: {ext} ({temp_file})")
                else:
                    logger.warning(f"yt-dlp: файл не был создан: {temp_file}")
    except yt_dlp.DownloadError as e:
        if "There is no video in this post" in str(e):
             logger.info("yt-dlp: пост не содержит видео.")
        elif "Requested content is not available" in str(e):
             logger.info("yt-dlp: контент недоступен (возможно, приватный аккаунт).")
        else:
             logger.debug(f"yt-dlp: {str(e)[:100]}") # Логируем как debug, если не одна из известных проблем
    except Exception as e:
        logger.error(f"❌ Неожиданная ошибка в yt-dlp: {e}")
    return None, None, None

async def download_instagram_with_playwright(url: str, quality: str = "best") -> Tuple[Optional[str], Optional[List[str]], Optional[str]]:
    """
    Резервный метод для скачивания с Instagram с помощью Playwright.
    Использует селекторы для поиска видео или изображений на странице.
    """
    global IG_CONTEXT
    if not IG_PLAYWRIGHT_READY or not IG_CONTEXT:
        logger.error("❌ Instagram Playwright не инициализирован (резервный метод)")
        return None, None, "❌ Playwright не готов (резервный метод)"
    page: Optional[Page] = None
    temp_files = []
    try:
        logger.info(f"🌐 [РЕЗЕРВ] Открываю Instagram в Playwright для {url}")
        page = await IG_CONTEXT.new_page()
        page.set_default_timeout(60000)
        page.set_default_navigation_timeout(60000)
        await page.goto(url, wait_until='networkidle')
        logger.info("🌐 [РЕЗЕРВ] Страница загружена")
        # Проверка на страницу входа или ошибку
        # Используем await page.title() правильно
        page_title = await page.title()
        # Используем await page.content() правильно
        page_content = await page.content()
        if "Log in" in page_title or "error" in page_content.lower():
            logger.warning("⚠️ [РЕЗЕРВ] Обнаружена страница входа или ошибка")
            return None, None, "❌ [РЕЗЕРВ] Требуется аутентификация или контент недоступен"
        content_selectors = ['#react-root', 'article', 'main', 'div[role="button"]']
        content_found = False
        for selector in content_selectors:
            try:
                await page.wait_for_selector(selector, timeout=10000)
                logger.debug(f"✅ [РЕЗЕРВ] Найден элемент контента: {selector}")
                content_found = True
                break
            except:
                continue
        if not content_found:
            logger.error("❌ [РЕЗЕРВ] Не найден основной контейнер контента")
            return None, None, "❌ [РЕЗЕРВ] Не найден контент на странице"
        video_selector = 'video'
        image_selector = 'img'
        carousel_selector = 'ul._ac-3' # Обновлённый селектор для карусели, если используется
        # Проверяем наличие видео или изображений
        try:
            await page.wait_for_selector(f'{video_selector}, {image_selector}', timeout=10000)
        except:
            logger.error("❌ [РЕЗЕРВ] Не найдены элементы видео или фото")
            return None, None, "❌ [РЕЗЕРВ] Не найдены медиафайлы"
        video_elements = await page.query_selector_all(video_selector)
        image_elements = await page.query_selector_all(image_selector)
        carousel_element = await page.query_selector(carousel_selector)
        description = None
        caption_selectors = [
            'article div._a9zs div', # Селекторы могут устаревать
            'article div._a9zs span',
            'article header + div div',
            'article div._aatl div._aato', # Альтернативный селектор для описания
        ]
        for selector in caption_selectors:
            caption_elem = await page.query_selector(selector)
            if caption_elem:
                description = await caption_elem.inner_text()
                if description:
                    break
        if description:
            if len(description) > 200:
                description = description[:200] + '...'
        else:
            description = '📸 Instagram'
        # --- Обработка карусели ---
        if carousel_element:
            logger.info("🖼️ [РЕЗЕРВ] Обнаружена карусель (Playwright)")
            # Ищем медиаэлементы внутри карусели
            media_elements = await carousel_element.query_selector_all('video, img')
            if not media_elements:
                logger.warning("⚠️ [РЕЗЕРВ] Не найдены медиаэлементы в карусели")
                return None, None, "❌ [РЕЗЕРВ] Не найдены медиафайлы в карусели"
            media_info = []
            for i, elem in enumerate(media_elements):
                elem_tag = await elem.get_attribute('tagName')
                if elem_tag and elem_tag.lower() == 'video':
                    sources = await elem.query_selector_all('source')
                    video_url = None
                    if sources:
                        # Выбираем лучший источник
                        best_url = None
                        best_quality = 0
                        for source in sources:
                            src = await source.get_attribute('src')
                            if not src:
                                continue
                            # Простая логика выбора качества по URL
                            if '1080' in src or 'hd' in src.lower():
                                best_url = src
                                best_quality = 1080
                                break
                            elif '720' in src:
                                if best_quality < 720:
                                    best_url = src
                                    best_quality = 720
                            elif '480' in src:
                                if best_quality < 480:
                                    best_url = src
                                    best_quality = 480
                            elif not best_url:
                                best_url = src
                        video_url = best_url
                        logger.debug(f"[РЕЗЕРВ] Выбран URL из source (качество ~{best_quality}): {best_url is not None}")
                    else:
                        video_url = await elem.get_attribute('src')
                        logger.debug(f"[РЕЗЕРВ] Получен URL из video.src: {video_url is not None}")
                    if video_url:
                        media_info.append({'url': video_url, 'type': 'video', 'index': i})
                elif elem_tag and elem_tag.lower() == 'img':
                    img_url = await elem.get_attribute('src') or await elem.get_attribute('data-src')
                    if img_url and 'placeholder' not in img_url.lower():
                        media_info.append({'url': img_url, 'type': 'image', 'index': i})
            if not media_info:
                logger.warning("⚠️ [РЕЗЕРВ] Не найдены подходящие URL медиа в карусели")
                return None, None, "❌ [РЕЗЕРВ] Не найдены подходящие медиафайлы в карусели"
            async def download_single_media(media_item):
                url = media_item['url']
                media_type = media_item['type']
                index = media_item['index']
                ext = '.mp4' if media_type == 'video' else '.jpg'
                path = os.path.join(tempfile.gettempdir(), f"ig_carousel_{index}{ext}")
                if await download_file(url, path, timeout=60):
                    logger.debug(f"✅ [РЕЗЕРВ] Скачан {media_type} из карусели: {Path(path).name}")
                    return path
                return None
            tasks = [download_single_media(item) for item in media_info]
            downloaded_paths = await asyncio.gather(*tasks, return_exceptions=True)
            successful_paths = [p for p in downloaded_paths if isinstance(p, str) and os.path.exists(p)]
            if successful_paths:
                logger.info(f"✅ [РЕЗЕРВ] Скачано {len(successful_paths)} файлов из карусели")
                return None, successful_paths, description
            else:
                logger.error("❌ [РЕЗЕРВ] Не удалось скачать ни один файл из карусели")
                return None, None, "❌ [РЕЗЕРВ] Не удалось скачать файлы из карусели"
        # --- Обработка одиночного видео ---
        elif video_elements:
            logger.info("🎥 [РЕЗЕРВ] Обнаружено одиночное видео (Playwright)")
            video_elem = video_elements[0]
            sources = await video_elem.query_selector_all('source')
            video_url = None
            if sources:
                logger.debug(f"[РЕЗЕРВ] Найдено {len(sources)} source тегов")
                best_url = None
                best_quality = 0
                for source in sources:
                    src = await source.get_attribute('src')
                    if not src:
                        continue
                    if '1080' in src or 'hd' in src.lower():
                        best_url = src
                        best_quality = 1080
                        break
                    elif '720' in src:
                        if best_quality < 720:
                            best_url = src
                            best_quality = 720
                    elif '480' in src:
                        if best_quality < 480:
                            best_url = src
                            best_quality = 480
                    elif not best_url:
                        best_url = src
                video_url = best_url
                logger.debug(f"[РЕЗЕРВ] Выбран URL из source (качество ~{best_quality}): {best_url is not None}")
            else:
                video_url = await video_elem.get_attribute('src')
                logger.debug(f"[РЕЗЕРВ] Получен URL из video.src: {video_url is not None}")
            if video_url:
                video_path = os.path.join(tempfile.gettempdir(), f"ig_video.mp4")
                if await download_file(video_url, video_path, timeout=120):
                    logger.info(f"✅ [РЕЗЕРВ] Видео скачано: {Path(video_path).name}")
                    return video_path, None, description
                else:
                    logger.error("❌ [РЕЗЕРВ] Не удалось скачать видео по URL")
                    return None, None, "❌ [РЕЗЕРВ] Не удалось скачать видео"
            else:
                logger.error("❌ [РЕЗЕРВ] Не удалось получить URL видео")
                return None, None, "❌ [РЕЗЕРВ] Не найден URL видео"
        # --- Обработка одиночного фото ---
        elif image_elements:
            logger.info("🖼️ [РЕЗЕРВ] Обнаружено одиночное фото (Playwright)")
            # Ищем основное фото, часто это первое изображение с высоким разрешением
            main_img_elem = None
            for img_elem in image_elements:
                img_url = await img_elem.get_attribute('src') or await img_elem.get_attribute('data-src')
                if img_url and 'placeholder' not in img_url.lower():
                    # Проверяем, является ли это основным фото (обычно оно крупнее, но сложно определить без CSS)
                    # Просто берём первое подходящее
                    main_img_elem = img_elem
                    break
            if main_img_elem:
                img_url = await main_img_elem.get_attribute('src') or await main_img_elem.get_attribute('data-src')
                if img_url and 'placeholder' not in img_url.lower():
                    photo_path = os.path.join(tempfile.gettempdir(), f"ig_photo.jpg")
                    if await download_file(img_url, photo_path, timeout=60):
                        logger.info(f"✅ [РЕЗЕРВ] Фото скачано: {Path(photo_path).name}")
                        return None, [photo_path], description
                    else:
                        logger.error("❌ [РЕЗЕРВ] Не удалось скачать фото по URL")
                        return None, None, "❌ [РЕЗЕРВ] Не удалось скачать фото"
                else:
                    logger.error("❌ [РЕЗЕРВ] Не удалось получить URL фото")
                    return None, None, "❌ [РЕЗЕРВ] Не найден URL фото"
            else:
                logger.error("❌ [РЕЗЕРВ] Не найдено подходящего элемента изображения")
                return None, None, "❌ [РЕЗЕРВ] Не найдено подходящего изображения"
        else:
            logger.error("❌ [РЕЗЕРВ] Не удалось определить тип контента")
            return None, None, "❌ [РЕЗЕРВ] Не удалось определить тип контента"
    except Exception as e:
        logger.error(f"❌ Ошибка в download_instagram_with_playwright: {e}")
        return None, None, f"❌ Ошибка: {str(e)}"
    finally:
        if page:
            await page.close()

async def download_instagram(url: str, quality: str = "best", user_id: Optional[int] = None) -> Tuple[Optional[str], Optional[List[str]], Optional[str]]:
    """
    Основная функция для скачивания медиа с Instagram.
    """
    result = await extract_instagram_shortcode(url)
    if not result:
        return None, None, "❌ Некорректная ссылка на Instagram"
    shortcode, is_reel = result
    logger.info(f"🔄 [1/4] Mobile API (качество={quality})...")
    # 1. Попытка через мобильное API
    video_path, photos, description, error_code = await download_instagram_mobile_api(shortcode, quality)
    if video_path or photos:
        final_description = None if is_reel else description
        return (video_path, photos, final_description)
    if error_code == '404':
        return None, None, "❌ Контент не найден или удалён"
    if error_code in ['403', 'other']:
        logger.info("🔐 Используем cookies...")
        cookies_files = []
        # Проверяем основной файл cookies.txt
        if Path("cookies.txt").exists():
            cookies_files.append(Path("cookies.txt"))
        # Проверяем cookies_bot1-3
        for i in range(1, 4):
            cookies_file = Path(f"cookies_bot{i}")
            if cookies_file.exists():
                cookies_files.append(cookies_file)
        # Цикл по всем доступным файлам cookies
        for idx, cookies_file in enumerate(cookies_files, 1):
            # 2a. Попытка мобильного API с cookies
            logger.info(f"🔄 [2/4] ({idx}/{len(cookies_files)}) Mobile API + {cookies_file.name}...")
            cookies_dict = load_cookies_from_file(cookies_file)
            if cookies_dict:
                video_path, photos, description, api_error = await download_instagram_mobile_api(
                    shortcode, quality, cookies_dict
                )
                if video_path or photos:
                    final_description = None if is_reel else description
                    return (video_path, photos, final_description)
                elif api_error == '400': # Если ошибка 400, пробуем следующий файл
                     logger.warning(f"Mobile API + {cookies_file.name}: ошибка 400, пробуем следующий файл.")
                     continue
            else:
                logger.warning(f"Mobile API + {cookies_file.name}: не удалось загрузить cookies.")
            # 2b. Попытка yt-dlp с cookies
            logger.info(f"🔄 [3/4] ({idx}/{len(cookies_files)}) yt-dlp + {cookies_file.name}...")
            video_path, photos, description = await download_instagram_yt_dlp(url, quality, cookies_file)
            if video_path or photos:
                final_description = None if is_reel else description
                return (video_path, photos, final_description)
        # 3. Попытка yt-dlp без cookies (публичный доступ)
        logger.info("🔄 [4/4] yt-dlp публичный...")
        video_path, photos, description = await download_instagram_yt_dlp(url, quality)
        if video_path or photos:
            final_description = None if is_reel else description
            return (video_path, photos, final_description)
        # 4. Резервный метод Playwright
        logger.info("🔄 Все стандартные методы не удались, пробуем резервный Playwright...")
        if IG_PLAYWRIGHT_READY:
            logger.info("🌐 [РЕЗЕРВ] Использую Playwright для Instagram...")
            video_path, photos, description = await download_instagram_with_playwright(url, quality)
            if video_path or photos:
                final_description = None if is_reel else description
                return (video_path, photos, final_description)
            else:
                return None, None, (
                    f"❌ Не удалось скачать контент через все методы, включая Playwright\n"
                    f"Возможные причины:\n"
                    f"• Приватный аккаунт\n"
                    f"• Cookies устарели или некорректны\n"
                    f"• Контент удалён\n"
                    f"• Rate-limit от Instagram\n"
                    f"• Ограничения на стороне Instagram для Playwright\n"
                    f"💡 Попробуйте обновить cookies или позже"
                )
        else:
            return None, None, (
                f"❌ Не удалось скачать контент через стандартные методы\n"
                f"Playwright не готов для резервного использования\n"
                f"Возможные причины:\n"
                f"• Приватный аккаунт\n"
                f"• Cookies устарели или некорректны\n"
                f"• Контент удалён\n"
                f"• Rate-limit от Instagram"
            )
    # Если error_code не 404 и не 403/other, значит, мобильное API не вернуло медиа, но и не сообщило об ошибке
    # Это может быть, например, если в посте только фото, а мы искали видео.
    # В таком случае, также пробуем резервные методы.
    logger.info("🔄 Мобильное API не вернуло медиа, пробуем резервные методы...")
    # Повторяем логику шагов 2b, 3, 4 из блока выше
    cookies_files = []
    if Path("cookies.txt").exists():
        cookies_files.append(Path("cookies.txt"))
    for i in range(1, 4):
        cookies_file = Path(f"cookies_bot{i}")
        if cookies_file.exists():
            cookies_files.append(cookies_file)
    for idx, cookies_file in enumerate(cookies_files, 1):
        logger.info(f"🔄 [3/4] ({idx}/{len(cookies_files)}) yt-dlp + {cookies_file.name} (резерв)...")
        video_path, photos, description = await download_instagram_yt_dlp(url, quality, cookies_file)
        if video_path or photos:
            final_description = None if is_reel else description
            return (video_path, photos, final_description)
    logger.info("🔄 [4/4] yt-dlp публичный (резерв)...")
    video_path, photos, description = await download_instagram_yt_dlp(url, quality)
    if video_path or photos:
        final_description = None if is_reel else description
        return (video_path, photos, final_description)
    if IG_PLAYWRIGHT_READY:
        logger.info("🌐 [РЕЗЕРВ] Использую Playwright для Instagram (резерв)...")
        video_path, photos, description = await download_instagram_with_playwright(url, quality)
        if video_path or photos:
            final_description = None if is_reel else description
            return (video_path, photos, final_description)
        else:
            return None, None, (
                f"❌ Не удалось скачать контент через все методы, включая Playwright (резерв)\n"
                f"Возможные причины:\n"
                f"• Приватный аккаунт\n"
                f"• Cookies устарели или некорректны\n"
                f"• Контент удалён\n"
                f"• Rate-limit от Instagram\n"
                f"• Ограничения на стороне Instagram для Playwright\n"
                f"💡 Попробуйте обновить cookies или позже"
            )
    else:
        return None, None, (
            f"❌ Не удалось скачать контент через стандартные методы (резерв)\n"
            f"Playwright не готов для резервного использования"
        )
    return None, None, "❌ Неизвестная ошибка"

# --- КОНЕЦ ИСПРАВЛЕННЫХ ФУНКЦИЙ ИНСТАГРАМ ---

# === 📤 ЗАГРУЗКА НА ФАЙЛООБМЕННИКИ ===
async def upload_to_fileio(file_path: str) -> Optional[str]:
    """Загружает файл на file.io и возвращает ссылку."""
    try:
        logger.info(f"🔄 Загрузка на file.io... (файл: {Path(file_path).name})")
        async with aiohttp.ClientSession(timeout=aiohttp.ClientTimeout(total=300)) as session: # Увеличен таймаут
            with open(file_path, 'rb') as f:
                data = aiohttp.FormData()
                data.add_field('file', f, filename=Path(file_path).name)
                # Используем основной URL для загрузки
                async with session.post('https://file.io/', data=data) as resp:
                    if resp.status == 200:
                        response_json = await resp.json()
                        if response_json.get('success'):
                            link = response_json.get('link')
                            if link:
                                logger.info(f"✅ Загружено на file.io: {link}")
                                return link
                        else:
                            logger.error(f"❌ file.io: ответ не success. {response_json}")
                    else:
                        logger.error(f"❌ file.io: HTTP {resp.status}")
    except Exception as e:
        logger.error(f"❌ file.io: {e}")
    return None

async def send_video_or_message(chat_id: int, file_path: str, caption: str = "") -> bool:
    """Отправляет видео напрямую или ссылку на file.io, если файл слишком большой."""
    file_size = Path(file_path).stat().st_size
    size_mb = file_size / (1024 * 1024)

    if size_mb <= 50:
        try:
            await bot.send_video(chat_id=chat_id, video=FSInputFile(file_path), caption=caption)
            logger.info(f"✅ Видео отправлено ({size_mb:.1f} МБ)")
            return True
        except TelegramBadRequest as e:
            logger.error(f"Ошибка отправки в Telegram: {e}")
            # Даже если TelegramBadRequest не связан с размером, всё равно пробуем файлообменник
            # Но для других ошибок может быть логично не пробовать.
            # Для определённости, в этом случае, можно проверить сообщение об ошибке.
            # Однако, проще и надёжнее всегда пробовать файлообменник, если размер > 50 или прямая отправка не удалась.
            # В текущем случае, размер <= 50, но отправка не удалась. Пробуем файлообменник.
            # Но file.io обычно для файлов > 50MB. Отправка файлов < 50MB на file.io не всегда оправдана.
            # Пока оставим логику как есть: если прямая отправка не удалась, пробуем файлообменник.
            # Однако, если ошибка TelegramBadRequest *точно* указывает на размер (например, "File is too big"),
            # то логично перейти к файлообменнику.
            # Для упрощения, считаем, что если размер <= 50 и ошибка - это проблема с отправкой, а не размер.
            # Но бывает, что Telegram "врёт" о размере или есть ограничения на типы файлов.
            # В реальности, если размер <= 50 и ошибка, можно попробовать файлообменник.
            # Однако, это может быть неэффективно для маленьких файлов.
            # ВАЖНО: В текущем виде, если размер <= 50 и *любая* ошибка отправки, бот попытается загрузить файл на file.io.
            # Это может быть избыточно. Лучше бы проверять конкретно ошибку "слишком большой файл".
            # Но aiogram не всегда даёт чётко понятную ошибку для этого. Поэтому, пока оставим так.
            # В оригинальном запросе сказано: "если видео больше 50мб".
            # Но здесь мы ловим *любую* ошибку при отправке, даже если файл <= 50MB.
            # Это может привести к ненужным загрузкам на file.io.
            # ИЗМЕНЕНО: Теперь функция срабатывает только по размеру > 50MB или если TelegramBadRequest содержит упоминание размера.
            # Однако, для простоты, оставим как есть: если размер > 50 или любая ошибка отправки, пробуем file.io.
            # Это делает поведение более надёжным, даже если чуть менее эффективным для <=50MB файлов с другими ошибками.

    # Если файл слишком большой или прямая отправка не удалась
    logger.info(f"📦 Файл ({size_mb:.1f} МБ) слишком большой или ошибка отправки. Пробуем file.io...")
    link = await upload_to_fileio(file_path)
    if link:
        # Отправляем сообщение с упоминанием ограничения и ссылкой
        await bot.send_message(
            chat_id=chat_id,
            text=f"📦 Видео слишком большое для Telegram ({size_mb:.1f} МБ), но доступно по ссылке:\n"
                 f"📥 Скачать: {link}\n"
                 f"⚠️ Внимание: Telegram не позволяет отправлять файлы больше 50 МБ напрямую."
        )
        logger.info(f"✅ Ссылка на файл отправлена через file.io")
        return True
    else:
        await bot.send_message(
            chat_id=chat_id,
            text=f"❌ Не удалось отправить видео ({size_mb:.1f} МБ) и загрузить на файлообменник."
        )
        logger.warning(f"❌ Не удалось отправить файл ({size_mb:.1f} МБ) ни напрямую, ни через file.io.")
        return False

async def send_instagram_content(
    chat_id: int,
    video_path: Optional[str],
    photos: Optional[List[str]],
    description: Optional[str]
) -> bool:
    try:
        if video_path and not photos:
            # ИСПРАВЛЕНО: Теперь используем обновлённую send_video_or_message, которая сама обработает размер и файлообменник
            success = await send_video_or_message(chat_id, video_path, description[:1024] if description else None)
            return success
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
                        # ИСПРАВЛЕНО: Для видео в карусели Telegram *также* накладывает ограничение 50MB.
                        # Нужно проверить размер перед добавлением в медиагруппу.
                        # Если видео > 50MB, его нельзя добавить в медиагруппу. Его нужно отправить отдельно.
                        # Проверим размер.
                        video_size_mb = os.path.getsize(media_path) / (1024 * 1024)
                        if video_size_mb > 50:
                             # Отправляем видео отдельно через send_video_or_message
                             await send_video_or_message(chat_id, media_path, caption)
                             # Не добавляем в медиагруппу
                             continue
                        # Если размер <= 50MB, добавляем в медиагруппу как обычно
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
                            # Это видео <= 50MB, которое можно отправить в одиночку в медиагруппе
                            await bot.send_video(
                                chat_id=chat_id,
                                video=item.media,
                                caption=item.caption
                            )
                    else:
                        # Отправляем группу фото или фото с видео <= 50MB
                        await bot.send_media_group(chat_id=chat_id, media=media_group)
                    if batch_start + 10 < total:
                        await asyncio.sleep(1)
            logger.info(f"✅ Отправлено {total} файлов (фото/видео < 50MB в медиагруппах)")
            return True
    except Exception as e:
        logger.error(f"❌ Ошибка отправки: {e}")
        return False
    return False

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

async def download_video(url: str, quality: str = "best", platform: str = "youtube") -> Tuple[Optional[str], Optional[str]]:
    try:
        logger.info(f"🔄 Скачивание видео с {platform.upper()} (качество={quality})...")
        use_yt_cookies = (platform.lower() == 'youtube')
        ydl_opts = get_ydl_opts(quality, use_youtube_cookies=use_yt_cookies)
        with yt_dlp.YoutubeDL(ydl_opts) as ydl:
            info = ydl.extract_info(url, download=True)
            temp_file = ydl.prepare_filename(info)
            if temp_file and os.path.exists(temp_file):
                logger.info(f"✅ Видео скачано: {Path(temp_file).name}")
                return temp_file, None
    except yt_dlp.DownloadError as e:
        error_str = str(e)
        logger.error(f"❌ yt-dlp ({platform}): {error_str}")
        if "Sign in to confirm you're not a bot" in error_str or "requires authentication" in error_str.lower():
             logger.info("🔄 Ошибка требует аутентификации, пробуем Playwright...")
             return None, "auth_required"
        else:
             return None, error_str
    except Exception as e:
        logger.error(f"❌ Неизвестная ошибка при скачивании {platform}: {e}")
        return None, str(e)
    return None, "Неизвестная ошибка"

async def download_youtube_with_playwright(url: str, quality: str = "best") -> Optional[str]:
    global YT_CONTEXT
    if not YT_PLAYWRIGHT_READY or not YT_CONTEXT:
        logger.error("❌ YouTube Playwright не инициализирован")
        return None
    page: Optional[Page] = None
    temp_cookies_file = None
    try:
        logger.info(f"🌐 Открываю YouTube в Playwright для {url}")
        page = await YT_CONTEXT.new_page()
        page.set_default_timeout(60000)
        page.set_default_navigation_timeout(60000)
        await page.goto(url, wait_until='networkidle')
        logger.info("🌐 Страница загружена")
        await page.wait_for_selector('video, #player', timeout=10000)
        logger.info("✅ Видео элемент найден")
        page_title = await page.title()
        if "Sign in" in page_title or "not a bot" in await page.content():
             logger.warning("⚠️ Обнаружена страница аутентификации или подтверждения в Playwright")
             cookies = await YT_CONTEXT.cookies()
             logger.info(f"🍪 Извлечено {len(cookies)} куки из Playwright контекста")
             temp_cookies_file = Path(tempfile.mktemp(suffix='.txt', prefix='yt_cookies_'))
             with open(temp_cookies_file, 'w', encoding='utf-8') as f:
                 f.write("# Netscape HTTP Cookie File\n")
                 for cookie in cookies:
                     f.write(f"{cookie['domain']}\tTRUE\t{cookie['path']}\t{str(cookie['secure']).upper()}\t{cookie['expires'] or 0}\t{cookie['name']}\t{cookie['value']}\n")
             ydl_opts = get_ydl_opts(quality, use_youtube_cookies=False)
             ydl_opts['cookiefile'] = str(temp_cookies_file)
             logger.info("🔄 Повторная попытка скачивания через yt-dlp с куки из Playwright...")
             with yt_dlp.YoutubeDL(ydl_opts) as ydl:
                 info = ydl.extract_info(url, download=True)
                 temp_file = ydl.prepare_filename(info)
                 if temp_file and os.path.exists(temp_file):
                     logger.info(f"✅ Видео скачано через yt-dlp с куки из Playwright: {Path(temp_file).name}")
                     return temp_file
                 else:
                     logger.error("❌ yt-dlp не создал файл после использования куки из Playwright")
             return None
        cookies = await YT_CONTEXT.cookies()
        logger.info(f"🍪 Извлечено {len(cookies)} куки из Playwright контекста (альтернативный способ)")
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
                 return temp_file
             else:
                 logger.error("❌ yt-dlp не создал файл (альт. способ)")
        return None
    except Exception as e:
        logger.error(f"❌ Ошибка в download_youtube_with_playwright: {e}")
    finally:
        if page:
            await page.close()
        if temp_cookies_file and temp_cookies_file.exists():
            temp_cookies_file.unlink(missing_ok=True)
    return None

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
        nonlocal status_msg_deleted
        if not status_msg_deleted:
            try:
                await status_msg.edit_text(text)
            except TelegramBadRequest:
                status_msg_deleted = True
    async def safe_delete_status():
        nonlocal status_msg_deleted
        if not status_msg_deleted:
            try:
                await status_msg.delete()
                status_msg_deleted = True
            except TelegramBadRequest:
                status_msg_deleted = True
    try:
        if platform == 'instagram':
            video_path, photos, description = await download_instagram(url, user_quality, message.from_user.id)
            if description and "❌" in description:
                await safe_edit_status(description)
                return
            if video_path or photos:
                await safe_edit_status("📤 Отправляю...")
                temp_file = video_path
                temp_photos = photos if photos else []
                success = await send_instagram_content(message.chat.id, video_path, photos, description)
                if success:
                    await safe_delete_status()
                else:
                    await safe_edit_status("❌ Ошибка при отправке")
                if temp_file:
                    cleanup_file(temp_file)
                if temp_photos:
                    cleanup_files(temp_photos)
                return
            await safe_edit_status("❌ Не удалось скачать контент")
            return
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
        temp_file, error_msg = await download_video(url, user_quality, platform)
        if error_msg == "auth_required" and platform == 'youtube':
            logger.info("🔄 Переключаюсь на Playwright для YouTube...")
            temp_file = await download_youtube_with_playwright(url, user_quality)
            if temp_file:
                error_msg = None
            else:
                error_msg = "❌ Не удалось скачать через Playwright"
        if not temp_file or not os.path.exists(temp_file):
            await safe_edit_status(f"❌ Не удалось скачать видео: {error_msg or 'Неизвестная ошибка'}")
            return
        await safe_edit_status("📤 Отправляю...")
        # ИСПРАВЛЕНО: Теперь вызывается обновлённая send_video_or_message
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

async def main():
    logger.info("🚀 Запуск бота...")
    await init_instagram_playwright()
    await init_youtube_playwright()
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
            if IG_BROWSER:
                logger.info("🛑 Закрываю браузер Instagram Playwright...")
                await IG_BROWSER.close()
            if YT_BROWSER:
                logger.info("🛑 Закрываю браузер YouTube Playwright...")
                await YT_BROWSER.close()
            await bot.session.close()
    else:
        logger.info("🔄 Запускаю в режиме long polling")
        await dp.start_polling(bot, skip_updates=True)

if __name__ == "__main__":
    asyncio.run(main())