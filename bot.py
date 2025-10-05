import os
import tempfile
import asyncio
import requests
from aiogram import Bot, Dispatcher, types, F
from aiogram.filters import Command, StateFilter
from aiogram.fsm.state import State, StatesGroup
from aiogram.fsm.context import FSMContext
from aiogram.types import ReplyKeyboardMarkup, KeyboardButton
import yt_dlp

# 🔑 Токен из переменной окружения
BOT_TOKEN = os.getenv("BOT_TOKEN")

if not BOT_TOKEN:
    raise ValueError("❌ BOT_TOKEN не установлен! Добавь его в Railway Variables.")

bot = Bot(token=BOT_TOKEN)
dp = Dispatcher()

# Хранилище настроек пользователей
user_settings = {}

class VideoStates(StatesGroup):
    choosing_quality = State()

def get_quality_setting(user_id):
    return user_settings.get(user_id, "1080p")

def get_ydl_opts(quality="1080p"):
    base_opts = {
        'noplaylist': True,
        'outtmpl': os.path.join(tempfile.gettempdir(), '%(title).50s.%(ext)s'),
        'quiet': True,
        'no_warnings': True,
        'retries': 5,
        'fragment_retries': 5,
        'extractor_retries': 5,
    }
    if quality == "480p":
        base_opts['format'] = 'best[height<=480][ext=mp4]/best[ext=mp4]/best'
    else:  # 1080p
        base_opts['format'] = 'bestvideo[ext=mp4][height<=1080]+bestaudio[ext=m4a]/best[ext=mp4]/best'
        base_opts['merge_output_format'] = 'mp4'
    
    # Используем куки, если файл существует
    if os.path.exists("cookies.txt"):
        base_opts['cookiefile'] = "cookies.txt"
        print("✅ Использую cookies.txt для YouTube")
    
    return base_opts

def upload_to_fileio(file_path):
    """Загружает файл на file.io, ссылка живёт 3 дня"""
    try:
        with open(file_path, 'rb') as f:
            response = requests.post('https://file.io/?expires=3d', files={'file': f}, timeout=600)
        if response.status_code == 200:
            data = response.json()
            return data.get('link')
    except Exception as e:
        print(f"Ошибка загрузки: {e}")
    return None

@dp.message(Command("start"))
async def start(message: types.Message, state: FSMContext):
    await state.clear()
    kb = ReplyKeyboardMarkup(
        keyboard=[[KeyboardButton(text="⚙️ Настройки")]],
        resize_keyboard=True,
        one_time_keyboard=False
    )
    await message.answer(
        "🎥 Отправь ссылку на TikTok или YouTube Shorts!\n\n"
        "По умолчанию: 1080p.\n"
        "Нажми «⚙️ Настройки», чтобы выбрать качество.",
        reply_markup=kb
    )

@dp.message(F.text == "⚙️ Настройки")
async def settings_menu(message: types.Message, state: FSMContext):
    await state.set_state(VideoStates.choosing_quality)
    kb = ReplyKeyboardMarkup(
        keyboard=[
            [KeyboardButton(text="🎬 1080p — Качество")],
            [KeyboardButton(text="⚡ 480p — Скорость")]
        ],
        resize_keyboard=True,
        one_time_keyboard=False
    )
    await message.answer("Выбери качество видео:", reply_markup=kb)

@dp.message(VideoStates.choosing_quality, F.text.contains("1080p"))
async def set_quality_1080p(message: types.Message, state: FSMContext):
    user_settings[message.from_user.id] = "1080p"
    await state.clear()
    kb = ReplyKeyboardMarkup(
        keyboard=[[KeyboardButton(text="⚙️ Настройки")]],
        resize_keyboard=True,
        one_time_keyboard=False
    )
    await message.answer("✅ Выбрано: **1080p (качество)**", reply_markup=kb)

@dp.message(VideoStates.choosing_quality, F.text.contains("480p"))
async def set_quality_480p(message: types.Message, state: FSMContext):
    user_settings[message.from_user.id] = "480p"
    await state.clear()
    kb = ReplyKeyboardMarkup(
        keyboard=[[KeyboardButton(text="⚙️ Настройки")]],
        resize_keyboard=True,
        one_time_keyboard=False
    )
    await message.answer("✅ Выбрано: **480p (скорость)**", reply_markup=kb)

@dp.message()
async def download_video(message: types.Message):
    url = message.text.strip()
    if not url.startswith(('http://', 'https://')):
        await message.answer("Пожалуйста, отправь корректную ссылку.")
        return

    user_id = message.from_user.id
    quality = get_quality_setting(user_id)
    await message.answer(f"⏳ Скачиваю в {quality}... Это может занять 1-3 минуты.")

    temp_file = None
    try:
        ydl_opts = get_ydl_opts(quality)
        with yt_dlp.YoutubeDL(ydl_opts) as ydl:
            info = ydl.extract_info(url, download=True)
            temp_file = ydl.prepare_filename(info)

        if not os.path.exists(temp_file):
            raise Exception("Файл не был сохранён")

        file_size = os.path.getsize(temp_file)
        file_size_mb = file_size / (1024 * 1024)

        if file_size <= 50 * 1024 * 1024:
            await bot.send_video(
                chat_id=message.chat.id,
                video=types.FSInputFile(temp_file),
                caption=f"✅ Вот твоё видео ({quality})!"
            )
        else:
            await message.answer(
                f"⚠️ Видео слишком большое ({file_size_mb:.1f} МБ), "
                "но ты можешь скачать его по ссылке!"
            )
            await message.answer("📤 Загружаю на облако...")

            download_link = await asyncio.get_event_loop().run_in_executor(None, upload_to_fileio, temp_file)

            if download_link:
                await message.answer(
                    f"🔥 Готово! Скачай по ссылке:\n\n{download_link}\n\n"
                    "📎 Ссылка работает 3 дня."
                )
            else:
                await message.answer("❌ Не удалось загрузить на облако.")

    except Exception as e:
        error_msg = str(e)
        if "private" in error_msg.lower() or "login" in error_msg.lower():
            await message.answer("❌ Это приватное видео.")
        elif "404" in error_msg or "not found" in error_msg.lower():
            await message.answer("❌ Видео не найдено.")
        elif "Sign in to confirm" in error_msg:
            await message.answer("❌ YouTube временно заблокировал загрузку. Попробуй позже или другое видео.")
        elif "ffmpeg" in error_msg.lower():
            await message.answer("❌ Проблема с обработкой видео. Попробуй 480p качество.")
        else:
            await message.answer(f"❌ Ошибка: {error_msg[:150]}")
    finally:
        if temp_file and os.path.exists(temp_file):
            try:
                os.remove(temp_file)
            except Exception as e:
                print(f"Не удалось удалить файл: {e}")

async def main():
    print("✅ Бот запущен и готов к работе!")
    await dp.start_polling(bot)

if __name__ == "__main__":
    asyncio.run(main())