
import os
import sys
import json
from bot import get_ydl_opts
import yt_dlp

# Mock environment variables if needed
os.environ["BOT_TOKEN"] = "test"
os.environ["BOT_USERNAME"] = "test_bot"

def test_format_selection(url, quality="best"):
    print(f"\n--- Testing URL: {url} with quality: {quality} ---")
    opts = get_ydl_opts(quality, use_youtube_cookies=False)
    
    # We only want to simulate, not download
    opts['simulate'] = True
    opts['quiet'] = True
    opts['forceurl'] = True  # Just to ensure we get some output if we were printing
    
    with yt_dlp.YoutubeDL(opts) as ydl:
        try:
            info = ydl.extract_info(url, download=False)
            if not info:
                print("No info extracted.")
                return

            print(f"Title: {info.get('title')}")
            
            # The 'format' field in info will be the selected format string (e.g. "137+140") 
            # or we can look at 'format_id' and find the corresponding format in 'formats' list.
            
            # However, when 'download=False' and we use format selectors, 'info' dict usually contains 
            # the *selected* format info merged into the main dict, or we can look at 'requested_formats'.
            
            requested = info.get('requested_formats')
            if requested:
                print("Selected Formats:")
                for fmt in requested:
                    print(f"  ID: {fmt.get('format_id')}, Res: {fmt.get('width')}x{fmt.get('height')}, Note: {fmt.get('format_note')}, Ext: {fmt.get('ext')}")
                    
                # Calculate combined resolution if video+audio
                video_fmt = next((f for f in requested if f.get('vcodec') != 'none'), None)
                if video_fmt:
                     print(f"FINAL VIDEO RESOLUTION: {video_fmt.get('width')}x{video_fmt.get('height')}")
            else:
                # If it's a single file download (no merge)
                print(f"Selected Format ID: {info.get('format_id')}")
                print(f"Resolution: {info.get('width')}x{info.get('height')}")
                print(f"FINAL VIDEO RESOLUTION: {info.get('width')}x{info.get('height')}")

        except Exception as e:
            print(f"Error: {e}")

if __name__ == "__main__":
    # Vertical (Shorts) - 1080x1920 usually available
    print("TESTING VERTICAL VIDEO (Expect 1080x1920 to fail with old logic)")
    test_format_selection("https://www.youtube.com/shorts/2g811Eo7K8U", "1080p")
    
    # Horizontal - 1920x1080 usually available
    print("\nTESTING HORIZONTAL VIDEO (Should work fine)")
    test_format_selection("https://www.youtube.com/watch?v=dQw4w9WgXcQ", "1080p")
