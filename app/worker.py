import asyncio
import logging
import os
import sys
from datetime import date

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s %(levelname)s %(name)s — %(message)s",
    stream=sys.stdout,
)
logger = logging.getLogger(__name__)

logger.info("=== EquityBuddy worker starting ===")
logger.info("Python %s", sys.version)
logger.info("Working dir: %s", os.getcwd())

try:
    from yahooquery import Ticker as YQTicker
    logger.info("yahooquery OK")
except Exception as e:
    logger.error("IMPORT FAIL yahooquery: %s", e)

try:
    from google import genai  # noqa: F401
    logger.info("google-genai OK")
except Exception as e:
    logger.error("IMPORT FAIL google-genai: %s", e)

try:
    from apscheduler.schedulers.blocking import BlockingScheduler
    from apscheduler.triggers.cron import CronTrigger
    from apscheduler.triggers.interval import IntervalTrigger
    logger.info("apscheduler OK")
except Exception as e:
    logger.error("IMPORT FAIL apscheduler: %s", e)
    raise

from . import database as db
from . import alerts, email_report, enrichment, extractor, scraper
from .config import get as get_config, load_config


def backfill_price_history(ticker: str, awareness_price: float | None) -> int:
    """Fetch up to 90 days of historical closes and populate price_history."""
    try:
        yq = YQTicker(ticker)
        hist = yq.history(period="3mo")
        if hist is None or hist.empty:
            logger.warning("No history returned for %s", ticker)
            return 0

        import pandas as pd
        if isinstance(hist.index, pd.MultiIndex):
            try:
                hist = hist.xs(ticker, level=0)
            except KeyError:
                hist = hist.xs(ticker.upper(), level=0)

        count = 0
        for date_idx, row in hist.iterrows():
            date_str = date_idx.strftime("%Y-%m-%d") if hasattr(date_idx, "strftime") else str(date_idx)[:10]
            close = float(row.get("close") or row.get("Close") or 0)
            if close <= 0:
                continue
            volume = int(row.get("volume") or row.get("Volume") or 0) or None
            pct = None
            if awareness_price and awareness_price > 0:
                pct = round((close - awareness_price) / awareness_price * 100, 2)
            db.insert_price_history(ticker, close, pct, volume, date_str=date_str)
            count += 1

        logger.info("Backfilled %d days of price history for %s", count, ticker)
        return count
    except Exception as e:
        logger.warning("Price backfill failed for %s: %s", ticker, e)
        return 0


def update_watchlist_prices() -> None:
    stocks = db.get_active_watchlist_stocks()
    if not stocks:
        return

    tickers = list({s["ticker"] for s in stocks})
    prices: dict[str, float] = {}
    volumes: dict[str, int] = {}

    try:
        yq = YQTicker(tickers)
        quotes = yq.price
        for ticker in tickers:
            data = quotes.get(ticker, {})
            if isinstance(data, dict) and "regularMarketPrice" in data:
                prices[ticker] = float(data["regularMarketPrice"])
                volumes[ticker] = int(data.get("regularMarketVolume") or 0) or None
            else:
                logger.warning("No price data for %s: %s", ticker, data)
    except Exception as e:
        logger.error("Batch price fetch failed: %s", e)

    logger.info("Fetched prices for %d/%d tickers", len(prices), len(tickers))

    for stock in stocks:
        ticker = stock["ticker"]
        if ticker not in prices:
            continue
        current = prices[ticker]
        awareness = stock.get("awareness_price")
        pct = None
        if awareness and awareness > 0:
            pct = round((current - awareness) / awareness * 100, 2)
        db.insert_price_history(ticker, current, pct, volumes.get(ticker))

    alerts.check_and_send_price_milestones()


async def run_full_cycle() -> dict:
    import traceback as _tb
    logger.info("Starting scrape+extract+enrich cycle")

    cookie_status = scraper.get_cookie_status()
    if cookie_status["status"] in ("expired", "expiring", "missing"):
        logger.warning("Twitter cookies: %s", cookie_status["message"])

    logger.info("STEP 1: scraping")
    scrape_result = await scraper.run_scrape()
    logger.info("Scrape complete: %s", scrape_result)

    logger.info("STEP 2: extraction")
    try:
        extraction_count = extractor.run_extraction()
        logger.info("Extracted %d ideas", extraction_count)
    except Exception as e:
        logger.error("STEP 2 FAILED: %s", e)
        for line in _tb.format_exc().splitlines():
            logger.error("  %s", line)
        extraction_count = 0

    logger.info("STEP 3: enrichment")
    try:
        new_ideas = db.get_ideas_since(hours=2)
        logger.info("Got %d recent ideas for enrichment", len(new_ideas))
        enrichment_count = enrichment.run_enrichment(new_ideas)
        logger.info("Enriched %d tickers", enrichment_count)
    except Exception as e:
        logger.error("STEP 3 FAILED: %s", e)
        for line in _tb.format_exc().splitlines():
            logger.error("  %s", line)
        new_ideas = []
        enrichment_count = 0

    logger.info("STEP 4: alerts")
    try:
        alerts_sent = alerts.send_idea_alerts_for_new_ideas(new_ideas)
        logger.info("Sent %d idea alerts", alerts_sent)
    except Exception as e:
        logger.error("STEP 4 FAILED: %s", e)
        for line in _tb.format_exc().splitlines():
            logger.error("  %s", line)
        alerts_sent = 0

    logger.info("STEP 5: watchlist prices")
    try:
        update_watchlist_prices()
        logger.info("Watchlist prices updated")
    except Exception as e:
        logger.error("STEP 5 FAILED: %s", e)
        for line in _tb.format_exc().splitlines():
            logger.error("  %s", line)

    return {
        "scrape": scrape_result,
        "ideas_extracted": extraction_count,
        "enriched": enrichment_count,
        "alerts_sent": alerts_sent,
    }


def run_cycle_sync() -> None:
    import traceback as _tb
    try:
        asyncio.run(run_full_cycle())
    except Exception as e:
        logger.error("Cycle failed: %s", e)
        for line in _tb.format_exc().splitlines():
            logger.error("  %s", line)


def run_digest() -> None:
    try:
        email_report.generate_and_send_digest()
    except Exception as e:
        logger.error("Digest failed: %s", e)


def main() -> None:
    load_config()
    db.init_db()

    cfg = get_config()
    interval_hours = cfg["schedule"].get("scrape_interval_hours", 2)
    digest_time = cfg["schedule"].get("digest_time", "07:00")
    tz = cfg["schedule"].get("timezone", "America/Chicago")

    digest_hour, digest_minute = map(int, digest_time.split(":"))

    scheduler = BlockingScheduler(timezone=tz)

    scheduler.add_job(
        run_cycle_sync,
        trigger=IntervalTrigger(hours=interval_hours),
        id="scrape_cycle",
        replace_existing=True,
        misfire_grace_time=300,
    )

    scheduler.add_job(
        run_digest,
        trigger=CronTrigger(hour=digest_hour, minute=digest_minute, timezone=tz),
        id="daily_digest",
        replace_existing=True,
        misfire_grace_time=300,
    )

    logger.info(
        "Worker started — scraping every %dh, digest at %s %s",
        interval_hours, digest_time, tz,
    )

    run_cycle_sync()

    try:
        scheduler.start()
    except (KeyboardInterrupt, SystemExit):
        logger.info("Worker stopped")


if __name__ == "__main__":
    main()
