"""ClickHouse persistence layer for GEX/gamma data.

Stores computed GEX snapshots into two tables:
  - gex_levels: one row per ticker per snapshot (pin, walls, flip, net GEX, skew)
  - gex_strikes: one row per strike per snapshot (OI, GEX, IV breakdowns)

Tables are auto-created on first use. Uses the clickhouse-driver native protocol.
"""

import logging
from datetime import datetime, timezone

import config

logger = logging.getLogger("cockpit.clickhouse")

# ── Table DDL ──────────────────────────────────────────────────────────────

CREATE_GEX_LEVELS = """
CREATE TABLE IF NOT EXISTS {db}.gex_levels
(
    ticker       LowCardinality(String),
    snapshot_ts  DateTime,
    date         Date,
    spot_price   Float64,
    pin_level    Float64,
    put_wall     Float64,
    call_wall    Float64,
    gamma_flip   Float64,
    net_gex      Float64,
    net_gex_fmt  String,
    regime       LowCardinality(String),
    regime_label String,
    pc_ratio_oi  Float32,
    pc_ratio_vol Float32,
    skew_sentiment LowCardinality(String),
    narrative    String,
    expiry_used  Nullable(String)
)
ENGINE = MergeTree()
PARTITION BY toYYYYMM(date)
ORDER BY (ticker, snapshot_ts)
"""

CREATE_GEX_STRIKES = """
CREATE TABLE IF NOT EXISTS {db}.gex_strikes
(
    ticker      LowCardinality(String),
    snapshot_ts DateTime,
    date        Date,
    strike      Float64,
    call_oi     UInt32,
    put_oi      UInt32,
    call_gex    Float64,
    put_gex     Float64,
    net_gex     Float64,
    avg_iv      Nullable(Float32)
)
ENGINE = MergeTree()
PARTITION BY toYYYYMM(date)
ORDER BY (ticker, date, strike)
"""


# ── Store Class ────────────────────────────────────────────────────────────


class GEXClickHouseStore:
    """Persists GEX computation results to ClickHouse."""

    def __init__(self, client=None):
        """Initialize with optional pre-built client (for testing).

        Args:
            client: A clickhouse_driver.Client instance. If None, one is
                    created from config env vars on first use.
        """
        self._client = client
        self._tables_created = False

    @property
    def client(self):
        if self._client is not None:
            return self._client

        if not config.CH_HOST:
            logger.warning("CH_HOST not set — ClickHouse persistence disabled")
            return None

        try:
            from clickhouse_driver import Client

            self._client = Client(
                host=config.CH_HOST,
                port=int(config.CH_PORT),
                database=config.CH_DATABASE,
                user=config.CH_USER,
                password=config.CH_PASSWORD,
                connect_timeout=5,
                send_receive_timeout=10,
            )
            return self._client
        except ImportError:
            logger.error("clickhouse-driver not installed — pip install clickhouse-driver")
            return None
        except Exception:
            logger.exception("Failed to connect to ClickHouse")
            return None

    def ensure_tables(self):
        """Create tables if they don't exist."""
        if self._tables_created:
            return True

        client = self.client
        if client is None:
            return False

        try:
            db = config.CH_DATABASE
            client.execute(CREATE_GEX_LEVELS.format(db=db))
            client.execute(CREATE_GEX_STRIKES.format(db=db))
            self._tables_created = True
            logger.info("GEX tables ensured in ClickHouse (%s)", db)
            return True
        except Exception:
            logger.exception("Failed to create GEX tables")
            return False

    def store_ticker_result(self, ticker, result, spot_price, snapshot_ts=None):
        """Persist a single ticker's GEX result.

        Args:
            ticker: symbol (e.g. "SPY")
            result: dict from DatabentGammaCollector.collect_ticker()
            spot_price: current spot price
            snapshot_ts: datetime of snapshot (defaults to now)
        """
        if not self.ensure_tables():
            return False

        client = self.client
        if client is None:
            return False

        if not result:
            return False

        if snapshot_ts is None:
            snapshot_ts = datetime.now(timezone.utc)

        date = snapshot_ts.date()

        try:
            # ── Insert gex_levels ──
            gex = result.get("gex", {})
            levels = result.get("levels", {})
            skew = result.get("skew", {})

            levels_row = {
                "ticker": ticker,
                "snapshot_ts": snapshot_ts,
                "date": date,
                "spot_price": float(spot_price),
                "pin_level": float(levels.get("pin_level") or 0),
                "put_wall": float(levels.get("put_wall") or 0),
                "call_wall": float(levels.get("call_wall") or 0),
                "gamma_flip": float(levels.get("gamma_flip") or 0),
                "net_gex": float(gex.get("net_gex", 0)),
                "net_gex_fmt": gex.get("net_gex_formatted", ""),
                "regime": gex.get("regime", "UNKNOWN"),
                "regime_label": gex.get("regime_label", "Unknown"),
                "pc_ratio_oi": float(skew.get("put_call_ratio", 0)),
                "pc_ratio_vol": float(skew.get("volume_put_call", 0)),
                "skew_sentiment": skew.get("sentiment", "NEUTRAL"),
                "narrative": result.get("dealer_narrative", ""),
                "expiry_used": result.get("expiry_used"),
            }

            client.execute(
                f"INSERT INTO {config.CH_DATABASE}.gex_levels VALUES",
                [levels_row],
            )

            # ── Insert gex_strikes ──
            top_strikes = result.get("top_strikes", [])
            if top_strikes:
                strike_rows = []
                for s in top_strikes:
                    strike_rows.append({
                        "ticker": ticker,
                        "snapshot_ts": snapshot_ts,
                        "date": date,
                        "strike": float(s.get("strike", 0)),
                        "call_oi": int(s.get("call_oi", 0)),
                        "put_oi": int(s.get("put_oi", 0)),
                        "call_gex": float(s.get("call_gex", 0)),
                        "put_gex": float(s.get("put_gex", 0)),
                        "net_gex": float(s.get("net_gex", 0)),
                        "avg_iv": None,  # Could be enriched later
                    })

                client.execute(
                    f"INSERT INTO {config.CH_DATABASE}.gex_strikes VALUES",
                    strike_rows,
                )

            logger.info(
                "Stored GEX for %s: %d level rows, %d strike rows",
                ticker, 1, len(top_strikes),
            )
            return True

        except Exception:
            logger.exception("Failed to store GEX data for %s", ticker)
            return False

    def store_all_results(self, results, quotes, snapshot_ts=None):
        """Persist results for all tickers.

        Args:
            results: dict from collect_all_tickers() — {ticker: result_dict}
            quotes: dict of {ticker: {"price": float, ...}}
            snapshot_ts: datetime of snapshot (defaults to now)

        Returns:
            dict of {ticker: bool} indicating success/failure per ticker
        """
        if snapshot_ts is None:
            snapshot_ts = datetime.now(timezone.utc)

        statuses = {}
        for ticker, result in results.items():
            spot = 0
            if quotes and ticker in quotes:
                spot = quotes[ticker].get("price", 0)

            statuses[ticker] = self.store_ticker_result(
                ticker, result, spot, snapshot_ts
            )

        stored = sum(1 for v in statuses.values() if v)
        logger.info("ClickHouse store complete: %d/%d tickers persisted", stored, len(results))
        return statuses

    def query_latest_levels(self, ticker=None, limit=10):
        """Query most recent GEX levels (utility for debugging/dashboard).

        Args:
            ticker: optional filter by ticker
            limit: max rows to return

        Returns:
            list of dicts
        """
        client = self.client
        if client is None:
            return []

        try:
            where = f"WHERE ticker = '{ticker}'" if ticker else ""
            query = f"""
                SELECT *
                FROM {config.CH_DATABASE}.gex_levels
                {where}
                ORDER BY snapshot_ts DESC
                LIMIT {limit}
            """
            rows = client.execute(query, with_column_types=True)
            if not rows or len(rows) < 2:
                return []

            data, columns = rows
            col_names = [c[0] for c in columns]
            return [dict(zip(col_names, row)) for row in data]
        except Exception:
            logger.exception("Failed to query gex_levels")
            return []

    def query_levels_history(self, ticker, days=30):
        """Query GEX level history for a ticker (for backtesting/analysis).

        Args:
            ticker: symbol
            days: lookback period

        Returns:
            list of dicts ordered by date
        """
        client = self.client
        if client is None:
            return []

        try:
            query = f"""
                SELECT
                    ticker, date, snapshot_ts, spot_price,
                    pin_level, put_wall, call_wall, gamma_flip,
                    net_gex, regime, pc_ratio_oi, skew_sentiment
                FROM {config.CH_DATABASE}.gex_levels
                WHERE ticker = %(ticker)s
                  AND date >= today() - %(days)s
                ORDER BY snapshot_ts ASC
            """
            rows = client.execute(
                query,
                {"ticker": ticker, "days": days},
                with_column_types=True,
            )
            if not rows or len(rows) < 2:
                return []

            data, columns = rows
            col_names = [c[0] for c in columns]
            return [dict(zip(col_names, row)) for row in data]
        except Exception:
            logger.exception("Failed to query level history for %s", ticker)
            return []
