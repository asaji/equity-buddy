import json
import logging
import time
from datetime import date
from typing import Optional

from google import genai
from google.genai import types
import requests

from . import database as db
from .config import get as get_config

logger = logging.getLogger(__name__)

PUSHOVER_API = "https://api.pushover.net/1/messages.json"

REPORT_PROMPT = """Generate a full HTML report for a stock investment ideas digest. Use inline CSS only (no external stylesheets — use style="" attributes directly on elements).

Date: {date}
New ideas today: {idea_count}
Active sources: {source_count}

Ideas data (JSON):
{ideas_json}

Requirements:
- Dark background (#0f172a), light text (#e2e8f0)
- Header section with date and counts
- Ideas grouped by conviction: High → Medium → Low
- Per idea: ticker (bold), source+link, sentiment badge, freshness signal pill, thesis, research summary, key fundamentals (MCap, 3m change, PE)
- Professional, clean layout
- Return only the HTML starting with <!DOCTYPE html>, no preamble"""


def _call_gemini(client: genai.Client, prompt: str, max_retries: int = 3) -> Optional[str]:
    for attempt in range(max_retries):
        try:
            response = client.models.generate_content(
                model="gemini-2.0-flash",
                contents=prompt,
                config=types.GenerateContentConfig(max_output_tokens=8192),
            )
            return response.text
        except Exception as e:
            msg = str(e)
            wait = 2 ** attempt * (5 if "429" in msg or "RESOURCE_EXHAUSTED" in msg else 2)
            logger.warning("Gemini API error, retrying in %ds: %s", wait, e)
            time.sleep(wait)
    return None


def _send_pushover_digest(subject: str, summary: str) -> bool:
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
        "priority": 0,
    }
    if base_url:
        payload["url"] = f"{base_url.rstrip('/')}/reports"
        payload["url_title"] = "View Full Report"

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

    lines = [f"{today.strftime('%b %d')} · {len(ideas)} ideas · {stats['sources_active']} sources"]

    if high:
        tickers = ", ".join(
            f"${i['ticker']} ({'↑' if i.get('sentiment') == 'bullish' else '↓'})"
            for i in high[:5]
        )
        lines.append(f"HIGH: {tickers}")

    if med:
        tickers = ", ".join(f"${i['ticker']}" for i in med[:4])
        lines.append(f"MED: {tickers}")

    if low:
        lines.append(f"LOW: {len(low)} idea{'s' if len(low) != 1 else ''}")

    return "\n".join(lines)


def generate_and_send_digest() -> Optional[int]:
    cfg = get_config()
    today = date.today()
    ideas = db.get_ideas_today()
    stats = db.get_stats()

    if not ideas:
        logger.info("No ideas today — skipping digest")
        return None

    subject = f"EquityBuddy — {today.strftime('%Y-%m-%d')} — {len(ideas)} new ideas"

    # Always send a Pushover summary (no Claude needed for this part)
    summary = _build_pushover_summary(ideas, stats, today)
    _send_pushover_digest(subject, summary)

    # Generate full HTML report with Claude if API key is available
    api_key = cfg.get("gemini_api_key", "")
    if not api_key:
        logger.warning("No Gemini API key — storing plain text report only")
        html = f"<html><body><pre>{subject}\n\n{summary}</pre></body></html>"
        report_id = db.insert_report(subject=subject, html_content=html)
        return report_id

    ideas_with_enrichment = []
    for idea in ideas:
        enrichment = db.get_enrichment_today(idea["ticker"]) or {}
        ideas_with_enrichment.append({**idea, "enrichment": enrichment})

    prompt = REPORT_PROMPT.format(
        date=today.strftime("%B %d, %Y"),
        idea_count=len(ideas),
        source_count=stats["sources_active"],
        ideas_json=json.dumps(ideas_with_enrichment, indent=2, default=str)[:12000],
    )

    client = genai.Client(api_key=api_key)
    html = _call_gemini(client, prompt)
    if not html:
        logger.error("Failed to generate HTML report")
        html = f"<html><body><pre>{subject}\n\n{summary}</pre></body></html>"

    report_id = db.insert_report(subject=subject, html_content=html)
    logger.info("Digest report stored (id=%d)", report_id)
    return report_id
