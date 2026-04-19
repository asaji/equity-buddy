# EquityBuddy

Monitors FinTwit X/Twitter accounts and Substack RSS feeds for stock investment ideas, enriches them with AI research, delivers Pushover alerts and daily email digests, and tracks stock performance over time.

## Prerequisites

- [Docker Compose Manager](https://forums.unraid.net/topic/114415-plugin-docker-compose-manager/) plugin installed via Unraid Community Applications
- Anthropic API key
- Pushover account (for alerts)
- SMTP credentials (for digest emails)
- Twitter/X account (for Twitter scraping)

## Unraid Setup

### 1. Create host directories

```bash
mkdir -p /mnt/user/appdata/equitybuddy/cookies
```

### 2. Copy and configure config.yaml

```bash
cp config.yaml /mnt/user/appdata/equitybuddy/config.yaml
```

Edit the file and fill in:
- `anthropic_api_key` — from console.anthropic.com
- `pushover.user_key` and `pushover.api_token` — from pushover.net
- `email.*` — your SMTP server credentials
- `schedule.timezone` — your local timezone (e.g. `America/Chicago`)

You can leave `accounts.twitter` and `accounts.substack` empty — they can be managed from the web UI.

### 3. Deploy via Docker Compose Manager

1. In Unraid, go to **Docker → Compose** (Docker Compose Manager plugin)
2. Click **Add New Stack**
3. Name it `equitybuddy`
4. Paste the contents of `docker-compose.yml` (or point to the file)
5. Set `PUID` and `PGID` to match your Unraid user (check with `id nobody` — defaults to 99/100)
6. Click **Compose Up**

### 4. Authenticate Twitter (first run)

Navigate to `http://<UNRAID-IP>:7842/auth/twitter` and enter your Twitter username, email, and password. This generates session cookies saved to `/mnt/user/appdata/equitybuddy/cookies/twitter_cookies.json`. You only need to do this once (or if cookies expire).

### 5. Add sources

Go to `http://<UNRAID-IP>:7842/watchlist` and add:
- Twitter handles (e.g. `unusual_whales`, `TihoBrkan`)
- Substack RSS URLs (e.g. `https://example.substack.com/feed`)

### 6. Access the web UI

```
http://<UNRAID-IP>:7842
```

Find your Unraid IP in the Unraid dashboard under **Info**, or run `ip addr` in the Unraid terminal.

## How it works

| Component | What it does |
|-----------|-------------|
| **worker** | Scrapes sources every N hours, extracts tickers via Claude, enriches with yfinance + Claude research, sends Pushover alerts, generates daily email digest |
| **web** | FastAPI UI for viewing ideas, reports, alerts, managing sources and watchlist |
| **SQLite** | Shared via Docker named volume at `/app/data/equitybuddy.db` |

## Manual scrape

Click **Run Now** on the dashboard or settings page to trigger an immediate scrape + extract + enrich cycle without waiting for the schedule.

## Troubleshooting

**Twitter auth fails**
- Make sure you're using the correct username (not email) in the Username field
- Twitter may challenge with a verification code — check your email or phone
- If twikit raises a `TooManyRequests` error, wait a few minutes and retry
- Cookies expire periodically — re-authenticate at `/auth/twitter` if scraping stops working

**No ideas appearing**
- Check container logs: `docker logs equitybuddy-worker`
- Confirm `anthropic_api_key` is set in config.yaml
- Confirm at least one Twitter or Substack source is added
- Click Run Now to trigger manually

**Emails not sending**
- Verify SMTP credentials in config.yaml
- For Gmail, use an App Password (not your account password)
- Check that port 587 is not blocked by your network

**Price data missing**
- yfinance may throttle or fail for some tickers — check worker logs
- OTC/pink sheet tickers may not be available

## File layout

```
equitybuddy/
  docker-compose.yml     # Unraid deployment
  Dockerfile             # Python 3.11 image
  entrypoint.sh          # PUID/PGID handler
  requirements.txt
  config.yaml            # Template — copy to appdata
  app/
    main.py              # FastAPI web app
    worker.py            # APScheduler background worker
    scraper.py           # twikit + feedparser
    extractor.py         # Claude ticker extraction
    enrichment.py        # yfinance + Claude research
    alerts.py            # Pushover notifications
    email_report.py      # HTML digest + SMTP
    database.py          # SQLite schema + all queries
    config.py            # config.yaml loader
  templates/
    base.html            # Nav + shared layout
    dashboard.html       # Today's ideas
    reports.html         # Past digests
    alerts.html          # Alert log
    watchlist.html       # Source management
    watchlist_stocks.html # Performance tracker
```
