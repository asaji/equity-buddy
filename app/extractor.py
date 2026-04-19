import json
import logging
import time
from typing import Optional

import anthropic

from . import database as db
from .config import get as get_config

logger = logging.getLogger(__name__)

EXTRACTION_PROMPT = """You are a financial analyst assistant. Analyze the following social media posts and extract investment ideas.

For each post, identify any stock tickers mentioned and extract structured investment information.

Return a JSON array. Each element must have:
- "ticker": stock symbol (uppercase, no $)
- "conviction": "low", "medium", or "high"
- "sentiment": "bullish" or "bearish"
- "thesis": 1-2 sentence investment thesis
- "direct_quote": a key phrase directly from the post
- "post_index": the index (0-based) of the source post in the list

Only include clear investment ideas with specific tickers. Skip general market commentary without specific tickers. Skip tickers that are only mentioned in passing without an investment thesis.

Return valid JSON only, no explanation. Example:
[{"ticker": "NVDA", "conviction": "high", "sentiment": "bullish", "thesis": "...", "direct_quote": "...", "post_index": 0}]

If no investment ideas found, return an empty array: []

Posts to analyze:
{posts_json}"""


def _call_claude_with_retry(client: anthropic.Anthropic, prompt: str,
                             max_retries: int = 3) -> Optional[str]:
    for attempt in range(max_retries):
        try:
            response = client.messages.create(
                model="claude-sonnet-4-20250514",
                max_tokens=4096,
                messages=[{"role": "user", "content": prompt}],
            )
            return response.content[0].text
        except anthropic.RateLimitError as e:
            wait = 2 ** attempt * 5
            logger.warning("Rate limit hit, retrying in %ds (attempt %d): %s", wait, attempt + 1, e)
            time.sleep(wait)
        except anthropic.APIError as e:
            wait = 2 ** attempt * 2
            logger.warning("API error, retrying in %ds (attempt %d): %s", wait, attempt + 1, e)
            time.sleep(wait)
    logger.error("Claude API failed after %d retries", max_retries)
    return None


def extract_ideas_from_posts(posts: list[dict]) -> int:
    cfg = get_config()
    api_key = cfg.get("anthropic_api_key", "")
    if not api_key:
        logger.warning("No Anthropic API key — skipping extraction")
        return 0

    if not posts:
        return 0

    client = anthropic.Anthropic(api_key=api_key)

    posts_data = [
        {
            "index": i,
            "author": p["author"],
            "source": p["source_type"],
            "content": p["content"][:2000],
        }
        for i, p in enumerate(posts)
    ]

    prompt = EXTRACTION_PROMPT.format(posts_json=json.dumps(posts_data, indent=2))
    raw = _call_claude_with_retry(client, prompt)
    if not raw:
        return 0

    try:
        raw = raw.strip()
        if raw.startswith("```"):
            raw = raw.split("```")[1]
            if raw.startswith("json"):
                raw = raw[4:]
        ideas_data = json.loads(raw)
    except json.JSONDecodeError as e:
        logger.error("Failed to parse extraction response as JSON: %s\nRaw: %s", e, raw[:500])
        return 0

    total = 0
    for item in ideas_data:
        try:
            post_index = item.get("post_index", 0)
            if post_index >= len(posts):
                continue
            post = posts[post_index]
            ticker = item.get("ticker", "").upper().strip()
            if not ticker:
                continue

            if db.ticker_extracted_for_author_recently(ticker, post["author"]):
                logger.debug("Skipping %s from %s — already extracted recently", ticker, post["author"])
                continue

            db.insert_idea(
                post_id=post["id"],
                ticker=ticker,
                conviction=item.get("conviction", "low"),
                sentiment=item.get("sentiment", "bullish"),
                thesis=item.get("thesis", ""),
                quote=item.get("direct_quote", ""),
            )
            total += 1
        except Exception as e:
            logger.warning("Error saving idea %s: %s", item, e)

    logger.info("Extracted %d new ideas from %d posts", total, len(posts))
    return total


def run_extraction() -> int:
    posts = db.get_posts_since(hours=24)
    if not posts:
        logger.info("No recent posts to extract ideas from")
        return 0

    return extract_ideas_from_posts(posts)
