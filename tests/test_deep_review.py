# -*- coding: utf-8 -*-
"""深度复盘结构配置与历史链单测（mock 数据，不调外网）。"""

import unittest
from datetime import datetime

from src.services.review import (
    available_structures,
    build_history_context,
    load_structure,
    render_lifemap,
    resolve_structure_name,
)
from src.services.review.builder import DeepReviewBuilder


class _FakeFetcher:
    """最小假 fetcher：只提供 builder 需要的方法."""

    def __init__(self):
        self.calls = []

    def _fetch_raw_data(self, code, start, end, adjust="forward"):
        import pandas as pd

        self.calls.append((code, adjust))
        if code == "000001.SH":
            return pd.DataFrame(
                [
                    {"date_ms": 1791734400000, "close_price": 3797.24,
                     "open_price": 3806.98, "high_price": 3828.22,
                     "low_price": 3791.00, "volume": 1.0, "turnover": 714909910000.0},
                    {"date_ms": 1791820800000, "close_price": 3800.86,
                     "open_price": 3760.80, "high_price": 3822.11,
                     "low_price": 3741.53, "volume": 1.0, "turnover": 779302290000.0},
                ]
            )
        if code == "883900.TI":
            return pd.DataFrame(
                [
                    {"date_ms": 1791734400000, "close_price": 3721.22,
                     "open_price": 1.0, "high_price": 1.0,
                     "low_price": 1.0, "volume": 1.0, "turnover": 1.0},
                    {"date_ms": 1791820800000, "close_price": 3785.09,
                     "open_price": 1.0, "high_price": 1.0,
                     "low_price": 1.0, "volume": 1.0, "turnover": 1.0},
                ]
            )
        return pd.DataFrame(
            [
                {"date_ms": 0, "close_price": 10.0, "open_price": 10.0,
                 "high_price": 10.0, "low_price": 10.0, "volume": 1.0, "turnover": 1.0},
                {"date_ms": 1, "close_price": 11.0, "open_price": 11.0,
                 "high_price": 11.0, "low_price": 11.0, "volume": 1.0, "turnover": 1.0},
            ]
        )

    def get_market_stats(self):
        return {"up_count": 100, "down_count": 50, "flat_count": 10}

    def get_limit_up_pool(self, date=None, n=20, page_size=200):
        today_pool = [
            {"thscode": "605577.SH", "code": "605577", "name": "龙版传媒",
             "is_st": False, "price": 10.0, "change_pct": 10.0,
             "continue_boards": 3, "continue_text": "3连板",
             "limit_up_time": "09:30", "reason": "AI漫剧+测试",
             "seal_amount": 52000000.0, "max_seal_amount": 60000000.0},
            {"thscode": "600001.SH", "code": "600001", "name": "首板股",
             "is_st": False, "price": 10.0, "change_pct": 10.0,
             "continue_boards": 1, "continue_text": "首板",
             "limit_up_time": "10:30", "reason": "PCB+测试",
             "seal_amount": 388000000.0, "max_seal_amount": 400000000.0},
        ]
        if date == "20261012":
            return today_pool + [
                {"thscode": "600003.SH", "code": "600003", "name": "掉队股",
                 "is_st": False, "price": 9.0, "change_pct": 5.0,
                 "continue_boards": 2, "continue_text": "2连板",
                 "limit_up_time": "09:30", "reason": "测试",
                 "seal_amount": 1000000.0, "max_seal_amount": 2000000.0},
            ]
        return today_pool

    def get_limit_down_pool(self, date=None, n=200, page_size=200):
        return []

    def get_limit_break_pool(self, date=None, n=200, page_size=200):
        return [
            {"thscode": "600002.SH", "code": "600002", "name": "炸板股",
             "is_st": False, "price": 9.0, "change_pct": 5.0,
             "continue_boards": 0, "continue_text": "",
             "limit_up_time": "", "reason": "",
             "seal_amount": 0.0, "max_seal_amount": 0.0},
        ]

    def get_hot_stocks(self, n=10):
        return [{"rank": 1, "thscode": "600418.SH", "code": "600418",
                 "name": "江淮汽车", "heat": "123", "rank_change": 0,
                 "rank_trend": "flat", "trend_text": "持平", "source": "x"}]

    def get_auction_snapshot(self, thscodes, stage="final"):
        return {"605577.SH": 8500000000.0}


class _FakeAnalyzer:
    def __init__(self):
        self.prompts = []

    def generate_text(self, prompt, max_tokens=2048, temperature=0.7):
        self.prompts.append(prompt)
        return "### 三、当日主线判断\n- 测试主线：连板高度 3 板。"


def _structure():
    return {
        "name": "test_struct",
        "data_sources": {
            "index_codes": ["000001.SH"],
            "yesterday_limit_index": "883900.TI",
            "hot_n": 30,
            "first_board_top_n": 8,
            "reason_rank_top_n": 4,
            "reason_groups": [{"name": "PCB", "keywords": ["PCB"]}],
        },
        "conventions": {"flat_tolerance_pct": 0.2, "not_advice": "非投资建议。"},
        "sections": [
            {"key": "market_status", "title": "一、大盘状态", "mode": "data"},
            {"key": "limitup", "title": "二、涨停板分析", "mode": "data"},
            {"key": "hotlist", "title": "热度榜", "mode": "data"},
            {"key": "mainline", "title": "三、当日主线判断", "mode": "llm", "prompt": "写主线。"},
            {"key": "lifemap", "title": "八、本周生命图谱", "mode": "history"},
            {"key": "notes", "title": "数据说明", "mode": "data"},
        ],
        "llm": {"groups": [["mainline"]], "temperature": 0.3, "max_tokens": 100},
    }


class TestLoader(unittest.TestCase):
    def test_deep_structure_loads(self):
        self.assertIn("deep_limitup_cn", available_structures())
        structure = load_structure("deep_limitup_cn")
        self.assertIsNotNone(structure)
        keys = [s.get("key") for s in structure["sections"]]
        for expected in ("market_status", "limitup", "mainline", "lifemap", "notes"):
            self.assertIn(expected, keys)

    def test_legacy_and_unknown_return_none(self):
        self.assertIsNone(load_structure("legacy"))
        self.assertIsNone(load_structure("no_such_structure_xyz"))

    def test_resolve_defaults(self):
        self.assertEqual(resolve_structure_name(""), "deep_limitup_cn")
        self.assertEqual(resolve_structure_name("LEGACY"), "legacy")


class TestHistory(unittest.TestCase):
    def test_empty_history_is_fail_open(self):
        ctx = build_history_context("20261013", "20261012")
        # 本地无昨日文件时不断言内容，只断言不抛异常且结构完整
        self.assertIsInstance(ctx.prev_expectations, list)
        rendered = render_lifemap(ctx, "20261013")
        self.assertIn("42", rendered)

    def test_expectation_extraction(self):
        from src.services.review.history import _extract_expectations

        text = "### 六\n明日只核对这 5 条：\n- 上证收红\n- 龙版涨停\n\n### 七\n"
        self.assertEqual(_extract_expectations(text), ["上证收红", "龙版涨停"])


class TestBuilder(unittest.TestCase):
    def test_data_packet_math(self):
        builder = DeepReviewBuilder(fetcher=_FakeFetcher(), structure=_structure())
        packet = builder._collect_packet("20261013")
        self.assertEqual(packet.prev_date, "20261012")
        self.assertEqual(packet.prev_date, "20261012")
        self.assertEqual(packet.limit_up, 2)
        self.assertEqual(packet.limit_break, 1)
        self.assertAlmostEqual(packet.seal_rate, round(2 / 3 * 100, 2))
        self.assertEqual(packet.yesterday_limit_chg, "+1.72%")
        self.assertEqual(len(packet.ladder), 1)
        self.assertEqual(packet.ladder[0]["name"], "龙版传媒")
        self.assertEqual(packet.first_board_total, 1)
        self.assertEqual(len(packet.failed_ladder), 1)
        self.assertEqual(packet.failed_ladder[0]["name"], "掉队股")

    def test_preclose_guardrails_to_legacy(self):
        from src.services.review import DeepReviewUnavailable

        builder = DeepReviewBuilder(fetcher=_FakeFetcher(), structure=_structure())
        # 假数据最后一根是 10/13，用 10/14 复盘应拒绝（当日未收盘）
        with self.assertRaises(DeepReviewUnavailable):
            builder._collect_packet("20261014")

    def test_board_shape_classification(self):
        builder = DeepReviewBuilder(fetcher=_FakeFetcher(), structure=_structure())
        # FakeFetcher 个股 K 开=高=低=收→一字；000001.SH 上证 K→换手
        flat = builder._stock_day_shape("600001", "2026-10-12", "2026-10-13")
        self.assertEqual(flat["shape"], "一字板")

    def test_full_build_without_llm(self):
        builder = DeepReviewBuilder(fetcher=_FakeFetcher(), structure=_structure())
        report = builder.build("20261013")
        for title in ("一、大盘状态", "二、涨停板分析", "热度榜", "八、本周生命图谱", "数据说明"):
            self.assertIn(title, report)
        self.assertIn("66.67%", report)  # 封板率 2/(2+1)

    def test_llm_group_injection(self):
        analyzer = _FakeAnalyzer()
        builder = DeepReviewBuilder(
            fetcher=_FakeFetcher(), structure=_structure(), analyzer=analyzer
        )
        report = builder.build("20261013")
        self.assertIn("测试主线", report)
        self.assertTrue(any("数据包" in p for p in analyzer.prompts))


if __name__ == "__main__":
    unittest.main()
