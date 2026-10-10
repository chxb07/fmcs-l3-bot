
import sys
import requests
from bs4 import BeautifulSoup

# Ensure Windows terminal handles UTF-8 characters (Arabic/French text)
if hasattr(sys.stdout, "reconfigure"):
    sys.stdout.reconfigure(encoding="utf-8")

URL = "https://www.facebook.com/fmcs.univbba"

headers = {
    "User-Agent": (
        "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
        "AppleWebKit/537.36 (KHTML, like Gecko) "
        "Chrome/130.0.0.0 Safari/537.36"
    ),
    "Accept-Language": "fr-FR,fr;q=0.9,ar;q=0.8,en;q=0.7",
}

try:
    response = requests.get(
        URL,
        headers=headers,
        timeout=20,
    )

    print("HTTP status:", response.status_code)
    print("Final URL:", response.url)

    soup = BeautifulSoup(response.text, "html.parser")

    if soup.title:
        print("Page title:", soup.title.get_text(strip=True))

    posts = soup.select('[data-ad-preview="message"]')
    print("Possible post messages:", len(posts))

    for number, post in enumerate(posts[:5], start=1):
        print(f"\n--- Post {number} ---")
        print(post.get_text(" ", strip=True)[:1000])

    if response.status_code != 200:
        print("\n[!] Facebook restricted standard HTTP GET request (HTTP", response.status_code, ").")
        print("    Facebook blocks automated scraping without browser sessions/cookies or Graph API authentication.")
    elif not posts:
        print("\n[*] Page retrieved but no post messages were matched with selector '[data-ad-preview=\"message\"]'.")

except requests.RequestException as error:
    print("Connection error:", error)

