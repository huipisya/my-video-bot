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
        }
    }

def is_valid_url(url):
    regex = re.compile(
        r'^(https?://)?(www\.)?'
        r'(youtube\.com|youtu\.be|tiktok\.com|instagram\.com|vm\.tiktok\.com|vt\.tiktok\.com)/',
        re.IGNORECASE
    )
    return re.match(regex, url) is not None

# === 📥 СКАЧИВАНИЕ ===
async def download_instagram(url):
    try:
        L = instaloader.Instaloader(
            download_videos=True,
            download_geotags=False,
            download_comments=False,
            save_metadata=False,
            quiet=True
        )
        shortcode = re.search(r'/p/([^/]+)|/reel/([^/]+)', url)
        if not shortcode:
            return None, "❌ Не удалось извлечь код поста из ссылки."

        shortcode = shortcode.group(1) or shortcode.group(2)
        post = instaloader.Post.from_shortcode(L.context, shortcode)

        if not post.is_video:
            return None, "📸 Это не видео, а фото."

        video_url = post.video_url
        temp_path = os.path.join(tempfile.gettempdir(), f"insta_{shortcode}.mp4")

        async with aiohttp.ClientSession() as session:
            async with session.get(video_url) as resp:
                if resp.status == 200:
                    with open(temp_path, 'wb') as f:
                        async for chunk in resp.content.iter_chunked(8192):
                            f.write(chunk)
                    return temp_path, None
                else:
                    return None, f"❌ Ошибка загрузки: {resp.status}"
    except Exception as e:
        return None, str(e)

# === 📤 ОТПРАВКА ФАЙЛОВ ===
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
        await bot.send_message(chat_id=chat_id, text=f"📦 Файл слишком большой ({size_mb:.1f} МБ). Реализуйте загрузку на облако.")
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

    status_msg = await message.answer("⏳ Обрабатываю ссылку...")
    user_quality = get_quality_setting(message.from_user.id)
    temp_file = None

    try:
        # Instagram
        if 'instagram.com' in url:
            temp_file, error = await download_instagram(url)
            if error:
                await status_msg.edit_text(error)
                return

        # YouTube / TikTok
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

    except yt_dlp.DownloadError as e:
        await status_msg.edit_text(f"❌ Ошибка скачивания: {str(e)}")
    except Exception as e:
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