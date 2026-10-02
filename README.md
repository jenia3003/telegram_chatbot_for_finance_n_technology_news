# Telegram News Bot

Telegram chatbot built with Python to collect, filter, and summarize finance, market, technology, and Data/AI job-market news. It uses RSS/HTML from controlled sources, SQLite for persistence, and the Gemini API for classification and summarization.

## Author and development assistance

This project was created and developed by the repository owner. Codex model GPT-5.6 Luna was used as a supporting tool during the design, implementation, testing, and documentation process.

## Features

- On-demand local operation; the Telegram mobile app does not need to stay open.
- Per-chat and per-command checkpoints for incremental news retrieval.
- Time windows covering 24 hours, multiple days, weeks, or one month.
- SHA-256 URL hashing to prevent duplicate articles.
- Domain reliability scores from 1 to 10; the default minimum is 7.
- JSON-validated summaries that preserve important figures, dates, entities, and developments.
- A link to the complete original article; URLs are stored in `articles.url`.
- Data/AI job-market snapshots from ITviec, CareerViet, Glints, and TopDev.
- SQLite timestamps stored in Vietnam time: `YYYY-MM-DD HH:MM:SS+07:00`.

## Telegram commands

Command names use lowercase ASCII. When a time window is supplied, put one space between the command and its argument; the argument itself must contain no spaces:

```text
/start
/help
/news
/news 24h
/news 2ngay
/news 5ngay
/news 1tuan
/news 1thang
/tech 24h
/finance 3ngay
```

`/news` returns a general digest. `/tech` filters for Technology. `/finance` filters for Finance. The first call without a time argument looks back 24 hours; later calls return articles after the checkpoint for that chat and command.

News is collected only when `/news`, `/tech`, or `/finance` is called.

## Sources and filtering

- `sources.json`: fixed endpoints for domestic and international news, market data, and official company announcements.
- `source_catalog.json`: approved domains, important entities, and discovery queries.
- Discovery accepts only approved domains that meet the reliability threshold; new domains are stored in `discovery_candidates` for manual review.
- LinkedIn, Indeed, forums, and social networks are currently not collected automatically.

## Environment setup

Requirements: Python 3.14+ and optionally Git.

### Windows PowerShell

```powershell
cd C:\path\to\your-repository
py -3.14 -m venv venv
.\venv\Scripts\python.exe -m pip install -r requirements.txt
Copy-Item .env.example .env
```

### Linux/Ubuntu

```bash
cd /path/to/your-repository
python3 -m venv venv
./venv/bin/python -m pip install -r requirements.txt
cp .env.example .env
chmod 600 .env
```

## Get a Telegram bot token

1. Open Telegram and find the official [@BotFather](https://t.me/BotFather).
2. Send `/newbot`.
3. Enter a display name for the bot.
4. Enter a username ending in `bot`, for example `my_news_bot`.
5. Copy the token returned by BotFather.

The token provides full control over the bot and must be kept private. If it is exposed, use `/revoke` in BotFather to revoke it and create a replacement. See [Telegram Bot Introduction](https://core.telegram.org/bots).

## Get a Gemini API key

1. Open [Google AI Studio API Keys](https://aistudio.google.com/app/apikey).
2. Sign in with Google.
3. Select an existing project or create a new one.
4. Click **Create API key** and copy the key.
5. Restrict the key to the Gemini API/Generative Language API when the interface offers that option.

Google recommends storing the key in an environment variable instead of source code. See the [Gemini API key guide](https://ai.google.dev/gemini-api/docs/api-key). The Free Tier has request and token limits; this bot batches multiple articles into one request to reduce usage.

## Create `.env`

Open the newly created `.env` file and replace the sample values. Do not include angle brackets:

```env
TELEGRAM_BOT_TOKEN=token_from_BotFather
GEMINI_API_KEY=api_key_from_Google_AI_Studio
GEMINI_MODEL=gemini-3.1-flash-lite
GEMINI_BATCH_SIZE=8
GEMINI_EXCERPT_CHARS=3000
SOURCES_PATH=sources.json
SOURCE_CATALOG_PATH=source_catalog.json
MIN_RELIABILITY_SCORE=7
DISCOVERY_ENABLED=true
DISCOVERY_MAX_ITEMS=6
```

Never commit `.env`, send a token/API key in chat, or place secrets in `README.md`. `.gitignore` excludes `.env` from Git.

`GEMINI_API_KEY` is required for classification and summarization. Without it, the bot can collect data, but articles will not be marked `ready_to_send`.

## Run locally

Check the source code:

```powershell
.\venv\Scripts\python.exe -m py_compile bot.py
```

Start the bot:

```powershell
.\venv\Scripts\python.exe .\bot.py
```

Keep the terminal open, then send these commands in Telegram:

```text
/start
/news 24h
/tech 24h
/finance 3ngay
```

Restart the bot after changing `.env` or the source code. Run only one polling instance for a token to avoid `Conflict: terminated by other getUpdates request`.

## Gemini workflow and quota usage

The bot sends up to `GEMINI_BATCH_SIZE` articles in one request, limits the evidence sent for each article to `GEMINI_EXCERPT_CHARS` characters, and skips articles that have already been analyzed successfully. The prompt prioritizes faithful, complete summaries, preserves important figures and facts, avoids unsupported inferences, and does not provide investment recommendations.

Each result must contain:

```json
{
  "category": "Finance|Market|Technology",
  "bullet_points": ["...", "..."]
}
```

An article is marked `ready_to_send = 1` only after valid JSON is returned. If Gemini returns `429 RESOURCE_EXHAUSTED`, the current batch stops, the checkpoint is not advanced, and the bot asks you to wait and retry. See [Gemini Rate Limits](https://ai.google.dev/gemini-api/docs/rate-limits).

## SQLite

The default database is `bot_news.db`. Main tables:

- `articles`: `id`, `url_hash`, `title`, `source`, `url`, `category`, `summary`, `published_at`, `sent_to_user`, `ready_to_send`.
- `chat_checkpoints`: checkpoints by chat and command type.
- `discovery_candidates`: new domains awaiting manual approval.

Open the database with the **SQLite Viewer** extension in VS Code or with DB Browser for SQLite. Back up `bot_news.db` before making direct edits.

## Current limitations

- The bot is intentionally local-only: it works while the local Python process is running and does not send scheduled briefings.
- LinkedIn, Indeed, social-media groups, forums, and social networks are not automatically collected.
- Some job boards and news sites use JavaScript, require login, or block automated requests; the bot may not be able to create a snapshot from those pages.
- Summaries are based on the RSS/HTML content provided by each source. Always open **Read the complete original article** for the full text and context.

## Troubleshooting

### The bot does not respond

- Check that the terminal is still running `bot.py`.
- Check `TELEGRAM_BOT_TOKEN` in `.env`.
- Stop duplicate instances and start only one.

### Gemini returns `429 RESOURCE_EXHAUSTED`

- Wait about one minute before retrying.
- Do not call `/news`, `/tech`, and `/finance` repeatedly in a short period.
- Check the quota in Google AI Studio.

### The bot says there are no new articles

- Try `/news 24h`.
- Check whether `GEMINI_API_KEY` is configured.
- Check whether SQLite contains articles with `ready_to_send = 1`.
- Inspect the terminal log for RSS or Gemini errors.
