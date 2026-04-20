import logging
import os
import yaml

logger = logging.getLogger(__name__)

CONFIG_PATH = os.environ.get("CONFIG_PATH", "/app/config.yaml")

_config: dict = {}


def load_config() -> dict:
    global _config
    if not os.path.exists(CONFIG_PATH):
        logger.warning("config.yaml not found at %s — using defaults", CONFIG_PATH)
        _config = _defaults()
        return _config
    with open(CONFIG_PATH) as f:
        loaded = yaml.safe_load(f) or {}
    _config = _merge(_defaults(), loaded)
    _warn_missing(_config)
    return _config


def get() -> dict:
    if not _config:
        return load_config()
    return _config


def _defaults() -> dict:
    return {
        "gemini_api_key": "",
        "pushover": {"user_key": "", "api_token": ""},
        "base_url": "",
        "schedule": {
            "scrape_interval_hours": 2,
            "digest_time": "07:00",
            "timezone": "America/Chicago",
        },
        "conviction_alert_threshold": "high",
        "watchlist_alerts": {"gain_thresholds": [20, 50, 100], "loss_threshold": -20},
        "accounts": {"twitter": [], "substack": []},
    }


def _merge(base: dict, override: dict) -> dict:
    result = dict(base)
    for k, v in override.items():
        if isinstance(v, dict) and isinstance(result.get(k), dict):
            result[k] = _merge(result[k], v)
        else:
            result[k] = v
    return result


def _warn_missing(cfg: dict) -> None:
    if not cfg.get("gemini_api_key"):
        logger.warning("gemini_api_key is not set — extraction and enrichment disabled")
    if not cfg["pushover"].get("user_key"):
        logger.warning("Pushover not configured — alerts and digest notifications disabled")
    # Accounts are stored in the DB, not config.yaml — no warning needed here
