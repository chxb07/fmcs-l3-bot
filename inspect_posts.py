import asyncio
from playwright.async_api import async_playwright

URL = "https://www.facebook.com/fmcs.univbba"

async def main():
    async with async_playwright() as p:
        browser = await p.chromium.launch(headless=True)
        page = await browser.new_page(locale="fr-FR")

        try:
            await page.goto(
                URL,
                wait_until="domcontentloaded",
                timeout=60000,
            )
            await page.wait_for_timeout(6000)

            articles = page.locator('[role="article"]')
            count = await articles.count()

            print("Articles found:", count)

            for i in range(min(count, 10)):
                article = articles.nth(i)
                text = (await article.inner_text()).strip()

                if not text:
                    continue

                print(f"\n{'=' * 40}")
                print(f"ARTICLE {i + 1}")
                print(text[:1800])

                links = await article.locator("a[href]").evaluate_all(
                    """elements => elements.map(a => ({
                        text: (a.innerText || "").trim(),
                        href: a.href
                    })).filter(x => x.href.includes("facebook.com"))"""
                )

                seen = set()
                for link in links:
                    href = link["href"].split("?")[0]
                    if href not in seen:
                        seen.add(href)
                        print("LINK:", link["text"][:80], href)

            if count == 0:
                print("No article containers detected.")

        except Exception as error:
            print("Error:", str(error))

        finally:
            await browser.close()

asyncio.run(main())