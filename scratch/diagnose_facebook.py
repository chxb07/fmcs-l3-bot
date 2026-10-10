import sys
import asyncio
import logging
from pathlib import Path

if sys.platform == "win32":
    sys.stdout.reconfigure(encoding="utf-8")

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from playwright.async_api import async_playwright
from monitor import extract_posts, PAGE_URL

logging.basicConfig(level=logging.INFO, format="%(asctime)s [%(levelname)s] %(message)s")

async def run_diagnostic():
    async with async_playwright() as p:
        browser = await p.chromium.launch(headless=True)
        page = await browser.new_page(
            locale="fr-FR",
            user_agent="Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/130.0.0.0 Safari/537.36"
        )
        try:
            posts = await extract_posts(page)
            print("\n" + "="*80)
            print("DIAGNOSTIC EXTRACTION REPORT (Using monitor.py extract_posts)")
            print("="*80)
            print(f"Total Unique Posts Extracted: {len(posts)}")
            print("-"*80)

            for idx, post in enumerate(posts, start=1):
                print(f"Post #{idx}:")
                print(f"  URL: {post['url']}")
                print(f"  Timestamp: {post.get('timestamp', 'N/A')}")
                print(f"  Snippet: {post['text'].replace('\n', ' ')[:180]}...")
                print("-"*40)

        finally:
            await browser.close()

if __name__ == "__main__":
    asyncio.run(run_diagnostic())
