import json
import logging
import time
from typing import Optional

from google import genai
from google.genai import types

from . import database as db
from .config import get as get_config

logger = logging.getLogger(__name__)

EXTRACTION_PROMPT = """You are a buy-side equity analyst. Analyze the following posts and extract investment ideas using two modes:

MODE 1 — EXPLICIT: Post directly mentions a ticker or company with an investment view.
MODE 2 — THEMATIC: Post discusses an investment theme, trend, or concept without naming specific stocks. Infer the most relevant publicly traded beneficiaries (max 3 per post).

For every idea return a JSON object with:
- "ticker": stock symbol (uppercase, no $)
- "conviction": "high", "medium", or "low". Thematic/inferred ideas are capped at "low".
- "sentiment": "bullish" or "bearish"
- "thesis": 1-2 sentence investment thesis. For thematic ideas, start with "[Thematic] " and explain the connection to the trend.
- "direct_quote": a key phrase or concept from the post that supports this idea
- "post_index": 0-based index of the source post

Rules:
- Explicit tickers with a clear thesis: use stated conviction level
- Tickers only mentioned in passing (no thesis): skip
- Thematic posts with no tickers: infer up to 3 relevant tickers, set conviction "low"
- Purely macro/political commentary with no investable angle: skip
- General news reposts with no analysis: skip

Return valid JSON array only, no explanation.
Example: [{"ticker": "NVDA", "conviction": "high", "sentiment": "bullish", "thesis": "...", "direct_quote": "...", "post_index": 0}]
If nothing found: []

Posts:
{posts_json}"""


def _call_gemini_with_retry(client: genai.Client, prompt: str,
                             max_retries: int = 3) -> Optional[str]:
    for attempt in range(max_retries):
        try:
            response = client.models.generate_content(
                model="gemini-2.5-flash-lite",
                contents=prompt,
                config=types.GenerateContentConfig(max_output_tokens=8192),
            )
            return response.text
        except Exception as e:
            msg = str(e)
            wait = 2 ** attempt * (5 if "429" in msg or "RESOURCE_EXHAUSTED" in msg else 2)
            logger.warning("Gemini API error, retrying in %ds (attempt %d): %s", wait, attempt + 1, e)
            time.sleep(wait)
    logger.error("Gemini API failed after %d retries", max_retries)
    return None


BATCH_SIZE = 10


def _parse_ideas_json(raw: str) -> list:
    raw = raw.strip()
    if raw.startswith("```"):
        raw = raw.split("```")[1]
        if raw.startswith("json"):
            raw = raw[4:]
    raw = raw.strip()
    try:
        return json.loads(raw)
    except json.JSONDecodeError:
        # Truncated response — salvage complete objects by trimming to last valid ]
        last = raw.rfind("},")
        if last > 0:
            try:
                return json.loads(raw[:last + 1] + "]")
            except json.JSONDecodeError:
                pass
        logger.warning("Could not parse extraction response; skipping batch")
        return []


def _save_ideas(ideas_data: list, posts: list) -> int:
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
    return total


def extract_ideas_from_posts(posts: list[dict]) -> int:
    cfg = get_config()
    api_key = cfg.get("gemini_api_key", "")
    if not api_key:
        logger.warning("No Gemini API key — skipping extraction")
        return 0

    if not posts:
        return 0

    client = genai.Client(api_key=api_key)
    total = 0

    for batch_start in range(0, len(posts), BATCH_SIZE):
        batch = posts[batch_start:batch_start + BATCH_SIZE]
        posts_data = [
            {
                "index": i,
                "author": p["author"],
                "source": p["source_type"],
                "content": p["content"][:3000],
            }
            for i, p in enumerate(batch)
        ]

        prompt = EXTRACTION_PROMPT.replace("{posts_json}", json.dumps(posts_data, indent=2))
        raw = _call_gemini_with_retry(client, prompt)
        if not raw:
            continue

        ideas_data = _parse_ideas_json(raw)
        saved = _save_ideas(ideas_data, batch)
        total += saved
        logger.info("Batch %d-%d: extracted %d ideas", batch_start, batch_start + len(batch) - 1, saved)

    logger.info("Extracted %d new ideas from %d posts", total, len(posts))
    return total


def run_extraction() -> int:
    posts = db.get_unextracted_posts(hours=24)
    if not posts:
        logger.info("No recent posts to extract ideas from")
        return 0

    return extract_ideas_from_posts(posts)
