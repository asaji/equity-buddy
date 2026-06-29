import logging
import os
import sqlite3
from contextlib import contextmanager
from datetime import date, datetime, timedelta, timezone
from typing import Generator, Optional

logger = logging.getLogger(__name__)

DB_PATH = os.environ.get("DB_PATH", "/app/data/equitybuddy.db")

SCHEMA = """
CREATE TABLE IF NOT EXISTS accounts (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    source_type TEXT NOT NULL,
    value TEXT NOT NULL,
    added_at TEXT NOT NULL,
    is_active INTEGER NOT NULL DEFAULT 1,
    UNIQUE(source_type, value)
);

CREATE TABLE IF NOT EXISTS posts (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    source_type TEXT NOT NULL,
    author TEXT NOT NULL,
    content TEXT NOT NULL,
    url TEXT,
    published_at TEXT,
    fetched_at TEXT NOT NULL,
    content_hash TEXT UNIQUE NOT NULL
);

CREATE TABLE IF NOT EXISTS ideas (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    post_id INTEGER NOT NULL REFERENCES posts(id),
    ticker TEXT NOT NULL,
    conviction TEXT NOT NULL,
    sentiment TEXT NOT NULL,
    thesis TEXT,
    quote TEXT,
    idea_type TEXT NOT NULL DEFAULT 'explicit',
    extracted_at TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS enrichment (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    ticker TEXT NOT NULL,
    date TEXT NOT NULL,
    current_price REAL,
    week52_high REAL,
    week52_low REAL,
    pct_change_1w REAL,
    pct_change_1m REAL,
    pct_change_3m REAL,
    volume INTEGER,
    avg_volume_30d INTEGER,
    market_cap REAL,
    pe_ratio REAL,
    revenue_growth_yoy REAL,
    short_interest REAL,
    freshness_signal TEXT,
    research_summary TEXT,
    enriched_at TEXT NOT NULL,
    UNIQUE(ticker, date)
);

CREATE TABLE IF NOT EXISTS alerts (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    ticker TEXT NOT NULL,
    alert_type TEXT NOT NULL,
    idea_id INTEGER REFERENCES ideas(id),
    message TEXT NOT NULL,
    sent_at TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS reports (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    subject TEXT NOT NULL,
    html_content TEXT NOT NULL,
    generated_at TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS watchlist_stocks (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    ticker TEXT NOT NULL,
    added_date TEXT NOT NULL,
    awareness_price REAL,
    idea_id INTEGER REFERENCES ideas(id),
    notes TEXT,
    is_active INTEGER NOT NULL DEFAULT 1
);

CREATE TABLE IF NOT EXISTS price_history (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    ticker TEXT NOT NULL,
    date TEXT NOT NULL,
    close_price REAL NOT NULL,
    pct_from_awareness REAL,
    volume INTEGER,
    UNIQUE(ticker, date)
);
"""


def init_db() -> None:
    os.makedirs(os.path.dirname(DB_PATH), exist_ok=True)
    with _conn() as conn:
        conn.executescript(SCHEMA)
        # Migrations
        try:
            conn.execute("ALTER TABLE accounts ADD COLUMN is_active INTEGER NOT NULL DEFAULT 1")
        except Exception:
            pass
        try:
            conn.execute("ALTER TABLE ideas ADD COLUMN idea_type TEXT NOT NULL DEFAULT 'explicit'")
        except Exception:
            pass
    logger.info("Database initialized at %s", DB_PATH)


@contextmanager
def _conn() -> Generator[sqlite3.Connection, None, None]:
    conn = sqlite3.connect(DB_PATH, timeout=30)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA journal_mode=WAL")
    conn.execute("PRAGMA foreign_keys=ON")
    try:
        yield conn
        conn.commit()
    except Exception:
        conn.rollback()
        raise
    finally:
        conn.close()


def _now() -> str:
    return datetime.now(timezone.utc).isoformat()


# --- Posts ---

def insert_post(source_type: str, author: str, content: str, url: str,
                published_at: Optional[str], content_hash: str) -> Optional[int]:
    try:
        with _conn() as conn:
            cur = conn.execute(
                """INSERT INTO posts (source_type, author, content, url, published_at, fetched_at, content_hash)
                   VALUES (?, ?, ?, ?, ?, ?, ?)""",
                (source_type, author, content, url, published_at, _now(), content_hash),
            )
            return cur.lastrowid
    except sqlite3.IntegrityError:
        return None  # duplicate


def get_posts_since(hours: int = 24) -> list[dict]:
    cutoff = (datetime.now(timezone.utc) - timedelta(hours=hours)).isoformat()
    with _conn() as conn:
        rows = conn.execute(
            "SELECT * FROM posts WHERE fetched_at >= ? ORDER BY fetched_at DESC", (cutoff,)
        ).fetchall()
    return [dict(r) for r in rows]


def get_unextracted_posts(hours: int = 24) -> list[dict]:
    cutoff = (datetime.now(timezone.utc) - timedelta(hours=hours)).isoformat()
    with _conn() as conn:
        rows = conn.execute(
            """SELECT p.* FROM posts p
               LEFT JOIN ideas i ON i.post_id = p.id
               WHERE p.fetched_at >= ? AND i.id IS NULL
               ORDER BY p.fetched_at DESC""",
            (cutoff,)
        ).fetchall()
    return [dict(r) for r in rows]


def get_unextracted_posts_for_date(date_str: str) -> list[dict]:
    with _conn() as conn:
        rows = conn.execute(
            """SELECT p.* FROM posts p
               LEFT JOIN ideas i ON i.post_id = p.id
               WHERE date(p.fetched_at) = ? AND i.id IS NULL
               ORDER BY p.fetched_at DESC""",
            (date_str,)
        ).fetchall()
    return [dict(r) for r in rows]


def count_posts_for_date(date_str: str) -> int:
    with _conn() as conn:
        return conn.execute(
            "SELECT COUNT(*) FROM posts WHERE date(fetched_at) = ?", (date_str,)
        ).fetchone()[0]


# --- Ideas ---

def insert_idea(post_id: int, ticker: str, conviction: str, sentiment: str,
                thesis: str, quote: str, idea_type: str = "explicit") -> int:
    with _conn() as conn:
        cur = conn.execute(
            """INSERT INTO ideas (post_id, ticker, conviction, sentiment, thesis, quote, idea_type, extracted_at)
               VALUES (?, ?, ?, ?, ?, ?, ?, ?)""",
            (post_id, ticker, conviction, sentiment, thesis, quote, idea_type, _now()),
        )
        return cur.lastrowid


def get_ideas_today() -> list[dict]:
    return get_ideas_for_date(date.today().isoformat())


def get_ideas_for_date(date_str: str) -> list[dict]:
    with _conn() as conn:
        rows = conn.execute(
            """SELECT i.*, p.author, p.source_type, p.url, p.published_at
               FROM ideas i JOIN posts p ON i.post_id = p.id
               WHERE date(i.extracted_at) = ?
               ORDER BY i.extracted_at DESC""",
            (date_str,),
        ).fetchall()
    return [dict(r) for r in rows]


def get_idea_dates() -> list[str]:
    with _conn() as conn:
        rows = conn.execute(
            "SELECT DISTINCT date(extracted_at) AS d FROM ideas ORDER BY d DESC LIMIT 30"
        ).fetchall()
    return [r["d"] for r in rows]


def get_scrape_health(days: int = 7) -> list[dict]:
    cutoff = (datetime.now(timezone.utc) - timedelta(days=days)).isoformat()
    with _conn() as conn:
        rows = conn.execute(
            """SELECT author, source_type, date(fetched_at) as day, COUNT(*) as post_count
               FROM posts
               WHERE fetched_at >= ?
               GROUP BY author, day
               ORDER BY author, day DESC""",
            (cutoff,),
        ).fetchall()
    return [dict(r) for r in rows]


def get_ideas_for_ticker(ticker: str) -> list[dict]:
    with _conn() as conn:
        rows = conn.execute(
            """SELECT i.*, p.author, p.source_type, p.url, p.published_at
               FROM ideas i JOIN posts p ON i.post_id = p.id
               WHERE i.ticker = ?
               ORDER BY i.extracted_at DESC""",
            (ticker.upper(),),
        ).fetchall()
    return [dict(r) for r in rows]


def get_ideas_for_period(days: int = 7) -> list[dict]:
    cutoff = (datetime.now(timezone.utc) - timedelta(days=days)).isoformat()
    with _conn() as conn:
        rows = conn.execute(
            """SELECT i.*, p.author, p.source_type, p.url, p.published_at
               FROM ideas i JOIN posts p ON i.post_id = p.id
               WHERE i.extracted_at >= ?
               ORDER BY i.extracted_at DESC""",
            (cutoff,),
        ).fetchall()
    return [dict(r) for r in rows]


def get_ideas_since(hours: int = 24) -> list[dict]:
    cutoff = (datetime.now(timezone.utc) - timedelta(hours=hours)).isoformat()
    with _conn() as conn:
        rows = conn.execute(
            """SELECT i.*, p.author, p.source_type, p.url, p.published_at
               FROM ideas i JOIN posts p ON i.post_id = p.id
               WHERE i.extracted_at >= ?
               ORDER BY i.extracted_at DESC""",
            (cutoff,),
        ).fetchall()
    return [dict(r) for r in rows]


def ticker_extracted_for_author_recently(ticker: str, author: str, hours: int = 24) -> bool:
    cutoff = (datetime.now(timezone.utc) - timedelta(hours=hours)).isoformat()
    with _conn() as conn:
        row = conn.execute(
            """SELECT 1 FROM ideas i JOIN posts p ON i.post_id = p.id
               WHERE i.ticker = ? AND p.author = ? AND i.extracted_at >= ?""",
            (ticker, author, cutoff),
        ).fetchone()
    return row is not None


def get_idea_by_id(idea_id: int) -> Optional[dict]:
    with _conn() as conn:
        row = conn.execute(
            """SELECT i.*, p.author, p.source_type, p.url, p.published_at
               FROM ideas i JOIN posts p ON i.post_id = p.id
               WHERE i.id = ?""",
            (idea_id,),
        ).fetchone()
    return dict(row) if row else None


# --- Enrichment ---

def get_enrichment_today(ticker: str) -> Optional[dict]:
    today = date.today().isoformat()
    with _conn() as conn:
        row = conn.execute(
            "SELECT * FROM enrichment WHERE ticker = ? AND date = ?", (ticker, today)
        ).fetchone()
    return dict(row) if row else None


def upsert_enrichment(ticker: str, data: dict) -> None:
    today = date.today().isoformat()
    with _conn() as conn:
        conn.execute(
            """INSERT INTO enrichment
               (ticker, date, current_price, week52_high, week52_low, pct_change_1w,
                pct_change_1m, pct_change_3m, volume, avg_volume_30d, market_cap,
                pe_ratio, revenue_growth_yoy, short_interest, freshness_signal,
                research_summary, enriched_at)
               VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)
               ON CONFLICT(ticker, date) DO UPDATE SET
                 current_price=excluded.current_price,
                 week52_high=excluded.week52_high,
                 week52_low=excluded.week52_low,
                 pct_change_1w=excluded.pct_change_1w,
                 pct_change_1m=excluded.pct_change_1m,
                 pct_change_3m=excluded.pct_change_3m,
                 volume=excluded.volume,
                 avg_volume_30d=excluded.avg_volume_30d,
                 market_cap=excluded.market_cap,
                 pe_ratio=excluded.pe_ratio,
                 revenue_growth_yoy=excluded.revenue_growth_yoy,
                 short_interest=excluded.short_interest,
                 freshness_signal=excluded.freshness_signal,
                 research_summary=excluded.research_summary,
                 enriched_at=excluded.enriched_at""",
            (
                ticker, today,
                data.get("current_price"), data.get("week52_high"), data.get("week52_low"),
                data.get("pct_change_1w"), data.get("pct_change_1m"), data.get("pct_change_3m"),
                data.get("volume"), data.get("avg_volume_30d"), data.get("market_cap"),
                data.get("pe_ratio"), data.get("revenue_growth_yoy"), data.get("short_interest"),
                data.get("freshness_signal"), data.get("research_summary"), _now(),
            ),
        )


# --- Alerts ---

def insert_alert(ticker: str, alert_type: str, message: str,
                 idea_id: Optional[int] = None) -> int:
    with _conn() as conn:
        cur = conn.execute(
            "INSERT INTO alerts (ticker, alert_type, idea_id, message, sent_at) VALUES (?,?,?,?,?)",
            (ticker, alert_type, idea_id, message, _now()),
        )
        return cur.lastrowid


def get_alerts(limit: int = 100) -> list[dict]:
    with _conn() as conn:
        rows = conn.execute(
            "SELECT * FROM alerts ORDER BY sent_at DESC LIMIT ?", (limit,)
        ).fetchall()
    return [dict(r) for r in rows]


def price_milestone_already_fired(ticker: str, threshold: float) -> bool:
    label = f"milestone_{threshold}"
    with _conn() as conn:
        row = conn.execute(
            "SELECT 1 FROM alerts WHERE ticker = ? AND alert_type = ? AND message LIKE ?",
            (ticker, "price_milestone", f"%{label}%"),
        ).fetchone()
    return row is not None


# --- Reports ---

def insert_report(subject: str, html_content: str) -> int:
    with _conn() as conn:
        cur = conn.execute(
            "INSERT INTO reports (subject, html_content, generated_at) VALUES (?,?,?)",
            (subject, html_content, _now()),
        )
        return cur.lastrowid


def get_reports(limit: int = 50, offset: int = 0) -> list[dict]:
    with _conn() as conn:
        rows = conn.execute(
            "SELECT id, subject, generated_at FROM reports ORDER BY generated_at DESC LIMIT ? OFFSET ?",
            (limit, offset),
        ).fetchall()
    return [dict(r) for r in rows]


def get_report_by_id(report_id: int) -> Optional[dict]:
    with _conn() as conn:
        row = conn.execute("SELECT * FROM reports WHERE id = ?", (report_id,)).fetchone()
    return dict(row) if row else None


# --- Watchlist Stocks ---

def add_watchlist_stock(ticker: str, awareness_price: Optional[float],
                        idea_id: Optional[int] = None, notes: str = "") -> int:
    with _conn() as conn:
        cur = conn.execute(
            """INSERT INTO watchlist_stocks (ticker, added_date, awareness_price, idea_id, notes)
               VALUES (?,?,?,?,?)""",
            (ticker, date.today().isoformat(), awareness_price, idea_id, notes),
        )
        return cur.lastrowid


def remove_watchlist_stock(ticker: str) -> None:
    with _conn() as conn:
        conn.execute(
            "UPDATE watchlist_stocks SET is_active = 0 WHERE ticker = ? AND is_active = 1",
            (ticker,),
        )


def update_watchlist_stock_notes(ticker: str, notes: str) -> None:
    with _conn() as conn:
        conn.execute(
            "UPDATE watchlist_stocks SET notes = ? WHERE ticker = ? AND is_active = 1",
            (notes, ticker),
        )


def get_active_watchlist_stocks() -> list[dict]:
    with _conn() as conn:
        rows = conn.execute(
            """SELECT ws.*, i.thesis, i.sentiment, p.author, p.published_at as mention_date
               FROM watchlist_stocks ws
               LEFT JOIN ideas i ON ws.idea_id = i.id
               LEFT JOIN posts p ON i.post_id = p.id
               WHERE ws.is_active = 1
               ORDER BY ws.added_date DESC""",
        ).fetchall()
    return [dict(r) for r in rows]


def insert_price_history(ticker: str, close_price: float,
                         pct_from_awareness: Optional[float], volume: Optional[int],
                         date_str: Optional[str] = None) -> None:
    day = date_str or date.today().isoformat()
    with _conn() as conn:
        conn.execute(
            """INSERT INTO price_history (ticker, date, close_price, pct_from_awareness, volume)
               VALUES (?,?,?,?,?)
               ON CONFLICT(ticker, date) DO UPDATE SET
                 close_price=excluded.close_price,
                 pct_from_awareness=excluded.pct_from_awareness,
                 volume=excluded.volume""",
            (ticker, day, close_price, pct_from_awareness, volume),
        )


def get_price_history(ticker: str, days: int = 90) -> list[dict]:
    cutoff = (date.today() - timedelta(days=days)).isoformat()
    with _conn() as conn:
        rows = conn.execute(
            "SELECT * FROM price_history WHERE ticker = ? AND date >= ? ORDER BY date ASC",
            (ticker, cutoff),
        ).fetchall()
    return [dict(r) for r in rows]


# --- Stats ---

def get_stats() -> dict:
    with _conn() as conn:
        ideas_today = conn.execute(
            "SELECT COUNT(*) FROM ideas WHERE date(extracted_at) = ?", (date.today().isoformat(),)
        ).fetchone()[0]
        alerts_sent = conn.execute(
            "SELECT COUNT(*) FROM alerts WHERE date(sent_at) = ?", (date.today().isoformat(),)
        ).fetchone()[0]
        sources_active = conn.execute(
            """SELECT COUNT(DISTINCT author) FROM posts
               WHERE fetched_at >= ?""",
            ((datetime.now(timezone.utc) - timedelta(hours=24)).isoformat(),),
        ).fetchone()[0]
    return {"ideas_today": ideas_today, "alerts_sent": alerts_sent, "sources_active": sources_active}


# --- Account management (stored in DB so both containers share it) ---

def get_accounts(source_type: str) -> list[str]:
    """Return only active account values — used by the scraper."""
    with _conn() as conn:
        rows = conn.execute(
            "SELECT value FROM accounts WHERE source_type = ? AND is_active = 1 ORDER BY added_at",
            (source_type,),
        ).fetchall()
    return [r[0] for r in rows]


def get_accounts_with_status(source_type: str) -> list[dict]:
    """Return all accounts (active + paused) with their status — used by the UI."""
    with _conn() as conn:
        rows = conn.execute(
            "SELECT value, is_active FROM accounts WHERE source_type = ? ORDER BY added_at",
            (source_type,),
        ).fetchall()
    return [dict(r) for r in rows]


def set_account_active(source_type: str, value: str, is_active: int) -> None:
    with _conn() as conn:
        conn.execute(
            "UPDATE accounts SET is_active = ? WHERE source_type = ? AND value = ?",
            (is_active, source_type, value),
        )


def add_account(source_type: str, value: str) -> None:
    with _conn() as conn:
        conn.execute(
            "INSERT OR IGNORE INTO accounts (source_type, value, added_at) VALUES (?, ?, ?)",
            (source_type, value, _now()),
        )


def remove_account(source_type: str, value: str) -> None:
    with _conn() as conn:
        conn.execute(
            "DELETE FROM accounts WHERE source_type = ? AND value = ?",
            (source_type, value),
        )


# --- Search ---

def search_ideas(query: str, limit: int = 50) -> list[dict]:
    q = query.strip()
    ticker_q = q.upper()
    like_q = f"%{q}%"
    with _conn() as conn:
        rows = conn.execute(
            """SELECT i.*, p.author, p.source_type, p.url, p.published_at
               FROM ideas i JOIN posts p ON i.post_id = p.id
               WHERE i.ticker = ?
                  OR i.ticker LIKE ?
                  OR i.thesis LIKE ?
                  OR i.quote LIKE ?
               ORDER BY i.extracted_at DESC
               LIMIT ?""",
            (ticker_q, ticker_q + "%", like_q, like_q, limit),
        ).fetchall()
    return [dict(r) for r in rows]


# --- Consensus ---

def get_consensus_signals(date_str: str, min_authors: int = 2) -> list[dict]:
    with _conn() as conn:
        rows = conn.execute(
            """SELECT
                 i.ticker,
                 COUNT(DISTINCT p.author) as author_count,
                 COUNT(*) as mention_count,
                 GROUP_CONCAT(DISTINCT p.author) as authors,
                 SUM(CASE WHEN i.sentiment='bullish' THEN 1 ELSE 0 END) as bullish,
                 SUM(CASE WHEN i.sentiment='bearish' THEN 1 ELSE 0 END) as bearish,
                 MAX(i.conviction) as top_conviction
               FROM ideas i JOIN posts p ON i.post_id = p.id
               WHERE date(i.extracted_at) = ?
               GROUP BY i.ticker
               HAVING author_count >= ?
               ORDER BY author_count DESC, mention_count DESC""",
            (date_str, min_authors),
        ).fetchall()
    return [dict(r) for r in rows]


# --- Discovery ---

def get_trending_tickers(days: int = 7) -> list[dict]:
    cutoff = (datetime.now(timezone.utc) - timedelta(days=days)).isoformat()
    with _conn() as conn:
        rows = conn.execute(
            """SELECT
                 i.ticker,
                 COUNT(*) as mention_count,
                 COUNT(DISTINCT p.author) as source_count,
                 SUM(CASE WHEN i.sentiment = 'bullish' THEN 1 ELSE 0 END) as bullish_count,
                 SUM(CASE WHEN i.sentiment = 'bearish' THEN 1 ELSE 0 END) as bearish_count,
                 SUM(CASE WHEN i.conviction = 'high' THEN 1 ELSE 0 END) as high_conviction_count,
                 GROUP_CONCAT(DISTINCT p.author) as authors,
                 MAX(i.extracted_at) as last_seen
               FROM ideas i JOIN posts p ON i.post_id = p.id
               WHERE i.extracted_at >= ?
               GROUP BY i.ticker
               ORDER BY mention_count DESC, source_count DESC
               LIMIT 50""",
            (cutoff,),
        ).fetchall()
    return [dict(r) for r in rows]


def get_author_leaderboard() -> list[dict]:
    """Return authors ranked by avg % return on their tracked ideas."""
    with _conn() as conn:
        rows = conn.execute(
            """WITH latest_price AS (
                 SELECT ticker, pct_from_awareness, date
                 FROM price_history
                 WHERE (ticker, date) IN (
                   SELECT ticker, MAX(date) FROM price_history GROUP BY ticker
                 )
               ),
               author_mentions AS (
                 SELECT p.author, COUNT(DISTINCT i.ticker) as total_tickers_mentioned
                 FROM posts p JOIN ideas i ON i.post_id = p.id
                 GROUP BY p.author
               )
               SELECT
                 p.author,
                 COUNT(DISTINCT ws.id) as ideas_tracked,
                 AVG(lp.pct_from_awareness) as avg_return,
                 MAX(lp.pct_from_awareness) as best_return,
                 MIN(lp.pct_from_awareness) as worst_return,
                 COUNT(DISTINCT CASE WHEN lp.pct_from_awareness > 0 THEN ws.id END) as winners,
                 am.total_tickers_mentioned
               FROM posts p
               JOIN ideas i ON i.post_id = p.id
               JOIN author_mentions am ON am.author = p.author
               LEFT JOIN watchlist_stocks ws ON ws.idea_id = i.id AND ws.is_active = 1
               LEFT JOIN latest_price lp ON lp.ticker = ws.ticker
               GROUP BY p.author
               HAVING am.total_tickers_mentioned > 0
               ORDER BY ideas_tracked DESC, avg_return DESC""",
        ).fetchall()
    return [dict(r) for r in rows]
