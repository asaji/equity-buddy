import logging
from typing import Optional

import requests

from . import database as db
from .config import get as get_config

logger = logging.getLogger(__name__)

PUSHOVER_API = "https://api.pushover.net/1/messages.json"

CONVICTION_ORDER = {"low": 0, "medium": 1, "high": 2}


def _send_pushover(title: str, message: str, url: Optional[str] = None) -> bool:
    cfg = get_config()
    user_key = cfg["pushover"].get("user_key", "")
    api_token = cfg["pushover"].get("api_token", "")

    if not user_key or not api_token:
        logger.debug("Pushover not configured — skipping alert")
        return False

    payload = {
        "token": api_token,
        "user": user_key,
        "title": title[:250],
        "message": message[:1024],
    }
    if url:
        payload["url"] = url
        payload["url_title"] = "View Post"

    try:
        resp = requests.post(PUSHOVER_API, data=payload, timeout=10)
        resp.raise_for_status()
        logger.info("Pushover alert sent: %s", title)
        return True
    except Exception as e:
        logger.error("Pushover send failed: %s", e)
        return False


def send_idea_alert(idea: dict, enrichment: Optional[dict] = None) -> bool:
    cfg = get_config()
    threshold = cfg.get("conviction_alert_threshold", "high")
    threshold_level = CONVICTION_ORDER.get(threshold, 2)
    idea_level = CONVICTION_ORDER.get(idea.get("conviction", "low"), 0)

    if idea_level < threshold_level:
        return False

    ticker = idea["ticker"]
    author = idea.get("author", "unknown")
    sentiment = idea.get("sentiment", "bullish")
    thesis = idea.get("thesis", "")

    title = f"${ticker} — {sentiment} via @{author}"

    lines = [thesis]
    if enrichment:
        freshness = enrichment.get("freshness_signal", "")
        mcap = enrichment.get("market_cap") or 0
        pct_3m = enrichment.get("pct_change_3m")
        pe = enrichment.get("pe_ratio")

        mcap_str = f"${mcap/1e9:.1f}b" if mcap else "N/A"
        pct_str = f"{pct_3m:+.1f}%" if pct_3m is not None else "N/A"
        pe_str = f"{pe:.1f}" if pe else "N/A"

        lines.append(f"Signal: {freshness} | MCap: {mcap_str} | 3m: {pct_str} | PE: {pe_str}")

    message = "\n".join(lines)
    url = idea.get("url")

    sent = _send_pushover(title, message, url)
    if sent:
        db.insert_alert(
            ticker=ticker,
            alert_type="idea",
            message=message,
            idea_id=idea.get("id"),
        )
    return sent


def check_and_send_price_milestones() -> int:
    cfg = get_config()
    gain_thresholds = cfg["watchlist_alerts"].get("gain_thresholds", [20, 50, 100])
    loss_threshold = cfg["watchlist_alerts"].get("loss_threshold", -20)

    stocks = db.get_active_watchlist_stocks()
    sent_count = 0

    for stock in stocks:
        ticker = stock["ticker"]
        awareness_price = stock.get("awareness_price")
        if not awareness_price:
            continue

        history = db.get_price_history(ticker, days=1)
        if not history:
            continue

        latest = history[-1]
        current_price = latest["close_price"]
        pct = (current_price - awareness_price) / awareness_price * 100

        author = stock.get("author") or "unknown"
        mention_date = (stock.get("mention_date") or stock.get("added_date") or "")[:10]

        for threshold in gain_thresholds:
            if pct >= threshold:
                label = f"milestone_{threshold}"
                if not db.price_milestone_already_fired(ticker, threshold):
                    title = f"{ticker} +{threshold}% since you started tracking"
                    message = (
                        f"Awareness: ${awareness_price:.2f} → Now: ${current_price:.2f}\n"
                        f"Mentioned by @{author} on {mention_date}\n"
                        f"[{label}]"
                    )
                    if _send_pushover(title, message):
                        db.insert_alert(ticker=ticker, alert_type="price_milestone", message=message)
                        sent_count += 1

        if pct <= loss_threshold:
            label = f"milestone_{loss_threshold}"
            if not db.price_milestone_already_fired(ticker, loss_threshold):
                title = f"{ticker} {loss_threshold}% since you started tracking"
                message = (
                    f"Awareness: ${awareness_price:.2f} → Now: ${current_price:.2f}\n"
                    f"Mentioned by @{author} on {mention_date}\n"
                    f"[{label}]"
                )
                if _send_pushover(title, message):
                    db.insert_alert(ticker=ticker, alert_type="price_milestone", message=message)
                    sent_count += 1

    return sent_count


def send_idea_alerts_for_new_ideas(ideas: list[dict]) -> int:
    sent = 0
    for idea in ideas:
        enrichment = db.get_enrichment_today(idea["ticker"])
        if send_idea_alert(idea, enrichment):
            sent += 1
    return sent
