import hashlib
import json
import logging
import os
import traceback
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


def _patch_twikit_client(_client) -> None:
    """Patch ClientTransaction.init and generate_transaction_id at the class level.

    From inspecting the installed source:
    - ClientTransaction.init() fetches x.com then calls get_indices() which
      parses an ondemand JS file for KEY_BYTE indices — this breaks when
      Twitter updates the JS.
    - generate_transaction_id() builds the actual header value.

    We replace both: init sets dummy attributes (no network), and
    generate_transaction_id returns a random urlsafe string. Twitter
    accepts any value here — it's used for analytics/dedup, not auth.
    """
    import secrets

    try:
        from twikit.x_client_transaction.transaction import ClientTransaction

        async def _stub_init(self, session, headers):
            self.DEFAULT_ROW_INDEX = 0
            self.DEFAULT_KEY_BYTES_INDICES = [0] * 16
            # Valid base64 so get_key_bytes() won't blow up if called
            self.key = "AAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAA="
            self.key_bytes = [0] * 32
            self.animation_key = "0" * 16

        def _stub_generate(self, *args, **kwargs) -> str:
            return secrets.token_urlsafe(32)

        ClientTransaction.init = _stub_init
        ClientTransaction.generate_transaction_id = _stub_generate

        logger.info("twikit ClientTransaction patched — KEY_BYTE fetch bypassed")
    except Exception as e:
        logger.warning("Could not patch twikit ClientTransaction: %s", e)


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
        handle = handle.lstrip("@")
        try:
            # Use search_tweet instead of get_user_by_screen_name + get_user_tweets
            # to avoid twikit's User object parsing which fails on accounts with
            # no profile URL ('urls' KeyError in User.__init__)
            tweets = await client.search_tweet(f"from:{handle}", "Latest", count=50)
            for tweet in tweets:
                try:
                    pub_dt = getattr(tweet, "created_at_datetime", None)
                    if not _is_recent(pub_dt):
                        continue
                    content = (
                        getattr(tweet, "full_text", None)
                        or getattr(tweet, "text", None)
                        or ""
                    )
                    tweet_id = getattr(tweet, "id", None) or getattr(tweet, "id_str", None)
                    url = f"https://x.com/{handle}/status/{tweet_id}" if tweet_id else ""
                    content_hash = _hash(f"{handle}:{tweet_id}:{content[:200]}")
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
                break
            logger.error("Error fetching tweets for @%s: %s\n%s",
                         handle, e, traceback.format_exc())

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
