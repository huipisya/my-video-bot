# Исправления для Instagram share ссылок
# Заменить соответствующие функции в bot.py

import aiohttp
import re
import time
import tempfile
import os
import json
from pathlib import Path
from typing import Optional, List, Tuple

# Эти переменные должны быть определены в bot.py
# IG_CONTEXT: Optional[BrowserContext] = None
# IG_PLAYWRIGHT_READY: bool = False

async def expand_instagram_share_url(url: str) -> Optional[str]:
    """Развернуть Instagram share ссылку в полную URL с несколькими методами"""
    try:
        # Метод 1: HTTP редиректы (текущий)
        headers = {
            'User-Agent': 'Mozilla/5.0 (iPhone; CPU iPhone OS 17_0 like Mac OS X) AppleWebKit/605.1.15 (KHTML, like Gecko) Version/17.0 Mobile/15E148 Safari/604.1',
            'Accept': 'text/html,application/xhtml+xml,application/xml;q=0.9,*/*;q=0.8',
            'Accept-Language': 'en-US,en;q=0.5',
            'Accept-Encoding': 'gzip, deflate',
            'Connection': 'keep-alive',
        }
        
        async with aiohttp.ClientSession(headers=headers) as session:
            async with session.get(url, allow_redirects=True, timeout=10) as response:
                final_url = str(response.url)
                logger.info(f"Instagram share URL развернут в: {final_url}")
                
                # Если все еще share ссылка, пробуем извлечь из HTML
                if '/share/' in final_url:
                    logger.info("Все еще share ссылка, пробуем извлечь из HTML...")
                    html = await response.text()
                    
                    # Ищем канонический URL
                    canonical_match = re.search(r'<link rel="canonical" href="([^"]+)"', html)
                    if canonical_match:
                        canonical_url = canonical_match.group(1)
                        if canonical_url != final_url and "instagram.com" in canonical_url:
                            logger.info(f"Найден канонический URL: {canonical_url}")
                            final_url = canonical_url
                    
                    # Ищем og:url
                    if '/share/' in final_url:
                        og_match = re.search(r'<meta property="og:url" content="([^"]+)"', html)
                        if og_match:
                            og_url = og_match.group(1)
                            if og_url != final_url and "instagram.com" in og_url:
                                logger.info(f"Найден og:url: {og_url}")
                                final_url = og_url
                
                # Проверяем, что это действительно Instagram URL с контентом
                if "instagram.com" in final_url and (
                    "/p/" in final_url or 
                    "/reel/" in final_url or 
                    "/tv/" in final_url
                ):
                    return final_url
                else:
                    logger.warning(f"Развернутый URL не содержит контента Instagram: {final_url}")
                    return None
                    
    except Exception as e:
        logger.error(f"Ошибка при развертывании Instagram share URL: {e}")
        return None

async def download_direct_video(video_url: str, description: str) -> Tuple[Optional[str], Optional[List[str]], str]:
    """Скачивание видео напрямую по URL"""
    try:
        temp_file = os.path.join(tempfile.gettempdir(), f"ig_video_{int(time.time())}.mp4")
        
        headers = {
            'User-Agent': 'Mozilla/5.0 (iPhone; CPU iPhone OS 17_0 like Mac OS X) AppleWebKit/605.1.15 (KHTML, like Gecko) Version/17.0 Mobile/15E148 Safari/604.1',
            'Referer': 'https://www.instagram.com/',
            'Accept': '*/*',
            'Accept-Language': 'en-US,en;q=0.5',
            'Accept-Encoding': 'gzip, deflate',
            'Connection': 'keep-alive',
        }
        
        async with aiohttp.ClientSession(headers=headers) as session:
            async with session.get(video_url, timeout=30) as response:
                if response.status == 200:
                    with open(temp_file, 'wb') as f:
                        async for chunk in response.content.iter_chunked(8192):
                            f.write(chunk)
                    logger.info(f"Successfully downloaded video directly: {os.path.basename(temp_file)}")
                    return temp_file, None, description
                else:
                    logger.error(f"Failed to download video directly, status: {response.status}")
    except Exception as e:
        logger.error(f"Direct download failed: {e}")
    
    return None, None, description

async def download_instagram_with_playwright(url: str) -> Tuple[Optional[str], Optional[List[str]], str]:
    """Улучшенное скачивание с Instagram через Playwright"""
    global IG_CONTEXT
    if not IG_PLAYWRIGHT_READY or not IG_CONTEXT:
        return None, None, ""

    logger.info(f"Скачивание с Instagram через Playwright...")
    page = None
    try:
        # Создаем новую страницу с мобильным user-agent
        page = await IG_CONTEXT.new_page(
            user_agent='Mozilla/5.0 (iPhone; CPU iPhone OS 17_0 like Mac OS X) AppleWebKit/605.1.15 (KHTML, like Gecko) Version/17.0 Mobile/15E148 Safari/604.1',
            viewport={'width': 375, 'height': 812},
            device_scale_factor=3,
            is_mobile=True,
            has_touch=True
        )
        
        await page.goto(url, wait_until='networkidle')
        await page.wait_for_timeout(5000)  # Увеличим ожидание

        page_title = await page.title()
        logger.info(f"Page title: {page_title}")
        logger.info(f"Final URL: {page.url}")
        
        # Если страница входа, пробуем обойти через JavaScript
        if "Login" in page_title or "Вход" in page_title or "accounts/login" in page.url:
            logger.warning("Instagram требует вход, пробуем альтернативные методы")
            
            # Пробуем получить og:video мета-тег напрямую
            try:
                og_video = await page.evaluate('''() => {
                    const meta = document.querySelector('meta[property="og:video"]');
                    return meta ? meta.getAttribute('content') : null;
                }''')
                if og_video:
                    logger.info(f"Found og:video via JS: {og_video}")
                    return await download_direct_video(og_video, "Instagram video")
            except Exception as e:
                logger.debug(f"JS extraction failed: {e}")
            
            # Пробуем получить данные из встроенных скриптов
            try:
                scripts = await page.query_selector_all('script[type="text/javascript"]')
                for script in scripts:
                    content = await script.text_content()
                    if content and 'video_url' in content:
                        video_urls = re.findall(r'"video_url":"([^"]+)"', content)
                        if video_urls:
                            decoded_url = video_urls[0].replace('\\/', '/')
                            logger.info(f"Found video URL in script: {decoded_url}")
                            return await download_direct_video(decoded_url, "Instagram video")
            except Exception as e:
                logger.debug(f"Script parsing failed: {e}")
            
            return None, None, ""

        description_element = page.locator('article div._ab1k._ab1l div._aa99._aamp span')
        description = await description_element.first.text_content() if await description_element.count() > 0 else ""

        # Улучшенное обнаружение видео через JavaScript
        video_url = None
        
        # Метод 1: og:video мета-теги
        if not video_url:
            try:
                video_url = await page.evaluate('''() => {
                    const selectors = [
                        'meta[property="og:video:secure_url"]',
                        'meta[property="og:video"]',
                        'meta[name="og:video"]'
                    ];
                    for (const selector of selectors) {
                        const meta = document.querySelector(selector);
                        if (meta) return meta.getAttribute('content');
                    }
                    return null;
                }''')
                if video_url:
                    logger.info(f"Found video URL via JS: {video_url}")
            except Exception as e:
                logger.debug(f"JS video extraction failed: {e}")

        # Метод 2: Поиск видео элементов
        if not video_url:
            try:
                video_url = await page.evaluate('''() => {
                    const selectors = [
                        'article video source[src*="mp4"]',
                        'article video[src*="mp4"]',
                        'video source[src*="mp4"]',
                        'video[src*="mp4"]'
                    ];
                    for (const selector of selectors) {
                        const element = document.querySelector(selector);
                        if (element) return element.src || element.getAttribute('src');
                    }
                    return null;
                }''')
                if video_url:
                    logger.info(f"Found video element via JS: {video_url}")
            except Exception as e:
                logger.debug(f"JS element extraction failed: {e}")

        # Метод 3: Поиск в window.__additionalData
        if not video_url:
            try:
                video_url = await page.evaluate('''() => {
                    if (window.__additionalData) {
                        const dataStr = JSON.stringify(window.__additionalData);
                        const match = dataStr.match(/https:\\/\\/[^"\\s]*\\.mp4[^"\\s]*/);
                        if (match) return match[0].replace(/\\\//g, '/');
                    }
                    if (window._sharedData) {
                        const dataStr = JSON.stringify(window._sharedData);
                        const match = dataStr.match(/https:\\/\\/[^"\\s]*\\.mp4[^"\\s]*/);
                        if (match) return match[0].replace(/\\\//g, '/');
                    }
                    return null;
                }''')
                if video_url:
                    logger.info(f"Found video URL in window data: {video_url}")
            except Exception as e:
                logger.debug(f"Window data extraction failed: {e}")

        if video_url:
            return await download_direct_video(video_url, description)

        # Если видео не найдено, пробуем скачать фото
        url_lower = (url or "").lower()
        should_download_photos = not any(token in url_lower for token in ("/reel/", "/reels/", "/tv/"))
        
        if should_download_photos:
            logger.info("Пробуем скачать фото...")
            try:
                photo_urls = await page.evaluate('''() => {
                    const urls = [];
                    // og:image
                    const ogImage = document.querySelector('meta[property="og:image"]');
                    if (ogImage) {
                        const url = ogImage.getAttribute('content');
                        if (url && !url.includes('150x150') && !url.includes('50x50')) {
                            urls.push(url);
                        }
                    }
                    // Фото элементы
                    document.querySelectorAll('article img[src*="cdninstagram"]').forEach(img => {
                        const url = img.src;
                        if (url && !url.includes('150x150') && !url.includes('50x50') && !urls.includes(url)) {
                            urls.push(url);
                        }
                    });
                    return urls.slice(0, 10);
                }''')
                
                if photo_urls:
                    logger.info(f"Found {len(photo_urls)} photos")
                    temp_dir = tempfile.mkdtemp()
                    photo_paths = []
                    async with aiohttp.ClientSession() as session:
                        for i, photo_url in enumerate(photo_urls):
                            try:
                                headers = {
                                    'User-Agent': 'Mozilla/5.0 (iPhone; CPU iPhone OS 17_0 like Mac OS X) AppleWebKit/605.1.15 (KHTML, like Gecko) Version/17.0 Mobile/15E148 Safari/604.1',
                                    'Referer': 'https://www.instagram.com/'
                                }
                                async with session.get(photo_url, headers=headers) as resp:
                                    if resp.status == 200:
                                        photo_path = os.path.join(temp_dir, f"ig_photo_{i+1}.jpg")
                                        with open(photo_path, 'wb') as f:
                                            f.write(await resp.read())
                                        photo_paths.append(photo_path)
                                        logger.info(f"Downloaded photo: {photo_path}")
                            except Exception as e:
                                logger.warning(f"Failed to download photo {i}: {e}")
                                continue
                    if photo_paths:
                        return None, photo_paths, description
            except Exception as e:
                logger.warning(f"Photo download failed: {e}")

    except Exception as e:
        logger.error(f"Ошибка в Playwright Instagram: {e}")
    finally:
        if page:
            await page.close()
    
    return None, None, ""

async def download_instagram_final_fallback(url: str) -> Tuple[Optional[str], Optional[List[str]], str]:
    """Финальный fallback метод - извлечение из HTML"""
    try:
        logger.info("Пробуем извлечь видео из HTML...")
        headers = {
            'User-Agent': 'Mozilla/5.0 (iPhone; CPU iPhone OS 17_0 like Mac OS X) AppleWebKit/605.1.15 (KHTML, like Gecko) Version/17.0 Mobile/15E148 Safari/604.1',
            'Accept': 'text/html,application/xhtml+xml,application/xml;q=0.9,*/*;q=0.8',
            'Accept-Language': 'en-US,en;q=0.5',
        }
        
        async with aiohttp.ClientSession(headers=headers) as session:
            async with session.get(url, timeout=15) as response:
                if response.status == 200:
                    html = await response.text()
                    
                    # Ищем видео URL в HTML
                    video_patterns = [
                        r'"video_url":"([^"]+\.mp4[^"]*)"',
                        r'"videoUrl":"([^"]+\.mp4[^"]*)"',
                        r'"contentUrl":"([^"]+\.mp4[^"]*)"',
                        r'https://[^"\s]*\.mp4[^"\s]*'
                    ]
                    
                    for pattern in video_patterns:
                        matches = re.findall(pattern, html)
                        if matches:
                            video_url = matches[0].replace('\\/', '/')
                            logger.info(f"Found video URL in HTML: {video_url}")
                            return await download_direct_video(video_url, "Instagram video")
                    
                    # Пробуем фото
                    photo_matches = re.findall(r'"display_url":"([^"]+)"', html)
                    if photo_matches:
                        temp_dir = tempfile.mkdtemp()
                        photo_paths = []
                        async with aiohttp.ClientSession() as photo_session:
                            for i, photo_url in enumerate(photo_matches[:10]):
                                try:
                                    photo_url = photo_url.replace('\\/', '/')
                                    async with photo_session.get(photo_url) as resp:
                                        if resp.status == 200:
                                            photo_path = os.path.join(temp_dir, f"ig_photo_{i+1}.jpg")
                                            with open(photo_path, 'wb') as f:
                                                f.write(await resp.read())
                                            photo_paths.append(photo_path)
                                except Exception:
                                    continue
                        if photo_paths:
                            return None, photo_paths, "Instagram photos"
                
    except Exception as e:
        logger.error(f"HTML extraction failed: {e}")
    
    return None, None, ""

# Инструкции по установке:
# 1. Заменить функцию expand_instagram_share_url на новую версию
# 2. Заменить функцию download_instagram_with_playwright на новую версию  
# 3. Добавить функцию download_direct_video
# 4. Добавить функцию download_instagram_final_fallback
# 5. В конце функции download_instagram добавить вызов download_instagram_final_fallback
