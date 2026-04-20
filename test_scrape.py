"""
Run inside the existing container to test tweet fetching end-to-end:
  docker exec equitybuddy-worker python /app/test_scrape.py
"""
import asyncio
import json
import os
import secrets

COOKIES_PATH = "/app/cookies/twitter_cookies.json"
HANDLE = "aleabitoreddit"


def patch_transaction(client):
    class _Stub:
        def get_transaction_id(self, *a, **kw): return secrets.token_urlsafe(32)
    try:
        from twikit.x_client_transaction.transaction import ClientTransaction
        async def _stub_init(self, session, headers):
            self.DEFAULT_ROW_INDEX = 0
            self.DEFAULT_KEY_BYTES_INDICES = [0] * 16
            self.key = "AAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAA="
            self.key_bytes = [0] * 32
            self.animation_key = "0" * 16
        def _stub_gen(self, *a, **kw): return secrets.token_urlsafe(32)
        ClientTransaction.init = _stub_init
        ClientTransaction.generate_transaction_id = _stub_gen
        print("[OK] ClientTransaction patched")
    except Exception as e:
        print(f"[WARN] ClientTransaction patch failed: {e}")


def patch_user():
    try:
        import twikit.user as u
        _orig = u.User.__init__
        def _safe(self, client, data, *a, **kw):
            try:
                _orig(self, client, data, *a, **kw)
            except (KeyError, TypeError):
                if not hasattr(self, "urls"): self.urls = []
                if not hasattr(self, "entities"): self.entities = {}
        u.User.__init__ = _safe
        print("[OK] User patched")
    except Exception as e:
        print(f"[WARN] User patch failed: {e}")


def load_cookies():
    if not os.path.exists(COOKIES_PATH):
        print(f"[FAIL] Cookie file not found: {COOKIES_PATH}")
        return None
    with open(COOKIES_PATH) as f:
        raw = json.load(f)
    if isinstance(raw, list):
        cookies = {c["name"]: c["value"] for c in raw if isinstance(c, dict) and "name" in c}
    elif isinstance(raw, dict):
        cookies = raw
    else:
        print("[FAIL] Unrecognized cookie format")
        return None
    print(f"[OK] Loaded {len(cookies)} cookies, auth_token={'auth_token' in cookies}")
    return cookies


async def main():
    from twikit import Client

    patch_user()
    cookies = load_cookies()
    if not cookies:
        return

    client = Client("en-US")
    patch_transaction(client)

    http = getattr(client, "http", None) or getattr(client, "_http", None)
    if http:
        http.cookies.update(cookies)
        print("[OK] Cookies applied to httpx client")
    else:
        print("[FAIL] Could not find httpx client on twikit Client")
        return

    print(f"\n--- Fetching user @{HANDLE} ---")
    try:
        user = await client.get_user_by_screen_name(HANDLE)
        print(f"[OK] User found: id={user.id}, name={user.name}")
    except Exception as e:
        print(f"[FAIL] get_user_by_screen_name: {e}")
        return

    print(f"\n--- Fetching tweets ---")
    try:
        tweets = await client.get_user_tweets(user.id, "Tweets", count=5)
        count = 0
        for tweet in tweets:
            try:
                text = getattr(tweet, "full_text", None) or getattr(tweet, "text", "")
                tid = getattr(tweet, "id", "?")
                print(f"  Tweet {tid}: {text[:100]!r}")
                count += 1
                if count >= 5:
                    break
            except Exception as e:
                print(f"  [WARN] Error reading tweet: {e}")
        print(f"\n[OK] Got {count} tweets")
    except Exception as e:
        import traceback
        print(f"[FAIL] get_user_tweets: {e}")
        traceback.print_exc()


asyncio.run(main())
