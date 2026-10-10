import asyncio
from playwright.async_api import async_playwright

URL = "https://www.facebook.com/fmcs.univbba"

async def main():
    async with async_playwright() as p:
        browser = await p.chromium.launch(headless=True)
        page = await browser.new_page(
            locale="fr-FR",
            user_agent=(
                "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
                "AppleWebKit/537.36 (KHTML, like Gecko) "
                "Chrome/130.0.0.0 Safari/537.36"
            ),
        )

        try:
            response = await page.goto(
                URL,
                wait_until="domcontentloaded",
                timeout=60000,
            )

            print("HTTP status:", response.status if response else "No response")
            print("Final URL:", page.url)
            print("Page title:", await page.title())

            await page.wait_for_timeout(5000)

            posts = await page.locator(
                '[data-ad-preview="message"]'
            ).all_inner_texts()

            print("Possible posts found:", len(posts))

            for i, post in enumerate(posts[:5], start=1):
                print(f"\n--- Post {i} ---")
                print(post[:1000])

            if not posts:
                print("\nNo posts detected. The page may restrict automated access.")

        except Exception as error:
            print("Error:", str(error))

        finally:
            await browser.close()

asyncio.run(main())