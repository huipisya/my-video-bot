import os
import tempfile
import asyncio
import logging
import re
import hashlib
import json
import time  # <-- добавлено для корректной работы кэша
from pathlib import Path
from typing import Optional, Tuple, List
import aiohttp
from aiogram import Bot, Dispatcher, types, F
from aiogram.filters import Command
from aiogram.fsm.state import State, StatesGroup
from aiogram.fsm.context import FSMContext
from aiogram.types import ReplyKeyboardMarkup, KeyboardButton, FSInputFile
from aiogram.exceptions import TelegramBadRequest
from dotenv import load_dotenv
import yt_dlp
import instaloader
import pickle
import sys

sys.stdout.reconfigure(encoding='utf-8')

# === 🧰 НАСТРОЙКА ЛОГИРОВАНИЯ ===
logging.basicConfig(
    level=logging.INFO,
    format='%(asctime)s - %(levelname)s - %(message)s'
)
logger = logging.getLogger(__name__)

# === 🔐 ЗАГРУЗКА ТОКЕНА ===
load_dotenv()
BOT_TOKEN = os.getenv("BOT_TOKEN")
if not BOT_TOKEN:
    raise ValueError("❌ BOT_TOKEN не найден в переменных окружения")

bot = Bot(token=BOT_TOKEN)
dp = Dispatcher()

# === 🧠 ХРАНИЛИЩЕ НАСТРОЕК ===
user_settings = {}

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

# === 🧺 НАСТРОЙКИ КЭША ===
CACHE_DIR = Path("cache")
CACHE_DIR.mkdir(exist_ok=True)
CACHE_TTL = 3600  # 1 час

# === 🛠 ВСПОМОГАТЕЛЬНЫЕ ФУНКЦИИ ===
def get_quality_setting(user_id: int) -> str:
    """Получить настройку качества для пользователя"""
    return user_settings.get(user_id, "best")

def get_ydl_opts(quality: str = "best") -> dict:
    """Получить настройки yt-dlp"""
    return {
        'format': QUALITY_FORMATS.get(quality, QUALITY_FORMATS["best"]),
        'merge_output_format': 'mp4',
        'noplaylist': True,
        'outtmpl': os.path.join(tempfile.gettempdir(), '%(id)s.%(ext)s'),
        'quiet': True,
        'no_warnings': True,
        'http_headers': {
            'User-Agent': 'Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36'
        },
        'extractor_args': {
            'youtube': {
                'skip': ['hls', 'dash'],
            }
        }
    }

def is_valid_url(url: str) -> bool:
    """Проверка валидности URL"""
    regex = re.compile(
        r'^(https?://)?(www\.)?'
        r'(youtube\.com|youtu\.be|tiktok\.com|instagram\.com|vm\.tiktok\.com|vt\.tiktok\.com)',
        re.IGNORECASE
    )
    return bool(re.match(regex, url))

def detect_platform(url: str) -> str:
    """Определить платформу по URL"""
    url_lower = url.lower()
    if 'youtube.com' in url_lower or 'youtu.be' in url_lower:
        return 'youtube'
    elif 'tiktok.com' in url_lower or 'vm.tiktok.com' in url_lower or 'vt.tiktok.com' in url_lower:
        return 'tiktok'
    elif 'instagram.com' in url_lower:
        return 'instagram'
    return 'unknown'

def get_cache_key(url: str) -> str:
    """Создать уникальный ключ для кэша"""
    return hashlib.md5(url.encode()).hexdigest()

def save_to_cache(key: str, data: any) -> None:
    """Сохранить данные в кэш"""
    try:
        cache_file = CACHE_DIR / f"{key}.pkl"
        with open(cache_file, 'wb') as f:
            pickle.dump(data, f)
    except Exception as e:
        logger.error(f"Ошибка сохранения в кэш: {e}")

def load_from_cache(key: str) -> Optional[any]:
    """Загрузить данные из кэша"""
    try:
        cache_file = CACHE_DIR / f"{key}.pkl"
        if cache_file.exists():
            # Проверка на устаревание кэша
            if (os.path.getmtime(cache_file) + CACHE_TTL) > time.time():  # <-- исправлено
                with open(cache_file, 'rb') as f:
                    return pickle.load(f)
            else:
                cache_file.unlink()  # Удалить устаревший кэш
    except Exception as e:
        logger.error(f"Ошибка загрузки из кэша: {e}")
    return None

async def download_file(url: str, save_path: str, timeout: int = 60) -> bool:
    """Скачать файл по URL"""
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

# === 📥 СКАЧИВАНИЕ INSTAGRAM - МЕТОД 1 (Instaloader) ===
async def download_instagram_instaloader(url: str, shortcode: str) -> Tuple[Optional[str], Optional[List[str]], Optional[str]]:
    """Скачать контент из Instagram через Instaloader"""
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
                logger.info("✅ Instagram: видео скачано через Instaloader")
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
                logger.info("✅ Instagram: фото скачано через Instaloader")
                return (None, photos, description)

    except Exception as e:
        logger.error(f"❌ Instagram Instaloader: {e}")
    
    return None, None, None

# === 📥 СКАЧИВАНИЕ INSTAGRAM - МЕТОД 2 (yt-dlp) ===
async def download_instagram_ytdlp(url: str, quality: str) -> Tuple[Optional[str], Optional[List[str]], Optional[str]]:
    """Скачать контент из Instagram через yt-dlp"""
    try:
        logger.info("🔄 Instagram: попытка через yt-dlp...")
        ydl_opts = get_ydl_opts(quality)
        with yt_dlp.YoutubeDL(ydl_opts) as ydl:
            info = ydl.extract_info(url, download=True)
            temp_file = ydl.prepare_filename(info)
            if temp_file and os.path.exists(temp_file):
                logger.info("✅ Instagram: скачано через yt-dlp")
                return (temp_file, None, None)
    except Exception as e:
        logger.error(f"❌ Instagram yt-dlp: {e}")
    
    return None, None, None

# === 📥 СКАЧИВАНИЕ INSTAGRAM - МЕТОД 3 (API) ===
async def download_instagram_api(url: str, shortcode: str) -> Tuple[Optional[str], Optional[List[str]], Optional[str]]:
    """Скачать контент из Instagram через публичный API"""
    try:
        logger.info("🔄 Instagram: попытка через API...")
        api_url = f"https://www.instagram.com/p/{shortcode}/?__a=1&__d=dis"
        
        async with aiohttp.ClientSession() as session:
            headers = {
                'User-Agent': 'Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36'
            }
            async with session.get(api_url, headers=headers) as resp:
                if resp.status == 200:
                    data = await resp.json()
                    media = data.get('graphql', {}).get('shortcode_media', {})
                    
                    if media.get('is_video'):
                        video_url = media.get('video_url')
                        if video_url:
                            temp_path = os.path.join(tempfile.gettempdir(), f"insta_api_{shortcode}.mp4")
                            if await download_file(video_url, temp_path):
                                logger.info("✅ Instagram: видео скачано через API")
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
                            logger.info("✅ Instagram: фото скачано через API")
                            return (None, photos, description)
    
    except Exception as e:
        logger.error(f"❌ Instagram API: {e}")
    
    return None, None, None

# === 📥 ГЛАВНАЯ ФУНКЦИЯ INSTAGRAM ===
async def download_instagram(url: str, quality: str = "best") -> Tuple[Optional[str], Optional[List[str]], Optional[str]]:
    """Скачать контент из Instagram (пробует все методы)"""
    cache_key = get_cache_key(url)
    cached_result = load_from_cache(cache_key)
    if cached_result:
        logger.info("✅ Instagram: загружено из кэша")
        return cached_result

    shortcode_match = re.search(r'/(?:p|reel)/([^/]+)', url)
    if not shortcode_match:
        return None, None, "❌ Не удалось извлечь shortcode из URL"
    
    shortcode = shortcode_match.group(1)

    methods = [
        lambda: download_instagram_instaloader(url, shortcode),
        lambda: download_instagram_ytdlp(url, quality),
        lambda: download_instagram_api(url, shortcode)
    ]

    for method in methods:
        result = await method()
        if result and (result[0] or result[1]):
            save_to_cache(cache_key, result)
            return result

    return None, None, "❌ Не удалось скачать контент из Instagram всеми методами"

# === 📤 СКАЧИВАНИЕ TIKTOK ФОТО ===
async def download_tiktok_photos(url: str) -> Tuple[Optional[List[str]], str]:
    """Скачать фото из TikTok через HTML парсинг"""
    cache_key = get_cache_key(url)
    cached_result = load_from_cache(cache_key)
    if cached_result:
        logger.info("✅ TikTok: загружено из кэша")
        return cached_result

    try:
        logger.info("🔄 TikTok: парсинг фото...")
        clean_url = url.split('?')[0]
        
        headers = {
            'User-Agent': 'Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36',
            'Accept': 'text/html,application/xhtml+xml,application/xml;q=0.9,image/webp,*/*;q=0.8',
            'Accept-Language': 'en-US,en;q=0.5',
            'Referer': 'https://www.tiktok.com/',
            'DNT': '1'
        }
        
        async with aiohttp.ClientSession(timeout=aiohttp.ClientTimeout(total=30)) as session:
            async with session.get(clean_url, headers=headers, allow_redirects=True) as resp:
                if resp.status != 200:
                    return None, f"❌ TikTok вернул статус {resp.status}"
                
                html = await resp.text()
                photos = []
                urls_found = set()
                
                img_patterns = [
                    r'<img[^>]*?src="([^"]*imagePost[^"]*?)"',
                    r'srcSet="([^"]*imagePost[^"]*?)"',
                    r'"imagePost":"([^"]*)"',
                    r'"images":\[([^\]]*)\]',
                    r'<img[^>]*?src="(https://[^"]*tiktok[^"]*\.jpg)"',
                    r'<img[^>]*?data-src="(https://[^"]*\.jpg)"'
                ]
                
                for pattern in img_patterns:
                    matches = re.finditer(pattern, html, re.DOTALL)
                    for match in matches:
                        raw_url = match.group(1)
                        if '[' in raw_url or '{' in raw_url:
                            json_matches = re.findall(r'"([https://][^"]*\.jpg)"', raw_url)
                            for url_str in json_matches:
                                urls_found.add(url_str)
                        else:
                            url_str = raw_url.replace(r'\/', '/').split('?')[0]
                            if url_str.startswith('http') and '.jpg' in url_str.lower():
                                urls_found.add(url_str)
                
                logger.info(f"TikTok: найдено {len(urls_found)} URL изображений")
                
                for i, img_url in enumerate(list(urls_found)[:10]):
                    try:
                        img_path = os.path.join(tempfile.gettempdir(), f"tiktok_photo_{i}.jpg")
                        if await download_file(img_url, img_path, timeout=15):
                            photos.append(img_path)
                            logger.info(f"✅ Фото {i+1} скачано")
                    except Exception as e:
                        logger.warning(f"Ошибка скачивания фото {i+1}: {e}")
                        continue
                
                if photos:
                    logger.info(f"✅ TikTok: скачано {len(photos)} фото")
                    result = (photos, "📸 Фото из TikTok")
                    save_to_cache(cache_key, result)
                    return result
                else:
                    return None, "❌ Не удалось найти фото на странице"
    
    except asyncio.TimeoutError:
        return None, "❌ Истёк timeout при подключении к TikTok"
    except Exception as e:
        logger.error(f"❌ TikTok парсинг: {e}")
        return None, f"❌ Ошибка: {str(e)[:50]}"

# === 📤 СКАЧИВАНИЕ ВИДЕО - МЕТОДЫ ===
async def download_video_ytdlp(url: str, quality: str) -> Optional[str]:
    try:
        logger.info("🔄 Видео: попытка через yt-dlp...")
        ydl_opts = get_ydl_opts(quality)
        with yt_dlp.YoutubeDL(ydl_opts) as ydl:
            info = ydl.extract_info(url, download=True)
            temp_file = ydl.prepare_filename(info)
            if temp_file and os.path.exists(temp_file):
                logger.info("✅ Видео: скачано через yt-dlp")
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
                logger.info("✅ Видео: скачано через yt-dlp с cookies")
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
                logger.info("✅ Видео: скачано через yt-dlp (альтернативный)")
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

# === 📤 ОТПРАВКА ФОТО ===
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
                async with session.post('https://filebin.net/', data=data, params={'expiry': '3d'}) as resp:  # <-- пробел убран
                    if resp.status == 200:
                        text = await resp.text()
                        match = re.search(r'https://filebin\.net/[a-zA-Z0-9]+/[^"\s<>\)]+', text)
                        if match:
                            logger.info("✅ Загружено на filebin.net")
                            return match.group(0)
    except Exception as e:
        logger.error(f"❌ filebin.net: {e}")
    return None

async def upload_to_gofile(file_path: str) -> Optional[str]:
    """Загрузить файл на gofile.io (до 10 ГБ)"""
    try:
        logger.info("🔄 Загрузка на gofile.io...")
        async with aiohttp.ClientSession(timeout=aiohttp.ClientTimeout(total=300)) as session:  # увеличено до 5 мин
            # Получаем сервер
            async with session.get('https://api.gofile.io/servers') as resp:
                if resp.status != 200:
                    logger.error(f"gofile.io: не удалось получить сервер, статус {resp.status}")
                    return None
                server_data = await resp.json()
                if not server_data.get('data', {}).get('servers'):
                    logger.error("gofile.io: нет доступных серверов")
                    return None
                # Берём первый сервер
                server = server_data['data']['servers'][0]['name']
                logger.info(f"Выбран сервер gofile: {server}")

            # Загружаем файл
            with open(file_path, 'rb') as f:
                data = aiohttp.FormData()
                data.add_field('file', f, filename=Path(file_path).name)
                upload_url = f'https://{server}.gofile.io/contents/uploadfile'
                async with session.post(upload_url, data=data) as upload_resp:
                    logger.info(f"gofile.io: статус загрузки {upload_resp.status}")
                    if upload_resp.status == 200:
                        result = await upload_resp.json()
                        if result.get('status') == 'ok':
                            download_page = result['data']['downloadPage']
                            logger.info(f"✅ Загружено на gofile.io: {download_page}")
                            return download_page
                        else:
                            logger.error(f"gofile.io: ошибка в ответе: {result}")
                    else:
                        text = await upload_resp.text()
                        logger.error(f"gofile.io: HTTP {upload_resp.status}, тело: {text[:200]}")
    except asyncio.TimeoutError:
        logger.error("❌ gofile.io: таймаут при загрузке (файл слишком большой или медленный интернет)")
    except Exception as e:
        logger.error(f"❌ gofile.io: {e}")
    return None

async def upload_to_oshi(file_path: str) -> Optional[str]:
    """Загрузить файл на oshi.at (до 5 ГБ, 30 дней)"""
    try:
        logger.info("🔄 Загрузка на oshi.at...")
        # Отключаем SSL-проверку ТОЛЬКО для этого хоста (из-за их самоподписанного сертификата)
        connector = aiohttp.TCPConnector(ssl=False)
        async with aiohttp.ClientSession(
            connector=connector,
            timeout=aiohttp.ClientTimeout(total=180)
        ) as session:
            with open(file_path, 'rb') as f:
                data = aiohttp.FormData()
                data.add_field('file', f, filename=Path(file_path).name)
                async with session.post('https://oshi.at/', data=data) as resp:
                    if resp.status == 200:
                        text = await resp.text()
                        lines = text.strip().split('\n')
                        if lines:
                            url = lines[0].strip()
                            if url.startswith('http'):
                                logger.info("✅ Загружено на oshi.at")
                                return url
    except Exception as e:
        logger.error(f"❌ oshi.at: {e}")
    return None

async def send_video_or_link(chat_id: int, file_path: str, caption: str = "") -> bool:
    file_size = Path(file_path).stat().st_size
    size_mb = file_size / (1024 * 1024)

    if size_mb <= 50:
        try:
            await bot.send_video(chat_id=chat_id, video=FSInputFile(file_path), caption=caption)
            return True
        except TelegramBadRequest as e:
            logger.error(f"Ошибка отправки видео в Telegram: {e}")

    # Только рабочие файлообменники
    uploaders = [
        ('filebin.net', upload_to_filebin),
        ('gofile.io', upload_to_gofile),
        ('oshi.at', upload_to_oshi)
    ]

    for name, uploader in uploaders:
        logger.info(f"Пробуем загрузить на {name}...")
        link = await uploader(file_path)
        if link:
            await bot.send_message(
                chat_id=chat_id,
                text=f"📦 Файл ({size_mb:.1f} МБ) загружен на {name}\n\n"
                     f"📥 Скачать: {link}\n\n"
                     f"⏱️ Ссылка действительна несколько дней"
            )
            return True

    await bot.send_message(
        chat_id=chat_id,
        text=f"❌ Файл слишком большой ({size_mb:.1f} МБ).\n"
             f"Все сервисы загрузки временно недоступны."
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
        await message.answer(
            "⚠️ Отправьте корректную ссылку на:\n"
            "• YouTube\n• TikTok\n• Instagram"
        )
        return

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
                await send_photos_with_caption(message.chat.id, photos, description)
                return

        if platform == 'tiktok' and '/photo/' in url:
            photos, description = await download_tiktok_photos(url)
            if photos:
                temp_photos = photos
                await status_msg.delete()
                await send_photos_with_caption(message.chat.id, photos, description)
                return
            else:
                await status_msg.edit_text(description)
                return

        temp_file = await download_video(url, user_quality)
        if not temp_file or not os.path.exists(temp_file):
            await status_msg.edit_text("❌ Не удалось скачать видео всеми доступными методами")
            return

        await status_msg.edit_text("📤 Отправляю...")
        await send_video_or_link(message.chat.id, temp_file, caption="🎥 Готово!")
        await status_msg.delete()

    except Exception as e:
        error_msg = f"❌ Ошибка: {str(e)}"
        logger.error(error_msg)
        try:
            await status_msg.edit_text(error_msg)
        except:
            pass
    
    finally:
        if temp_file and os.path.exists(temp_file):
            try:
                os.remove(temp_file)
                logger.info(f"Удалён временный файл: {temp_file}")
            except Exception as e:
                logger.warning(f"Не удалось удалить файл {temp_file}: {e}")
        
        for photo in temp_photos:
            try:
                if os.path.exists(photo):
                    os.remove(photo)
            except Exception as e:
                logger.warning(f"Не удалось удалить фото {photo}: {e}")

# === 🏁 ЗАПУСК ===
async def main():
    logger.info("🚀 Запуск бота...")
    try:
        await dp.start_polling(bot, skip_updates=True)
    finally:
        await bot.session.close()

if __name__ == "__main__":
    try:
        asyncio.run(main())
    except KeyboardInterrupt:
        logger.info("🛑 Бот остановлен")
    except Exception as e:
        logger.error(f"❌ Критическая ошибка: {e}")
