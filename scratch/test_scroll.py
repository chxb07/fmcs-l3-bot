import sys
import asyncio
import logging
from pathlib import Path

if sys.platform == "win32":
    sys.stdout.reconfigure(encoding="utf-8")

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from urllib.parse import parse_qsl, urlencode, urlparse, urlunparse
from playwright.async_api import async_playwright
from monitor import normalize_post_url, is_valid_post_link, PAGE_URL

logging.basicConfig(level=logging.INFO, format="%(asctime)s [%(levelname)s] %(message)s")

async def test_scroll_and_timestamp():
    async with async_playwright() as p:
        browser = await p.chromium.launch(headless=True)
        page = await browser.new_page(
            locale="fr-FR",
            user_agent="Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/130.0.0.0 Safari/537.36"
        )
        try:
            logging.info(f"Navigating to {PAGE_URL}...")
            await page.goto(PAGE_URL, wait_until="domcontentloaded", timeout=60000)
            await page.wait_for_timeout(5000)

            # Close cookie / login dialogs if present
            try:
                close_btn = page.locator('[aria-label="Fermer"], [aria-label="Close"], [aria-label="Allow all cookies"]')
                if await close_btn.count() > 0:
                    await close_btn.first.click()
                    await page.wait_for_timeout(1000)
            except Exception:
                pass

            print("\n--- Scrolling to trigger lazy loading ---")
            for step in range(1, 6):
                await page.evaluate("window.scrollBy(0, 1500)")
                await page.wait_for_timeout(3000)
                articles_count = await page.locator('[role="article"]').count()
                print(f"Scroll step {step}: Articles found in DOM = {articles_count}")

            articles = page.locator('[role="article"]')
            count = await articles.count()
            print(f"\nTotal Article Elements after Scrolling: {count}")

            extracted = []
            for i in range(count):
                article = articles.nth(i)
                text = (await article.inner_text()).strip()

                links = await article.locator("a[href]").evaluate_all(
                    """els => els.map(a => ({
                        text: (a.innerText || "").trim(),
                        href: a.href,
                        ariaLabel: a.getAttribute("aria-label") || "",
                        title: a.getAttribute("title") || ""
                    }))"""
                )

                post_url = None
                timestamp = None

                for link in links:
                    href = link["href"]
                    # Extract timestamp: on FB posts, the post timestamp link usually points to /posts/... or /permalink.php
                    # and its text or aria-label contains time info like "21 h", "Il y a 21 heures", "hier", etc.
                    if is_valid_post_link(href):
                        norm = normalize_post_url(href)
                        if norm and norm != "https://www.facebook.com/fmcs.univbba":
                            if not post_url:
                                post_url = norm
                            
                            # Check link text or aria-label or title for timestamp
                            for field in [link["text"], link["ariaLabel"], link["title"]]:
                                if field and any(kw in field.lower() for kw in ["h", "min", "j", "hier", "d’hier", "ساعة", "دقيقة", "أمس", "يوم", "202", "jan", "fév", "mar", "avr", "mai", "juin", "juil", "août", "sep", "oct", "nov", "déc"]):
                                    timestamp = field
                                    break

                extracted.append({
                    "index": i + 1,
                    "text_snippet": text.replace("\n", " ")[:200] if text else "<EMPTY>",
                    "links_count": len(links),
                    "post_url": post_url,
                    "timestamp": timestamp,
                })

            print("\n" + "="*80)
            print("DETAILED ARTICLE ANALYSIS")
            print("="*80)
            for item in extracted:
                print(f"Article #{item['index']}:")
                print(f"  Post URL: {item['post_url'] or 'NONE'}")
                print(f"  Timestamp: {item['timestamp'] or 'N/A'}")
                print(f"  Links inside container: {item['links_count']}")
                print(f"  Snippet: {item['text_snippet'][:150]}")
                print("-" * 40)

        finally:
            await browser.close()

if __name__ == "__main__":
    asyncio.run(test_scroll_and_timestamp())
