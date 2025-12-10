# 📊 Crypto Digest Bot

A powerful Telegram bot that aggregates cryptocurrency news from multiple channels, analyzes them using AI (OpenAI GPT / DeepSeek), and delivers structured digests to subscribers - as **formatted messages** or **beautiful PDF reports**.

**🤖 Try it live:** [@CryptoOwnAIDigestBot](https://t.me/CryptoOwnAIDigestBot)

---

## ✨ Features

### 📰 News Aggregation & Analysis
- **Multi-channel scraping** - fetches posts from configured Telegram channels via `snscrape`
- **AI-powered analysis** - uses OpenAI GPT models (or compatible APIs like DeepSeek) to extract insights
- **Structured output** - generates digests with:
  - 📝 **Summary** - general market context and sentiment
  - 📈 **Promising Assets** - coins with bullish/bearish signals and reasoning
  - ✅ **Positive News** - partnerships, listings, protocol updates
  - ❌ **Negative News** - hacks, delistings, regulatory pressure
  - 🌍 **Macro Events** - ETF decisions, interest rates, institutional moves
- **Priority tagging** - each item is marked as 🔥 High / ❗ Medium / 🟨 Low priority

### 📤 Delivery Modes
- **PDF Reports** - professionally styled A4 documents with sections, colors, and emojis
- **Telegram Messages** - expandable blockquotes with inline formatting
- Users choose their preferred mode via `/digest_mode`

### 📬 Support System
- **Support Tickets** - users send messages via `/support <message>`
- **Media Attachments** - users can attach photos, documents, videos within 1 minute after /support
- **Album Support** - send multiple files at once as a media group
- **Admin Replies** - admins reply to tickets (text or media) by replying to the message

### 🌐 Multi-language Support
- Interface and digests can be translated to multiple languages
- Translations are cached in the database to avoid repeated API calls
- Users select language via `/language` command
- Configure allowed languages in `.env`

### 💳 Subscription & Monetization
- **Telegram Stars** integration for payments
- **Free trial** - one free digest for new users
- **Monthly subscriptions** - configurable price and duration
- **Balance top-up** - users can add Stars via `/topup`
- **Renewal reminders** - automatic notifications before subscription expires

### 📅 Scheduled Jobs
- **Auto-digest** - broadcasts to all eligible users at configurable intervals
- **Report aggregation** - automatically generates weekly, monthly, and annual summaries
- **Missed report recovery** - detects downtime and catches up on missed digests
- **PDF cleanup** - removes old generated files to save disk space

### 🎄 Holiday Themes
- Automatic emoji decorations for holidays (New Year, Halloween, Valentine's Day)
- Customizable date ranges in `holidays_manager.py`

---

## 📋 Requirements

- **Python 3.10+**
- **Telegram Bot Token** - get it from [@BotFather](https://t.me/BotFather)
- **OpenAI API Key** - or compatible API (DeepSeek, etc.)
- **WeasyPrint dependencies** - for PDF generation (see installation notes below)

---

## 🚀 Installation

### 1. Clone the Repository

```bash
git clone https://github.com/ButterDevelop/crypto_digest_bot.git
cd crypto_digest_bot
```

### 2. Create Virtual Environment

```bash
python -m venv .venv

# Windows
.venv\Scripts\activate

# Linux / macOS
source .venv/bin/activate
```

### 3. Install Python Dependencies

```bash
pip install -r requirements.txt
```

### 4. Install System Dependencies (for PDF generation)

**WeasyPrint** requires some system libraries. Follow the [official installation guide](https://doc.courtbouillon.org/weasyprint/stable/first_steps.html#installation).

#### Windows
WeasyPrint should work out of the box after `pip install`.

#### Ubuntu / Debian
```bash
sudo apt-get install libpango-1.0-0 libpangocairo-1.0-0 libgdk-pixbuf2.0-0 libffi-dev shared-mime-info
```

#### macOS
```bash
brew install pango libffi
```

### 5. Install Emoji Font (Linux only)

For emoji support in PDF reports on Linux, install the Noto Color Emoji font:

```bash
sudo apt-get install fonts-noto-color-emoji
```

> ⚠️ Without this package, emojis in PDF files will appear as empty boxes on Linux systems.

---

## ⚙️ Configuration

### 1. Create Environment File

```bash
cp .env.template .env
```

### 2. Edit `.env`

```ini
# === Required ===
TELEGRAM_BOT_TOKEN=123456789:AAxxxxxxxxxxxxxxxxxxxxxxxx
OPENAI_API_KEY=sk-xxxxxxxxxxxxxxxxxxxxxxxx

# === Channels to Monitor ===
# Comma-separated usernames (without @)
CHANNELS=crypto_news,bitcoin_updates,defi_daily

# === Scraping Settings ===
MAX_HOURS=12                   # How far back to look for posts
MAX_POSTS_PER_CHANNEL=40       # Max posts to fetch per channel

# === AI Model Configuration ===
# Provider can be "openai" (default) or "deepseek"
LLM_PROVIDER=openai

# --- OpenAI Settings ---
OPENAI_MODEL=gpt-4.1-mini

# --- DeepSeek Settings ---
# Required only if LLM_PROVIDER=deepseek
DEEPSEEK_API_KEY=sk-xxxxxxxxxxxxxxxxxxxxxxxx
DEEPSEEK_BASE_URL=https://api.deepseek.com
DEEPSEEK_MODEL=deepseek-chat

# === Optional: Proxy for OpenAI ===
# OPENAI_PROXY=http://127.0.0.1:8080
# OPENAI_PROXY=http://user:pass@proxy.com:3128

# === Digest Schedule ===
DIGEST_INTERVAL_MINUTES=720    # Default: every 12 hours
DIGEST_START_HOUR=4            # UTC hour (0-23) - digest grid alignment

# Specific launch times (UTC). If enabled, valid times override DIGEST_INTERVAL_MINUTES.
# DIGEST_LAUNCH_TIMES=08:00,16:00,23:59

# === Subscription Pricing ===
SUBSCRIPTION_PRICE_STARS=10    # Cost in Telegram Stars
SUBSCRIPTION_PERIOD_DAYS=30    # Subscription duration
RENEWAL_REMINDER_DAYS=3        # Days before expiry to remind

# === Database ===
DB_PATH=bot.db

# === Admin Setup ===
# Your Telegram user ID - you'll get admin rights automatically
# INITIAL_ADMIN_ID=123456789

# === Localization ===
# Allowed language codes (comma-separated)
ALLOWED_LANGUAGES=en,ru,uk,de,fr,es,pt,zh,ja,ko
```

---

## ▶️ Running the Bot

```bash
python bot.py
```

The bot will:
1. Initialize the SQLite database
2. Start the auto-digest job (broadcasts every `DIGEST_INTERVAL_MINUTES`)
3. Schedule subscription reminders (daily check)
4. Schedule weekly/monthly report aggregation
5. Start listening for Telegram commands

---

## 📱 Bot Commands

### User Commands

| Command | Description |
|---------|-------------|
| `/start` | Welcome message with bot info and current status |
| `/status` | Your subscription status, balance, role, and next digest time |
| `/subscribe` | Activate or extend subscription using your Stars balance |
| `/topup [amount]` | Create a Telegram Stars invoice to top up balance |
| `/digest` | Request an immediate personal digest (uses free trial or subscription) |
| `/digest_mode` | Switch between PDF and message delivery |
| `/language` | Change interface and digest language |
| `/support <message>` | Send a support message (attach media within 1 min or immediately) |

### Admin Commands

| Command | Description |
|---------|-------------|
| `/digest` | **Broadcast** digest to all eligible users (admin version) |
| `/start_digest` | Start/restart the auto-digest job |
| `/stop_digest` | Stop the auto-digest job |
| `/auto_digest <minutes>` | Change the global digest interval |
| `/add_stars <user_id> <amount>` | Manually add Stars to a user's balance |
| `/rebuild_last_digest` | Resend the last digest (from cached data) |
| `/stats` | View user statistics and language distribution |

---

## 📁 Project Structure

```
crypto_digest_bot/
├── bot.py                 # Main entry point, handlers, jobs
├── config.py              # Environment configuration loader
├── db.py                  # SQLite database (users, reports, translations)
├── news_fetcher.py        # Telegram channel scraper (snscrape)
├── news_analyzer.py       # OpenAI integration, digest generation
├── pdf_renderer.py        # WeasyPrint PDF generation
├── report_aggregator.py   # Weekly/monthly/annual report aggregation
├── translator.py          # AI-powered translation with caching
├── holidays_manager.py    # Holiday detection and emoji decoration
├── ai_client.py           # OpenAI client wrapper (with proxy support)
├── requirements.txt       # Python dependencies
├── .env.template          # Environment variable template
└── bot.db                 # SQLite database (created on first run)
```

---

## 🗄️ Database Schema

The bot uses SQLite with three main tables:

### `users`
Stores user data: subscription status, balance, language preference, delivery mode.

### `reports`  
Stores generated digests (daily, weekly, monthly, annual) with JSON content and optional PDF paths.

### `translations`
Caches translated strings to reduce API calls.

### `support_tickets`
Stores support requests with user info, admin responses, and optional media attachments.

---

## 🔧 Customization

### Adding Holidays

Edit `holidays_manager.py` to add seasonal decorations:

```python
HOLIDAYS: List[Holiday] = [
    Holiday("New Year", "🎄", 12, 20, 1, 15),
    Holiday("Valentine's Day", "💖", 2, 13, 2, 15),
    Holiday("Halloween", "🎃", 10, 25, 11, 1),
    # Add your own:
    Holiday("Christmas", "🎅", 12, 24, 12, 26),
]
```

### Modifying PDF Styles

Edit the `CSS_STYLE` constant in `pdf_renderer.py` to customize colors, fonts, and layout.

### Using DeepSeek
The bot has native support for DeepSeek. To switch from OpenAI:

1. Set `LLM_PROVIDER=deepseek` in your `.env` file.
2. Provide your API key in `DEEPSEEK_API_KEY`.
3. (Optional) Customize `DEEPSEEK_MODEL` and `DEEPSEEK_BASE_URL` if needed.

---

## 📜 License

This project is licensed under the [GNU General Public License v3.0](LICENSE).

---

## 🤝 Contributing

Contributions are welcome! Feel free to open issues or submit pull requests.

---

## 💬 Support

If you have questions or need help, open an issue on GitHub or contact via Telegram.
