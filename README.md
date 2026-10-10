# FMCS Facebook Announcements Telegram Bot 📢

A Python bot that monitors public announcements on the Facebook page of the **Faculty of Mathematics and Computer Science (FMCS)** at Université Mohamed Bachir El Ibrahimi, Bordj Bou Arreridj, Algeria ([facebook.com/fmcs.univbba](https://www.facebook.com/fmcs.univbba)) and forwards new posts directly to a Telegram chat, group, or channel.

---

## 🌟 Features

- **Public Access Only**: Scrapes public Facebook page content using headless Chromium (Playwright) without requiring Facebook accounts or API credentials.
- **URL Normalization**: Strips tracking parameters (`__cft__`, `__tn__`, `mibextid`) to prevent duplicate post alerts.
- **Atomic State Persistence**: Tracks seen post IDs safely in `seen_posts.json` only after Telegram confirms successful message delivery.
- **Retryable Deliveries**: Persists failed post payloads in `baseline_state.json` and retries them even after they leave the current scrape window; invalid state data stops processing instead of being treated as empty.
- **Conservative History Migration**: For completed legacy state without a migration marker, currently visible posts not already delivered or pending are recorded as historical once. Since old state has no reliable post ages, this avoids repost floods but can skip a genuinely new post visible during that migration scan; persisted pending deliveries are preserved.
- **Resilient Polling**: Includes exponential retries for Telegram sending and handles temporary Facebook timeouts without crashing.
- **Latest Post on `/start`**: Immediately scans the Facebook page and sends its latest post whenever `/start` is requested, with a Telegram status if Facebook returns no extractable posts.
- **Message Formatting**: Truncates long text cleanly to fit Telegram's 4096 character limit without breaking links.

---

## 🛠️ Setup Instructions (Windows 11)

### 1. Prerequisites
- Python 3.10+ (Tested on Python 3.14)
- Git

### 2. Virtual Environment & Dependencies Setup
```powershell
# Clone or navigate to project directory
cd fmcs-l3-bot

# Create virtual environment if not created
python -m venv .venv

# Activate virtual environment
.\.venv\Scripts\Activate.ps1

# Install required Python packages
pip install -r requirements.txt
```

### 3. Install Playwright Chromium Browser
```powershell
playwright install chromium
```

### 4. Configure Environment Variables
Copy `.env.example` to `.env` and fill in your secrets:
```powershell
copy .env.example .env
```
Edit `.env`:
```env
BOT_TOKEN=123456789:ABCdefGHIjklMNOpqrsTUVwxyZ
CHAT_ID=123456789
```

---

## 🧪 Running Unit Tests

Run the test suite to verify URL normalization, link validation, baseline logic, and error handling:
```powershell
python test_monitor.py
```

---

## 🚀 Running the Bot

To start the Facebook monitoring service:
```powershell
python monitor.py
```

## 🐳 Run with Docker

Install Docker Desktop and start its Linux container engine. Configure `.env` as described above, then build the image and start the bot:

```powershell
docker compose up --build -d
```

View the bot logs or stop it with:

```powershell
docker compose logs -f
docker compose down
```

The Compose setup stores `seen_posts.json` and `baseline_state.json` in the local `bot-data` folder, so post history survives container recreation. When moving an existing installation into Docker, copy its state files into `bot-data` before the first start to preserve its history.

---

## ☁️ Deployment & Free Hosting Options

To run the bot 24/7 without keeping your local computer turned on, consider the following hosting options:

| Provider | Free Tier / Specs | Chromium Support | Persistence / Restart | Recommendation |
| :--- | :--- | :--- | :--- | :--- |
| **Render** | Free Web Service / Background Worker (512 MB RAM, sleeps after inactivity on web) | Docker container required for Playwright | Requires disk mount or database for `seen_posts.json` | ⚠️ Good, but memory (512MB) can be tight for Playwright Chromium. |
| **Railway** | \$5 trial credit / usage-based | Supported via Docker | Volume mounts supported | 👍 High performance and easy setup. |
| **Fly.io** | Pay-as-you-go / Low cost | Supported via Docker | Persistent volumes supported | 👍 Great reliability for background daemons. |
| **Oracle Cloud Free Tier** | Always Free VM (Up to 4 Ampere ARM cores / 24 GB RAM) | Supported natively or via Docker | Full OS root access, 24/7 continuous uptime | 🌟 **Best Free Option** (Unlimited uptime, ample RAM for Chromium). |

### Deployment Best Practices
1. **Docker Containerization**: Build the included `Dockerfile`; it installs Chromium and its system dependencies for Playwright.
2. **Persistent Storage**: Mount `seen_posts.json` to a persistent Docker volume or cloud volume so post history is preserved across process restarts.
3. **Process Supervision**: Use `systemd` or Docker restart policy (`restart: unless-stopped`) to ensure automatic recovery after server reboots.

---

## 👥 Adding Group / Channel Support for Classmates

To allow classmates to receive announcements:
1. Create a **Telegram Group** or **Channel**.
2. Add your bot as a member (or Administrator for Channels).
3. Update `CHAT_ID` in `.env`:
   - For Groups: Use the numeric group ID (e.g., `-100123456789`). Obtain via `get_chat_id.py` after sending a message in the group.
   - For Public Channels: Use the channel username (e.g., `@fmcs_announcements`).
