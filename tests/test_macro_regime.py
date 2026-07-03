"""SPEC_macro_regime §10 pytest 驗收條件（無需網路）。"""

import json

import numpy as np
import pandas as pd
import pytest

import macro_regime as mr


def _dates(n, start="2025-01-01"):
    return pd.bdate_range(start, periods=n)


def _series(vals, start="2025-01-01"):
    return pd.Series(vals, index=_dates(len(vals), start), dtype=float)


CFG = dict(mr.REGIME_CONFIG)  # confirm_days=3


# ---- 1. 遲滯：緩衝帶來回不反覆翻轉；連續 confirm_days 滿足進場條件才轉 BEAR ----
def test_hysteresis_no_flip_in_buffer():
    sr = _series([1.2, 0.9, 1.1, 0.9, 1.1, 0.9, 1.2, 1.1])
    br = _series([0.25] * 8)
    out = mr.run_regime_state_machine(sr, br)
    assert set(out["states"]) == {"NORMAL"}
    assert out["regime"] == "NORMAL"
    assert out["regime_changed_at"] is None

    # 連續 3 日滿足進場條件 → 第 3 日轉 BEAR
    sr2 = _series([1.2, 0.9, 0.9, 0.9, 1.1])
    br2 = _series([0.25] * 5)
    out2 = mr.run_regime_state_machine(sr2, br2)
    assert list(out2["states"]) == ["NORMAL", "NORMAL", "NORMAL", "BEAR", "BEAR"]
    # 之後回到緩衝帶 (1.1 < exit 1.3) → 維持 BEAR 不反覆翻轉
    assert out2["regime"] == "BEAR"
    assert out2["regime_changed_at"] == sr2.index[3]


# ---- 2. AND 條件：sector_rMOM = 0.8 但 Breadth = 0.50 → 維持 NORMAL ----
def test_and_condition():
    sr = _series([1.2] + [0.8] * 10)
    br = _series([0.50] * 11)
    out = mr.run_regime_state_machine(sr, br)
    assert out["regime"] == "NORMAL"
    assert set(out["states"]) == {"NORMAL"}


# ---- 3. confirm_days：滿足 2 日、第 3 日失效 → 不翻轉、計數歸零重計 ----
def test_confirm_days_reset():
    #        init  1    2    fail  1    2    (未達 3)
    sr = _series([1.2, 0.9, 0.9, 1.1, 0.9, 0.9])
    br = _series([0.25] * 6)
    out = mr.run_regime_state_machine(sr, br)
    assert out["regime"] == "NORMAL"
    # 歸零重計後需再連續 3 日才翻轉
    sr2 = _series([1.2, 0.9, 0.9, 1.1, 0.9, 0.9, 0.9])
    br2 = _series([0.25] * 7)
    out2 = mr.run_regime_state_machine(sr2, br2)
    assert out2["regime"] == "BEAR"
    assert out2["regime_changed_at"] == sr2.index[6]


# ---- 4. gating：BEAR 下買入 → FROZEN；賣出不被 regime 阻擋 ----
def test_gating_bear_freezes_buy_not_sell():
    # rMOM=1.8 且 Z=-2.3 → 個股層訊號 pullback (加碼候選)
    assert mr.gate_action("pullback", "BEAR") == "frozen"
    assert mr.gate_action("pullback", "NORMAL") == "pullback"
    # 動能轉弱 (減碼/退出) → regime 不阻擋、不改寫
    assert mr.gate_action("weak", "BEAR") == "weak"
    assert mr.gate_action("overheat", "BEAR") == "overheat"
    assert mr.gate_action("neutral", "BEAR") == "neutral"


# ---- 5. 優先序：narrative mode = exit 在 NORMAL 下技術條件全滿足仍無買入訊號 ----
def test_narrative_exit_priority():
    assert mr.gate_action("pullback", "NORMAL", narrative_mode="exit") not in ("pullback", "frozen")
    assert mr.gate_action("pullback", "BEAR", narrative_mode="exit") not in ("pullback", "frozen")
    # 非買入訊號不受 narrative 影響
    assert mr.gate_action("weak", "NORMAL", narrative_mode="exit") == "weak"


# ---- 6. 防前視：sector 迴歸係數 shift(1)，當日殘差不得使用含當日的係數 ----
def test_sector_regression_no_lookahead():
    dash = pytest.importorskip("us_stock_dark")
    rng = np.random.default_rng(7)
    n = 400
    idx = _dates(n)
    r_m = rng.normal(0, 0.01, n)
    spy = pd.Series(100 * np.exp(np.cumsum(r_m)), index=idx)
    soxx = pd.Series(50 * np.exp(np.cumsum(1.4 * r_m + rng.normal(0, 0.005, n))), index=idx)

    base = dash.compute_residual_momentum(soxx, spy, None)
    # 只竄改最後一日的 SOXX 價格 (當日報酬劇變)
    soxx2 = soxx.copy()
    soxx2.iloc[-1] *= 1.30
    mod = dash.compute_residual_momentum(soxx2, spy, None)
    # 當日係數只用 t-1 前資料估計 → 最後一日 β 必須完全不變
    assert mod["beta_mkt_series"].iloc[-1] == pytest.approx(base["beta_mkt_series"].iloc[-1])
    # 殘差本身要反映當日報酬變化 (證明不是整條凍結)
    assert not np.isclose(
        mod["cum_alpha"].iloc[-1] - mod["cum_alpha"].iloc[-2],
        base["cum_alpha"].iloc[-1] - base["cum_alpha"].iloc[-2])


# ---- 7. 邊界：checkpoints 檔缺失 → evidence_count = null；stale 6 日 → 不翻轉且 warning ----
def test_checkpoints_missing_and_stale_freeze(tmp_path):
    ck = mr.load_checkpoints(str(tmp_path / "missing.json"))
    assert ck["evidence_count"] is None
    assert ck["checkpoints"] == []

    # payload 組裝在檔案缺失下正常完成
    sr = _series([1.5] * 300)
    br = _series([0.6] * 300)
    payload = mr.build_macro_regime(sr, sr.cumsum() * 0, {"A": br},
                                    checkpoints_path=str(tmp_path / "missing.json"))
    assert payload is not None and payload["evidence_count"] is None

    # SOXX 連續 stale 6 日 (> 5)：條件即使滿足也不翻轉，且產生 warning
    vals = [1.2, 1.2] + [np.nan] * 6 + [1.2]
    sr2 = _series(vals)
    br2 = _series([0.25] * len(vals))
    # 讓 ffill 前值落在進場區，驗證 stale 日不得計入連續天數
    sr2.iloc[1] = 0.9
    out = mr.run_regime_state_machine(sr2, br2)
    assert out["regime"] == "NORMAL"
    assert any("stale" in w for w in out["warnings"])


def test_checkpoints_field_validation(tmp_path):
    p = tmp_path / "ck.json"
    p.write_text(json.dumps({
        "narrative": "n", "updated_at": "2026-07-03",
        "checkpoints": [
            {"id": "a", "desc": "ok", "triggered": True, "date": "2026-06-01"},
            {"id": "b", "desc": "bad-trig", "triggered": "yes", "date": None},
            {"id": "c", "desc": "bad-date", "triggered": False, "date": "06/01/2026"},
            {"id": "d", "desc": "ok2", "triggered": False, "date": None},
        ]}), encoding="utf-8")
    ck = mr.load_checkpoints(str(p))
    assert ck["evidence_count"] == 1
    assert [c["id"] for c in ck["checkpoints"]] == ["a", "d"]
    assert len(ck["warnings"]) == 2


# ---- 8. Breadth 分母：歷史不足的新股不入分母，與排除前一致 ----
def test_breadth_denominator_excludes_short_history():
    idx = _dates(300)
    full = pd.Series(1.5, index=idx)          # rMOM >= 1
    weak = pd.Series(0.2, index=idx)          # rMOM < 1
    newbie = pd.Series(np.nan, index=idx)     # 歷史 100 日 → rMOM 全 NaN (不足 252+21)
    b_with, valid, low = mr.compute_breadth_series(
        {"A": full, "B": weak, "C": newbie})
    b_without, _, _ = mr.compute_breadth_series({"A": full, "B": weak})
    pd.testing.assert_series_equal(b_with, b_without)
    assert float(b_with.iloc[-1]) == 0.5
    assert int(valid.iloc[-1]) == 2
    assert bool(low.iloc[-1])  # 有效 < 10 → low_sample


# ---- 加值驗證：low_sample 暫停翻轉、初始狀態、payload schema ----
def test_low_sample_pauses_flip():
    sr = _series([1.2] + [0.9] * 6)
    br = _series([0.25] * 7)
    ls = pd.Series([False, False, True, True, True, True, True], index=sr.index)
    out = mr.run_regime_state_machine(sr, br, low_sample=ls)
    assert out["regime"] == "NORMAL"  # 暫停翻轉，維持前狀態
    out2 = mr.run_regime_state_machine(sr, br)
    assert out2["regime"] == "BEAR"


def test_initial_state_by_first_day_condition():
    # 第一天即滿足進場條件 → 直接 BEAR；緩衝帶 → NORMAL
    out = mr.run_regime_state_machine(_series([0.5, 0.5]), _series([0.1, 0.1]))
    assert out["states"].iloc[0] == "BEAR"
    out2 = mr.run_regime_state_machine(_series([1.1, 1.1]), _series([0.1, 0.1]))
    assert out2["states"].iloc[0] == "NORMAL"


def test_payload_schema_and_json_writable(tmp_path):
    n = 60
    sr = _series([np.nan] * 5 + [1.4] * (n - 5))
    ca = _series(np.linspace(0, 0.1, n))
    rmom = {f"T{i}": _series([1.2 if i % 2 else 0.5] * n) for i in range(12)}
    payload = mr.build_macro_regime(sr, ca, rmom, checkpoints_path="macro_checkpoints.json")
    for key in ("as_of", "regime", "regime_changed_at", "sector_rmom", "sector_rmom_series",
                "sector_cum_alpha_series", "breadth", "breadth_series", "evidence_count",
                "checkpoints", "flags"):
        assert key in payload
    assert payload["regime"] == "NORMAL"
    assert payload["breadth"] == pytest.approx(0.5, abs=0.1)
    out = mr.write_macro_regime_json(payload, str(tmp_path / "d" / "macro_regime.json"))
    loaded = json.loads(open(out, encoding="utf-8").read())
    assert loaded["regime"] == "NORMAL"
    # sector 資料不足 → 模組輸出 None，JSON 占位
    assert mr.build_macro_regime(_series([np.nan] * 10), None, rmom) is None
