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
        "SPY|SPX|QQQ|VIX|Fed|FOMC|inflation|CPI|jobs report|interest rate|"
        "S&P|Nasdaq|ETF|options|index|indices|futures|yield|tariff|"
        "crash|recession|selloff|plunge|GDP|Powell|Treasury|"
        "crude oil|WTI|Brent|OPEC|oil price|oil supply|oil sanctions|"
        "energy market|refinery|LNG|natural gas|"
        "Iran|Strait of Hormuz|Iran sanctions|Iran nuclear|"
        "Middle East conflict|Israel Iran|airstrike|oil embargo|Persian Gulf|geopolit"
    ),
    "negative_keywords": (
        "enrollment|tuition|student|university|college|e-learning|eLearning|"
        "real estate|mortgage|pharma|biotech|FDA|clinical trial|IPO lockup|"
        "earnings call|housing market|retail sales|consumer spend|"
        "job listing|hiring|layoff|workforce|supply chain|"
        "crypto|bitcoin|blockchain|NFL|NBA|MLB|NHL|soccer|tournament|"
        "box office|album|movie|TV show"
    ),
    "max_age_hours": 4,
    "max_headlines": 25,
    "alert_keywords": "MOC|market on close|halted|circuit breaker|trading halt|Fed statement|FOMC|emergency",
    "bullish_words":  "beat|surge|rally|rise|gain|strong|upgrade|buy|above|jumps|soars|record|high|positive|growth",
    "bearish_words":  "miss|drop|fall|crash|weak|cut|below|downgrade|sell|recession|tumbles|slumps|negative|loss|decline",
}

# Defaults — overridden per-fetch from news_config.json
_BULLISH = re.compile(
    r"\b(beat|surge|rally|rise|gain|strong|upgrade|buy|above|jumps|soars|record|high|positive|growth)\b",
    re.IGNORECASE,
)
_BEARISH = re.compile(
    r"\b(miss|drop|fall|crash|weak|cut|below|downgrade|sell|recession|tumbles|slumps|negative|loss|decline)\b",
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


def _compile_filter(pattern: str, fallback: str) -> Optional[re.Pattern]:
    """Compile a regex pattern, falling back to fallback on error. Returns None if blank."""
    pattern = (pattern or "").strip()
    if not pattern:
        return None
    try:
        return re.compile(pattern, re.IGNORECASE)
    except re.error:
        try:
            return re.compile(fallback, re.IGNORECASE)
        except re.error:
            return None


def _sentiment(text: str, bullish_re=None, bearish_re=None) -> str:
    br = bullish_re or _BULLISH
    be = bearish_re or _BEARISH
    b = len(br.findall(text))
    d = len(be.findall(text))
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


def _fetch_rss(name: str, url: str, neg_filt: Optional[re.Pattern]) -> list:
    """Fetch headlines from an RSS feed, dropping any that match the negative filter."""
    try:
        import feedparser
        feed  = feedparser.parse(url)
        total = len(feed.entries)
        items = []
        blocked = 0
        for entry in feed.entries[:50]:
            title = getattr(entry, "title", "").strip()
            if not title:
                continue
            if neg_filt and neg_filt.search(title):
                blocked += 1
                logger.debug("RSS %s blocked: %s", name, title)
                continue
            items.append({
                "headline":     title,
                "source":       name,
                "url":          getattr(entry, "link", "") or "",
                "published_at": _parse_date(entry).isoformat(),
                "_dt":          _parse_date(entry),
                "sentiment":    _sentiment(title),
            })
        logger.debug("RSS %-14s  feed_entries=%d  parsed=%d  blocked=%d",
                     name, total, len(items), blocked)
        if total > 0 and len(items) == 0 and blocked == 0:
            logger.warning("RSS %s: %d feed entries but 0 parsed (all titles empty?)", name, total)
        elif total == 0:
            logger.warning("RSS %s: feed returned 0 entries (unreachable or empty)", name)
        return items
    except Exception as e:
        logger.warning("RSS %s fetch error: %s", name, e)
        return []


def _fetch_alpaca(api_key: str, api_secret: str, filt: re.Pattern,
                  neg_filt: Optional[re.Pattern]) -> list:
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
            if neg_filt and neg_filt.search(title):
                logger.debug("Alpaca blocked: %s", title)
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
    neg_keywords = nc.get("negative_keywords", "") or ""
    feeds        = nc.get("feeds", [])

    # Compile positive keyword filter — applied to ALL sources
    pos_filt = _compile_filter(keywords, _DEFAULT_NEWS_CONFIG["keywords"]) \
               or re.compile(r".", re.IGNORECASE)
    alpaca_filt = pos_filt  # alias for Alpaca

    # Compile negative/blocked filter — applied to all sources
    neg_filt = _compile_filter(neg_keywords, _DEFAULT_NEWS_CONFIG["negative_keywords"])

    # Alert keywords — headlines matching these get alert=True flag
    alert_kw_str = nc.get("alert_keywords", _DEFAULT_NEWS_CONFIG.get("alert_keywords", ""))
    alert_filt   = _compile_filter(alert_kw_str, "MOC|halted|circuit breaker")

    # Compile configurable bullish/bearish word lists
    bull_str = nc.get("bullish_words", "") or ""
    bear_str = nc.get("bearish_words", "") or ""
    bull_re  = _compile_filter(bull_str, "") or _BULLISH
    bear_re  = _compile_filter(bear_str, "") or _BEARISH

    cutoff          = datetime.now(tz=timezone.utc) - timedelta(hours=max_age)
    seen:  set      = set()
    items: list     = []
    source_raw:dict = {}
    source_kept:dict= {}

    for feed in feeds:
        if not feed.get("enabled", True):
            continue
        name = feed.get("name", "RSS")
        url  = (feed.get("url") or "").strip()
        if not url:
            continue
        raw = _fetch_rss(name, url, neg_filt)
        source_raw[name] = len(raw)
        kept = 0
        for it in raw:
            key = it["headline"].lower().strip()
            if key in seen or it["_dt"] < cutoff:
                continue
            # Apply positive keyword filter to RSS (show only matching)
            if keywords and not pos_filt.search(it["headline"]):
                continue
            seen.add(key)
            # Re-compute sentiment with configurable word lists
            it["sentiment"] = _sentiment(it["headline"], bull_re, bear_re)
            # Flag alert headlines
            it["alert"] = bool(alert_filt and alert_filt.search(it["headline"]))
            items.append(it)
            kept += 1
        source_kept[name] = kept
        if len(raw) > 0 and kept == 0:
            logger.warning("RSS %s: %d fetched but 0 kept after age/dedup filter (max_age=%dh)",
                           name, len(raw), max_age)

    alpaca_key    = (cfg.get("alpaca_api_key")    or "").strip()
    alpaca_secret = (cfg.get("alpaca_secret_key") or "").strip()
    if alpaca_key and alpaca_secret:
        raw_alpaca = _fetch_alpaca(alpaca_key, alpaca_secret, alpaca_filt, neg_filt)
        source_raw["Alpaca"]  = len(raw_alpaca)
        kept = 0
        for it in raw_alpaca:
            key = it["headline"].lower().strip()
            if key in seen or it["_dt"] < cutoff:
                continue
            seen.add(key)
            it["sentiment"] = _sentiment(it["headline"], bull_re, bear_re)
            it["alert"] = bool(alert_filt and alert_filt.search(it["headline"]))
            items.append(it)
            kept += 1
        source_kept["Alpaca"] = kept

    summary = "  ".join(f"{n}={source_kept.get(n,0)}/{source_raw.get(n,0)}"
                        for n in source_raw)
    if not items:
        logger.warning("News: 0 headlines after all sources. Per-source kept/fetched: %s", summary)
    else:
        logger.debug("News: %d total.  Per-source kept/fetched: %s", len(items), summary)

    # ── MOC auto-capture ──────────────────────────────────────────────────────
    # Detect Financial Juice MOC headlines and queue for DB write via api.py
    _MOC_RE = re.compile(
        r"MOC\s+Imbalance.*?S&P\s*500\s*:\s*([+-]?\d+(?:\.\d+)?)\s*mln",
        re.IGNORECASE
    )
    _MOC_NQ_RE   = re.compile(r"Nasdaq\s*100\s*:\s*([+-]?\d+(?:\.\d+)?)\s*mln", re.IGNORECASE)
    _MOC_DOW_RE  = re.compile(r"Dow\s*30\s*:\s*([+-]?\d+(?:\.\d+)?)\s*mln",     re.IGNORECASE)
    _MOC_MAG_RE  = re.compile(r"Mag\s*7\s*:\s*([+-]?\d+(?:\.\d+)?)\s*mln",       re.IGNORECASE)

    for it in items:
        if not it.get("alert"):
            continue
        headline = it.get("headline", "")
        m = _MOC_RE.search(headline)
        if not m:
            continue
        try:
            sp500 = float(m.group(1))
            nq_m  = _MOC_NQ_RE.search(headline)
            dow_m = _MOC_DOW_RE.search(headline)
            mag_m = _MOC_MAG_RE.search(headline)
            nasdaq = float(nq_m.group(1))  if nq_m  else None
            dow    = float(dow_m.group(1)) if dow_m else None
            mag7   = float(mag_m.group(1)) if mag_m else None
            direction = "buy" if sp500 > 0 else "sell" if sp500 < 0 else "unknown"
            it["_moc_event"] = {
                "sp500_mln":    sp500,
                "nasdaq_mln":   nasdaq,
                "dow_mln":      dow,
                "mag7_mln":     mag7,
                "direction":    direction,
                "raw_headline": headline,
                "published_at": it.get("published_at"),
                "source":       "financial_juice",
            }
            logger.info("MOC detected: S&P %+.0fM  Nasdaq %s  Dow %s  Mag7 %s",
                        sp500,
                        f"{nasdaq:+.0f}M" if nasdaq else "N/A",
                        f"{dow:+.0f}M"    if dow    else "N/A",
                        f"{mag7:+.0f}M"   if mag7   else "N/A")
        except Exception as e:
            logger.warning("MOC parse error: %s  headline: %s", e, headline)

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
                # Auto-save MOC events detected in this cycle
                for it in items:
                    moc = it.pop("_moc_event", None)
                    if moc:
                        try:
                            import requests as _req
                            _req.post("http://127.0.0.1:8001/moc/event",
                                      json=moc, timeout=3)
                        except Exception:
                            pass
                with _lock:
                    _cache      = items
                    _cache_time = datetime.now(tz=timezone.utc)
                logger.debug("News: cached %d headlines", len(items))
            except Exception as e:
                logger.warning("News fetch error: %s", e)
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
