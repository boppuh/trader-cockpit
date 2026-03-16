"""Configuration for Trader's Cockpit data collection and rendering."""

import os
from pathlib import Path

# ── API Keys (from environment variables) ──────────────────────────────────
FMP_API_KEY = os.getenv("FMP_API_KEY", "")
DATABENTO_API_KEY = os.getenv("DATABENTO_API_KEY", "")

# ── Core Watchlist ──────────────────────────────────────────────────────────
CORE_TICKERS = ["SPY", "QQQ", "NVDA", "TSLA", "IWM"]

TICKER_NAMES = {
    "SPY": "S&P 500 ETF",
    "QQQ": "Nasdaq 100 ETF",
    "NVDA": "NVIDIA",
    "TSLA": "Tesla",
    "IWM": "Russell 2000",
}

# ── Paths ───────────────────────────────────────────────────────────────────
BASE_DIR = Path(__file__).resolve().parent
TEMPLATE_DIR = BASE_DIR / "templates"
STATIC_DIR = BASE_DIR / "static"
OUTPUT_DIR = Path(os.getenv("COCKPIT_OUTPUT_DIR", "/var/www/cockpit"))

# ── Data Source Toggles ─────────────────────────────────────────────────────
# Options: "api", "manual", "stub"
DARK_POOL_SOURCE = os.getenv("COCKPIT_DARK_POOL_SOURCE", "stub")
OPTIONS_SOURCE = os.getenv("COCKPIT_OPTIONS_SOURCE", "stub")
GAMMA_SOURCE = os.getenv("COCKPIT_GAMMA_SOURCE", "stub")
WSB_SOURCE = os.getenv("COCKPIT_WSB_SOURCE", "api")

# ── Databento / GEX ───────────────────────────────────────────────────────
DATABENTO_DATASET = "OPRA.PILLAR"
OPTIONS_EXPIRY_RANGE_DAYS = 7  # Only look at options expiring within N days
RISK_FREE_RATE = float(os.getenv("RISK_FREE_RATE", "0.043"))  # 4.3%

# ── Manual Override Paths ───────────────────────────────────────────────────
# Drop JSON files here to override API data for any section
MANUAL_DATA_DIR = BASE_DIR / "manual_data"

# ── FMP API Base URL ────────────────────────────────────────────────────────
FMP_BASE_URL = "https://financialmodelingprep.com/api/v3"

# ── Reddit ──────────────────────────────────────────────────────────────────
REDDIT_WSB_URL = "https://www.reddit.com/r/wallstreetbets/hot.json"
REDDIT_USER_AGENT = "TradingCockpit/1.0 (by /u/cockpit-bot)"

# ── Caching ─────────────────────────────────────────────────────────────────
CACHE_DIR = BASE_DIR / ".cache"
CACHE_TTL_SECONDS = 300  # 5 minutes

# ── Logging ─────────────────────────────────────────────────────────────────
LOG_FILE = BASE_DIR / "cockpit.log"
LOG_LEVEL = os.getenv("COCKPIT_LOG_LEVEL", "INFO")
