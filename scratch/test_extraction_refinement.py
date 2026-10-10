import sys
import asyncio
import logging
from pathlib import Path

if sys.platform == "win32":
    sys.stdout.reconfigure(encoding="utf-8")

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from playwright.async_api import async_playwright
from monitor import PAGE_URL, is_valid_post_link, normalize_post_url

logging.basicConfig(level=logging.INFO, format="%(asctime)s [%(levelname)s] %(message)s")

def clean_post_text(raw_text: str) -> str:
    if not raw_text:
        return ""
    lines = [line.strip() for line in raw_text.splitlines() if line.strip()]
    cleaned_lines = []
    
    IGNORE_EXACT = {"·", "J’aime", "Commenter", "Partager", "Voir plus", "Voir plus de commentaires", "En voir plus"}
    
    for line in lines:
        if line in IGNORE_EXACT:
            continue
        if line.startswith("Faculty of Mathematics and Computer Science"):
            continue
        if line.startswith("Toutes les réactions"):
            break
        if line.startswith("il y a ") and ("heures" in line or "jours" in line or "minutes" in line):
            continue
        # Check if line is just a relative timestamp like "22 h" or "5 j"
        if len(line) <= 6 and (line.endswith(" h") or line.endswith(" j") or line.endswith(" min") or line.endswith(" h") or line.endswith(" j")):
            continue

        cleaned_lines.append(line)
        
    return "\n".join(cleaned_lines).strip()

async def test_refinement():
    async with async_playwright() as p:
        browser = await p.chromium.launch(headless=True)
        page = await browser.new_page(
            locale="fr-FR",
            user_agent="Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/130.0.0.0 Safari/537.36"
        )
        try:
            await page.goto(PAGE_URL, wait_until="domcontentloaded", timeout=60000)
            await page.wait_for_timeout(4000)

            # Close overlays if any
            try:
                close_btn = page.locator('[aria-label="Fermer"], [aria-label="Close"], [aria-label="Allow all cookies"]')
                if await close_btn.count() > 0:
                    await close_btn.first.click()
                    await page.wait_for_timeout(1000)
            except Exception:
                pass

            await page.evaluate("window.scrollBy(0, 2000)")
            await page.wait_for_timeout(3000)

            articles = page.locator('[role="article"]')
            count = await articles.count()
            
            posts = []
            for i in range(min(count, 15)):
                article = articles.nth(i)
                raw_text = (await article.inner_text()).strip()
                cleaned_text = clean_post_text(raw_text)
                if not cleaned_text:
                    continue

                images = await article.locator("img").evaluate_all(
                    """imgs => imgs.map(img => ({
                        src: img.src,
                        width: img.width,
                        height: img.height
                    }))"""
                )

                main_image = None
                for img in images:
                    src = img["src"]
                    w, h = img["width"], img["height"]
                    if src.startswith("http") and ("scontent" in src or "fbcdn.net" in src):
                        if w > 100 and h > 100 and not any(p in src for p in ["/p50x50/", "/p40x40/", "/p100x100/", "/s50x50/"]):
                            main_image = src
                            break

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
                    if is_valid_post_link(href):
                        norm = normalize_post_url(href)
                        if norm and norm != "https://www.facebook.com/fmcs.univbba":
                            if not post_url:
                                post_url = norm

                            for field in [link["text"], link["ariaLabel"], link["title"]]:
                                if field and any(kw in field.lower() for kw in ["h", "min", "j", "hier", "d’hier", "ساعة", "دقيقة", "أمس", "يوم", "2026", "2025", "2024", "jan", "fév", "mar", "avr", "mai", "juin", "juil", "août", "sep", "oct", "nov", "déc"]):
                                    timestamp = field
                                    break

                if post_url:
                    posts.append({
                        "url": post_url,
                        "text": cleaned_text,
                        "image_url": main_image,
                        "timestamp": timestamp or "N/A"
                    })

            unique = {}
            for p_item in posts:
                unique.setdefault(p_item["url"], p_item)

            unique_list = list(unique.values())
            print(f"Unique Extracted Refined Posts Count: {len(unique_list)}\n")
            for idx, item in enumerate(unique_list, start=1):
                print(f"POST #{idx}:")
                print(f"  URL: {item['url']}")
                print(f"  Timestamp: {item['timestamp']}")
                print(f"  Image URL: {item['image_url'] or 'None'}")
                print(f"  Clean Text:\n{item['text'][:200]}...\n" + "-"*40)

        finally:
            await browser.close()

if __name__ == "__main__":
    asyncio.run(test_refinement())
