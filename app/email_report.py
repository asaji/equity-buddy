import logging
import time
from datetime import date
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
