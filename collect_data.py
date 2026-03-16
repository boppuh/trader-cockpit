"""Data collection module for Trader's Cockpit.

Each collector function is independent — if one fails, others continue.
Returns a dict matching the schema in market_data.json.
"""

import hashlib
import json
import logging
import re
import time
from datetime import datetime, timezone
from pathlib import Path

import requests
from dateutil import tz

import config

logger = logging.getLogger("cockpit.collect")

ET = tz.gettz("America/New_York")

# ── Caching Layer ───────────────────────────────────────────────────────────

def _cache_path(key: str) -> Path:
    config.CACHE_DIR.mkdir(parents=True, exist_ok=True)
    hashed = hashlib.md5(key.encode()).hexdigest()
    return config.CACHE_DIR / f"{hashed}.json"


def _get_cached(key: str):
    path = _cache_path(key)
    if path.exists():
        data = json.loads(path.read_text())
        if time.time() - data.get("_ts", 0) < config.CACHE_TTL_SECONDS:
            return data.get("payload")
    return None


def _set_cache(key: str, payload):
    path = _cache_path(key)
    path.write_text(json.dumps({"_ts": time.time(), "payload": payload}))


# ── HTTP Helpers ────────────────────────────────────────────────────────────

def _fmp_get(endpoint: str, params: dict | None = None, timeout: int = 15):
    """Make a GET request to FMP API with caching."""
    if not config.FMP_API_KEY:
        logger.warning("FMP_API_KEY not set — skipping %s", endpoint)
        return None

    params = params or {}
    params["apikey"] = config.FMP_API_KEY
    url = f"{config.FMP_BASE_URL}/{endpoint}"

    cache_key = f"fmp:{endpoint}:{json.dumps(params, sort_keys=True)}"
    cached = _get_cached(cache_key)
    if cached is not None:
        return cached

    try:
        resp = requests.get(url, params=params, timeout=timeout)
        resp.raise_for_status()
        data = resp.json()
        _set_cache(cache_key, data)
        return data
    except Exception:
        logger.exception("FMP request failed: %s", endpoint)
        return None


def _load_manual_override(name: str):
    """Load a manual JSON override file if it exists."""
    path = config.MANUAL_DATA_DIR / f"{name}.json"
    if path.exists():
        try:
            return json.loads(path.read_text())
        except Exception:
            logger.exception("Failed to load manual override: %s", name)
    return None


# ── Collectors ──────────────────────────────────────────────────────────────

def collect_quotes() -> dict:
    """Pull real-time quotes from FMP for core tickers."""
    symbols = ",".join(config.CORE_TICKERS)
    data = _fmp_get(f"quote/{symbols}")
    if not data:
        return {}

    result = {}
    for q in data:
        sym = q.get("symbol", "")
        if sym not in config.CORE_TICKERS:
            continue
        avg_vol = q.get("avgVolume", 1) or 1
        vol = q.get("volume", 0)
        result[sym] = {
            "price": q.get("price", 0),
            "change": q.get("change", 0),
            "change_pct": q.get("changesPercentage", 0),
            "open": q.get("open", 0),
            "day_high": q.get("dayHigh", 0),
            "day_low": q.get("dayLow", 0),
            "prev_close": q.get("previousClose", 0),
            "volume": vol,
            "avg_volume": avg_vol,
            "volume_ratio": round(vol / avg_vol, 2) if avg_vol else 0,
        }
    return result


def collect_rsi() -> dict:
    """Fetch RSI(14) for each core ticker from FMP technical indicator endpoint."""
    rsi_data = {}
    for sym in config.CORE_TICKERS:
        data = _fmp_get(
            f"technical_indicator/daily/{sym}",
            params={"period": "14", "type": "rsi"},
        )
        if data and isinstance(data, list) and len(data) > 0:
            rsi_data[sym] = data[0].get("rsi", None)
        else:
            rsi_data[sym] = None
    return rsi_data


def collect_market_movers() -> dict:
    """Pull top gainers, losers, and most active from FMP."""
    result = {"gainers": [], "losers": [], "most_active": []}

    for category in ("gainers", "losers", "actives"):
        data = _fmp_get(f"stock_market/{category}")
        if not data or not isinstance(data, list):
            continue
        key = "most_active" if category == "actives" else category
        for item in data[:6]:
            result[key].append({
                "ticker": item.get("symbol", ""),
                "change_pct": item.get("changesPercentage", 0),
                "price": item.get("price", 0),
                "name": item.get("name", ""),
            })
    return result


def collect_dark_pool() -> dict:
    """Dark pool data — stub with manual override support."""
    manual = _load_manual_override("dark_pool")
    if manual:
        return manual

    if config.DARK_POOL_SOURCE == "stub":
        return {
            "top_flow": [],
            "analysis": "Dark pool data requires a premium data source. Add manual_data/dark_pool.json to populate.",
        }

    # Future: implement whalestream/tradytics scraping here
    return {"top_flow": [], "analysis": "DATA UNAVAILABLE"}


def collect_gamma_squeeze() -> dict:
    """Gamma squeeze board — stub with manual override support."""
    manual = _load_manual_override("gamma_squeeze")
    if manual:
        return manual

    if config.GAMMA_SOURCE == "stub":
        return {
            "entries": [],
            "note": "Gamma squeeze data requires Fintel or manual input. Add manual_data/gamma_squeeze.json.",
        }

    return {"entries": [], "note": "DATA UNAVAILABLE"}


def collect_options_data() -> dict:
    """Options/gamma data — stub with manual override support."""
    manual = _load_manual_override("options")
    if manual:
        return manual

    if config.OPTIONS_SOURCE == "stub":
        return {}

    return {}


def collect_wsb_trending() -> dict:
    """Pull trending tickers from r/wallstreetbets."""
    manual = _load_manual_override("wsb")
    if manual:
        return manual

    if config.WSB_SOURCE != "api":
        return {
            "top_picks": [],
            "avg_performance": "N/A",
            "sentiment": "DATA UNAVAILABLE",
        }

    cache_key = "wsb:hot"
    cached = _get_cached(cache_key)
    if cached is not None:
        return cached

    try:
        headers = {"User-Agent": config.REDDIT_USER_AGENT}
        resp = requests.get(config.REDDIT_WSB_URL, headers=headers, timeout=15)
        resp.raise_for_status()
        data = resp.json()

        # Extract ticker mentions from post titles
        ticker_pattern = re.compile(r'\$([A-Z]{1,5})\b')
        mentions: dict[str, int] = {}
        posts = data.get("data", {}).get("children", [])
        for post in posts[:25]:
            title = post.get("data", {}).get("title", "")
            for match in ticker_pattern.findall(title):
                mentions[match] = mentions.get(match, 0) + 1

        # Also look for uppercase tickers without $ (common WSB style)
        bare_ticker = re.compile(r'\b([A-Z]{2,5})\b')
        # Common words to exclude
        exclude = {
            "THE", "AND", "FOR", "ARE", "BUT", "NOT", "YOU", "ALL",
            "CAN", "HER", "WAS", "ONE", "OUR", "OUT", "DAY", "HAD",
            "HOT", "OIL", "SIT", "NOW", "OLD", "RED", "RUN", "EAT",
            "TOP", "FAR", "NEW", "PUT", "SAY", "SHE", "TOO", "USE",
            "WSB", "IMO", "DD", "YOLO", "FD", "ITM", "OTM", "ATM",
            "CEO", "CFO", "IPO", "ETF", "GDP", "SEC", "AI", "EPS",
            "PE", "RSI", "DTE", "OI", "IV", "PM", "AM", "EST", "PST",
            "USD", "GDP", "CPI", "FED", "FOMC", "QE", "QT", "YOY",
            "MOM", "WOW", "LOL", "LMAO", "OMG", "WTF", "TBH", "IMO",
        }
        for post in posts[:25]:
            title = post.get("data", {}).get("title", "")
            for match in bare_ticker.findall(title):
                if match not in exclude and len(match) >= 2:
                    mentions[match] = mentions.get(match, 0) + 1

        top_picks = sorted(mentions.items(), key=lambda x: -x[1])[:10]
        result = {
            "top_picks": [t[0] for t in top_picks],
            "avg_performance": "N/A",
            "sentiment": f"Top WSB mentions: {', '.join(t[0] for t in top_picks[:5])}",
        }
        _set_cache(cache_key, result)
        return result

    except Exception:
        logger.exception("Failed to fetch WSB data")
        return {
            "top_picks": [],
            "avg_performance": "N/A",
            "sentiment": "DATA UNAVAILABLE",
        }


def collect_sentiment() -> dict:
    """Collect sentiment / bull-bear cases from FMP news or manual override."""
    manual = _load_manual_override("sentiment")
    if manual:
        return manual

    # Stub — sentiment requires curated analysis
    return {}


# ── Main Collection Orchestrator ────────────────────────────────────────────

def collect_all() -> dict:
    """Run all collectors and assemble the full data dict.

    Each collector runs in a try/except — failures are logged but don't
    block other collectors.
    """
    now = datetime.now(ET)
    data = {
        "timestamp": now.isoformat(),
        "market_date": now.strftime("%A, %B %d, %Y").replace(" 0", " "),
        "market_date_short": now.strftime("%a %b %d, %Y").upper(),
        "generated_at": now.strftime("%H:%M ET"),
        "overall_sentiment": "BEARISH",
        "core_watchlist": {},
        "trade_setups": [],
        "watchlist_additions": [],
        "dark_pool_leaders": {"top_flow": [], "analysis": ""},
        "unusual_gamma_names": {},
        "wsb_trending": {"top_picks": [], "avg_performance": "N/A", "sentiment": ""},
        "rsi_data": {},
        "market_regime": {},
        "skew_data": {},
    }

    # 1. Quotes
    try:
        quotes = collect_quotes()
        for sym in config.CORE_TICKERS:
            if sym in quotes:
                data["core_watchlist"][sym] = quotes[sym]
            else:
                data["core_watchlist"][sym] = {"price": 0, "change": 0, "change_pct": 0}
    except Exception:
        logger.exception("Quote collection failed")

    # 2. RSI
    try:
        rsi = collect_rsi()
        data["rsi_data"] = rsi
        for sym, val in rsi.items():
            if sym in data["core_watchlist"] and val is not None:
                data["core_watchlist"][sym]["rsi_14"] = val
    except Exception:
        logger.exception("RSI collection failed")

    # 3. Market movers
    try:
        movers = collect_market_movers()
        data["gainers"] = movers.get("gainers", [])
        data["losers"] = movers.get("losers", [])
    except Exception:
        logger.exception("Market movers collection failed")

    # 4. Dark pool
    try:
        data["dark_pool_leaders"] = collect_dark_pool()
    except Exception:
        logger.exception("Dark pool collection failed")

    # 5. Gamma squeeze
    try:
        gs = collect_gamma_squeeze()
        data["gamma_squeeze_entries"] = gs.get("entries", [])
        data["gamma_squeeze_note"] = gs.get("note", "")
    except Exception:
        logger.exception("Gamma squeeze collection failed")

    # 6. Options data (merge into core watchlist)
    try:
        opts = collect_options_data()
        for sym, opt_data in opts.items():
            if sym in data["core_watchlist"]:
                data["core_watchlist"][sym]["options_data"] = opt_data
    except Exception:
        logger.exception("Options data collection failed")

    # 7. WSB trending
    try:
        data["wsb_trending"] = collect_wsb_trending()
    except Exception:
        logger.exception("WSB collection failed")

    # 8. Sentiment
    try:
        sentiment = collect_sentiment()
        for sym, sent in sentiment.items():
            if sym in data["core_watchlist"]:
                data["core_watchlist"][sym]["sentiment"] = sent
    except Exception:
        logger.exception("Sentiment collection failed")

    # 9. Trade setups, watchlist, regime — manual overrides
    for name in ("trade_setups", "watchlist_additions", "market_regime", "skew_data"):
        try:
            manual = _load_manual_override(name)
            if manual:
                data[name] = manual
        except Exception:
            logger.exception("Manual override load failed for %s", name)

    # 10. Ticker display names (needed by template)
    data["ticker_names"] = config.TICKER_NAMES

    return data
