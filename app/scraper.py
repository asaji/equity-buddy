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


def _patch_twikit_client(client) -> None:
    """Pre-populate client._transaction so twikit never fetches x.com to parse KEY_BYTES.

    twikit lazily initialises self._transaction on the first request by
    fetching https://x.com and parsing obfuscated JS. Twitter changes this
    JS often, breaking the parser. By injecting a stub that returns a random
    hex string, twikit skips that fetch entirely and the header is accepted
    fine (it's analytics-only, not auth).
    """
    import secrets

    class _StubTransaction:
        def get_transaction_id(self, *args, **kwargs) -> str:
            return secrets.token_hex(32)

    if hasattr(client, "_transaction"):
        client._transaction = _StubTransaction()
        logger.info("twikit transaction-ID patch applied (pre-populated _transaction)")
    else:
        logger.warning("client._transaction attribute not found — patch may not work")


def _read_cookies_as_dict() -> Optional[dict]:
    """Read the cookie file and return a {name: value} dict regardless of source format.

    Handles both Cookie-Editor browser export (list of objects) and twikit
    native format (dict or list of [name, value] pairs).
    """
    if not os.path.exists(COOKIES_PATH):
        return None
    try:
        with open(COOKIES_PATH) as f:
            raw = json.load(f)

        if isinstance(raw, dict) and raw:
            return raw

        if isinstance(raw, list) and raw:
            first = raw[0]
            if isinstance(first, dict) and "name" in first:
                # Cookie-Editor format: [{name, value, domain, ...}, ...]
                cookies = {
                    c["name"]: c["value"]
                    for c in raw
                    if isinstance(c, dict) and "name" in c and "value" in c
                }
                logger.info("Parsed browser cookie export (%d cookies)", len(cookies))
                return cookies or None
            if isinstance(first, (list, tuple)) and len(first) == 2:
                # twikit list-of-pairs format: [[name, value], ...]
                return dict(raw)

        logger.error("Unrecognized cookie file format (type=%s)", type(raw).__name__)
        return None
    except Exception as e:
        logger.error("Failed to read cookie file: %s", e)
        return None


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

    cookies = _read_cookies_as_dict()
    if not cookies:
        logger.warning(
            "Twitter cookies not found or invalid at %s — "
            "export cookies from your browser (Cookie-Editor extension → Export as JSON) "
            "and save them there, or use /auth/twitter",
            COOKIES_PATH,
        )
        return None

    if "auth_token" not in cookies:
        logger.warning("auth_token not found in cookies — scraping will likely fail")

    client = Client("en-US")
    _patch_twikit_client(client)

    try:
        # Bypass client.load_cookies() — set directly on the underlying httpx client
        # to avoid format-version mismatches
        http = getattr(client, "http", None) or getattr(client, "_http", None)
        if http is not None:
            http.cookies.update(cookies)
        else:
            # Last resort: let twikit try with the dict written to a temp file
            import tempfile
            with tempfile.NamedTemporaryFile("w", suffix=".json", delete=False) as tmp:
                json.dump(cookies, tmp)
                tmp_path = tmp.name
            client.load_cookies(tmp_path)
            os.unlink(tmp_path)

        logger.info("Twitter cookies loaded (%d cookies, auth_token=%s)",
                    len(cookies), "auth_token" in cookies)
        return client
    except Exception as e:
        logger.error("Failed to apply Twitter cookies: %s", e)
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
