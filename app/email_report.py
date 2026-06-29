import logging
import time
from collections import defaultdict
from datetime import date, timedelta
from typing import Optional

import requests

from . import database as db
from .config import get as get_config

logger = logging.getLogger(__name__)

PUSHOVER_API = "https://api.pushover.net/1/messages.json"


def _fmt_mcap(v) -> str:
    if not v:
        return "N/A"
    b = v / 1e9
    return f"${b:.1f}B" if b >= 1 else f"${v/1e6:.0f}M"


def _fmt_pct(v) -> str:
    if v is None:
        return "N/A"
    sign = "+" if v >= 0 else ""
    return f"{sign}{v:.1f}%"


def _fmt_price(v) -> str:
    return f"${v:.2f}" if v else "N/A"


def _conviction_label(c: str) -> str:
    return {"high": "HIGH", "medium": "MED", "low": "LOW"}.get(c, c.upper())


def _build_idea_html(idea: dict, enr: dict) -> str:
    ticker = idea.get("ticker", "")
    author = idea.get("author", "")
    source_type = idea.get("source_type", "")
    url = idea.get("url", "")
    sentiment = idea.get("sentiment", "")
    conviction = idea.get("conviction", "")
    thesis = idea.get("thesis", "")
    quote = idea.get("quote", "")
    idea_type = idea.get("idea_type", "explicit")
    research = enr.get("research_summary", "")

    price = _fmt_price(enr.get("current_price"))
    pct_1w = _fmt_pct(enr.get("pct_change_1w"))
    pct_1m = _fmt_pct(enr.get("pct_change_1m"))
    pct_3m = _fmt_pct(enr.get("pct_change_3m"))
    mcap = _fmt_mcap(enr.get("market_cap"))
    pe = f"{enr['pe_ratio']:.1f}x" if enr.get("pe_ratio") else "N/A"
    freshness = enr.get("freshness_signal", "")
    sentiment_color = "#34d399" if sentiment == "bullish" else "#f87171"
    conviction_color = {"high": "#34d399", "medium": "#fbbf24", "low": "#94a3b8"}.get(conviction, "#94a3b8")

    source_link = f'<a href="{url}" style="color:#7dd3fc;text-decoration:none;">@{author}</a>' if url else f"@{author}"

    quote_html = f'<blockquote style="margin:10px 0 0 0;padding:8px 12px;border-left:3px solid #334155;color:#94a3b8;font-style:italic;font-size:13px;">"{quote}"</blockquote>' if quote else ""
    research_html = f'<p style="margin:10px 0 0 0;color:#94a3b8;font-size:13px;line-height:1.6;"><strong style="color:#cbd5e1;">Research:</strong> {research}</p>' if research else ""
    freshness_html = f' &nbsp;<span style="background:#1e293b;color:#94a3b8;border:1px solid #334155;padding:1px 6px;border-radius:4px;font-size:11px;">{freshness}</span>' if freshness else ""
    thematic_html = ' &nbsp;<span style="background:#1e1b4b;color:#a78bfa;border:1px solid #4c1d95;padding:1px 6px;border-radius:4px;font-size:11px;">THEMATIC</span>' if idea_type == "thematic" else ""

    return f"""
<div style="border-bottom:1px solid #1e293b;padding:18px 0;">
  <div style="display:flex;align-items:baseline;gap:12px;flex-wrap:wrap;">
    <span style="font-size:18px;font-weight:700;color:#f1f5f9;">{ticker}</span>
    <span style="color:{sentiment_color};font-size:12px;font-weight:600;background:{sentiment_color}22;padding:2px 8px;border-radius:4px;">{sentiment.upper()}</span>
    <span style="color:{conviction_color};font-size:12px;font-weight:600;">{_conviction_label(conviction)}</span>
    {thematic_html}
    {freshness_html}
    <span style="color:#64748b;font-size:12px;margin-left:auto;">{source_type} · {source_link}</span>
  </div>
  <p style="margin:8px 0 0 0;color:#cbd5e1;font-size:14px;line-height:1.6;">{thesis}</p>
  {quote_html}
  {research_html}
  <div style="margin-top:10px;display:flex;gap:20px;flex-wrap:wrap;">
    <span style="color:#64748b;font-size:12px;">Price: <strong style="color:#94a3b8;">{price}</strong></span>
    <span style="color:#64748b;font-size:12px;">MCap: <strong style="color:#94a3b8;">{mcap}</strong></span>
    <span style="color:#64748b;font-size:12px;">1W: <strong style="color:#94a3b8;">{pct_1w}</strong></span>
    <span style="color:#64748b;font-size:12px;">1M: <strong style="color:#94a3b8;">{pct_1m}</strong></span>
    <span style="color:#64748b;font-size:12px;">3M: <strong style="color:#94a3b8;">{pct_3m}</strong></span>
    <span style="color:#64748b;font-size:12px;">P/E: <strong style="color:#94a3b8;">{pe}</strong></span>
  </div>
</div>"""


def _build_html_report(ideas: list[dict], stats: dict, today: date) -> str:
    enrichments = {i["ticker"]: (db.get_enrichment_today(i["ticker"]) or {}) for i in ideas}

    high = [i for i in ideas if i.get("conviction") == "high"]
    medium = [i for i in ideas if i.get("conviction") == "medium"]
    low = [i for i in ideas if i.get("conviction") == "low"]

    def section(title: str, color: str, items: list) -> str:
        if not items:
            return ""
        ideas_html = "".join(_build_idea_html(i, enrichments.get(i["ticker"], {})) for i in items)
        return f"""
<h2 style="margin:32px 0 0 0;padding-bottom:8px;border-bottom:2px solid {color};color:{color};font-size:15px;font-weight:700;letter-spacing:0.05em;text-transform:uppercase;">{title} ({len(items)})</h2>
{ideas_html}"""

    body = (
        section("High Conviction", "#34d399", high)
        + section("Medium Conviction", "#fbbf24", medium)
        + section("Low Conviction", "#94a3b8", low)
    )

    return f"""<!DOCTYPE html>
<html>
<head><meta charset="utf-8"><title>EquityBuddy — {today.strftime('%Y-%m-%d')}</title></head>
<body style="margin:0;padding:0;background:#0f172a;color:#e2e8f0;font-family:-apple-system,BlinkMacSystemFont,'Segoe UI',sans-serif;">
<div style="max-width:800px;margin:0 auto;padding:32px 24px;">
  <h1 style="margin:0 0 4px 0;font-size:22px;font-weight:700;color:#f1f5f9;">EquityBuddy Investment Digest</h1>
  <p style="margin:0 0 24px 0;color:#64748b;font-size:14px;">{today.strftime('%B %d, %Y')} &nbsp;·&nbsp; {len(ideas)} ideas &nbsp;·&nbsp; {stats.get('sources_active', 0)} active sources</p>
  <hr style="border:none;border-top:1px solid #1e293b;margin-bottom:8px;">
  {body}
</div>
</body>
</html>"""


def _send_pushover_digest(subject: str, summary: str, report_id: Optional[int] = None) -> bool:
    cfg = get_config()
    user_key = cfg["pushover"].get("user_key", "")
    api_token = cfg["pushover"].get("api_token", "")

    if not user_key or not api_token:
        logger.warning("Pushover not configured — skipping digest notification")
        return False

    base_url = cfg.get("base_url", "")
    payload = {
        "token": api_token,
        "user": user_key,
        "title": subject[:250],
        "message": summary[:1024],
        "html": "1",
        "priority": 0,
    }
    if base_url and report_id:
        payload["url"] = f"{base_url.rstrip('/')}/reports/{report_id}"
        payload["url_title"] = "View Full Report"
    elif base_url:
        payload["url"] = f"{base_url.rstrip('/')}/reports"
        payload["url_title"] = "View Reports"

    try:
        resp = requests.post(PUSHOVER_API, data=payload, timeout=10)
        resp.raise_for_status()
        logger.info("Digest Pushover notification sent")
        return True
    except Exception as e:
        logger.error("Pushover digest send failed: %s", e)
        return False


def _build_pushover_summary(ideas: list[dict], stats: dict, today: date) -> str:
    high = [i for i in ideas if i.get("conviction") == "high"]
    med  = [i for i in ideas if i.get("conviction") == "medium"]
    low  = [i for i in ideas if i.get("conviction") == "low"]

    parts: list[str] = []
    parts.append(
        f"<b>{today.strftime('%b %d')}</b> · {len(ideas)} ideas · {stats['sources_active']} sources"
    )

    if high:
        parts.append("<b>── HIGH CONVICTION ──</b>")
        for i in high[:6]:
            arrow = "↑" if i.get("sentiment") == "bullish" else "↓"
            thesis = (i.get("thesis") or "").strip()
            # Strip [Thematic] prefix for compactness
            thesis = thesis.removeprefix("[Thematic] ")
            snippet = thesis[:90] + "…" if len(thesis) > 90 else thesis
            parts.append(f"<b>${i['ticker']}</b> {arrow}  {snippet}")

    if med:
        parts.append("<b>── MEDIUM ──</b>")
        for i in med[:5]:
            arrow = "↑" if i.get("sentiment") == "bullish" else "↓"
            thesis = (i.get("thesis") or "").strip().removeprefix("[Thematic] ")
            snippet = thesis[:70] + "…" if len(thesis) > 70 else thesis
            parts.append(f"<b>${i['ticker']}</b> {arrow}  {snippet}")

    if low:
        tickers = "  ".join(
            f"${i['ticker']} {'↑' if i.get('sentiment') == 'bullish' else '↓'}"
            for i in low[:8]
        )
        parts.append(f"<b>── LOW ({len(low)}) ──</b>  {tickers}")

    msg = "\n".join(parts)
    # Pushover hard cap is 1024 chars; trim to last complete line if over
    if len(msg) > 1024:
        msg = msg[:1021] + "…"
    return msg


def send_test_pushover() -> str:
    """Send a test Pushover notification using today's real ideas if any exist."""
    today = date.today()
    ideas = db.get_ideas_today()
    stats = db.get_stats()

    cfg = get_config()
    if not cfg["pushover"].get("user_key") or not cfg["pushover"].get("api_token"):
        return "error: Pushover is not configured in config.yaml"

    if ideas:
        subject = f"[TEST] EquityBuddy — {today.strftime('%Y-%m-%d')} — {len(ideas)} ideas"
        summary = _build_pushover_summary(ideas, stats, today)
    else:
        subject = f"[TEST] EquityBuddy — {today.strftime('%Y-%m-%d')}"
        summary = (
            f"<b>{today.strftime('%b %d')}</b> · Pushover is working\n"
            "No ideas extracted today yet — this is just a connectivity test."
        )

    ok = _send_pushover_digest(subject, summary)
    return "ok" if ok else "error: Pushover request failed — check logs"


def generate_and_send_digest() -> Optional[int]:
    today = date.today()
    ideas = db.get_ideas_today()
    stats = db.get_stats()

    if not ideas:
        logger.info("No ideas today — skipping digest")
        return None

    subject = f"EquityBuddy — {today.strftime('%Y-%m-%d')} — {len(ideas)} new ideas"

    html = _build_html_report(ideas, stats, today)
    report_id = db.insert_report(subject=subject, html_content=html)
    logger.info("Digest report stored (id=%d)", report_id)

    summary = _build_pushover_summary(ideas, stats, today)
    _send_pushover_digest(subject, summary, report_id=report_id)

    return report_id


# ── Weekly report ──────────────────────────────────────────────────────────


def _aggregate_weekly_ideas(ideas: list[dict]) -> list[dict]:
    """Aggregate raw ideas into per-ticker weekly summaries ranked by persistence."""
    conv_rank = {"high": 3, "medium": 2, "low": 1}
    buckets: dict[str, dict] = defaultdict(lambda: {
        "mention_count": 0,
        "days": set(),
        "authors": set(),
        "high_count": 0,
        "medium_count": 0,
        "low_count": 0,
        "bullish_count": 0,
        "bearish_count": 0,
        "best_thesis": "",
        "best_conviction": "low",
        "last_seen": "",
        "first_seen": "",
    })

    for idea in ideas:
        ticker = idea["ticker"]
        d = buckets[ticker]
        d["mention_count"] += 1
        d["days"].add(idea["extracted_at"][:10])
        d["authors"].add(idea["author"])

        conv = idea.get("conviction", "low")
        if conv == "high":
            d["high_count"] += 1
        elif conv == "medium":
            d["medium_count"] += 1
        else:
            d["low_count"] += 1

        if idea.get("sentiment") == "bullish":
            d["bullish_count"] += 1
        else:
            d["bearish_count"] += 1

        thesis = idea.get("thesis", "")
        if thesis and conv_rank.get(conv, 1) >= conv_rank.get(d["best_conviction"], 1):
            d["best_thesis"] = thesis
            d["best_conviction"] = conv

        ts = idea.get("extracted_at", "")
        if ts:
            if not d["last_seen"] or ts > d["last_seen"]:
                d["last_seen"] = ts
            if not d["first_seen"] or ts < d["first_seen"]:
                d["first_seen"] = ts

    result = []
    for ticker, d in buckets.items():
        day_count = len(d["days"])
        author_count = len(d["authors"])
        top_conviction = "high" if d["high_count"] else "medium" if d["medium_count"] else "low"
        conviction_score = d["high_count"] * 3 + d["medium_count"] * 2 + d["low_count"]
        persistence_score = conviction_score * author_count + day_count * 2
        result.append({
            "ticker": ticker,
            "mention_count": d["mention_count"],
            "day_count": day_count,
            "author_count": author_count,
            "authors": ", ".join(sorted(d["authors"])),
            "high_count": d["high_count"],
            "medium_count": d["medium_count"],
            "low_count": d["low_count"],
            "bullish_count": d["bullish_count"],
            "bearish_count": d["bearish_count"],
            "top_conviction": top_conviction,
            "best_thesis": d["best_thesis"],
            "persistence_score": persistence_score,
            "last_seen": d["last_seen"],
            "first_seen": d["first_seen"],
        })

    result.sort(key=lambda x: (-x["persistence_score"], -x["mention_count"]))
    return result


def _build_weekly_ticker_html(agg: dict, enr: dict) -> str:
    ticker = agg["ticker"]
    top_conviction = agg["top_conviction"]
    thesis = agg["best_thesis"].removeprefix("[Thematic] ")
    pct_1w = _fmt_pct(enr.get("pct_change_1w"))
    pct_1m = _fmt_pct(enr.get("pct_change_1m"))
    price = _fmt_price(enr.get("current_price"))
    mcap = _fmt_mcap(enr.get("market_cap"))

    conviction_color = {"high": "#34d399", "medium": "#fbbf24", "low": "#94a3b8"}.get(top_conviction, "#94a3b8")
    sentiment = "bullish" if agg["bullish_count"] >= agg["bearish_count"] else "bearish"
    sentiment_color = "#34d399" if sentiment == "bullish" else "#f87171"

    day_count = agg["day_count"]
    mention_count = agg["mention_count"]
    author_count = agg["author_count"]
    authors = agg["authors"]

    high_pills = f'<span style="color:#34d399;font-size:11px;background:#0d2414;padding:1px 5px;border-radius:3px;margin-right:3px;">H:{agg["high_count"]}</span>' if agg["high_count"] else ""
    med_pills  = f'<span style="color:#fbbf24;font-size:11px;background:#1c1400;padding:1px 5px;border-radius:3px;margin-right:3px;">M:{agg["medium_count"]}</span>' if agg["medium_count"] else ""
    low_pills  = f'<span style="color:#94a3b8;font-size:11px;background:#1e293b;padding:1px 5px;border-radius:3px;margin-right:3px;">L:{agg["low_count"]}</span>' if agg["low_count"] else ""

    return f"""
<div style="border-bottom:1px solid #1e293b;padding:16px 0;">
  <div style="display:flex;align-items:baseline;gap:10px;flex-wrap:wrap;">
    <span style="font-size:18px;font-weight:700;color:#f1f5f9;">{ticker}</span>
    <span style="color:{conviction_color};font-size:12px;font-weight:600;">{top_conviction.upper()} CONVICTION</span>
    <span style="color:{sentiment_color};font-size:11px;background:{sentiment_color}22;padding:1px 6px;border-radius:3px;">{sentiment.upper()}</span>
    {high_pills}{med_pills}{low_pills}
    <span style="color:#64748b;font-size:11px;margin-left:auto;">{mention_count} mentions · {day_count}d · {author_count} source{"s" if author_count != 1 else ""}</span>
  </div>
  <p style="margin:6px 0 0 0;color:#cbd5e1;font-size:14px;line-height:1.5;">{thesis}</p>
  <p style="margin:6px 0 0 0;color:#475569;font-size:12px;">Sources: {authors}</p>
  <div style="margin-top:8px;display:flex;gap:16px;flex-wrap:wrap;">
    <span style="color:#64748b;font-size:12px;">Price: <strong style="color:#94a3b8;">{price}</strong></span>
    <span style="color:#64748b;font-size:12px;">MCap: <strong style="color:#94a3b8;">{mcap}</strong></span>
    <span style="color:#64748b;font-size:12px;">1W: <strong style="color:#94a3b8;">{pct_1w}</strong></span>
    <span style="color:#64748b;font-size:12px;">1M: <strong style="color:#94a3b8;">{pct_1m}</strong></span>
  </div>
</div>"""


def _build_weekly_html_report(
    aggregated: list[dict],
    period_start: date,
    period_end: date,
    total_ideas: int,
    active_sources: int,
) -> str:
    enrichments = {a["ticker"]: (db.get_enrichment_today(a["ticker"]) or {}) for a in aggregated}

    # Strong: high conviction OR appearing 2+ days OR 2+ sources
    strong = [a for a in aggregated if a["top_conviction"] == "high" or a["day_count"] >= 2 or a["author_count"] >= 2]
    notable = [a for a in aggregated if a not in strong and a["top_conviction"] != "low"]
    low_tier = [a for a in aggregated if a not in strong and a not in notable]

    def section(title: str, color: str, items: list) -> str:
        if not items:
            return ""
        cards = "".join(_build_weekly_ticker_html(a, enrichments.get(a["ticker"], {})) for a in items)
        return f"""
<h2 style="margin:32px 0 0 0;padding-bottom:8px;border-bottom:2px solid {color};color:{color};font-size:14px;font-weight:700;letter-spacing:0.08em;text-transform:uppercase;">{title} ({len(items)})</h2>
{cards}"""

    body = (
        section("Strong Conviction", "#a78bfa", strong)
        + section("Notable", "#fbbf24", notable)
        + section("Low Conviction", "#475569", low_tier)
    )

    date_range = f"{period_start.strftime('%b %d')}–{period_end.strftime('%b %d, %Y')}"
    return f"""<!DOCTYPE html>
<html>
<head><meta charset="utf-8"><title>EquityBuddy Weekly — {date_range}</title></head>
<body style="margin:0;padding:0;background:#0f172a;color:#e2e8f0;font-family:-apple-system,BlinkMacSystemFont,'Segoe UI',sans-serif;">
<div style="max-width:800px;margin:0 auto;padding:32px 24px;">
  <div style="border-left:3px solid #7c3aed;padding-left:16px;margin-bottom:24px;">
    <h1 style="margin:0 0 4px 0;font-size:22px;font-weight:700;color:#f1f5f9;">EquityBuddy Weekly Intelligence</h1>
    <p style="margin:0;color:#64748b;font-size:14px;">{date_range} &nbsp;·&nbsp; {total_ideas} ideas &nbsp;·&nbsp; {len(aggregated)} unique tickers &nbsp;·&nbsp; {active_sources} sources</p>
    <p style="margin:6px 0 0 0;color:#475569;font-size:12px;">Ranked by persistence: conviction strength × source diversity × days seen</p>
  </div>
  <hr style="border:none;border-top:1px solid #1e293b;margin-bottom:8px;">
  {body}
</div>
</body>
</html>"""


def _build_weekly_pushover_summary(
    aggregated: list[dict],
    period_start: date,
    period_end: date,
    total_ideas: int,
) -> str:
    date_range = f"{period_start.strftime('%b %d')}–{period_end.strftime('%b %d')}"
    parts = [f"<b>Week {date_range}</b> · {total_ideas} ideas · {len(aggregated)} tickers"]

    strong = [a for a in aggregated if a["top_conviction"] == "high" or a["day_count"] >= 2 or a["author_count"] >= 2]

    if strong:
        parts.append("<b>── TOP PERSISTENT BETS ──</b>")
        for a in strong[:8]:
            arrow = "↑" if a["bullish_count"] >= a["bearish_count"] else "↓"
            thesis = a["best_thesis"].removeprefix("[Thematic] ").strip()
            snippet = thesis[:80] + "…" if len(thesis) > 80 else thesis
            parts.append(
                f"<b>${a['ticker']}</b> {arrow} {a['day_count']}d·{a['author_count']}src  {snippet}"
            )

    msg = "\n".join(parts)
    if len(msg) > 1024:
        msg = msg[:1021] + "…"
    return msg


def generate_weekly_report(days: int = 7) -> Optional[int]:
    period_end = date.today()
    period_start = period_end - timedelta(days=days - 1)
    ideas = db.get_ideas_for_period(days=days)

    if not ideas:
        logger.info("No ideas in the past %d days — skipping weekly digest", days)
        return None

    aggregated = _aggregate_weekly_ideas(ideas)
    stats = db.get_stats()
    active_sources = stats.get("sources_active", 0)

    subject = (
        f"EquityBuddy Weekly — {period_start.strftime('%b %d')}–{period_end.strftime('%b %d')} "
        f"— {len(aggregated)} tickers"
    )
    html = _build_weekly_html_report(aggregated, period_start, period_end, len(ideas), active_sources)
    report_id = db.insert_report(subject=subject, html_content=html)
    logger.info("Weekly report stored (id=%d)", report_id)

    summary = _build_weekly_pushover_summary(aggregated, period_start, period_end, len(ideas))
    _send_pushover_digest(subject, summary, report_id=report_id)

    return report_id
