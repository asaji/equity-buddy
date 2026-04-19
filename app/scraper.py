import asyncio
import hashlib
import logging
import os
from datetime import datetime, timedelta, timezone
from typing import Optional

import feedparser

from . import database as db
from .config import get as get_config

logger = logging.getLogger(__name__)

COOKIES_PATH = "/app/cookies/twitter_cookies.json"


def _hash(text: str) -> str:
    return hashlib.sha256(text.encode()).hexdigest()


def _is_recent(dt: Optional[datetime], hours: int = 24) -> bool:
    if dt is None:
        return True
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=timezone.utc)
    return dt >= datetime.now(timezone.utc) - timedelta(hours=hours)


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
        client = Client("en-US")
        if os.path.exists(COOKIES_PATH):
            client.load_cookies(COOKIES_PATH)
            return client
        else:
            logger.warning(
                "Twitter cookies not found at %s — navigate to /auth/twitter to authenticate",
                COOKIES_PATH,
            )
            return None
    except ImportError:
        logger.error("twikit not installed")
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
    cfg = get_config()
    twitter_handles = cfg["accounts"].get("twitter", [])
    substack_urls = cfg["accounts"].get("substack", [])

    twitter_count = await scrape_twitter(twitter_handles)
    substack_count = scrape_substack(substack_urls)

    return {"twitter": twitter_count, "substack": substack_count}
