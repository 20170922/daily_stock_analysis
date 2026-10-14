# -*- coding: utf-8 -*-
"""HithinkFetcher unit tests (mocked HTTP, no API key needed)."""

import unittest

import pandas as pd

from data_provider.base import DataFetchError
from data_provider.hithink_fetcher import HithinkFetcher, _is_index_thscode, to_thscode
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
        self.assertEqual(to_thscode("sh000001"), "000001.SH")
        self.assertEqual(to_thscode("sz399001"), "399001.SZ")
        self.assertEqual(to_thscode("000001.SZ"), "000001.SZ")
        self.assertEqual(to_thscode("sz300364"), "300364.SZ")
        self.assertEqual(to_thscode("430047"), "430047.BJ")
        self.assertEqual(to_thscode("920002.bj"), "920002.BJ")
        self.assertEqual(to_thscode("883900.TI"), "883900.TI")

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

    def test_realtime_quote_routes_index_to_index_endpoint(self):
        seen = []
        payload = {"code": 0, "message": "success", "data": {"item": [{
            "thscode": "000001.SH", "ticker": "000001",
            "volume": 100, "turnover": 200,
            "last_price": 3797.24, "price_change": -16.5,
            "price_change_ratio_pct": -0.434,
            "open_price": 3806.9, "high_price": 3828.2,
            "low_price": 3790.9, "prev_price": 3813.79,
        }]}}

        class _RouteSession(_FakeSession):
            def get(self, url, params=None, headers=None, timeout=None):
                seen.append(url)
                return _FakeResponse(payload)

        fetcher = HithinkFetcher(api_key="k", session_factory=lambda: _RouteSession([]))
        quote = fetcher.get_realtime_quote("sh000001")
        self.assertIn("/api/a-share-index/prices/snapshot", seen[0])
        self.assertEqual(quote.code, "000001")
        self.assertAlmostEqual(quote.price, 3797.24)

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

        class _PagedSession(_FakeSession):
            def get(self, url, params=None, headers=None, timeout=None):
                if "/prices/snapshot" in url:
                    offset = (params or {}).get("offset", 0)
                    return _FakeResponse(page2 if offset else page1)
                return super().get(url, params=params, headers=headers, timeout=timeout)

        def factory():
            session = _PagedSession([
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

    def test_session_is_reused_across_requests(self):
        created = []

        def factory():
            session = _FakeSession([
                ("snapshot", {"code": 0, "message": "ok", "data": {"item": []}}),
                ("hot-stock-list", {"code": 0, "message": "ok", "data": {"item": []}}),
            ])
            created.append(session)
            return session

        fetcher = HithinkFetcher(api_key="k", session_factory=factory)
        fetcher.get_main_indices()
        fetcher.get_hot_stocks()
        self.assertEqual(len(created), 1)

    def test_limit_up_pool_passes_date_ms(self):
        captured = {}

        class _CaptureSession(_FakeSession):
            def get(self, url, params=None, headers=None, timeout=None):
                captured.update(dict(params or {}))
                return _FakeResponse({"code": 0, "message": "ok", "data": {"item": []}})

        fetcher = HithinkFetcher(api_key="k", session_factory=lambda: _CaptureSession([]))
        fetcher.get_limit_up_pool(date="20261012", n=5)
        # 与线上日线最后一个交易日的 date_ms 口径一致（Asia/Shanghai 00:00）。
        self.assertEqual(captured.get("date_ms"), 1791734400000)
        with self.assertRaises(DataFetchError):
            fetcher.get_limit_up_pool(date="not-a-date")

    def test_index_daily_uses_index_endpoint(self):
        seen = []

        class _RouteSession(_FakeSession):
            def get(self, url, params=None, headers=None, timeout=None):
                seen.append((url, dict(params or {})))
                return _FakeResponse({"code": 0, "message": "ok", "data": {"item": []}})

        fetcher = HithinkFetcher(api_key="k", session_factory=lambda: _RouteSession([]))
        fetcher._fetch_raw_data("sh000001", "2026-10-08", "2026-10-12")
        self.assertIn("/api/a-share-index/prices/historical", seen[0][0])
        self.assertNotIn("adjust", seen[0][1])
        fetcher._fetch_raw_data("300364", "2026-10-08", "2026-10-12")
        self.assertIn("/api/a-share/prices/historical", seen[1][0])
        self.assertEqual(seen[1][1].get("adjust"), "forward")

    def test_is_index_thscode(self):
        self.assertTrue(_is_index_thscode("000001.SH"))
        self.assertTrue(_is_index_thscode("399006.SZ"))
        self.assertTrue(_is_index_thscode("886042.TI"))
        self.assertFalse(_is_index_thscode("600519.SH"))
        self.assertFalse(_is_index_thscode("000001.SZ"))

    def test_main_indices_amplitude(self):
        payload = {
            "code": 0,
            "message": "success",
            "data": {"item": [{
                "thscode": "000001.SH", "ticker": "000001",
                "volume": 100, "turnover": 200,
                "last_price": 100.0, "price_change": 1.0,
                "price_change_ratio_pct": 1.0,
                "open_price": 99.0, "high_price": 101.0,
                "low_price": 98.0, "prev_price": 99.0,
            }]},
        }
        fetcher = _fetcher([("index", payload)])
        result = fetcher.get_main_indices()
        self.assertAlmostEqual(result[0]["amplitude"], round(3 / 99 * 100, 2))

    def test_pool_pagination_merges_all_pages(self):
        def pool_payload(page, total, codes):
            return {
                "code": 0, "message": "ok",
                "data": {
                    "pagination": {"total": total, "pages": 2, "size": 2, "page": page},
                    "item": [
                        {"ticker": c, "thscode": f"{c}.SH", "name": f"股{c}",
                         "last_price": 10.0, "price_change_ratio_pct": 10.0,
                         "continue_day_cnt": 3, "continue_day_text": "3连板",
                         "limit_up_reason": "测试", "seal_money": 1.0,
                         "max_seal_money": 2.0}
                        for c in codes
                    ],
                },
            }

        seen_pages = []

        class _PagedSession(_FakeSession):
            def get(self, url, params=None, headers=None, timeout=None):
                page = (params or {}).get("page", 1)
                seen_pages.append(page)
                if page == 1:
                    return _FakeResponse(pool_payload(1, 3, ["600001", "600002"]))
                return _FakeResponse(pool_payload(2, 3, ["600003"]))

        fetcher = HithinkFetcher(api_key="k", session_factory=lambda: _PagedSession([]))
        result = fetcher.get_limit_up_pool(date="20261013", n=200, page_size=2)
        self.assertEqual([r["code"] for r in result], ["600001", "600002", "600003"])
        self.assertEqual(seen_pages, [1, 2])
        self.assertEqual(result[0]["continue_text"], "3连板")
        self.assertEqual(result[0]["thscode"], "600001.SH")

    def test_down_and_break_pools_normalize(self):
        payload = {
            "code": 0, "message": "ok",
            "data": {
                "pagination": {"total": 1, "pages": 1, "size": 200, "page": 1},
                "item": [{"ticker": "600004", "thscode": "600004.SH", "name": "跌股",
                          "last_price": 5.0, "price_change_ratio_pct": -10.0}],
            },
        }
        fetcher = _fetcher([("limit-down-pool", payload), ("limit-break-pool", payload)])
        down = fetcher.get_limit_down_pool(date="20261013")
        brk = fetcher.get_limit_break_pool(date="20261013")
        self.assertEqual(down[0]["code"], "600004")
        self.assertEqual(brk[0]["change_pct"], -10.0)

    def test_hot_stocks_full_fields_and_day_period(self):
        captured = {}
        payload = {
            "code": 0, "message": "ok",
            "data": {"item": [
                {"rank": 1, "thscode": "600418.SH", "ticker": "600418",
                 "name": "江淮汽车", "heat": "5714814",
                 "rank_change": 1, "rank_trend": "up"},
                {"rank": 2, "thscode": "600127.SH", "ticker": "600127",
                 "name": "金健米业", "heat": "4869802",
                 "rank_change": -1, "rank_trend": "down"},
            ]},
        }

        class _CaptureSession(_FakeSession):
            def get(self, url, params=None, headers=None, timeout=None):
                captured.update(dict(params or {}))
                return _FakeResponse(payload)

        fetcher = HithinkFetcher(api_key="k", session_factory=lambda: _CaptureSession([]))
        result = fetcher.get_hot_stocks(n=30)
        self.assertEqual(captured.get("period"), "day")
        self.assertEqual(result[0]["heat"], "5714814")
        self.assertEqual(result[0]["rank_change"], 1)
        self.assertEqual(result[0]["trend_text"], "上升")
        self.assertEqual(result[1]["trend_text"], "下降")

    def test_auction_snapshot_maps_caps(self):
        payload = {
            "code": 0, "message": "ok",
            "data": {"item": [
                {"thscode": "605577.SH", "float_market_cap": 8500000000},
                {"thscode": "600519.SH", "float_market_cap": 0},
            ]},
        }
        fetcher = _fetcher([("auction/snapshot", payload)])
        caps = fetcher.get_auction_snapshot(["605577.SH", "600519.SH"])
        self.assertEqual(caps, {"605577.SH": 8500000000})

    def test_fetch_raw_data_adjust_none(self):
        seen = []

        class _RouteSession(_FakeSession):
            def get(self, url, params=None, headers=None, timeout=None):
                seen.append((url, dict(params or {})))
                return _FakeResponse({"code": 0, "message": "ok", "data": {"item": []}})

        fetcher = HithinkFetcher(api_key="k", session_factory=lambda: _RouteSession([]))
        fetcher._fetch_raw_data("300364", "2026-10-12", "2026-10-13", adjust="none")
        self.assertEqual(seen[0][1].get("adjust"), "none")


if __name__ == "__main__":
    unittest.main()
