import asyncio
import logging
import os
from datetime import date
from typing import Optional

from fastapi import FastAPI, Form, Request, Response
from fastapi.responses import HTMLResponse
from fastapi.templating import Jinja2Templates

from yahooquery import Ticker as YQTicker

from . import database as db
from . import alerts, email_report, enrichment, extractor, scraper, worker
from .config import get as get_config, load_config, save_config

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s %(levelname)s %(name)s — %(message)s",
)
logger = logging.getLogger(__name__)

app = FastAPI(title="EquityBuddy")
templates = Jinja2Templates(directory="templates")


@app.on_event("startup")
async def startup():
    load_config()
    db.init_db()


# --- Dashboard ---

@app.get("/", response_class=HTMLResponse)
async def dashboard(request: Request, date: Optional[str] = None):
    from datetime import date as date_type
    today = date_type.today().isoformat()
    selected_date = date if date else today
    ideas = db.get_ideas_for_date(selected_date)
    stats = db.get_stats()
    enrichments = {
        i["ticker"]: (db.get_enrichment_today(i["ticker"]) or {})
        for i in ideas
    }
    dates = db.get_idea_dates()
    unextracted_count = db.count_posts_for_date(selected_date) - len(
        {i["post_id"] for i in db.get_ideas_for_date(selected_date)}
    )
    consensus = db.get_consensus_signals(selected_date, min_authors=2)
    return templates.TemplateResponse("dashboard.html", {
        "request": request,
        "ideas": ideas,
        "stats": stats,
        "enrichments": enrichments,
        "selected_date": selected_date,
        "today": today,
        "dates": dates,
        "unextracted_count": max(unextracted_count, 0),
        "consensus": consensus,
    })


# --- Ticker detail ---

@app.get("/ticker/{ticker}", response_class=HTMLResponse)
async def ticker_detail(request: Request, ticker: str):
    ticker = ticker.upper()
    ideas = db.get_ideas_for_ticker(ticker)
    enrichment_data = db.get_enrichment_today(ticker) or {}
    return templates.TemplateResponse("ticker.html", {
        "request": request,
        "ticker": ticker,
        "ideas": ideas,
        "enrichment": enrichment_data,
    })


# --- Reports ---

@app.get("/reports", response_class=HTMLResponse)
async def reports_list(request: Request, page: int = 1):
    limit = 20
    offset = (page - 1) * limit
    reports = db.get_reports(limit=limit, offset=offset)
    return templates.TemplateResponse("reports.html", {
        "request": request,
        "reports": reports,
        "page": page,
    })


@app.get("/reports/{report_id}", response_class=HTMLResponse)
async def report_detail(report_id: int):
    report = db.get_report_by_id(report_id)
    if not report:
        return HTMLResponse("<p>Report not found</p>", status_code=404)
    return HTMLResponse(report["html_content"])


# --- Alerts ---

@app.get("/alerts", response_class=HTMLResponse)
async def alerts_log(request: Request):
    alert_list = db.get_alerts(limit=100)
    return templates.TemplateResponse("alerts.html", {
        "request": request,
        "alerts": alert_list,
    })


# --- Watchlist (sources) ---

def _watchlist_context(request: Request) -> dict:
    health_rows = db.get_scrape_health(days=7)
    by_author = {}
    for r in health_rows:
        by_author.setdefault(r["author"], []).append(r)
    return {
        "request": request,
        "twitter_accounts": db.get_accounts_with_status("twitter"),
        "substack_urls": db.get_accounts_with_status("substack"),
        "cookie_status": scraper.get_cookie_status(),
        "scrape_health": by_author,
    }


def _watchlist_response(request: Request):
    return templates.TemplateResponse("watchlist.html",
        _watchlist_context(request),
        headers={"HX-Reswap": "outerHTML", "HX-Retarget": "#watchlist-content"})


@app.get("/watchlist", response_class=HTMLResponse)
async def watchlist(request: Request):
    return templates.TemplateResponse("watchlist.html", _watchlist_context(request))


@app.post("/watchlist/twitter/add", response_class=HTMLResponse)
async def add_twitter(request: Request, handle: str = Form(...)):
    handle = handle.lstrip("@").strip()
    if handle:
        db.add_account("twitter", handle)
    return _watchlist_response(request)


@app.post("/watchlist/twitter/remove", response_class=HTMLResponse)
async def remove_twitter(request: Request, handle: str = Form(...)):
    db.remove_account("twitter", handle.lstrip("@").strip())
    return _watchlist_response(request)


@app.post("/watchlist/substack/add", response_class=HTMLResponse)
async def add_substack(request: Request, url: str = Form(...)):
    url = url.strip()
    if url:
        db.add_account("substack", url)
    return _watchlist_response(request)


@app.post("/watchlist/source/toggle", response_class=HTMLResponse)
async def toggle_source(request: Request, source_type: str = Form(...), value: str = Form(...)):
    accounts = db.get_accounts_with_status(source_type)
    current = next((a["is_active"] for a in accounts if a["value"] == value), 1)
    db.set_account_active(source_type, value, 0 if current else 1)
    return _watchlist_response(request)


@app.post("/watchlist/substack/remove", response_class=HTMLResponse)
async def remove_substack(request: Request, url: str = Form(...)):
    db.remove_account("substack", url.strip())
    return _watchlist_response(request)


# --- Watchlist Stocks ---

@app.get("/watchlist-stocks", response_class=HTMLResponse)
async def watchlist_stocks(request: Request):
    stocks = db.get_active_watchlist_stocks()
    for stock in stocks:
        history = db.get_price_history(stock["ticker"], days=90)
        stock["price_history"] = history
        stock["alerts"] = [
            a for a in db.get_alerts(limit=500)
            if a["ticker"] == stock["ticker"] and a["alert_type"] == "price_milestone"
        ]

    tracking_count = len(stocks)
    beating = sum(1 for s in stocks if (s.get("pct_from_awareness") or 0) > 0)
    avg_return = (
        sum((s.get("pct_from_awareness") or 0) for s in stocks) / tracking_count
        if tracking_count else 0
    )

    return templates.TemplateResponse("watchlist_stocks.html", {
        "request": request,
        "stocks": stocks,
        "tracking_count": tracking_count,
        "beating_count": beating,
        "avg_return": round(avg_return, 2),
    })


@app.post("/watchlist-stocks/add", response_class=HTMLResponse)
async def add_watchlist_stock(
    request: Request,
    ticker: str = Form(...),
    idea_id: Optional[int] = Form(None),
    notes: str = Form(""),
):
    ticker = ticker.upper().strip()
    awareness_price = None
    try:
        t = YQTicker(ticker)
        s = t.summary_detail.get(ticker, {})
        awareness_price = s.get("regularMarketPrice") or s.get("previousClose")
    except Exception as e:
        logger.warning("Could not fetch price for %s: %s", ticker, e)

    db.add_watchlist_stock(ticker=ticker, awareness_price=awareness_price,
                           idea_id=idea_id, notes=notes)
    asyncio.create_task(asyncio.to_thread(worker.backfill_price_history, ticker, awareness_price))

    stocks = db.get_active_watchlist_stocks()
    for stock in stocks:
        stock["price_history"] = db.get_price_history(stock["ticker"], days=90)
        stock["alerts"] = []

    tracking_count = len(stocks)
    beating = sum(1 for s in stocks if (s.get("pct_from_awareness") or 0) > 0)
    avg_return = (
        sum((s.get("pct_from_awareness") or 0) for s in stocks) / tracking_count
        if tracking_count else 0
    )

    return templates.TemplateResponse("watchlist_stocks.html", {
        "request": request,
        "stocks": stocks,
        "tracking_count": tracking_count,
        "beating_count": beating,
        "avg_return": round(avg_return, 2),
    }, headers={"HX-Reswap": "outerHTML", "HX-Retarget": "#watchlist-stocks-content"})


@app.post("/watchlist-stocks/update-notes", response_class=HTMLResponse)
async def update_watchlist_notes(ticker: str = Form(...), notes: str = Form("")):
    ticker = ticker.upper().strip()
    db.update_watchlist_stock_notes(ticker, notes.strip())
    notes_html = notes.strip() or '<span style="color:#2a4060;">no notes</span>'
    html = (
        f'<span id="notes-{ticker}" class="mono text-xs" style="color:#5a7a9a;">{notes_html}</span>'
        f"<button onclick=\"showNotesEdit('{ticker}')\" class=\"mono text-xs ml-2\""
        f' style="color:#2a4060;background:none;border:none;cursor:pointer;"'
        f" onmouseover=\"this.style.color='#5a7a9a'\" onmouseout=\"this.style.color='#2a4060'\">edit</button>"
    )
    return HTMLResponse(html)


@app.post("/watchlist-stocks/refresh-prices", response_class=HTMLResponse)
async def refresh_watchlist_prices(request: Request):
    await asyncio.to_thread(worker.update_watchlist_prices)
    stocks = db.get_active_watchlist_stocks()
    for stock in stocks:
        stock["price_history"] = db.get_price_history(stock["ticker"], days=90)
        stock["alerts"] = [
            a for a in db.get_alerts(limit=500)
            if a["ticker"] == stock["ticker"] and a["alert_type"] == "price_milestone"
        ]
    tracking_count = len(stocks)
    beating = sum(1 for s in stocks if (s.get("pct_from_awareness") or 0) > 0)
    avg_return = (
        sum((s.get("pct_from_awareness") or 0) for s in stocks) / tracking_count
        if tracking_count else 0
    )
    return templates.TemplateResponse("watchlist_stocks.html", {
        "request": request,
        "stocks": stocks,
        "tracking_count": tracking_count,
        "beating_count": beating,
        "avg_return": round(avg_return, 2),
    }, headers={"HX-Reswap": "outerHTML", "HX-Retarget": "#watchlist-stocks-content"})


@app.post("/watchlist-stocks/remove", response_class=HTMLResponse)
async def remove_watchlist_stock(request: Request, ticker: str = Form(...)):
    db.remove_watchlist_stock(ticker.upper().strip())
    stocks = db.get_active_watchlist_stocks()
    for stock in stocks:
        stock["price_history"] = db.get_price_history(stock["ticker"], days=90)
        stock["alerts"] = []

    tracking_count = len(stocks)
    beating = sum(1 for s in stocks if (s.get("pct_from_awareness") or 0) > 0)
    avg_return = (
        sum((s.get("pct_from_awareness") or 0) for s in stocks) / tracking_count
        if tracking_count else 0
    )

    return templates.TemplateResponse("watchlist_stocks.html", {
        "request": request,
        "stocks": stocks,
        "tracking_count": tracking_count,
        "beating_count": beating,
        "avg_return": round(avg_return, 2),
    }, headers={"HX-Reswap": "outerHTML", "HX-Retarget": "#watchlist-stocks-content"})


@app.post("/watchlist-stocks/track-idea", response_class=HTMLResponse)
async def track_idea(ticker: str = Form(...), idea_id: Optional[int] = Form(None)):
    ticker = ticker.upper().strip()
    awareness_price = None
    try:
        t = YQTicker(ticker)
        s = t.summary_detail.get(ticker, {})
        awareness_price = s.get("regularMarketPrice") or s.get("previousClose")
    except Exception:
        pass
    db.add_watchlist_stock(ticker=ticker, awareness_price=awareness_price, idea_id=idea_id, notes="")
    asyncio.create_task(asyncio.to_thread(worker.backfill_price_history, ticker, awareness_price))
    return HTMLResponse(
        f'<span class="text-emerald-400 text-xs font-medium">✓ Tracking</span>'
    )


@app.post("/test-pushover", response_class=HTMLResponse)
async def test_pushover():
    result = await asyncio.to_thread(email_report.send_test_pushover)
    if result == "ok":
        return HTMLResponse(
            '<div class="text-green-400 p-3 rounded bg-green-900/20 border border-green-700 text-sm">'
            "Test notification sent — check your phone."
            "</div>"
        )
    return HTMLResponse(
        f'<div class="text-red-400 p-3 rounded bg-red-900/20 border border-red-700 text-sm">'
        f"{result}"
        "</div>"
    )


@app.post("/generate-report", response_class=HTMLResponse)
async def generate_report(request: Request):
    asyncio.create_task(asyncio.to_thread(email_report.generate_and_send_digest))
    return HTMLResponse(
        '<div class="text-green-400 p-3 rounded bg-green-900/20 border border-green-700 text-sm">'
        "Report generation started — refresh in a moment to see it."
        "</div>"
    )


@app.post("/generate-weekly-report", response_class=HTMLResponse)
async def generate_weekly_report(request: Request):
    asyncio.create_task(asyncio.to_thread(email_report.generate_weekly_report))
    return HTMLResponse(
        '<div class="text-purple-400 p-3 rounded bg-purple-900/20 border border-purple-700 text-sm">'
        "Weekly report generation started — refresh in a moment to see it."
        "</div>"
    )


# --- Settings ---

@app.get("/settings", response_class=HTMLResponse)
async def settings(request: Request):
    cfg = get_config()
    return templates.TemplateResponse("settings.html", {
        "request": request,
        "cfg": cfg,
        "saved": False,
    })


@app.post("/settings", response_class=HTMLResponse)
async def settings_save(
    request: Request,
    base_url: str = Form(""),
    scrape_interval_hours: int = Form(2),
    digest_time: str = Form("07:00"),
    weekly_digest_day: str = Form("sun"),
    weekly_digest_time: str = Form("08:00"),
    timezone: str = Form("America/Chicago"),
    conviction_alert_threshold: str = Form("high"),
    gain_thresholds: str = Form("20,50,100"),
    loss_threshold: float = Form(-20),
):
    try:
        thresholds = [int(x.strip()) for x in gain_thresholds.split(",") if x.strip()]
    except ValueError:
        thresholds = [20, 50, 100]

    updates = {
        "base_url": base_url.strip(),
        "schedule": {
            "scrape_interval_hours": scrape_interval_hours,
            "digest_time": digest_time.strip(),
            "weekly_digest_day": weekly_digest_day.strip(),
            "weekly_digest_time": weekly_digest_time.strip(),
            "timezone": timezone.strip(),
        },
        "conviction_alert_threshold": conviction_alert_threshold,
        "watchlist_alerts": {
            "gain_thresholds": thresholds,
            "loss_threshold": loss_threshold,
        },
    }
    save_config(updates)
    cfg = get_config()
    return templates.TemplateResponse("settings.html", {
        "request": request,
        "cfg": cfg,
        "saved": True,
    })


# --- Search ---

@app.get("/search", response_class=HTMLResponse)
async def search(request: Request, q: str = ""):
    results = db.search_ideas(q) if q.strip() else []
    return templates.TemplateResponse("search.html", {
        "request": request,
        "q": q,
        "results": results,
    })


# --- Discovery ---

@app.get("/trending", response_class=HTMLResponse)
async def trending(request: Request, days: int = 7):
    tickers = db.get_trending_tickers(days=days)
    return templates.TemplateResponse("trending.html", {
        "request": request,
        "tickers": tickers,
        "days": days,
    })


@app.get("/leaderboard", response_class=HTMLResponse)
async def leaderboard(request: Request):
    authors = db.get_author_leaderboard()
    return templates.TemplateResponse("leaderboard.html", {
        "request": request,
        "authors": authors,
    })


# --- Manual run ---

@app.post("/re-extract", response_class=HTMLResponse)
async def re_extract(request: Request, date: str = Form(...)):
    posts = db.get_unextracted_posts_for_date(date)
    if not posts:
        return HTMLResponse(
            '<div class="mono text-xs mt-2" style="color:#2a4060;">'
            f'No unextracted posts found for {date}.'
            '</div>'
        )
    count = await asyncio.to_thread(extractor.extract_ideas_from_posts, posts)
    color = "#00d97e" if count > 0 else "#5a7a9a"
    return HTMLResponse(
        f'<div class="mono text-xs mt-2" style="color:{color};">'
        f'Extracted {count} new idea{"s" if count != 1 else ""} from {len(posts)} unprocessed post{"s" if len(posts) != 1 else ""}.'
        f' Refresh to see them.'
        '</div>'
    )


@app.post("/run-now", response_class=HTMLResponse)
async def run_now(request: Request):
    asyncio.create_task(worker.run_full_cycle())
    return HTMLResponse(
        '<div class="text-green-400 p-4 rounded bg-green-900/20 border border-green-700">'
        "Scrape + extract + enrich cycle started. Check logs for progress."
        "</div>"
    )


# --- Twitter Auth ---

@app.get("/auth/twitter", response_class=HTMLResponse)
async def auth_twitter_form(request: Request):
    return templates.TemplateResponse("base.html", {
        "request": request,
        "page": "auth_twitter",
    })


@app.post("/auth/twitter", response_class=HTMLResponse)
async def auth_twitter_submit(
    request: Request,
    username: str = Form(...),
    password: str = Form(...),
    email: str = Form(...),
):
    success = await scraper.auth_twitter(username, password, email)
    if success:
        return HTMLResponse(
            '<div class="text-green-400 p-4 rounded bg-green-900/20 border border-green-700">'
            "Twitter authentication successful. Cookies saved. The worker will now be able to scrape Twitter."
            "</div>"
        )
    return HTMLResponse(
        '<div class="text-red-400 p-4 rounded bg-red-900/20 border border-red-700">'
        "Twitter authentication failed. Check your credentials and try again. "
        "See container logs for details."
        "</div>"
    )
