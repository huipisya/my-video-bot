# bot.py
import asyncio
import json
import logging
import os
import tempfile
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
    CallbackQuery,
    FSInputFile
)

from dotenv import load_dotenv
from playwright.async_api import async_playwright, Browser, BrowserContext
import sys

# Настройка кодировки stdout (важно для Windows и некоторых других сред)
sys.stdout.reconfigure(encoding='utf-8')

# Загрузка переменных окружения из .env файла
load_dotenv()

# Настройка логирования
logging.basicConfig(
    level=logging.INFO,  # Уровень логирования, можно изменить на DEBUG для более подробного вывода
    format='%(asctime)s - %(levelname)s - %(message)s'
)
logger = logging.getLogger(__name__)

# - Глобальные переменные -
BOT_TOKEN = os.getenv("BOT_TOKEN")
WEBHOOK_HOST = os.getenv("WEBHOOK_HOST") # Опционально, если используется webhook

YT_BROWSER: Optional[Browser] = None
YT_CONTEXT: Optional[BrowserContext] = None
YT_PLAYWRIGHT_READY = False

IG_BROWSER: Optional[Browser] = None
IG_CONTEXT: Optional[BrowserContext] = None
IG_PLAYWRIGHT_READY = False

bot: Optional[Bot] = None
dp = Dispatcher()

user_settings = {} # Словарь для хранения настроек качества для пользователей
SETTINGS_FILE = 'user_settings.json' # Имя файла для хранения настроек
RATE_LIMIT_DELAY = {} # Словарь для отслеживания задержки на пользователя (если нужно)
# - Функция для загрузки настроек пользователей из файла -
def load_user_settings():
    global user_settings
    try:
        with open(SETTINGS_FILE, 'r', encoding='utf-8') as f:
            user_settings = json.load(f)
            logger.info(f"✅ Загружено {len(user_settings)} настроек пользователей из {SETTINGS_FILE}")
    except FileNotFoundError:
        logger.info(f"📁 Файл {SETTINGS_FILE} не найден, создаю новый.")
        user_settings = {}
        save_user_settings() # Создаём пустой файл, если его не было
    except json.JSONDecodeError:
        logger.error(f"❌ Ошибка чтения JSON из {SETTINGS_FILE}, создаю новый.")
        user_settings = {}
        save_user_settings() # Пересоздаём файл, если повреждён
    except Exception as e:
        logger.error(f"❌ Неизвестная ошибка при загрузке настроек: {e}")
        user_settings = {} # На всякий случай, если что-то пошло не так

# - Функция для сохранения настроек пользователей в файл -
def save_user_settings():
    try:
        with open(SETTINGS_FILE, 'w', encoding='utf-8') as f:
            json.dump(user_settings, f, ensure_ascii=False, indent=2)
        logger.info(f"✅ Настройки пользователей сохранены в {SETTINGS_FILE}")
    except Exception as e:
        logger.error(f"❌ Ошибка сохранения настроек в {SETTINGS_FILE}: {e}")

# - Настройка состояний FSM -
class VideoStates(StatesGroup):
    choosing_quality = State()

# - Функция для инициализации cookies из переменных окружения -
def init_cookies_from_env():
    """Создаёт файлы cookies из переменных окружения."""
    # Словарь соответствия переменной окружения и имени файла
    cookie_env_to_file = {
        "COOKIES_BOT1": "cookies_bot1",
        "COOKIES_BOT2": "cookies_bot2",
        "COOKIES_BOT3": "cookies_bot3",
        "COOKIES_YOUTUBE": "cookies_youtube.txt",
    }

    created_files = []
    for env_var, filename in cookie_env_to_file.items():
        cookies_json = os.getenv(env_var)
        if cookies_json:
            try:
                cookies_data = json.loads(cookies_json)
                with open(filename, 'w', encoding='utf-8') as f:
                    # Предполагается, что данные в формате JSON для yt-dlp
                    json.dump(cookies_data, f, ensure_ascii=False, indent=2)
                logger.info(f"✅ Создан {filename}")
                created_files.append(filename)
            except json.JSONDecodeError:
                logger.error(f"❌ Ошибка декодирования JSON для {env_var}")
            except Exception as e:
                logger.error(f"❌ Ошибка записи файла {filename}: {e}")
        else:
            logger.info(f"🍪 Переменная окружения {env_var} не найдена, пропускаю создание {filename}")

    # Создание пустого файла cookies_youtube.txt, если он не был создан из переменной окружения
    if "cookies_youtube.txt" not in created_files:
        if not os.path.exists("cookies_youtube.txt"):
            Path("cookies_youtube.txt").touch()
            logger.info("✅ Создан пустой файл cookies_youtube.txt")

    logger.info(f"✅ Создано {len(created_files)} файлов cookies")

# - Функция для получения настроек качества пользователя -
def get_quality_setting(user_id: int) -> str:
    return user_settings.get(user_id, "best") # По умолчанию "best"

# - Вспомогательная функция для получения опций yt-dlp -
def get_ydl_opts(quality: str = "best", use_youtube_cookies: bool = True) -> Dict[str, Any]:
    cookie_file = "cookies_youtube.txt" if use_youtube_cookies else None
    ydl_opts = {
        'format': quality if quality != 'best' else 'best',
        'outtmpl': '%(title)s.%(ext)s',
        'noplaylist': True,
        'extractaudio': False,
        'nocheckcertificate': True,
        'ignoreerrors': False,
        'no_warnings': False,
        'quiet': False,
    }
    if cookie_file and os.path.exists(cookie_file):
        ydl_opts['cookiefile'] = cookie_file
    return ydl_opts

# - Функция для инициализации Playwright для Instagram -
async def init_instagram_playwright():
    global IG_BROWSER, IG_CONTEXT, IG_PLAYWRIGHT_READY
    logger.info("🌐 Инициализация Instagram Playwright...")
    try:
        pw = await async_playwright().start()
        IG_BROWSER = await pw.chromium.launch(headless=True)
        IG_CONTEXT = await IG_BROWSER.new_context(
            # viewport={'width': 1920, 'height': 1080}, # Опционально
            user_agent='Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/91.0.4472.124 Safari/537.36'
        )

        # Попытка загрузить cookies для Instagram из переменной окружения
        cookies_json = os.getenv("COOKIES_INSTAGRAM") or os.getenv("COOKIES_TXT")
        if cookies_json:
            try:
                cookies_data = json.loads(cookies_json)
                # yt-dlp формат cookies -> Playwright формат cookies
                cookies_to_load = []
                for cookie in cookies_data:
                    # Преобразование формата, если нужно
                    # yt-dlp использует {'name', 'value', 'domain', 'path', 'expires', 'secure', 'httpOnly'}
                    # Playwright ожидает {'name', 'value', 'domain', 'path', 'expires', 'secure', 'httpOnly', 'sameSite'}
                    pw_cookie = {
                        'name': cookie.get('name', ''),
                        'value': cookie.get('value', ''),
                        'domain': cookie.get('domain', ''),
                        'path': cookie.get('path', '/'),
                        'expires': int(cookie.get('expires', 0)) if cookie.get('expires') else None,
                        'secure': bool(cookie.get('secure', False)),
                        'httpOnly': bool(cookie.get('httpOnly', False)),
                        'sameSite': 'Lax' # или 'Strict', 'None' в зависимости от требований
                    }
                    # Убираем None значения
                    pw_cookie = {k: v for k, v in pw_cookie.items() if v is not None}
                    cookies_to_load.append(pw_cookie)

                await IG_CONTEXT.add_cookies(cookies_to_load)
                logger.info(f"✅ Загружено {len(cookies_to_load)} Instagram cookies в контекст Playwright.")
            except json.JSONDecodeError:
                logger.error("❌ Ошибка декодирования JSON для COOKIES_INSTAGRAM/COOKIES_TXT")
            except Exception as e:
                logger.error(f"❌ Ошибка загрузки cookies в контекст Playwright: {e}")
        else:
            logger.info("🍪 Переменная окружения COOKIES_INSTAGRAM/COOKIES_TXT не найдена для Playwright, запуск без cookies.")

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

# - Функция для инициализации Playwright для YouTube -
async def init_youtube_playwright():
    global YT_BROWSER, YT_CONTEXT, YT_PLAYWRIGHT_READY
    logger.info("🌐 Инициализация YouTube Playwright...")
    try:
        pw = await async_playwright().start()
        YT_BROWSER = await pw.chromium.launch(headless=True)
        YT_CONTEXT = await YT_BROWSER.new_context(
            # viewport={'width': 1920, 'height': 1080}, # Опционально
            user_agent='Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/91.0.4472.124 Safari/537.36'
        )

        # Загрузка cookies из файла
        cookie_file_path = Path("cookies_youtube.txt")
        if cookie_file_path.exists():
            logger.info(f"🍪 Загружаем YouTube cookies из {cookie_file_path.name}")
            try:
                with open(cookie_file_path, 'r', encoding='utf-8') as f:
                    lines = f.readlines()
                cookies_to_load = []
                for line in lines:
                    # Пропускаем комментарии и пустые строки
                    if line.startswith('#') or not line.strip():
                        continue
                    try:
                        parts = line.strip().split('\t')
                        if len(parts) >= 7:
                            # Преобразование формата Netscape cookie в формат Playwright
                            domain, flag, path, secure, expiration, name, value = parts[:7]
                            pw_cookie = {
                                'name': name,
                                'value': value,
                                'domain': domain.lstrip('.'), # Убираем начальную точку, если есть
                                'path': path,
                                'expires': int(expiration) if expiration.isdigit() else None,
                                'secure': secure.lower() == 'true',
                                'httpOnly': False, # yt-dlp не всегда указывает это
                                'sameSite': 'Lax' # или 'Strict', 'None' в зависимости от требований
                            }
                            # Убираем None значения
                            pw_cookie = {k: v for k, v in pw_cookie.items() if v is not None}
                            cookies_to_load.append(pw_cookie)
                    except ValueError:
                        logger.warning(f"⚠️ Неверный формат строки cookie: {line.strip()}")
                        continue

                if cookies_to_load:
                    await YT_CONTEXT.add_cookies(cookies_to_load)
                    logger.info(f"✅ Загружено {len(cookies_to_load)} YouTube cookies в контекст Playwright.")
                else:
                    logger.info("🍪 Файл cookies_youtube.txt найден, но не содержит действительных cookies для Playwright.")
            except Exception as e:
                logger.error(f"❌ Ошибка загрузки cookies из файла: {e}")
        else:
            logger.info("🍪 Файл cookies_youtube.txt не найден, запуск без cookies.")

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

# - Функция для очистки файлов -
def cleanup_file(file_path: str):
    try:
        if file_path and os.path.exists(file_path):
            os.remove(file_path)
            # Изменяем уровень логирования с debug на info
            logger.info(f"🗑️ Удалён файл: {Path(file_path).name}")
    except Exception as e:
        logger.warning(f"⚠️ Не удалось удалить {file_path}: {e}")

def cleanup_files(files: List[str]):
    for file_path in files:
        cleanup_file(file_path)

# - Функция для скачивания видео с YouTube -
async def download_youtube(url: str, quality: str = "best") -> Optional[str]:
    logger.info(f"🔄 Скачивание видео с YOUTUBE (качество={quality})...")
    ydl_opts = get_ydl_opts(quality, use_youtube_cookies=True)
    try:
        with yt_dlp.YoutubeDL(ydl_opts) as ydl:
            info = ydl.extract_info(url, download=True)
            temp_file = ydl.prepare_filename(info)
            if temp_file and os.path.exists(temp_file):
                logger.info(f"✅ Видео скачано: {Path(temp_file).name}")
                return temp_file
            else:
                logger.error("❌ yt-dlp не создал файл")
                return None
    except yt_dlp.DownloadError as e:
        logger.error(f"❌ Ошибка yt-dlp: {e}")
        return None
    except Exception as e:
        logger.error(f"❌ Неизвестная ошибка при скачивании с YouTube: {e}")
        return None

# - Функция для скачивания видео с YouTube через Playwright как резервный метод -
async def download_youtube_with_playwright(url: str, quality: str = "best") -> Optional[str]:
    global YT_CONTEXT
    if not YT_PLAYWRIGHT_READY or not YT_CONTEXT:
        logger.error("❌ YouTube Playwright не инициализирован")
        return None

    logger.info(f"🔄 Скачивание видео с YOUTUBE через Playwright (качество={quality})...")
    page = None
    temp_cookies_file = None
    try:
        page = await YT_CONTEXT.new_page()
        await page.goto(url, wait_until='networkidle')

        # Проверка на страницу входа или ошибку
        if "consent.youtube.com" in page.url or page.url == "https://www.youtube.com/":
            logger.warning("⚠️ Требуется аутентификация или согласие на куки на YouTube (Playwright).")
            # Попытка принять куки, если возможно
            try:
                accept_button = page.get_by_text("I agree", exact=True).or_(page.get_by_text("Принять", exact=True))
                if await accept_button.count() > 0:
                    await accept_button.click()
                    await page.wait_for_load_state('networkidle')
            except Exception as e:
                logger.info(f"ℹ️ Кнопка согласия не найдена или ошибка: {e}")

        # Получение cookies из Playwright и сохранение во временный файл для yt-dlp
        cookies = await YT_CONTEXT.cookies()
        if not cookies:
            logger.warning("⚠️ Не удалось получить cookies из Playwright для yt-dlp.")
            # Всё равно пробуем скачать без куки
            ydl_opts = get_ydl_opts(quality, use_youtube_cookies=False)
        else:
            # Сохраняем cookies во временный файл в формате Netscape
            temp_cookies_file = Path(tempfile.mktemp(suffix='.txt'))
            with open(temp_cookies_file, 'w', encoding='utf-8') as f:
                f.write("# Netscape HTTP Cookie File\n")
                f.write("# This is a generated file! Do not edit.\n\n")
                for cookie in cookies:
                    f.write(f"{cookie['domain']}\t")
                    f.write(f"{'TRUE' if cookie['domain'].startswith('.') else 'FALSE'}\t")
                    f.write(f"{cookie['path']}\t")
                    f.write(f"{'TRUE' if cookie['secure'] else 'FALSE'}\t")
                    f.write(f"{int(cookie['expires']) if cookie['expires'] else 0}\t")
                    f.write(f"{cookie['name']}\t")
                    f.write(f"{cookie['value']}\n")

            ydl_opts = get_ydl_opts(quality, use_youtube_cookies=False) # Уже используем куки из Playwright
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

    except Exception as e:
        logger.error(f"❌ Ошибка в download_youtube_with_playwright: {e}")
        return None
    finally:
        if page:
            await page.close()
        if temp_cookies_file and temp_cookies_file.exists():
            temp_cookies_file.unlink(missing_ok=True) # Удаляем временный файл куки

# - Функция для скачивания видео с TikTok -
async def download_tiktok(url: str, quality: str = "1080p") -> Optional[str]:
    logger.info(f"🔄 Скачивание видео с TIKTOK (качество={quality})...")
    # yt-dlp сам определяет качество для TikTok, но мы можем попробовать указать формат
    # Однако, для TikTok часто работает просто 'best'
    ydl_opts = {
        'format': 'best', # yt-dlp обычно сам находит лучшее качество для TikTok
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
                logger.info(f"✅ Видео TikTok скачано: {Path(temp_file).name}")
                return temp_file
            else:
                logger.error("❌ yt-dlp не создал файл для TikTok")
                return None
    except yt_dlp.DownloadError as e:
        error_str = str(e).lower()
        if "Sign in to confirm you're not a bot" in error_str or "requires authentication" in error_str:
            logger.info("🔄 Ошибка требует аутентификации, пробуем Playwright...")
            # Здесь можно реализовать логику с Playwright для TikTok, если необходимо
            # Пока возвращаем None
            pass
        logger.error(f"❌ Ошибка скачивания с TikTok: {e}")
        return None
    except Exception as e:
        logger.error(f"❌ Неизвестная ошибка при скачивании с TikTok: {e}")
        return None

# - Функция для скачивания фото с TikTok (карусель) -
async def download_tiktok_photos(url: str) -> Tuple[Optional[List[str]], str]:
    logger.info(f"🔄 Скачивание фото с TIKTOK (карусель)...")
    ydl_opts = {
        'format': 'best',
        'outtmpl': '%(id)s/%(autonumber)s.%(ext)s',
        'noplaylist': False, # Позволяем скачивать плейлисты (фото как плейлист)
        'extractaudio': False,
        'nocheckcertificate': True,
        'ignoreerrors': False,
        'no_warnings': True,
        'quiet': True,
        'playlistend': 10, # Ограничиваем количество скачиваемых фото
    }

    try:
        with yt_dlp.YoutubeDL(ydl_opts) as ydl:
            info = ydl.extract_info(url, download=True)
            temp_dir = info.get('id') # Используем ID TikTok в качестве имени папки
            if temp_dir and os.path.isdir(temp_dir):
                # Собираем все файлы из папки
                photo_files = [os.path.join(temp_dir, f) for f in sorted(os.listdir(temp_dir)) if f.lower().endswith(('.jpg', '.jpeg', '.png', '.webp'))]
                if photo_files:
                    description = info.get('description', '') or info.get('title', '')
                    logger.info(f"✅ Скачано {len(photo_files)} фото из TikTok карусели.")
                    return photo_files, description
                else:
                    logger.error("❌ Не найдено файлов изображений в папке TikTok.")
                    return None, ""
            else:
                logger.error("❌ Папка для фото TikTok не найдена.")
                return None, ""
    except yt_dlp.DownloadError as e:
        logger.error(f"❌ Ошибка скачивания фото с TikTok: {e}")
        return None, ""
    except Exception as e:
        logger.error(f"❌ Неизвестная ошибка при скачивании фото с TikTok: {e}")
        return None, ""

# - Функция для скачивания с Instagram -
async def download_instagram(url: str) -> Tuple[Optional[str], Optional[List[str]], str]:
    logger.info(f"🔄 Скачивание медиа с INSTAGRAM...")
    ydl_opts = get_ydl_opts(quality="best", use_youtube_cookies=False) # Куки для YouTube, но yt-dlp сам разберётся
    # Попробуем сначала через yt-dlp
    try:
        with yt_dlp.YoutubeDL(ydl_opts) as ydl:
            info = ydl.extract_info(url, download=True)
            temp_file = ydl.prepare_filename(info)
            if temp_file and os.path.exists(temp_file):
                logger.info(f"✅ Медиа Instagram скачано через yt-dlp: {Path(temp_file).name}")
                description = info.get('description', '') or info.get('title', '')
                return temp_file, None, description # Видео, нет фото, описание
            else:
                # Если не видео, возможно, это фото или карусель
                # yt-dlp может сохранить в папку
                temp_dir = info.get('id') # Используем ID как имя папки
                if temp_dir and os.path.isdir(temp_dir):
                    photo_files = [os.path.join(temp_dir, f) for f in sorted(os.listdir(temp_dir)) if f.lower().endswith(('.jpg', '.jpeg', '.png', '.webp'))]
                    if photo_files:
                        logger.info(f"✅ Скачано {len(photo_files)} фото из Instagram.")
                        description = info.get('description', '') or info.get('title', '')
                        return None, photo_files, description # Нет видео, фото, описание
                logger.error("❌ yt-dlp не создал файл или папку для Instagram.")
                return None, None, "❌ Не удалось получить медиа через yt-dlp."
    except yt_dlp.DownloadError as e:
        error_str = str(e).lower()
        logger.info(f"🔄 yt-dlp не удалось: {e}. Пробуем Playwright...")
        if "login" in error_str or "private" in error_str or "requires authentication" in error_str:
            logger.info("🔐 yt-dlp требует аутентификации, пробуем Playwright...")
            # В любом случае, пробуем Playwright
            return await download_instagram_with_playwright(url)
    except Exception as e:
        logger.error(f"❌ Неизвестная ошибка при скачивании с Instagram (yt-dlp): {e}")
        # Если yt-dlp не сработал, пробуем Playwright
        return await download_instagram_with_playwright(url)

# - Функция для скачивания с Instagram через Playwright -
async def download_instagram_with_playwright(url: str) -> Tuple[Optional[str], Optional[List[str]], str]:
    global IG_CONTEXT
    if not IG_PLAYWRIGHT_READY or not IG_CONTEXT:
        logger.error("❌ Instagram Playwright не инициализирован")
        return None, None, "❌ Playwright не инициализирован"

    logger.info(f"🔄 Скачивание медиа с INSTAGRAM через Playwright...")
    page = None
    try:
        page = await IG_CONTEXT.new_page()
        await page.goto(url, wait_until='networkidle')

        # Проверка на страницу входа или ошибку
        if "accounts/login" in page.url or "challenge" in page.url:
            logger.warning("⚠️ Требуется аутентификация на Instagram (Playwright).")
            return None, None, "🔐 Требуется аутентификация. Куки могут быть недействительны."

        # Попытка получить описание (опционально)
        description_element = page.locator('article div[data-testid="tweet"] div[dir="auto"] span, article div._ab1k._ab1l div._aa99._aamp span')
        description = await description_element.first.text_content() if await description_element.count() > 0 else ""

        # Проверка на видео
        video_element = page.locator('article video source')
        if await video_element.count() > 0:
            video_url = await video_element.first.get_attribute('src')
            if video_url:
                # Скачиваем видео через yt-dlp
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
                        info = ydl.extract_info(video_url, download=True)
                        temp_file = ydl.prepare_filename(info)
                        if temp_file and os.path.exists(temp_file):
                            logger.info(f"✅ Видео Instagram (через Playwright) скачано: {Path(temp_file).name}")
                            return temp_file, None, description
                except Exception as e:
                    logger.error(f"❌ Ошибка скачивания видео из URL: {e}")

        # Проверка на фото (одиночное или карусель)
        photo_elements = page.locator('article img')
        photo_count = await photo_elements.count()
        if photo_count > 0:
            photo_urls = []
            for i in range(photo_count):
                photo_url = await photo_elements.nth(i).get_attribute('src')
                if photo_url:
                    photo_urls.append(photo_url)

            # Скачиваем фото
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
                    logger.info(f"✅ Скачано {len(photo_paths)} фото из Instagram (через Playwright).")
                    return None, photo_paths, description

        logger.error("❌ Не удалось найти видео или фото на странице Instagram (Playwright).")
        return None, None, "❌ Медиа не найдено."
    except Exception as e:
        logger.error(f"❌ Ошибка в download_instagram_with_playwright: {e}")
        return None, None, f"❌ Ошибка Playwright: {e}"
    finally:
        if page:
            await page.close()

# - Функция для загрузки файла на file.io -
async def upload_to_fileio(file_path: str) -> Optional[str]:
    url = 'https://file.io/'
    max_size = 50 * 1024 * 1024 # 50 MB в байтах
    file_size = os.path.getsize(file_path)
    if file_size > max_size:
        logger.info(f"📁 Файл {Path(file_path).name} превышает 50 MB, загружаю на file.io...")
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
                                logger.info(f"✅ Файл загружен на file.io: {fileio_link}")
                                return fileio_link
                            else:
                                logger.error(f"❌ file.io вернул ошибку: {response_json.get('message')}")
                                return None
                        else:
                            logger.error(f"❌ Ошибка загрузки на file.io: статус {resp.status}")
                            return None
        except Exception as e:
            logger.error(f"❌ Исключение при загрузке на file.io: {e}")
            return None
    else:
        logger.info(f"📁 Файл {Path(file_path).name} меньше 50 MB, отправляю напрямую.")
        return None # Файл не нужно загружать на file.io

# - Функция для отправки видео или фото -
async def send_video_or_message(chat_id: int, file_path: str, caption: str = ""):
    max_telegram_file_size = 50 * 1024 * 1024 # 50 MB в байтах
    file_size = os.path.getsize(file_path)
    if file_size > max_telegram_file_size:
        # Загружаем на file.io
        fileio_link = await upload_to_fileio(file_path)
        if fileio_link:
            await bot.send_message(chat_id, f"📁 Файл слишком большой для Telegram. Вот ссылка: {fileio_link}")
        else:
            await bot.send_message(chat_id, "❌ Не удалось загрузить файл на file.io для отправки.")
    else:
        # Отправляем напрямую
        input_file = FSInputFile(file_path)
        try:
            # Попробуем отправить как видео
            await bot.send_video(chat_id=chat_id, video=input_file, caption=caption)
            logger.info("✅ Видео отправлено напрямую.")
        except TelegramBadRequest as e:
            if "Wrong type of the web page content" in str(e) or "PHOTO_AS_DOCUMENT" in str(e):
                try:
                    # Если не получилось как видео, пробуем как фото
                    await bot.send_photo(chat_id=chat_id, photo=input_file, caption=caption)
                    logger.info("✅ Фото отправлено напрямую.")
                except TelegramBadRequest as e2:
                    logger.error(f"❌ Ошибка отправки фото: {e2}")
                    # Если и фото не получилось, возможно, это документ
                    await bot.send_document(chat_id=chat_id, document=input_file, caption=caption)
                    logger.info("✅ Файл отправлен как документ.")
            else:
                logger.error(f"❌ Ошибка отправки видео: {e}")
                await bot.send_message(chat_id, f"❌ Ошибка при отправке файла: {e}")
        except Exception as e:
            logger.error(f"❌ Неизвестная ошибка при отправке файла: {e}")
            await bot.send_message(chat_id, f"❌ Неизвестная ошибка при отправке файла: {e}")

# --- Вспомогательная функция для создания клавиатуры настроек ---
def settings_menu_keyboard() -> ReplyKeyboardMarkup:
    QUALITY_FORMATS = {
        "best": "best",
        "1080p": "1080p",
        "720p": "720p",
        "480p": "480p",
        "360p": "360p"
    }
    keyboard = [
        [KeyboardButton(text=q.upper()) for q in QUALITY_FORMATS.keys()],
        [KeyboardButton(text="◀️ Назад")]
    ]
    return ReplyKeyboardMarkup(keyboard=keyboard, resize_keyboard=True)

# --- Обработчик выбора качества ---
@dp.message(VideoStates.choosing_quality)
async def process_quality_choice(message: Message, state: FSMContext):
    # Получаем текст сообщения и приводим к нижнему регистру
    choice = message.text.strip().lower()

    # Определяем, является ли выбор "назад", проверяем оба варианта
    if choice in ["назад", "◀️ назад"]:
        await state.clear()
        await message.answer("⚙️ Настройки закрыты.", reply_markup=main_keyboard())
        return

    # Проверяем, соответствует ли выбор одному из доступных качеств (с учётом регистра)
    # QUALITY_FORMATS.keys() = ["best", "1080p", "720p", "480p", "360p"]
    # Пользователь мог ввести "BEST", "1080P", "720p" и т.д.
    # После .lower() это станет "best", "1080p", "720p"
    QUALITY_FORMATS = {
        "best": "best",
        "1080p": "1080p",
        "720p": "720p",
        "480p": "480p",
        "360p": "360p"
    }
    if choice in QUALITY_FORMATS:
        user_settings[message.from_user.id] = choice
        save_user_settings()
        await state.clear()
        # Отправляем сообщение с установленным качеством, используя значение choice (в верхнем регистре)
        # и возвращаем основную клавиатуру
        await message.answer(
            f"✅ Качество установлено на <b>{choice.upper()}</b>.",
            reply_markup=main_keyboard(),
            parse_mode="HTML"
        )
        # Состояние уже очищено, можно выйти
        return
    else:
        # Если выбор некорректен, отправляем сообщение об ошибке
        # и *не сбрасываем* состояние, чтобы пользователь мог снова выбрать
        await message.answer("❌ Некорректный выбор. Пожалуйста, выберите из предложенных вариантов.")
        # Опционально: повторно отправить клавиатуру настроек
        current = get_quality_setting(message.from_user.id)
        # Используем обновлённую функцию для клавиатуры
        keyboard = settings_menu_keyboard() # Вызов функции
        await message.answer(
            f"⚙️ Текущее качество: <b>{current.upper()}</b>\nВыберите новое:",
            reply_markup=keyboard,
            parse_mode="HTML"
        )
        # Не вызываем state.clear(), чтобы пользователь оставался в состоянии выбора
        # Не сбрасываем состояние, чтобы пользователь мог повторить попытку

# - Функция для создания основной клавиатуры -
def main_keyboard() -> ReplyKeyboardMarkup:
    keyboard = [
        [KeyboardButton(text="🎬 Инструкция")],
        [KeyboardButton(text="⚙️ Настройки")],
    ]
    return ReplyKeyboardMarkup(keyboard=keyboard, resize_keyboard=True)

# - Обработчик команды /start -
@dp.message(Command("start"))
async def cmd_start(message: Message):
    user_id = message.from_user.id
    welcome_text = (
        f"🎬 <b>Добро пожаловать в VideoBot!</b>\n"
        f"Я могу скачать видео с:\n"
        f"• YouTube\n"
        f"• TikTok\n"
        f"• Instagram (посты, reels, карусели)\n\n"
        f"📲 Просто отправь мне ссылку!\n"
        f"⚙️ Текущее качество: <b>{get_quality_setting(user_id).upper()}</b>"
    )
    await message.answer(welcome_text, reply_markup=main_keyboard(), parse_mode="HTML")

# - Обработчик команды /help -
@dp.message(F.text == "🎬 Инструкция")
async def send_welcome(message: Message):
    help_text = (
        "🎬 <b>Инструкция по использованию VideoBot</b>\n"
        "1. Отправьте боту ссылку на видео с YouTube, TikTok или Instagram.\n"
        "2. Бот скачает и отправит вам видео (или фото, если это карусель)."
    )
    await message.answer(help_text, reply_markup=main_keyboard(), parse_mode="HTML")

# - Обработчик команды /settings -
@dp.message(F.text == "⚙️ Настройки")
async def settings_menu(message: Message, state: FSMContext):
    current = get_quality_setting(message.from_user.id)
    keyboard = settings_menu_keyboard()
    await message.answer(
        f"⚙️ Текущее качество: <b>{current.upper()}</b>\nВыберите новое:",
        reply_markup=keyboard,
        parse_mode="HTML"
    )
    # Устанавливаем состояние FSM
    await state.set_state(VideoStates.choosing_quality)

# - Обработчик всех остальных сообщений (предположительно ссылок) -
@dp.message(F.text)
async def handle_link(message: Message, state: FSMContext):
    # Проверяем, не находится ли пользователь в состоянии выбора качества
    current_state = await state.get_state()
    if current_state == VideoStates.choosing_quality.state:
        # Если пользователь в состоянии выбора качества, но сюда попал,
        # это означает, что process_quality_choice не сработал.
        # Это может быть из-за синтаксической ошибки или другой проблемы.
        # Логика внутри process_quality_choice должна покрывать все случаи.
        # Если сюда всё же дойдёт, бот просто скажет, что ожидает ссылку.
        # Но с правильной логикой в process_quality_choice, этого не должно произойти.
        # Однако, для надёжности, можно снова показать меню настроек.
        # await message.answer("❌ Некорректный выбор. Пожалуйста, выберите из предложенных вариантов.")
        # await settings_menu(message, state) # Повторный вызов меню
        # return
        # Лучше оставить как есть, так как process_quality_choice должен обработать всё.
        return # Выход, если FSM активна, но обработчик не сработал - это проблема в логике FSM.

    url = message.text.strip()
    user_id = message.from_user.id
    chat_id = message.chat.id
    quality = get_quality_setting(user_id)

    # Проверка на ссылку
    if not (url.startswith("http://") or url.startswith("https://")):
        await message.answer("🔗 Пожалуйста, отправьте действительную ссылку.")
        return

    # Определение платформы
    if "youtube.com" in url or "youtu.be" in url:
        platform = "youtube"
    elif "tiktok.com" in url or "vm.tiktok.com" in url or "vt.tiktok.com" in url:
        platform = "tiktok"
    elif "instagram.com" in url or "instagr.am" in url:
        platform = "instagram"
    else:
        await message.answer("❌ Неизвестная платформа. Поддерживаются: YouTube, TikTok, Instagram.")
        return

    status_msg = await message.answer("⏳ Обрабатываю...")
    temp_file = None
    temp_photos = [] # Для фото из Instagram/TikTok

    try:
        if platform == "youtube":
            temp_file = await download_youtube(url, quality)
            if not temp_file:
                # Если основной способ не сработал, пробуем Playwright
                temp_file = await download_youtube_with_playwright(url, quality)
        elif platform == "tiktok":
            if '/photo/' in url.lower() or '/photos/' in url.lower():
                # Обработка фото/карусели TikTok
                photos, description = await download_tiktok_photos(url)
                await bot.delete_message(chat_id=chat_id, message_id=status_msg.message_id)
                if photos:
                    temp_photos = photos
                    # Отправляем фото как медиа-группу
                    media_group = [InputMediaPhoto(media=FSInputFile(photo),
                                                   caption=description if i == 0 else None # Подпись только к первому фото
                                                  ) for i, photo in enumerate(photos)]
                    # Отправляем группу фото (ограничение Telegram - 10 фото за раз)
                    batch_size = 10
                    for i in range(0, len(media_group), batch_size):
                        batch = media_group[i:i + batch_size]
                        await bot.send_media_group(chat_id=message.chat.id, media=batch)
                    logger.info(f"✅ Отправлено {len(photos)} фото из TikTok")
                    cleanup_files(photos)
                    return # Выход из обработки, так как фото отправлены
                else:
                    await message.answer("❌ Не удалось скачать фото с TikTok.")
                    return
            else:
                # Обработка видео TikTok
                temp_file = await download_tiktok(url, quality)
        elif platform == "instagram":
            # Обработка Instagram (видео, фото, карусель)
            video_path, photos, description = await download_instagram(url)
            await bot.delete_message(chat_id=chat_id, message_id=status_msg.message_id)
            if video_path:
                temp_file = video_path
                # Проверяем, является ли ссылка Reel
                is_reel = '/reel/' in url.lower()
                # Если это Reel, не отправляем описание
                caption_to_send = "" if is_reel else description
                await send_video_or_message(message.chat.id, temp_file, caption=caption_to_send)
                cleanup_file(temp_file)
                return
            elif photos:
                temp_photos = photos
                # Отправляем фото как медиа-группу
                media_group = [InputMediaPhoto(media=FSInputFile(photo),
                                               caption=description if i == 0 else None # Подпись только к первому фото
                                              ) for i, photo in enumerate(photos)]
                # Отправляем группу фото (ограничение Telegram - 10 фото за раз)
                batch_size = 10
                for i in range(0, len(media_group), batch_size):
                    batch = media_group[i:i + batch_size]
                    await bot.send_media_group(chat_id=message.chat.id, media=batch)
                logger.info(f"✅ Отправлено {len(photos)} фото из Instagram")
                cleanup_files(photos)
                return
            else:
                await message.answer("❌ Не удалось скачать медиа с Instagram.")
                return

                # Обработка результата для видео (YouTube, TikTok)
        await bot.delete_message(chat_id=chat_id, message_id=status_msg.message_id)
        if temp_file:
            # --- ИСПРАВЛЕНО ---
            # Для YouTube и TikTok видео caption (описание) не отправляется.
            # Переменная description не определена для YouTube, её использование вызовет ошибку.
            # Для Instagram Reels caption также не отправляется (is_reel).
            # Для других Instagram видео caption может быть определён выше.
            if platform == "youtube":
                 # Не передаём caption для YouTube
                await send_video_or_message(message.chat.id, temp_file) # caption не передаётся
            elif platform == "tiktok":
                 # Не передаём caption для TikTok видео
                await send_video_or_message(message.chat.id, temp_file) # caption не передаётся
            else: # platform == "instagram" (и это видео, а не фото)
                 # Здесь description должна быть определена выше для Instagram
                 # Проверяем, является ли это Reel
                is_reel = '/reel/' in url.lower()
                caption_to_send = "" if is_reel else description
                await send_video_or_message(message.chat.id, temp_file, caption=caption_to_send)
            # --- КОНЕЦ ИСПРАВЛЕНИЯ ---
            cleanup_file(temp_file) # Удаляем файл после отправки
        else:
            await message.answer("❌ Не удалось скачать видео.")

    except Exception as e:
        error_msg = f"❌ Ошибка: {str(e)}"
        logger.error(error_msg)
        try:
            await bot.edit_message_text(text=error_msg, chat_id=chat_id, message_id=status_msg.message_id)
        except:
            await message.answer(error_msg)
    finally:
        # Убедимся, что файлы удалены в любом случае
        if temp_file:
            cleanup_file(temp_file)
        if temp_photos:
            cleanup_files(temp_photos)


# - Основная функция запуска -
async def main():
    global bot
    logger.info("🚀 Запуск бота...")
    if not BOT_TOKEN:
        raise ValueError("❌ BOT_TOKEN не найден в переменных окружения")

    # Инициализация cookies из переменных окружения
    init_cookies_from_env()
    load_user_settings()

    # Инициализация Playwright
    await init_instagram_playwright()
    await init_youtube_playwright()

    bot = Bot(token=BOT_TOKEN, session=AiohttpSession())

    WEBHOOK_PATH = "/"
    WEBHOOK_URL = f"{WEBHOOK_HOST}{WEBHOOK_PATH}" if WEBHOOK_HOST else None

    if WEBHOOK_URL:
        # - Режим Webhook -
        logger.info(f"📡 Работаю в режиме Webhook: {WEBHOOK_URL}")
        await bot.set_webhook(WEBHOOK_URL)
        app = aiohttp.web.Application()
        from aiogram.webhook.aiohttp_server import SimpleRequestHandler
        webhook_requests_handler = SimpleRequestHandler(dispatcher=dp, bot=bot)
        webhook_requests_handler.register(app, path=WEBHOOK_PATH)

        # Установка диспетчера для вебхука
        await dp.start_polling(bot) # Это может быть не нужно для вебхука, см. документацию aiogram

        runner = aiohttp.web.AppRunner(app)
        await runner.setup()
        site = aiohttp.web.TCPSite(runner, '0.0.0.0', 8080) # Порт для вебхука
        await site.start()
        logger.info("🚀 Вебхук запущен на порту 8080")
        # Ожидание завершения (обычно через сигнал)
        await asyncio.Event().wait()
    else:
        # - Режим Polling -
        logger.info("🔄 Работаю в режиме Polling")
        try:
            await dp.start_polling(bot)
        finally:
            save_user_settings()
            logger.info("🛑 Бот остановлен (Polling)")

    # Закрытие браузеров Playwright при завершении
    if IG_BROWSER:
        logger.info("🛑 Закрываю браузер Instagram Playwright...")
        await IG_BROWSER.close()
    if YT_BROWSER:
        logger.info("🛑 Закрываю браузер YouTube Playwright...")
        await YT_BROWSER.close()

if __name__ == "__main__":
    asyncio.run(main())