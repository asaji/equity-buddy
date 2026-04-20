import json
import logging
import time
from typing import Optional

import anthropic
import yfinance as yf

from . import database as db
from .config import get as get_config

logger = logging.getLogger(__name__)

RESEARCH_PROMPT = """You are a buy-side equity analyst. Write a 4-5 sentence research summary for {ticker}.

Cover:
1. What the company does (one sentence)
2. The current investment thesis — why investors are interested now
3. Whether this opportunity appears early-stage or already widely followed
4. Key risk or counterargument

Be direct and opinionated. Use current market context.
Ticker: {ticker}
Current price: ${price}
3-month change: {pct_3m}%
Market cap: ${market_cap_b}B
Freshness signal: {freshness}"""


def _fetch_yfinance(ticker: str) -> Optional[dict]:
    try:
        t = yf.Ticker(ticker)
        info = t.info or {}

        hist_3m = t.history(period="3mo")
        hist_1m = t.history(period="1mo")
        hist_1w = t.history(period="5d")

        def pct_change(hist):
            if hist is None or len(hist) < 2:
                return None
            start = hist["Close"].iloc[0]
            end = hist["Close"].iloc[-1]
            if start == 0:
                return None
            return round((end - start) / start * 100, 2)

        current_price = info.get("currentPrice") or info.get("regularMarketPrice")
        week52_high = info.get("fiftyTwoWeekHigh")
        week52_low = info.get("fiftyTwoWeekLow")
        volume = info.get("regularMarketVolume")
        avg_volume = info.get("averageVolume")
        market_cap = info.get("marketCap")
        pe_ratio = info.get("trailingPE")
        revenue_growth = info.get("revenueGrowth")
        short_interest = info.get("shortPercentOfFloat")

        p3m = pct_change(hist_3m)
        p1m = pct_change(hist_1m)
        p1w = pct_change(hist_1w)

        freshness = "in_motion"
        if p3m is not None and week52_high:
            if p3m < 15 and current_price and current_price < week52_high * 0.7:
                freshness = "early"
            elif p3m > 30 or (current_price and current_price > week52_high * 0.9):
                freshness = "may_have_moved"

        return {
            "current_price": current_price,
            "week52_high": week52_high,
            "week52_low": week52_low,
            "pct_change_1w": p1w,
            "pct_change_1m": p1m,
            "pct_change_3m": p3m,
            "volume": volume,
            "avg_volume_30d": avg_volume,
            "market_cap": market_cap,
            "pe_ratio": pe_ratio,
            "revenue_growth_yoy": revenue_growth,
            "short_interest": short_interest,
            "freshness_signal": freshness,
        }
    except Exception as e:
        logger.warning("yfinance error for %s: %s", ticker, e)
        return None


def _fetch_research_summary(client: anthropic.Anthropic, ticker: str, data: dict,
                             max_retries: int = 3) -> Optional[str]:
    price = data.get("current_price") or 0
    pct_3m = data.get("pct_change_3m") or 0
    market_cap = (data.get("market_cap") or 0) / 1e9
    freshness = data.get("freshness_signal", "unknown")

    prompt = RESEARCH_PROMPT.format(
        ticker=ticker,
        price=f"{price:.2f}",
        pct_3m=f"{pct_3m:+.1f}",
        market_cap_b=f"{market_cap:.1f}",
        freshness=freshness,
    )

    for attempt in range(max_retries):
        try:
            response = client.messages.create(
                model="claude-sonnet-4-6",
                max_tokens=1024,
                tools=[{"type": "web_search_20250305", "name": "web_search"}],
                messages=[{"role": "user", "content": prompt}],
            )
            for block in response.content:
                if hasattr(block, "text"):
                    return block.text.strip()
            return None
        except anthropic.RateLimitError as e:
            wait = 2 ** attempt * 5
            logger.warning("Rate limit, retrying research for %s in %ds: %s", ticker, wait, e)
            time.sleep(wait)
        except anthropic.APIError as e:
            wait = 2 ** attempt * 2
            logger.warning("API error for %s research, retrying in %ds: %s", ticker, wait, e)
            time.sleep(wait)
    return None


def enrich_ticker(ticker: str) -> Optional[dict]:
    existing = db.get_enrichment_today(ticker)
    if existing:
        logger.debug("Enrichment already done today for %s", ticker)
        return existing

    logger.info("Enriching %s", ticker)
    data = _fetch_yfinance(ticker)
    if not data:
        logger.warning("No yfinance data for %s — skipping enrichment", ticker)
        return None

    cfg = get_config()
    api_key = cfg.get("anthropic_api_key", "")
    if api_key:
        client = anthropic.Anthropic(api_key=api_key)
        summary = _fetch_research_summary(client, ticker, data)
        data["research_summary"] = summary
    else:
        data["research_summary"] = None

    db.upsert_enrichment(ticker, data)
    return data


def run_enrichment(ideas: Optional[list[dict]] = None) -> int:
    if ideas is None:
        ideas = db.get_ideas_since(hours=24)

    tickers = list({idea["ticker"] for idea in ideas if idea.get("ticker")})
    if not tickers:
        logger.info("No tickers to enrich")
        return 0

    enriched = 0
    for ticker in tickers:
        result = enrich_ticker(ticker)
        if result:
            enriched += 1

    logger.info("Enriched %d/%d tickers", enriched, len(tickers))
    return enriched
