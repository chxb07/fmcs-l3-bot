import sys
import asyncio
import logging
from pathlib import Path

if sys.platform == "win32":
    sys.stdout.reconfigure(encoding="utf-8")

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from playwright.async_api import async_playwright
from monitor import PAGE_URL

logging.basicConfig(level=logging.INFO, format="%(asctime)s [%(levelname)s] %(message)s")

async def inspect_details():
    async with async_playwright() as p:
        browser = await p.chromium.launch(headless=True)
        page = await browser.new_page(
            locale="fr-FR",
            user_agent="Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/130.0.0.0 Safari/537.36"
        )
        try:
            await page.goto(PAGE_URL, wait_until="domcontentloaded", timeout=60000)
            await page.wait_for_timeout(4000)

            # Dismiss overlay modal if present
            try:
                close_btn = page.locator('[aria-label="Fermer"], [aria-label="Close"], [aria-label="Allow all cookies"]')
                if await close_btn.count() > 0:
                    await close_btn.first.click()
                    await page.wait_for_timeout(1000)
            except Exception:
                pass

            await page.evaluate("window.scrollBy(0, 1500)")
            await page.wait_for_timeout(3000)

            articles = page.locator('[role="article"]')
            count = await articles.count()
            print(f"Total Articles Found: {count}\n")

            for i in range(min(count, 5)):
                article = articles.nth(i)
                raw_text = (await article.inner_text()).strip()

                # Inspect all images inside this article container
                images = await article.locator("img").evaluate_all(
                    """imgs => imgs.map(img => ({
                        src: img.src,
                        alt: img.alt || "",
                        width: img.width,
                        height: img.height,
                        ariaLabel: img.getAttribute("aria-label") || ""
                    }))"""
                )

                # Inspect links
                links = await article.locator("a[href]").evaluate_all(
                    """els => els.map(a => ({
                        text: (a.innerText || "").trim(),
                        href: a.href,
                        ariaLabel: a.getAttribute("aria-label") || ""
                    }))"""
                )

                print("="*80)
                print(f"ARTICLE #{i + 1}")
                print("="*80)
                print(f"RAW TEXT:\n{raw_text[:400]}...\n")
                print("IMAGES FOUND:")
                for img in images:
                    print(f"  - src: {img['src'][:100]} | size: {img['width']}x{img['height']} | alt: {img['alt'][:50]}")
                print("\nLINKS FOUND (First 5):")
                for l in links[:5]:
                    print(f"  - [{l['text']}] aria: '{l['ariaLabel']}' -> {l['href'][:80]}")
                print("\n")

        finally:
            await browser.close()

if __name__ == "__main__":
    asyncio.run(inspect_details())
