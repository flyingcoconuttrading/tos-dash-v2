"""
news_fetcher.py — Background news fetcher for tos-dash-v2.

Polls RSS feeds every 60s. Feed list, keyword filter, max-age and
max-headlines are read from news_config.json on every fetch cycle so
changes made in Settings take effect without a restart.

Optionally pulls from Alpaca News API if keys are configured in config.json.
"""

import json
import re
import threading
import time
import logging
from datetime import datetime, timezone, timedelta
from pathlib import Path
from typing import Optional, Callable

logger = logging.getLogger("tos_dash.news")

# ── Defaults (used when news_config.json is absent or a key is missing) ───────
POLL_INTERVAL = 60   # seconds between refreshes

_NEWS_CONFIG_FILE = Path(__file__).parent / "news_config.json"

_DEFAULT_NEWS_CONFIG = {
    "feeds": [
        {
            "name": "Yahoo Finance",
            "url": "https://feeds.finance.yahoo.com/rss/2.0/headline?s=SPY,QQQ,VIX&region=US&lang=en-US",
            "enabled": True,
        },
        {
            "name": "Reuters",
            "url": "https://feeds.reuters.com/reuters/businessNews",
            "enabled": True,
        },
        {
            "name": "MarketWatch",
            "url": "https://feeds.marketwatch.com/marketwatch/topstories/",
            "enabled": True,
        },
        {
            "name": "CNBC",
            "url": "https://www.cnbc.com/id/100003114/device/rss/rss.html",
            "enabled": True,
        },
        {
            "name": "Seeking Alpha",
            "url": "https://seekingalpha.com/feed.xml",
            "enabled": True,
        },
    ],
    "keywords": (
        "SPY|SPX|QQQ|VIX|Fed|FOMC|inflation|CPI|jobs|rate\\b|market|stocks|equit|"
        "S&P|Nasdaq|rally|sell|crash|tariff|recession|GDP|Powell|Treasury|ETF|"
        "options|index|indices|futures|yield|interest"
    ),
    "max_age_hours": 4,
    "max_headlines": 25,
}

_BULLISH = re.compile(
    r"\b(beat|surge|rally|rise|gain|strong|upgrade|buy|above|jumps|soars)\b",
    re.IGNORECASE,
)
_BEARISH = re.compile(
    r"\b(miss|drop|fall|crash|weak|cut|below|downgrade|sell|recession|tumbles|slumps)\b",
    re.IGNORECASE,
)

# ── Module state ──────────────────────────────────────────────────────────────
_cache:      list               = []
_cache_time: Optional[datetime] = None
_lock        = threading.Lock()
_running     = False
_thread: Optional[threading.Thread] = None


# ── Helpers ───────────────────────────────────────────────────────────────────

def _load_news_config() -> dict:
    """Read news_config.json, merging with defaults."""
    try:
        saved = json.loads(_NEWS_CONFIG_FILE.read_text(encoding="utf-8"))
        return {**_DEFAULT_NEWS_CONFIG, **saved}
    except Exception:
        return dict(_DEFAULT_NEWS_CONFIG)


def _sentiment(text: str) -> str:
    b = len(_BULLISH.findall(text))
    d = len(_BEARISH.findall(text))
    if b > d:  return "bullish"
    if d > b:  return "bearish"
    return "neutral"


def _parse_date(entry) -> datetime:
    """Return a UTC-aware datetime from a feedparser entry."""
    import calendar
    if getattr(entry, "published_parsed", None):
        return datetime.fromtimestamp(calendar.timegm(entry.published_parsed), tz=timezone.utc)
    if getattr(entry, "published", None):
        import email.utils
        try:
            dt = email.utils.parsedate_to_datetime(entry.published)
            return dt if dt.tzinfo else dt.replace(tzinfo=timezone.utc)
        except Exception:
            pass
    return datetime.now(tz=timezone.utc)


def _fetch_rss(name: str, url: str) -> list:
    """Fetch all headlines from an RSS feed — no keyword filtering (user filters visually)."""
    try:
        import feedparser
        feed  = feedparser.parse(url)
        items = []
        for entry in feed.entries[:50]:
            title = getattr(entry, "title", "").strip()
            if not title:
                continue
            items.append({
                "headline":     title,
                "source":       name,
                "url":          getattr(entry, "link", "") or "",
                "published_at": _parse_date(entry).isoformat(),
                "_dt":          _parse_date(entry),
                "sentiment":    _sentiment(title),
            })
        return items
    except Exception as e:
        logger.debug("RSS %s error: %s", name, e)
        return []


def _fetch_alpaca(api_key: str, api_secret: str, filt: re.Pattern) -> list:
    try:
        import requests
        r = requests.get(
            "https://data.alpaca.markets/v1beta1/news",
            headers={
                "APCA-API-KEY-ID":     api_key,
                "APCA-API-SECRET-KEY": api_secret,
            },
            params={"symbols": "SPY,QQQ", "limit": 20},
            timeout=10,
        )
        if r.status_code != 200:
            logger.debug("Alpaca news HTTP %s", r.status_code)
            return []
        items = []
        for art in r.json().get("news", []):
            title = art.get("headline", "").strip()
            if not title or not filt.search(title):
                continue
            raw_dt = art.get("created_at", "")
            try:
                dt = datetime.fromisoformat(raw_dt.replace("Z", "+00:00"))
            except Exception:
                dt = datetime.now(tz=timezone.utc)
            items.append({
                "headline":     title,
                "source":       "Alpaca",
                "url":          art.get("url", ""),
                "published_at": dt.isoformat(),
                "_dt":          dt,
                "sentiment":    _sentiment(title),
            })
        return items
    except Exception as e:
        logger.debug("Alpaca fetch error: %s", e)
        return []


def _fetch_all(cfg: dict) -> list:
    nc           = _load_news_config()
    max_age      = int(nc.get("max_age_hours", 4))
    max_count    = int(nc.get("max_headlines", 25))
    keywords     = nc.get("keywords", "") or ""
    feeds        = nc.get("feeds", [])

    # Compile keyword filter — used only for Alpaca (RSS shows all headlines)
    try:
        alpaca_filt = re.compile(keywords, re.IGNORECASE) if keywords.strip() else re.compile(r".", re.IGNORECASE)
    except re.error:
        alpaca_filt = re.compile(_DEFAULT_NEWS_CONFIG["keywords"], re.IGNORECASE)

    cutoff     = datetime.now(tz=timezone.utc) - timedelta(hours=max_age)
    seen:  set = set()
    items: list = []

    for feed in feeds:
        if not feed.get("enabled", True):
            continue
        name = feed.get("name", "RSS")
        url  = (feed.get("url") or "").strip()
        if not url:
            continue
        for it in _fetch_rss(name, url):
            key = it["headline"].lower().strip()
            if key in seen or it["_dt"] < cutoff:
                continue
            seen.add(key)
            items.append(it)

    alpaca_key    = (cfg.get("alpaca_api_key")    or "").strip()
    alpaca_secret = (cfg.get("alpaca_secret_key") or "").strip()
    if alpaca_key and alpaca_secret:
        for it in _fetch_alpaca(alpaca_key, alpaca_secret, alpaca_filt):
            key = it["headline"].lower().strip()
            if key in seen or it["_dt"] < cutoff:
                continue
            seen.add(key)
            items.append(it)

    items.sort(key=lambda x: x["_dt"], reverse=True)
    for it in items:
        it.pop("_dt", None)

    return items[:max_count]


# ── Public API ────────────────────────────────────────────────────────────────

def start(cfg_loader: Callable[[], dict]) -> None:
    """Launch background polling thread. cfg_loader() returns the current config dict."""
    global _running, _thread

    _running = True

    def _loop():
        global _cache, _cache_time
        while _running:
            try:
                cfg   = cfg_loader()
                items = _fetch_all(cfg)
                with _lock:
                    _cache      = items
                    _cache_time = datetime.now(tz=timezone.utc)
                logger.debug("News: cached %d headlines", len(items))
            except Exception as e:
                logger.warning("News fetch error: %s", e)
            # sleep in 1s slices so stop() is responsive
            for _ in range(POLL_INTERVAL):
                if not _running:
                    break
                time.sleep(1)

    _thread = threading.Thread(target=_loop, daemon=True, name="news-fetcher")
    _thread.start()
    logger.info("News fetcher started")


def stop() -> None:
    global _running
    _running = False


def get_news() -> dict:
    with _lock:
        return {
            "headlines":  list(_cache),
            "updated_at": _cache_time.isoformat() if _cache_time else None,
            "count":      len(_cache),
        }


def get_config() -> dict:
    """Return the current news_config.json contents (with defaults filled in)."""
    return _load_news_config()


def save_config(nc: dict) -> None:
    """Persist news config to news_config.json."""
    _NEWS_CONFIG_FILE.write_text(json.dumps(nc, indent=2), encoding="utf-8")
