# Trader's Cockpit — Self-Hosted Dashboard

A cron-based trading dashboard that pulls live market data from APIs and regenerates a static HTML dashboard on a schedule. Designed to run on an EC2 instance behind nginx.

## Architecture

```
cockpit-server/
├── config.py           # Configuration (API keys, tickers, schedule)
├── collect_data.py     # Data collection from multiple sources
├── render.py           # Jinja2 renderer: data dict → HTML
├── generate.py         # Main entry: collect → render → write HTML
├── templates/
│   └── cockpit.html    # Jinja2 template (dark terminal aesthetic)
├── static/
│   └── style.css       # Stylesheet (JetBrains Mono + Inter)
├── output/             # Generated HTML (local dev)
├── manual_data/        # Manual JSON overrides for any data source
├── requirements.txt    # Python dependencies
├── cockpit.service     # systemd oneshot unit
├── cockpit.timer       # systemd timer (weekday market hours)
├── nginx.conf          # nginx site config
├── setup.sh            # One-command setup script
└── README.md           # This file
```

### Data Flow

```
 ┌─────────────┐     ┌──────────────┐     ┌────────────┐     ┌───────────┐
 │  FMP API     │────▸│              │────▸│            │────▸│  /var/www  │
 │  Reddit API  │     │ collect_data │     │   render   │     │  /cockpit │
 │  Manual JSON │────▸│   .py        │────▸│   .py      │────▸│           │
 └─────────────┘     └──────────────┘     └────────────┘     └─────┬─────┘
                                                                    │
                     systemd timer triggers generate.py          nginx
                     every 30-60 min on weekdays                serves
```

## Prerequisites

- Python 3.10+
- nginx (for serving the dashboard)
- FMP API key ([financialmodelingprep.com](https://financialmodelingprep.com))

## Quick Start

```bash
# 1. Clone/copy the cockpit-server directory to your server

# 2. Set your API key
export FMP_API_KEY='your-fmp-api-key'

# 3. Run setup
bash setup.sh

# 4. Or run locally for development:
python3 -m venv venv
source venv/bin/activate
pip install -r requirements.txt

# Generate with live data
python generate.py --output-dir ./output

# Generate from existing JSON (for testing)
python generate.py --from-json /path/to/market_data.json --output-dir ./output
```

## Configuration

All configuration is in `config.py` and can be overridden via environment variables:

| Variable | Default | Description |
|----------|---------|-------------|
| `FMP_API_KEY` | (empty) | Financial Modeling Prep API key |
| `COCKPIT_OUTPUT_DIR` | `/var/www/cockpit` | Where generated HTML is written |
| `COCKPIT_DARK_POOL_SOURCE` | `stub` | Dark pool data source: `api`, `manual`, `stub` |
| `COCKPIT_OPTIONS_SOURCE` | `stub` | Options data source: `api`, `manual`, `stub` |
| `COCKPIT_GAMMA_SOURCE` | `stub` | Gamma squeeze source: `api`, `manual`, `stub` |
| `COCKPIT_WSB_SOURCE` | `api` | WSB data: `api` (Reddit), `manual`, `stub` |
| `COCKPIT_LOG_LEVEL` | `INFO` | Python logging level |

### Schedule (systemd timer)

Default schedule (all times ET, weekdays only):

| Time | Purpose |
|------|---------|
| 8:30 AM | Pre-market briefing |
| 9:35 AM | Post-open check |
| 12:00 PM | Midday update |
| 3:00 PM | Into-close update |

To modify, edit `cockpit.timer` and run `sudo systemctl daemon-reload`.

### Cron Alternative

```cron
30 8 * * 1-5   /opt/cockpit/venv/bin/python /opt/cockpit/generate.py
35 9 * * 1-5   /opt/cockpit/venv/bin/python /opt/cockpit/generate.py
0 12 * * 1-5   /opt/cockpit/venv/bin/python /opt/cockpit/generate.py
0 15 * * 1-5   /opt/cockpit/venv/bin/python /opt/cockpit/generate.py
```

## Adding Custom Data Sources

### Manual JSON Overrides

Drop JSON files in the `manual_data/` directory to override any data section:

| File | Overrides |
|------|-----------|
| `dark_pool.json` | Dark pool flow data |
| `gamma_squeeze.json` | Gamma squeeze board |
| `options.json` | Per-ticker options data (keyed by symbol) |
| `wsb.json` | WSB trending data |
| `sentiment.json` | Bull/bear sentiment (keyed by symbol) |
| `trade_setups.json` | Trade setup cards (list) |
| `watchlist_additions.json` | Watchlist entries (list) |
| `market_regime.json` | Market regime analysis |
| `skew_data.json` | Put skew data |

Example `manual_data/trade_setups.json`:
```json
[
  {
    "name": "QQQ 600 Pin-and-Fade",
    "type": "Mean Reversion / Gamma Pin",
    "ticker": "QQQ",
    "thesis": "600 is the single largest gamma strike...",
    "entry": "Sell 600C 0DTE if QQQ hits 602-603",
    "target": "598-596 by close",
    "stop": "604.50",
    "risk_reward": "1:2.5",
    "conviction": "HIGH",
    "edge": "Options structure tells you 600 is the pin."
  }
]
```

### Adding a New API Data Source

1. Add a `collect_xxx()` function in `collect_data.py`
2. Add a config toggle in `config.py`
3. Wire it into `collect_all()` with try/except
4. Update the Jinja2 template if new sections are needed

## Troubleshooting

**Dashboard shows "DATA UNAVAILABLE" for most sections**
- Check that `FMP_API_KEY` is set: `echo $FMP_API_KEY`
- Check the log: `cat /opt/cockpit/cockpit.log`
- Test API key: `curl "https://financialmodelingprep.com/api/v3/quote/SPY?apikey=YOUR_KEY"`

**Stale data / not updating**
- Check timer: `systemctl status cockpit.timer`
- Check last run: `systemctl status cockpit.service`
- Manual run: `/opt/cockpit/venv/bin/python /opt/cockpit/generate.py`

**Nginx not serving**
- Test config: `sudo nginx -t`
- Check symlink: `ls -la /etc/nginx/sites-enabled/cockpit`
- Check output dir: `ls -la /var/www/cockpit/`

**Template rendering errors**
- Run with debug logging: `COCKPIT_LOG_LEVEL=DEBUG python generate.py`
- Test with sample data: `python generate.py --from-json market_data.json --output-dir ./output`

**API rate limiting**
- Responses are cached for 5 minutes (configurable via `CACHE_TTL_SECONDS` in config.py)
- Clear cache: `rm -rf /opt/cockpit/.cache/`
