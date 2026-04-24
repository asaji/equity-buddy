import logging
import time
from typing import Optional

from google import genai
from google.genai import types
from yahooquery import Ticker as YQTicker

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



def _fetch_market_data(tickers: list[str]) -> dict[str, Optional[dict]]:
    try:
        t = YQTicker(tickers)
        summaries = t.summary_detail
        key_stats = t.key_stats
        hist = t.history(period="3mo", interval="1d")
    except Exception as e:
        logger.warning("yahooquery batch fetch error: %s", e)
        return {ticker: None for ticker in tickers}

    results = {}
    for ticker in tickers:
        try:
            info = (summaries.get(ticker) or {}) if isinstance(summaries, dict) else {}
            stats = (key_stats.get(ticker) or {}) if isinstance(key_stats, dict) else {}
            if isinstance(info, str) or isinstance(stats, str):
                results[ticker] = None
                continue

            ticker_hist = None
            if hist is not None and not hist.empty:
                try:
                    ticker_hist = hist.xs(ticker, level=0) if ticker in hist.index.get_level_values(0) else None
                except Exception:
                    ticker_hist = None

            def ph(period_days):
                if ticker_hist is None or len(ticker_hist) < 2:
                    return None
                sliced = ticker_hist.tail(period_days)
                if len(sliced) < 2:
                    return None
                start, end = sliced["close"].iloc[0], sliced["close"].iloc[-1]
                return round((end - start) / start * 100, 2) if start else None

            current_price = info.get("regularMarketPrice") or info.get("previousClose")
            week52_high = info.get("fiftyTwoWeekHigh")
            week52_low = info.get("fiftyTwoWeekLow")
            market_cap = info.get("marketCap")
            pe_ratio = info.get("trailingPE")
            volume = info.get("regularMarketVolume")
            avg_volume = info.get("averageDailyVolume3Month") or info.get("averageDailyVolume10Day")
            revenue_growth = stats.get("revenueGrowth")
            short_interest = stats.get("shortPercentOfFloat")

            p3m = ph(63)
            p1m = ph(21)
            p1w = ph(5)

            freshness = "in_motion"
            if p3m is not None and week52_high and current_price:
                if p3m < 15 and current_price < week52_high * 0.7:
                    freshness = "early"
                elif p3m > 30 or current_price > week52_high * 0.9:
                    freshness = "may_have_moved"

            results[ticker] = {
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
            logger.warning("yahooquery parse error for %s: %s", ticker, e)
            results[ticker] = None

    return results


def _fetch_research_summary(client: genai.Client, ticker: str, data: dict,
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
            response = client.models.generate_content(
                model="gemini-2.5-flash-lite",
                contents=prompt,
                config=types.GenerateContentConfig(
                    tools=[types.Tool(google_search=types.GoogleSearch())],
                    max_output_tokens=1024,
                ),
            )
            return response.text
        except Exception as e:
            msg = str(e)
            wait = 2 ** attempt * (5 if "429" in msg or "RESOURCE_EXHAUSTED" in msg else 2)
            logger.warning("Gemini error for %s research, retrying in %ds: %s", ticker, wait, e)
            time.sleep(wait)
    return None


def run_enrichment(ideas: Optional[list[dict]] = None) -> int:
    if ideas is None:
        ideas = db.get_ideas_since(hours=24)

    tickers = list({idea["ticker"] for idea in ideas if idea.get("ticker")})
    if not tickers:
        logger.info("No tickers to enrich")
        return 0

    # Skip already-enriched tickers
    to_enrich = [t for t in tickers if not db.get_enrichment_today(t)]
    already_done = len(tickers) - len(to_enrich)
    if already_done:
        logger.debug("Skipping %d already-enriched tickers", already_done)
    if not to_enrich:
        logger.info("All tickers already enriched today")
        return already_done

    logger.info("Fetching market data for %d tickers: %s", len(to_enrich), to_enrich)
    market_data = _fetch_market_data(to_enrich)

    cfg = get_config()
    api_key = cfg.get("gemini_api_key", "")
    client = genai.Client(api_key=api_key) if api_key else None

    enriched = already_done
    for ticker in to_enrich:
        data = market_data.get(ticker)
        if not data:
            logger.warning("No market data for %s — skipping enrichment", ticker)
            continue

        logger.info("Enriching %s", ticker)
        if client:
            summary = _fetch_research_summary(client, ticker, data)
            data["research_summary"] = summary
        else:
            data["research_summary"] = None

        db.upsert_enrichment(ticker, data)
        enriched += 1

    logger.info("Enriched %d/%d tickers", enriched, len(tickers))
    return enriched
