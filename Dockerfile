# Используем официальный образ Python 3.12
FROM python:3.12-slim

# Устанавливаем рабочую директорию в контейнере
WORKDIR /app

# Обновляем список пакетов и устанавливаем ffmpeg
RUN apt-get update && apt-get install -y --no-install-recommends ffmpeg \
    && rm -rf /var/lib/apt/lists/*

# Копируем файл зависимостей в контейнер
COPY requirements.txt .

# Устанавливаем pip и зависимости Python
RUN pip install --no-cache-dir --upgrade pip
RUN pip install --no-cache-dir -r requirements.txt

# Устанавливаем системные зависимости для Playwright
RUN playwright install-deps chromium

# Устанавливаем браузеры Playwright
RUN playwright install chromium

# Копируем исходный код бота в контейнер
COPY . .

# Устанавливаем переменные окружения
ENV PYTHONUNBUFFERED=1
ENV PORT=8000

# Команда для запуска бота
CMD ["python", "bot.py"]
