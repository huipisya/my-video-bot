# bot.py - Luno Bot
import asyncio
import json
import logging
import os
import tempfile
import hashlib
from datetime import datetime, timedelta
from pathlib import Path
from typing import Optional, List, Tuple, Dict, Any

import aiohttp.web
import yt_dlp
from aiogram import Bot, Dispatcher, F
from aiogram.client.session.aiohttp import AiohttpSession
from aiogram.exceptions import TelegramBadRequest
from aiogram.filters import Command
from aiogram.fsm.context import FSMContext
from aiogram.fsm.state import StatesGroup, State
from aiogram.types import (
    Message,
    FSInputFile,
    InputMediaPhoto,
    ReplyKeyboardMarkup,
    KeyboardButton,
    InlineKeyboardMarkup,
    InlineKeyboardButton,
    CallbackQuery,
)

from dotenv import load_dotenv
from playwright.async_api import async_playwright, Browser, BrowserContext
import sys

sys.stdout.reconfigure(encoding='utf-8')
load_dotenv()

logging.basicConfig(level=logging.INFO, format='%(asctime)s - %(levelname)s - %(message)s')
logger = logging.getLogger(__name__)

# Глобальные переменные
BOT_TOKEN = os.getenv("BOT_TOKEN")
BOT_USERNAME = os.getenv("BOT_USERNAME", "VideoDL_All_bot")
WEBHOOK_HOST = os.getenv("WEBHOOK_HOST")
PORT = int(os.getenv("PORT", 8000))
ADMIN_USERNAME = "@somersbyewich"

YT_BROWSER: Optional[Browser] = None
YT_CONTEXT: Optional[BrowserContext] = None
YT_PLAYWRIGHT_READY = False

IG_BROWSER: Optional[Browser] = None
IG_CONTEXT: Optional[BrowserContext] = None
IG_PLAYWRIGHT_READY = False

bot: Optional[Bot] = None
dp = Dispatcher()

# Файлы для хранения данных
SETTINGS_FILE = 'user_settings.json'
USERS_FILE = 'users_data.json'
REFERRALS_FILE = 'referrals.json'

user_settings = {}
users_data = {}  # {user_id: {premium: bool, premium_until: timestamp, downloads_today: int, last_download_date: str, referral_code: str, referred_by: user_id}}
referrals = {}  # {referral_code: user_id}

# Константы
FREE_DAILY_LIMIT = 5
PREMIUM_QUALITY_OPTIONS = ['best', '1080p']

# FSM состояния
class VideoStates(StatesGroup):
    choosing_quality = State()

# ==================== РАБОТА С ДАННЫМИ ====================

def load_user_settings():
    global user_settings
    try:
        with open(SETTINGS_FILE, 'r', encoding='utf-8') as f:
            user_settings = json.load(f)
            # Конвертируем ключи обратно в int
            user_settings = {int(k): v for k, v in user_settings.items()}
            logger.info(f"Загружено {len(user_settings)} настроек пользователей")
    except FileNotFoundError:
        user_settings = {}
        save_user_settings()
    except Exception as e:
        logger.error(f"Ошибка загрузки настроек: {e}")
        user_settings = {}

def save_user_settings():
    try:
        with open(SETTINGS_FILE, 'w', encoding='utf-8') as f:
            json.dump(user_settings, f, ensure_ascii=False, indent=2)
    except Exception as e:
        logger.error(f"Ошибка сохранения настроек: {e}")

def load_users_data():
    global users_data
    try:
        with open(USERS_FILE, 'r', encoding='utf-8') as f:
            users_data = json.load(f)
            users_data = {int(k): v for k, v in users_data.items()}
            logger.info(f"Загружено {len(users_data)} пользователей")
    except FileNotFoundError:
        users_data = {}
        save_users_data()
    except Exception as e:
        logger.error(f"Ошибка загрузки данных пользователей: {e}")
        users_data = {}

def save_users_data():
    try:
        with open(USERS_FILE, 'w', encoding='utf-8') as f:
            json.dump(users_data, f, ensure_ascii=False, indent=2)
    except Exception as e:
        logger.error(f"Ошибка сохранения данных пользователей: {e}")

def load_referrals():
    global referrals
    try:
        with open(REFERRALS_FILE, 'r', encoding='utf-8') as f:
            referrals = json.load(f)
            logger.info(f"Загружено {len(referrals)} реферальных кодов")
    except FileNotFoundError:
        referrals = {}
        save_referrals()
    except Exception as e:
        logger.error(f"Ошибка загрузки реферальных кодов: {e}")
        referrals = {}

def save_referrals():
    try:
        with open(REFERRALS_FILE, 'w', encoding='utf-8') as f:
            json.dump(referrals, f, ensure_ascii=False, indent=2)
    except Exception as e:
        logger.error(f"Ошибка сохранения реферальных кодов: {e}")

def generate_referral_code(user_id: int) -> str:
    """Генерирует уникальный реферальный код для пользователя"""
    return hashlib.md5(f"{user_id}{datetime.now()}".encode()).hexdigest()[:8]

def get_or_create_user(user_id: int) -> dict:
    """Получает или создает данные пользователя"""
    if user_id not in users_data:
        referral_code = generate_referral_code(user_id)
        users_data[user_id] = {
            'premium': False,
            'premium_until': None,
            'downloads_today': 0,
            'last_download_date': None,
            'referral_code': referral_code,
            'referred_by': None,
            'referrals_completed': []
        }
        referrals[referral_code] = user_id
        save_users_data()
        save_referrals()
    return users_data[user_id]

def is_premium(user_id: int) -> bool:
    """Проверяет, является ли пользователь премиум"""
    user = get_or_create_user(user_id)
    if user['premium'] and user['premium_until']:
        if datetime.fromisoformat(user['premium_until']) > datetime.now():
            return True
        else:
            user['premium'] = False
            save_users_data()
    return False

def check_daily_limit(user_id: int) -> bool:
    """Проверяет дневной лимит загрузок. Возвращает True если можно загружать"""
    if is_premium(user_id):
        return True
    
    user = get_or_create_user(user_id)
    today = datetime.now().strftime('%Y-%m-%d')
    
    if user['last_download_date'] != today:
        user['downloads_today'] = 0
        user['last_download_date'] = today
    
    if user['downloads_today'] >= FREE_DAILY_LIMIT:
        return False
    
    return True

def increment_downloads(user_id: int):
    """Увеличивает счетчик загрузок"""
    user = get_or_create_user(user_id)
    today = datetime.now().strftime('%Y-%m-%d')
    
    if user['last_download_date'] != today:
        user['downloads_today'] = 1
        user['last_download_date'] = today
    else:
        user['downloads_today'] += 1
    
    save_users_data()

def activate_premium(user_id: int, days: int = 365):
    """Активирует премиум для пользователя"""
    user = get_or_create_user(user_id)
    user['premium'] = True
    user['premium_until'] = (datetime.now() + timedelta(days=days)).isoformat()
    save_users_data()

def get_quality_setting(user_id: int) -> str:
    """Получает настройку качества пользователя"""
    return user_settings.get(user_id, "720p")

# ==================== COOKIES ====================

def init_cookies_from_env():
    """Создаёт файлы cookies из переменных окружения"""
    cookie_env_to_file = {
        "COOKIES_YOUTUBE": "cookies_youtube.txt",
        "COOKIES_BOT1": "cookies_instagram_bot1.txt",
        "COOKIES_BOT2": "cookies_instagram_bot2.txt",
        "COOKIES_BOT3": "cookies_instagram_bot3.txt",
    }
    
    for env_var, filename in cookie_env_to_file.items():
        cookies_json = os.getenv(env_var)
        if cookies_json:
            try:
                cookies_data = json.loads(cookies_json)
                with open(filename, 'w', encoding='utf-8') as f:
                    json.dump(cookies_data, f, ensure_ascii=False, indent=2)
                logger.info(f"Создан {filename}")
            except json.JSONDecodeError:
                try:
                    with open(filename, 'w', encoding='utf-8') as f:
                        f.write(cookies_json)
                    logger.info(f"Создан {filename} (текстовый формат)")
                except Exception as e:
                    logger.error(f"Ошибка записи {filename}: {e}")
            except Exception as e:
                logger.error(f"Ошибка записи {filename}: {e}")

    if not os.path.exists("cookies_youtube.txt"):
        Path("cookies_youtube.txt").touch()
        logger.info("Создан пустой файл cookies_youtube.txt")

# ==================== YT-DLP ====================

def get_ydl_opts(quality: str = "720p", use_youtube_cookies: bool = True) -> Dict[str, Any]:
    """Формирует опции для yt_dlp"""
    quality_formats = {
        'best': 'bestvideo+bestaudio/best',
        '1080p': 'bestvideo[height<=1080]+bestaudio/best[height<=1080]',
        '720p': 'bestvideo[height<=720]+bestaudio/best[height<=720]',
        '480p': 'bestvideo[height<=480]+bestaudio/best[height<=480]',
        'audio': 'bestaudio/best',
    }
    
    format_str = quality_formats.get(quality.lower(), quality_formats['720p'])
    
    ydl_opts = {
        'format': format_str,
        'outtmpl': '%(title)s.%(ext)s',
        'noplaylist': True,
        'extractaudio': quality.lower() == 'audio',
        'nocheckcertificate': True,
        'ignoreerrors': False,
        'no_warnings': False,
        'quiet': False,
        'merge_output_format': 'mp4' if quality.lower() != 'audio' else 'mp3',
    }
    
    cookie_file = "cookies_youtube.txt"
    if use_youtube_cookies and os.path.exists(cookie_file):
        ydl_opts['cookiefile'] = cookie_file
    
    return ydl_opts

# ==================== PLAYWRIGHT ====================

async def init_instagram_playwright():
    global IG_BROWSER, IG_CONTEXT, IG_PLAYWRIGHT_READY
    logger.info("Инициализация Instagram Playwright...")
    try:
        pw = await async_playwright().start()
        IG_BROWSER = await pw.chromium.launch(headless=True)
        IG_CONTEXT = await IG_BROWSER.new_context(
            user_agent='Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36'
        )

        cookies_json = os.getenv("COOKIES_INSTAGRAM") or os.getenv("COOKIES_TXT")
        if cookies_json:
            try:
                cookies_data = json.loads(cookies_json)
                cookies_to_load = []
                for cookie in cookies_data:
                    pw_cookie = {
                        'name': cookie.get('name', ''),
                        'value': cookie.get('value', ''),
                        'domain': cookie.get('domain', ''),
                        'path': cookie.get('path', '/'),
                        'expires': int(cookie.get('expires', 0)) if cookie.get('expires') else None,
                        'secure': bool(cookie.get('secure', False)),
                        'httpOnly': bool(cookie.get('httpOnly', False)),
                        'sameSite': 'Lax'
                    }
                    pw_cookie = {k: v for k, v in pw_cookie.items() if v is not None}
                    cookies_to_load.append(pw_cookie)

                await IG_CONTEXT.add_cookies(cookies_to_load)
                logger.info(f"Загружено {len(cookies_to_load)} Instagram cookies")
            except Exception as e:
                logger.error(f"Ошибка загрузки Instagram cookies: {e}")

        IG_PLAYWRIGHT_READY = True
        logger.info("Instagram Playwright инициализирован")
    except Exception as e:
        logger.error(f"Ошибка инициализации Instagram Playwright: {e}")
        IG_PLAYWRIGHT_READY = False

async def init_youtube_playwright():
    global YT_BROWSER, YT_CONTEXT, YT_PLAYWRIGHT_READY
    logger.info("Инициализация YouTube Playwright...")
    try:
        pw = await async_playwright().start()
        YT_BROWSER = await pw.chromium.launch(headless=True)
        YT_CONTEXT = await YT_BROWSER.new_context(
            user_agent='Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36'
        )

        cookie_file_path = Path("cookies_youtube.txt")
        if cookie_file_path.exists():
            logger.info(f"Загружаем YouTube cookies из {cookie_file_path.name}")
            try:
                with open(cookie_file_path, 'r', encoding='utf-8') as f:
                    lines = f.readlines()
                cookies_to_load = []
                for line in lines:
                    if line.startswith('#') or not line.strip():
                        continue
                    try:
                        parts = line.strip().split('\t')
                        if len(parts) >= 7:
                            domain, flag, path, secure, expiration, name, value = parts[:7]
                            pw_cookie = {
                                'name': name,
                                'value': value,
                                'domain': domain.lstrip('.'),
                                'path': path,
                                'expires': int(expiration) if expiration.isdigit() else None,
                                'secure': secure.lower() == 'true',
                                'httpOnly': False,
                                'sameSite': 'Lax'
                            }
                            pw_cookie = {k: v for k, v in pw_cookie.items() if v is not None}
                            cookies_to_load.append(pw_cookie)
                    except ValueError:
                        continue

                if cookies_to_load:
                    await YT_CONTEXT.add_cookies(cookies_to_load)
                    logger.info(f"Загружено {len(cookies_to_load)} YouTube cookies")
            except Exception as e:
                logger.error(f"Ошибка загрузки cookies: {e}")

        YT_PLAYWRIGHT_READY = True
        logger.info("YouTube Playwright инициализирован")
    except Exception as e:
        logger.error(f"Ошибка инициализации YouTube Playwright: {e}")
        YT_PLAYWRIGHT_READY = False

# ==================== СКАЧИВАНИЕ ====================

def cleanup_file(file_path: str):
    try:
        if file_path and os.path.exists(file_path):
            os.remove(file_path)
            logger.info(f"Удалён файл: {Path(file_path).name}")
    except Exception as e:
        logger.warning(f"Не удалось удалить {file_path}: {e}")

def cleanup_files(files: List[str]):
    for file_path in files:
        cleanup_file(file_path)

async def download_youtube(url: str, quality: str = "720p") -> Optional[str]:
    """Скачивание с YouTube"""
    logger.info(f"Скачивание с YouTube (качество={quality})...")
    
    # Попытка без cookies
    ydl_opts_no_cookies = get_ydl_opts(quality, use_youtube_cookies=False)
    try:
        with yt_dlp.YoutubeDL(ydl_opts_no_cookies) as ydl:
            info = ydl.extract_info(url, download=True)
            temp_file = ydl.prepare_filename(info)
            if temp_file and os.path.exists(temp_file):
                logger.info(f"Видео скачано через yt-dlp без куки")
                return temp_file
    except Exception as e:
        logger.error(f"Ошибка yt-dlp (без куки): {e}")

    # Попытка с cookies
    ydl_opts_with_cookies = get_ydl_opts(quality, use_youtube_cookies=True)
    if ydl_opts_with_cookies.get('cookiefile'):
        try:
            with yt_dlp.YoutubeDL(ydl_opts_with_cookies) as ydl:
                info = ydl.extract_info(url, download=True)
                temp_file = ydl.prepare_filename(info)
                if temp_file and os.path.exists(temp_file):
                    logger.info(f"Видео скачано через yt-dlp с куки")
                    return temp_file
        except Exception as e:
            logger.error(f"Ошибка yt-dlp (с куки): {e}")

    logger.error("Обе попытки скачивания не удались")
    return None

async def download_youtube_with_playwright(url: str, quality: str = "720p") -> Optional[str]:
    """Резервный метод через Playwright"""
    global YT_CONTEXT
    if not YT_PLAYWRIGHT_READY or not YT_CONTEXT:
        return None

    logger.info(f"Скачивание через Playwright (качество={quality})...")
    page = None
    temp_cookies_file = None
    try:
        page = await YT_CONTEXT.new_page()
        await page.goto(url, wait_until='networkidle')

        if "consent.youtube.com" in page.url:
            try:
                accept_button = page.get_by_text("I agree", exact=True)
                if await accept_button.count() > 0:
                    await accept_button.click()
                    await page.wait_for_load_state('networkidle')
            except Exception:
                pass

        cookies = await YT_CONTEXT.cookies()
        if cookies:
            temp_cookies_file = Path(tempfile.mktemp(suffix='.txt'))
            with open(temp_cookies_file, 'w', encoding='utf-8') as f:
                f.write("# Netscape HTTP Cookie File\n")
                for cookie in cookies:
                    domain = cookie['domain']
                    flag = 'TRUE' if domain.startswith('.') else 'FALSE'
                    path = cookie['path']
                    secure = 'TRUE' if cookie['secure'] else 'FALSE'
                    expires = int(cookie['expires']) if cookie.get('expires') and cookie['expires'] > 0 else 0
                    name = cookie['name']
                    value = cookie['value']
                    f.write(f"{domain}\t{flag}\t{path}\t{secure}\t{expires}\t{name}\t{value}\n")

            ydl_opts = get_ydl_opts(quality, use_youtube_cookies=False)
            ydl_opts['cookiefile'] = str(temp_cookies_file)
        else:
            ydl_opts = get_ydl_opts(quality, use_youtube_cookies=False)

        with yt_dlp.YoutubeDL(ydl_opts) as ydl:
            info = ydl.extract_info(url, download=True)
            temp_file = ydl.prepare_filename(info)
            if temp_file and os.path.exists(temp_file):
                logger.info(f"Видео скачано через Playwright")
                return temp_file

    except Exception as e:
        logger.error(f"Ошибка в Playwright: {e}")
    finally:
        if page:
            await page.close()
        if temp_cookies_file and temp_cookies_file.exists():
            temp_cookies_file.unlink(missing_ok=True)
    
    return None

async def download_tiktok(url: str, quality: str = "720p") -> Optional[str]:
    """Скачивание с TikTok"""
    logger.info(f"Скачивание с TikTok...")
    ydl_opts = {
        'format': 'best',
        'outtmpl': '%(title)s.%(ext)s',
        'noplaylist': True,
        'extractaudio': False,
        'nocheckcertificate': True,
        'ignoreerrors': False,
        'no_warnings': False,
        'quiet': False,
    }

    try:
        with yt_dlp.YoutubeDL(ydl_opts) as ydl:
            info = ydl.extract_info(url, download=True)
            temp_file = ydl.prepare_filename(info)
            if temp_file and os.path.exists(temp_file):
                logger.info(f"Видео TikTok скачано")
                return temp_file
    except Exception as e:
        logger.error(f"Ошибка скачивания TikTok: {e}")
    
    return None

async def download_tiktok_photos(url: str) -> Tuple[Optional[List[str]], str]:
    """Скачивание фото с TikTok"""
    logger.info(f"Скачивание фото с TikTok...")
    ydl_opts = {
        'format': 'best',
        'outtmpl': '%(id)s/%(autonumber)s.%(ext)s',
        'noplaylist': False,
        'extractaudio': False,
        'nocheckcertificate': True,
        'ignoreerrors': False,
        'no_warnings': True,
        'quiet': True,
        'playlistend': 10,
    }

    try:
        with yt_dlp.YoutubeDL(ydl_opts) as ydl:
            info = ydl.extract_info(url, download=True)
            temp_dir = info.get('id')
            if temp_dir and os.path.isdir(temp_dir):
                photo_files = [os.path.join(temp_dir, f) for f in sorted(os.listdir(temp_dir)) 
                             if f.lower().endswith(('.jpg', '.jpeg', '.png', '.webp'))]
                if photo_files:
                    description = info.get('description', '') or info.get('title', '')
                    logger.info(f"Скачано {len(photo_files)} фото из TikTok")
                    return photo_files, description
    except Exception as e:
        logger.error(f"Ошибка скачивания фото TikTok: {e}")
    
    return None, ""

async def download_instagram(url: str) -> Tuple[Optional[str], Optional[List[str]], str]:
    """Скачивание с Instagram"""
    logger.info(f"Скачивание с Instagram...")
    ydl_opts = get_ydl_opts(quality="best", use_youtube_cookies=False)
    
    try:
        with yt_dlp.YoutubeDL(ydl_opts) as ydl:
            info = ydl.extract_info(url, download=True)
            temp_file = ydl.prepare_filename(info)
            if temp_file and os.path.exists(temp_file):
                logger.info(f"Медиа Instagram скачано")
                description = info.get('description', '') or info.get('title', '')
                return temp_file, None, description
            else:
                temp_dir = info.get('id')
                if temp_dir and os.path.isdir(temp_dir):
                    photo_files = [os.path.join(temp_dir, f) for f in sorted(os.listdir(temp_dir)) 
                                 if f.lower().endswith(('.jpg', '.jpeg', '.png', '.webp'))]
                    if photo_files:
                        logger.info(f"Скачано {len(photo_files)} фото из Instagram")
                        description = info.get('description', '') or info.get('title', '')
                        return None, photo_files, description
    except Exception as e:
        logger.info(f"yt-dlp не удалось, пробуем Playwright: {e}")
        return await download_instagram_with_playwright(url)

    return None, None, ""

async def download_instagram_with_playwright(url: str) -> Tuple[Optional[str], Optional[List[str]], str]:
    """Скачивание с Instagram через Playwright"""
    global IG_CONTEXT
    if not IG_PLAYWRIGHT_READY or not IG_CONTEXT:
        return None, None, ""

    logger.info(f"Скачивание с Instagram через Playwright...")
    page = None
    try:
        page = await IG_CONTEXT.new_page()
        await page.goto(url, wait_until='networkidle')

        if "accounts/login" in page.url or "challenge" in page.url:
            logger.warning("Требуется аутентификация на Instagram")
            return None, None, ""

        description_element = page.locator('article div._ab1k._ab1l div._aa99._aamp span')
        description = await description_element.first.text_content() if await description_element.count() > 0 else ""

        video_element = page.locator('article video source')
        if await video_element.count() > 0:
            video_url = await video_element.first.get_attribute('src')
            if video_url:
                ydl_opts = {
                    'format': 'best',
                    'outtmpl': '%(title)s.%(ext)s',
                    'noplaylist': True,
                    'extractaudio': False,
                    'nocheckcertificate': True,
                }
                try:
                    with yt_dlp.YoutubeDL(ydl_opts) as ydl:
                        info = ydl.extract_info(video_url, download=True)
                        temp_file = ydl.prepare_filename(info)
                        if temp_file and os.path.exists(temp_file):
                            return temp_file, None, description
                except Exception as e:
                    logger.error(f"Ошибка скачивания видео: {e}")

        photo_elements = page.locator('article img')
        photo_count = await photo_elements.count()
        if photo_count > 0:
            photo_urls = []
            for i in range(photo_count):
                photo_url = await photo_elements.nth(i).get_attribute('src')
                if photo_url:
                    photo_urls.append(photo_url)

            if photo_urls:
                temp_dir = tempfile.mkdtemp()
                photo_paths = []
                for i, photo_url in enumerate(photo_urls):
                    async with aiohttp.ClientSession() as session:
                        async with session.get(photo_url) as resp:
                            if resp.status == 200:
                                photo_path = os.path.join(temp_dir, f"ig_photo_{i+1}.jpg")
                                with open(photo_path, 'wb') as f:
                                    f.write(await resp.read())
                                photo_paths.append(photo_path)
                if photo_paths:
                    return None, photo_paths, description

    except Exception as e:
        logger.error(f"Ошибка в Playwright Instagram: {e}")
    finally:
        if page:
            await page.close()
    
    return None, None, ""

async def upload_to_fileio(file_path: str) -> Optional[str]:
    """Загрузка на file.io"""
    url = 'https://file.io/'
    max_size = 50 * 1024 * 1024
    file_size = os.path.getsize(file_path)
    
    if file_size > max_size:
        logger.info(f"Файл превышает 50 MB, загружаю на file.io...")
        try:
            async with aiohttp.ClientSession() as session:
                with open(file_path, 'rb') as f:
                    data = aiohttp.FormData()
                    data.add_field('file', f, filename=Path(file_path).name)
                    async with session.post(url, data=data) as resp:
                        if resp.status == 200:
                            response_json = await resp.json()
                            if response_json.get('success'):
                                fileio_link = response_json.get('link')
                                logger.info(f"Файл загружен на file.io")
                                return fileio_link
        except Exception as e:
            logger.error(f"Ошибка загрузки на file.io: {e}")
    
    return None

async def send_video_or_message(chat_id: int, file_path: str, caption: str = ""):
    """Отправка видео или файла"""
    max_telegram_file_size = 50 * 1024 * 1024
    file_size = os.path.getsize(file_path)
    
    if file_size > max_telegram_file_size:
        fileio_link = await upload_to_fileio(file_path)
        if fileio_link:
            await bot.send_message(chat_id, f"Файл слишком большой для Telegram.\nСсылка: {fileio_link}")
        else:
            await bot.send_message(chat_id, "Не удалось загрузить файл.")
    else:
        input_file = FSInputFile(file_path)
        try:
            await bot.send_video(chat_id=chat_id, video=input_file, caption=caption)
        except TelegramBadRequest as e:
            if "Wrong type of the web page content" in str(e):
                try:
                    await bot.send_photo(chat_id=chat_id, photo=input_file, caption=caption)
                except TelegramBadRequest:
                    await bot.send_document(chat_id=chat_id, document=input_file, caption=caption)
            else:
                await bot.send_message(chat_id, f"Ошибка при отправке файла.")

# ==================== КЛАВИАТУРЫ ====================

def main_keyboard() -> ReplyKeyboardMarkup:
    """Главное меню"""
    keyboard = [
        [KeyboardButton(text="Выбрать качество")],
        [KeyboardButton(text="Help")],
        [KeyboardButton(text="Расширить возможности")],
    ]
    return ReplyKeyboardMarkup(keyboard=keyboard, resize_keyboard=True)

def quality_keyboard(user_id: int) -> InlineKeyboardMarkup:
    """Клавиатура выбора качества"""
    is_premium_user = is_premium(user_id)
    
    buttons = [
        [InlineKeyboardButton(text="Максимальное качество без сжатия Telegram", 
                             callback_data="best")],
        [InlineKeyboardButton(text="1080p (Премиум)", 
                             callback_data="1080p")],
        [InlineKeyboardButton(text="720p", 
                             callback_data="720p")],
        [InlineKeyboardButton(text="480p", 
                             callback_data="480p")],
        [InlineKeyboardButton(text="Только аудио", 
                             callback_data="audio")],
        [InlineKeyboardButton(text="Отмена", 
                             callback_data="cancel")],
    ]
    
    return InlineKeyboardMarkup(inline_keyboard=buttons)

def premium_required_keyboard() -> InlineKeyboardMarkup:
    """Клавиатура для премиум-функций"""
    buttons = [
        [InlineKeyboardButton(text="Пригласить друга", 
                             callback_data="invite_friend")],
        [InlineKeyboardButton(text="Назад", 
                             callback_data="back_to_menu")],
    ]
    return InlineKeyboardMarkup(inline_keyboard=buttons)

def referral_keyboard(user_id: int) -> InlineKeyboardMarkup:
    """Клавиатура реферальной системы"""
    user = get_or_create_user(user_id)
    referral_link = f"https://t.me/{BOT_USERNAME}?start={user['referral_code']}"
    
    buttons = [
        [InlineKeyboardButton(text="Скопировать ссылку",
                            url=referral_link)],
        [InlineKeyboardButton(text="Проверить приглашение",
                            callback_data="check_referral")],
        [InlineKeyboardButton(text="Как это работает",
                            callback_data="how_referral_works")],
        [InlineKeyboardButton(text="Назад",
                            callback_data="back_to_menu")],
    ]
    return InlineKeyboardMarkup(inline_keyboard=buttons)

def back_to_menu_keyboard() -> InlineKeyboardMarkup:
    """Кнопка назад в меню"""
    buttons = [[InlineKeyboardButton(text="Вернуться в главное меню", 
                                     callback_data="back_to_menu")]]
    return InlineKeyboardMarkup(inline_keyboard=buttons)

def limit_reached_keyboard() -> InlineKeyboardMarkup:
    """Клавиатура при достижении лимита"""
    buttons = [
        [InlineKeyboardButton(text="Пригласить друга", 
                             callback_data="invite_friend")],
        [InlineKeyboardButton(text="Написать фидбэк админу", 
                             url=f"https://t.me/{ADMIN_USERNAME.replace('@', '')}")],
    ]
    return InlineKeyboardMarkup(inline_keyboard=buttons)

def conditions_keyboard(is_premium_user: bool) -> InlineKeyboardMarkup:
    """Клавиатура условий использования"""
    if is_premium_user:
        buttons = [
            [InlineKeyboardButton(text="Поделиться ботом", 
                                 callback_data="share_bot")],
            [InlineKeyboardButton(text="Дать обратную связь", 
                                 url=f"https://t.me/{ADMIN_USERNAME.replace('@', '')}")],
            [InlineKeyboardButton(text="Назад", 
                                 callback_data="back_to_menu")],
        ]
    else:
        buttons = [
            [InlineKeyboardButton(text="Получить бесплатно", 
                                 callback_data="invite_friend")],
            [InlineKeyboardButton(text="Проверить приглашение", 
                                 callback_data="check_referral")],
            [InlineKeyboardButton(text="Как это работает?", 
                                 callback_data="how_referral_works")],
            [InlineKeyboardButton(text="Назад в меню", 
                                 callback_data="back_to_menu")],
        ]
    return InlineKeyboardMarkup(inline_keyboard=buttons)

# ==================== ОБРАБОТЧИКИ КОМАНД ====================

@dp.message(Command("start"))
async def cmd_start(message: Message):
    """Обработчик /start"""
    user_id = message.from_user.id
    get_or_create_user(user_id)
    
    # Проверка реферального кода
    args = message.text.split()
    if len(args) > 1:
        referral_code = args[1]
        if referral_code in referrals:
            referrer_id = referrals[referral_code]
            if referrer_id != user_id:  # Нельзя приглашать самого себя
                user = users_data[user_id]
                if not user['referred_by']:  # Если еще не использовал реферальный код
                    user['referred_by'] = referrer_id
                    save_users_data()
                    logger.info(f"Пользователь {user_id} зарегистрирован по реферальной ссылке {referrer_id}")
    
    welcome_text = (
        "Кидайте ссылку — пришлю файл.\n\n"
        "Можно выбрать качество или оформить PRO."
    )
    await message.answer(welcome_text, reply_markup=main_keyboard())

@dp.message(F.text == "Help")
async def cmd_help(message: Message):
    """Обработчик Help"""
    help_text = (
        "<b>Как пользоваться:</b>\n\n"
        "1. Отправьте ссылку на видео.\n"
        "2. Если нужно — выберите качество.\n"
        "3. Получите файл.\n\n"
        "Поддерживаются YouTube, Instagram, TikTok, X (Twitter), Vimeo и другие.\n\n"
        "Приватные, удалённые и защищённые видео не скачиваются.\n\n"
        "<b>По умолчанию:</b>\n"
        "• 5 загрузок в сутки\n"
        "• Без максимального качества\n\n"
        "Разблокируйте всё на год бесплатно — пригласите друга в бота."
    )
    
    keyboard = InlineKeyboardMarkup(inline_keyboard=[
        [InlineKeyboardButton(text="Назад в меню", callback_data="back_to_menu")],
        [InlineKeyboardButton(text="Пригласить друга", callback_data="invite_friend")],
        [InlineKeyboardButton(text="Условия", callback_data="conditions")],
    ])
    
    await message.answer(help_text, reply_markup=keyboard, parse_mode="HTML")

@dp.message(F.text == "Выбрать качество")
async def cmd_choose_quality(message: Message):
    """Обработчик выбора качества"""
    user_id = message.from_user.id
    current = get_quality_setting(user_id)
    
    await message.answer(
        "Выберите качество загрузки:",
        reply_markup=quality_keyboard(user_id)
    )

@dp.message(F.text == "Расширить возможности")
async def cmd_expand(message: Message):
    """Обработчик расширения возможностей"""
    user_id = message.from_user.id
    is_premium_user = is_premium(user_id)
    
    if is_premium_user:
        user = users_data[user_id]
        premium_until = datetime.fromisoformat(user['premium_until']).strftime('%d.%m.%Y')
        
        text = (
            f"<b>У вас активен Премиум до {premium_until}.</b>\n\n"
            "Доступны:\n"
            "• Максимальное качество\n"
            "• Плейлисты и батчи\n"
            "• История загрузок\n"
            "• Приоритетная очередь"
        )
        
        keyboard = conditions_keyboard(True)
    else:
        text = (
            "<b>По умолчанию — 5 загрузок в сутки и без максимума качества.</b>\n\n"
            "Разблокируйте всё на 1 год бесплатно — пригласите друга по вашей ссылке.\n\n"
            "Через год это будет стоить 50 ₽/год.\n\n"
            "Сейчас — бесплатно. На год. Просто, блядь, бесплатно."
        )
        
        keyboard = conditions_keyboard(False)
    
    await message.answer(text, reply_markup=keyboard, parse_mode="HTML")

# ==================== ОБРАБОТЧИКИ CALLBACK ====================
@dp.callback_query(F.data == "invite_friend")
async def process_invite_friend(callback: CallbackQuery):
    """Обработчик приглашения друга"""
    logger.info(f"Пользователь {callback.from_user.id} нажал кнопку 'invite_friend'")
    await callback.answer() # Отвечаем перед редактированием
    user_id = callback.from_user.id
    user = get_or_create_user(user_id)
    referral_link = f"https://t.me/{BOT_USERNAME}?start={user['referral_code']}"
    text = (
        "<b>Пригласите друга и получите Премиум на 1 год!</b>\n\n"
        "Вот ваша персональная ссылка:\n"
        f"<code>{referral_link}</code>\n\n"
        "Как только ваш друг выполнит первое скачивание, вам и ему будет активирован Премиум."
    )
    keyboard = InlineKeyboardMarkup(inline_keyboard=[
        [InlineKeyboardButton(text="Скопировать ссылку", url=referral_link)],
        [InlineKeyboardButton(text="Вернуться в главное меню", callback_data="back_to_menu")],
    ])
    await callback.message.edit_text(text, reply_markup=keyboard, parse_mode="HTML")

@dp.callback_query(F.data == "conditions")
async def process_conditions(callback: CallbackQuery):
    """Условия использования"""
    logger.info(f"Пользователь {callback.from_user.id} нажал кнопку 'conditions'")
    await callback.answer() # Отвечаем перед редактированием
    user_id = callback.from_user.id
    is_premium_user = is_premium(user_id)
    if is_premium_user:
        user = users_data[user_id]
        premium_until = datetime.fromisoformat(user['premium_until']).strftime('%d.%m.%Y')
        text = (
            f"<b>У вас активен Премиум до {premium_until}.</b>\n"
            "Доступны:\n"
            "• Максимальное качество\n"
            "• Плейлисты и батчи\n"
            "• История загрузок\n"
            "• Приоритетная очередь"
        )
    else:
        text = (
            "<b>По умолчанию — 5 загрузок в сутки и без максимума качества.</b>\n"
            "Разблокируйте всё на 1 год бесплатно — пригласите друга по вашей ссылке.\n"
            "Через год это будет стоить 50 ₽/год.\n"
            "Сейчас — бесплатно. На год. Просто, блядь, бесплатно."
        )
    keyboard = conditions_keyboard(is_premium_user)
    await callback.message.edit_text(text, reply_markup=keyboard, parse_mode="HTML")

@dp.callback_query(F.data == "back_to_menu")
async def process_back_to_menu(callback: CallbackQuery):
    """Возврат в главное меню"""
    logger.info(f"Пользователь {callback.from_user.id} нажал кнопку 'back_to_menu'")
    await callback.answer() # Отвечаем перед редактированием
    try:
        await callback.message.delete()
    except Exception:
        pass
    welcome_text = (
        "Кидайте ссылку — пришлю файл.\n"
        "Можно выбрать качество или оформить PRO."
    )
    await bot.send_message(callback.from_user.id, welcome_text, reply_markup=main_keyboard())

@dp.callback_query(F.data == "share_bot")
async def process_share_bot(callback: CallbackQuery):
    """Поделиться ботом"""
    logger.info(f"Пользователь {callback.from_user.id} нажал кнопку 'share_bot'")
    await callback.answer()  # Отвечаем перед отправкой нового сообщения
    bot_info = await bot.get_me()
    bot_username = bot_info.username
    share_text = f"Попробуй этого бота для скачивания видео: https://t.me/{bot_username}"
    await bot.send_message(callback.from_user.id, f"Поделитесь этой ссылкой:\n{share_text}")

# Обработчик для неизвестных callback'ов (для отладки)
@dp.callback_query()
async def process_unknown_callback(callback: CallbackQuery):
    """Обработка неизвестных callback'ов"""
    logger.warning(f"Неизвестный callback от {callback.from_user.id}: {callback.data}")
    await callback.answer("Эта кнопка пока не работает", show_alert=True)



# ==================== ОБРАБОТЧИК ССЫЛОК ====================

@dp.message(F.text)
async def handle_link(message: Message):
    """Обработчик ссылок на видео"""
    url = message.text.strip()
    user_id = message.from_user.id
    chat_id = message.chat.id
    
    # Проверка на ссылку
    if not (url.startswith("http://") or url.startswith("https://")):
        return
    
    # Проверка лимита
    if not check_daily_limit(user_id):
        text = (
            "Лимит 5 загрузок в сутки исчерпан.\n\n"
            "Разблокируйте безлимит на год бесплатно: пригласите друга."
        )
        await message.answer(text, reply_markup=limit_reached_keyboard())
        return
    
    # Определение платформы
    if "youtube.com" in url or "youtu.be" in url:
        platform = "youtube"
    elif "tiktok.com" in url or "vm.tiktok.com" in url or "vt.tiktok.com" in url:
        platform = "tiktok"
    elif "instagram.com" in url or "instagr.am" in url:
        platform = "instagram"
    else:
        await message.answer(
            "Неподдерживаемая платформа.\n\n"
            "Поддерживаются: YouTube, Instagram, TikTok."
        )
        return
    
    quality = get_quality_setting(user_id)
    temp_file = None
    temp_photos = []
    
    try:
        if platform == "youtube":
            temp_file = await download_youtube(url, quality)
            if not temp_file:
                temp_file = await download_youtube_with_playwright(url, quality)
            
            if temp_file:
                await send_video_or_message(chat_id, temp_file)
                cleanup_file(temp_file)
                increment_downloads(user_id)
                
                # Проверка реферала
                user = users_data[user_id]
                if user['referred_by'] and user['downloads_today'] == 1:
                    referrer = users_data[user['referred_by']]
                    if user_id not in referrer.get('referrals_completed', []):
                        referrer.setdefault('referrals_completed', []).append(user_id)
                        activate_premium(user['referred_by'])
                        activate_premium(user_id)
                        save_users_data()
                        
                        await bot.send_message(
                            user['referred_by'],
                            "Поздравляем! Ваш друг выполнил условия.\n"
                            "Вам активирован Премиум на 1 год!"
                        )
            else:
                await message.answer(
                    "Не удалось скачать видео.\n\n"
                    "Возможные причины:\n"
                    "• Видео приватное или удалено\n"
                    "• Проблемы с доступом к платформе\n"
                    "• Некорректная ссылка"
                )
        
        elif platform == "tiktok":
            if '/photo/' in url.lower():
                photos, description = await download_tiktok_photos(url)
                if photos:
                    temp_photos = photos
                    media_group = [InputMediaPhoto(media=FSInputFile(photo)) for photo in photos]
                    
                    batch_size = 10
                    for i in range(0, len(media_group), batch_size):
                        batch = media_group[i:i + batch_size]
                        await bot.send_media_group(chat_id=chat_id, media=batch)
                    
                    cleanup_files(photos)
                    increment_downloads(user_id)
                else:
                    await message.answer("Не удалось скачать фото с TikTok.")
            else:
                temp_file = await download_tiktok(url, quality)
                if temp_file:
                    await send_video_or_message(chat_id, temp_file)
                    cleanup_file(temp_file)
                    increment_downloads(user_id)
                else:
                    await message.answer("Не удалось скачать видео с TikTok.")
        
        elif platform == "instagram":
            video_path, photos, description = await download_instagram(url)
            
            if video_path:
                await send_video_or_message(chat_id, video_path)
                cleanup_file(video_path)
                increment_downloads(user_id)
            elif photos:
                temp_photos = photos
                media_group = [InputMediaPhoto(media=FSInputFile(photo)) for photo in photos]
                
                batch_size = 10
                for i in range(0, len(media_group), batch_size):
                    batch = media_group[i:i + batch_size]
                    await bot.send_media_group(chat_id=chat_id, media=batch)
                
                cleanup_files(photos)
                increment_downloads(user_id)
            else:
                await message.answer("Не удалось скачать медиа с Instagram.")
    
    except Exception as e:
        logger.error(f"Ошибка обработки ссылки: {e}")
        await message.answer(
            "Произошла ошибка при обработке вашего запроса.\n\n"
            "Попробуйте позже или обратитесь к администратору."
        )
    finally:
        if temp_file:
            cleanup_file(temp_file)
        if temp_photos:
            cleanup_files(temp_photos)

# ==================== ЗАПУСК БОТА ====================

async def main():
    """Основная функция запуска"""
    global bot
    logger.info("Запуск бота...")
    
    if not BOT_TOKEN:
        raise ValueError("BOT_TOKEN не найден в переменных окружения")
    
    init_cookies_from_env()
    load_user_settings()
    load_users_data()
    load_referrals()
    
    await init_instagram_playwright()
    await init_youtube_playwright()
    
    bot = Bot(token=BOT_TOKEN, session=AiohttpSession())
    
    WEBHOOK_PATH = "/webhook"
    WEBHOOK_URL = f"{WEBHOOK_HOST}{WEBHOOK_PATH}" if WEBHOOK_HOST else None
    
    if WEBHOOK_URL:
        logger.info(f"Работаю в режиме Webhook: {WEBHOOK_URL}")
        await bot.delete_webhook(drop_pending_updates=True)
        await bot.set_webhook(WEBHOOK_URL)
        
        app = aiohttp.web.Application()
        from aiogram.webhook.aiohttp_server import SimpleRequestHandler
        webhook_requests_handler = SimpleRequestHandler(dispatcher=dp, bot=bot)
        webhook_requests_handler.register(app, path=WEBHOOK_PATH)
        
        async def health(request):
            return aiohttp.web.Response(text="OK")
        
        app.router.add_get("/", health)
        app.router.add_get("/health", health)
        
        runner = aiohttp.web.AppRunner(app)
        await runner.setup()
        site = aiohttp.web.TCPSite(runner, '0.0.0.0', PORT)
        await site.start()
        logger.info(f"Webhook запущен на порту {PORT}")
        
        await asyncio.Event().wait()
    else:
        logger.info("Работаю в режиме Polling")
        await bot.delete_webhook(drop_pending_updates=True)
        try:
            await dp.start_polling(bot)
        finally:
            save_user_settings()
            save_users_data()
            save_referrals()
            logger.info("Бот остановлен")
    
    if IG_BROWSER:
        await IG_BROWSER.close()
    if YT_BROWSER:
        await YT_BROWSER.close()

if __name__ == "__main__":
    asyncio.run(main())