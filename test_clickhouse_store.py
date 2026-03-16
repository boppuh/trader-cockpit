"""Tests for the ClickHouse persistence layer (clickhouse_store.py).

Uses a mock clickhouse_driver.Client to verify DDL, insert logic,
and query behavior without requiring a live ClickHouse instance.
"""

import sys
import unittest
from datetime import date, datetime
from unittest.mock import MagicMock, call, patch

sys.path.insert(0, ".")

from clickhouse_store import (
    CREATE_GEX_LEVELS,
    CREATE_GEX_STRIKES,
    GEXClickHouseStore,
)


def _make_gex_result(
    net_gex=2.1e9,
    regime="LONG_GAMMA",
    pin=585,
    put_wall=575,
    call_wall=595,
    gamma_flip=580,
):
    """Create a realistic GEX result dict matching DatabentGammaCollector output."""
    return {
        "gex": {
            "net_gex": net_gex,
            "net_gex_formatted": "+$2.1B",
            "regime": regime,
            "regime_label": "Mean-Reverting",
        },
        "levels": {
            "pin_level": pin,
            "put_wall": put_wall,
            "call_wall": call_wall,
            "gamma_flip": gamma_flip,
        },
        "skew": {
            "put_call_ratio": 1.15,
            "volume_put_call": 0.95,
            "sentiment": "SLIGHTLY_BEARISH",
        },
        "top_strikes": [
            {
                "strike": 585,
                "call_oi": 15000,
                "put_oi": 12000,
                "call_gex": 500e6,
                "put_gex": -350e6,
                "net_gex": 150e6,
            },
            {
                "strike": 590,
                "call_oi": 10000,
                "put_oi": 3000,
                "call_gex": 300e6,
                "put_gex": -100e6,
                "net_gex": 200e6,
            },
        ],
        "dealer_narrative": "Dealers net LONG gamma (+$2.1B). SPY pinned to 585.",
        "data_timestamp": "2026-03-16T14:30:00",
        "expiry_used": "2026-03-21",
    }


# ── Table Creation Tests ─────────────────────────────────────────────────


class TestTableCreation(unittest.TestCase):
    """Test DDL execution and table creation."""

    def test_ensure_tables_executes_ddl(self):
        """ensure_tables should execute both CREATE TABLE statements."""
        mock_client = MagicMock()
        store = GEXClickHouseStore(client=mock_client)
        result = store.ensure_tables()

        self.assertTrue(result)
        self.assertEqual(mock_client.execute.call_count, 2)
        # Verify both DDL templates were used
        calls = mock_client.execute.call_args_list
        self.assertIn("gex_levels", calls[0][0][0])
        self.assertIn("gex_strikes", calls[1][0][0])

    def test_ensure_tables_idempotent(self):
        """Second call should not re-execute DDL."""
        mock_client = MagicMock()
        store = GEXClickHouseStore(client=mock_client)
        store.ensure_tables()
        store.ensure_tables()

        # Only 2 calls (one per table), not 4
        self.assertEqual(mock_client.execute.call_count, 2)

    def test_ensure_tables_failure_returns_false(self):
        """DDL failure should return False."""
        mock_client = MagicMock()
        mock_client.execute.side_effect = Exception("Connection refused")
        store = GEXClickHouseStore(client=mock_client)
        result = store.ensure_tables()

        self.assertFalse(result)

    @patch("config.CH_HOST", "")
    def test_no_host_returns_none_client(self):
        """Missing CH_HOST should result in None client."""
        store = GEXClickHouseStore()
        self.assertIsNone(store.client)


# ── Insert Tests ─────────────────────────────────────────────────────────


class TestStoreTickerResult(unittest.TestCase):
    """Test inserting GEX data into ClickHouse."""

    def test_store_inserts_levels_and_strikes(self):
        """store_ticker_result should insert into both tables."""
        mock_client = MagicMock()
        store = GEXClickHouseStore(client=mock_client)
        result = _make_gex_result()
        ts = datetime(2026, 3, 16, 14, 30, 0)

        success = store.store_ticker_result("SPY", result, 585.0, snapshot_ts=ts)

        self.assertTrue(success)
        # 2 DDL + 2 INSERTs = 4 calls
        self.assertEqual(mock_client.execute.call_count, 4)

        # Verify gex_levels insert
        levels_call = mock_client.execute.call_args_list[2]
        self.assertIn("gex_levels", levels_call[0][0])
        levels_row = levels_call[0][1][0]
        self.assertEqual(levels_row["ticker"], "SPY")
        self.assertEqual(levels_row["spot_price"], 585.0)
        self.assertEqual(levels_row["pin_level"], 585)
        self.assertEqual(levels_row["put_wall"], 575)
        self.assertEqual(levels_row["call_wall"], 595)
        self.assertEqual(levels_row["gamma_flip"], 580)
        self.assertEqual(levels_row["regime"], "LONG_GAMMA")
        self.assertAlmostEqual(levels_row["pc_ratio_oi"], 1.15)
        self.assertIn("Dealers net LONG", levels_row["narrative"])

        # Verify gex_strikes insert
        strikes_call = mock_client.execute.call_args_list[3]
        self.assertIn("gex_strikes", strikes_call[0][0])
        strike_rows = strikes_call[0][1]
        self.assertEqual(len(strike_rows), 2)
        self.assertEqual(strike_rows[0]["strike"], 585)
        self.assertEqual(strike_rows[0]["call_oi"], 15000)
        self.assertEqual(strike_rows[1]["strike"], 590)

    def test_store_empty_result_returns_false(self):
        """Empty result dict should return False without inserting."""
        mock_client = MagicMock()
        store = GEXClickHouseStore(client=mock_client)
        success = store.store_ticker_result("SPY", {}, 585.0)
        self.assertFalse(success)

    def test_store_none_result_returns_false(self):
        """None result should return False."""
        mock_client = MagicMock()
        store = GEXClickHouseStore(client=mock_client)
        success = store.store_ticker_result("SPY", None, 585.0)
        self.assertFalse(success)

    def test_store_handles_none_levels_gracefully(self):
        """Result with None level values should default to 0."""
        mock_client = MagicMock()
        store = GEXClickHouseStore(client=mock_client)
        result = _make_gex_result(pin=None, put_wall=None, call_wall=None, gamma_flip=None)

        success = store.store_ticker_result("SPY", result, 585.0)
        self.assertTrue(success)

        levels_row = mock_client.execute.call_args_list[2][0][1][0]
        self.assertEqual(levels_row["pin_level"], 0)
        self.assertEqual(levels_row["put_wall"], 0)
        self.assertEqual(levels_row["call_wall"], 0)
        self.assertEqual(levels_row["gamma_flip"], 0)

    def test_store_no_top_strikes_skips_strikes_insert(self):
        """Result with empty top_strikes should skip strikes insert."""
        mock_client = MagicMock()
        store = GEXClickHouseStore(client=mock_client)
        result = _make_gex_result()
        result["top_strikes"] = []

        success = store.store_ticker_result("SPY", result, 585.0)
        self.assertTrue(success)
        # 2 DDL + 1 levels INSERT (no strikes INSERT) = 3 calls
        self.assertEqual(mock_client.execute.call_count, 3)

    def test_store_insert_failure_returns_false(self):
        """Insert failure should return False."""
        mock_client = MagicMock()
        call_count = [0]

        def side_effect(*args, **kwargs):
            call_count[0] += 1
            if call_count[0] > 2:  # Fail on insert (after DDL)
                raise Exception("Insert failed")

        mock_client.execute.side_effect = side_effect
        store = GEXClickHouseStore(client=mock_client)
        result = _make_gex_result()

        success = store.store_ticker_result("SPY", result, 585.0)
        self.assertFalse(success)

    def test_store_default_timestamp(self):
        """Should use current UTC time when snapshot_ts not provided."""
        mock_client = MagicMock()
        store = GEXClickHouseStore(client=mock_client)
        result = _make_gex_result()

        store.store_ticker_result("SPY", result, 585.0)

        levels_row = mock_client.execute.call_args_list[2][0][1][0]
        self.assertIsInstance(levels_row["snapshot_ts"], datetime)
        self.assertIsInstance(levels_row["date"], date)


# ── Batch Store Tests ────────────────────────────────────────────────────


class TestStoreAllResults(unittest.TestCase):
    """Test batch persistence of all tickers."""

    def test_store_all_multiple_tickers(self):
        """store_all_results should persist each ticker."""
        mock_client = MagicMock()
        store = GEXClickHouseStore(client=mock_client)

        results = {
            "SPY": _make_gex_result(),
            "QQQ": _make_gex_result(net_gex=-500e6, regime="SHORT_GAMMA"),
        }
        quotes = {
            "SPY": {"price": 585.0},
            "QQQ": {"price": 500.0},
        }

        statuses = store.store_all_results(results, quotes)

        self.assertTrue(statuses["SPY"])
        self.assertTrue(statuses["QQQ"])

    def test_store_all_missing_quote_uses_zero(self):
        """Ticker with no quote should still persist with spot=0."""
        mock_client = MagicMock()
        store = GEXClickHouseStore(client=mock_client)

        results = {"SPY": _make_gex_result()}
        quotes = {}  # No quotes

        statuses = store.store_all_results(results, quotes)
        self.assertTrue(statuses["SPY"])

        # Spot price should be 0
        levels_row = mock_client.execute.call_args_list[2][0][1][0]
        self.assertEqual(levels_row["spot_price"], 0)

    def test_store_all_empty_results(self):
        """Empty results dict should return empty statuses."""
        mock_client = MagicMock()
        store = GEXClickHouseStore(client=mock_client)
        statuses = store.store_all_results({}, {})
        self.assertEqual(statuses, {})


# ── Query Tests ──────────────────────────────────────────────────────────


class TestQueryMethods(unittest.TestCase):
    """Test query utility methods."""

    def test_query_latest_levels(self):
        """query_latest_levels should execute SELECT and return dicts."""
        mock_client = MagicMock()
        mock_client.execute.return_value = (
            [("SPY", datetime(2026, 3, 16), 585.0, "LONG_GAMMA")],
            [("ticker", "String"), ("snapshot_ts", "DateTime"),
             ("spot_price", "Float64"), ("regime", "String")],
        )
        store = GEXClickHouseStore(client=mock_client)
        rows = store.query_latest_levels(ticker="SPY", limit=5)

        self.assertEqual(len(rows), 1)
        self.assertEqual(rows[0]["ticker"], "SPY")
        self.assertEqual(rows[0]["regime"], "LONG_GAMMA")

    def test_query_latest_levels_no_data(self):
        """Empty result should return empty list."""
        mock_client = MagicMock()
        mock_client.execute.return_value = ([], [])
        store = GEXClickHouseStore(client=mock_client)
        rows = store.query_latest_levels()
        self.assertEqual(rows, [])

    def test_query_latest_levels_no_client(self):
        """No client should return empty list."""
        store = GEXClickHouseStore()
        with patch("config.CH_HOST", ""):
            rows = store.query_latest_levels()
            self.assertEqual(rows, [])

    def test_query_levels_history(self):
        """query_levels_history should pass ticker and days params."""
        mock_client = MagicMock()
        mock_client.execute.return_value = (
            [("SPY", date(2026, 3, 15), 580.0), ("SPY", date(2026, 3, 16), 585.0)],
            [("ticker", "String"), ("date", "Date"), ("spot_price", "Float64")],
        )
        store = GEXClickHouseStore(client=mock_client)
        rows = store.query_levels_history("SPY", days=7)

        self.assertEqual(len(rows), 2)
        # Verify parameterized query was used
        query_call = mock_client.execute.call_args
        self.assertIn("ticker", query_call[0][1])
        self.assertEqual(query_call[0][1]["ticker"], "SPY")
        self.assertEqual(query_call[0][1]["days"], 7)

    def test_query_history_failure_returns_empty(self):
        """Query failure should return empty list."""
        mock_client = MagicMock()
        mock_client.execute.side_effect = Exception("Query failed")
        store = GEXClickHouseStore(client=mock_client)
        rows = store.query_levels_history("SPY")
        self.assertEqual(rows, [])


# ── Integration: DatabentGammaCollector + ClickHouse ─────────────────────


class TestGammaCollectorClickHouseIntegration(unittest.TestCase):
    """Test that DatabentGammaCollector persists to ClickHouse."""

    def test_collector_calls_ch_store_on_collect(self):
        """collect_ticker should call ch_store.store_ticker_result."""
        from test_databento_gamma import TestDatabentGammaCollector

        # Reuse the mock client builder from existing tests
        helper = TestDatabentGammaCollector()
        mock_db_client = helper._make_mock_client()

        mock_ch_store = MagicMock()
        mock_ch_store.store_ticker_result.return_value = True

        from databento_gamma import DatabentGammaCollector

        collector = DatabentGammaCollector(
            client=mock_db_client, ch_store=mock_ch_store
        )
        result = collector.collect_ticker("SPY", spot_price=585.0)

        # ClickHouse store should have been called
        mock_ch_store.store_ticker_result.assert_called_once()
        ch_call_args = mock_ch_store.store_ticker_result.call_args
        self.assertEqual(ch_call_args[0][0], "SPY")  # ticker
        self.assertEqual(ch_call_args[0][2], 585.0)  # spot_price
        self.assertIsNotNone(ch_call_args[1].get("snapshot_ts") or ch_call_args[0][3])

    def test_collector_continues_on_ch_failure(self):
        """ClickHouse failure should not break the GEX pipeline."""
        from test_databento_gamma import TestDatabentGammaCollector

        helper = TestDatabentGammaCollector()
        mock_db_client = helper._make_mock_client()

        mock_ch_store = MagicMock()
        mock_ch_store.store_ticker_result.side_effect = Exception("CH down")

        from databento_gamma import DatabentGammaCollector

        collector = DatabentGammaCollector(
            client=mock_db_client, ch_store=mock_ch_store
        )
        result = collector.collect_ticker("SPY", spot_price=585.0)

        # Result should still be returned despite CH failure
        self.assertIn("gex", result)
        self.assertIn("levels", result)

    @patch("config.CH_GEX_ENABLED", False)
    @patch("config.CH_HOST", "")
    def test_collector_skips_ch_when_disabled(self):
        """CH persistence should be skipped when disabled."""
        from test_databento_gamma import TestDatabentGammaCollector

        helper = TestDatabentGammaCollector()
        mock_db_client = helper._make_mock_client()

        from databento_gamma import DatabentGammaCollector

        collector = DatabentGammaCollector(client=mock_db_client)
        result = collector.collect_ticker("SPY", spot_price=585.0)

        # Should still work, just no CH store
        self.assertIn("gex", result)
        self.assertIsNone(collector.ch_store)


if __name__ == "__main__":
    unittest.main()
