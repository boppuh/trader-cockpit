"""Tests for the GEX/Gamma Exposure computation module."""

import math
import sys
import unittest
from datetime import datetime, timedelta
from unittest.mock import MagicMock, patch

import numpy as np
import pandas as pd

# Ensure project dir is on path
sys.path.insert(0, ".")

from databento_gamma import (
    DatabentGammaCollector,
    bs_greeks,
    bs_price,
    compute_gex_per_strike,
    compute_iv,
    compute_skew,
    find_call_wall,
    find_gamma_flip,
    find_pin_level,
    find_put_wall,
    format_gex,
    generate_narrative,
)


# ── Black-Scholes Tests ───────────────────────────────────────────────────


class TestBlackScholes(unittest.TestCase):
    """Test Black-Scholes pricing and Greeks."""

    def test_atm_call_delta_near_05(self):
        """ATM call delta should be approximately 0.5 (drift from r>0 is expected)."""
        greeks = bs_greeks(S=100, K=100, T=0.25, r=0.043, sigma=0.20, is_call=True)
        self.assertAlmostEqual(greeks["delta"], 0.5, delta=0.08)

    def test_atm_put_delta_near_neg_05(self):
        """ATM put delta should be approximately -0.5."""
        greeks = bs_greeks(S=100, K=100, T=0.25, r=0.043, sigma=0.20, is_call=False)
        self.assertAlmostEqual(greeks["delta"], -0.5, delta=0.08)

    def test_deep_itm_call_delta_near_1(self):
        """Deep ITM call delta should be near 1.0."""
        greeks = bs_greeks(S=150, K=100, T=0.25, r=0.043, sigma=0.20, is_call=True)
        self.assertGreater(greeks["delta"], 0.95)

    def test_deep_otm_call_delta_near_0(self):
        """Deep OTM call delta should be near 0."""
        greeks = bs_greeks(S=50, K=100, T=0.25, r=0.043, sigma=0.20, is_call=True)
        self.assertLess(greeks["delta"], 0.05)

    def test_gamma_always_positive(self):
        """Gamma should always be positive for both calls and puts."""
        call_greeks = bs_greeks(S=100, K=100, T=0.25, r=0.043, sigma=0.20, is_call=True)
        put_greeks = bs_greeks(S=100, K=100, T=0.25, r=0.043, sigma=0.20, is_call=False)
        self.assertGreater(call_greeks["gamma"], 0)
        self.assertGreater(put_greeks["gamma"], 0)

    def test_call_put_gamma_equal(self):
        """Call and put gamma at the same strike should be equal."""
        call_greeks = bs_greeks(S=100, K=100, T=0.25, r=0.043, sigma=0.20, is_call=True)
        put_greeks = bs_greeks(S=100, K=100, T=0.25, r=0.043, sigma=0.20, is_call=False)
        self.assertAlmostEqual(call_greeks["gamma"], put_greeks["gamma"], places=10)

    def test_atm_gamma_highest(self):
        """ATM gamma should be higher than OTM gamma."""
        atm = bs_greeks(S=100, K=100, T=0.25, r=0.043, sigma=0.20, is_call=True)
        otm = bs_greeks(S=100, K=120, T=0.25, r=0.043, sigma=0.20, is_call=True)
        self.assertGreater(atm["gamma"], otm["gamma"])

    def test_bs_price_call_positive(self):
        """Call price should be positive for reasonable inputs."""
        price = bs_price(S=100, K=100, T=0.25, r=0.043, sigma=0.20, is_call=True)
        self.assertGreater(price, 0)

    def test_bs_price_put_positive(self):
        """Put price should be positive for reasonable inputs."""
        price = bs_price(S=100, K=100, T=0.25, r=0.043, sigma=0.20, is_call=False)
        self.assertGreater(price, 0)

    def test_put_call_parity(self):
        """Put-call parity: C - P = S - K*exp(-rT)."""
        S, K, T, r, sigma = 100, 100, 0.25, 0.043, 0.30
        call_price = bs_price(S, K, T, r, sigma, is_call=True)
        put_price = bs_price(S, K, T, r, sigma, is_call=False)
        parity_rhs = S - K * math.exp(-r * T)
        self.assertAlmostEqual(call_price - put_price, parity_rhs, places=6)

    def test_zero_time_returns_zero(self):
        """Zero time to expiry should return zero greeks."""
        greeks = bs_greeks(S=100, K=100, T=0, r=0.043, sigma=0.20, is_call=True)
        self.assertEqual(greeks["delta"], 0.0)
        self.assertEqual(greeks["gamma"], 0.0)

    def test_zero_vol_returns_zero(self):
        """Zero volatility should return zero greeks."""
        greeks = bs_greeks(S=100, K=100, T=0.25, r=0.043, sigma=0, is_call=True)
        self.assertEqual(greeks["delta"], 0.0)
        self.assertEqual(greeks["gamma"], 0.0)


# ── Implied Volatility Tests ──────────────────────────────────────────────


class TestImpliedVolatility(unittest.TestCase):
    """Test Newton-Raphson IV solver."""

    def test_iv_roundtrip_call(self):
        """IV solver should recover known volatility from BS price."""
        S, K, T, r, true_sigma = 100, 100, 0.25, 0.043, 0.25
        price = bs_price(S, K, T, r, true_sigma, is_call=True)
        recovered_iv = compute_iv(price, S, K, T, r, is_call=True)
        self.assertIsNotNone(recovered_iv)
        self.assertAlmostEqual(recovered_iv, true_sigma, places=3)

    def test_iv_roundtrip_put(self):
        """IV solver should recover known volatility from BS put price."""
        S, K, T, r, true_sigma = 100, 105, 0.25, 0.043, 0.30
        price = bs_price(S, K, T, r, true_sigma, is_call=False)
        recovered_iv = compute_iv(price, S, K, T, r, is_call=False)
        self.assertIsNotNone(recovered_iv)
        self.assertAlmostEqual(recovered_iv, true_sigma, places=3)

    def test_iv_high_vol(self):
        """IV solver should handle high volatility."""
        S, K, T, r, true_sigma = 100, 100, 0.25, 0.043, 0.80
        price = bs_price(S, K, T, r, true_sigma, is_call=True)
        recovered_iv = compute_iv(price, S, K, T, r, is_call=True)
        self.assertIsNotNone(recovered_iv)
        self.assertAlmostEqual(recovered_iv, true_sigma, delta=0.02)

    def test_iv_zero_price_returns_none(self):
        """IV solver should return None for zero mid-price."""
        result = compute_iv(0, 100, 100, 0.25, 0.043, is_call=True)
        self.assertIsNone(result)

    def test_iv_negative_price_returns_none(self):
        """IV solver should return None for negative price."""
        result = compute_iv(-1, 100, 100, 0.25, 0.043, is_call=True)
        self.assertIsNone(result)

    def test_iv_otm_call(self):
        """IV solver should work for OTM calls."""
        S, K, T, r, true_sigma = 100, 110, 0.25, 0.043, 0.25
        price = bs_price(S, K, T, r, true_sigma, is_call=True)
        recovered_iv = compute_iv(price, S, K, T, r, is_call=True)
        self.assertIsNotNone(recovered_iv)
        self.assertAlmostEqual(recovered_iv, true_sigma, delta=0.01)


# ── GEX Computation Tests ────────────────────────────────────────────────


class TestGEXComputation(unittest.TestCase):
    """Test GEX calculations."""

    def test_call_gex_positive(self):
        """Call GEX should be positive."""
        gex = compute_gex_per_strike(gamma=0.05, oi=1000, spot=585, is_call=True)
        self.assertGreater(gex, 0)

    def test_put_gex_negative(self):
        """Put GEX should be negative (dealer model)."""
        gex = compute_gex_per_strike(gamma=0.05, oi=1000, spot=585, is_call=False)
        self.assertLess(gex, 0)

    def test_gex_formula(self):
        """Verify GEX formula: gamma * OI * 100 * S^2 * 0.01."""
        gamma = 0.05
        oi = 1000
        spot = 500
        expected = gamma * oi * 100 * spot ** 2 * 0.01
        gex = compute_gex_per_strike(gamma, oi, spot, is_call=True)
        self.assertAlmostEqual(gex, expected, places=2)

    def test_gex_put_magnitude_equals_call(self):
        """Put and call GEX should have same magnitude, opposite signs."""
        call_gex = compute_gex_per_strike(0.05, 1000, 500, is_call=True)
        put_gex = compute_gex_per_strike(0.05, 1000, 500, is_call=False)
        self.assertAlmostEqual(abs(call_gex), abs(put_gex), places=2)
        self.assertGreater(call_gex, 0)
        self.assertLess(put_gex, 0)

    def test_zero_oi_zero_gex(self):
        """Zero OI should produce zero GEX."""
        gex = compute_gex_per_strike(0.05, 0, 500, is_call=True)
        self.assertEqual(gex, 0)

    def test_zero_gamma_zero_gex(self):
        """Zero gamma should produce zero GEX."""
        gex = compute_gex_per_strike(0, 1000, 500, is_call=True)
        self.assertEqual(gex, 0)


# ── Format Tests ─────────────────────────────────────────────────────────


class TestFormatGEX(unittest.TestCase):
    """Test GEX formatting."""

    def test_billions(self):
        self.assertEqual(format_gex(2.1e9), "+$2.1B")

    def test_negative_billions(self):
        self.assertEqual(format_gex(-1.5e9), "-$1.5B")

    def test_millions(self):
        self.assertEqual(format_gex(450e6), "+$450M")

    def test_thousands(self):
        self.assertEqual(format_gex(50e3), "+$50K")

    def test_zero(self):
        self.assertEqual(format_gex(0), "+$0")


# ── Level Detection Tests ────────────────────────────────────────────────


class TestLevelDetection(unittest.TestCase):
    """Test pin, wall, and gamma flip detection."""

    def _make_chain(self):
        """Create a mock options chain DataFrame."""
        return pd.DataFrame([
            {"strike": 580, "is_call": True, "oi": 5000, "volume": 1000},
            {"strike": 580, "is_call": False, "oi": 8000, "volume": 2000},
            {"strike": 585, "is_call": True, "oi": 15000, "volume": 5000},
            {"strike": 585, "is_call": False, "oi": 12000, "volume": 3000},
            {"strike": 590, "is_call": True, "oi": 20000, "volume": 8000},
            {"strike": 590, "is_call": False, "oi": 3000, "volume": 1000},
            {"strike": 595, "is_call": True, "oi": 10000, "volume": 4000},
            {"strike": 595, "is_call": False, "oi": 2000, "volume": 500},
        ])

    def test_pin_level_highest_total_oi(self):
        """Pin level should be the strike with highest total OI."""
        chain = self._make_chain()
        # 585: 15000+12000=27000, 590: 20000+3000=23000
        pin = find_pin_level(chain)
        self.assertEqual(pin, 585)

    def test_put_wall_highest_put_oi(self):
        """Put wall should be the strike with highest put OI."""
        chain = self._make_chain()
        # 585: 12000 puts
        put_wall = find_put_wall(chain)
        self.assertEqual(put_wall, 585)

    def test_call_wall_highest_call_oi(self):
        """Call wall should be the strike with highest call OI."""
        chain = self._make_chain()
        # 590: 20000 calls
        call_wall = find_call_wall(chain)
        self.assertEqual(call_wall, 590)

    def test_gamma_flip_sign_change(self):
        """Gamma flip should be at the strike where cumulative GEX changes sign."""
        # cumulative: -100, -200, -100, +200, +500
        # sign flips from negative to positive at strike 590
        gex_df = pd.DataFrame([
            {"strike": 575, "net_gex": -100e6},
            {"strike": 580, "net_gex": -100e6},
            {"strike": 585, "net_gex": 100e6},
            {"strike": 590, "net_gex": 300e6},
            {"strike": 595, "net_gex": 300e6},
        ])
        flip = find_gamma_flip(gex_df)
        self.assertEqual(flip, 590)

    def test_gamma_flip_no_sign_change(self):
        """Gamma flip should be None when all GEX is same sign."""
        gex_df = pd.DataFrame([
            {"strike": 580, "net_gex": 100e6},
            {"strike": 585, "net_gex": 200e6},
            {"strike": 590, "net_gex": 300e6},
        ])
        flip = find_gamma_flip(gex_df)
        self.assertIsNone(flip)

    def test_empty_chain_returns_none(self):
        """Empty DataFrame should return None for all levels."""
        empty = pd.DataFrame()
        self.assertIsNone(find_pin_level(empty))
        self.assertIsNone(find_put_wall(empty))
        self.assertIsNone(find_call_wall(empty))
        self.assertIsNone(find_gamma_flip(empty))


# ── Skew Tests ───────────────────────────────────────────────────────────


class TestSkew(unittest.TestCase):
    """Test put/call skew computation."""

    def test_bearish_skew(self):
        """High put/call ratio should be bearish."""
        chain = pd.DataFrame([
            {"strike": 580, "is_call": True, "oi": 1000, "volume": 500},
            {"strike": 580, "is_call": False, "oi": 2000, "volume": 1000},
        ])
        skew = compute_skew(chain)
        self.assertGreater(skew["put_call_ratio"], 1.0)
        self.assertIn("BEARISH", skew["sentiment"])

    def test_bullish_skew(self):
        """Low put/call ratio should be bullish."""
        chain = pd.DataFrame([
            {"strike": 580, "is_call": True, "oi": 3000, "volume": 1500},
            {"strike": 580, "is_call": False, "oi": 1500, "volume": 500},
        ])
        skew = compute_skew(chain)
        self.assertLess(skew["put_call_ratio"], 1.0)

    def test_empty_chain_neutral(self):
        """Empty chain should return neutral."""
        skew = compute_skew(pd.DataFrame())
        self.assertEqual(skew["sentiment"], "NEUTRAL")


# ── Narrative Tests ──────────────────────────────────────────────────────


class TestNarrative(unittest.TestCase):
    """Test dealer positioning narrative generation."""

    def test_long_gamma_narrative(self):
        """Long gamma narrative should mention LONG and mean-reverting."""
        gex_data = {
            "net_gex": 2.1e9,
            "net_gex_formatted": "+$2.1B",
            "regime": "LONG_GAMMA",
            "regime_label": "Mean-Reverting",
        }
        levels = {"pin_level": 585, "put_wall": 575, "call_wall": 595, "gamma_flip": 580}
        skew = {"put_call_ratio": 1.15, "sentiment": "SLIGHTLY_BEARISH"}
        narrative = generate_narrative("SPY", gex_data, levels, skew)
        self.assertIn("LONG", narrative)
        self.assertIn("585", narrative)
        self.assertIn("575", narrative)
        self.assertIn("595", narrative)
        self.assertIn("MEAN-REVERTING", narrative)

    def test_short_gamma_narrative(self):
        """Short gamma narrative should mention SHORT and trend-following."""
        gex_data = {
            "net_gex": -500e6,
            "net_gex_formatted": "-$500M",
            "regime": "SHORT_GAMMA",
            "regime_label": "Trend-Following",
        }
        levels = {"pin_level": 580, "put_wall": 570, "call_wall": 590, "gamma_flip": 578}
        skew = {"put_call_ratio": 0.8, "sentiment": "NEUTRAL"}
        narrative = generate_narrative("QQQ", gex_data, levels, skew)
        self.assertIn("SHORT", narrative)
        self.assertIn("TREND-FOLLOWING", narrative)


# ── DatabentGammaCollector Integration Tests (Mock) ──────────────────────


class TestDatabentGammaCollector(unittest.TestCase):
    """Integration tests with mock Databento client."""

    def _make_mock_client(self, spot=585.0):
        """Create a mock Databento client that returns realistic data."""
        client = MagicMock()
        now = datetime.now()
        expiry = now + timedelta(days=5)
        expiry_str = expiry.strftime("%Y-%m-%d")

        # Mock definitions
        defs_data = MagicMock()
        defs_df = pd.DataFrame([
            {
                "instrument_id": 1001,
                "strike_price": int(580 * 1e9),
                "instrument_class": "C",
                "expiration": expiry,
                "underlying": "SPY",
                "raw_symbol": "SPY260321C580",
            },
            {
                "instrument_id": 1002,
                "strike_price": int(580 * 1e9),
                "instrument_class": "P",
                "expiration": expiry,
                "underlying": "SPY",
                "raw_symbol": "SPY260321P580",
            },
            {
                "instrument_id": 1003,
                "strike_price": int(585 * 1e9),
                "instrument_class": "C",
                "expiration": expiry,
                "underlying": "SPY",
                "raw_symbol": "SPY260321C585",
            },
            {
                "instrument_id": 1004,
                "strike_price": int(585 * 1e9),
                "instrument_class": "P",
                "expiration": expiry,
                "underlying": "SPY",
                "raw_symbol": "SPY260321P585",
            },
            {
                "instrument_id": 1005,
                "strike_price": int(590 * 1e9),
                "instrument_class": "C",
                "expiration": expiry,
                "underlying": "SPY",
                "raw_symbol": "SPY260321C590",
            },
            {
                "instrument_id": 1006,
                "strike_price": int(590 * 1e9),
                "instrument_class": "P",
                "expiration": expiry,
                "underlying": "SPY",
                "raw_symbol": "SPY260321P590",
            },
        ])
        defs_data.to_df.return_value = defs_df

        # Mock OI (statistics with stat_type=9)
        oi_data = MagicMock()
        oi_df = pd.DataFrame([
            {"instrument_id": 1001, "stat_type": 9, "quantity": 5000},
            {"instrument_id": 1002, "stat_type": 9, "quantity": 8000},
            {"instrument_id": 1003, "stat_type": 9, "quantity": 15000},
            {"instrument_id": 1004, "stat_type": 9, "quantity": 12000},
            {"instrument_id": 1005, "stat_type": 9, "quantity": 10000},
            {"instrument_id": 1006, "stat_type": 9, "quantity": 3000},
        ])
        oi_data.to_df.return_value = oi_df

        # Mock volume
        vol_data = MagicMock()
        vol_df = pd.DataFrame([
            {"instrument_id": 1001, "volume": 2000},
            {"instrument_id": 1002, "volume": 3000},
            {"instrument_id": 1003, "volume": 8000},
            {"instrument_id": 1004, "volume": 5000},
            {"instrument_id": 1005, "volume": 6000},
            {"instrument_id": 1006, "volume": 1000},
        ])
        vol_data.to_df.return_value = vol_df

        # Mock mid prices (bid/ask as int64 fixed-point)
        mid_data = MagicMock()
        mid_df = pd.DataFrame([
            {"instrument_id": 1001, "bid_px_00": int(3.50 * 1e9), "ask_px_00": int(3.70 * 1e9)},
            {"instrument_id": 1002, "bid_px_00": int(1.20 * 1e9), "ask_px_00": int(1.40 * 1e9)},
            {"instrument_id": 1003, "bid_px_00": int(2.80 * 1e9), "ask_px_00": int(3.00 * 1e9)},
            {"instrument_id": 1004, "bid_px_00": int(2.10 * 1e9), "ask_px_00": int(2.30 * 1e9)},
            {"instrument_id": 1005, "bid_px_00": int(1.50 * 1e9), "ask_px_00": int(1.70 * 1e9)},
            {"instrument_id": 1006, "bid_px_00": int(3.80 * 1e9), "ask_px_00": int(4.00 * 1e9)},
        ])
        mid_data.to_df.return_value = mid_df

        def get_range_side_effect(**kwargs):
            schema = kwargs.get("schema", "")
            if schema == "definition":
                return defs_data
            elif schema == "statistics":
                return oi_data
            elif schema == "ohlcv-1d":
                return vol_data
            elif schema == "mbp-1":
                return mid_data
            return MagicMock(to_df=MagicMock(return_value=pd.DataFrame()))

        client.timeseries.get_range = MagicMock(side_effect=get_range_side_effect)
        return client

    def test_collect_ticker_returns_expected_keys(self):
        """collect_ticker should return dict with all expected top-level keys."""
        mock_client = self._make_mock_client()
        collector = DatabentGammaCollector(client=mock_client)
        result = collector.collect_ticker("SPY", spot_price=585.0)

        self.assertIn("gex", result)
        self.assertIn("levels", result)
        self.assertIn("skew", result)
        self.assertIn("top_strikes", result)
        self.assertIn("dealer_narrative", result)
        self.assertIn("data_timestamp", result)

    def test_collect_ticker_gex_structure(self):
        """GEX output should have net_gex, regime, and formatted value."""
        mock_client = self._make_mock_client()
        collector = DatabentGammaCollector(client=mock_client)
        result = collector.collect_ticker("SPY", spot_price=585.0)

        gex = result["gex"]
        self.assertIn("net_gex", gex)
        self.assertIn("regime", gex)
        self.assertIn("regime_label", gex)
        self.assertIn("net_gex_formatted", gex)
        self.assertIn(gex["regime"], ("LONG_GAMMA", "SHORT_GAMMA"))

    def test_collect_ticker_levels_structure(self):
        """Levels should contain pin, walls, and gamma flip."""
        mock_client = self._make_mock_client()
        collector = DatabentGammaCollector(client=mock_client)
        result = collector.collect_ticker("SPY", spot_price=585.0)

        levels = result["levels"]
        self.assertIn("pin_level", levels)
        self.assertIn("put_wall", levels)
        self.assertIn("call_wall", levels)
        self.assertIn("gamma_flip", levels)

    def test_collect_ticker_pin_level_correct(self):
        """Pin level should be 585 (highest total OI: 15000+12000=27000)."""
        mock_client = self._make_mock_client()
        collector = DatabentGammaCollector(client=mock_client)
        result = collector.collect_ticker("SPY", spot_price=585.0)
        self.assertEqual(result["levels"]["pin_level"], 585)

    def test_collect_ticker_call_wall_correct(self):
        """Call wall should be 585 (highest call OI: 15000)."""
        mock_client = self._make_mock_client()
        collector = DatabentGammaCollector(client=mock_client)
        result = collector.collect_ticker("SPY", spot_price=585.0)
        self.assertEqual(result["levels"]["call_wall"], 585)

    def test_collect_ticker_put_wall_correct(self):
        """Put wall should be 585 (highest put OI: 12000)."""
        mock_client = self._make_mock_client()
        collector = DatabentGammaCollector(client=mock_client)
        result = collector.collect_ticker("SPY", spot_price=585.0)
        self.assertEqual(result["levels"]["put_wall"], 585)

    def test_collect_ticker_skew_structure(self):
        """Skew should have put_call_ratio, volume_put_call, sentiment."""
        mock_client = self._make_mock_client()
        collector = DatabentGammaCollector(client=mock_client)
        result = collector.collect_ticker("SPY", spot_price=585.0)

        skew = result["skew"]
        self.assertIn("put_call_ratio", skew)
        self.assertIn("volume_put_call", skew)
        self.assertIn("sentiment", skew)

    def test_collect_ticker_top_strikes(self):
        """top_strikes should be a list of dicts with expected keys."""
        mock_client = self._make_mock_client()
        collector = DatabentGammaCollector(client=mock_client)
        result = collector.collect_ticker("SPY", spot_price=585.0)

        top = result["top_strikes"]
        self.assertIsInstance(top, list)
        self.assertGreater(len(top), 0)
        for item in top:
            self.assertIn("strike", item)
            self.assertIn("call_oi", item)
            self.assertIn("put_oi", item)
            self.assertIn("net_gex", item)

    def test_collect_ticker_template_compat_fields(self):
        """Result should include flattened fields for template compatibility."""
        mock_client = self._make_mock_client()
        collector = DatabentGammaCollector(client=mock_client)
        result = collector.collect_ticker("SPY", spot_price=585.0)

        self.assertIn("gamma_flip", result)
        self.assertIn("put_wall", result)
        self.assertIn("call_wall", result)
        self.assertIn("net_gamma", result)
        self.assertIn("dealer_positioning", result)
        self.assertIn("max_gamma_strike", result)

    def test_collect_ticker_no_spot_returns_empty(self):
        """collect_ticker with no spot price should return empty dict."""
        mock_client = self._make_mock_client()
        collector = DatabentGammaCollector(client=mock_client)
        result = collector.collect_ticker("SPY", spot_price=None)
        self.assertEqual(result, {})

    def test_collect_ticker_zero_spot_returns_empty(self):
        """collect_ticker with zero spot price should return empty dict."""
        mock_client = self._make_mock_client()
        collector = DatabentGammaCollector(client=mock_client)
        result = collector.collect_ticker("SPY", spot_price=0)
        self.assertEqual(result, {})

    def test_collect_all_tickers(self):
        """collect_all_tickers should process multiple tickers."""
        mock_client = self._make_mock_client()
        collector = DatabentGammaCollector(client=mock_client)
        quotes = {
            "SPY": {"price": 585.0},
            "QQQ": {"price": 500.0},
        }
        results = collector.collect_all_tickers(quotes=quotes)
        self.assertIn("SPY", results)
        self.assertIn("QQQ", results)

    def test_collect_all_tickers_missing_spot_skips(self):
        """Tickers without spot price should be skipped."""
        mock_client = self._make_mock_client()
        collector = DatabentGammaCollector(client=mock_client)
        quotes = {"SPY": {"price": 585.0}}
        results = collector.collect_all_tickers(quotes=quotes)
        self.assertIn("SPY", results)
        # Other tickers should be skipped (no quotes)
        self.assertNotIn("NVDA", results)

    def test_no_api_key_returns_empty(self):
        """Missing API key should return empty from collect_all_tickers."""
        collector = DatabentGammaCollector()
        with patch("config.DATABENTO_API_KEY", ""):
            results = collector.collect_all_tickers(
                quotes={"SPY": {"price": 585.0}}
            )
            # Client will be None, so chain will be empty -> skip
            self.assertEqual(results, {})

    def test_caching(self):
        """Second call should use cached result (client not called twice)."""
        mock_client = self._make_mock_client()
        collector = DatabentGammaCollector(client=mock_client)

        result1 = collector.collect_ticker("SPY", spot_price=585.0)
        call_count_1 = mock_client.timeseries.get_range.call_count

        result2 = collector.collect_ticker("SPY", spot_price=585.0)
        call_count_2 = mock_client.timeseries.get_range.call_count

        # Should not have made additional API calls
        self.assertEqual(call_count_1, call_count_2)
        self.assertEqual(result1, result2)


# ── collect_options_data Integration Test ─────────────────────────────────


class TestCollectOptionsDataDelegation(unittest.TestCase):
    """Test that collect_options_data delegates to DatabentGammaCollector."""

    @patch("config.OPTIONS_SOURCE", "databento")
    @patch("collect_data._load_manual_override", return_value=None)
    def test_delegates_to_databento(self, mock_manual):
        """When OPTIONS_SOURCE is 'databento', should create DatabentGammaCollector."""
        with patch("databento_gamma.DatabentGammaCollector") as MockCollector:
            mock_instance = MockCollector.return_value
            mock_instance.collect_all_tickers.return_value = {"SPY": {"gex": {}}}

            from collect_data import collect_options_data
            result = collect_options_data(quotes={"SPY": {"price": 585}})

            MockCollector.assert_called_once()
            mock_instance.collect_all_tickers.assert_called_once_with(
                quotes={"SPY": {"price": 585}}
            )
            self.assertEqual(result, {"SPY": {"gex": {}}})

    @patch("config.OPTIONS_SOURCE", "stub")
    @patch("collect_data._load_manual_override", return_value=None)
    def test_stub_returns_empty(self, mock_manual):
        """When OPTIONS_SOURCE is 'stub', should return empty dict."""
        from collect_data import collect_options_data
        result = collect_options_data()
        self.assertEqual(result, {})

    @patch("config.OPTIONS_SOURCE", "databento")
    @patch("collect_data._load_manual_override")
    def test_manual_override_takes_priority(self, mock_manual):
        """Manual override should take priority over databento."""
        mock_manual.return_value = {"SPY": {"manual": True}}
        from collect_data import collect_options_data
        result = collect_options_data()
        self.assertEqual(result, {"SPY": {"manual": True}})


if __name__ == "__main__":
    unittest.main()
