"""SPEC_macro_regime_v2 §8 pytest 驗收條件（無需網路）+ 沿用仍有效的 v1 測試。"""

import json

import numpy as np
import pandas as pd
import pytest

import macro_regime as mr

WARMUP = mr.REGIME_CONFIG_V2["warmup_days"]  # 60


def _dates(n, start="2025-01-01"):
    return pd.bdate_range(start, periods=n)


def _series(vals, start="2025-01-01"):
    return pd.Series(vals, index=_dates(len(vals), start), dtype=float)


def _rmom_universe(n_hi, n_lo, days=100):
    """構造 rmom dict：n_hi 檔恆 1.5 (≥1)、n_lo 檔恆 0.5。"""
    out = {}
    for i in range(n_hi):
        out[f"H{i}"] = _series([1.5] * days)
    for i in range(n_lo):
        out[f"L{i}"] = _series([0.5] * days)
    return out


# ---- 1. WARMUP：前 60 交易日 == WARMUP 且無 FROZEN；第 61 日以當日值判定初始狀態 ----
def test_warmup_period_and_initialization():
    sr = _series([2.0] * 100)
    out = mr.run_regime_state_machine(sr)
    assert set(out["states"].iloc[:WARMUP]) == {"WARMUP"}
    assert out["states"].iloc[WARMUP] == "NORMAL"  # 第 61 日：2.0 ≥ 1.0 → NORMAL
    # 第 61 日 < 1.0 → 直接 BEAR
    sr2 = _series([0.5] * 100)
    out2 = mr.run_regime_state_machine(sr2)
    assert set(out2["states"].iloc[:WARMUP]) == {"WARMUP"}
    assert out2["states"].iloc[WARMUP] == "BEAR"
    # WARMUP 期間不做 gating（BUY 正常顯示，無任何 FROZEN 改寫）
    assert mr.gate_action("pullback", "WARMUP") == "pullback"


# ---- 2. Breadth 退出狀態機（關鍵回歸測試）：sector 恆 2.0、Breadth 恆 0.05 → NORMAL ----
def test_breadth_out_of_state_machine():
    days = 100
    sr = _series([2.0] * days)
    rmom_own = _rmom_universe(1, 19, days)  # Breadth_own 恆 0.05
    payload = mr.build_macro_regime(sr, None, rmom_own)
    assert payload["breadth_own"] == pytest.approx(0.05)
    assert payload["regime"] == "NORMAL"  # v1 (AND Breadth<0.30) 下會是 BEAR


# ---- 3. 遲滯：緩衝帶 (1.0–1.3) 內不翻轉；連續 confirm_days 才轉 BEAR ----
def test_hysteresis_buffer():
    sr = _series([2.0] * WARMUP + [1.2, 0.9, 1.1, 0.9, 1.1, 0.9, 1.2])
    out = mr.run_regime_state_machine(sr)
    assert set(out["states"].iloc[WARMUP:]) == {"NORMAL"}
    assert out["regime_changed_at"] is None

    sr2 = _series([2.0] * WARMUP + [1.2, 0.9, 0.9, 0.9, 1.1])
    out2 = mr.run_regime_state_machine(sr2)
    assert list(out2["states"].iloc[WARMUP:]) == ["NORMAL", "NORMAL", "NORMAL", "BEAR", "BEAR"]
    assert out2["regime"] == "BEAR"  # 回到緩衝帶 1.1 < 1.3 → 維持 BEAR
    # confirm 中斷 → 歸零重計
    sr3 = _series([2.0] * WARMUP + [1.2, 0.9, 0.9, 1.1, 0.9, 0.9])
    assert mr.run_regime_state_machine(sr3)["regime"] == "NORMAL"


# ---- 4. 塌陷警報：AND 範圍 + 冷卻 ----
def test_breadth_crash_and_scope_and_cooldown():
    # own 20 日差分 −0.18、ref −0.08 → 不觸發
    own = _series([0.50] * 20 + [0.32] * 20)
    ref = _series([0.50] * 20 + [0.42] * 20)
    assert mr.detect_breadth_crash(own, ref) == []

    # 兩者皆 ≤ −0.15 → 觸發；其後 10 個交易日內再次滿足 → 不重複發報
    own2 = _series([0.50] * 20 + [0.32] * 8 + [0.50] * 12)
    ref2 = _series([0.50] * 20 + [0.30] * 8 + [0.50] * 12)
    alerts = mr.detect_breadth_crash(own2, ref2)
    assert len(alerts) == 1  # 條件在 20~27 日連續滿足 8 日，僅首發
    a = alerts[0]
    assert a["type"] == "BREADTH_CRASH"
    assert a["date"] == own2.index[20].strftime("%Y-%m-%d")
    assert a["own_diff"] == pytest.approx(-0.18)
    assert a["ref_diff"] == pytest.approx(-0.20)

    # ref 缺席 (scope=both) → 整體輸出 None
    assert mr.detect_breadth_crash(own2, None) is None


# ---- 5. Divergence：own 0.40 − ref 0.30 = +0.10（4 位精度） ----
def test_divergence_precision():
    days = 100
    sr = _series([2.0] * days)
    rmom_own = _rmom_universe(2, 3, days)    # 0.4000
    rmom_ref = _rmom_universe(3, 7, days)    # 0.3000
    payload = mr.build_macro_regime(sr, None, rmom_own, rmom_ref, ref_name="REF")
    assert payload["breadth_own"] == pytest.approx(0.40)
    assert payload["breadth_ref"] == pytest.approx(0.30)
    assert payload["divergence"] == pytest.approx(0.10, abs=1e-9)


# ---- 6. 共用快取：重疊 ticker 的殘差迴歸僅執行一次 ----
def test_ref_shared_cache_call_count(monkeypatch):
    dash = pytest.importorskip("us_stock_dark")
    idx = _dates(50)
    spy = pd.Series(np.linspace(400, 420, 50), index=idx)
    calls = []
    real = dash.compute_residual_momentum

    def counting(close, mkt, sec=None):
        calls.append(1)
        return real(close, mkt, sec)

    monkeypatch.setattr(dash, "compute_residual_momentum", counting)
    nvda_cached = {"rmom_series": _series([1.5] * 50)}
    stocks_data = {"NVDA": {"resid": nvda_cached}}
    fetched = []

    def fetch_stub(tk):
        fetched.append(tk)
        return pd.Series(np.linspace(100, 130, 50), index=idx)

    out = dash.compute_ref_rmom(["NVDA", "TXN"], stocks_data, spy, None, fetch_close=fetch_stub)
    assert len(calls) == 1          # 只有 TXN 跑迴歸，NVDA 共用快取
    assert fetched == ["TXN"]       # NVDA 不重抓
    assert out["NVDA"] is nvda_cached["rmom_series"]
    assert "TXN" in out


# ---- 7. ref_universe 降級：缺檔 → 對照層 null，主流程正常 ----
def test_ref_universe_degradation(tmp_path):
    assert mr.load_ref_universe(str(tmp_path / "missing.json")) is None
    p = tmp_path / "few.json"
    p.write_text(json.dumps({"name": "X", "tickers": ["A", "B", "C"]}), encoding="utf-8")
    assert mr.load_ref_universe(str(p)) is None  # < 15 檔 → null

    days = 100
    payload = mr.build_macro_regime(_series([2.0] * days), None,
                                    _rmom_universe(5, 5, days), rmom_ref_by_ticker=None)
    assert payload["regime"] == "NORMAL"           # 主流程正常完成
    assert payload["breadth_ref"] is None
    assert payload["breadth_ref_series"] is None
    assert payload["divergence"] is None
    assert payload["alerts"] is None
    assert payload["flags"]["ref_universe_ok"] is False


def test_ref_universe_load_ok(tmp_path):
    p = tmp_path / "ref.json"
    tickers = [f"T{i}" for i in range(20)] + ["T0"]  # 含重複
    p.write_text(json.dumps({"name": "SOXX_TOP30", "as_of": "2026-07-03",
                             "tickers": tickers}), encoding="utf-8")
    ref = mr.load_ref_universe(str(p))
    assert ref["name"] == "SOXX_TOP30"
    assert len(ref["tickers"]) == 20  # 去重


# ---- 8. 跨板塊獨立性：XLF stale 不影響 SOXX regime；Systemic 分母排除 stale 板塊 ----
def test_cross_sector_independence():
    days = 100
    sr = _series([2.0] * days)
    cross = [
        {"ticker": "SOXX", "label": "半導體", "rmom_series": sr},
        {"ticker": "XLF", "label": "金融", "rmom_series": _series([np.nan] * days)},  # 全 stale
        {"ticker": "XLI", "label": "工業", "rmom_series": _series([-0.5] * days)},
    ]
    payload = mr.build_macro_regime(sr, None, _rmom_universe(5, 5, days),
                                    cross_sector=cross)
    assert payload["regime"] == "NORMAL"  # XLF stale 不影響 SOXX 判定
    by_tk = {c["ticker"]: c for c in payload["cross_sector"]}
    assert by_tk["XLF"]["rmom"] is None
    # Systemic = count(<0)/count(非 stale) = 1/2
    assert payload["systemic"] == pytest.approx(0.5)
    assert mr.compute_systemic({"SOXX": 2.0, "XLF": float("nan"), "XLI": -0.5}) == pytest.approx(0.5)
    assert mr.compute_systemic({"A": None}) is None


# ---- 9. Schema v2：schema_version == 2，v1 欄位名（舊 breadth）不再出現 ----
def test_schema_v2_no_v1_fields(tmp_path):
    days = 100
    payload = mr.build_macro_regime(_series([2.0] * days), _series(np.linspace(0, .1, days)),
                                    _rmom_universe(6, 6, days), _rmom_universe(8, 8, days),
                                    cross_sector=[{"ticker": "SOXX", "label": "半導體",
                                                   "rmom_series": _series([2.0] * days)}],
                                    ref_name="SOXX_TOP30")
    assert payload["schema_version"] == 2
    assert "breadth" not in payload           # 舊欄位名不再出現
    assert "breadth_series" not in payload
    assert "low_sample" not in payload["flags"]
    assert set(payload["flags"]) == {"stale", "ref_universe_ok", "warmup"}
    for key in ("breadth_own", "breadth_ref", "divergence", "breadth_own_series",
                "breadth_ref_series", "breadth_pctile_bands", "alerts",
                "cross_sector", "systemic", "evidence_count", "checkpoints"):
        assert key in payload
    out = mr.write_macro_regime_json(payload, str(tmp_path / "macro_regime.json"))
    assert json.loads(open(out, encoding="utf-8").read())["schema_version"] == 2


# ===== 沿用 v1 仍有效的測試 =====

def test_gating_bear_freezes_buy_not_sell():
    assert mr.gate_action("pullback", "BEAR") == "frozen"
    assert mr.gate_action("pullback", "NORMAL") == "pullback"
    assert mr.gate_action("weak", "BEAR") == "weak"       # 賣出永不被 regime 阻擋
    assert mr.gate_action("overheat", "BEAR") == "overheat"
    assert mr.gate_action("neutral", "BEAR") == "neutral"


def test_narrative_exit_priority():
    assert mr.gate_action("pullback", "NORMAL", narrative_mode="exit") not in ("pullback", "frozen")
    assert mr.gate_action("pullback", "BEAR", narrative_mode="exit") not in ("pullback", "frozen")
    assert mr.gate_action("weak", "NORMAL", narrative_mode="exit") == "weak"


def test_sector_regression_no_lookahead():
    dash = pytest.importorskip("us_stock_dark")
    rng = np.random.default_rng(7)
    n = 400
    idx = _dates(n)
    r_m = rng.normal(0, 0.01, n)
    spy = pd.Series(100 * np.exp(np.cumsum(r_m)), index=idx)
    soxx = pd.Series(50 * np.exp(np.cumsum(1.4 * r_m + rng.normal(0, 0.005, n))), index=idx)
    base = dash.compute_residual_momentum(soxx, spy, None)
    soxx2 = soxx.copy()
    soxx2.iloc[-1] *= 1.30
    mod = dash.compute_residual_momentum(soxx2, spy, None)
    assert mod["beta_mkt_series"].iloc[-1] == pytest.approx(base["beta_mkt_series"].iloc[-1])
    assert not np.isclose(
        mod["cum_alpha"].iloc[-1] - mod["cum_alpha"].iloc[-2],
        base["cum_alpha"].iloc[-1] - base["cum_alpha"].iloc[-2])


def test_stale_freeze_and_warning():
    # 61 日有效 (過 warmup、init NORMAL) + 6 日 stale (>5 → warning) + 進場區 1 日 → 不翻轉
    sr = _series([2.0] * (WARMUP + 1) + [np.nan] * 6 + [0.9])
    out = mr.run_regime_state_machine(sr)
    assert out["regime"] == "NORMAL"
    assert any("stale" in w for w in out["warnings"])
    # warmup 前導 NaN 不算 stale、不觸發 warning
    sr2 = _series([np.nan] * 30 + [2.0] * (WARMUP + 5))
    out2 = mr.run_regime_state_machine(sr2)
    assert out2["warnings"] == []


def test_checkpoints_missing_and_validation(tmp_path):
    ck = mr.load_checkpoints(str(tmp_path / "missing.json"))
    assert ck["evidence_count"] is None and ck["checkpoints"] == []
    p = tmp_path / "ck.json"
    p.write_text(json.dumps({
        "narrative": "n", "updated_at": "2026-07-03",
        "checkpoints": [
            {"id": "a", "desc": "ok", "triggered": True, "date": "2026-06-01"},
            {"id": "b", "desc": "bad-trig", "triggered": "yes", "date": None},
            {"id": "c", "desc": "bad-date", "triggered": False, "date": "06/01/2026"},
            {"id": "d", "desc": "ok2", "triggered": False, "date": None},
        ]}), encoding="utf-8")
    ck2 = mr.load_checkpoints(str(p))
    assert ck2["evidence_count"] == 1
    assert [c["id"] for c in ck2["checkpoints"]] == ["a", "d"]
    assert len(ck2["warnings"]) == 2


def test_breadth_denominator_excludes_short_history():
    idx = _dates(300)
    full = pd.Series(1.5, index=idx)
    weak = pd.Series(0.2, index=idx)
    newbie = pd.Series(np.nan, index=idx)  # 歷史不足 → rMOM 全 NaN
    b_with, valid = mr.compute_breadth_series({"A": full, "B": weak, "C": newbie})
    b_without, _ = mr.compute_breadth_series({"A": full, "B": weak})
    pd.testing.assert_series_equal(b_with, b_without)
    assert float(b_with.iloc[-1]) == 0.5
    assert int(valid.iloc[-1]) == 2


def test_repo_ref_universe_and_checkpoints_files():
    ref = mr.load_ref_universe("ref_universe.json")
    assert ref is not None and len(ref["tickers"]) >= 15
    ck = mr.load_checkpoints("macro_checkpoints.json")
    assert ck["evidence_count"] == 0 and len(ck["checkpoints"]) == 4
