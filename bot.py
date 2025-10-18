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
    Message, FSInputFile, InputMediaPhoto, ReplyKeyboardMarkup, KeyboardButton,
    CallbackQuery, FSInputFile
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

# --- Глобальные переменные ---
BOT_TOKEN = os.getenv("BOT_TOKEN")
WEBHOOK_HOST = os.getenv("WEBHOOK_HOST")  # Опционально, если используется webhook
YT_BROWSER: Optional[Browser] = None
YT_CONTEXT: Optional[BrowserContext] = None
YT_PLAYWRIGHT_READY = False
IG_BROWSER: Optional[Browser] = None
IG_CONTEXT: Optional[BrowserContext] = None
IG_PLAYWRIGHT_READY = False
bot: Optional[Bot] = None
dp = Dispatcher()
user_settings = {}  # Словарь для хранения настроек качества для пользователей
RATE_LIMIT_DELAY = {}  # Словарь для отслеживания задержки на пользователя (если нужно)

# --- Настройка состояний FSM ---
class VideoStates(StatesGroup):
    choosing_quality = State()

# --- Определение доступных качеств ---
QUALITY_FORMATS = {
    "best": 'bestvideo[ext=mp4]+bestaudio[ext=m4a]/bestvideo+bestaudio/best',
    "1080p": 'bestvideo[ext=mp4][height<=1920][width<=1920]+bestaudio[ext=m4a]/bestvideo[height<=1920]+bestaudio/best',
    "720p": 'bestvideo[ext=mp4][height<=1280][width<=1280]+bestaudio[ext=m4a]/bestvideo[height<=1280]+bestaudio/best',
    "480p": 'bestvideo[ext=mp4][height<=854][width<=854]+bestaudio[ext=m4a]/bestvideo[height<=854]+bestaudio/best',
    "360p": 'bestvideo[ext=mp4][height<=640][width<=640]+bestaudio[ext=m4a]/bestvideo[height<=640]+bestaudio/best',
}

# --- Функция для получения настроек качества пользователя ---
def get_quality_setting(user_id: int) -> str:
    return user_settings.get(user_id, "best") # По умолчанию "best"

# --- Функция для получения опций yt-dlp ---
def get_ydl_opts(quality: str, use_youtube_cookies: bool = True) -> Dict[str, Any]:
    opts = {
        'format': quality,
        'outtmpl': '%(id)s.%(ext)s',
        'noplaylist': True,
        'extractaudio': False,
        'audioformat': 'mp4',
        'nocheckcertificate': True,
        'ignoreerrors': False,
        'no_warnings': True,
        'quiet': True,
    }
    if use_youtube_cookies:
        cookie_file = Path("cookies_youtube.txt")
        if cookie_file.exists():
            opts['cookiefile'] = str(cookie_file)
            logger.info(f"🍪 Использую cookies из {cookie_file.name}")
        else:
            logger.info(f"🍪 Файл {cookie_file.name} не найден, запуск без cookies.")
    return opts

# --- Функция для инициализации cookies из переменных окружения ---
def init_cookies_from_env():
    cookies_created = 0
    # Cookies из переменной COOKIES_TXT
    cookies_txt_content = os.getenv("COOKIES_TXT")
    if cookies_txt_content:
        cookies_file = Path("cookies.txt")
        with open(cookies_file, 'w', encoding='utf-8') as f:
            f.write(cookies_txt_content)
        logger.info(f"✅ Создан {cookies_file.name}")
        cookies_created += 1

    # Cookies для ботов
    for i in range(1, 4):
        env_var = f"COOKIES_BOT{i}"
        content = os.getenv(env_var)
        if content:
            cookies_file = Path(f"cookies_bot{i}")
            with open(cookies_file, 'w', encoding='utf-8') as f:
                f.write(content)
            logger.info(f"✅ Создан {cookies_file.name}")
            cookies_created += 1

    # Cookies для YouTube
    youtube_cookies_content = os.getenv("COOKIES_YOUTUBE")
    if youtube_cookies_content:
        cookies_file = Path("cookies_youtube.txt")
        with open(cookies_file, 'w', encoding='utf-8') as f:
            f.write(youtube_cookies_content)
        logger.info(f"✅ Создан {cookies_file.name}")
        cookies_created += 1

    logger.info(f"✅ Создано {cookies_created} файлов cookies")

# --- Функция для инициализации Playwright для Instagram ---
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
                    try:
                        parts = line.strip().split('\t')
                        if len(parts) < 7:
                            continue  # Пропускаем строки с недостаточным количеством полей
                        # Парсим числовые поля
                        expires_val = int(parts[4]) if parts[4].isdigit() else None
                        http_only_val = bool(int(parts[3])) if parts[3].isdigit() else False
                        secure_val = bool(int(parts[1])) if parts[1].isdigit() else False

                        cookies_to_load.append({
                            "name": parts[5],
                            "value": parts[6],
                            "domain": parts[0],
                            "path": parts[2],
                            "expires": expires_val,
                            "httpOnly": http_only_val,
                            "secure": secure_val
                        })
                    except (ValueError, IndexError) as e:
                        logger.debug(f"⚠️ Пропущена некорректная строка cookie: {line[:50]} ({e})")
                        continue  # Переходим к следующей строке
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

# --- Функция для инициализации Playwright для YouTube ---
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
                        # Пропускаем комментарии и пустые строки
                        if line.startswith('#') or not line.strip():
                            continue
                        try:
                            parts = line.strip().split('\t')
                            if len(parts) < 7:
                                continue  # Пропускаем строки с недостаточным количеством полей
                            # Парсим числовые поля
                            expires_val = int(parts[4]) if parts[4].isdigit() else None
                            http_only_val = bool(int(parts[3])) if parts[3].isdigit() else False
                            secure_val = bool(int(parts[1])) if parts[1].isdigit() else False

                            cookies_to_load.append({
                                "name": parts[5],
                                "value": parts[6],
                                "domain": parts[0],
                                "path": parts[2],
                                "expires": expires_val,
                                "httpOnly": http_only_val,
                                "secure": secure_val
                            })
                        except (ValueError, IndexError) as e:
                            logger.debug(f"⚠️ Пропущена некорректная строка cookie: {line[:50]} ({e})")
                            continue  # Переходим к следующей строке
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

# --- Функция для очистки файлов ---
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

# --- Функция для скачивания видео с YouTube ---
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
        logger.error(f"❌ Ошибка скачивания с YouTube: {e}")
        # Если первая попытка с куки не удалась, пробуем без куки
        logger.info("🔄 Повторная попытка без cookies...")
        ydl_opts_no_cookie = get_ydl_opts(quality, use_youtube_cookies=False)
        try:
            with yt_dlp.YoutubeDL(ydl_opts_no_cookie) as ydl:
                info = ydl.extract_info(url, download=True)
                temp_file = ydl.prepare_filename(info)
                if temp_file and os.path.exists(temp_file):
                    logger.info(f"✅ Видео скачано (без cookies): {Path(temp_file).name}")
                    return temp_file
                else:
                    logger.error("❌ yt-dlp не создал файл (без cookies)")
                    return None
        except Exception as e2:
            logger.error(f"❌ Ошибка при повторной попытке: {e2}")
            return None
    except Exception as e:
        logger.error(f"❌ Неизвестная ошибка при скачивании с YouTube: {e}")
        return None

# --- Функция для скачивания видео с YouTube через Playwright (если основной способ не работает) ---
async def download_youtube_with_playwright(url: str, quality: str = "best") -> Optional[str]:
    global YT_CONTEXT
    if not YT_PLAYWRIGHT_READY or not YT_CONTEXT:
        logger.error("❌ YouTube Playwright не инициализирован")
        return None

    logger.info(f"🔄 Скачивание видео с YOUTUBE через Playwright...")
    page = None
    temp_cookies_file = None
    try:
        page = await YT_CONTEXT.new_page()
        await page.goto(url, wait_until='networkidle')
        # Ждём загрузки контента
        await page.wait_for_selector('ytd-video-primary-info-renderer', timeout=10000)

        # Извлекаем куки из контекста Playwright
        cookies = await YT_CONTEXT.cookies()
        logger.info(f"🍪 Извлечено {len(cookies)} куки из Playwright контекста")

        # Создаём временный файл куки для yt-dlp
        temp_cookies_file = Path(tempfile.mktemp(suffix='.txt', prefix='yt_cookies_'))
        with open(temp_cookies_file, 'w', encoding='utf-8') as f:
            f.write("# Netscape HTTP Cookie File\n") # Заголовок Netscape cookie file
            for cookie in cookies:
                # Правильный порядок: domain flag path secure expires name value
                # flag: TRUE если домен включает поддомены, FALSE если нет
                # secure: TRUE если куки требует HTTPS, FALSE если нет
                # expires: timestamp (целое число)
                domain = cookie['domain']
                flag = 'TRUE' if domain.startswith('.') else 'FALSE'
                path = cookie['path']
                secure = 'TRUE' if cookie['secure'] else 'FALSE'
                expires = str(cookie['expires']) if cookie['expires'] else '0'
                name = cookie['name']
                value = cookie['value']
                f.write(f"{domain}\t{flag}\t{path}\t{secure}\t{expires}\t{name}\t{value}\n")

        # Настройки для yt-dlp с использованием временного файла куки
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

# --- Функция для скачивания видео с TikTok ---
async def download_tiktok(url: str, quality: str = "1080p") -> Optional[str]:
    logger.info(f"🔄 Скачивание видео с TIKTOK (качество={quality})...")
    # yt-dlp сам определяет качество для TikTok, но мы можем попробовать указать формат
    # Однако, для TikTok часто работает просто 'best'
    # Попробуем использовать 'best' или 'best[height<=1080]'
    format_str = 'best[height<=1080]' if quality == '1080p' else 'best'
    ydl_opts = {
        'format': format_str,
        'outtmpl': '%(id)s.%(ext)s',
        'noplaylist': True,
        'extractaudio': False,
        'nocheckcertificate': True,
        'ignoreerrors': False,
        'no_warnings': True,
        'quiet': True,
    }
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

# --- Функция для скачивания фото с TikTok (карусель) ---
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
            temp_dir = ydl.prepare_filename(info).split('%(autonumber)s')[0].rstrip('/')
            if os.path.exists(temp_dir):
                photos = [os.path.join(temp_dir, f) for f in sorted(os.listdir(temp_dir)) if f.lower().endswith(('.jpg', '.jpeg', '.png', '.webp'))]
                logger.info(f"✅ Скачано {len(photos)} фото: {photos}")
                description = info.get('description', '') or info.get('title', '')
                return photos, description
            else:
                logger.error("❌ yt-dlp не создал директорию для фото")
                return None, "❌ Не удалось создать директорию для фото"
    except Exception as e:
        logger.error(f"❌ Ошибка при скачивании фото с TikTok: {e}")
        return None, str(e)

# --- Функция для скачивания с Instagram ---
async def download_instagram(url: str) -> Tuple[Optional[str], Optional[List[str]], str]:
    logger.info(f"🔄 Скачивание медиа с INSTAGRAM...")
    # yt-dlp для Instagram
    ydl_opts = {
        'format': 'best',
        'outtmpl': '%(id)s.%(ext)s',
        'noplaylist': True,
        'extractaudio': False,
        'nocheckcertificate': True,
        'ignoreerrors': False,
        'no_warnings': True,
        'quiet': True,
    }
    # Попробуем сначала через yt-dlp
    try:
        with yt_dlp.YoutubeDL(ydl_opts) as ydl:
            info = ydl.extract_info(url, download=True)
            temp_file = ydl.prepare_filename(info)
            description = info.get('description', '') or info.get('title', '')
            if temp_file and os.path.exists(temp_file):
                logger.info(f"✅ Медиа (вероятно видео) скачано: {Path(temp_file).name}")
                return temp_file, None, description
            else:
                # yt-dlp мог не создать файл, если это, например, только фото
                # Попробуем получить фото из info
                entries = info.get('entries')
                if entries:
                    photos = []
                    for entry in entries:
                        # yt-dlp может включать фото как отдельные entry
                        # Или фото могут быть в thumbnails
                        # Попробуем thumbnails
                        thumbnails = entry.get('thumbnails')
                        if thumbnails:
                            # Берём последнее (наиболее качественное) фото
                            best_thumb = thumbnails[-1]
                            thumb_url = best_thumb.get('url')
                            if thumb_url:
                                # Скачиваем фото напрямую
                                temp_photo = await download_file_ig(thumb_url, f"{entry.get('id', 'ig_photo')}.jpg")
                                if temp_photo:
                                    photos.append(temp_photo)
                        else:
                            # Попробуем получить media из entry, если это фото
                            # yt-dlp не всегда сохраняет фото как файл напрямую
                            # Поэтому возвратим None и попробуем Playwright
                            pass
                    if photos:
                        logger.info(f"✅ Скачано {len(photos)} фото из Instagram: {photos}")
                        return None, photos, description
                logger.info("🔄 yt-dlp не вернул медиа, пробуем Playwright...")
                # Если yt-dlp не нашёл медиа, пробуем Playwright
                return await download_instagram_with_playwright(url)
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

# --- Функция для скачивания с Instagram через Playwright ---
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
        page_title = await page.title()
        page_content = await page.content()
        if "Log in" in page_title or "error" in page_content.lower():
            logger.warning("⚠️ [РЕЗЕРВ] Обнаружена страница входа или ошибка")
            return None, None, "❌ [РЕЗЕРВ] Требуется аутентификация или контент недоступен"

        content_selectors = ['#react-root', 'article', 'main', 'div[role="button"]']
        content_found = False
        for selector in content_selectors:
            try:
                await page.wait_for_selector(selector, timeout=10000)
                content_found = True
                break
            except:
                continue

        if not content_found:
            logger.error("❌ [РЕЗЕРВ] Не найден контейнер с медиа")
            return None, None, "❌ [РЕЗЕРВ] Не найден контейнер с медиа"

        # Попробуем найти видео
        video_selector = 'video'
        video_element = await page.query_selector(video_selector)
        if video_element:
            video_url = await video_element.get_attribute('src')
            if video_url:
                temp_file = await download_file_ig(video_url, f"ig_video_{hash(url)}.mp4")
                if temp_file:
                    logger.info(f"✅ Видео с Instagram скачано через Playwright: {Path(temp_file).name}")
                    # Попробуем получить описание
                    description_element = await page.query_selector('article div[style*="margin"] span')
                    description = await description_element.inner_text() if description_element else ""
                    return temp_file, None, description
                else:
                    logger.error("❌ [РЕЗЕРВ] Не удалось скачать видео")
                    return None, None, "❌ [РЕЗЕРВ] Не удалось скачать видео"
            else:
                logger.error("❌ [РЕЗЕРВ] Не удалось получить URL видео")
                return None, None, "❌ [РЕЗЕРВ] Не найден URL видео"

        # Попробуем найти фото (одиночное или в карусели)
        img_selector = 'article img'
        img_elements = await page.query_selector_all(img_selector)
        if img_elements:
            photos = []
            for i, img_element in enumerate(img_elements):
                img_url = await img_element.get_attribute('src')
                if img_url:
                    temp_photo = await download_file_ig(img_url, f"ig_photo_{hash(url)}_{i}.jpg")
                    if temp_photo:
                        photos.append(temp_photo)
            if photos:
                logger.info(f"✅ Скачано {len(photos)} фото с Instagram через Playwright: {photos}")
                # Попробуем получить описание
                description_element = await page.query_selector('article div[style*="margin"] span')
                description = await description_element.inner_text() if description_element else ""
                return None, photos, description
            else:
                logger.error("❌ [РЕЗЕРВ] Не удалось скачать фото")
                return None, None, "❌ [РЕЗЕРВ] Не удалось скачать фото"
        else:
            logger.error("❌ [РЕЗЕРВ] Не найдено подходящего элемента изображения")
            return None, None, "❌ [РЕЗЕРВ] Не найдено подходящего изображения"

        logger.error("❌ [РЕЗЕРВ] Не удалось определить тип контента")
        return None, None, "❌ [РЕЗЕРВ] Не удалось определить тип контента"

    except Exception as e:
        logger.error(f"❌ Ошибка в download_instagram_with_playwright: {e}")
        return None, None, f"❌ Ошибка: {str(e)}"
    finally:
        if page:
            await page.close()

# --- Вспомогательная функция для скачивания файла ---
async def download_file_ig(url: str, save_path: str, timeout: int = 60) -> Optional[str]:
    try:
        async with aiohttp.ClientSession() as session:
            async with session.get(url, timeout=aiohttp.ClientTimeout(total=timeout)) as resp:
                if resp.status == 200:
                    with open(save_path, 'wb') as f:
                        f.write(await resp.read())
                    return save_path
                else:
                    logger.error(f"❌ Ошибка загрузки файла {url}: статус {resp.status}")
                    return None
    except Exception as e:
        logger.error(f"❌ Ошибка загрузки файла {url}: {e}")
        return None

# --- Функция для отправки видео или фото ---
async def send_video_or_message(chat_id: int, file_path: str, caption: str = ""):
    if not file_path or not os.path.exists(file_path):
        logger.error(f"❌ Файл не существует: {file_path}")
        return False

    file_size = os.path.getsize(file_path)
    size_mb = file_size / (1024 * 1024)

    # Проверяем, является ли файл фото или видео
    file_ext = Path(file_path).suffix.lower()
    is_photo = file_ext in ['.jpg', '.jpeg', '.png', '.webp']

    if size_mb > 50:
        # Если файл больше 50 МБ, пробуем загрузить на файлообменник
        logger.info(f"📦 Файл ({size_mb:.1f} МБ) слишком большой для Telegram. Пробуем file.io...")
        link = await upload_to_fileio(file_path)
        if link:
            await bot.send_message(
                chat_id=chat_id,
                text=f"📦 Медиафайл слишком большой для Telegram ({size_mb:.1f} МБ), но доступен по ссылке:\n"
                     f"📥 Скачать: {link}\n"
                     f"⚠️ Внимание: Telegram не позволяет отправлять файлы больше 50 МБ напрямую."
            )
            logger.info(f"✅ Ссылка на файл отправлена через file.io")
            return True
        else:
            await bot.send_message(
                chat_id=chat_id,
                text=f"❌ Не удалось отправить медиафайл ({size_mb:.1f} МБ) и загрузить на файлообменник."
            )
            logger.warning(f"❌ Не удалось отправить файл ({size_mb:.1f} МБ) ни напрямую, ни через file.io.")
            return False
    else:
        # Если файл <= 50 МБ, пробуем отправить напрямую
        try:
            if is_photo:
                await bot.send_photo(chat_id=chat_id, photo=FSInputFile(file_path), caption=caption)
            else: # Предполагаем, что это видео
                await bot.send_video(chat_id=chat_id, video=FSInputFile(file_path), caption=caption)
            logger.info(f"✅ Медиа отправлено ({size_mb:.1f} МБ)")
            return True
        except TelegramBadRequest as e:
            logger.error(f"Ошибка отправки в Telegram: {e}")
            # Даже если TelegramBadRequest не связан с размером, всё равно пробуем файлообменник
            logger.info(f"📦 Ошибка отправки, пробуем file.io... (файл {size_mb:.1f} МБ)")
            link = await upload_to_fileio(file_path)
            if link:
                await bot.send_message(
                    chat_id=chat_id,
                    text=f"📦 Медиафайл не удалось отправить напрямую ({size_mb:.1f} МБ), но доступен по ссылке:\n"
                         f"📥 Скачать: {link}"
                )
                logger.info(f"✅ Ссылка на файл отправлена через file.io")
                return True
            else:
                await bot.send_message(
                    chat_id=chat_id,
                    text=f"❌ Не удалось отправить медиафайл ({size_mb:.1f} МБ) и загрузить на файлообменник."
                )
                logger.warning(f"❌ Не удалось отправить файл ({size_mb:.1f} МБ) ни напрямую, ни через file.io.")
                return False
        except Exception as e:
            logger.error(f"❌ Неизвестная ошибка при отправке: {e}")
            return False

# --- Функция для загрузки файла на file.io ---
async def upload_to_fileio(file_path: str) -> Optional[str]:
    try:
        async with aiohttp.ClientSession() as session:
            with open(file_path, 'rb') as f:
                data = aiohttp.FormData()
                data.add_field('file', f, filename=Path(file_path).name)
                async with session.post('https://file.io/', data=data) as resp:
                    if resp.status == 200:
                        response_json = await resp.json()
                        if response_json.get('success'):
                            return response_json.get('link')
                        else:
                            logger.error(f"❌ file.io вернул ошибку: {response_json.get('message')}")
                            return None
                    else:
                        logger.error(f"❌ file.io вернул статус: {resp.status}")
                        return None
    except Exception as e:
        logger.error(f"❌ Ошибка загрузки на file.io: {e}")
        return None

# --- Функция для создания основной клавиатуры ---
def main_keyboard() -> ReplyKeyboardMarkup:
    keyboard = [
        [KeyboardButton(text="🎬 Инструкция")],
        [KeyboardButton(text="⚙️ Настройки")],
    ]
    return ReplyKeyboardMarkup(keyboard=keyboard, resize_keyboard=True)

# --- Обработчик команды /start ---
@dp.message(Command("start"))
async def cmd_start(message: Message):
    user_id = message.from_user.id
    welcome_text = (
        f"🎬 <b>Добро пожаловать в VideoBot!</b>\n"
        f"Я могу скачать видео с:\n"
        f"• YouTube\n"
        f"• TikTok\n"
        f"• Instagram (посты, reels, карусели)\n"
        f"📲 Просто отправь мне ссылку!\n"
        f"⚙️ Текущее качество: <b>{get_quality_setting(user_id).upper()}</b>"
    )
    await message.answer(welcome_text, reply_markup=main_keyboard(), parse_mode="HTML")

# --- Обработчик команды /help ---
@dp.message(F.text == "🎬 Инструкция")
async def send_welcome(message: Message):
    help_text = (
        "🎬 <b>Инструкция по использованию VideoBot</b>\n\n"
        "1. Отправьте боту ссылку на видео с YouTube, TikTok или Instagram.\n"
        "2. Бот скачает и отправит вам видео (или фото, если это карусель).\n"
        "3. Вы можете изменить качество видео в настройках.\n\n"
        "⚠️ <b>Важно:</b> Некоторые видео могут быть недоступны из-за ограничений платформы или приватности."
    )
    await message.answer(help_text, parse_mode="HTML")

# --- Обработчик команды /settings ---
@dp.message(F.text == "⚙️ Настройки")
async def settings_menu(message: Message, state: FSMContext):
    await state.set_state(VideoStates.choosing_quality)
    current = get_quality_setting(message.from_user.id)
    keyboard = ReplyKeyboardMarkup(
        keyboard=[
            [KeyboardButton(text=q.upper()) for q in QUALITY_FORMATS.keys()],
            [KeyboardButton(text="◀️ Назад")]
        ],
        resize_keyboard=True
    )
    await message.answer(
        f"⚙️ Текущее качество: <b>{current.upper()}</b>\nВыберите новое:",
        reply_markup=keyboard,
        parse_mode="HTML"
    )

# --- Обработчик выбора качества ---
@dp.message(VideoStates.choosing_quality, F.text.in_(list(QUALITY_FORMATS.keys()) + ["◀️ Назад"]))
async def process_quality_choice(message: Message, state: FSMContext):
    choice = message.text.lower()
    if choice == "назад":
        await state.clear()
        await message.answer("⚙️ Настройки закрыты.", reply_markup=main_keyboard())
        return

    if choice in QUALITY_FORMATS:
        user_settings[message.from_user.id] = choice
        await state.clear()
        await message.answer(
            f"✅ Качество установлено на <b>{choice.upper()}</b>.",
            reply_markup=main_keyboard(),
            parse_mode="HTML"
        )
    else:
        await message.answer("❌ Некорректный выбор. Пожалуйста, выберите из предложенных вариантов.")

# --- Обработчик ссылок ---
@dp.message(F.text)
async def handle_link(message: Message):
    # Проверяем, находится ли пользователь в состоянии выбора качества
    state = await dp.current_state(chat=message.chat.id, user=message.from_user.id)
    if state == VideoStates.choosing_quality:
        # Если да, передаем управление обработчику выбора качества
        # Это позволит избежать конфликта между обработкой ссылок и выбора качества
        return await process_quality_choice(message, state)

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
    temp_photos = []

    try:
        # Замените эти вызовы на фактические функции скачивания
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
                    media_group = [
                        InputMediaPhoto(
                            media=FSInputFile(photo),
                            caption=description if i == 0 else None # Подпись только к первому фото
                        )
                        for i, photo in enumerate(photos)
                    ]
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
                await send_video_or_message(message.chat.id, temp_file, caption=description)
                cleanup_file(temp_file)
                return
            elif photos:
                temp_photos = photos
                media_group = [
                    InputMediaPhoto(
                        media=FSInputFile(photo),
                        caption=description if i == 0 else None
                    )
                    for i, photo in enumerate(photos)
                ]
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
            # ИСПРАВЛЕНО: Теперь вызывается обновлённая send_video_or_message
            await send_video_or_message(message.chat.id, temp_file)
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


# --- Основная функция запуска ---
async def main():
    global bot
    logger.info("🚀 Запуск бота...")

    if not BOT_TOKEN:
        raise ValueError("❌ BOT_TOKEN не найден в переменных окружения")

    # Инициализация cookies из переменных окружения
    init_cookies_from_env()

    # Инициализация Playwright
    await init_instagram_playwright()
    await init_youtube_playwright()

    bot = Bot(token=BOT_TOKEN, session=AiohttpSession())

    WEBHOOK_PATH = "/"
    WEBHOOK_URL = f"{WEBHOOK_HOST}{WEBHOOK_PATH}" if WEBHOOK_HOST else None

    if WEBHOOK_URL:
        # --- Режим Webhook ---
        logger.info(f"📡 Работаю в режиме Webhook: {WEBHOOK_URL}")
        await bot.set_webhook(WEBHOOK_URL)
        app = aiohttp.web.Application()
        from aiogram.webhook.aiohttp_server import SimpleRequestHandler
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
            logger.info("🛑 Бот остановлен (Webhook)")
        finally:
            await bot.delete_webhook(drop_pending_updates=True)
            await runner.cleanup()
    else:
        # --- Режим Polling ---
        logger.info("🔄 Работаю в режиме Polling")
        try:
            await dp.start_polling(bot, skip_updates=True)
        except (KeyboardInterrupt, SystemExit):
            logger.info("🛑 Бот остановлен (Polling)")
        finally:
            await bot.session.close()

    # Закрытие браузеров Playwright при завершении
    if IG_BROWSER:
        logger.info("🛑 Закрываю браузер Instagram Playwright...")
        await IG_BROWSER.close()
    if YT_BROWSER:
        logger.info("🛑 Закрываю браузер YouTube Playwright...")
        await YT_BROWSER.close()

if __name__ == "__main__":
    asyncio.run(main())