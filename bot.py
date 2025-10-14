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
    # Проверяем cookies_bot1/2/3 (Instagram) - могут использоваться в старом коде, если не удалены
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
    # Проверяем Instagram cookies (новая переменная)
    instagram_cookies = os.getenv("COOKIES_INSTAGRAM")
    if instagram_cookies:
        cookies_file = Path("cookies_instagram.txt") # Новый файл для Instagram
        with open(cookies_file, 'w', encoding='utf-8') as f:
            # Добавляем заголовок если его нет
            if not instagram_cookies.strip().startswith('#'):
                f.write("# Netscape HTTP Cookie File\n")
            f.write(instagram_cookies)
        logger.info(f"✅ Создан cookies_instagram.txt")
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

# --- НОВОЕ (Instagram Playwright - Обновлённая версия) ---
# Переменные для Instagram Playwright
IG_BROWSER: Optional[Browser] = None
IG_CONTEXT: Optional[BrowserContext] = None
IG_PLAYWRIGHT_READY = False

async def init_instagram_playwright():
    """
    Инициализирует браузер и контекст Playwright для Instagram.
    Загружает sessionid и ds_user_id из переменных окружения и применяет их к контексту.
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

        # Загружаем cookies из переменных окружения
        cookies_to_load = []
        # Проверяем cookies_instagram (предпочтительно)
        instagram_cookies_content = os.getenv("COOKIES_INSTAGRAM") or os.getenv("COOKIES_TXT") # Резервный вариант
        if instagram_cookies_content:
            logger.info(f"🍪 Загружаем Instagram cookies из переменной окружения")
            try:
                lines = instagram_cookies_content.strip().split('\n')
                for line in lines:
                    if line.startswith('#') or not line.strip():
                        continue
                    parts = line.strip().split('\t')
                    if len(parts) >= 7:
                        cookie_name = parts[5]
                        # Проверяем, нужна ли нам именно sessionid или ds_user_id
                        if cookie_name in ['sessionid', 'ds_user_id']:
                            cookies_to_load.append({
                                "name": cookie_name,
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
            logger.info(f"🍪 Переменная окружения COOKIES_INSTAGRAM/COOKIES_TXT не найдена, запуск без cookies.")

        # Создаём контекст
        IG_CONTEXT = await IG_BROWSER.new_context(
            user_agent='Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/118.0.5993.118 Safari/537.36'
        )

        # Применяем загруженные cookies
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
# --- /НОВОЕ (Instagram Playwright - Обновлённая версия) ---

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


# --- НОВОЕ (Instagram Playwright - Скачивание - Улучшенный поиск качества) ---
async def download_instagram_with_playwright(url: str, quality: str = "best") -> Tuple[Optional[str], Optional[List[str]], Optional[str]]:
    """
    Использует Playwright для скачивания Instagram постов/рилсов.
    Пытается получить и установить sessionid/ds_user_id из переменных окружения.
    Скачивает видео (в т.ч. reels) с наилучшим доступным качеством или 1080p, если возможно.
    Скачивает фото/карусель.
    """
    global IG_CONTEXT
    if not IG_PLAYWRIGHT_READY or not IG_CONTEXT:
        logger.error("❌ Instagram Playwright не инициализирован")
        return None, None, "❌ Playwright не готов"

    page: Optional[Page] = None
    temp_files = []
    try:
        logger.info(f"🌐 Открываю Instagram в Playwright для {url}")
        page = await IG_CONTEXT.new_page()
        page.set_default_timeout(60000) # 60 секунд
        page.set_default_navigation_timeout(60000)

        # Навигация
        await page.goto(url, wait_until='networkidle')
        logger.info("🌐 Страница загружена")

        # Ждём появления элементов, указывающих на контент
        await page.wait_for_selector('#react-root, #permalink-modal', timeout=15000)
        logger.info("✅ Основной контейнер найден")

        # Проверим, не появилась ли страница входа или ошибка
        page_title = await page.title()
        if "Log in" in page_title or "error" in page.content().lower():
            logger.warning("⚠️ Обнаружена страница входа или ошибка")
            return None, None, "❌ Требуется аутентификация или контент недоступен"

        # Определяем тип контента
        video_selector = 'video'
        image_selector = 'img'
        carousel_selector = 'ul._ac-3' # Обычно для каруселей

        video_elements = await page.query_selector_all(video_selector)
        image_elements = await page.query_selector_all(image_selector)
        carousel_element = await page.query_selector(carousel_selector)

        # Получаем описание (caption)
        description = None
        caption_selectors = [
            'article div._a9zs div', # Общий селектор для описания
            'article div._a9zs span', # Альтернатива
            'article header + div div', # Альтернатива
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

        # --- ЛОГИКА СКАЧИВАНИЯ ---
        if carousel_element:
            # КАРУСЕЛЬ
            logger.info("🖼️ Обнаружена карусель")
            media_elements = await carousel_element.query_selector_all('video, img')
            if not media_elements:
                logger.warning("⚠️ Не найдены медиаэлементы в карусели")
                return None, None, "❌ Не найдены медиафайлы в карусели"

            media_info = []
            for i, elem in enumerate(media_elements):
                elem_tag = await elem.get_attribute('tagName')
                if elem_tag.lower() == 'video':
                    # Пытаемся получить URL из source тегов или основного src
                    sources = await elem.query_selector_all('source')
                    video_url = None
                    if sources:
                        # Ищем source с наилучшим качеством
                        # Сортировка по атрибутам может помочь, но часто они одинаковы
                        # Берём первый, если нет явного указания
                        # Попробуем получить URL из основного тега video, если source нет
                        video_url = await elem.get_attribute('src')
                        if not video_url:
                            for source in sources:
                                src = await source.get_attribute('src')
                                # Простой выбор: первый source, можно улучшить
                                if src:
                                    video_url = src
                                    break
                    else:
                        video_url = await elem.get_attribute('src')

                    if video_url:
                        media_info.append({'url': video_url, 'type': 'video', 'index': i})
                elif elem_tag.lower() == 'img':
                    img_url = await elem.get_attribute('src') or await elem.get_attribute('data-src')
                    if img_url and 'placeholder' not in img_url:
                        media_info.append({'url': img_url, 'type': 'image', 'index': i})

            if not media_info:
                logger.warning("⚠️ Не найдены подходящие URL медиа в карусели")
                return None, None, "❌ Не найдены подходящие медиафайлы в карусели"

            async def download_single_media(media_item):
                url = media_item['url']
                media_type = media_item['type']
                index = media_item['index']
                ext = '.mp4' if media_type == 'video' else '.jpg'
                path = os.path.join(tempfile.gettempdir(), f"ig_carousel_{index}{ext}")
                if await download_file(url, path, timeout=60):
                    logger.debug(f"✅ Скачан {media_type} из карусели: {Path(path).name}")
                    return path
                return None

            tasks = [download_single_media(item) for item in media_info]
            downloaded_paths = await asyncio.gather(*tasks, return_exceptions=True)
            successful_paths = [p for p in downloaded_paths if isinstance(p, str) and os.path.exists(p)]
            if successful_paths:
                logger.info(f"✅ Скачано {len(successful_paths)} файлов из карусели")
                return None, successful_paths, description
            else:
                logger.error("❌ Не удалось скачать ни один файл из карусели")
                return None, None, "❌ Не удалось скачать файлы из карусели"

        elif video_elements:
            # ОДИНОЧНОЕ ВИДЕО (POST или REEL)
            logger.info("🎥 Обнаружено одиночное видео")
            video_elem = video_elements[0]

            # Пытаемся получить URL из source тегов внутри video
            sources = await video_elem.query_selector_all('source')
            video_url = None
            if sources:
                logger.debug(f"Найдено {len(sources)} source тегов")
                # Ищем source с наилучшим качеством
                # Это не всегда надёжно, но пробуем
                best_url = None
                best_quality = 0
                for source in sources:
                    src = await source.get_attribute('src')
                    if not src:
                        continue
                    # Простая эвристика: ищем в URL признаки качества (не всегда работает)
                    if '1080' in src or 'hd' in src.lower():
                        best_url = src
                        best_quality = 1080
                        break # Нашли 1080p, останавливаемся
                    elif '720' in src:
                        if best_quality < 720:
                            best_url = src
                            best_quality = 720
                    elif '480' in src:
                        if best_quality < 480:
                            best_url = src
                            best_quality = 480
                    # Если не нашли ничего по эвристике, берём первый
                    elif not best_url:
                        best_url = src

                video_url = best_url
                logger.debug(f"Выбран URL из source с приоритетом качества: {best_quality if best_url else 'None'}")
            else:
                # Если source нет, пробуем получить src из самого video тега
                video_url = await video_elem.get_attribute('src')
                logger.debug(f"Получен URL из video.src: {video_url is not None}")

            if video_url:
                video_path = os.path.join(tempfile.gettempdir(), f"ig_video.mp4")
                if await download_file(video_url, video_path, timeout=120):
                    logger.info(f"✅ Видео скачано: {Path(video_path).name}")
                    return video_path, None, description
                else:
                    logger.error("❌ Не удалось скачать видео по URL")
                    return None, None, "❌ Не удалось скачать видео"
            else:
                logger.error("❌ Не удалось получить URL видео")
                return None, None, "❌ Не найден URL видео"

        elif image_elements:
            # ОДИНОЧНОЕ ФОТО (POST)
            logger.info("🖼️ Обнаружено одиночное фото")
            img_elem = image_elements[0]
            img_url = await img_elem.get_attribute('src') or await img_elem.get_attribute('data-src')
            if img_url and 'placeholder' not in img_url:
                photo_path = os.path.join(tempfile.gettempdir(), f"ig_photo.jpg")
                if await download_file(img_url, photo_path, timeout=60):
                    logger.info(f"✅ Фото скачано: {Path(photo_path).name}")
                    return None, [photo_path], description
                else:
                    logger.error("❌ Не удалось скачать фото по URL")
                    return None, None, "❌ Не удалось скачать фото"
            else:
                logger.error("❌ Не удалось получить URL фото")
                return None, None, "❌ Не найден URL фото"

        else:
            logger.error("❌ Не удалось определить тип контента")
            return None, None, "❌ Не удалось определить тип контента"

    except Exception as e:
        logger.error(f"❌ Ошибка в download_instagram_with_playwright: {e}")
        return None, None, f"❌ Ошибка: {str(e)}"

    finally:
        if page:
            await page.close()

# --- /НОВОЕ (Instagram Playwright - Скачивание - Улучшенный поиск качества) ---


# === 📸 ОБНОВЛЁННАЯ ФУНКЦИЯ СКАЧИВАНИЯ INSTAGRAM ===
async def download_instagram(url: str, quality: str = "best", user_id: Optional[int] = None) -> Tuple[Optional[str], Optional[List[str]], Optional[str]]:
    """
    Главная функция скачивания Instagram.
    Использует только Playwright.
    """
    logger.info(f"🔄 Скачивание Instagram (качество={quality}) через Playwright...")
    # Используем только новый метод
    video_path, photos, description = await download_instagram_with_playwright(url, quality)
    if video_path or photos:
        logger.info(f"✅ Instagram контент успешно скачан")
        return video_path, photos, description
    else:
        logger.info(f"❌ Не удалось скачать Instagram контент: {description}")
        return None, None, description


# --- НОВОЕ (Instagram Content Sending) ---
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
# --- /НОВОЕ (Instagram Content Sending) ---


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


# === 📤 ОБНОВЛЁННАЯ ФУНКЦИЯ СКАЧИВАНИЯ ВИДЕО (YouTube Playwright) ===
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