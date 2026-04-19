import logging
import smtplib
import time
from datetime import date
from email.mime.multipart import MIMEMultipart
from email.mime.text import MIMEText
from typing import Optional

import anthropic

from . import database as db
from .config import get as get_config

logger = logging.getLogger(__name__)

DIGEST_PROMPT = """Generate an HTML email digest for stock investment ideas. Use inline CSS only (no external stylesheets or <style> blocks with class selectors — use style="" attributes directly on elements).

Date: {date}
New ideas today: {idea_count}
Active sources: {source_count}

Ideas data (JSON):
{ideas_json}

Requirements:
- Dark background (#0f172a), light text (#e2e8f0)
- Header section with date and counts
- Ideas grouped by conviction: High → Medium → Low
- Per idea show: ticker (bold, large), source+link, sentiment badge (green=bullish, red=bearish), freshness signal pill (green=early, yellow=in_motion, red=may_have_moved), thesis, research summary, fundamentals row (MCap, 3m change, PE ratio), "Track This" link to http://localhost:7842/watchlist-stocks (replace localhost with actual host if known)
- Professional, clean layout with good spacing
- Subject line at the very top of your response as: SUBJECT: <subject here>
- Then the full HTML starting with <!DOCTYPE html>"""


def _call_claude_with_retry(client: anthropic.Anthropic, prompt: str,
                             max_retries: int = 3) -> Optional[str]:
    for attempt in range(max_retries):
        try:
            response = client.messages.create(
                model="claude-sonnet-4-20250514",
                max_tokens=8192,
                messages=[{"role": "user", "content": prompt}],
            )
            return response.content[0].text
        except anthropic.RateLimitError as e:
            wait = 2 ** attempt * 5
            logger.warning("Rate limit hit for digest, retrying in %ds: %s", wait, e)
            time.sleep(wait)
        except anthropic.APIError as e:
            wait = 2 ** attempt * 2
            logger.warning("API error for digest, retrying in %ds: %s", wait, e)
            time.sleep(wait)
    return None


def _send_email(subject: str, html: str) -> bool:
    cfg = get_config()
    smtp_host = cfg["email"].get("smtp_host", "")
    if not smtp_host:
        logger.warning("Email not configured — skipping send")
        return False

    smtp_port = cfg["email"].get("smtp_port", 587)
    smtp_user = cfg["email"].get("smtp_user", "")
    smtp_password = cfg["email"].get("smtp_password", "")
    to_address = cfg["email"].get("to_address", "")

    if not to_address:
        logger.warning("No to_address configured — skipping email send")
        return False

    try:
        msg = MIMEMultipart("alternative")
        msg["Subject"] = subject
        msg["From"] = smtp_user
        msg["To"] = to_address
        msg.attach(MIMEText(html, "html"))

        with smtplib.SMTP(smtp_host, smtp_port) as server:
            server.ehlo()
            server.starttls()
            if smtp_user and smtp_password:
                server.login(smtp_user, smtp_password)
            server.sendmail(smtp_user, to_address, msg.as_string())

        logger.info("Digest email sent to %s", to_address)
        return True
    except Exception as e:
        logger.error("Email send failed: %s", e)
        return False


def generate_and_send_digest() -> Optional[int]:
    cfg = get_config()
    if not cfg.get("anthropic_api_key"):
        logger.warning("No Anthropic API key — skipping digest generation")
        return None

    today = date.today()
    ideas = db.get_ideas_today()
    stats = db.get_stats()

    if not ideas:
        logger.info("No ideas today — skipping digest")
        return None

    ideas_with_enrichment = []
    for idea in ideas:
        enrichment = db.get_enrichment_today(idea["ticker"]) or {}
        ideas_with_enrichment.append({**idea, "enrichment": enrichment})

    import json
    ideas_json = json.dumps(ideas_with_enrichment, indent=2, default=str)

    prompt = DIGEST_PROMPT.format(
        date=today.strftime("%B %d, %Y"),
        idea_count=len(ideas),
        source_count=stats["sources_active"],
        ideas_json=ideas_json[:12000],
    )

    client = anthropic.Anthropic(api_key=cfg["anthropic_api_key"])
    raw = _call_claude_with_retry(client, prompt)
    if not raw:
        logger.error("Failed to generate digest")
        return None

    subject = f"EquityBuddy — {today.strftime('%Y-%m-%d')} — {len(ideas)} new ideas"
    html = raw

    if raw.startswith("SUBJECT:"):
        lines = raw.split("\n", 1)
        subject = lines[0].replace("SUBJECT:", "").strip()
        html = lines[1].strip() if len(lines) > 1 else raw

    report_id = db.insert_report(subject=subject, html_content=html)
    _send_email(subject, html)
    logger.info("Digest generated (report id=%d)", report_id)
    return report_id
