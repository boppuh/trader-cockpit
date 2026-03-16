"""GEX/Gamma Exposure computation module using Databento OPRA data.

Fetches raw options data from Databento, computes Greeks via Black-Scholes,
and produces structured GEX output for the trader cockpit dashboard.
"""

import logging
import math
import os
import time
from datetime import datetime, timedelta

import numpy as np
import pandas as pd
from scipy.stats import norm

import config

logger = logging.getLogger("cockpit.gamma")

# ── Constants ──────────────────────────────────────────────────────────────

PRICE_SCALE = 1e9  # Databento int64 fixed-point: divide by 1e9

# ── Black-Scholes ──────────────────────────────────────────────────────────


def bs_price(S, K, T, r, sigma, is_call=True):
    """Compute Black-Scholes option price.

    Args:
        S: spot price
        K: strike price
        T: time to expiry in years
        r: risk-free rate
        sigma: implied volatility
        is_call: True for call, False for put
    """
    if T <= 0 or sigma <= 0 or S <= 0 or K <= 0:
        return 0.0

    d1 = (math.log(S / K) + (r + 0.5 * sigma ** 2) * T) / (sigma * math.sqrt(T))
    d2 = d1 - sigma * math.sqrt(T)

    if is_call:
        return S * norm.cdf(d1) - K * math.exp(-r * T) * norm.cdf(d2)
    else:
        return K * math.exp(-r * T) * norm.cdf(-d2) - S * norm.cdf(-d1)


def bs_greeks(S, K, T, r, sigma, is_call=True):
    """Compute Black-Scholes delta and gamma.

    Returns:
        dict with 'delta' and 'gamma' keys
    """
    if T <= 0 or sigma <= 0 or S <= 0 or K <= 0:
        return {"delta": 0.0, "gamma": 0.0}

    d1 = (math.log(S / K) + (r + 0.5 * sigma ** 2) * T) / (sigma * math.sqrt(T))

    gamma = norm.pdf(d1) / (S * sigma * math.sqrt(T))

    if is_call:
        delta = norm.cdf(d1)
    else:
        delta = norm.cdf(d1) - 1.0

    return {"delta": delta, "gamma": gamma}


def compute_iv(mid_price, S, K, T, r, is_call=True, max_iter=50, tol=1e-6):
    """Newton-Raphson implied volatility solver.

    Args:
        mid_price: observed option mid-price
        S: spot price
        K: strike price
        T: time to expiry in years
        r: risk-free rate
        is_call: True for call, False for put
        max_iter: max Newton-Raphson iterations
        tol: convergence tolerance

    Returns:
        Implied volatility as float, or None if solver fails
    """
    if mid_price <= 0 or T <= 0 or S <= 0 or K <= 0:
        return None

    # Intrinsic value check
    if is_call:
        intrinsic = max(S - K * math.exp(-r * T), 0)
    else:
        intrinsic = max(K * math.exp(-r * T) - S, 0)

    if mid_price < intrinsic * 0.99:
        return None

    # Initial guess
    sigma = 0.3

    for _ in range(max_iter):
        price = bs_price(S, K, T, r, sigma, is_call)
        diff = price - mid_price

        if abs(diff) < tol:
            return sigma

        # Vega = S * sqrt(T) * N'(d1)
        d1 = (math.log(S / K) + (r + 0.5 * sigma ** 2) * T) / (sigma * math.sqrt(T))
        vega = S * math.sqrt(T) * norm.pdf(d1)

        if vega < 1e-12:
            break

        sigma -= diff / vega

        if sigma <= 0.001:
            sigma = 0.001
        if sigma > 5.0:
            break

    return sigma if 0.001 < sigma < 5.0 else None


# ── GEX Computation ───────────────────────────────────────────────────────


def compute_gex_per_strike(gamma, oi, spot, is_call):
    """Compute GEX for a single strike.

    GEX = gamma * OI * 100 * S^2 * 0.01
    Positive for calls, negative for puts (dealer model).
    """
    raw_gex = gamma * oi * 100 * spot ** 2 * 0.01
    return raw_gex if is_call else -raw_gex


def format_gex(value):
    """Format GEX value to human-readable string (e.g. +$2.1B, -$450M)."""
    sign = "+" if value >= 0 else "-"
    abs_val = abs(value)
    if abs_val >= 1e9:
        return f"{sign}${abs_val / 1e9:.1f}B"
    elif abs_val >= 1e6:
        return f"{sign}${abs_val / 1e6:.0f}M"
    elif abs_val >= 1e3:
        return f"{sign}${abs_val / 1e3:.0f}K"
    else:
        return f"{sign}${abs_val:.0f}"


# ── Level Detection ───────────────────────────────────────────────────────


def find_pin_level(chain_df):
    """Find strike with highest total OI (calls + puts)."""
    if chain_df.empty:
        return None
    grouped = chain_df.groupby("strike")["oi"].sum()
    if grouped.empty:
        return None
    return float(grouped.idxmax())


def find_put_wall(chain_df):
    """Find strike with highest put OI."""
    if chain_df.empty or "is_call" not in chain_df.columns:
        return None
    puts = chain_df[chain_df["is_call"] == False]  # noqa: E712
    if puts.empty:
        return None
    grouped = puts.groupby("strike")["oi"].sum()
    if grouped.empty:
        return None
    return float(grouped.idxmax())


def find_call_wall(chain_df):
    """Find strike with highest call OI."""
    if chain_df.empty or "is_call" not in chain_df.columns:
        return None
    calls = chain_df[chain_df["is_call"] == True]  # noqa: E712
    if calls.empty:
        return None
    grouped = calls.groupby("strike")["oi"].sum()
    if grouped.empty:
        return None
    return float(grouped.idxmax())


def find_gamma_flip(gex_df):
    """Find strike where cumulative net GEX changes sign.

    Iterates strikes low→high. Returns the strike where cumulative
    GEX transitions from negative to positive (or vice versa).
    """
    if gex_df.empty:
        return None

    sorted_df = gex_df.sort_values("strike")
    cumulative = 0.0
    prev_sign = None

    for _, row in sorted_df.iterrows():
        cumulative += row["net_gex"]
        current_sign = 1 if cumulative >= 0 else -1

        if prev_sign is not None and current_sign != prev_sign:
            return float(row["strike"])

        prev_sign = current_sign

    return None


def compute_skew(chain_df):
    """Compute put/call skew ratios.

    Returns dict with put_call_ratio (OI-based), volume_put_call,
    and sentiment label.
    """
    if chain_df.empty:
        return {"put_call_ratio": 0.0, "volume_put_call": 0.0, "sentiment": "NEUTRAL"}

    calls = chain_df[chain_df["is_call"] == True]  # noqa: E712
    puts = chain_df[chain_df["is_call"] == False]  # noqa: E712

    total_call_oi = calls["oi"].sum()
    total_put_oi = puts["oi"].sum()

    total_call_vol = calls["volume"].sum() if "volume" in calls.columns else 0
    total_put_vol = puts["volume"].sum() if "volume" in puts.columns else 0

    pc_ratio = total_put_oi / total_call_oi if total_call_oi > 0 else 0.0
    vol_pc = total_put_vol / total_call_vol if total_call_vol > 0 else 0.0

    if pc_ratio > 1.3:
        sentiment = "BEARISH"
    elif pc_ratio > 1.1:
        sentiment = "SLIGHTLY_BEARISH"
    elif pc_ratio < 0.7:
        sentiment = "BULLISH"
    elif pc_ratio < 0.9:
        sentiment = "SLIGHTLY_BULLISH"
    else:
        sentiment = "NEUTRAL"

    return {
        "put_call_ratio": round(pc_ratio, 2),
        "volume_put_call": round(vol_pc, 2),
        "sentiment": sentiment,
    }


def generate_narrative(ticker, gex_data, levels, skew):
    """Generate dealer positioning narrative text."""
    parts = []

    # Net GEX regime
    regime = gex_data.get("regime", "UNKNOWN")
    net_formatted = gex_data.get("net_gex_formatted", "$0")
    regime_word = "LONG" if regime == "LONG_GAMMA" else "SHORT"
    parts.append(f"Dealers net {regime_word} gamma ({net_formatted}).")

    # Pin level
    pin = levels.get("pin_level")
    if pin is not None:
        parts.append(f"{ticker} pinned to {pin:.0f}.")

    # Walls
    put_wall = levels.get("put_wall")
    if put_wall is not None:
        parts.append(f"Put wall at {put_wall:.0f} acts as support.")

    call_wall = levels.get("call_wall")
    if call_wall is not None:
        parts.append(f"Call wall at {call_wall:.0f} caps upside.")

    # Regime label
    regime_label = gex_data.get("regime_label", "")
    if regime_label:
        parts.append(f"Regime: {regime_label.upper()}.")

    return " ".join(parts)


# ── Main Collector Class ──────────────────────────────────────────────────


class DatabentGammaCollector:
    """Fetches OPRA data from Databento and computes GEX/gamma levels."""

    def __init__(self, client=None):
        """Initialize with optional Databento client for testing."""
        self._client = client
        self._cache = {}
        self._cache_ts = {}

    @property
    def client(self):
        if self._client is not None:
            return self._client

        api_key = config.DATABENTO_API_KEY
        if not api_key:
            logger.warning("DATABENTO_API_KEY not set — cannot fetch options data")
            return None

        try:
            import databento as db
            self._client = db.Historical(key=api_key)
            return self._client
        except ImportError:
            logger.error("databento package not installed")
            return None

    def _get_date_str(self):
        """Get today's date string for API calls."""
        return datetime.now().strftime("%Y-%m-%d")

    def _fetch_definitions(self, ticker, date):
        """Fetch option contract definitions from Databento."""
        client = self.client
        if client is None:
            return pd.DataFrame()

        try:
            data = client.timeseries.get_range(
                dataset=config.DATABENTO_DATASET,
                symbols=[f"{ticker}.OPT"],
                stype_in="parent",
                schema="definition",
                start=date,
                end=date,
            )
            return data.to_df()
        except Exception:
            logger.exception("Failed to fetch definitions for %s", ticker)
            return pd.DataFrame()

    def _fetch_oi(self, ticker, date):
        """Fetch open interest from statistics schema (stat_type=9)."""
        client = self.client
        if client is None:
            return pd.DataFrame()

        try:
            data = client.timeseries.get_range(
                dataset=config.DATABENTO_DATASET,
                symbols=[f"{ticker}.OPT"],
                stype_in="parent",
                schema="statistics",
                start=date,
                end=date,
            )
            df = data.to_df()
            # stat_type=9 is OI
            if not df.empty and "stat_type" in df.columns:
                df = df[df["stat_type"] == 9]
            return df
        except Exception:
            logger.exception("Failed to fetch OI for %s", ticker)
            return pd.DataFrame()

    def _fetch_volume(self, ticker, date):
        """Fetch daily volume from ohlcv-1d schema."""
        client = self.client
        if client is None:
            return pd.DataFrame()

        try:
            data = client.timeseries.get_range(
                dataset=config.DATABENTO_DATASET,
                symbols=[f"{ticker}.OPT"],
                stype_in="parent",
                schema="ohlcv-1d",
                start=date,
                end=date,
            )
            return data.to_df()
        except Exception:
            logger.exception("Failed to fetch volume for %s", ticker)
            return pd.DataFrame()

    def _fetch_mid_prices(self, ticker, date):
        """Fetch top-of-book bid/ask from mbp-1 schema."""
        client = self.client
        if client is None:
            return pd.DataFrame()

        try:
            data = client.timeseries.get_range(
                dataset=config.DATABENTO_DATASET,
                symbols=[f"{ticker}.OPT"],
                stype_in="parent",
                schema="mbp-1",
                start=date,
                end=date,
            )
            return data.to_df()
        except Exception:
            logger.exception("Failed to fetch mid prices for %s", ticker)
            return pd.DataFrame()

    def _build_chain(self, ticker, date, spot_price):
        """Assemble full option chain DataFrame with Greeks and GEX.

        Args:
            ticker: underlying symbol
            date: date string
            spot_price: current spot price of underlying

        Returns:
            DataFrame with columns: strike, is_call, oi, volume, iv, delta,
            gamma, gex, expiration, instrument_id
        """
        # Fetch all data
        defs_df = self._fetch_definitions(ticker, date)
        if defs_df.empty:
            logger.warning("No definitions found for %s", ticker)
            return pd.DataFrame()

        oi_df = self._fetch_oi(ticker, date)
        vol_df = self._fetch_volume(ticker, date)
        mid_df = self._fetch_mid_prices(ticker, date)

        # Parse definitions
        chain_records = []
        expiry_cutoff = datetime.now() + timedelta(days=config.OPTIONS_EXPIRY_RANGE_DAYS)
        strike_low = spot_price * 0.80
        strike_high = spot_price * 1.20
        r = config.RISK_FREE_RATE

        # Build OI lookup: instrument_id -> OI quantity
        oi_lookup = {}
        if not oi_df.empty and "instrument_id" in oi_df.columns:
            for _, row in oi_df.iterrows():
                iid = row.get("instrument_id")
                qty = row.get("quantity", 0)
                if iid is not None:
                    oi_lookup[iid] = qty

        # Build volume lookup
        vol_lookup = {}
        if not vol_df.empty and "instrument_id" in vol_df.columns:
            for _, row in vol_df.iterrows():
                iid = row.get("instrument_id")
                v = row.get("volume", 0)
                if iid is not None:
                    vol_lookup[iid] = v

        # Build mid-price lookup (last mid for each instrument)
        mid_lookup = {}
        if not mid_df.empty and "instrument_id" in mid_df.columns:
            for _, row in mid_df.iterrows():
                iid = row.get("instrument_id")
                bid = row.get("bid_px_00", row.get("bid_px", 0))
                ask = row.get("ask_px_00", row.get("ask_px", 0))
                if isinstance(bid, (int, float)) and isinstance(ask, (int, float)):
                    bid_f = bid / PRICE_SCALE if bid > 1e6 else bid
                    ask_f = ask / PRICE_SCALE if ask > 1e6 else ask
                    if bid_f > 0 and ask_f > 0:
                        mid_lookup[iid] = (bid_f + ask_f) / 2

        # Process each definition
        for _, defn in defs_df.iterrows():
            instrument_id = defn.get("instrument_id")
            strike_raw = defn.get("strike_price", 0)
            strike = strike_raw / PRICE_SCALE if strike_raw > 1e6 else strike_raw

            # Filter: strike range
            if strike < strike_low or strike > strike_high:
                continue

            # Filter: expiration
            exp = defn.get("expiration")
            if exp is not None:
                if isinstance(exp, str):
                    try:
                        exp_dt = datetime.fromisoformat(exp.replace("Z", "+00:00"))
                    except ValueError:
                        continue
                elif hasattr(exp, "year"):
                    exp_dt = exp if isinstance(exp, datetime) else datetime.combine(exp, datetime.min.time())
                else:
                    continue

                # Make naive for comparison
                if hasattr(exp_dt, "tzinfo") and exp_dt.tzinfo is not None:
                    exp_dt = exp_dt.replace(tzinfo=None)

                if exp_dt > expiry_cutoff:
                    continue
            else:
                continue

            # Determine call/put
            inst_class = defn.get("instrument_class", "")
            is_call = inst_class == "C"

            # Get OI — filter < 100
            oi = oi_lookup.get(instrument_id, 0)
            if oi < 100:
                continue

            volume = vol_lookup.get(instrument_id, 0)
            mid_price = mid_lookup.get(instrument_id, None)

            # Time to expiry in years
            days_to_exp = (exp_dt - datetime.now().replace(tzinfo=None)).days
            T = max(days_to_exp / 365.0, 1 / 365.0)  # Minimum 1 day

            # Compute IV
            iv = None
            if mid_price is not None and mid_price > 0:
                iv = compute_iv(mid_price, spot_price, strike, T, r, is_call)

            if iv is None:
                iv = 0.25  # Fallback IV

            # Compute Greeks
            greeks = bs_greeks(spot_price, strike, T, r, iv, is_call)

            # Compute GEX
            gex = compute_gex_per_strike(greeks["gamma"], oi, spot_price, is_call)

            chain_records.append({
                "strike": strike,
                "is_call": is_call,
                "oi": oi,
                "volume": volume,
                "iv": iv,
                "delta": greeks["delta"],
                "gamma": greeks["gamma"],
                "gex": gex,
                "expiration": exp_dt.strftime("%Y-%m-%d") if exp_dt else None,
                "instrument_id": instrument_id,
            })

        if not chain_records:
            return pd.DataFrame()

        return pd.DataFrame(chain_records)

    def _compute_gex_summary(self, chain_df):
        """Compute aggregate GEX metrics from chain DataFrame.

        Returns dict with net_gex, regime info, and per-strike GEX.
        """
        if chain_df.empty:
            return {"net_gex": 0, "net_gex_formatted": "$0",
                    "regime": "UNKNOWN", "regime_label": "Unknown"}

        # Aggregate GEX per strike
        gex_by_strike = chain_df.groupby("strike").agg(
            call_gex=("gex", lambda x: x[chain_df.loc[x.index, "is_call"]].sum()),
            put_gex=("gex", lambda x: x[~chain_df.loc[x.index, "is_call"]].sum()),
        ).reset_index()
        gex_by_strike["net_gex"] = gex_by_strike["call_gex"] + gex_by_strike["put_gex"]

        net_gex = chain_df["gex"].sum()

        if net_gex >= 0:
            regime = "LONG_GAMMA"
            regime_label = "Mean-Reverting"
        else:
            regime = "SHORT_GAMMA"
            regime_label = "Trend-Following"

        return {
            "net_gex": net_gex,
            "net_gex_formatted": format_gex(net_gex),
            "regime": regime,
            "regime_label": regime_label,
        }

    def _get_gex_by_strike(self, chain_df):
        """Get per-strike GEX breakdown for level detection."""
        if chain_df.empty:
            return pd.DataFrame()

        calls = chain_df[chain_df["is_call"] == True]  # noqa: E712
        puts = chain_df[chain_df["is_call"] == False]  # noqa: E712

        call_gex = calls.groupby("strike").agg(
            call_oi=("oi", "sum"),
            call_gex=("gex", "sum"),
        )
        put_gex = puts.groupby("strike").agg(
            put_oi=("oi", "sum"),
            put_gex=("gex", "sum"),
        )

        merged = call_gex.join(put_gex, how="outer").fillna(0).reset_index()
        merged["net_gex"] = merged["call_gex"] + merged["put_gex"]

        return merged

    def _get_top_strikes(self, chain_df, n=10):
        """Get top N strikes by total OI."""
        if chain_df.empty:
            return []

        gex_df = self._get_gex_by_strike(chain_df)
        if gex_df.empty:
            return []

        gex_df["total_oi"] = gex_df["call_oi"] + gex_df["put_oi"]
        top = gex_df.nlargest(n, "total_oi")

        return [
            {
                "strike": float(row["strike"]),
                "call_oi": int(row["call_oi"]),
                "put_oi": int(row["put_oi"]),
                "call_gex": float(row["call_gex"]),
                "put_gex": float(row["put_gex"]),
                "net_gex": float(row["net_gex"]),
            }
            for _, row in top.iterrows()
        ]

    def collect_ticker(self, ticker, spot_price=None):
        """Full GEX pipeline for one ticker.

        Args:
            ticker: symbol (e.g. "SPY")
            spot_price: current price. If None, must be fetched elsewhere.

        Returns:
            dict with gex, levels, skew, top_strikes, dealer_narrative
        """
        if spot_price is None or spot_price <= 0:
            logger.warning("No spot price for %s, skipping", ticker)
            return {}

        # Check cache
        cache_key = f"gamma:{ticker}"
        if cache_key in self._cache:
            if time.time() - self._cache_ts.get(cache_key, 0) < config.CACHE_TTL_SECONDS:
                return self._cache[cache_key]

        date = self._get_date_str()

        chain_df = self._build_chain(ticker, date, spot_price)
        if chain_df.empty:
            logger.warning("Empty chain for %s", ticker)
            return {}

        # GEX summary
        gex_data = self._compute_gex_summary(chain_df)

        # Per-strike GEX for level detection
        gex_df = self._get_gex_by_strike(chain_df)

        # Levels
        levels = {
            "pin_level": find_pin_level(chain_df),
            "put_wall": find_put_wall(chain_df),
            "call_wall": find_call_wall(chain_df),
            "gamma_flip": find_gamma_flip(gex_df),
        }

        # Skew
        skew = compute_skew(chain_df)

        # Top strikes
        top_strikes = self._get_top_strikes(chain_df)

        # Narrative
        narrative = generate_narrative(ticker, gex_data, levels, skew)

        # Nearest expiry used
        if not chain_df.empty and "expiration" in chain_df.columns:
            expiry_used = chain_df["expiration"].min()
        else:
            expiry_used = None

        result = {
            "gex": gex_data,
            "levels": levels,
            "skew": skew,
            "top_strikes": top_strikes,
            "dealer_narrative": narrative,
            "data_timestamp": datetime.now().isoformat(),
            "expiry_used": expiry_used,
            # Flattened fields for template compatibility
            "gamma_flip": levels.get("gamma_flip"),
            "put_wall": levels.get("put_wall"),
            "call_wall": levels.get("call_wall"),
            "net_gamma": gex_data.get("net_gex_formatted"),
            "dealer_positioning": narrative,
            "max_gamma_strike": levels.get("pin_level"),
        }

        # Cache
        self._cache[cache_key] = result
        self._cache_ts[cache_key] = time.time()

        return result

    def collect_all_tickers(self, quotes=None):
        """Run GEX pipeline for all CORE_TICKERS.

        Args:
            quotes: dict of {ticker: {"price": float, ...}} for spot prices.
                    If None, attempts to use quote data from FMP.

        Returns:
            dict of {ticker: gex_result_dict}
        """
        results = {}

        for ticker in config.CORE_TICKERS:
            spot = None
            if quotes and ticker in quotes:
                spot = quotes[ticker].get("price", 0)

            if not spot or spot <= 0:
                logger.warning("No spot price for %s, skipping GEX", ticker)
                continue

            try:
                result = self.collect_ticker(ticker, spot_price=spot)
                if result:
                    results[ticker] = result
            except Exception:
                logger.exception("GEX collection failed for %s", ticker)

        return results
