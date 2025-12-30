"""
Тестовый скрипт для проверки InstagramDownloader.
Запуск: python test_instagram.py
"""
import asyncio
import sys
sys.path.insert(0, '.')

from bot import InstagramDownloader, download_instagram

# Тестовые URL (публичные посты)
TEST_URLS = [
    # Reel
    "https://www.instagram.com/reel/C0xyzABC123/",
    # Post
    "https://www.instagram.com/p/C0xyzDEF456/",
]

async def test_method(downloader: InstagramDownloader, method_name: str, url: str):
    """Тестирует отдельный метод."""
    print(f"\n--- Testing {method_name} ---")
    method = getattr(downloader, method_name)
    try:
        result = await method(url)
        video, photos, desc = result
        if video:
            print(f"✓ Got video: {video}")
            return True
        elif photos:
            print(f"✓ Got {len(photos)} photos")
            return True
        else:
            print(f"✗ No result")
            return False
    except Exception as e:
        print(f"✗ Error: {e}")
        return False

async def main():
    print("=== Instagram Downloader Test ===\n")
    
    downloader = InstagramDownloader()
    
    # Тест расширения share URL
    print("1. Testing share URL expansion:")
    share_url = "https://www.instagram.com/share/test123"
    expanded = await downloader._expand_share_url(share_url)
    print(f"   Share URL: {share_url}")
    print(f"   Expanded: {expanded}")
    
    # Тест shortcode extraction
    print("\n2. Testing shortcode extraction:")
    test_cases = [
        "https://www.instagram.com/reel/ABC123/",
        "https://www.instagram.com/p/DEF456/",
        "https://www.instagram.com/tv/GHI789/",
    ]
    for url in test_cases:
        code = downloader._extract_shortcode(url)
        print(f"   {url} -> {code}")
    
    print("\n3. Quick method test (using public reel):")
    print("   Note: Actual download may fail if Instagram blocks the request")
    
    # Если хотите протестировать реальное скачивание, укажите рабочий URL:
    # test_url = "https://www.instagram.com/reel/REAL_SHORTCODE/"
    # result = await download_instagram(test_url)
    # print(f"   Result: video={result[0] is not None}, photos={result[1] is not None}")
    
    print("\n=== Test Complete ===")

if __name__ == "__main__":
    asyncio.run(main())
