# Используем официальный образ Python 3.12
FROM python:3.12-slim

# Устанавливаем рабочую директорию в контейнере
WORKDIR /app

# Обновляем список пакетов
RUN apt-get update

# Устанавливаем ffmpeg
RUN apt-get install -y --no-install-recommends ffmpeg

# Устанавливаем зависимости для запуска браузеров Playwright
# playwright install chromium требует эти пакеты
RUN apt-get install -y \
    chromium \
    chromium-driver \
    libnss3 \
    libatk-bridge2.0-0 \
    libdrm2 \
    libxkbcommon0 \
    libxcomposite1 \
    libxdamage1 \
    libxrandr2 \
    libgbm1 \
    libxss1 \
    libasound2 \
    --no-install-recommends

# Удаляем кэш пакетов для уменьшения размера образа
RUN rm -rf /var/lib/apt/lists/*

# Копируем файл зависимостей в контейнер
COPY requirements.txt .

# Устанавливаем pip и зависимости Python
RUN pip install --no-cache-dir --upgrade pip
RUN pip install --no-cache-dir -r requirements.txt

# Устанавливаем браузеры Playwright
# playwright install chromium
RUN playwright install chromium

# Копируем исходный код бота в контейнер
COPY . .

# Устанавливаем переменные окружения
ENV PYTHONUNBUFFERED=1
ENV PORT=8000

# Команда для запуска бота
CMD ["python", "bot.py"]