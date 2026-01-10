# bot.py - Luno Bot
import asyncio
import json
import logging
import os
import tempfile
import hashlib
import importlib.util
import ast
import time
from datetime import datetime, timedelta
from pathlib import Path
from typing import Optional, List, Tuple, Dict, Any

import aiohttp
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

# YouTube Auto-Cookie Refresh
YOUTUBE_VISITOR_DATA: Optional[str] = None
YOUTUBE_PO_TOKEN: Optional[str] = None  # Proof of Origin token for bot bypass
YOUTUBE_COOKIES_LAST_REFRESH: Optional[datetime] = None
YOUTUBE_REFRESH_INTERVAL = 1800  # 30 минут

# Shutdown control
SHUTDOWN_FLAG = False
YOUTUBE_REFRESH_TASK: Optional[asyncio.Task] = None
INSTAGRAM_REFRESH_TASK: Optional[asyncio.Task] = None

# Instagram Auto-Cookie Refresh
INSTAGRAM_COOKIES_LAST_REFRESH: Optional[datetime] = None
INSTAGRAM_REFRESH_INTERVAL = 1800  # 30 минут
INSTAGRAM_SESSION_ID: Optional[str] = None

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
FREE_DAILY_LIMIT = 1488
PREMIUM_QUALITY_OPTIONS = ['best', '1080p']

# FSM состояния
class VideoStates(StatesGroup):
    choosing_quality = State()

# ==================== РАБОТА С ДАННЫМИ ===================

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

    def _write_netscape_cookiefile(file_path: str, cookies: List[Dict[str, Any]]):
        with open(file_path, 'w', encoding='utf-8') as f:
            f.write("# Netscape HTTP Cookie File\n")
            for cookie in cookies:
                domain = (cookie.get('domain') or '').strip()
                name = cookie.get('name') or ''
                value = cookie.get('value') or ''
                if not domain or not name:
                    continue
                flag = 'TRUE' if domain.startswith('.') else 'FALSE'
                path = cookie.get('path') or '/'
                secure = 'TRUE' if cookie.get('secure') else 'FALSE'
                expires_raw = cookie.get('expires')
                try:
                    expires = int(expires_raw) if expires_raw else 0
                except Exception:
                    expires = 0
                f.write(f"{domain}\t{flag}\t{path}\t{secure}\t{expires}\t{name}\t{value}\n")
    
    for env_var, filename in cookie_env_to_file.items():
        cookies_json = os.getenv(env_var)
        if cookies_json:
            cookies_data = None
            try:
                cookies_data = json.loads(cookies_json)
            except json.JSONDecodeError:
                try:
                    # Попытка распарсить как Python-структуру (например, если скопировано как список dict'ов)
                    cookies_data = ast.literal_eval(cookies_json)
                except Exception:
                    pass
            
            if isinstance(cookies_data, list):
                try:
                    _write_netscape_cookiefile(filename, cookies_data)
                    logger.info(f"Создан {filename} (из JSON/List)")
                except Exception as e:
                    logger.error(f"Ошибка записи Netscape cookies {filename}: {e}")
            else:
                # Если не удалось распарсить как список, пишем как есть (возможно, это уже Netscape формат)
                try:
                    with open(filename, 'w', encoding='utf-8') as f:
                        f.write(cookies_json)
                    logger.info(f"Создан {filename} (текстовый формат)")
                except Exception as e:
                    logger.error(f"Ошибка записи {filename}: {e}")
    
    if not os.path.exists("cookies_youtube.txt"):
        Path("cookies_youtube.txt").touch()
        logger.info("Создан пустой файл cookies_youtube.txt")

def _read_netscape_cookiefile(file_path: str) -> List[Dict[str, Any]]:
    cookies_to_load: List[Dict[str, Any]] = []
    try:
        with open(file_path, 'r', encoding='utf-8') as f:
            lines = f.readlines()
    except Exception:
        return cookies_to_load

    for line in lines:
        if line.startswith('#') or not line.strip():
            continue
        parts = line.strip().split('\t')
        if len(parts) < 7:
            continue
        domain, _flag, path, secure, expiration, name, value = parts[:7]
        cookie = {
            'name': name,
            'value': value,
            'domain': domain.lstrip('.'),
            'path': path or '/',
            'expires': int(expiration) if expiration.isdigit() else None,
            'secure': secure.lower() == 'true',
            'httpOnly': False,
            'sameSite': 'Lax',
        }
        cookie = {k: v for k, v in cookie.items() if v is not None}
        cookies_to_load.append(cookie)
    return cookies_to_load

# ==================== YT-DLP ====================

def get_ydl_opts(quality: str = "720p", use_youtube_cookies: bool = True) -> Dict[str, Any]:
    """Формирует опции для yt_dlp"""
    # Форматы с fallback на единый поток (для обхода блокировки audio)
    # best[ext=mp4] - единый поток со звуком, не требует merge
    quality_formats = {
        'best': 'bestvideo[ext=mp4]+bestaudio[ext=m4a]/best[ext=mp4]/best',
        '1080p': 'bestvideo[height<=1080][ext=mp4]+bestaudio[ext=m4a]/best[height<=1080][ext=mp4]/best[height<=1080]',
        '720p': 'bestvideo[height<=720][ext=mp4]+bestaudio[ext=m4a]/best[height<=720][ext=mp4]/best[height<=720]',
        '480p': 'bestvideo[height<=480][ext=mp4]+bestaudio[ext=m4a]/best[height<=480][ext=mp4]/best[height<=480]',
        'audio': 'bestaudio[ext=m4a]/bestaudio/best',
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
        'retries': 3,
        'fragment_retries': 3,
        'extractor_retries': 3,
        'concurrent_fragment_downloads': 1,
    }

    ytdlp_proxy = (os.getenv("YTDLP_PROXY") or "").strip()
    if ytdlp_proxy:
        ydl_opts['proxy'] = ytdlp_proxy

    curl_cffi_available = importlib.util.find_spec("curl_cffi") is not None
    ytdlp_impersonate = (os.getenv("YTDLP_IMPERSONATE") or "chrome").strip()
    if curl_cffi_available and ytdlp_impersonate:
        ydl_opts['impersonate'] = ytdlp_impersonate

    ytdlp_force_ipv4 = (os.getenv("YTDLP_FORCE_IPV4") or "").strip().lower()
    if ytdlp_force_ipv4 in {"1", "true", "yes"}:
        ydl_opts['force_ipv4'] = True

    ydl_opts['http_headers'] = {
        'User-Agent': 'Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/120.0.0.0 Safari/537.36',
        'Accept-Language': 'ru-RU,ru;q=0.9,en-US;q=0.8,en;q=0.7',
        'Referer': 'https://www.youtube.com/',
    }

    cookie_file = "cookies_youtube.txt"
    has_cookiefile = bool(use_youtube_cookies and os.path.exists(cookie_file) and os.path.getsize(cookie_file) > 0)
    if has_cookiefile:
        ydl_opts['cookiefile'] = cookie_file

    player_clients_raw = (os.getenv("YTDLP_YT_PLAYER_CLIENT") or "").strip()
    if player_clients_raw:
        player_clients = [c.strip() for c in player_clients_raw.split(",") if c.strip()]
    else:
        player_clients = ["web"]

    ydl_opts['extractor_args'] = ydl_opts.get('extractor_args') or {}
    ydl_opts['extractor_args']['youtube'] = ydl_opts['extractor_args'].get('youtube') or {}
    ydl_opts['extractor_args']['youtube']['player_client'] = player_clients

    po_token_raw = (os.getenv("YTDLP_YT_PO_TOKEN") or "").strip()
    if po_token_raw:
        po_token_value = po_token_raw if "+" in po_token_raw else f"mweb.gvs+{po_token_raw}"
        ydl_opts['extractor_args']['youtube']['po_token'] = po_token_value
    
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

        cookies_to_load: List[Dict[str, Any]] = []
        cookies_json = os.getenv("COOKIES_INSTAGRAM") or os.getenv("COOKIES_TXT")
        if cookies_json:
            try:
                cookies_data = json.loads(cookies_json)
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

            except Exception as e:
                logger.error(f"Ошибка загрузки Instagram cookies: {e}")

        if not cookies_to_load:
            for path in [
                "cookies_instagram_bot1.txt",
                "cookies_instagram_bot2.txt",
                "cookies_instagram_bot3.txt",
                "cookies_instagram.txt",
                "cookies.txt",
            ]:
                if os.path.exists(path) and os.path.getsize(path) > 0:
                    # Пробуем как Netscape
                    cookies_to_load = _read_netscape_cookiefile(path)
                    if not cookies_to_load:
                        # Пробуем как JSON (если не распарсилось выше, но записалось как текст)
                        try:
                            with open(path, 'r', encoding='utf-8') as f:
                                content = f.read()
                            try:
                                c_data = json.loads(content)
                            except json.JSONDecodeError:
                                try:
                                    c_data = ast.literal_eval(content)
                                except Exception:
                                    c_data = None
                            
                            if isinstance(c_data, list):
                                for cookie in c_data:
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
                        except Exception:
                            pass

                    if cookies_to_load:
                        break

        if cookies_to_load:
            try:
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
        YT_BROWSER = await pw.chromium.launch(
            headless=True,
            args=[
                '--disable-blink-features=AutomationControlled',
                '--disable-dev-shm-usage',
                '--no-sandbox',
            ]
        )
        YT_CONTEXT = await YT_BROWSER.new_context(
            user_agent='Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/120.0.0.0 Safari/537.36',
            viewport={'width': 1920, 'height': 1080},
            locale='en-US',
            timezone_id='America/New_York',
            extra_http_headers={
                'Accept-Language': 'en-US,en;q=0.9',
            }
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
            try:
                parent_dir = str(Path(file_path).resolve().parent)
                temp_root = str(Path(tempfile.gettempdir()).resolve())
                if os.path.isdir(parent_dir) and not os.listdir(parent_dir):
                    if os.path.commonpath([temp_root, parent_dir]) == temp_root:
                        os.rmdir(parent_dir)
            except Exception:
                pass
    except Exception as e:
        logger.warning(f"Не удалось удалить {file_path}: {e}")

def cleanup_files(files: List[str]):
    for file_path in files:
        cleanup_file(file_path)

def _is_netscape_cookiefile(file_path: str) -> bool:
    try:
        with open(file_path, 'r', encoding='utf-8') as f:
            for line in f:
                if line.startswith('#') or not line.strip():
                    continue
                return len(line.strip().split('\t')) >= 7
    except Exception:
        return False
    return False

def _get_instagram_cookiefile() -> Optional[str]:
    candidates = [
        "cookies_instagram_bot1.txt",
        "cookies_instagram_bot2.txt",
        "cookies_instagram_bot3.txt",
        "cookies_instagram.txt",
        "cookies.txt",
    ]
    for path in candidates:
        try:
            if os.path.exists(path) and os.path.getsize(path) > 0 and _is_netscape_cookiefile(path):
                return path
        except Exception:
            continue
    return None

def _pick_best_media_file(file_paths: List[str], exts: Tuple[str, ...]) -> Optional[str]:
    candidates = [p for p in file_paths if os.path.isfile(p) and p.lower().endswith(exts)]
    if not candidates:
        return None
    return max(candidates, key=lambda p: os.path.getsize(p) if os.path.exists(p) else 0)

def _instagram_url_prefers_video(url: str) -> bool:
    u = (url or "").lower()
    return any(token in u for token in ("/reel/", "/reels/", "/tv/"))

def _info_prefers_video(info: Any) -> bool:
    if not isinstance(info, dict):
        return False
    if info.get('duration') or info.get('vcodec') and info.get('vcodec') != 'none':
        return True
    formats = info.get('formats')
    if isinstance(formats, list):
        for f in formats:
            if not isinstance(f, dict):
                continue
            vcodec = f.get('vcodec')
            ext = (f.get('ext') or '').lower()
            if (isinstance(vcodec, str) and vcodec != 'none') or ext in {'mp4', 'webm', 'mkv', 'mov', 'm4v'}:
                return True
    return False

def _ydl_extract_info(url: str, ydl_opts: Dict[str, Any]) -> Dict[str, Any]:
    with yt_dlp.YoutubeDL(ydl_opts) as ydl:
        return ydl.extract_info(url, download=True)

def _ydl_download_info_and_path(url: str, ydl_opts: Dict[str, Any]) -> Tuple[Dict[str, Any], Optional[str]]:
    with yt_dlp.YoutubeDL(ydl_opts) as ydl:
        info = ydl.extract_info(url, download=True)
        temp_file: Optional[str] = None
        try:
            temp_file = ydl.prepare_filename(info)
        except Exception:
            temp_file = None
        if temp_file and os.path.exists(temp_file):
            return info, temp_file
        return info, None

def _ydl_download_path(url: str, ydl_opts: Dict[str, Any]) -> Optional[str]:
    with yt_dlp.YoutubeDL(ydl_opts) as ydl:
        info = ydl.extract_info(url, download=True)
        temp_file = ydl.prepare_filename(info)
        if temp_file and os.path.exists(temp_file):
            return temp_file
    return None

# ==================== YOUTUBE DOWNLOADER ====================

async def refresh_youtube_visitor_data():
    """Обновляет YouTube visitor_data и PO Token через Playwright с перехватом сетевых запросов."""
    global YOUTUBE_VISITOR_DATA, YOUTUBE_PO_TOKEN, YOUTUBE_COOKIES_LAST_REFRESH, YT_CONTEXT, YT_PLAYWRIGHT_READY
    
    if not YT_PLAYWRIGHT_READY or not YT_CONTEXT:
        logger.warning("YouTube Playwright не готов для обновления cookies")
        return False
    
    page = None
    captured_po_token = None
    captured_visitor_data = None
    
    async def handle_response(response):
        """Перехватываем ответы от YouTube API для извлечения PO Token."""
        nonlocal captured_po_token, captured_visitor_data
        try:
            url = response.url
            # Ищем запросы к player API или innertube
            if 'youtubei/v1/player' in url or 'youtubei/v1/next' in url:
                try:
                    body = await response.body()
                    text = body.decode('utf-8', errors='ignore')
                    data = json.loads(text)
                    
                    # Извлекаем PO Token из serviceIntegrityDimensions
                    sid = data.get('serviceIntegrityDimensions', {})
                    po_token = sid.get('poToken')
                    if po_token and not captured_po_token:
                        captured_po_token = po_token
                        logger.info(f"Захвачен PO Token: {po_token[:30]}...")
                    
                    # Извлекаем visitorData из responseContext
                    rc = data.get('responseContext', {})
                    vd = rc.get('visitorData')
                    if vd and not captured_visitor_data:
                        captured_visitor_data = vd
                        logger.info(f"Захвачен visitorData из API: {vd[:20]}...")
                except Exception:
                    pass
        except Exception:
            pass
    
    try:
        logger.info("Обновление YouTube visitor_data и PO Token...")
        page = await YT_CONTEXT.new_page()
        
        # Подписываемся на ответы до навигации
        page.on('response', handle_response)
        
        # Устанавливаем дополнительные заголовки для обхода детекции бота
        await page.set_extra_http_headers({
            'Accept-Language': 'en-US,en;q=0.9',
            'Accept': 'text/html,application/xhtml+xml,application/xml;q=0.9,image/webp,*/*;q=0.8',
            'sec-ch-ua': '"Not_A Brand";v="8", "Chromium";v="120", "Google Chrome";v="120"',
            'sec-ch-ua-mobile': '?0',
            'sec-ch-ua-platform': '"Windows"',
            'Sec-Fetch-Dest': 'document',
            'Sec-Fetch-Mode': 'navigate',
            'Sec-Fetch-Site': 'none',
            'Sec-Fetch-User': '?1',
        })
        
        # Заходим на YouTube
        await page.goto("https://www.youtube.com", wait_until='domcontentloaded', timeout=30000)
        await page.wait_for_timeout(2000)
        
        # Принимаем consent если есть
        consent_selectors = [
            'button[aria-label*="Accept"]',
            'button[aria-label*="accept"]',
            'button:has-text("Accept all")',
            'button:has-text("Accept All")',
            'button:has-text("I agree")',
            'button:has-text("Agree")',
            'button:has-text("Принять все")',
            'button:has-text("Согласен")',
            'button:has-text("Reject all")',
            '[aria-label="Accept the use of cookies and other data for the purposes described"]',
            'form[action*="consent"] button',
            'ytd-consent-bump-v2-lightbox button.yt-spec-button-shape-next--call-to-action',
        ]
        
        for selector in consent_selectors:
            try:
                btn = page.locator(selector).first
                if await btn.is_visible(timeout=500):
                    await btn.click()
                    logger.info(f"Нажата кнопка consent: {selector[:50]}")
                    await page.wait_for_timeout(2000)
                    break
            except Exception:
                continue
        
        # Ждём полной загрузки главной
        await page.wait_for_timeout(3000)
        
        # Переходим на популярное видео Shorts для получения PO Token
        # Shorts обычно лучше загружаются и дают токены
        test_video_urls = [
            "https://www.youtube.com/shorts/2g811Eo7K8U",  # Популярный shorts
            "https://www.youtube.com/watch?v=dQw4w9WgXcQ",  # Rick Roll (всегда работает)
        ]
        
        for test_url in test_video_urls:
            try:
                await page.goto(test_url, wait_until='domcontentloaded', timeout=15000)
                await page.wait_for_timeout(5000)  # Ждём загрузки плеера и API запросов
                
                # Проверяем получили ли мы токены
                if captured_po_token:
                    break
            except Exception as e:
                logger.debug(f"Ошибка загрузки тестового видео {test_url}: {e}")
                continue
        
        # Извлекаем cookies
        cookies = await page.context.cookies()
        
        visitor_data = captured_visitor_data
        
        # Если не захватили visitor_data из API, ищем в cookies
        if not visitor_data:
            for cookie in cookies:
                if cookie.get('name') == 'VISITOR_INFO1_LIVE':
                    visitor_data = cookie.get('value')
                    break
        
        # Если всё ещё нет, пробуем извлечь из JavaScript
        if not visitor_data:
            try:
                visitor_data = await page.evaluate('''() => {
                    try {
                        if (typeof ytcfg !== 'undefined' && ytcfg.get) {
                            const vd = ytcfg.get('VISITOR_DATA');
                            if (vd) return vd;
                        }
                        if (window.yt && window.yt.config_ && window.yt.config_.VISITOR_DATA) {
                            return window.yt.config_.VISITOR_DATA;
                        }
                        if (typeof ytInitialData !== 'undefined' && ytInitialData.responseContext) {
                            const visitorData = ytInitialData.responseContext.visitorData;
                            if (visitorData) return visitorData;
                        }
                        return null;
                    } catch (e) {
                        return null;
                    }
                }''')
                if visitor_data:
                    logger.info("visitor_data извлечён из JavaScript")
            except Exception as e:
                logger.debug(f"Не удалось извлечь visitor_data из JS: {e}")
        
        # Обновляем глобальные переменные
        success = False
        if visitor_data:
            YOUTUBE_VISITOR_DATA = visitor_data
            YOUTUBE_COOKIES_LAST_REFRESH = datetime.now()
            logger.info(f"YouTube visitor_data обновлён: {visitor_data[:20]}...")
            success = True
        
        if captured_po_token:
            YOUTUBE_PO_TOKEN = captured_po_token
            logger.info(f"YouTube PO Token сохранён")
            success = True
        
        if success:
            # Сохраняем cookies в файл для yt-dlp
            cookie_file = "cookies_youtube.txt"
            one_year_from_now = int(time.time()) + 365 * 24 * 60 * 60
            with open(cookie_file, 'w', encoding='utf-8') as f:
                f.write("# Netscape HTTP Cookie File\n")
                for cookie in cookies:
                    domain = cookie.get('domain', '')
                    if 'youtube' in domain or 'google' in domain:
                        flag = 'TRUE' if domain.startswith('.') else 'FALSE'
                        path = cookie.get('path', '/')
                        secure = 'TRUE' if cookie.get('secure') else 'FALSE'
                        raw_expires = cookie.get('expires')
                        if raw_expires and isinstance(raw_expires, (int, float)) and raw_expires > time.time():
                            expires = int(raw_expires)
                        else:
                            expires = one_year_from_now
                        name = cookie.get('name', '')
                        value = cookie.get('value', '')
                        if name and value:
                            f.write(f"{domain}\t{flag}\t{path}\t{secure}\t{expires}\t{name}\t{value}\n")
            
            logger.info(f"YouTube cookies сохранены в {cookie_file}")
            return True
        else:
            cookie_names = [c.get('name') for c in cookies if 'youtube' in c.get('domain', '') or 'google' in c.get('domain', '')]
            logger.warning(f"Не удалось получить токены. Доступные cookies: {cookie_names[:10]}")
            return False
            
    except Exception as e:
        logger.error(f"Ошибка обновления YouTube токенов: {e}")
        return False
    finally:
        if page:
            try:
                page.remove_listener('response', handle_response)
                await page.close()
            except Exception:
                pass



async def youtube_cookie_refresh_loop():
    """Фоновый цикл обновления YouTube cookies."""
    global YOUTUBE_REFRESH_INTERVAL, SHUTDOWN_FLAG
    
    # Первоначальная задержка для инициализации Playwright
    await asyncio.sleep(10)
    
    while not SHUTDOWN_FLAG:
        try:
            if SHUTDOWN_FLAG:
                break
            await refresh_youtube_visitor_data()
        except asyncio.CancelledError:
            logger.info("YouTube cookie refresh loop cancelled")
            break
        except Exception as e:
            if SHUTDOWN_FLAG:
                break
            logger.error(f"Ошибка в цикле обновления YouTube cookies: {e}")
        
        # Ждём перед следующим обновлением
        try:
            await asyncio.sleep(YOUTUBE_REFRESH_INTERVAL)
        except asyncio.CancelledError:
            logger.info("YouTube cookie refresh loop cancelled during sleep")
            break


async def refresh_instagram_cookies():
    """Обновляет Instagram cookies через Playwright."""
    global IG_CONTEXT, IG_PLAYWRIGHT_READY, INSTAGRAM_COOKIES_LAST_REFRESH, INSTAGRAM_SESSION_ID
    
    if not IG_PLAYWRIGHT_READY or not IG_CONTEXT:
        logger.warning("Instagram Playwright не готов для обновления cookies")
        return False
    
    page = None
    try:
        logger.info("Обновление Instagram cookies...")
        page = await IG_CONTEXT.new_page()
        
        # Устанавливаем мобильный viewport
        await page.set_viewport_size({"width": 375, "height": 812})
        
        # Заходим на Instagram главную
        await page.goto("https://www.instagram.com/", wait_until='domcontentloaded', timeout=30000)
        await page.wait_for_timeout(3000)
        
        # Принимаем cookies политику если есть
        cookie_accept_selectors = [
            'button[tabindex="0"]:has-text("Allow")',
            'button:has-text("Accept")',
            'button:has-text("Allow essential and optional cookies")',
            '[role="button"]:has-text("Accept")',
        ]
        
        for selector in cookie_accept_selectors:
            try:
                btn = page.locator(selector).first
                if await btn.is_visible(timeout=1000):
                    await btn.click()
                    logger.info(f"Нажата кнопка cookie consent: {selector[:50]}")
                    await page.wait_for_timeout(1000)
                    break
            except Exception:
                continue
        
        # Ждём загрузки страницы
        await page.wait_for_timeout(2000)
        
        # Переходим на какой-нибудь публичный Reel для активации сессии
        test_urls = [
            "https://www.instagram.com/reels/",
            "https://www.instagram.com/explore/",
        ]
        
        for test_url in test_urls:
            try:
                await page.goto(test_url, wait_until='domcontentloaded', timeout=15000)
                await page.wait_for_timeout(3000)
                break
            except Exception:
                continue
        
        # Извлекаем cookies
        cookies = await page.context.cookies()
        
        # Ищем sessionid и другие важные cookies
        session_id = None
        important_cookies = []
        for cookie in cookies:
            domain = cookie.get('domain', '')
            if 'instagram' in domain:
                important_cookies.append(cookie)
                if cookie.get('name') == 'sessionid':
                    session_id = cookie.get('value')
                    INSTAGRAM_SESSION_ID = session_id
                    logger.info(f"Instagram sessionid получен: {session_id[:15]}...")
        
        if important_cookies:
            # Сохраняем cookies в файл для yt-dlp
            cookie_file = "cookies_instagram.txt"
            one_year_from_now = int(time.time()) + 365 * 24 * 60 * 60
            with open(cookie_file, 'w', encoding='utf-8') as f:
                f.write("# Netscape HTTP Cookie File\n")
                for cookie in important_cookies:
                    domain = cookie.get('domain', '')
                    flag = 'TRUE' if domain.startswith('.') else 'FALSE'
                    path = cookie.get('path', '/')
                    secure = 'TRUE' if cookie.get('secure') else 'FALSE'
                    raw_expires = cookie.get('expires')
                    if raw_expires and isinstance(raw_expires, (int, float)) and raw_expires > time.time():
                        expires = int(raw_expires)
                    else:
                        expires = one_year_from_now
                    name = cookie.get('name', '')
                    value = cookie.get('value', '')
                    if name and value:
                        f.write(f"{domain}\t{flag}\t{path}\t{secure}\t{expires}\t{name}\t{value}\n")
            
            INSTAGRAM_COOKIES_LAST_REFRESH = datetime.now()
            logger.info(f"Instagram cookies сохранены ({len(important_cookies)} cookies)")
            return True
        else:
            logger.warning("Не удалось получить Instagram cookies")
            return False
            
    except Exception as e:
        logger.error(f"Ошибка обновления Instagram cookies: {e}")
        return False
    finally:
        if page:
            try:
                await page.close()
            except Exception:
                pass


async def instagram_cookie_refresh_loop():
    """Фоновый цикл обновления Instagram cookies."""
    global INSTAGRAM_REFRESH_INTERVAL, SHUTDOWN_FLAG
    
    # Первоначальная задержка для инициализации Playwright
    await asyncio.sleep(15)
    
    while not SHUTDOWN_FLAG:
        try:
            if SHUTDOWN_FLAG:
                break
            await refresh_instagram_cookies()
        except asyncio.CancelledError:
            logger.info("Instagram cookie refresh loop cancelled")
            break
        except Exception as e:
            if SHUTDOWN_FLAG:
                break
            logger.error(f"Ошибка в цикле обновления Instagram cookies: {e}")
        
        # Ждём перед следующим обновлением
        try:
            await asyncio.sleep(INSTAGRAM_REFRESH_INTERVAL)
        except asyncio.CancelledError:
            logger.info("Instagram cookie refresh loop cancelled during sleep")
            break

async def download_youtube(url: str, quality: str = "720p") -> Optional[str]:
    """Скачивание с YouTube через yt-dlp + внешние API."""
    global YOUTUBE_VISITOR_DATA, YOUTUBE_PO_TOKEN, YOUTUBE_COOKIES_LAST_REFRESH
    
    logger.info(f"Скачивание YouTube: {url[:60]}... (качество={quality})")
    
    # Проверяем возраст cookies - обновляем если старше 25 минут
    if YOUTUBE_COOKIES_LAST_REFRESH:
        age_seconds = (datetime.now() - YOUTUBE_COOKIES_LAST_REFRESH).total_seconds()
        if age_seconds > 1500:  # 25 минут
            logger.info(f"Cookies устарели ({age_seconds/60:.1f} мин) - обновляем...")
            await refresh_youtube_visitor_data()
    elif YOUTUBE_COOKIES_LAST_REFRESH is None:
        await refresh_youtube_visitor_data()
    
    # =============== МЕТОД 1: yt-dlp с cookies ===============
    async def _try_ydl() -> Optional[str]:
        ydl_opts = get_ydl_opts(quality, use_youtube_cookies=True)
        
        # Добавляем visitor_data и PO Token если есть
        ydl_opts['extractor_args'] = ydl_opts.get('extractor_args') or {}
        ydl_opts['extractor_args']['youtube'] = ydl_opts['extractor_args'].get('youtube') or {}
        
        if YOUTUBE_VISITOR_DATA:
            ydl_opts['extractor_args']['youtube']['visitor_data'] = [YOUTUBE_VISITOR_DATA]
        
        # Добавляем PO Token для обхода детекции бота
        # Формат: web.gvs+TOKEN или mweb.gvs+TOKEN
        if YOUTUBE_PO_TOKEN:
            # mweb.gvs для мобильного клиента который обычно лучше работает
            po_token_value = f"mweb.gvs+{YOUTUBE_PO_TOKEN}"
            ydl_opts['extractor_args']['youtube']['po_token'] = [po_token_value]
            logger.debug(f"Используем PO Token для yt-dlp")
        
        try:
            return await asyncio.to_thread(_ydl_download_path, url, ydl_opts)
        except Exception as e:
            if "Impersonate target" in str(e) and "not available" in str(e) and ydl_opts.get('impersonate'):
                ydl_opts_retry = dict(ydl_opts)
                ydl_opts_retry.pop('impersonate', None)
                return await asyncio.to_thread(_ydl_download_path, url, ydl_opts_retry)
            raise
    
    # Паттерны ошибок блокировки
    BLOCK_PATTERNS = ["Sign in", "bot", "confirm you're not", "age-restricted", 
                      "login required", "blocked", "403", "HTTP Error 403"]
    
    def _is_block_error(error: Exception) -> bool:
        err_str = str(error).lower()
        return any(p.lower() in err_str for p in BLOCK_PATTERNS)
    
    # Попытка 1: yt-dlp
    try:
        result = await _try_ydl()
        if result:
            logger.info("YouTube скачан через yt-dlp")
            return result
    except Exception as e:
        logger.warning(f"yt-dlp ошибка: {str(e)[:100]}")
        
        # Если блокировка - обновляем cookies и пробуем ещё раз
        if _is_block_error(e):
            logger.info("Блокировка! Обновляем cookies...")
            if await refresh_youtube_visitor_data():
                try:
                    result = await _try_ydl()
                    if result:
                        logger.info("YouTube скачан после обновления cookies!")
                        return result
                except Exception as e2:
                    logger.warning(f"yt-dlp retry ошибка: {str(e2)[:80]}")
    
    # =============== МЕТОД 2: Внешние API ===============
    
    # Извлекаем video_id
    video_id = None
    import re
    patterns = [
        r'(?:youtube\.com/watch\?v=|youtu\.be/|youtube\.com/shorts/)([a-zA-Z0-9_-]{11})',
        r'(?:youtube\.com/embed/)([a-zA-Z0-9_-]{11})',
    ]
    for pattern in patterns:
        match = re.search(pattern, url)
        if match:
            video_id = match.group(1)
            break
    
    if not video_id:
        logger.error("Не удалось извлечь video_id")
        return None
    
    # API 1: Cobalt.tools (v7 API)
    async def try_cobalt() -> Optional[str]:
        logger.info("Пробуем Cobalt API...")
        async with aiohttp.ClientSession() as session:
            try:
                payload = {
                    "url": f"https://www.youtube.com/watch?v={video_id}",
                    "downloadMode": "auto",
                    "filenameStyle": "basic",
                    "videoQuality": "720" if quality == "720p" else "1080" if quality == "1080p" else "max",
                }
                
                # Cobalt v7 API требует специальные заголовки
                headers = {
                    "Accept": "application/json",
                    "Content-Type": "application/json",
                    "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 Chrome/120.0.0.0",
                }
                
                # Актуальные эндпоинты Cobalt
                endpoints = [
                    ("https://api.cobalt.tools/", {"Accept": "application/json", "Content-Type": "application/json"}),
                    ("https://cobalt-api.hyper.lol/", {"Accept": "application/json", "Content-Type": "application/json"}),
                    ("https://co.wuk.sh/api/json", {"Accept": "application/json", "Content-Type": "application/json"}),
                ]
                
                for endpoint, extra_headers in endpoints:
                    try:
                        req_headers = {**headers, **extra_headers}
                        async with session.post(
                            endpoint,
                            json=payload,
                            headers=req_headers,
                            timeout=aiohttp.ClientTimeout(total=30)
                        ) as resp:
                            resp_text = await resp.text()
                            logger.debug(f"Cobalt {endpoint} status={resp.status}")
                            
                            if resp.status == 200:
                                try:
                                    data = json.loads(resp_text)
                                except:
                                    continue
                                
                                status = data.get("status")
                                
                                # Обработка разных типов ответов Cobalt v7
                                if status == "tunnel" or status == "redirect":
                                    download_url = data.get("url")
                                elif status == "picker":
                                    # Picker возвращает список вариантов
                                    picker = data.get("picker", [])
                                    if picker:
                                        download_url = picker[0].get("url")
                                    else:
                                        download_url = None
                                else:
                                    # Старый формат
                                    download_url = data.get("url") or data.get("stream") or data.get("audio")
                                
                                if download_url:
                                    logger.info(f"Cobalt вернул URL ({status}), скачиваем...")
                                    async with session.get(download_url, timeout=aiohttp.ClientTimeout(total=300)) as dl_resp:
                                        if dl_resp.status == 200:
                                            temp_dir = tempfile.mkdtemp()
                                            temp_file = os.path.join(temp_dir, f"{video_id}.mp4")
                                            with open(temp_file, 'wb') as f:
                                                async for chunk in dl_resp.content.iter_chunked(8192):
                                                    f.write(chunk)
                                            if os.path.getsize(temp_file) > 10000:
                                                logger.info("Скачано через Cobalt!")
                                                return temp_file
                                else:
                                    logger.debug(f"Cobalt ответ: status={status}, данные: {resp_text[:200]}")
                            elif resp.status == 400:
                                logger.debug(f"Cobalt 400: {resp_text[:200]}")
                    except Exception as ep_e:
                        logger.debug(f"Cobalt endpoint {endpoint} ошибка: {ep_e}")
                        continue
            except Exception as e:
                logger.warning(f"Cobalt общая ошибка: {e}")
        return None
    
    # API 2: RapidSave
    async def try_rapidsave() -> Optional[str]:
        logger.info("Пробуем RapidSave API...")
        async with aiohttp.ClientSession() as session:
            try:
                api_url = "https://rapidsave.com/api/ajax/download"
                headers = {
                    "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) Chrome/120.0.0.0",
                    "Content-Type": "application/x-www-form-urlencoded",
                    "Origin": "https://rapidsave.com",
                    "Referer": "https://rapidsave.com/",
                }
                data = {"url": f"https://www.youtube.com/watch?v={video_id}"}
                async with session.post(api_url, data=data, headers=headers, timeout=aiohttp.ClientTimeout(total=30)) as resp:
                    if resp.status == 200:
                        result = await resp.json()
                        logger.debug(f"RapidSave response: {str(result)[:200]}")
                        download_url = result.get("url") or result.get("download_url")
                        if download_url:
                            async with session.get(download_url, timeout=aiohttp.ClientTimeout(total=300)) as dl_resp:
                                if dl_resp.status == 200:
                                    temp_dir = tempfile.mkdtemp()
                                    temp_file = os.path.join(temp_dir, f"{video_id}.mp4")
                                    with open(temp_file, 'wb') as f:
                                        async for chunk in dl_resp.content.iter_chunked(8192):
                                            f.write(chunk)
                                    if os.path.getsize(temp_file) > 10000:
                                        logger.info("Скачано через RapidSave!")
                                        return temp_file
                    else:
                        logger.debug(f"RapidSave status {resp.status}")
            except Exception as e:
                logger.warning(f"RapidSave ошибка: {e}")
        return None
    
    # API 3: Y2mate (улучшенный)
    async def try_y2mate() -> Optional[str]:
        logger.info("Пробуем Y2mate API...")
        async with aiohttp.ClientSession() as session:
            try:
                # Шаг 1: Analyze
                analyze_url = "https://www.y2mate.com/mates/analyzeV2/ajax"
                form_data = {"k_query": url, "k_page": "home", "hl": "en", "q_auto": "1"}
                headers = {
                    "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) Chrome/120.0.0.0",
                    "Content-Type": "application/x-www-form-urlencoded",
                    "Origin": "https://www.y2mate.com",
                    "Referer": "https://www.y2mate.com/",
                }
                async with session.post(analyze_url, data=form_data, headers=headers, timeout=aiohttp.ClientTimeout(total=30)) as resp:
                    if resp.status != 200:
                        logger.debug(f"Y2mate analyze status {resp.status}")
                        return None
                    result = await resp.json()
                    logger.debug(f"Y2mate analyze: {str(result)[:200]}")
                    
                    vid = result.get("vid")
                    links = result.get("links", {}).get("mp4", {})
                    
                    if not links:
                        logger.debug("Y2mate: нет mp4 ссылок")
                        return None
                    
                    # Ищем подходящее качество (720p, 480p, 360p)
                    target_key = None
                    for key, info in links.items():
                        q = str(info.get("q", ""))
                        if any(x in q for x in ["720", "480", "360"]):
                            target_key = key
                            break
                    
                    if not target_key:
                        # Берём первый доступный
                        target_key = list(links.keys())[0] if links else None
                    
                    if not target_key or not vid:
                        logger.debug("Y2mate: не найден ключ или vid")
                        return None
                    
                    # Шаг 2: Convert
                    convert_url = "https://www.y2mate.com/mates/convertV2/index"
                    convert_data = {"vid": vid, "k": target_key}
                    async with session.post(convert_url, data=convert_data, headers=headers, timeout=aiohttp.ClientTimeout(total=60)) as conv_resp:
                        if conv_resp.status != 200:
                            logger.debug(f"Y2mate convert status {conv_resp.status}")
                            return None
                        conv_result = await conv_resp.json()
                        logger.debug(f"Y2mate convert: {str(conv_result)[:200]}")
                        
                        download_url = conv_result.get("dlink")
                        if not download_url:
                            logger.debug("Y2mate: нет dlink")
                            return None
                        
                        # Скачиваем
                        async with session.get(download_url, headers={"User-Agent": headers["User-Agent"]}, timeout=aiohttp.ClientTimeout(total=300)) as dl_resp:
                            if dl_resp.status == 200:
                                temp_dir = tempfile.mkdtemp()
                                temp_file = os.path.join(temp_dir, f"{video_id}.mp4")
                                with open(temp_file, 'wb') as f:
                                    async for chunk in dl_resp.content.iter_chunked(8192):
                                        f.write(chunk)
                                if os.path.getsize(temp_file) > 10000:
                                    logger.info("Скачано через Y2mate!")
                                    return temp_file
                            else:
                                logger.debug(f"Y2mate download status {dl_resp.status}")
            except Exception as e:
                logger.warning(f"Y2mate ошибка: {e}")
        return None
    
    # API 4: SnapSave
    async def try_snapsave() -> Optional[str]:
        logger.info("Пробуем SnapSave API...")
        async with aiohttp.ClientSession() as session:
            try:
                api_url = "https://snapsave.io/action.php"
                headers = {
                    "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) Chrome/120.0.0.0",
                    "Content-Type": "application/x-www-form-urlencoded",
                    "Origin": "https://snapsave.io",
                }
                data = {"url": f"https://www.youtube.com/watch?v={video_id}"}
                async with session.post(api_url, data=data, headers=headers, timeout=aiohttp.ClientTimeout(total=30)) as resp:
                    if resp.status == 200:
                        result = await resp.text()
                        logger.debug(f"SnapSave response: {result[:200]}")
                        # Парсим HTML ответ для поиска ссылок
                        import re
                        urls = re.findall(r'href="(https://[^"]+\.mp4[^"]*)"', result)
                        if urls:
                            download_url = urls[0]
                            async with session.get(download_url, timeout=aiohttp.ClientTimeout(total=300)) as dl_resp:
                                if dl_resp.status == 200:
                                    temp_dir = tempfile.mkdtemp()
                                    temp_file = os.path.join(temp_dir, f"{video_id}.mp4")
                                    with open(temp_file, 'wb') as f:
                                        async for chunk in dl_resp.content.iter_chunked(8192):
                                            f.write(chunk)
                                    if os.path.getsize(temp_file) > 10000:
                                        logger.info("Скачано через SnapSave!")
                                        return temp_file
            except Exception as e:
                logger.warning(f"SnapSave ошибка: {e}")
        return None
    
    # API 5: pytubefix (не зависит от yt-dlp)
    async def try_pytubefix() -> Optional[str]:
        logger.info("Пробуем pytubefix...")
        try:
            from pytubefix import YouTube
            from pytubefix.cli import on_progress
            
            def _download_with_pytubefix():
                yt = YouTube(url, on_progress_callback=on_progress)
                
                # Выбираем качество
                if quality == "audio":
                    stream = yt.streams.filter(only_audio=True).first()
                else:
                    # Сначала пробуем progressive (видео+аудио вместе)
                    stream = yt.streams.filter(progressive=True, file_extension='mp4').order_by('resolution').desc().first()
                    
                    if not stream:
                        # Если нет progressive, берём adaptive
                        stream = yt.streams.filter(adaptive=True, file_extension='mp4').order_by('resolution').desc().first()
                
                if stream:
                    temp_dir = tempfile.mkdtemp()
                    output_path = stream.download(output_path=temp_dir)
                    if output_path and os.path.exists(output_path) and os.path.getsize(output_path) > 10000:
                        return output_path
                return None
            
            result = await asyncio.to_thread(_download_with_pytubefix)
            if result:
                logger.info("Скачано через pytubefix!")
                return result
        except ImportError:
            logger.debug("pytubefix не установлен")
        except Exception as e:
            logger.warning(f"pytubefix ошибка: {e}")
        return None
    
    # API 6: Invidious (бесплатные инстансы)
    async def try_invidious() -> Optional[str]:
        logger.info("Пробуем Invidious API...")
        # Список рабочих инстансов Invidious
        invidious_instances = [
            "https://invidious.fdn.fr",
            "https://inv.nadeko.net",
            "https://invidious.protokolla.fi",
            "https://vid.puffyan.us",
            "https://yewtu.be",
        ]
        
        async with aiohttp.ClientSession() as session:
            for instance in invidious_instances:
                try:
                    # Получаем информацию о видео
                    api_url = f"{instance}/api/v1/videos/{video_id}"
                    async with session.get(
                        api_url,
                        timeout=aiohttp.ClientTimeout(total=15),
                        headers={"User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) Chrome/120.0.0.0"}
                    ) as resp:
                        if resp.status != 200:
                            continue
                        
                        data = await resp.json()
                        
                        # Ищем подходящий формат для скачивания
                        adaptive_formats = data.get("adaptiveFormats", [])
                        format_streams = data.get("formatStreams", [])
                        
                        download_url = None
                        
                        # Сначала ищем в formatStreams (progressive - видео+аудио)
                        for fmt in format_streams:
                            quality_label = fmt.get("qualityLabel", "")
                            if "720" in quality_label or "480" in quality_label or "360" in quality_label:
                                download_url = fmt.get("url")
                                if download_url:
                                    break
                        
                        # Если не нашли, берём первый доступный
                        if not download_url and format_streams:
                            download_url = format_streams[0].get("url")
                        
                        if download_url:
                            logger.info(f"Invidious ({instance}) вернул URL, скачиваем...")
                            async with session.get(
                                download_url,
                                timeout=aiohttp.ClientTimeout(total=300),
                                headers={"User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) Chrome/120.0.0.0"}
                            ) as dl_resp:
                                if dl_resp.status == 200:
                                    temp_dir = tempfile.mkdtemp()
                                    temp_file = os.path.join(temp_dir, f"{video_id}.mp4")
                                    with open(temp_file, 'wb') as f:
                                        async for chunk in dl_resp.content.iter_chunked(8192):
                                            f.write(chunk)
                                    if os.path.getsize(temp_file) > 10000:
                                        logger.info(f"Скачано через Invidious ({instance})!")
                                        return temp_file
                except Exception as e:
                    logger.debug(f"Invidious {instance} ошибка: {e}")
                    continue
        return None
    
    # Пробуем pytubefix первым (самый надёжный)
    result = await try_pytubefix()
    if result:
        return result
    
    # Пробуем внешние API
    for api_func in [try_cobalt, try_rapidsave, try_y2mate, try_snapsave]:
        try:
            result = await api_func()
            if result:
                return result
        except Exception as e:
            logger.debug(f"API ошибка: {e}")
            continue
    
    logger.error("Все методы скачивания YouTube исчерпаны")
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

        # Не используем куки, так как пользователь просил их убрать
        ydl_opts = get_ydl_opts(quality, use_youtube_cookies=False)

        try:
            temp_file = await asyncio.to_thread(_ydl_download_path, url, ydl_opts)
            if temp_file:
                logger.info(f"Видео скачано через Playwright")
                return temp_file
        except Exception as e:
            if "Impersonate target" in str(e) and "not available" in str(e) and ydl_opts.get('impersonate'):
                ydl_opts_retry = dict(ydl_opts)
                ydl_opts_retry.pop('impersonate', None)
                temp_file = await asyncio.to_thread(_ydl_download_path, url, ydl_opts_retry)
                if temp_file:
                    logger.info(f"Видео скачано через Playwright")
                    return temp_file
            raise

    except Exception as e:
        logger.error(f"Ошибка в Playwright: {e}")
    finally:
        if page:
            await page.close()
        if temp_cookies_file and temp_cookies_file.exists():
            temp_cookies_file.unlink(missing_ok=True)
    
    return None

async def download_rutube(url: str, quality: str = "720p") -> Optional[str]:
    logger.info(f"Скачивание с RuTube (качество={quality})...")

    ydl_opts = get_ydl_opts(quality, use_youtube_cookies=False)
    ydl_opts['http_headers'] = dict(ydl_opts.get('http_headers') or {})
    ydl_opts['http_headers']['Referer'] = 'https://rutube.ru/'

    try:
        temp_file = await asyncio.to_thread(_ydl_download_path, url, ydl_opts)
        if temp_file:
            logger.info("Видео RuTube скачано")
            return temp_file
    except Exception as e:
        if "Impersonate target" in str(e) and "not available" in str(e) and ydl_opts.get('impersonate'):
            try:
                ydl_opts_retry = dict(ydl_opts)
                ydl_opts_retry.pop('impersonate', None)
                temp_file = await asyncio.to_thread(_ydl_download_path, url, ydl_opts_retry)
                if temp_file:
                    logger.info("Видео RuTube скачано")
                    return temp_file
            except Exception as e2:
                logger.error(f"Ошибка yt-dlp (RuTube): {e2}")
        else:
            logger.error(f"Ошибка yt-dlp (RuTube): {e}")

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
        temp_file = await asyncio.to_thread(_ydl_download_path, url, ydl_opts)
        if temp_file:
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
        info = await asyncio.to_thread(_ydl_extract_info, url, ydl_opts)
        temp_dir = info.get('id') if isinstance(info, dict) else None
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


# ==================== INSTAGRAM DOWNLOADER ====================

class InstagramDownloader:
    """Класс для скачивания Instagram контента (Reels, посты, фото)."""
    
    # HTTP заголовки для запросов
    HEADERS = {
        'User-Agent': 'Mozilla/5.0 (iPhone; CPU iPhone OS 17_0 like Mac OS X) AppleWebKit/605.1.15 (KHTML, like Gecko) Version/17.0 Mobile/15E148 Safari/604.1',
        'Accept': 'text/html,application/xhtml+xml,application/xml;q=0.9,*/*;q=0.8',
        'Accept-Language': 'en-US,en;q=0.5',
    }
    
    def __init__(self):
        self.logger = logging.getLogger('InstagramDownloader')
    
    async def download(self, url: str) -> Tuple[Optional[str], Optional[List[str]], str]:
        """
        Главная точка входа - скачивает контент с Instagram.
        
        Returns:
            Tuple[video_path, photo_paths, description]
        """
        self.logger.info(f"Начинаем скачивание Instagram: {url[:60]}...")
        
        # 1. Разворачиваем share-ссылки
        if '/share/' in url:
            expanded = await self._expand_share_url(url)
            if expanded:
                url = expanded
                self.logger.info(f"Share URL развёрнут: {url[:60]}...")
        
        # Определяем тип контента
        is_video_url = any(x in url.lower() for x in ['/reel/', '/reels/', '/tv/'])
        
        # 2. Пробуем методы последовательно
        methods = [
            ('yt-dlp', self._method_ytdlp),
            ('Embed API', self._method_embed),
            ('FastDL', self._method_fastdl),
            ('iGram', self._method_igram),
            ('Playwright', self._method_playwright),
        ]
        
        for name, method in methods:
            try:
                self.logger.info(f"Пробуем метод: {name}")
                result = await method(url)
                if result[0] or result[1]:  # video или photos
                    self.logger.info(f"Успех через {name}!")
                    return result
            except Exception as e:
                self.logger.warning(f"Метод {name} не сработал: {e}")
                continue
        
        self.logger.error("Все методы скачивания Instagram исчерпаны")
        return None, None, ""
    
    async def _expand_share_url(self, url: str) -> Optional[str]:
        """Разворачивает share-ссылку в полный URL."""
        import re
        import aiohttp
        
        try:
            async with aiohttp.ClientSession(headers=self.HEADERS) as session:
                async with session.get(url, allow_redirects=True, timeout=aiohttp.ClientTimeout(total=10)) as resp:
                    final_url = str(resp.url)
                    
                    # Очищаем от tracking параметров
                    final_url = re.sub(r'[?&]igsh=[^&]+', '', final_url)
                    final_url = re.sub(r'[?&]utm_[^&]+', '', final_url)
                    final_url = re.sub(r'\?$', '', final_url)
                    
                    if 'instagram.com' in final_url and any(x in final_url for x in ['/p/', '/reel/', '/tv/', '/reels/']):
                        return final_url
                    
                    # Пробуем найти в HTML
                    html = await resp.text()
                    match = re.search(r'instagram\.com/(reel|p|tv)/([A-Za-z0-9_-]+)', html)
                    if match:
                        return f"https://www.instagram.com/{match.group(1)}/{match.group(2)}/"
        except Exception as e:
            self.logger.warning(f"Ошибка разворачивания share URL: {e}")
        
        return None
    
    def _extract_shortcode(self, url: str) -> Optional[str]:
        """Извлекает shortcode из Instagram URL."""
        import re
        # Поддержка всех типов: /p/ (посты), /reel/, /reels/, /tv/
        match = re.search(r'/(p|reel|tv|reels)/([A-Za-z0-9_-]+)', url)
        return match.group(2) if match else None
    
    def _is_post_url(self, url: str) -> bool:
        """Проверяет, является ли URL постом (не видео)."""
        return '/p/' in url.lower() and '/reel' not in url.lower() and '/tv/' not in url.lower()
    
    async def _download_video(self, video_url: str, session: 'aiohttp.ClientSession' = None, extra_cookies: dict = None) -> Optional[str]:
        """Скачивает видео по прямой ссылке с поддержкой cookies."""
        import aiohttp
        global IG_CONTEXT
        
        headers = {
            'User-Agent': self.HEADERS['User-Agent'],
            'Referer': 'https://www.instagram.com/',
            'Origin': 'https://www.instagram.com',
            'Accept': '*/*',
        }
        
        # Собираем cookies
        cookie_str = ""
        if extra_cookies:
            cookie_str = "; ".join(f"{k}={v}" for k, v in extra_cookies.items())
        else:
            # Пробуем получить cookies из Playwright контекста
            try:
                if IG_CONTEXT:
                    cookies = await IG_CONTEXT.cookies()
                    ig_cookies = {c['name']: c['value'] for c in cookies if 'instagram' in c.get('domain', '')}
                    if ig_cookies:
                        cookie_str = "; ".join(f"{k}={v}" for k, v in ig_cookies.items())
            except Exception:
                pass
        
        if cookie_str:
            headers['Cookie'] = cookie_str
        
        close_session = False
        if session is None:
            session = aiohttp.ClientSession()
            close_session = True
        
        try:
            async with session.get(video_url, headers=headers, timeout=aiohttp.ClientTimeout(total=60)) as resp:
                if resp.status == 200:
                    content = await resp.read()
                    if len(content) > 10000:  # Минимум 10KB
                        temp_dir = tempfile.mkdtemp(prefix="ig_")
                        temp_file = os.path.join(temp_dir, "video.mp4")
                        with open(temp_file, 'wb') as f:
                            f.write(content)
                        self.logger.info(f"Видео скачано: {len(content)} bytes")
                        return temp_file
                else:
                    self.logger.debug(f"Ошибка скачивания: HTTP {resp.status}")
        except Exception as e:
            self.logger.warning(f"Ошибка скачивания видео: {e}")
        finally:
            if close_session:
                await session.close()
        
        return None
    
    async def _download_photos(self, photo_urls: List[str], session: 'aiohttp.ClientSession' = None) -> Optional[List[str]]:
        """Скачивает фото по прямым ссылкам."""
        import aiohttp
        global IG_CONTEXT
        
        headers = {
            'User-Agent': self.HEADERS['User-Agent'],
            'Referer': 'https://www.instagram.com/',
            'Origin': 'https://www.instagram.com',
            'Accept': 'image/*,*/*',
        }
        
        # Собираем cookies
        cookie_str = ""
        try:
            if IG_CONTEXT:
                cookies = await IG_CONTEXT.cookies()
                ig_cookies = {c['name']: c['value'] for c in cookies if 'instagram' in c.get('domain', '')}
                if ig_cookies:
                    cookie_str = "; ".join(f"{k}={v}" for k, v in ig_cookies.items())
        except Exception:
            pass
        
        if cookie_str:
            headers['Cookie'] = cookie_str
        
        close_session = False
        if session is None:
            session = aiohttp.ClientSession()
            close_session = True
        
        downloaded_photos = []
        temp_dir = tempfile.mkdtemp(prefix="ig_photos_")
        
        try:
            for i, photo_url in enumerate(photo_urls):
                try:
                    self.logger.debug(f"Скачиваем фото {i+1}: {photo_url[:80]}...")
                    async with session.get(photo_url, headers=headers, timeout=aiohttp.ClientTimeout(total=30)) as resp:
                        self.logger.debug(f"Фото {i+1} статус: {resp.status}")
                        if resp.status == 200:
                            content = await resp.read()
                            self.logger.debug(f"Фото {i+1} размер: {len(content)} bytes")
                            if len(content) > 5000:  # Минимум 5KB для фото
                                # Определяем расширение
                                content_type = resp.headers.get('content-type', '')
                                if 'jpeg' in content_type or 'jpg' in content_type:
                                    ext = '.jpg'
                                elif 'png' in content_type:
                                    ext = '.png'
                                elif 'webp' in content_type:
                                    ext = '.webp'
                                else:
                                    ext = '.jpg'  # По умолчанию
                                
                                temp_file = os.path.join(temp_dir, f"photo_{i+1}{ext}")
                                with open(temp_file, 'wb') as f:
                                    f.write(content)
                                downloaded_photos.append(temp_file)
                                self.logger.debug(f"Фото {i+1} скачано: {len(content)} bytes")
                            else:
                                self.logger.warning(f"Фото {i+1} слишком маленькое: {len(content)} bytes (минимум 5000)")
                        else:
                            self.logger.warning(f"Фото {i+1} HTTP ошибка: {resp.status}")
                except Exception as e:
                    self.logger.warning(f"Ошибка скачивания фото {i+1}: {e}")
            
            if downloaded_photos:
                self.logger.info(f"Скачано {len(downloaded_photos)} фото")
                return downloaded_photos
            else:
                self.logger.warning(f"Не удалось скачать ни одного фото из {len(photo_urls)}")
        finally:
            if close_session:
                await session.close()
        
        return None
    
    # ==================== МЕТОД 1: YT-DLP ====================
    
    async def _method_ytdlp(self, url: str) -> Tuple[Optional[str], Optional[List[str]], str]:
        """Скачивание через yt-dlp с поддержкой cookies."""
        temp_dir = tempfile.mkdtemp(prefix="ig_ytdlp_")
        
        ydl_opts = {
            'format': 'best[ext=mp4]/best',
            'outtmpl': os.path.join(temp_dir, '%(id)s.%(ext)s'),
            'noplaylist': True,
            'quiet': True,
            'no_warnings': True,
            'ignoreerrors': False,
            'extract_flat': False,
            'socket_timeout': 15,
            'retries': 3,
            'http_headers': {
                'User-Agent': self.HEADERS['User-Agent'],
                'Referer': 'https://www.instagram.com/',
            }
        }
        
        # Добавляем cookies если есть
        cookie_file = "cookies_instagram.txt"
        if os.path.exists(cookie_file) and os.path.getsize(cookie_file) > 0:
            ydl_opts['cookiefile'] = cookie_file
            self.logger.debug(f"Используем Instagram cookies из {cookie_file}")
        
        try:
            info = await asyncio.to_thread(self._ytdlp_extract, url, ydl_opts)
            
            if isinstance(info, dict):
                description = info.get('description', '') or info.get('title', '')
            else:
                description = ""
            
            # Ищем скачанные файлы
            files = [os.path.join(temp_dir, f) for f in os.listdir(temp_dir)]
            
            # Видео
            video_exts = ('.mp4', '.mkv', '.webm', '.mov')
            for f in files:
                if f.lower().endswith(video_exts) and os.path.getsize(f) > 10000:
                    return f, None, description
            
            # Фото
            photo_exts = ('.jpg', '.jpeg', '.png', '.webp')
            photos = [f for f in files if f.lower().endswith(photo_exts)]
            if photos:
                return None, sorted(photos), description
                
        except Exception as e:
            self.logger.debug(f"yt-dlp не сработал: {e}")
            # Очистка при ошибке
            try:
                import shutil
                shutil.rmtree(temp_dir, ignore_errors=True)
            except:
                pass
        
        return None, None, ""
    
    def _ytdlp_extract(self, url: str, opts: dict):
        """Синхронная обёртка для yt-dlp."""
        with yt_dlp.YoutubeDL(opts) as ydl:
            return ydl.extract_info(url, download=True)
    
    # ==================== МЕТОД 2: EMBED API ====================
    
    def _get_best_photo_urls(self, photo_urls: List[str]) -> List[str]:
        """Выбирает лучшие (полноразмерные) версии фото, убирая дубликаты и миниатюры."""
        import re
        
        # Группируем по базовому ID изображения
        # Instagram URL формат: https://scontent.../hash_n.jpg?...
        url_groups = {}
        
        for url in photo_urls:
            # Извлекаем hash изображения (уникальный идентификатор)
            # Пример: 123456789_123456789_123456789_n.jpg
            hash_match = re.search(r'/([a-zA-Z0-9_]+)_n\.(jpg|jpeg|png|webp)', url)
            if hash_match:
                img_hash = hash_match.group(1)
            else:
                # Fallback - используем часть URL как ID
                img_hash = url.split('/')[-1].split('?')[0]
            
            # Определяем размер изображения из URL
            size = 0
            size_match = re.search(r'/s(\d+)x\d+/', url)
            if size_match:
                size = int(size_match.group(1))
            elif '/p1080x1080/' in url or '/e35/' in url:
                size = 1080
            elif '/p640x640/' in url:
                size = 640
            elif '/p320x320/' in url:
                size = 320
            elif '/p150x150/' in url:
                size = 150
            else:
                # Если размер не указан, предполагаем полный размер
                size = 1440
            
            # Сохраняем URL с наибольшим размером для каждого hash
            if img_hash not in url_groups or url_groups[img_hash][1] < size:
                url_groups[img_hash] = (url, size)
        
        # Возвращаем только URL с минимальным размером 640
        best_urls = []
        for img_hash, (url, size) in url_groups.items():
            if size >= 640:
                best_urls.append(url)
        
        self.logger.debug(f"Отфильтровано {len(photo_urls)} -> {len(best_urls)} фото (удалены миниатюры)")
        return best_urls
    
    async def _method_embed(self, url: str) -> Tuple[Optional[str], Optional[List[str]], str]:
        """Скачивание через Instagram Embed страницу (видео и фото)."""
        import re
        import aiohttp
        
        shortcode = self._extract_shortcode(url)
        if not shortcode:
            return None, None, ""
        
        # Пробуем оба варианта embed
        embed_urls = [
            f"https://www.instagram.com/p/{shortcode}/embed/",
            f"https://www.instagram.com/reel/{shortcode}/embed/captioned/",
        ]
        
        async with aiohttp.ClientSession() as session:
            for embed_url in embed_urls:
                try:
                    async with session.get(embed_url, headers=self.HEADERS, timeout=aiohttp.ClientTimeout(total=15)) as resp:
                        if resp.status != 200:
                            continue
                        
                        html = await resp.text()
                        
                        # Ищем video_url в JSON
                        video_patterns = [
                            r'"video_url"\s*:\s*"([^"]+)"',
                            r'"contentUrl"\s*:\s*"([^"]+)"',
                        ]
                        
                        for pattern in video_patterns:
                            matches = re.findall(pattern, html)
                            for match in matches:
                                video_url = match.replace('\\u0026', '&').replace('\\/', '/')
                                if 'mp4' in video_url.lower() or 'video' in video_url.lower():
                                    video_path = await self._download_video(video_url, session)
                                    if video_path:
                                        return video_path, None, ""
                        
                        # Ищем фото URL в embed - приоритет display_url (полноразмерные)
                        photo_patterns = [
                            # Высокий приоритет - полноразмерные
                            r'"display_url"\s*:\s*"([^"]+)"',
                            r'"display_src"\s*:\s*"([^"]+)"',
                            # Карусели - edge_sidecar_to_children содержит все фото
                            r'"display_resources"\s*:\s*\[.*?"src"\s*:\s*"([^"]+)".*?\]',
                        ]
                        
                        photo_urls = []
                        for pattern in photo_patterns:
                            matches = re.findall(pattern, html)
                            for match in matches:
                                photo_url = match.replace('\\u0026', '&').replace('\\/', '/')
                                # Фильтруем только scontent (CDN Instagram)
                                if 'scontent' in photo_url and photo_url not in photo_urls:
                                    # Пропускаем явные миниатюры
                                    if '/s150x150/' not in photo_url and '/s320x320/' not in photo_url:
                                        photo_urls.append(photo_url)
                        
                        # Дополнительно ищем все scontent URLs с высоким разрешением
                        all_scontent = re.findall(r'https://scontent[^"\\]+(?:\.jpg|\.jpeg|\.png|\.webp)[^"\\]*', html)
                        for url in all_scontent:
                            clean_url = url.replace('\\u0026', '&').replace('\\/', '/')
                            # Только высокое разрешение
                            if any(x in clean_url for x in ['/p1080x1080/', '/e35/', '/s1080x1080/', '/s1440x1440/']):
                                if clean_url not in photo_urls:
                                    photo_urls.append(clean_url)
                        
                        if photo_urls:
                            # Фильтруем дубликаты и миниатюры
                            best_photos = self._get_best_photo_urls(photo_urls)
                            if best_photos:
                                self.logger.info(f"Найдено {len(best_photos)} полноразмерных фото в embed")
                                photos = await self._download_photos(best_photos, session)
                                if photos:
                                    return None, photos, ""
                except Exception as e:
                    self.logger.debug(f"Embed {embed_url} не сработал: {e}")
        
        return None, None, ""

    
    # ==================== МЕТОД 3: FASTDL ====================
    
    async def _method_fastdl(self, url: str) -> Tuple[Optional[str], Optional[List[str]], str]:
        """Скачивание через FastDL.app API (видео и фото)."""
        import re
        import aiohttp
        
        api_headers = {
            'User-Agent': 'Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36',
            'Origin': 'https://fastdl.app',
            'Referer': 'https://fastdl.app/en',
        }
        
        async with aiohttp.ClientSession() as session:
            try:
                form_data = aiohttp.FormData()
                form_data.add_field('url', url)
                
                async with session.post('https://fastdl.app/api/convert', data=form_data, headers=api_headers, timeout=aiohttp.ClientTimeout(total=15)) as resp:
                    if resp.status != 200:
                        return None, None, ""
                    
                    data = await resp.json()
                    data_str = json.dumps(data)
                    
                    # Ищем video URL
                    video_url = None
                    if isinstance(data, dict):
                        video_url = data.get('url') or data.get('video_url') or data.get('download_url')
                    
                    if not video_url:
                        matches = re.findall(r'https://[^\s"\\]+\.mp4[^\s"\\]*', data_str)
                        if matches:
                            video_url = matches[0].replace('\\u0026', '&')
                    
                    if video_url:
                        video_path = await self._download_video(video_url, session)
                        if video_path:
                            return video_path, None, ""
                    
                    # Ищем photo URLs
                    photo_urls = []
                    photo_patterns = [
                        r'https://[^\s"\\]+scontent[^\s"\\]+\.(?:jpg|jpeg|png|webp)[^\s"\\]*',
                    ]
                    for pattern in photo_patterns:
                        matches = re.findall(pattern, data_str)
                        for m in matches:
                            photo_url = m.replace('\\u0026', '&').replace('\\/', '/')
                            if photo_url not in photo_urls and 'profile' not in photo_url.lower():
                                photo_urls.append(photo_url)
                    
                    if photo_urls:
                        self.logger.info(f"FastDL: найдено {len(photo_urls)} фото")
                        photos = await self._download_photos(photo_urls, session)
                        if photos:
                            return None, photos, ""
            except Exception as e:
                self.logger.debug(f"FastDL не сработал: {e}")
        
        return None, None, ""
    
    # ==================== МЕТОД 4: IGRAM ====================
    
    async def _method_igram(self, url: str) -> Tuple[Optional[str], Optional[List[str]], str]:
        """Скачивание через iGram.world API (видео и фото)."""
        import re
        import aiohttp
        
        api_headers = {
            'User-Agent': 'Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36',
            'Accept': 'application/json, text/plain, */*',
            'Content-Type': 'application/x-www-form-urlencoded',
            'Origin': 'https://igram.world',
            'Referer': 'https://igram.world/',
        }
        
        async with aiohttp.ClientSession() as session:
            try:
                form_data = aiohttp.FormData()
                form_data.add_field('url', url)
                form_data.add_field('locale', 'en')
                
                async with session.post('https://api.igram.world/api/convert', data=form_data, headers=api_headers, timeout=aiohttp.ClientTimeout(total=15)) as resp:
                    if resp.status != 200:
                        return None, None, ""
                    
                    data = await resp.json()
                    
                    # Ищем video URL
                    items = data if isinstance(data, list) else data.get('items', []) if isinstance(data, dict) else []
                    photo_urls = []
                    
                    for item in items:
                        if isinstance(item, dict):
                            item_url = item.get('url') or item.get('video_url') or item.get('image_url')
                            if item_url:
                                # Проверяем на видео
                                if 'mp4' in item_url or 'video' in item_url:
                                    video_path = await self._download_video(item_url, session)
                                    if video_path:
                                        return video_path, None, ""
                                # Собираем фото URL
                                elif any(ext in item_url.lower() for ext in ['.jpg', '.jpeg', '.png', '.webp']):
                                    if item_url not in photo_urls:
                                        photo_urls.append(item_url)
                    
                    # Если видео не найдено, скачиваем фото
                    if photo_urls:
                        self.logger.info(f"iGram: найдено {len(photo_urls)} фото")
                        photos = await self._download_photos(photo_urls, session)
                        if photos:
                            return None, photos, ""
            except Exception as e:
                self.logger.debug(f"iGram не сработал: {e}")
        
        return None, None, ""

    
    # ==================== МЕТОД 5: PLAYWRIGHT ====================
    
    async def _method_playwright(self, url: str) -> Tuple[Optional[str], Optional[List[str]], str]:
        """Скачивание через Playwright (fallback, поддержка видео и фото).
        
        Извлекает фото/видео напрямую из данных поста, а не перехватывает все сетевые запросы.
        Это исключает попадание рекомендаций, аватарок и других посторонних изображений.
        """
        import re
        global IG_CONTEXT, IG_PLAYWRIGHT_READY
        
        if not IG_PLAYWRIGHT_READY or not IG_CONTEXT:
            self.logger.info("Playwright не инициализирован, пропускаем")
            return None, None, ""
        
        page = None
        captured_video_urls: List[str] = []
        
        async def on_response(response):
            """Перехват только видео URL из сетевых запросов."""
            try:
                url_str = response.url.lower()
                content_type = response.headers.get('content-type', '')
                
                # Проверяем только на видео
                if 'video' in content_type.lower() or any(x in url_str for x in ['/o1/v/t16/', '/o1/v/t2/', '.mp4']):
                    if response.url not in captured_video_urls:
                        captured_video_urls.append(response.url)
                        self.logger.debug(f"Перехвачен video URL: {response.url[:80]}...")
            except:
                pass
        
        try:
            page = await IG_CONTEXT.new_page()
            page.on('response', on_response)
            
            await page.set_viewport_size({"width": 375, "height": 812})
            
            # Загружаем страницу
            await page.goto(url, wait_until='networkidle', timeout=20000)
            await page.wait_for_timeout(2000)
            
            # Пробуем активировать видео
            try:
                video_el = page.locator('video')
                if await video_el.count() > 0:
                    await video_el.first.click(timeout=2000)
                    await page.wait_for_timeout(2000)
            except:
                pass
            
            description = ""
            try:
                og_desc = page.locator('meta[property="og:description"]')
                if await og_desc.count() > 0:
                    description = await og_desc.first.get_attribute('content') or ""
            except:
                pass
            
            # Скачиваем перехваченное видео
            if captured_video_urls:
                # Приоритет: /o1/v/t16/ > /o1/v/t2/ > .mp4 > любой
                best_url = None
                for pattern in ['/o1/v/t16/', '/o1/v/t2/', '.mp4']:
                    for u in captured_video_urls:
                        if pattern in u.lower():
                            best_url = u
                            break
                    if best_url:
                        break
                
                if not best_url:
                    best_url = captured_video_urls[0]
                
                self.logger.info(f"Используем перехваченный video URL: {best_url[:60]}...")
                video_path = await self._download_video(best_url)
                if video_path:
                    return video_path, None, description
            
            # Fallback: og:video
            try:
                og_video = page.locator('meta[property="og:video"], meta[property="og:video:secure_url"]')
                if await og_video.count() > 0:
                    video_url = await og_video.first.get_attribute('content')
                    if video_url:
                        video_path = await self._download_video(video_url)
                        if video_path:
                            return video_path, None, description
            except:
                pass
            
            # ========== ИЗВЛЕЧЕНИЕ ФОТО ИЗ ДАННЫХ ПОСТА ==========
            # Вместо перехвата всех сетевых запросов, парсим данные поста напрямую
            post_photo_urls = []
            
            try:
                # Получаем HTML страницы
                html_content = await page.content()
                
                # Ищем JSON данные поста в различных местах
                # 1. window._sharedData (старый формат)
                shared_data_match = re.search(r'window\._sharedData\s*=\s*(\{.+?\});</script>', html_content)
                if shared_data_match:
                    try:
                        import json
                        shared_data = json.loads(shared_data_match.group(1))
                        media = shared_data.get('entry_data', {}).get('PostPage', [{}])[0].get('graphql', {}).get('shortcode_media', {})
                        
                        # Одиночное фото
                        if media.get('display_url'):
                            post_photo_urls.append(media['display_url'])
                        
                        # Карусель
                        edges = media.get('edge_sidecar_to_children', {}).get('edges', [])
                        for edge in edges:
                            node = edge.get('node', {})
                            if node.get('display_url') and not node.get('is_video'):
                                if node['display_url'] not in post_photo_urls:
                                    post_photo_urls.append(node['display_url'])
                    except Exception as e:
                        self.logger.debug(f"Ошибка парсинга _sharedData: {e}")
                
                # 2. Ищем display_url напрямую в HTML (более надёжно)
                if not post_photo_urls:
                    # Паттерн для display_url с полным размером
                    display_urls = re.findall(r'"display_url"\s*:\s*"(https://scontent[^"]+)"', html_content)
                    for url in display_urls:
                        clean_url = url.replace('\\u0026', '&').replace('\\/', '/')
                        # Берём только уникальные URL
                        if clean_url not in post_photo_urls:
                            post_photo_urls.append(clean_url)
                
                # 3. Ищем в meta тегах og:image (последний fallback)
                if not post_photo_urls:
                    og_images = re.findall(r'<meta[^>]+property="og:image"[^>]+content="([^"]+)"', html_content)
                    for img_url in og_images:
                        if 'scontent' in img_url and img_url not in post_photo_urls:
                            post_photo_urls.append(img_url)
            
            except Exception as e:
                self.logger.debug(f"Ошибка извлечения фото из HTML: {e}")
            
            # Скачиваем найденные фото поста
            if post_photo_urls:
                # Ограничиваем количество - обычно в посте не больше 10 фото
                # Это защита от случайного захвата рекомендаций
                max_photos = 10
                post_photo_urls = post_photo_urls[:max_photos]
                
                # Декодируем escaped Unicode символы в URL
                decoded_urls = []
                for url in post_photo_urls:
                    # Декодируем только известные escape-последовательности Instagram
                    # НЕ используем codecs.decode - он может сломать URL
                    decoded_url = url.replace('\\u0026', '&').replace('\\/', '/').replace('\\u003c', '<').replace('\\u003e', '>')
                    decoded_urls.append(decoded_url)
                
                self.logger.debug(f"Найденные photo URLs: {[u[:60] for u in decoded_urls]}")
                
                # Фильтруем дубликаты и миниатюры
                best_photos = self._get_best_photo_urls(decoded_urls)
                if best_photos:
                    self.logger.info(f"Извлечено {len(best_photos)} фото из данных поста")
                    photos = await self._download_photos(best_photos)
                    if photos:
                        return None, photos, description
                    else:
                        self.logger.warning(f"_download_photos вернул None для URL: {best_photos[0][:80]}...")
            
        except Exception as e:
            self.logger.error(f"Playwright ошибка: {e}")
        finally:
            if page:
                await page.close()
        
        return None, None, ""


# Глобальный экземпляр загрузчика
_instagram_downloader = InstagramDownloader()


async def download_instagram(url: str) -> Tuple[Optional[str], Optional[List[str]], str]:
    """Обёртка для обратной совместимости."""
    return await _instagram_downloader.download(url)


async def expand_instagram_share_url(url: str) -> Optional[str]:
    """Обёртка для обратной совместимости."""
    return await _instagram_downloader._expand_share_url(url)



async def upload_to_0x0(file_path: str) -> Optional[str]:
    url = (os.getenv('ZEROX0_URL') or 'https://0x0.st').strip()
    try:
        async with aiohttp.ClientSession() as session:
            with open(file_path, 'rb') as f:
                data = aiohttp.FormData()
                data.add_field('file', f, filename=Path(file_path).name)
                async with session.post(url, data=data, headers={'Accept': 'text/plain'}) as resp:
                    body_text = (await resp.text()).strip()
                    if resp.status == 200 and body_text.startswith('http'):
                        return body_text
                    logger.error(f"0x0.st ответил {resp.status}: {body_text[:500]}")
    except Exception as e:
        logger.error(f"Ошибка загрузки на 0x0.st: {e}")
    return None

async def upload_to_uguu(file_path: str) -> Optional[str]:
    url = (os.getenv('UGUU_URL') or 'https://uguu.se/upload').strip()
    try:
        async with aiohttp.ClientSession() as session:
            with open(file_path, 'rb') as f:
                data = aiohttp.FormData()
                data.add_field('files[]', f, filename=Path(file_path).name)
                async with session.post(url, data=data, headers={'Accept': 'application/json'}) as resp:
                    body_text = (await resp.text()).strip()
                    if resp.status != 200:
                        logger.error(f"uguu ответил {resp.status}: {body_text[:500]}")
                        return None
                    try:
                        payload = json.loads(body_text)
                    except Exception:
                        if body_text.startswith('http'):
                            return body_text.splitlines()[0].strip()
                        logger.error(f"uguu вернул не-JSON (Content-Type={resp.headers.get('Content-Type')}): {body_text[:500]}")
                        return None
                    files = payload.get('files')
                    if isinstance(files, list) and files:
                        file0 = files[0]
                        if isinstance(file0, dict):
                            url_value = file0.get('url') or file0.get('link')
                            if isinstance(url_value, str) and url_value.startswith('http'):
                                return url_value
    except Exception as e:
        logger.error(f"Ошибка загрузки на uguu: {e}")
    return None

async def upload_to_fileio(file_path: str) -> Optional[str]:
    """Загрузка на file.io"""
    url_candidates_raw = [
        (os.getenv('FILEIO_URL') or '').strip(),
        'https://file.io/',
        'https://www.file.io/',
    ]
    url_candidates = [u for u in url_candidates_raw if u]
    max_size = 50 * 1024 * 1024
    file_size = os.path.getsize(file_path)
    
    if file_size > max_size:
        link = await upload_to_0x0(file_path)
        if not link:
            link = await upload_to_uguu(file_path)
        if link:
            return link
        logger.error(f"Не удалось загрузить файл на 0x0.st и uguu")
        return None

    try:
        async with aiohttp.ClientSession() as session:
            for url in url_candidates:
                try:
                    with open(file_path, 'rb') as f:
                        data = aiohttp.FormData()
                        data.add_field('file', f, filename=Path(file_path).name)
                        async with session.post(url, data=data, headers={'Accept': 'application/json'}) as resp:
                            body_text = await resp.text()
                            if resp.status == 405:
                                logger.error(f"file.io ответил 405 на {url}: {body_text[:200]}")
                                continue
                            if resp.status != 200:
                                logger.error(f"file.io ответил {resp.status} на {url}: {body_text[:500]}")
                                continue
                            try:
                                response_json = json.loads(body_text)
                            except Exception:
                                logger.error(f"file.io вернул не-JSON на {url} (Content-Type={resp.headers.get('Content-Type')}): {body_text[:500]}")
                                continue
                            if response_json.get('success'):
                                fileio_link = response_json.get('link')
                                if fileio_link:
                                    logger.info(f"Файл загружен на file.io")
                                    return fileio_link
                except Exception as e:
                    logger.error(f"Ошибка загрузки на file.io ({url}): {e}")
    except Exception as e:
        logger.error(f"Ошибка загрузки на file.io: {e}")
    
    return None

async def fix_video_for_telegram(file_path: str) -> Optional[str]:
    """Исправляет метаданные видео для корректного воспроизведения в Telegram.
    
    Telegram может показывать видео как GIF (0:00 длительность) если:
    - Отсутствует moov atom в начале файла
    - Повреждены метаданные
    - Неправильный контейнер
    
    Эта функция использует ffmpeg для перепаковки видео с правильными метаданными.
    """
    import subprocess
    import shutil
    
    # Проверяем наличие ffmpeg
    ffmpeg_path = shutil.which('ffmpeg')
    if not ffmpeg_path:
        logger.warning("ffmpeg не найден в системе, пропускаем исправление метаданных")
        return file_path
    
    try:
        # Создаём временный файл для выходного видео
        temp_dir = os.path.dirname(file_path)
        fixed_file = os.path.join(temp_dir, "fixed_video.mp4")
        
        # ffmpeg команда для перепаковки с moov atom в начале
        cmd = [
            ffmpeg_path,
            '-y',  # Перезаписать без запроса
            '-i', file_path,
            '-c', 'copy',  # Копируем потоки без перекодирования (быстро)
            '-movflags', '+faststart',  # Переместить moov atom в начало
            '-f', 'mp4',
            fixed_file
        ]
        
        logger.info("Исправление метаданных видео через ffmpeg...")
        
        # Запускаем ffmpeg
        process = await asyncio.to_thread(
            subprocess.run,
            cmd,
            capture_output=True,
            timeout=60
        )
        
        if process.returncode == 0 and os.path.exists(fixed_file):
            fixed_size = os.path.getsize(fixed_file)
            if fixed_size > 10000:  # Минимум 10KB
                logger.info(f"Видео исправлено: {fixed_file} ({fixed_size} bytes)")
                # Удаляем оригинальный файл
                try:
                    os.remove(file_path)
                except Exception:
                    pass
                return fixed_file
            else:
                logger.warning(f"Исправленный файл слишком маленький: {fixed_size} bytes")
                try:
                    os.remove(fixed_file)
                except Exception:
                    pass
        else:
            stderr = process.stderr.decode('utf-8', errors='ignore') if process.stderr else ''
            logger.warning(f"ffmpeg вернул код {process.returncode}: {stderr[:200]}")
            
    except subprocess.TimeoutExpired:
        logger.warning("ffmpeg таймаут при исправлении видео")
    except Exception as e:
        logger.warning(f"Ошибка при исправлении видео: {e}")
    
    return file_path

async def send_video_or_message(chat_id: int, file_path: str, caption: str = ""):
    """Отправка видео или файла"""
    max_telegram_file_size = 50 * 1024 * 1024
    file_size = os.path.getsize(file_path)
    
    # Исправляем метаданные видео для корректного отображения в Telegram
    if file_path.lower().endswith(('.mp4', '.mkv', '.webm', '.mov')):
        fixed_path = await fix_video_for_telegram(file_path)
        if fixed_path and fixed_path != file_path:
            file_path = fixed_path
            file_size = os.path.getsize(file_path)
    
    if file_size > max_telegram_file_size:
        link = await upload_to_0x0(file_path)
        if not link:
            link = await upload_to_uguu(file_path)
        if link:
            await bot.send_message(chat_id, f"Файл слишком большой для Telegram.\nСсылка: {link}")
        else:
            await bot.send_message(chat_id, "Не удалось загрузить файл.")
    else:
        input_file = FSInputFile(file_path)
        try:
            await bot.send_video(chat_id=chat_id, video=input_file, caption=caption, supports_streaming=True)
        except TelegramBadRequest as e:
            if "Wrong type of the web page content" in str(e):
                try:
                    await bot.send_photo(chat_id=chat_id, photo=input_file, caption=caption)
                except TelegramBadRequest:
                    await bot.send_document(chat_id=chat_id, document=input_file, caption=caption)
            else:
                # Пробуем отправить как документ при любой другой ошибке
                try:
                    await bot.send_document(chat_id=chat_id, document=input_file, caption=caption)
                except Exception:
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
    
    is_new_referral = False
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
                    
                    # Активируем премиум сразу
                    referrer = users_data.get(referrer_id)
                    if referrer and user_id not in referrer.get('referrals_completed', []):
                        referrer.setdefault('referrals_completed', []).append(user_id)
                        activate_premium(referrer_id)
                        activate_premium(user_id)
                        save_users_data()
                        is_new_referral = True
                        logger.info(f"Пользователь {user_id} зарегистрирован по реферальной ссылке {referrer_id}. Премиум активирован для обоих.")
                        
                        try:
                            await bot.send_message(
                                referrer_id,
                                "Поздравляем! Ваш друг присоединился по вашей ссылке.\n"
                                "Вам и вашему другу активирован Премиум на 1 год!"
                            )
                        except Exception as e:
                            logger.error(f"Не удалось уведомить реферера {referrer_id}: {e}")
                    else:
                        save_users_data()
                        logger.info(f"Пользователь {user_id} зарегистрирован по реферальной ссылке {referrer_id}.")

    if is_new_referral:
        welcome_text = (
            "Вы присоединились по реферальной ссылке! Вам и вашему другу активирован Премиум на 1 год!\n\n"
            "Теперь кидайте ссылку — пришлю файл."
        )
    else:
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
        "Как только ваш друг перейдет по ссылке, вам и ему будет активирован Премиум."
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
    try:
        await callback.message.edit_text(text, reply_markup=keyboard, parse_mode="HTML")
    except TelegramBadRequest as e:
        if "message is not modified" in str(e).lower():
            return
        logger.error(f"Не удалось отредактировать сообщение для conditions: {e}")
        await bot.send_message(callback.from_user.id, text, reply_markup=keyboard, parse_mode="HTML")
    except Exception as e:
        logger.error(f"Ошибка в обработчике conditions: {e}")
        await bot.send_message(callback.from_user.id, text, reply_markup=keyboard, parse_mode="HTML")

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

@dp.callback_query(F.data.in_(["best", "1080p", "720p", "480p", "audio"]))
async def process_quality_choice(callback: CallbackQuery):
    """Обработчик выбора качества"""
    logger.info(f"Пользователь {callback.from_user.id} нажал кнопку качества: {callback.data}")
    user_id = callback.from_user.id
    quality = callback.data
    
    user_settings[user_id] = quality
    save_user_settings()
    
    await callback.answer(f"Качество установлено: {quality}")
    await callback.message.edit_text(
        f"Качество загрузки изменено на {quality}.",
        reply_markup=back_to_menu_keyboard()
    )

@dp.callback_query(F.data == "cancel")
async def process_cancel(callback: CallbackQuery):
    """Отмена выбора качества"""
    logger.info(f"Пользователь {callback.from_user.id} нажал кнопку 'cancel'")
    await callback.answer()
    await callback.message.delete()
    welcome_text = (
        "Кидайте ссылку — пришлю файл.\n"
        "Можно выбрать качество или оформить PRO."
    )
    await bot.send_message(callback.from_user.id, welcome_text, reply_markup=main_keyboard())

@dp.callback_query(F.data == "check_referral")
async def process_check_referral(callback: CallbackQuery):
    """Проверка приглашения"""
    logger.info(f"Пользователь {callback.from_user.id} нажал кнопку 'check_referral'")
    user_id = callback.from_user.id
    user = get_or_create_user(user_id)
    
    if user['referred_by']:
        referrer = users_data.get(user['referred_by'])
        if referrer:
            text = f"Вы были приглашены пользователем {user['referred_by']}"
        else:
            text = "Ваш пригласивший пользователь не найден"
    else:
        text = "Вы еще не использовали реферальную ссылку"
    
    await callback.answer()
    await callback.message.edit_text(
        text,
        reply_markup=back_to_menu_keyboard()
    )

@dp.callback_query(F.data == "how_referral_works")
async def process_how_referral_works(callback: CallbackQuery):
    """Как работает реферальная система"""
    logger.info(f"Пользователь {callback.from_user.id} нажал кнопку 'how_referral_works'")
    text = (
        "<b>Как работает реферальная система:</b>\n\n"
        "1. Вы получаете уникальную ссылку\n"
        "2. Приглашаете друга по этой ссылке\n"
        "3. Друг выполняет первое скачивание\n"
        "4. Вам и другу активируется Премиум на 1 год\n\n"
        "Премиум дает:\n"
        "• Безлимитные загрузки\n"
        "• Максимальное качество\n"
        "• Приоритетную обработку"
    )
    
    await callback.answer()
    await callback.message.edit_text(
        text,
        reply_markup=back_to_menu_keyboard(),
        parse_mode="HTML"
    )


# ==================== ОБРАБОТЧИК ССЫЛОК ====================

@dp.message(F.text.startswith("http"))
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
    
    # Определение платформы и обработка share ссылок
    if "youtube.com" in url or "youtu.be" in url:
        platform = "youtube"
    elif "rutube.ru" in url:
        platform = "rutube"
    elif "tiktok.com" in url or "vm.tiktok.com" in url or "vt.tiktok.com" in url:
        platform = "tiktok"
    elif "instagram.com" in url or "instagr.am" in url:
        platform = "instagram"
        # Обработка всех Instagram share ссылок (включая iPhone share ссылки)
        if "/share/" in url or "instagram.com/share/" in url or url.startswith("https://www.instagram.com/share/"):
            logger.info(f"Обнаружена Instagram share ссылка: {url}")
            try:
                # Развернуть share ссылку в полную
                expanded_url = await expand_instagram_share_url(url)
                if expanded_url and expanded_url != url:
                    url = expanded_url
                    logger.info(f"Развернутая URL: {url}")
            except Exception as e:
                logger.warning(f"Не удалось развернуть share ссылку: {e}")
    else:
        await message.answer(
            "Неподдерживаемая платформа.\n\n"
            "Поддерживаются: YouTube, RuTube, Instagram, TikTok."
        )
        return
    
    quality = get_quality_setting(user_id)
    temp_file = None
    temp_photos = []
    
    try:
        if platform == "youtube":
            status_msg = None
            try:
                status_msg = await message.answer("Скачиваю с YouTube...")
            except Exception:
                pass

            try:
                temp_file = await download_youtube(url, quality)
                if not temp_file:
                    temp_file = await download_youtube_with_playwright(url, quality)
            finally:
                if status_msg:
                    try:
                        await status_msg.delete()
                    except Exception:
                        pass
            
            if temp_file:
                await send_video_or_message(chat_id, temp_file)
                cleanup_file(temp_file)
                increment_downloads(user_id)
                
            else:
                await message.answer(
                    "Не удалось скачать видео.\n\n"
                    "Возможные причины:\n"
                    "• Видео приватное или удалено\n"
                    "• Проблемы с доступом к платформе\n"
                    "• Некорректная ссылка"
                )

        elif platform == "rutube":
            status_msg = None
            try:
                status_msg = await message.answer("Скачиваю с RuTube...")
            except Exception:
                pass

            try:
                temp_file = await download_rutube(url, quality)
            finally:
                if status_msg:
                    try:
                        await status_msg.delete()
                    except Exception:
                        pass

            if temp_file:
                await send_video_or_message(chat_id, temp_file)
                cleanup_file(temp_file)
                increment_downloads(user_id)
            else:
                await message.answer("Не удалось скачать видео с RuTube.")
        
        elif platform == "tiktok":
            if '/photo/' in url.lower():
                status_msg = None
                try:
                    status_msg = await message.answer("Скачиваю с TikTok...")
                except Exception:
                    pass

                try:
                    photos, description = await download_tiktok_photos(url)
                finally:
                    if status_msg:
                        try:
                            await status_msg.delete()
                        except Exception:
                            pass

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
                status_msg = None
                try:
                    status_msg = await message.answer("Скачиваю с TikTok...")
                except Exception:
                    pass

                try:
                    temp_file = await download_tiktok(url, quality)
                finally:
                    if status_msg:
                        try:
                            await status_msg.delete()
                        except Exception:
                            pass

                if temp_file:
                    await send_video_or_message(chat_id, temp_file)
                    cleanup_file(temp_file)
                    increment_downloads(user_id)
                else:
                    await message.answer("Не удалось скачать видео с TikTok.")
        
        elif platform == "instagram":
            # Показываем исчезающее статусное сообщение
            status_msg = None
            try:
                status_msg = await message.answer("Скачиваю с Instagram...")
            except Exception:
                pass
            
            try:
                video_path, photos, description = await download_instagram(url)
            finally:
                # Удаляем статусное сообщение
                if status_msg:
                    try:
                        await status_msg.delete()
                    except Exception:
                        pass
            
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

async def shutdown_cleanup():
    """Очистка ресурсов при завершении"""
    global SHUTDOWN_FLAG, YOUTUBE_REFRESH_TASK, IG_BROWSER, YT_BROWSER
    
    logger.info("Начало cleanup...")
    SHUTDOWN_FLAG = True
    
    # Отменяем фоновые задачи
    if YOUTUBE_REFRESH_TASK and not YOUTUBE_REFRESH_TASK.done():
        YOUTUBE_REFRESH_TASK.cancel()
        try:
            await asyncio.wait_for(YOUTUBE_REFRESH_TASK, timeout=2.0)
        except (asyncio.CancelledError, asyncio.TimeoutError):
            pass
    
    if INSTAGRAM_REFRESH_TASK and not INSTAGRAM_REFRESH_TASK.done():
        INSTAGRAM_REFRESH_TASK.cancel()
        try:
            await asyncio.wait_for(INSTAGRAM_REFRESH_TASK, timeout=2.0)
        except (asyncio.CancelledError, asyncio.TimeoutError):
            pass
    
    # Закрываем браузеры
    if IG_BROWSER:
        try:
            await IG_BROWSER.close()
        except Exception as e:
            logger.debug(f"Error closing IG browser: {e}")
    
    if YT_BROWSER:
        try:
            await YT_BROWSER.close()
        except Exception as e:
            logger.debug(f"Error closing YT browser: {e}")
    
    logger.info("Cleanup завершён")


async def main():
    """Основная функция запуска"""
    global bot, YOUTUBE_REFRESH_TASK, INSTAGRAM_REFRESH_TASK, SHUTDOWN_FLAG
    logger.info("Запуск бота...")
    
    SHUTDOWN_FLAG = False
    
    if not BOT_TOKEN:
        raise ValueError("BOT_TOKEN не найден в переменных окружения")
    
    init_cookies_from_env()
    load_user_settings()
    load_users_data()
    load_referrals()
    
    await init_instagram_playwright()
    await init_youtube_playwright()
    
    # Запускаем фоновые задачи обновления cookies
    YOUTUBE_REFRESH_TASK = asyncio.create_task(youtube_cookie_refresh_loop())
    logger.info("Фоновое обновление YouTube cookies запущено")
    
    INSTAGRAM_REFRESH_TASK = asyncio.create_task(instagram_cookie_refresh_loop())
    logger.info("Фоновое обновление Instagram cookies запущено")
    
    bot = Bot(token=BOT_TOKEN, session=AiohttpSession())
    
    webhook_url = os.getenv("WEBHOOK_URL", "")
    if webhook_url and webhook_url.strip():
        logger.info(f"Работаю в рэжиме Webhook: {webhook_url}")
        try:
            await bot.delete_webhook(drop_pending_updates=True)
            allowed_updates = dp.resolve_used_update_types()
            await bot.set_webhook(webhook_url, allowed_updates=allowed_updates)
            
            app = aiohttp.web.Application()
            from aiogram.webhook.aiohttp_server import SimpleRequestHandler
            webhook_requests_handler = SimpleRequestHandler(dispatcher=dp, bot=bot)
            webhook_requests_handler.register(app, path="/webhook")
            
            async def health(request):
                return aiohttp.web.Response(text="OK")
            
            async def webhook_info(request):
                """Эндпоинт для проверки информации о webhook"""
                try:
                    webhook_info = await bot.get_webhook_info()
                    info_text = f"Webhook URL: {webhook_info.url}\n"
                    info_text += f"Custom certificate: {webhook_info.use_custom_certificate}\n"
                    info_text += f"Max connections: {webhook_info.max_connections}\n"
                    info_text += f"Allowed updates: {webhook_info.allowed_updates}\n"
                    info_text += f"Pending update count: {webhook_info.pending_update_count}\n"
                    info_text += f"Last error: {webhook_info.last_error_message}\n"
                    return aiohttp.web.Response(text=info_text, content_type="text/plain")
                except Exception as e:
                    return aiohttp.web.Response(text=f"Error: {e}", content_type="text/plain")
            
            app.router.add_get("/", health)
            app.router.add_get("/health", health)
            app.router.add_get("/webhook-info", webhook_info)
            
            runner = aiohttp.web.AppRunner(app)
            await runner.setup()
            site = aiohttp.web.TCPSite(runner, '0.0.0.0', PORT)
            await site.start()
            logger.info(f"Webhook запущен на порту {PORT}")
            
            await asyncio.Event().wait()
        except Exception as e:
            logger.error(f"Ошибка webhook режима: {e}")
            logger.info("Переключаемся на polling режим...")
            await bot.delete_webhook(drop_pending_updates=True)
            allowed_updates = dp.resolve_used_update_types()
            await dp.start_polling(bot, allowed_updates=allowed_updates)
    else:
        logger.info("Работаю в ржиме Polling")
        try:
            await bot.delete_webhook(drop_pending_updates=True)
            allowed_updates = dp.resolve_used_update_types()
            await dp.start_polling(bot, allowed_updates=allowed_updates)
        finally:
            save_user_settings()
            save_users_data()
            save_referrals()
            await shutdown_cleanup()
            logger.info("Бот остановлен")

if __name__ == "__main__":
    asyncio.run(main())