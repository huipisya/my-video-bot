import os
import tempfile
import asyncio
import requests
from aiogram import Bot, Dispatcher, types, F
from aiogram.filters import Command
from aiogram.fsm.state import State, StatesGroup
from aiogram.fsm.context import FSMContext
from aiogram.types import ReplyKeyboardMarkup, KeyboardButton
from dotenv import load_dotenv
import yt_dlp
import instaloader

# Загружаем переменные из .env файла
load_dotenv()

# 🔑 Токен бота из переменной окружения
BOT_TOKEN = os.getenv("BOT_TOKEN")

if not BOT_TOKEN:
    raise ValueError("❌ Токен бота не найден! Создайте файл .env и добавьте BOT_TOKEN=ваш_токен")

bot = Bot(token=BOT_TOKEN)
dp = Dispatcher()

# Хранилище настроек пользователей
user_settings = {}

class VideoStates(StatesGroup):
    choosing_quality = State()

def get_quality_setting(user_id):
    """Получить настройку качества пользователя"""
    return user_settings.get(user_id, "best")

def get_ydl_opts(quality="best"):
    """Настройки для yt-dlp в зависимости от качества"""
    quality_formats = {
        "best": 'bestvideo[ext=mp4]+bestaudio[ext=m4a]/best[ext=mp4]/best',
        "1080p": 'bestvideo[ext=mp4][height<=1080]+bestaudio[ext=m4a]/best[height<=1080][ext=mp4]/best',
        "720p": 'bestvideo[ext=mp4][height<=720]+bestaudio[ext=m4a]/best[height<=720][ext=mp4]/best',
        "480p": 'best[height<=480][ext=mp4]/best[ext=mp4]/best',
        "360p": 'best[height<=360][ext=mp4]/best[ext=mp4]/best'
    }
    
    return {
        'format': quality_formats.get(quality, quality_formats["best"]),
        'merge_output_format': 'mp4',
        'noplaylist': True,
        'outtmpl': os.path.join(tempfile.gettempdir(), '%(title).50s.%(ext)s'),
        'quiet': True,
        'no_warnings': True,
    }

def upload_to_fileio(file_path):
    """Загрузка файла на file.io (ссылка живёт 3 дня)"""
    try:
        with open(file_path, 'rb') as f:
            response = requests.post('https://file.io/?expires=3d', files={'file': f}, timeout=300)
        if response.status_code == 200:
            data = response.json()
            return data.get('link')
    except Exception as e:
        print(f"Ошибка загрузки: {e}")
    return None

async def download_instagram(url):
    """Скачивание видео с Instagram"""
    try:
        L = instaloader.Instaloader()
        
        # Извлекаем shortcode из URL
        if '/p/' in url or '/reel/' in url:
            shortcode = url.split('/')[-2]
        else:
            return None, "Неверная ссылка Instagram"
        
        post = instaloader.Post.from_shortcode(L.context, shortcode)
        
        if post.is_video:
            video_url = post.video_url
            temp_path = os.path.join(tempfile.gettempdir(), f"insta_{shortcode}.mp4")
            
            # Скачиваем видео
            response = requests.get(video_url, stream=True, timeout=60)
            with open(temp_path, 'wb') as f:
                for chunk in response.iter_content(chunk_size=8192):
                    f.write(chunk)
            
            return temp_path, None
        else:
            return None, "Это не видео, а фото"
            
    except Exception as e:
        return None, str(e)

# ═══════════════════════════════════════════════════════════
# 📱 КОМАНДЫ БОТА
# ═══════════════════════════════════════════════════════════

@dp.message(Command("start"))
async def start(message: types.Message, state: FSMContext):
    """Приветствие при запуске бота"""
    await state.clear()
    kb = ReplyKeyboardMarkup(
        keyboard=[[KeyboardButton(text="⚙️ Настройки")]],
        resize_keyboard=True
    )
    
    welcome_text = (
        "🎬 <b>Добро пожаловать!</b>\n\n"
        "Я помогу скачать видео с:\n"
        "• TikTok\n"
        "• YouTube (обычные и Shorts)\n"
        "• Instagram (Reels и посты)\n\n"
        "📲 <b>Просто отправь ссылку!</b>\n\n"
        "⚙️ Текущее качество: <code>Лучшее доступное</code>"
    )
    
    await message.answer(welcome_text, reply_markup=kb, parse_mode="HTML")

@dp.message(F.text == "⚙️ Настройки")
async def settings_menu(message: types.Message, state: FSMContext):
    """Меню настроек качества"""
    await state.set_state(VideoStates.choosing_quality)
    
    current_quality = get_quality_setting(message.from_user.id)
    quality_names = {
        "best": "Лучшее доступное",
        "1080p": "Full HD (1080p)",
        "720p": "HD (720p)",
        "480p": "SD (480p)",
        "360p": "Низкое (360p)"
    }
    
    kb = ReplyKeyboardMarkup(
        keyboard=[
            [KeyboardButton(text="🌟 Лучшее доступное")],
            [KeyboardButton(text="🎬 Full HD (1080p)")],
            [KeyboardButton(text="📺 HD (720p)")],
            [KeyboardButton(text="⚡ SD (480p)")],
            [KeyboardButton(text="📱 Низкое (360p)")],
            [KeyboardButton(text="◀️ Назад")]
        ],
        resize_keyboard=True
    )
    
    settings_text = (
        f"⚙️ <b>Настройки качества</b>\n\n"
        f"Сейчас: <b>{quality_names.get(current_quality, 'Лучшее')}</b>\n\n"
        f"Выбери новое качество:"
    )
    
    await message.answer(settings_text, reply_markup=kb, parse_mode="HTML")

# ═══════════════════════════════════════════════════════════
# ⚙️ ОБРАБОТЧИКИ ВЫБОРА КАЧЕСТВА
# ═══════════════════════════════════════════════════════════

@dp.message(VideoStates.choosing_quality, F.text.contains("Лучшее"))
async def set_quality_best(message: types.Message, state: FSMContext):
    user_settings[message.from_user.id] = "best"
    await state.clear()
    kb = ReplyKeyboardMarkup(keyboard=[[KeyboardButton(text="⚙️ Настройки")]], resize_keyboard=True)
    await message.answer("🌟 Выбрано максимальное качество", reply_markup=kb)

@dp.message(VideoStates.choosing_quality, F.text.contains("1080"))
async def set_quality_1080p(message: types.Message, state: FSMContext):
    user_settings[message.from_user.id] = "1080p"
    await state.clear()
    kb = ReplyKeyboardMarkup(keyboard=[[KeyboardButton(text="⚙️ Настройки")]], resize_keyboard=True)
    await message.answer("🎬 Выбрано Full HD (1080p)", reply_markup=kb)

@dp.message(VideoStates.choosing_quality, F.text.contains("720"))
async def set_quality_720p(message: types.Message, state: FSMContext):
    user_settings[message.from_user.id] = "720p"
    await state.clear()
    kb = ReplyKeyboardMarkup(keyboard=[[KeyboardButton(text="⚙️ Настройки")]], resize_keyboard=True)
    await message.answer("📺 Выбрано HD (720p)", reply_markup=kb)

@dp.message(VideoStates.choosing_quality, F.text.contains("480"))
async def set_quality_480p(message: types.Message, state: FSMContext):
    user_settings[message.from_user.id] = "480p"
    await state.clear()
    kb = ReplyKeyboardMarkup(keyboard=[[KeyboardButton(text="⚙️ Настройки")]], resize_keyboard=True)
    await message.answer("⚡ Выбрано SD (480p)", reply_markup=kb)

@dp.message(VideoStates.choosing_quality, F.text.contains("360"))
async def set_quality_360p(message: types.Message, state: FSMContext):
    user_settings[message.from_user.id] = "360p"
    await state.clear()
    kb = ReplyKeyboardMarkup(keyboard=[[KeyboardButton(text="⚙️ Настройки")]], resize_keyboard=True)
    await message.answer("📱 Выбрано низкое качество (360p)", reply_markup=kb)

@dp.message(VideoStates.choosing_quality, F.text == "◀️ Назад")
async def back_to_main(message: types.Message, state: FSMContext):
    await state.clear()
    kb = ReplyKeyboardMarkup(keyboard=[[KeyboardButton(text="⚙️ Настройки")]], resize_keyboard=True)
    await message.answer("Главное меню", reply_markup=kb)

# ═══════════════════════════════════════════════════════════
# 📥 СКАЧИВАНИЕ ВИДЕО
# ═══════════════════════════════════════════════════════════

@dp.message()
async def download_video(message: types.Message):
    """Обработка ссылок на видео"""
    url = message.text.strip()
    
    if not url.startswith(('http://', 'https://')):
        await message.answer("⚠️ Отправь корректную ссылку")
        return

    user_id = message.from_user.id
    quality = get_quality_setting(user_id)
    
    # Определяем платформу
    is_instagram = 'instagram.com' in url
    
    quality_display = {
        "best": "максимальном",
        "1080p": "1080p",
        "720p": "720p",
        "480p": "480p",
        "360p": "360p"
    }
    
    status_msg = await message.answer(
        f"⏳ Скачиваю в {quality_display.get(quality, 'хорошем')} качестве..."
    )

    temp_file = None
    try:
        # ═══════════════════════════════════════════════════════════
        # INSTAGRAM
        # ═══════════════════════════════════════════════════════════
        if is_instagram:
            await status_msg.edit_text("📸 Обрабатываю Instagram...")
            temp_file, error = await download_instagram(url)
            
            if error:
                await status_msg.edit_text(f"❌ {error}")
                return
        
        # ═══════════════════════════════════════════════════════════
        # YOUTUBE / TIKTOK
        # ═══════════════════════════════════════════════════════════
        else:
            ydl_opts = get_ydl_opts(quality)
            with yt_dlp.YoutubeDL(ydl_opts) as ydl:
                info = ydl.extract_info(url, download=True)
                temp_file = ydl.prepare_filename(info)

        # ═══════════════════════════════════════════════════════════
        # ПРОВЕРКА ФАЙЛА
        # ═══════════════════════════════════════════════════════════
        if not os.path.exists(temp_file):
            raise Exception("Не удалось сохранить файл")

        file_size = os.path.getsize(temp_file)
        file_size_mb = file_size / (1024 * 1024)

        # ═══════════════════════════════════════════════════════════
        # ОТПРАВКА ВИДЕО
        # ═══════════════════════════════════════════════════════════
        if file_size <= 50 * 1024 * 1024:
            await status_msg.edit_text(f"📤 Отправляю ({file_size_mb:.1f} МБ)...")
            await bot.send_video(
                chat_id=message.chat.id,
                video=types.FSInputFile(temp_file)
            )
            await status_msg.delete()
        else:
            # Файл больше 50 МБ — загружаем на облако
            await status_msg.edit_text(
                f"📦 Файл большой ({file_size_mb:.1f} МБ)\n"
                f"Загружаю на облако..."
            )

            download_link = await asyncio.get_event_loop().run_in_executor(
                None, upload_to_fileio, temp_file
            )

            if download_link:
                await message.answer(
                    f"☁️ <b>Файл загружен на облако</b>\n\n"
                    f"📎 <a href='{download_link}'>Скачать видео</a>\n\n"
                    f"⏱ Ссылка работает 3 дня",
                    parse_mode="HTML"
                )
                await status_msg.delete()
            else:
                await status_msg.edit_text("❌ Не удалось загрузить на облако")

    except Exception as e:
        error_msg = str(e).lower()
        
        if "private" in error_msg or "login" in error_msg:
            await status_msg.edit_text("🔒 Видео приватное или требует входа")
        elif "404" in error_msg or "not found" in error_msg:
            await status_msg.edit_text("❌ Видео не найдено")
        elif "geo" in error_msg or "country" in error_msg:
            await status_msg.edit_text("🌍 Видео недоступно в вашей стране")
        else:
            await status_msg.edit_text(f"❌ Ошибка: {str(e)[:100]}")
    
    finally:
        # Удаляем временный файл
        if temp_file and os.path.exists(temp_file):
            try:
                os.remove(temp_file)
            except:
                pass

# ═══════════════════════════════════════════════════════════
# 🚀 ЗАПУСК БОТА
# ═══════════════════════════════════════════════════════════

async def main():
    print("\n" + "="*50)
    print("✅ Бот запущен!")
    print("📱 Поддерживаемые платформы:")
    print("   • TikTok")
    print("   • YouTube")
    print("   • Instagram")
    print("="*50 + "\n")
    await dp.start_polling(bot)

if __name__ == "__main__":
    try:
        asyncio.run(main())
    except KeyboardInterrupt:
        print("\n🛑 Бот остановлен")