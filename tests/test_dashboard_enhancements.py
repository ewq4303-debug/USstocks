from datetime import datetime

import pandas as pd

import pytest

import us_stock_dark as dash


def test_compute_portfolio_risk_concentration_and_beta():
    ibkr = {
        "summary": {"net_liquidation": 1000, "total_cash_value": 100, "gross_position_value": 900},
        "positions": [
            {"symbol": "AAA", "market_value": 600},
            {"symbol": "BBB", "market_value": 300},
        ],
    }
    fund = {"AAA": {"sector": "Tech", "beta": 1.2}, "BBB": {"sector": "Health", "beta": 0.8}}
    risk = dash.compute_portfolio_risk(ibkr, fund)
    assert risk["position_ratio"] == 0.9
    assert risk["cash_ratio"] == 0.1
    assert risk["top3"] == pytest.approx(0.9)
    assert round(risk["weighted_beta"], 2) == 0.96
    assert risk["sectors"][0]["sector"] == "Tech"


def test_trade_review_section_contains_forward_returns():
    idx = pd.to_datetime(["2026-01-02", "2026-01-05", "2026-01-06", "2026-01-07", "2026-01-08", "2026-01-09"])
    stocks_data = {"AAA": {"close_full": pd.Series([10, 11, 12, 13, 14, 15], index=idx)}}
    ibkr = {"trades": [{"symbol": "AAA", "side": "BUY", "size": 1, "price": 10, "trade_time": "2026-01-02T16:00:00Z"}]}
    html = dash.generate_trade_review_section(ibkr, stocks_data)
    assert "交易檢討" in html
    assert "AAA" in html
    assert "+1D" in html


def test_freshness_bar_without_status_file_is_resilient(tmp_path, monkeypatch):
    monkeypatch.setattr(dash, "BUILD_STATUS_FILE", str(tmp_path / "missing.json"))
    monkeypatch.setattr(dash, "NAV_HISTORY_FILE", str(tmp_path / "missing_nav.json"))
    html = dash.generate_freshness_bar({}, None, dash.load_build_status())
    assert "Data Freshness" in html
    assert "NAV" in html


def test_rating_history_annotations_detect_upgrade_and_new(tmp_path, monkeypatch):
    monkeypatch.setattr(dash, "RATING_HISTORY_FILE", str(tmp_path / "rating_history.json"))
    history = {
        "series": [{
            "date": "2026-01-02",
            "ratings": {"AAA": {"rating": "中性", "rating_key": "n", "total": 8}},
        }]
    }
    stocks = {
        "AAA": {"rating": {"rating": "買進", "rating_key": "b", "total": 11}},
        "BBB": {"rating": {"rating": "中性", "rating_key": "n", "total": 7}},
    }
    dash.annotate_rating_trends(stocks, history)
    assert stocks["AAA"]["rating_trend"]["direction"] == "upgrade"
    assert stocks["AAA"]["rating_trend"]["prev_rating"] == "中性"
    assert stocks["BBB"]["rating_trend"]["direction"] == "new"


def test_save_rating_history_replaces_same_day(tmp_path, monkeypatch):
    out = tmp_path / "rating_history.json"
    monkeypatch.setattr(dash, "RATING_HISTORY_FILE", str(out))
    stocks = {"AAA": {"rating": {"rating": "買進", "rating_key": "b", "total": 11, "tech": 6, "chip": 5}}}
    first = dash.save_rating_history(stocks, str(out))
    second = dash.save_rating_history(stocks, str(out))
    assert len(first["series"]) == 1
    assert len(second["series"]) == 1
    assert second["series"][0]["ratings"]["AAA"]["rating"] == "買進"
