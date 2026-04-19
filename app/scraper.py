import hashlib
import json
import logging
import os
from datetime import datetime, timedelta, timezone
from typing import Optional

import feedparser

from . import database as db
from .config import get as get_config  # still used for cookie path env override

logger = logging.getLogger(__name__)

COOKIES_PATH = "/app/cookies/twitter_cookies.json"

# Errors that indicate cookies are expired/invalid rather than transient
_AUTH_ERROR_PATTERNS = (
    "32",           # Could not authenticate
    "135",          # Timestamp out of bounds
    "326",          # Account locked
    "AuthError",
    "Unauthorized",
    "Could not authenticate",
)


def _hash(text: str) -> str:
    return hashlib.sha256(text.encode()).hexdigest()


def _is_recent(dt: Optional[datetime], hours: int = 24) -> bool:
    if dt is None:
        return True
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=timezone.utc)
    return dt >= datetime.now(timezone.utc) - timedelta(hours=hours)


def _looks_like_auth_error(exc: Exception) -> bool:
    msg = str(exc)
    return any(p in msg for p in _AUTH_ERROR_PATTERNS)


def _normalize_cookies_file() -> bool:
    """Ensure cookies file exists and is in twikit's {name: value} dict format.

    Cookie-Editor exports a list of full cookie objects — convert on first load.
    Returns True if the file is valid and ready to use.
    """
    if not os.path.exists(COOKIES_PATH):
        return False
    try:
        with open(COOKIES_PATH) as f:
            data = json.load(f)

        if isinstance(data, dict) and data:
            return True  # already in twikit format

        if isinstance(data, list) and data:
            # Browser export format — convert to {name: value}
            cookie_dict = {}
            for cookie in data:
                if not isinstance(cookie, dict) or "name" not in cookie:
                    continue
                domain = cookie.get("domain", "")
                if any(d in domain for d in ("twitter.com", "x.com", "t.co")):
                    cookie_dict[cookie["name"]] = cookie["value"]

            if not cookie_dict:
                # No domain match — just take all cookies (some exports omit domain)
                cookie_dict = {
                    c["name"]: c["value"]
                    for c in data
                    if isinstance(c, dict) and "name" in c
                }

            if not cookie_dict:
                logger.error("Cookie file contained no usable cookies")
                return False

            with open(COOKIES_PATH, "w") as f:
                json.dump(cookie_dict, f, indent=2)
            logger.info(
                "Converted browser cookie export to twikit format (%d cookies)", len(cookie_dict)
            )
            return True

        logger.error("Cookie file is empty or unrecognized format")
        return False
    except Exception as e:
        logger.error("Failed to read/normalize cookie file: %s", e)
        return False


# --- Twitter ---

async def auth_twitter(username: str, password: str, email: str) -> bool:
    try:
        from twikit import Client
        client = Client("en-US")
        await client.login(auth_info_1=username, auth_info_2=email, password=password)
        os.makedirs(os.path.dirname(COOKIES_PATH), exist_ok=True)
        client.save_cookies(COOKIES_PATH)
        logger.info("Twitter auth successful, cookies saved to %s", COOKIES_PATH)
        return True
    except Exception as e:
        logger.error("Twitter auth failed: %s", e)
        return False


async def _get_twitter_client():
    try:
        from twikit import Client
    except ImportError:
        logger.error("twikit not installed")
        return None

    if not _normalize_cookies_file():
        logger.warning(
            "Twitter cookies not found or invalid at %s — "
            "export cookies from your browser and place them there, "
            "or navigate to /auth/twitter to authenticate via username/password",
            COOKIES_PATH,
        )
        return None

    client = Client("en-US")
    try:
        client.load_cookies(COOKIES_PATH)
        logger.debug("Twitter cookies loaded from %s", COOKIES_PATH)
        return client
    except Exception as e:
        logger.error("Failed to load Twitter cookies: %s", e)
        return None


async def scrape_twitter(handles: list[str]) -> int:
    if not handles:
        return 0

    client = await _get_twitter_client()
    if client is None:
        return 0

    total = 0
    for handle in handles:
        try:
            handle = handle.lstrip("@")
            user = await client.get_user_by_screen_name(handle)
            tweets = await client.get_user_tweets(user.id, "Tweets", count=50)
            for tweet in tweets:
                try:
                    pub_dt = tweet.created_at_datetime
                    if not _is_recent(pub_dt):
                        continue
                    content = tweet.full_text or tweet.text or ""
                    url = f"https://x.com/{handle}/status/{tweet.id}"
                    content_hash = _hash(f"{handle}:{tweet.id}:{content[:200]}")
                    post_id = db.insert_post(
                        source_type="twitter",
                        author=handle,
                        content=content,
                        url=url,
                        published_at=pub_dt.isoformat() if pub_dt else None,
                        content_hash=content_hash,
                    )
                    if post_id:
                        total += 1
                except Exception as e:
                    logger.warning("Error processing tweet from @%s: %s", handle, e)
        except Exception as e:
            if _looks_like_auth_error(e):
                logger.error(
                    "Twitter auth error for @%s — cookies may be expired. "
                    "Re-export from your browser to %s and restart the worker. Error: %s",
                    handle, COOKIES_PATH, e,
                )
                break  # No point continuing — all handles will fail
            logger.error("Error fetching tweets for @%s: %s", handle, e)

    logger.info("Scraped %d new tweets from %d handles", total, len(handles))
    return total


# --- Substack ---

def scrape_substack(rss_urls: list[str]) -> int:
    if not rss_urls:
        return 0

    total = 0
    for url in rss_urls:
        try:
            feed = feedparser.parse(url)
            author = feed.feed.get("title", url)
            for entry in feed.entries:
                try:
                    pub_time = entry.get("published_parsed")
                    if pub_time:
                        pub_dt = datetime(*pub_time[:6], tzinfo=timezone.utc)
                    else:
                        pub_dt = None

                    if pub_dt and not _is_recent(pub_dt):
                        continue

                    content = entry.get("summary", "") or entry.get("content", [{}])[0].get("value", "")
                    entry_url = entry.get("link", url)
                    content_hash = _hash(f"{url}:{entry.get('id', entry_url)}:{content[:200]}")

                    post_id = db.insert_post(
                        source_type="substack",
                        author=author,
                        content=content,
                        url=entry_url,
                        published_at=pub_dt.isoformat() if pub_dt else None,
                        content_hash=content_hash,
                    )
                    if post_id:
                        total += 1
                except Exception as e:
                    logger.warning("Error processing Substack entry from %s: %s", url, e)
        except Exception as e:
            logger.error("Error fetching Substack feed %s: %s", url, e)

    logger.info("Scraped %d new Substack posts from %d feeds", total, len(rss_urls))
    return total


async def run_scrape() -> dict:
    twitter_handles = db.get_accounts("twitter")
    substack_urls = db.get_accounts("substack")

    if not twitter_handles and not substack_urls:
        logger.warning("No accounts configured — add Twitter handles or Substack URLs via the web UI")
        return {"twitter": 0, "substack": 0}

    twitter_count = await scrape_twitter(twitter_handles)
    substack_count = scrape_substack(substack_urls)

    return {"twitter": twitter_count, "substack": substack_count}
