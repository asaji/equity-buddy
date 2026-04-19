import asyncio
import logging
import os
from datetime import date
from typing import Optional

import yfinance as yf
from fastapi import FastAPI, Form, Request, Response
from fastapi.responses import HTMLResponse
from fastapi.templating import Jinja2Templates

from . import database as db
from . import alerts, enrichment, scraper, worker
from .config import get as get_config, load_config

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
async def dashboard(request: Request):
    ideas = db.get_ideas_today()
    stats = db.get_stats()
    enrichments = {e["ticker"]: e for e in [
        db.get_enrichment_today(i["ticker"]) or {} for i in ideas
    ]}
    return templates.TemplateResponse("dashboard.html", {
        "request": request,
        "ideas": ideas,
        "stats": stats,
        "enrichments": enrichments,
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

@app.get("/watchlist", response_class=HTMLResponse)
async def watchlist(request: Request):
    cfg = get_config()
    twitter = cfg["accounts"].get("twitter", [])
    substack = cfg["accounts"].get("substack", [])
    return templates.TemplateResponse("watchlist.html", {
        "request": request,
        "twitter_accounts": twitter,
        "substack_urls": substack,
    })


def _save_accounts(twitter: list, substack: list) -> None:
    import yaml
    cfg = get_config()
    cfg["accounts"]["twitter"] = twitter
    cfg["accounts"]["substack"] = substack
    config_path = os.environ.get("CONFIG_PATH", "/app/config.yaml")
    if os.path.exists(config_path) and os.access(config_path, os.W_OK):
        with open(config_path, "w") as f:
            yaml.dump(cfg, f, default_flow_style=False)
        load_config()
    else:
        logger.warning("Config file not writable — account changes stored in memory only")


@app.post("/watchlist/twitter/add", response_class=HTMLResponse)
async def add_twitter(request: Request, handle: str = Form(...)):
    cfg = get_config()
    twitter = list(cfg["accounts"].get("twitter", []))
    handle = handle.lstrip("@").strip()
    if handle and handle not in twitter:
        twitter.append(handle)
        _save_accounts(twitter, cfg["accounts"].get("substack", []))
    cfg = get_config()
    return templates.TemplateResponse("watchlist.html", {
        "request": request,
        "twitter_accounts": cfg["accounts"].get("twitter", []),
        "substack_urls": cfg["accounts"].get("substack", []),
    }, headers={"HX-Reswap": "outerHTML", "HX-Retarget": "#watchlist-content"})


@app.post("/watchlist/twitter/remove", response_class=HTMLResponse)
async def remove_twitter(request: Request, handle: str = Form(...)):
    cfg = get_config()
    twitter = [h for h in cfg["accounts"].get("twitter", []) if h != handle.lstrip("@")]
    _save_accounts(twitter, cfg["accounts"].get("substack", []))
    cfg = get_config()
    return templates.TemplateResponse("watchlist.html", {
        "request": request,
        "twitter_accounts": cfg["accounts"].get("twitter", []),
        "substack_urls": cfg["accounts"].get("substack", []),
    }, headers={"HX-Reswap": "outerHTML", "HX-Retarget": "#watchlist-content"})


@app.post("/watchlist/substack/add", response_class=HTMLResponse)
async def add_substack(request: Request, url: str = Form(...)):
    cfg = get_config()
    substack = list(cfg["accounts"].get("substack", []))
    url = url.strip()
    if url and url not in substack:
        substack.append(url)
        _save_accounts(cfg["accounts"].get("twitter", []), substack)
    cfg = get_config()
    return templates.TemplateResponse("watchlist.html", {
        "request": request,
        "twitter_accounts": cfg["accounts"].get("twitter", []),
        "substack_urls": cfg["accounts"].get("substack", []),
    }, headers={"HX-Reswap": "outerHTML", "HX-Retarget": "#watchlist-content"})


@app.post("/watchlist/substack/remove", response_class=HTMLResponse)
async def remove_substack(request: Request, url: str = Form(...)):
    cfg = get_config()
    substack = [u for u in cfg["accounts"].get("substack", []) if u != url]
    _save_accounts(cfg["accounts"].get("twitter", []), substack)
    cfg = get_config()
    return templates.TemplateResponse("watchlist.html", {
        "request": request,
        "twitter_accounts": cfg["accounts"].get("twitter", []),
        "substack_urls": cfg["accounts"].get("substack", []),
    }, headers={"HX-Reswap": "outerHTML", "HX-Retarget": "#watchlist-content"})


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
        t = yf.Ticker(ticker)
        hist = t.history(period="2d")
        if hist is not None and not hist.empty:
            awareness_price = float(hist["Close"].iloc[-1])
    except Exception as e:
        logger.warning("Could not fetch price for %s: %s", ticker, e)

    db.add_watchlist_stock(ticker=ticker, awareness_price=awareness_price,
                           idea_id=idea_id, notes=notes)

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


# --- Settings ---

@app.get("/settings", response_class=HTMLResponse)
async def settings(request: Request):
    from apscheduler.schedulers.background import BackgroundScheduler
    cfg = get_config()
    return templates.TemplateResponse("base.html", {
        "request": request,
        "page": "settings",
        "cfg": cfg,
    })


# --- Manual run ---

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
