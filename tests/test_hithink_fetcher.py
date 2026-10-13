# -*- coding: utf-8 -*-
"""HithinkFetcher unit tests (mocked HTTP, no API key needed)."""

import unittest

import pandas as pd

from data_provider.base import DataFetchError
from data_provider.hithink_fetcher import HithinkFetcher, to_thscode
from data_provider.realtime_types import RealtimeSource


class _FakeResponse:
    def __init__(self, payload, status_code=200):
        self._payload = payload
        self.status_code = status_code

    def raise_for_status(self):
        if self.status_code >= 400:
            raise RuntimeError(f"http {self.status_code}")

    def json(self):
        return self._payload


class _FakeSession:
    def __init__(self, routes):
        # routes: list of (path_substring, payload)
        self.routes = routes
        self.calls = []

    def get(self, url, params=None, headers=None, timeout=None):
        self.calls.append((url, dict(params or {}), dict(headers or {})))
        for needle, payload in self.routes:
            if needle in url:
                if isinstance(payload, Exception):
                    raise payload
                return _FakeResponse(payload)
        return _FakeResponse({"code": 404, "message": "no route", "data": None})


def _fetcher(routes):
    return HithinkFetcher(
        api_key="test-key",
        session_factory=lambda: _FakeSession(routes),
    )


class TestToThscode(unittest.TestCase):
    def test_plain_and_prefixed(self):
        self.assertEqual(to_thscode("600519"), "600519.SH")
        self.assertEqual(to_thscode("sh600519"), "600519.SH")
        self.assertEqual(to_thscode("000001.SZ"), "000001.SZ")
        self.assertEqual(to_thscode("sz300364"), "300364.SZ")
        self.assertEqual(to_thscode("430047"), "430047.BJ")
        self.assertEqual(to_thscode("920002.bj"), "920002.BJ")

    def test_invalid(self):
        with self.assertRaises(DataFetchError):
            to_thscode("hk00700")
        with self.assertRaises(DataFetchError):
            HithinkFetcher(api_key="  ")


class TestHithinkFetcher(unittest.TestCase):
    def test_auth_error_hints_key(self):
        fetcher = _fetcher([("snapshot", {"code": 2003, "message": "Missing X-api-key", "data": None})])
        with self.assertRaises(DataFetchError) as ctx:
            fetcher.get_main_indices()
        self.assertIn("HITHINK_FINANCE_API_KEY", str(ctx.exception))

    def test_main_indices_mapping(self):
        payload = {
            "code": 0,
            "message": "success",
            "data": {
                "item": [
                    {
                        "thscode": "000001.SH",
                        "ticker": "000001",
                        "volume": 100,
                        "turnover": 200,
                        "last_price": 3797.24,
                        "price_change": -16.5,
                        "price_change_ratio_pct": -0.434,
                        "open_price": 3806.9,
                        "high_price": 3828.2,
                        "low_price": 3790.9,
                        "prev_price": 3813.79,
                    }
                ]
            },
        }
        fetcher = _fetcher([("index", payload)])
        result = fetcher.get_main_indices()
        self.assertEqual(len(result), 1)
        self.assertEqual(result[0]["code"], "sh000001")
        self.assertEqual(result[0]["name"], "上证指数")
        self.assertAlmostEqual(result[0]["current"], 3797.24)
        self.assertAlmostEqual(result[0]["change_pct"], -0.434)

    def test_realtime_quote_mapping(self):
        payload = {
            "code": 0,
            "message": "success",
            "data": {
                "item": [
                    {
                        "thscode": "300364.SZ",
                        "ticker": "300364",
                        "volume": 1756059,
                        "turnover": 446000000.0,
                        "last_price": 25.41,
                        "price_change": 0.94,
                        "price_change_ratio_pct": 3.84,
                        "open_price": 24.91,
                        "high_price": 26.66,
                        "low_price": 24.24,
                        "prev_price": 24.47,
                    }
                ]
            },
        }
        fetcher = _fetcher([("snapshot", payload)])
        quote = fetcher.get_realtime_quote("sz300364")
        self.assertEqual(quote.code, "300364")
        self.assertEqual(quote.source, RealtimeSource.HITHINK)
        self.assertAlmostEqual(quote.price, 25.41)
        self.assertAlmostEqual(quote.change_pct, 3.84)

    def test_normalize_daily_computes_pct_chg(self):
        fetcher = _fetcher([])
        df = pd.DataFrame(
            [
                {"date_ms": 1788883200000, "open_price": 24.0, "high_price": 25.0,
                 "low_price": 23.9, "close_price": 24.47, "volume": 800000, "turnover": 1.9e8},
                {"date_ms": 1788969600000, "open_price": 24.91, "high_price": 26.66,
                 "low_price": 24.24, "close_price": 25.41, "volume": 1756059, "turnover": 4.46e8},
            ]
        )
        normalized = fetcher._normalize_data(df, "300364")
        self.assertEqual(list(normalized.columns),
                         ["date", "open", "high", "low", "close", "volume", "amount", "pct_chg"])
        self.assertAlmostEqual(normalized.iloc[1]["pct_chg"], round((25.41 - 24.47) / 24.47 * 100, 2))

    def test_limit_up_pool_and_hot_stocks(self):
        pool = {"code": 0, "message": "ok", "data": {"item": [{
            "thscode": "600519.SH", "ticker": "600519", "name": "贵州茅台",
            "last_price": 1288.0, "price_change_ratio_pct": 1.979,
            "limit_up_time": "09:34", "limit_up_reason": "测试",
            "continue_day_cnt": 2, "seal_money": 123.0}]}}
        hot = {"code": 0, "message": "ok", "data": {"item": [{
            "thscode": "603822.SH", "ticker": "603822", "name": "测试股", "rank": 1}]}}
        fetcher = _fetcher([("limit-up-pool", pool), ("hot-stock-list", hot)])
        pool_result = fetcher.get_limit_up_pool(n=20)
        self.assertEqual(pool_result[0]["code"], "600519")
        self.assertEqual(pool_result[0]["continue_boards"], 2)
        hot_result = fetcher.get_hot_stocks(n=10)
        self.assertEqual(hot_result[0]["name"], "测试股")

    def test_market_stats_paginates_and_counts_pools(self):
        page1 = {"code": 0, "message": "ok", "data": {
            "total": 3,
            "item": [
                {"price_change_ratio_pct": 1.0, "turnover": 1e8},
                {"price_change_ratio_pct": -2.0, "turnover": 2e8},
            ]}}
        page2 = {"code": 0, "message": "ok", "data": {
            "total": 3,
            "item": [{"price_change_ratio_pct": 0.0, "turnover": 3e8}]}}
        pool = {"code": 0, "message": "ok",
                "data": {"pagination": {"total": 47}, "item": []}}
        sessions = []

        def factory():
            session = _FakeSession([
                ("/prices/snapshot", page1 if len(sessions) == 0 else page2),
                ("limit-up-pool", pool),
                ("limit-down-pool", pool),
            ])
            sessions.append(session)
            return session

        fetcher = HithinkFetcher(api_key="k", snapshot_page_size=2, session_factory=factory)
        stats = fetcher.get_market_stats()
        self.assertEqual(stats["up_count"], 1)
        self.assertEqual(stats["down_count"], 1)
        self.assertEqual(stats["flat_count"], 1)
        self.assertEqual(stats["limit_up_count"], 47)
        self.assertAlmostEqual(stats["total_amount"], 6.0)


if __name__ == "__main__":
    unittest.main()
