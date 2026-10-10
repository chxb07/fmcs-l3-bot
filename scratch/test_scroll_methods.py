import sys
import asyncio
import logging
from pathlib import Path

if sys.platform == "win32":
    sys.stdout.reconfigure(encoding="utf-8")

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from playwright.async_api import async_playwright
from monitor import is_valid_post_link, normalize_post_url, PAGE_URL

logging.basicConfig(level=logging.INFO, format="%(asctime)s [%(levelname)s] %(message)s")

async def test_scroll_methods():
    async with async_playwright() as p:
        browser = await p.chromium.launch(headless=True)
        page = await browser.new_page(
            locale="fr-FR",
            user_agent="Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/130.0.0.0 Safari/537.36"
        )
        try:
            await page.goto(PAGE_URL, wait_until="domcontentloaded", timeout=60000)
            await page.wait_for_timeout(4000)

            # Attempt closing overlays
            try:
                close_btn = page.locator('[aria-label="Fermer"], [aria-label="Close"], [aria-label="Allow all cookies"]')
                if await close_btn.count() > 0:
                    await close_btn.first.click()
                    print("Closed cookie/login overlay.")
                    await page.wait_for_timeout(1000)
            except Exception:
                pass

            initial_count = await page.locator('[role="article"]').count()
            print(f"Initial DOM articles count: {initial_count}")

            # Try scrolling using Page.keyboard 'PageDown' / 'End'
            for i in range(1, 4):
                await page.keyboard.press("PageDown")
                await page.keyboard.press("PageDown")
                await page.wait_for_timeout(2000)
                c = await page.locator('[role="article"]').count()
                print(f"PageDown step {i}: articles = {c}")

        finally:
            await browser.close()

if __name__ == "__main__":
    asyncio.run(test_scroll_methods())
