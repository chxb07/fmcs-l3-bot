import os
import json
import asyncio
import logging
import re
from datetime import datetime
from pathlib import Path
from urllib.parse import parse_qsl, urlencode, urlparse, urlunparse
from dotenv import load_dotenv
from playwright.async_api import async_playwright
from telegram.ext import ApplicationBuilder, CommandHandler

# Configure logging
logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(message)s",
)

PAGE_URL = "https://www.facebook.com/fmcs.univbba"
SEEN_FILE = Path("seen_posts.json")
BASELINE_FILE = Path("baseline_state.json")
CHECK_INTERVAL = 600

ALLOWED_QUERY_PARAMS = {"story_fbid", "id", "fbid", "v", "set"}

LEGACY_MIGRATION_AMBIGUITY = (
    "Existing state cannot establish the age or delivery status of every visible "
    "post. Recorded URLs are preserved as delivered, persisted pending deliveries "
    "are retained, and other currently visible unrecorded posts are classified as "
    "historical during migration to avoid a repost flood. This conservative "
    "snapshot cannot recover failed deliveries that were never persisted, and may "
    "skip a genuinely new post visible in the migration scan."
)


class StateFileError(Exception):
    """Raised when an existing state file is corrupt, truncated, or unreadable."""


class FatalStatePersistenceError(StateFileError):
    """Raised when required state persistence fails and monitoring must stop."""


def normalize_post_url(url: str) -> str:
    """Normalize Facebook URLs so proven-equivalent post variants share one identity.

    Handles scheme, Facebook host variants, trailing slashes, and stable query
    parameter order. `/posts/{id}` and `permalink.php`/`story.php` with the same
    `story_fbid` collapse to `https://www.facebook.com/posts/{id}`. Photo/video
    paths and mismatched identifiers are left distinct.
    """
    if not url:
        return ""

    url_without_frag = url.split("#")[0].strip()
    parsed = urlparse(url_without_frag)

    netloc = (parsed.netloc or "").lower()
    if netloc.startswith("www."):
        host_core = netloc[4:]
    else:
        host_core = netloc
    is_facebook = host_core == "facebook.com" or host_core.endswith(".facebook.com")

    if is_facebook:
        netloc = "www.facebook.com"
        scheme = "https"
    else:
        scheme = parsed.scheme or "https"

    path = parsed.path or ""
    if len(path) > 1 and path.endswith("/"):
        path = path[:-1]

    query_tuples = [
        (k, v) for k, v in parse_qsl(parsed.query, keep_blank_values=True)
        if k in ALLOWED_QUERY_PARAMS
    ]
    query_tuples.sort(key=lambda item: (item[0], item[1]))

    if is_facebook:
        post_id = _facebook_post_id(path, query_tuples)
        if post_id:
            return urlunparse(("https", "www.facebook.com", f"/posts/{post_id}", "", "", ""))

    clean_query = urlencode(query_tuples)
    return urlunparse((scheme, netloc, path, "", clean_query, ""))


def _diagnose_published_at(
    value: str | None, time_element_count: int = 1
) -> tuple[str, datetime | None]:
    if time_element_count == 0:
        return "time_element_missing", None
    if time_element_count > 1:
        return "multiple_time_elements", None
    if not isinstance(value, str) or not value.strip():
        return "datetime_missing_or_empty", None
    try:
        published_at = datetime.fromisoformat(value.strip().replace("Z", "+00:00"))
    except ValueError:
        return "invalid_iso_format", None
    if published_at.utcoffset() is None:
        return "timezone_missing", None
    return "valid", published_at


def _classify_date_attribute(value: str | None) -> str:
    if not isinstance(value, str) or not value.strip():
        return "empty"
    category, _ = _diagnose_published_at(value)
    if category == "valid":
        return "absolute_valid"
    if re.search(
        r"\b(?:il y a|ago|yesterday|hier|منذ|أمس)\b|"
        r"\d+\s*(?:h|min|j|hour|minute|day|heure|jour)\b",
        value,
        flags=re.IGNORECASE,
    ):
        return "relative_text"
    return "unknown"


def _parse_published_at(value: str | None) -> datetime | None:
    """Parse a trusted ISO publication time, or return None when it is untrusted."""
    category, published_at = _diagnose_published_at(value)
    return published_at if category == "valid" else None


def _facebook_post_id(path: str, query_tuples: list[tuple[str, str]]) -> str | None:
    """Return a post id only when the URL shape uniquely identifies a feed post."""
    parts = [part for part in path.split("/") if part]
    lowered = [part.lower() for part in parts]

    if "photos" in lowered or "videos" in lowered:
        return None

    if "posts" in lowered:
        idx = lowered.index("posts")
        if idx + 1 < len(parts) and parts[idx + 1]:
            return parts[idx + 1]
        return None

    if lowered in (["permalink.php"], ["story.php"]):
        for key, value in query_tuples:
            if key == "story_fbid" and value:
                return value
    return None


def is_valid_post_link(href: str) -> bool:
    """Check whether a URL points to an individual Facebook post, photo, video, or permalink."""
    if not href:
        return False
    parsed = urlparse(href)
    if "facebook.com" not in parsed.netloc.lower():
        return False

    path = parsed.path.lower().rstrip("/")
    if path.endswith("/permalink.php") and "story_fbid" in parsed.query:
        return True

    parts = path.split("/")
    for marker in ("posts", "photos", "videos", "story.php"):
        if marker in parts:
            idx = parts.index(marker)
            if idx < len(parts) - 1 and parts[idx + 1]:
                return True
    return False


def atomic_write_text(filepath: Path, text: str) -> None:
    """Write text atomically (temp file + fsync + replace). Leaves the original on failure."""
    tmp_path = filepath.with_name(filepath.name + ".tmp")
    try:
        with open(tmp_path, "w", encoding="utf-8", newline="\n") as handle:
            handle.write(text)
            handle.flush()
            os.fsync(handle.fileno())
        os.replace(tmp_path, filepath)
    except Exception:
        try:
            tmp_path.unlink(missing_ok=True)
        except OSError:
            pass
        raise


def _load_json_if_exists(filepath: Path):
    """Load JSON from an existing file. Missing files return None. Corrupt files raise."""
    if not filepath.exists():
        return None
    try:
        raw = filepath.read_text(encoding="utf-8")
    except OSError as err:
        raise StateFileError(f"Unable to read state file {filepath}: {err}") from err
    try:
        return json.loads(raw)
    except json.JSONDecodeError as err:
        raise StateFileError(
            f"Corrupt or truncated state file {filepath}: {err}. "
            "Refusing to treat it as empty to avoid mass reposting."
        ) from err


def load_seen(filepath: Path = SEEN_FILE) -> set[str]:
    """Load previously seen post URLs from JSON file, converting to normalized URLs."""
    raw_data = _load_json_if_exists(filepath)
    if raw_data is None:
        return set()
    if not isinstance(raw_data, list):
        raise StateFileError(
            f"State file {filepath} must be a JSON list of URLs, got {type(raw_data).__name__}."
        )
    normalized_urls = set()
    for url in raw_data:
        if not isinstance(url, str) or not url.strip():
            raise StateFileError(
                f"State file {filepath} must contain only non-empty URL strings."
            )
        normalized_url = normalize_post_url(url)
        if not normalized_url:
            raise StateFileError(f"State file {filepath} contains an invalid URL.")
        normalized_urls.add(normalized_url)
    return normalized_urls


def save_seen(seen: set[str], filepath: Path = SEEN_FILE) -> None:
    """Atomically save seen post URLs to JSON file."""
    payload = json.dumps(sorted(seen), ensure_ascii=False, indent=2)
    atomic_write_text(filepath, payload)


def load_baseline_state(filepath: Path = BASELINE_FILE) -> dict:
    """Load baseline state from JSON file. Missing file yields a fresh baseline."""
    data = _load_json_if_exists(filepath)
    if data is None:
        return {
            "baseline_completed": False,
            "historical_urls": [],
            "pending_posts": [],
            "history_migration_completed": False,
            "latest_post_startup_migration_completed": False,
            "first_start_post_sent": False,
            "last_successful_scan_at": None,
        }
    if not isinstance(data, dict):
        raise StateFileError(
            f"State file {filepath} must be a JSON object, got {type(data).__name__}."
        )
    baseline_completed = data.get("baseline_completed")
    if not isinstance(baseline_completed, bool):
        raise StateFileError(
            f"State file {filepath} has invalid baseline_completed (expected boolean)."
        )
    history_migration_completed = data.get("history_migration_completed", False)
    if not isinstance(history_migration_completed, bool):
        raise StateFileError(
            f"State file {filepath} has invalid history_migration_completed (expected boolean)."
        )
    latest_post_startup_migration_completed = data.get(
        "latest_post_startup_migration_completed", False
    )
    if not isinstance(latest_post_startup_migration_completed, bool):
        raise StateFileError(
            f"State file {filepath} has invalid "
            "latest_post_startup_migration_completed (expected boolean)."
        )
    first_start_post_sent = data.get("first_start_post_sent", False)
    if not isinstance(first_start_post_sent, bool):
        raise StateFileError(
            f"State file {filepath} has invalid "
            "first_start_post_sent (expected boolean)."
        )
    last_successful_scan_at = data.get("last_successful_scan_at")
    if last_successful_scan_at is not None:
        if (
            not isinstance(last_successful_scan_at, str)
            or _parse_published_at(last_successful_scan_at) is None
        ):
            raise StateFileError(
                f"State file {filepath} has invalid "
                "last_successful_scan_at (expected timezone-aware ISO datetime)."
            )
    historical = data.get("historical_urls")
    if not isinstance(historical, list):
        raise StateFileError(
            f"State file {filepath} has invalid historical_urls (expected list)."
        )
    if any(not isinstance(url, str) or not url.strip() for url in historical):
        raise StateFileError(
            f"State file {filepath} has invalid historical_urls (expected non-empty URL strings)."
        )

    pending_posts = data.get("pending_posts", [])
    if not isinstance(pending_posts, list):
        raise StateFileError(
            f"State file {filepath} has invalid pending_posts (expected list)."
        )
    normalized_pending_posts = []
    for post in pending_posts:
        if not isinstance(post, dict):
            raise StateFileError(
                f"State file {filepath} has invalid pending_posts entry (expected object)."
            )
        url = post.get("url")
        text = post.get("text")
        image_url = post.get("image_url")
        timestamp = post.get("timestamp", "N/A")
        first_start_delivery = post.get("first_start_delivery", False)
        baseline_initial_delivery = post.get("baseline_initial_delivery", False)
        confirmed_new_post = post.get("confirmed_new_post", False)
        published_at = post.get("published_at")
        if (
            not isinstance(url, str) or not url.strip()
            or not isinstance(text, str)
            or (image_url is not None and not isinstance(image_url, str))
            or not isinstance(timestamp, str)
            or not isinstance(first_start_delivery, bool)
            or not isinstance(baseline_initial_delivery, bool)
            or not isinstance(confirmed_new_post, bool)
            or (
                published_at is not None
                and (
                    not isinstance(published_at, str)
                    or _parse_published_at(published_at) is None
                )
            )
        ):
            raise StateFileError(
                f"State file {filepath} has invalid pending_posts entry fields."
            )
        normalized_url = normalize_post_url(url)
        if not normalized_url:
            raise StateFileError(
                f"State file {filepath} has invalid pending_posts URL."
            )
        normalized_post = {
            "url": normalized_url,
            "text": text,
            "image_url": image_url,
            "timestamp": timestamp,
        }
        if first_start_delivery:
            normalized_post["first_start_delivery"] = True
        if baseline_initial_delivery:
            normalized_post["baseline_initial_delivery"] = True
        if confirmed_new_post:
            normalized_post["confirmed_new_post"] = True
        if published_at is not None:
            normalized_post["published_at"] = published_at
        normalized_pending_posts.append(normalized_post)
    return {
        "baseline_completed": baseline_completed,
        "history_migration_completed": history_migration_completed,
        "latest_post_startup_migration_completed": (
            latest_post_startup_migration_completed
        ),
        "first_start_post_sent": first_start_post_sent,
        "last_successful_scan_at": last_successful_scan_at,
        "historical_urls": [
            normalize_post_url(url) for url in historical if normalize_post_url(url)
        ],
        "pending_posts": normalized_pending_posts,
    }


def save_baseline_state(state: dict, filepath: Path = BASELINE_FILE) -> None:
    """Atomically save baseline state to JSON file."""
    state.setdefault("latest_post_startup_migration_completed", True)
    state.setdefault("first_start_post_sent", False)
    state.setdefault("last_successful_scan_at", None)
    payload = json.dumps(state, ensure_ascii=False, indent=2)
    atomic_write_text(filepath, payload)


def apply_legacy_seen_migration(
    posts: list[dict],
    seen: set[str],
    baseline_state: dict,
    baseline_filepath: Path,
) -> bool:
    """Conservatively snapshot visible history when existing state is ambiguous.

    Legacy completed baselines are not proof that all visible posts were recorded.
    Preserve delivered and pending URLs, classify other visible URLs as historical,
    and persist the migration marker before any later delivery decisions. A fresh
    empty setup is not migrated here; it follows the initial-baseline behavior.
    """
    if (
        baseline_state.get("history_migration_completed", False)
        or not posts
        or (not baseline_state.get("baseline_completed") and not seen)
    ):
        return False

    historical = list(baseline_state.get("historical_urls") or [])
    historical_set = {normalize_post_url(url) for url in historical}
    normalized_seen = {normalize_post_url(url) for url in seen}
    pending_urls = {
        normalize_post_url(post.get("url", ""))
        for post in baseline_state.get("pending_posts", [])
    }

    for post in posts:
        url = normalize_post_url(post.get("url", ""))
        if (
            url
            and url not in normalized_seen
            and url not in historical_set
            and url not in pending_urls
        ):
            historical.append(url)
            historical_set.add(url)

    pending = {
        **baseline_state,
        "baseline_completed": True,
        "history_migration_completed": True,
        "latest_post_startup_migration_completed": True,
        "historical_urls": historical,
        "pending_posts": baseline_state.get("pending_posts", []),
    }
    try:
        save_baseline_state(pending, baseline_filepath)
    except Exception as err:
        raise FatalStatePersistenceError(
            f"Unable to persist legacy history migration to {baseline_filepath}: {err}"
        ) from err
    baseline_state.clear()
    baseline_state.update(pending)
    logging.warning(LEGACY_MIGRATION_AMBIGUITY)
    logging.info(
        "Legacy migration persisted: %s recorded URL(s) kept as delivered, "
        "%s currently visible unrecorded URL(s) marked historical.",
        len(normalized_seen),
        len(historical),
    )
    return True


def apply_latest_post_startup_migration(
    posts: list[dict],
    seen: set[str],
    baseline_state: dict,
    baseline_filepath: Path,
) -> str | bool | None:
    """Queue the first post in the extracted feed for one-time legacy setup."""
    if (
        baseline_state.get("latest_post_startup_migration_completed", True)
        or not baseline_state.get("baseline_completed")
        or not baseline_state.get("history_migration_completed")
        or not seen
        or not posts
    ):
        return None

    latest_post = posts[0]
    latest_url = normalize_post_url(latest_post.get("url", ""))
    if not latest_url:
        logging.warning(
            "Latest-post startup migration deferred: first extracted post "
            "has no usable URL."
        )
        return False
    normalized_seen = {normalize_post_url(url) for url in seen}
    historical = list(baseline_state.get("historical_urls", []))
    historical_set = {normalize_post_url(url) for url in historical}
    pending_posts = list(baseline_state.get("pending_posts", []))
    pending_urls = {
        normalize_post_url(post.get("url", "")) for post in pending_posts
    }

    for post in posts:
        url = normalize_post_url(post.get("url", ""))
        if (
            url
            and url != latest_url
            and url not in normalized_seen
            and url not in historical_set
            and url not in pending_urls
        ):
            historical.append(url)
            historical_set.add(url)

    if latest_url not in normalized_seen and latest_url not in pending_urls:
        pending_posts.append({
            "url": latest_url,
            "text": latest_post["text"],
            "image_url": latest_post.get("image_url"),
            "timestamp": latest_post.get("timestamp", "N/A"),
        })

    updated_state = {
        **baseline_state,
        "baseline_completed": True,
        "history_migration_completed": True,
        "latest_post_startup_migration_completed": True,
        "historical_urls": historical,
        "pending_posts": pending_posts,
    }
    try:
        save_baseline_state(updated_state, baseline_filepath)
    except Exception as err:
        raise FatalStatePersistenceError(
            "Unable to persist latest-post startup migration to "
            f"{baseline_filepath}: {err}"
        ) from err
    baseline_state.clear()
    baseline_state.update(updated_state)
    logging.info(
        "Latest-post startup migration persisted; latest post %s.",
        "was already recorded" if latest_url in normalized_seen else "was queued",
    )
    return latest_url


def clean_post_text(raw_text: str) -> str:
    """Clean raw article inner text by removing page header chrome, FB action buttons, and reaction footers."""
    if not raw_text:
        return ""
    lines = [line.strip() for line in raw_text.splitlines() if line.strip()]
    cleaned_lines = []

    IGNORE_EXACT = {
        "·", "J\u2019aime", "Commenter", "Commentaires", "Partager", "Partages",
        "Voir plus", "Voir plus de commentaires", "En voir plus", "Voir plus de r\u00e9ponses",
        "R\u00e9actions",
    }

    for line in lines:
        if line in IGNORE_EXACT:
            continue
        if line.startswith("Faculty of Mathematics and Computer Science"):
            continue
        if line.startswith("Toutes les r\u00e9actions"):
            break
        if line.startswith("il y a ") and ("heures" in line or "jours" in line or "minutes" in line):
            continue
        if len(line) <= 6 and (line.endswith(" h") or line.endswith(" j") or line.endswith(" min") or line.endswith("\u00a0h") or line.endswith("\u00a0j")):
            continue
        if line.lower() in {"hier", "aujourd\u2019hui"}:
            continue

        cleaned_lines.append(line)

    return "\n".join(cleaned_lines).strip()


def format_telegram_message(post_text: str, post_url: str, timestamp: str = "", max_length: int = 4096) -> str:
    """Format a Telegram message ensuring length <= max_length."""
    ts_str = f" ({timestamp})" if timestamp and timestamp != "N/A" else ""
    header = f"📢 منشور جديد — صفحة الكلية{ts_str}\n\n"
    footer = f"\n\n🔗 {post_url}"

    max_text_len = max_length - len(header) - len(footer) - 5
    if len(post_text) > max_text_len:
        post_text = post_text[:max_text_len] + "..."
    return f"{header}{post_text}{footer}"


def parse_chat_id(chat_id_str: str) -> int | str:
    """Parse CHAT_ID env variable safely as numeric int or channel username string."""
    s = chat_id_str.strip()
    if s.startswith("@"):
        return s
    try:
        return int(s)
    except ValueError:
        return s


def is_telegram_destination_configured(bot, chat_id: int | str | None) -> bool:
    """Return True only when a bot and a non-blank chat destination are present."""
    if bot is None or chat_id is None:
        return False
    if isinstance(chat_id, str) and not chat_id.strip():
        return False
    return True


def get_send_initial_batch_on_start() -> bool:
    """Check whether sending the latest post on startup is enabled."""
    val = os.getenv("SEND_INITIAL_BATCH_ON_START", os.getenv("SEND_LATEST_ON_START", "false")).strip().lower()
    return val in ("true", "1", "yes")


async def send_telegram_message_with_retry(
    bot, chat_id: int | str, text: str, max_retries: int = 3
) -> bool:
    """Send a Telegram message with retry logic for transient network failures."""
    for attempt in range(1, max_retries + 1):
        try:
            await bot.send_message(chat_id=chat_id, text=text)
            return True
        except Exception as err:
            logging.warning(
                f"Telegram send attempt {attempt}/{max_retries} failed: {type(err).__name__}: {err}"
            )
            if attempt < max_retries:
                await asyncio.sleep(2 ** attempt)
    return False


async def send_telegram_photo_with_retry(
    bot, chat_id: int | str, photo_url: str, caption: str, max_retries: int = 3
) -> bool:
    """Send a Telegram photo with caption, with retry logic for transient failures."""
    for attempt in range(1, max_retries + 1):
        try:
            await bot.send_photo(chat_id=chat_id, photo=photo_url, caption=caption)
            return True
        except Exception as err:
            logging.warning(
                f"Telegram photo send attempt {attempt}/{max_retries} failed: {type(err).__name__}: {err}"
            )
            if attempt < max_retries:
                await asyncio.sleep(2 ** attempt)
    return False


async def send_post_to_telegram(bot, chat_id: int | str, post: dict) -> bool:
    """Send a single post to Telegram as photo or text message.

    Returns True only after Telegram confirms a successful send. Missing bot or
    chat configuration is a failed delivery, never a success. Photo failures
    fall back to a text message that still includes the original post URL.
    """
    if not is_telegram_destination_configured(bot, chat_id):
        logging.error("Cannot deliver post: Telegram bot or chat ID is missing/blank.")
        return False

    image_url = post.get("image_url")
    timestamp = post.get("timestamp", "")

    if image_url:
        caption = format_telegram_message(
            post["text"], post["url"], timestamp=timestamp, max_length=1024
        )
        photo_ok = await send_telegram_photo_with_retry(bot, chat_id, image_url, caption)
        if photo_ok:
            return True
        logging.warning(
            "Photo delivery failed for %s; falling back to text-only send.",
            post.get("url"),
        )
        text = format_telegram_message(
            post["text"], post["url"], timestamp=timestamp, max_length=4096
        )
        return await send_telegram_message_with_retry(bot, chat_id, text)

    text = format_telegram_message(
        post["text"], post["url"], timestamp=timestamp, max_length=4096
    )
    return await send_telegram_message_with_retry(bot, chat_id, text)


async def extract_posts(page) -> list[dict]:
    """Extract public posts from Facebook page using Playwright with bounded smart scrolling."""
    try:
        response = await page.goto(
            PAGE_URL,
            wait_until="domcontentloaded",
            timeout=60000,
        )
        status = response.status if response else "No response"
        logging.info(f"Facebook HTTP status: {status} | Page URL: {page.url}")
        if response is None or not 200 <= response.status < 300:
            raise RuntimeError(
                f"Facebook page request failed with HTTP status {status}."
            )
    except Exception as goto_err:
        logging.error(f"Navigation to Facebook page failed: {type(goto_err).__name__}: {goto_err}")
        raise

    await page.wait_for_timeout(4000)

    # Dismiss cookie / login modal overlay if present to allow unblocked scrolling
    try:
        close_btn = page.locator('[aria-label="Fermer"], [aria-label="Close"], [aria-label="Allow all cookies"], [aria-label="Decline optional cookies"]')
        if await close_btn.count() > 0:
            await close_btn.first.click()
            await page.wait_for_timeout(1000)
    except Exception:
        pass

    # Bounded smart scroll: scroll up to 3 times to trigger Facebook lazy loading
    MAX_SCROLLS = 3
    previous_count = 0

    for scroll_idx in range(MAX_SCROLLS):
        await page.evaluate("window.scrollBy(0, 2000)")
        await page.wait_for_timeout(2500)
        articles_loc = page.locator('[role="article"]')
        current_count = await articles_loc.count()

        if scroll_idx > 0 and current_count <= previous_count:
            logging.info(f"Smart scroll stopped at step {scroll_idx + 1}: no new articles rendered ({current_count} total).")
            break
        previous_count = current_count

    articles = page.locator('[role="article"]')
    count = await articles.count()
    logging.info(f"Article elements found in DOM: {count}")

    posts = []
    for i in range(min(count, 30)):
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

        time_elements = article.locator("time[datetime]")
        time_count = await time_elements.count()
        published_at = (
            await time_elements.first.get_attribute("datetime")
            if time_count == 1
            else None
        )
        publication_date_category, _ = _diagnose_published_at(
            published_at,
            time_count,
        )
        if time_count == 0:
            date_candidate_elements = await article.locator(
                "time, a[href]"
            ).evaluate_all(
                """els => els.map(el => ({
                    tag: el.tagName.toLowerCase(),
                    attributes: ["datetime", "title", "aria-label", "data-utime", "data-time"]
                        .map(name => [name, el.getAttribute(name)])
                        .filter(([, value]) => value !== null)
                }))"""
            )
            diagnostic_candidates = []
            for element in date_candidate_elements:
                for attribute, value in element["attributes"]:
                    diagnostic_candidates.append(
                        f"{element['tag']}[{attribute}]="
                        f"{_classify_date_attribute(value)}"
                    )
            logging.info(
                "Publication date DOM diagnostic: post_index=%d candidates=%s",
                i + 1,
                ",".join(diagnostic_candidates) if diagnostic_candidates else "none",
            )

        post_url = None
        timestamp = None

        for link in links:
            href = link["href"]
            if is_valid_post_link(href):
                normalized = normalize_post_url(href)
                if normalized and normalized != "https://www.facebook.com/fmcs.univbba":
                    if not post_url:
                        post_url = normalized

                    for field in [link["text"], link["ariaLabel"], link["title"]]:
                        if field and any(kw in field.lower() for kw in ["h", "min", "j", "hier", "d’hier", "ساعة", "دقيقة", "أمس", "يوم", "2026", "2025", "2024", "jan", "fév", "mar", "avr", "mai", "juin", "juil", "août", "sep", "oct", "nov", "déc"]):
                            timestamp = field
                            break

        if post_url:
            posts.append({
                "url": post_url,
                "text": cleaned_text,
                "image_url": main_image,
                "timestamp": timestamp or "N/A",
                "published_at": published_at,
                "_time_element_count": time_count,
                "_publication_date_category": publication_date_category,
            })

    # Preserve top-down feed order while deduplicating by normalized URL
    unique = {}
    for post in posts:
        unique.setdefault(post["url"], post)

    extracted_list = list(unique.values())
    logging.info(f"Extracted {len(extracted_list)} unique post(s) after deduplication.")
    return extracted_list


def _commit_delivered_url(seen: set[str], url: str, filepath: Path) -> None:
    normalized = normalize_post_url(url)
    if not normalized:
        return
    updated_seen = seen | {normalized}
    try:
        save_seen(updated_seen, filepath=filepath)
    except Exception as err:
        raise FatalStatePersistenceError(
            f"Unable to persist delivered post state to {filepath}: {err}"
        ) from err
    seen.add(normalized)


async def process_posts_scan(
    posts: list[dict],
    seen: set[str],
    bot=None,
    chat_id: int | str | None = None,
    filepath: Path = SEEN_FILE,
    baseline_filepath: Path = BASELINE_FILE,
    baseline_state: dict | None = None,
    send_initial_batch_on_start: bool = False,
    send_first_start_post: bool = False,
) -> tuple[int, int]:
    """
    Process extracted posts against the seen set and baseline state.

    First startup (baseline not completed, empty seen set):
    - If send_initial_batch_on_start is True, send the first post in the
      extracted feed. Only mark it as seen after successful delivery.
    - All remaining posts are recorded as historical in baseline_state so they
      won't be sent later.
    - A summary message is sent.

    Ambiguous-state migration (a completed baseline without the migration marker,
    or a non-empty legacy seen set without a completed baseline):
    - Preserve recorded URLs as delivered.
    - Preserve persisted pending deliveries and classify other currently visible
      unrecorded posts as historical (do not send).
    - Persist the migration marker before any further delivery decisions.

    Subsequent scans (baseline completed):
    - Send ALL new posts individually in feed order, oldest first after reversing
      the newest-first page results.
    - Filter out both successfully-delivered posts (seen) and historical posts.
    - Only mark as seen after successful delivery.

    Returns (detected_count, new_sent_count).
    """
    detected_count = len(posts)
    new_sent_count = 0

    if baseline_state is None:
        baseline_state = load_baseline_state(baseline_filepath)

    previous_scan_at_text = (
        baseline_state.get("last_successful_scan_at")
    )
    previous_scan_at = _parse_published_at(previous_scan_at_text)
    scan_checkpoint_at = datetime.now().astimezone()

    baseline_state.setdefault("pending_posts", [])

    legacy_migrated = bool(
        posts
        and apply_legacy_seen_migration(
            posts, seen, baseline_state, baseline_filepath
        )
    )
    if legacy_migrated and not send_first_start_post:
        if is_telegram_destination_configured(bot, chat_id):
            summary_text = (
                f"ℹ️ اكتملت تهيئة الحالة القديمة. "
                f"تم الاحتفاظ بـ {len(seen)} رابطًا مسجلاً، "
                f"وتسجيل {len(baseline_state['historical_urls'])} منشورًا ظاهرًا غير مسجل كمنشورات تاريخية. "
                "لن تُرسل المنشورات الحالية تلقائيًا."
            )
            await send_telegram_message_with_retry(bot, chat_id, summary_text)
        logging.info(
            "Legacy migration persisted. Continuing with pending deliveries; "
            "currently visible unrecorded posts remain historical. Detected %s post(s).",
            detected_count,
        )

    startup_migration_url = None
    startup_migration_deferred = False
    if posts:
        startup_migration_url = apply_latest_post_startup_migration(
            posts, seen, baseline_state, baseline_filepath
        )
    if startup_migration_url is False:
        logging.warning(
            "Startup state migration was deferred; new posts will not be queued "
            "in this scan, but persisted pending deliveries will still be retried."
        )
        startup_migration_deferred = True
    if isinstance(startup_migration_url, str):
        logging.info(
            "Latest-post startup migration complete; continuing with persisted "
            "pending deliveries only."
        )

    first_start_post_url = None
    first_start_selection_deferred = False
    first_start_historical_urls = None
    if send_first_start_post and posts:
        selected_post = posts[0]
        first_start_post_url = normalize_post_url(selected_post.get("url", ""))
        if first_start_post_url:
            logging.info(
                "/start latest post selected from DOM: post_index=1 already_seen=%s.",
                first_start_post_url in {normalize_post_url(url) for url in seen},
            )
            pending_urls = {
                normalize_post_url(post.get("url", ""))
                for post in baseline_state["pending_posts"]
            }
            historical = list(baseline_state["historical_urls"])
            historical_set = {normalize_post_url(url) for url in historical}
            seen_set = {normalize_post_url(url) for url in seen}
            for post in posts[1:]:
                url = normalize_post_url(post.get("url", ""))
                if (
                    url
                    and url not in seen_set
                    and url not in historical_set
                    and url not in pending_urls
                ):
                    historical.append(url)
                    historical_set.add(url)
            first_start_historical_urls = historical
        else:
            first_start_selection_deferred = True
            logging.warning(
                "First /start post selection skipped: first DOM post has no usable URL."
            )

    is_first_scan = bool(posts and not baseline_state["baseline_completed"])
    if is_first_scan:
        # First startup / baseline
        if send_initial_batch_on_start and posts:
            latest_post = posts[0]
            latest_url = normalize_post_url(latest_post["url"])
            batch_posts = [latest_post]
            historical = [
                post for post in posts
                if normalize_post_url(post["url"]) != latest_url
            ]

            # Record historical posts as intentionally ignored, then mark baseline done
            pending_historical = list(baseline_state["historical_urls"])
            for post in historical:
                url = normalize_post_url(post["url"])
                if url and url not in pending_historical:
                    pending_historical.append(url)
            pending = {
                "baseline_completed": True,
                "history_migration_completed": True,
                "latest_post_startup_migration_completed": True,
                "historical_urls": pending_historical,
                "pending_posts": list(baseline_state["pending_posts"]),
            }
            pending_urls = {
                normalize_post_url(post["url"])
                for post in pending["pending_posts"]
            }
            for post in batch_posts:
                url = normalize_post_url(post["url"])
                if url and url not in pending_urls:
                    pending["pending_posts"].append({
                        "url": url,
                        "text": post["text"],
                        "image_url": post.get("image_url"),
                        "timestamp": post.get("timestamp", "N/A"),
                        "baseline_initial_delivery": True,
                    })
                    pending_urls.add(url)
            save_baseline_state(pending, baseline_filepath)
            baseline_state.clear()
            baseline_state.update(pending)
        else:
            # No initial batch: mark all posts as historical
            pending_historical = list(baseline_state["historical_urls"])
            for post in posts:
                url = normalize_post_url(post["url"])
                if url and url not in pending_historical:
                    pending_historical.append(url)
            pending = {
                "baseline_completed": True,
                "history_migration_completed": True,
                "latest_post_startup_migration_completed": True,
                "historical_urls": pending_historical,
                "pending_posts": list(baseline_state["pending_posts"]),
            }
            save_baseline_state(pending, baseline_filepath)
            baseline_state.clear()
            baseline_state.update(pending)

    historical_set = {
        normalize_post_url(url) for url in baseline_state["historical_urls"]
    }
    seen_set = {normalize_post_url(url) for url in seen}
    pending_posts = [
        dict(post) for post in baseline_state["pending_posts"]
    ]
    pending_urls = {normalize_post_url(post["url"]) for post in pending_posts}
    added_posts = 0
    new_posts = []
    for post_index, post in enumerate(posts, start=1):
        if first_start_selection_deferred or startup_migration_deferred:
            break
        url = normalize_post_url(post.get("url", ""))
        if (
            url
            and url not in seen_set
            and url not in historical_set
            and url not in pending_urls
        ):
            published_at = _parse_published_at(post.get("published_at"))
            if previous_scan_at is None:
                logging.info(
                    "Ignoring unseen post during scan initialization: "
                    "post_index=%d reason=no_prior_successful_scan_timestamp.",
                    post_index,
                )
                continue
            if published_at is None:
                category, _ = _diagnose_published_at(
                    post.get("published_at"),
                    post.get("_time_element_count", 1),
                )
                logging.info(
                    "Ignoring unseen post: post_index=%d "
                    "reason=untrusted_publication_timestamp category=%s.",
                    post_index,
                    category,
                )
                continue
            if published_at <= previous_scan_at:
                logging.info(
                    "Ignoring unseen post: post_index=%d "
                    "reason=not_newer_than_last_successful_scan.",
                    post_index,
                )
                continue
            new_posts.append({
                "url": url,
                "text": post["text"],
                "image_url": post.get("image_url"),
                "timestamp": post.get("timestamp", "N/A"),
                "published_at": published_at.isoformat(),
                "confirmed_new_post": True,
            })
            pending_urls.add(url)
            added_posts += 1
    new_posts.sort(
        key=lambda post: _parse_published_at(post["published_at"]),
        reverse=True,
    )
    pending_posts = new_posts + pending_posts

    if first_start_post_url:
        matching_pending_post = next(
            (
                post for post in pending_posts
                if normalize_post_url(post.get("url", "")) == first_start_post_url
            ),
            None,
        )
        if matching_pending_post is None:
            selected_post = posts[0]
            matching_pending_post = {
                "url": first_start_post_url,
                "text": selected_post["text"],
                "image_url": selected_post.get("image_url"),
                "timestamp": selected_post.get("timestamp", "N/A"),
            }
            pending_posts.append(matching_pending_post)
        matching_pending_post["first_start_delivery"] = True

    updated_baseline_state = {
        **baseline_state,
        "pending_posts": pending_posts,
    }
    if first_start_historical_urls is not None:
        updated_baseline_state["historical_urls"] = first_start_historical_urls
    if not startup_migration_deferred:
        updated_baseline_state["last_successful_scan_at"] = (
            scan_checkpoint_at.isoformat()
        )

    pending_posts_changed = pending_posts != baseline_state["pending_posts"]
    historical_urls_changed = (
        first_start_historical_urls is not None
        and first_start_historical_urls != baseline_state["historical_urls"]
    )
    if (
        pending_posts_changed
        or historical_urls_changed
        or (
            not startup_migration_deferred
            and previous_scan_at_text
            != updated_baseline_state["last_successful_scan_at"]
        )
    ):
        try:
            save_baseline_state(updated_baseline_state, baseline_filepath)
        except Exception as err:
            raise FatalStatePersistenceError(
                "Unable to persist scan checkpoint and pending delivery state to "
                f"{baseline_filepath}: {err}"
            ) from err
    baseline_state.clear()
    baseline_state.update(updated_baseline_state)

    if posts and baseline_state["baseline_completed"]:
        logging.info(
            "Scan complete: %s detected, %s new queued, %s pending delivery.",
            detected_count,
            added_posts,
            len(pending_posts),
        )
    elif not posts and not pending_posts:
        logging.info("Scan complete: 0 posts detected.")
        return 0, 0

    # Pending deliveries are retried even if their posts are no longer in the scrape window.
    for post in reversed(list(baseline_state["pending_posts"])):
        url = normalize_post_url(post["url"])
        is_first_start_delivery = bool(post.get("first_start_delivery"))
        is_baseline_initial_delivery = bool(
            post.get("baseline_initial_delivery")
        )
        if url in seen_set and not is_first_start_delivery:
            baseline_state["pending_posts"] = [
                item for item in baseline_state["pending_posts"]
                if normalize_post_url(item["url"]) != url
            ]
            save_baseline_state(baseline_state, baseline_filepath)
            continue
        if not (
            is_first_start_delivery
            or is_baseline_initial_delivery
            or post.get("confirmed_new_post")
        ):
            logging.warning(
                "Deferring pending post without verified new-publication state."
            )
            continue
        sent_ok = await send_post_to_telegram(bot, chat_id, post)
        if sent_ok:
            if is_first_start_delivery:
                baseline_state["first_start_post_sent"] = True
                historical = list(baseline_state["historical_urls"])
                historical_set = {normalize_post_url(item) for item in historical}
                if url not in historical_set:
                    historical.append(url)
                baseline_state["historical_urls"] = historical
                baseline_state["pending_posts"] = [
                    item for item in baseline_state["pending_posts"]
                    if normalize_post_url(item["url"]) != url
                ]
                try:
                    save_baseline_state(baseline_state, baseline_filepath)
                except Exception as err:
                    raise FatalStatePersistenceError(
                        "Unable to persist first /start delivery state to "
                        f"{baseline_filepath}: {err}"
                    ) from err
            _commit_delivered_url(seen, url, filepath)
            seen_set.add(url)
            if not is_first_start_delivery:
                baseline_state["pending_posts"] = [
                    item for item in baseline_state["pending_posts"]
                    if normalize_post_url(item["url"]) != url
                ]
                save_baseline_state(baseline_state, baseline_filepath)
            new_sent_count += 1
            logging.info(f"Sent notification and saved post ID: {url}")
            await asyncio.sleep(1)
        else:
            logging.error(
                "Failed to send post notification: %s. Post remains pending for retry.",
                url,
            )

    if is_first_scan:
        if is_telegram_destination_configured(bot, chat_id):
            if send_initial_batch_on_start and new_sent_count > 0:
                summary_text = (
                    f"ℹ️ انتهى الفحص الأول مع الإرسال الأولي. "
                    f"تم إرسال {new_sent_count} من أصل منشور واحد. "
                    "سيتم متابعة المنشورات الجديدة لاحقًا."
                )
            else:
                summary_text = (
                    f"ℹ️ انتهى الفحص الأول. "
                    f"تم تسجيل {len(posts)} منشورًا حاليًا. "
                    "سيتم إرسال المنشورات المكتشفة لاحقًا."
                )
            await send_telegram_message_with_retry(bot, chat_id, summary_text)

        if send_initial_batch_on_start:
            logging.info(
                "First scan baseline completed. Detected %s post(s), sent %s in initial batch.",
                detected_count,
                new_sent_count,
            )
        else:
            logging.info("First scan complete. Detected %s post(s), sent 0.", detected_count)

    return detected_count, new_sent_count


async def main():
    load_dotenv()
    token = os.getenv("BOT_TOKEN")
    raw_chat_id = os.getenv("CHAT_ID")

    if not token or not raw_chat_id or not str(raw_chat_id).strip():
        raise RuntimeError("BOT_TOKEN or CHAT_ID is missing from .env")

    chat_id = parse_chat_id(raw_chat_id)

    try:
        seen = load_seen(SEEN_FILE)
        baseline_state = load_baseline_state(BASELINE_FILE)
    except FatalStatePersistenceError as err:
        logging.critical(
            "Fatal state persistence error; stopping monitoring: %s",
            err,
        )
        raise
    except StateFileError as err:
        logging.critical("Refusing to start with unreadable state: %s", err)
        raise

    logging.info(f"Loaded {len(seen)} previously seen post(s) from {SEEN_FILE}")

    # Persist normalized forms of already-recorded URLs without dropping any.
    if seen:
        save_seen(seen, SEEN_FILE)

    # Ambiguous legacy/completed state is migrated on the first successful
    # extraction so currently visible unrecorded posts are not sent as new.
    # See apply_legacy_seen_migration() for the documented trade-off.

    app = (
        ApplicationBuilder()
        .token(token)
        .connect_timeout(20)
        .read_timeout(30)
        .write_timeout(20)
        .build()
    )
    if app.updater is None:
        raise RuntimeError("Telegram updater is unavailable")

    scan_requested = asyncio.Event()

    async def start_command(update, context):
        logging.info("Telegram /start command received.")
        logging.info("Immediate Facebook scan requested by /start.")
        scan_requested.set()
        await send_telegram_message_with_retry(
            app.bot,
            chat_id,
            "⏳ تم استلام الطلب، جارٍ جلب أحدث منشور من صفحة الكلية.",
        )

    app.add_handler(CommandHandler("start", start_command))

    async with app:
        await send_telegram_message_with_retry(
            app.bot,
            chat_id,
            "🟢 بدأت مراقبة صفحة الكلية.",
        )

        await app.start()
        try:
            await app.updater.start_polling()
            async with async_playwright() as p:
                browser = await p.chromium.launch(headless=True)
                page = None
                start_command_scan = False
                try:
                    while True:
                        run_start_command_scan = start_command_scan
                        start_command_scan = False
                        try:
                            if page is None or page.is_closed():
                                page = await browser.new_page(locale="fr-FR")

                            posts = await extract_posts(page)
                            logging.info(f"Unique posts extracted: {len(posts)}")

                            do_send_initial_batch = not baseline_state["baseline_completed"]

                            detected_count, sent_count = await process_posts_scan(
                                posts=posts,
                                seen=seen,
                                bot=app.bot,
                                chat_id=chat_id,
                                filepath=SEEN_FILE,
                                baseline_filepath=BASELINE_FILE,
                                baseline_state=baseline_state,
                                send_initial_batch_on_start=do_send_initial_batch,
                                send_first_start_post=run_start_command_scan,
                            )
                            if run_start_command_scan and not detected_count:
                                await send_telegram_message_with_retry(
                                    app.bot,
                                    chat_id,
                                    "⚠️ لم أتمكن من استخراج أي منشور من صفحة فيسبوك. "
                                    "تحقق من سجل التشغيل أو حاول مجددًا لاحقًا.",
                                )
                            elif run_start_command_scan and not sent_count:
                                await send_telegram_message_with_retry(
                                    app.bot,
                                    chat_id,
                                    "⚠️ لم يؤكد Telegram إرسال أحدث منشور. "
                                    "سيُعاد المحاولة في الفحص التالي.",
                                )

                        except FatalStatePersistenceError as err:
                            logging.critical(
                                "Fatal state persistence error; stopping monitoring: %s",
                                err,
                            )
                            raise
                        except StateFileError as err:
                            logging.critical(
                                "State file error; stopping processing without overwrite: %s",
                                err,
                            )
                            raise
                        except Exception as error:
                            logging.error(f"Check failed: {type(error).__name__}: {error}")
                            if run_start_command_scan:
                                await send_telegram_message_with_retry(
                                    app.bot,
                                    chat_id,
                                    "⚠️ تعذر جلب أحدث منشور من Facebook. "
                                    "تحقق من سجل تشغيل البوت.",
                                )
                            if page and not page.is_closed():
                                try:
                                    await page.close()
                                except Exception:
                                    pass
                                page = None

                        logging.info(f"Next check in {CHECK_INTERVAL // 60} minutes.")
                        try:
                            await asyncio.wait_for(
                                scan_requested.wait(),
                                timeout=CHECK_INTERVAL,
                            )
                        except asyncio.TimeoutError:
                            pass
                        else:
                            scan_requested.clear()
                            start_command_scan = True

                finally:
                    if page and not page.is_closed():
                        try:
                            await page.close()
                        except Exception:
                            pass
                    try:
                        await browser.close()
                    except Exception as error:
                        logging.warning(f"Browser cleanup warning: {error}")
        finally:
            try:
                if app.updater.running:
                    await app.updater.stop()
            finally:
                if app.running:
                    await app.stop()


if __name__ == "__main__":
    asyncio.run(main())
