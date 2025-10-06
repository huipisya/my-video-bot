import os
import tempfile
import asyncio
import logging
import re
from pathlib import Path
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

# === 🧰 НАСТРОЙКА ЛОГИРОВАНИЯ ===
logging.basicConfig(level=logging.INFO, format='%(asctime)s - %(levelname)s - %(message)s')
logger = logging.getLogger(__name__)

# === 🔐 ЗАГРУЗКА ТОКЕНА ===
load_dotenv()
BOT_TOKEN = os.getenv("BOT_TOKEN")
if not BOT_TOKEN:
    raise ValueError("❌ Токен бота не найден! Создайте файл .env и добавьте BOT_TOKEN=ваш_токен")

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

# === 🛠 ВСПОМОГАТЕЛЬНЫЕ ФУНКЦИИ ===
def get_quality_setting(user_id):
    return user_settings.get(user_id, "best")

def get_ydl_opts(quality="best"):
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

def is_valid_url(url):
    regex = re.compile(
        r'^(https?://)?(www\.)?'
        r'(youtube\.com|youtu\.be|tiktok\.com|instagram\.com|vm\.tiktok\.com|vt\.tiktok\.com)/',
        re.IGNORECASE
    )
    return re.match(regex, url) is not None

def detect_platform(url):
    if 'youtube.com' in url or 'youtu.be' in url:
        return 'youtube'
    elif 'tiktok.com' in url or 'vm.tiktok.com' in url or 'vt.tiktok.com' in url:
        return 'tiktok'
    elif 'instagram.com' in url:
        return 'instagram'
    return 'unknown'

# === 🧺 КЭШ (в папке cache) ===
CACHE_DIR = Path("cache")
CACHE_DIR.mkdir(exist_ok=True)

def save_to_cache(key, data):
    cache_file = CACHE_DIR / f"{key}.pkl"
    with open(cache_file, 'wb') as f:
        pickle.dump(data, f)

def load_from_cache(key):
    cache_file = CACHE_DIR / f"{key}.pkl"
    if cache_file.exists():
        with open(cache_file, 'rb') as f:
            return pickle.load(f)
    return None

def get_cache_key(url):
    # Создаём уникальный ключ для кэша
    import hashlib
    return hashlib.md5(url.encode()).hexdigest()

# === 📥 СКАЧИВАНИЕ ===
async def download_instagram(url):
    cache_key = get_cache_key(url)
    cached_result = load_from_cache(cache_key)
    if cached_result:
        logger.info("✅ Instagram: загружено из кэша")
        return cached_result

    # Попытка 1: instaloader
    try:
        L = instaloader.Instaloader(
            download_videos=True,
            download_pictures=True,
            download_geotags=False,
            download_comments=False,
            save_metadata=False,
            quiet=True
        )
        shortcode = re.search(r'/p/([^/]+)|/reel/([^/]+)', url)
        if not shortcode:
            pass
        else:
            shortcode = shortcode.group(1) or shortcode.group(2)
            post = instaloader.Post.from_shortcode(L.context, shortcode)

            if post.is_video:
                # Это видео
                video_url = post.video_url
                temp_path = os.path.join(tempfile.gettempdir(), f"insta_{shortcode}.mp4")

                async with aiohttp.ClientSession(timeout=aiohttp.ClientTimeout(total=60)) as session:
                    async with session.get(video_url) as resp:
                        if resp.status == 200:
                            with open(temp_path, 'wb') as f:
                                async for chunk in resp.content.iter_chunked(8192):
                                    f.write(chunk)
                            result = temp_path, None, None  # файл, фото, описание
                            save_to_cache(cache_key, result)
                            return result
                        else:
                            pass
            else:
                # Это фото/фото-галерея
                photos = []
                if post.typename == "GraphSidecar":
                    # Это галерея
                    for i, node in enumerate(post.get_sidecar_nodes()):
                        if node.is_video:
                            continue
                        if i >= 10:  # Максимум 10 фото
                            break
                        else:
                            photo_url = node.display_url
                            photo_path = os.path.join(tempfile.gettempdir(), f"insta_{shortcode}_{i}.jpg")
                            async with aiohttp.ClientSession(timeout=aiohttp.ClientTimeout(total=60)) as session:
                                async with session.get(photo_url) as resp:
                                    if resp.status == 200:
                                        with open(photo_path, 'wb') as f:
                                            async for chunk in resp.content.iter_chunked(8192):
                                                f.write(chunk)
                                        photos.append(photo_path)
                else:
                    # Это одиночное фото
                    photo_url = post.url
                    photo_path = os.path.join(tempfile.gettempdir(), f"insta_{shortcode}.jpg")
                    async with aiohttp.ClientSession(timeout=aiohttp.ClientTimeout(total=60)) as session:
                        async with session.get(photo_url) as resp:
                            if resp.status == 200:
                                with open(photo_path, 'wb') as f:
                                    async for chunk in resp.content.iter_chunked(8192):
                                        f.write(chunk)
                                photos.append(photo_path)

                description = post.caption if post.caption else "Без описания"
                result = None, photos, description
                save_to_cache(cache_key, result)
                return result

    except Exception as e:
        logger.error(f"Ошибка при скачивании Instagram (способ 1): {e}")

    # Попытка 2: yt-dlp
    try:
        ydl_opts = get_ydl_opts(get_quality_setting(0))
        with yt_dlp.YoutubeDL(ydl_opts) as ydl:
            info = ydl.extract_info(url, download=True)
            temp_file = ydl.prepare_filename(info)
        result = temp_file, None, None
        save_to_cache(cache_key, result)
        return result
    except Exception as e:
        logger.error(f"Ошибка при скачивании Instagram (способ 2): {e}")

    return None, None, "❌ Не удалось скачать пост с Instagram."

# === 📤 СКАЧИВАНИЕ TIKTOK ФОТО ===
async def download_tiktok_photos(url):
    cache_key = get_cache_key(url)
    cached_result = load_from_cache(cache_key)
    if cached_result:
        logger.info("✅ TikTok: загружено из кэша")
        return cached_result

    # Попытка 1: yt-dlp
    try:
        ydl_opts = get_ydl_opts(get_quality_setting(0))
        with yt_dlp.YoutubeDL(ydl_opts) as ydl:
            info = ydl.extract_info(url, download=False)

            if info.get('_type') == 'playlist' or 'entries' in info:
                # Это TikTok галерея (фото)
                photos = []
                for i, entry in enumerate(info['entries']):
                    if i >= 30:  # Максимум 30 фото
                        break
                    img_url = entry.get('thumbnail')
                    if img_url:
                        img_path = os.path.join(tempfile.gettempdir(), f"tiktok_{entry.get('id', 'unknown')}_{i}.jpg")
                        async with aiohttp.ClientSession() as session:
                            async with session.get(img_url) as img_resp:
                                if img_resp.status == 200:
                                    with open(img_path, 'wb') as f:
                                        async for chunk in img_resp.content.iter_chunked(8192):
                                            f.write(chunk)
                                    photos.append(img_path)

                description = info.get('description', 'Без описания')
                result = photos, description
                save_to_cache(cache_key, result)
                return result
            else:
                # Это видео
                return None, "❌ Это TikTok видео, а не фото."

    except Exception as e:
        logger.error(f"Ошибка при скачивании TikTok (способ 1): {e}")

    # Попытка 2: через oembed API
    try:
        api_url = f"https://www.tiktok.com/oembed?url={url}"
        async with aiohttp.ClientSession() as session:
            async with session.get(api_url) as resp:
                if resp.status == 200:
                    data = await resp.json()
                    title = data.get('title', 'Без описания')
                    author = data.get('author_name', 'Неизвестный автор')

                    # Пытаемся получить изображения
                    photos = []
                    # TikTok API может возвращать изображения в формате "slide"
                    # Пока используем yt-dlp для получения информации
                    ydl_opts = get_ydl_opts(get_quality_setting(0))
                    with yt_dlp.YoutubeDL(ydl_opts) as ydl:
                        info = ydl.extract_info(url, download=False)

                        if info.get('_type') == 'playlist' or 'entries' in info:
                            for i, entry in enumerate(info['entries']):
                                if i >= 30:  # Максимум 30 фото
                                    break
                                img_url = entry.get('thumbnail')
                                if img_url:
                                    img_path = os.path.join(tempfile.gettempdir(), f"tiktok_{info.get('id', 'unknown')}_{i}.jpg")
                                    async with session.get(img_url) as img_resp:
                                        if img_resp.status == 200:
                                            with open(img_path, 'wb') as f:
                                                async for chunk in img_resp.content.iter_chunked(8192):
                                                    f.write(chunk)
                                            photos.append(img_path)

                    description = f"{title} (@{author})"
                    result = photos, description
                    save_to_cache(cache_key, result)
                    return result

    except Exception as e:
        logger.error(f"Ошибка при скачивании TikTok (способ 2): {e}")

    # Попытка 3: через Selenium (обход защиты)
    try:
        from selenium import webdriver
        from selenium.webdriver.chrome.options import Options
        import undetected_chromedriver as uc

        options = Options()
        options.add_argument('--headless')
        options.add_argument('--no-sandbox')
        options.add_argument('--disable-dev-shm-usage')
        options.add_argument('--disable-blink-features=AutomationControlled')
        options.add_experimental_option("excludeSwitches", ["enable-automation"])
        options.add_experimental_option('useAutomationExtension', False)

        driver = uc.Chrome(options=options)
        driver.execute_script("Object.defineProperty(navigator, 'webdriver', {get: () => undefined})")
        driver.get(url)
        await asyncio.sleep(5)  # Ждём загрузки

        # Ищем изображения на странице
        img_elements = driver.find_elements("tag name", "img")
        photos = []
        for i, img in enumerate(img_elements):
            if i >= 30:  # Максимум 30 фото
                break
            img_url = img.get_attribute('src')
            if img_url and 'tiktok' in img_url:
                img_path = os.path.join(tempfile.gettempdir(), f"tiktok_selenium_{i}.jpg")
                async with aiohttp.ClientSession() as session:
                    async with session.get(img_url) as img_resp:
                        if img_resp.status == 200:
                            with open(img_path, 'wb') as f:
                                async for chunk in img_resp.content.iter_chunked(8192):
                                    f.write(chunk)
                            photos.append(img_path)

        driver.quit()

        description = "Описание не найдено (получено через Selenium)"
        result = photos, description
        save_to_cache(cache_key, result)
        return result

    except Exception as e:
        logger.error(f"Ошибка при скачивании TikTok (способ 3 - Selenium): {e}")

    result = None, "❌ Не удалось скачать фото из TikTok."
    save_to_cache(cache_key, result)
    return result

# === 📤 ОТПРАВКА ФОТО И ОПИСАНИЯ (В ОДНОМ СООБЩЕНИИ) ===
async def send_photos_and_caption(chat_id, photos, caption):
    if not photos:
        return False

    if len(photos) == 1:
        # Одно фото
        await bot.send_photo(chat_id=chat_id, photo=FSInputFile(photos[0]), caption=caption)
    else:
        # Несколько фото (ограничено до 10 для Instagram, 30 для TikTok)
        media_group = []
        for i, photo_path in enumerate(photos):
            if i == 0:
                media_group.append(types.InputMediaPhoto(media=FSInputFile(photo_path), caption=caption))
            else:
                media_group.append(types.InputMediaPhoto(media=FSInputFile(photo_path)))
        await bot.send_media_group(chat_id=chat_id, media=media_group)

    return True

# === 📤 ОТПРАВКА ФАЙЛОВ ===
async def upload_to_filebin_net(file_path):
    try:
        timeout = aiohttp.ClientTimeout(total=60)
        async with aiohttp.ClientSession(timeout=timeout) as session:
            with open(file_path, 'rb') as f:
                data = aiohttp.FormData()
                data.add_field('file', f, filename=Path(file_path).name)
                resp = await session.post('https://filebin.net/', data=data, params={'expiry': '3d'})
                if resp.status == 200:
                    result = await resp.text()
                    import re
                    match = re.search(r'https://filebin\.net/[^"\s<>\)]+', result)
                    if match:
                        return match.group(0)
    except Exception as e:
        logger.error(f"Ошибка загрузки на filebin.net: {e}")
    return None

async def upload_to_gofile_io(file_path):
    try:
        timeout = aiohttp.ClientTimeout(total=60)
        async with aiohttp.ClientSession(timeout=timeout) as session:
            with open(file_path, 'rb') as f:
                data = aiohttp.FormData()
                data.add_field('file', f, filename=Path(file_path).name)
                resp = await session.post('https://store2.gofile.io/UploadServer', data=data)
                if resp.status == 200:
                    result = await resp.json()
                    url = result.get('data', {}).get('downloadPage', '')
                    if url:
                        return url.replace('?c=', '/?c=')
    except Exception as e:
        logger.error(f"Ошибка загрузки на gofile.io: {e}")
    return None

async def upload_to_tmpfiles_org(file_path):
    try:
        timeout = aiohttp.ClientTimeout(total=60)
        async with aiohttp.ClientSession(timeout=timeout) as session:
            with open(file_path, 'rb') as f:
                data = aiohttp.FormData()
                data.add_field('file', f, filename=Path(file_path).name)
                resp = await session.post('https://tmpfiles.org/api/v1/upload', data=data)
                if resp.status == 200:
                    result = await resp.json()
                    url = result.get('data', {}).get('url', '')
                    if url:
                        direct_url = url.replace('tmpfiles.org/', 'tmpfiles.org/dl/')
                        return direct_url
    except Exception as e:
        logger.error(f"Ошибка загрузки на tmpfiles.org: {e}")
    return None

async def send_video_or_link(chat_id, file_path, caption=""):
    file_size = Path(file_path).stat().st_size
    size_mb = file_size / (1024 * 1024)

    if size_mb <= 50:
        try:
            await bot.send_video(chat_id=chat_id, video=FSInputFile(file_path), caption=caption)
            return True
        except TelegramBadRequest as e:
            logger.error(f"Telegram error: {e}")
            return False
    else:
        # Пробуем filebin.net
        download_link = await upload_to_filebin_net(file_path)
        if download_link:
            await bot.send_message(
                chat_id=chat_id,
                text=f"📦 Файл слишком большой ({size_mb:.1f} МБ)\n\n"
                     f"📥 Скачать по ссылке: {download_link}\n\n"
                     f"⏱️ Ссылка доступна 3 дня."
            )
            return True

        # Если filebin.net не сработал — пробуем gofile.io
        download_link = await upload_to_gofile_io(file_path)
        if download_link:
            await bot.send_message(
                chat_id=chat_id,
                text=f"📦 Файл слишком большой ({size_mb:.1f} МБ)\n\n"
                     f"📥 Скачать по ссылке: {download_link}\n\n"
                     f"⏱️ Ссылка доступна 3 дня."
            )
            return True

        # Если gofile.io не сработал — пробуем tmpfiles.org
        download_link = await upload_to_tmpfiles_org(file_path)
        if download_link:
            await bot.send_message(
                chat_id=chat_id,
                text=f"📦 Файл слишком большой ({size_mb:.1f} МБ)\n\n"
                     f"📥 Скачать по ссылке: {download_link}\n\n"
                     f"⏱️ Ссылка доступна 3 дня."
            )
            return True

        # Если все не сработали — сообщаем пользователю
        await bot.send_message(
            chat_id=chat_id,
            text=f"❌ Файл слишком большой ({size_mb:.1f} МБ). Временно наши серверы не работают."
        )
        return False

# === 🧭 КЛАВИАТУРЫ ===
def main_keyboard():
    return ReplyKeyboardMarkup(
        keyboard=[[KeyboardButton(text="⚙️ Настройки")]],
        resize_keyboard=True
    )

def settings_keyboard():
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
async def start(message: types.Message, state: FSMContext):
    await state.clear()
    welcome_text = (
        "🎬 <b>Добро пожаловать в VideoBot!</b>\n\n"
        "Я могу скачать видео с:\n"
        "• YouTube\n"
        "• TikTok\n"
        "• Instagram\n\n"
        " просто пришли мне ссылку и я всё сделаю за тебя!"
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

@dp.message(VideoStates.choosing_quality, F.text.in_(["🌟 Лучшее", "🎬 1080p", "📺 720p", "⚡ 480p", "📱 360p"]))
async def set_quality(message: types.Message, state: FSMContext):
    quality_map = {
        "🌟 Лучшее": "best",
        "🎬 1080p": "1080p",
        "📺 720p": "720p",
        "⚡ 480p": "480p",
        "📱 360p": "360p"
    }
    user_settings[message.from_user.id] = quality_map[message.text]
    await message.answer(f"✅ Установлено качество: <b>{message.text}</b>", reply_markup=main_keyboard(), parse_mode="HTML")
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
        await message.answer("⚠️ Пожалуйста, отправьте корректную ссылку на YouTube, TikTok или Instagram.")
        return

    platform = detect_platform(url)
    status_msg = await message.answer(f"⏳ Обрабатываю {platform.upper()}...")
    user_quality = get_quality_setting(message.from_user.id)
    temp_file = None

    try:
        # Instagram
        if platform == 'instagram':
            temp_file, photos, description = await download_instagram(url)
            if description and "❌" in description:
                await status_msg.edit_text(description)
                return

            if photos:
                # Это фото/фото-галерея
                await status_msg.delete()
                await send_photos_and_caption(message.chat.id, photos, description)
                return

        # TikTok
        elif platform == 'tiktok':
            if '/photo/' in url:
                # Это TikTok фото
                photos, description = await download_tiktok_photos(url)
                if photos:
                    await status_msg.delete()
                    await send_photos_and_caption(message.chat.id, photos, description)
                    return
                else:
                    await status_msg.edit_text(description)
                    return
            else:
                # Это TikTok видео
                ydl_opts = get_ydl_opts(user_quality)
                with yt_dlp.YoutubeDL(ydl_opts) as ydl:
                    info = ydl.extract_info(url, download=True)
                    temp_file = ydl.prepare_filename(info)

        # YouTube
        else:
            ydl_opts = get_ydl_opts(user_quality)
            with yt_dlp.YoutubeDL(ydl_opts) as ydl:
                info = ydl.extract_info(url, download=True)
                temp_file = ydl.prepare_filename(info)

        if not temp_file or not os.path.exists(temp_file):
            await status_msg.edit_text("❌ Не удалось сохранить видео.")
            return

        await status_msg.edit_text("📤 Отправляю видео...")
        await send_video_or_link(message.chat.id, temp_file, caption="🎥 Вот ваше видео!")

    except Exception as e:
        if "Unsupported URL" in str(e):
            # Это TikTok фото, пробуем Selenium
            if platform == 'tiktok' and '/photo/' in url:
                logger.info("🔄 yt-dlp не поддерживает URL, пробуем Selenium...")
                photos, description = await download_tiktok_photos(url)
                if photos:
                    await status_msg.delete()
                    await send_photos_and_caption(message.chat.id, photos, description)
                    return
                else:
                    await status_msg.edit_text(description)
                    return
        await status_msg.edit_text(f"❌ Неизвестная ошибка: {str(e)}")
    finally:
        if temp_file and os.path.exists(temp_file):
            try:
                os.remove(temp_file)
            except Exception as e:
                logger.warning(f"Не удалось удалить временный файл: {e}")

# === 🏁 ЗАПУСК ===
async def main():
    logger.info("🚀 Бот запускается...")
    await dp.start_polling(bot)

if __name__ == "__main__":
    try:
        asyncio.run(main())
    except KeyboardInterrupt:
        logger.info("🛑 Бот остановлен пользователем")