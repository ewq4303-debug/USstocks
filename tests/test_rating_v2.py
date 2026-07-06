"""SPEC_rating_v2 §9 Phase 2 pytest 驗收（無需網路）。

覆蓋：每項 pos/neg/na 三態、PO2 死區邊界、bucket clamp、
缺值重正規化、MIN_AVAILABLE 擋下映射、五級映射邊界。
"""

import math

import pytest

import us_stock_dark as us


# ---------- helpers ----------

def mk_data(close=100.0, prev_close=99.0, volume=2000, vol_ma20=1000.0,
            ma60=90.0, ma200=80.0, hi252=110.0, lo252=60.0,
            rsi=55.0, macd_hist=0.5):
    """全利多預設：TR1+2 TR2+2 TR3 pos=(100-60)/50=0.8→0、MO1+2 MO2+2 MO3+1。"""
    return {
        "latest": {"close": close, "volume": volume, "prev_close": prev_close},
        "prev": {"close": prev_close},
        "indicators": {
            "ma60": ma60, "ma200": ma200, "high_252": hi252, "low_252": lo252,
            "vol_ma20": vol_ma20, "rsi_raw": rsi, "macd_hist_raw": macd_hist,
        },
    }


FUND_BULL = {"inst_pct": 0.7, "shares_short": 80, "shares_short_prior": 100,
             "target_price": 110.0}
OPT_BULL = {"pcr_oi": 0.6}


def item(r, iid):
    return next(d for d in r["details"] if d["id"] == iid)


def rate(data=None, fund=None, opt=None, tp30=None, config=None):
    return us.calculate_rating_v2(data or mk_data(), fund or {}, opt,
                                  target_price_30d_ago=tp30, config=config)


# ---------- 1. 全 N/A → 資料不足 na ----------

def test_all_na_gives_na_rating():
    data = {"latest": {"close": None, "volume": None, "prev_close": None},
            "prev": {"close": None}, "indicators": {}}
    r = rate(data)
    assert r["rating_key"] == "na"
    assert r["score_norm"] is None
    assert r["available_max"] == 0
    assert r["completeness"] == "0/10"
    assert all(d["status"] == "na" for d in r["details"])


# ---------- 2. RSI 缺值不得得分（廢除預設 50） ----------

def test_rsi_missing_is_na_not_scored():
    data = mk_data(rsi=None)
    r = rate(data, FUND_BULL, OPT_BULL, tp30=100.0)
    d = item(r, "MO2")
    assert d["points"] is None and d["status"] == "na"
    # 分母同步縮小：MOMENTUM available = min(5, 2+1) = 3
    assert r["buckets"]["MOMENTUM"]["score"] == 3.0  # MO1+2, MO3+1
    assert r["available_max"] == 17.0  # 19 − 2


def test_rsi_bands():
    for rsi, exp in [(55, 2.0), (42, 1.0), (74, 1.0), (35, -1.0),
                     (25, -2.0), (85, -2.0), (79, 0.0)]:
        r = rate(mk_data(rsi=rsi))
        assert item(r, "MO2")["points"] == exp, f"rsi={rsi}"


# ---------- 3. 對稱性：空方訊號要能扣分 ----------

def test_symmetric_bearish_scores_negative():
    data = mk_data(close=62.0, prev_close=63.0, ma60=70.0, ma200=80.0,
                   rsi=25.0, macd_hist=-0.5, volume=2000, vol_ma20=1000.0,
                   hi252=110.0, lo252=60.0)  # pos=(62-60)/50=0.04 → −2
    fund = {"inst_pct": 0.1, "shares_short": 130, "shares_short_prior": 100,
            "target_price": 90.0}
    r = rate(data, fund, {"pcr_oi": 1.5}, tp30=100.0)
    assert r["buckets"]["TREND"]["score"] == -6.0
    assert r["buckets"]["MOMENTUM"]["score"] == -5.0  # −2−2−1
    assert r["buckets"]["POSITIONING"]["score"] == -3.0  # −1−2
    assert r["buckets"]["SENTIMENT"]["score"] == -2.0
    assert r["buckets"]["VALUATION"]["score"] == -2.0
    assert r["earned"] == -18.0
    assert r["score_norm"] == pytest.approx(-18.0 / 19.0 * 20.0, abs=0.01)
    assert r["rating_key"] == "ss"


def test_full_bullish_maps_sb():
    data = mk_data(close=109.0, prev_close=100.0)  # pos=(109-60)/50=0.98 → TR3 +2
    r = rate(data, FUND_BULL, OPT_BULL, tp30=100.0)
    assert r["earned"] == 19.0 and r["available_max"] == 19.0
    assert r["score_norm"] == 20.0
    assert r["rating_key"] == "sb"
    assert r["completeness"] == "10/10"


# ---------- 4. MO3 量能方向（修正 v1 量增一律加分） ----------

def test_mo3_volume_direction():
    up = rate(mk_data(close=101, prev_close=100))
    dn = rate(mk_data(close=99, prev_close=100))
    shrink = rate(mk_data(volume=500, vol_ma20=1000))
    no_prev = rate({"latest": {"close": 100, "volume": 2000, "prev_close": None},
                    "prev": {"close": None},
                    "indicators": mk_data()["indicators"]})
    assert item(up, "MO3")["points"] == 1.0
    assert item(dn, "MO3")["points"] == -1.0
    assert item(shrink, "MO3")["points"] == 0.0
    assert item(no_prev, "MO3")["status"] == "na"  # 量增但方向無法判定


# ---------- 5. PO2 死區邊界 ----------

def test_po2_deadzone_boundaries():
    def po2(ss, ssp=100):
        return item(rate(fund={"shares_short": ss, "shares_short_prior": ssp}), "PO2")
    assert po2(89.99)["points"] == 2.0   # < 0.9×
    assert po2(90.0)["points"] == 0.0    # 邊界落死區
    assert po2(115.0)["points"] == 0.0   # 邊界落死區
    assert po2(115.01)["points"] == -2.0  # > 1.15×
    assert po2(100, ssp=0)["status"] == "na"
    assert po2(None)["status"] == "na"


# ---------- 6. SE1：廢除 pcr 預設值 ----------

def test_se1_pcr_states():
    assert item(rate(opt=None), "SE1")["status"] == "na"
    assert item(rate(opt={"pcr_oi": 0}), "SE1")["status"] == "na"  # 除零守門值
    assert item(rate(opt={"pcr_oi": 0.65}), "SE1")["points"] == 2.0
    assert item(rate(opt={"pcr_oi": 0.85}), "SE1")["points"] == 1.0
    assert item(rate(opt={"pcr_oi": 1.2}), "SE1")["points"] == 0.0
    assert item(rate(opt={"pcr_oi": 1.5}), "SE1")["points"] == -2.0


# ---------- 7. VA1 目標價修訂方向 ----------

def test_va1_target_revision():
    f = {"target_price": 103.0}
    assert item(rate(fund=f, tp30=100.0), "VA1")["points"] == 2.0   # +3%
    assert item(rate(fund={"target_price": 96.0}, tp30=100.0), "VA1")["points"] == -2.0
    assert item(rate(fund={"target_price": 101.0}, tp30=100.0), "VA1")["points"] == 0.0
    assert item(rate(fund=f, tp30=None), "VA1")["status"] == "na"   # 歷史不足 30 日
    assert item(rate(fund={}, tp30=100.0), "VA1")["status"] == "na"


# ---------- 8. bucket clamp（以 config 縮 cap 驗證） ----------

def test_bucket_clamp_and_available_respect_cap():
    r = rate(mk_data(close=109.0), config={"cap_trend": 3.0})
    assert r["buckets"]["TREND"]["score"] == 3.0  # 6 → clamp 3
    b_avail_total = r["available_max"]
    # TREND available 亦被 cap 限制: min(3, 6)=3；MOMENTUM 5 → 合計 8 < 10 → na
    assert b_avail_total == 8.0
    assert r["rating_key"] == "na"

    r2 = rate(mk_data(close=62.0, prev_close=63.0, ma60=70.0, ma200=80.0,
                      rsi=25.0, macd_hist=-0.5, lo252=60.0),
              config={"cap_trend": 3.0, "min_available": 8.0})
    assert r2["buckets"]["TREND"]["score"] == -3.0  # −6 → clamp −3


# ---------- 9. MIN_AVAILABLE 擋下映射 / 重正規化 ----------

def test_min_available_gate_and_renormalization():
    # 只有技術面（fund/opt 全缺）：available = 6+5 = 11 ≥ 10 → 正常映射
    r = rate(mk_data(close=109.0))
    assert r["available_max"] == 11.0
    assert r["score_norm"] == 20.0  # 11/11×20，重正規化不因缺籌碼被壓分
    assert r["rating_key"] == "sb"
    # 再拿掉 MOMENTUM → 只剩 TREND 6 < 10 → na
    data = mk_data(close=109.0, rsi=None, macd_hist=None)
    data["indicators"]["vol_ma20"] = None
    r2 = rate(data)
    assert r2["available_max"] == 6.0
    assert r2["rating_key"] == "na" and r2["score_norm"] is None


# ---------- 10. 五級映射邊界 ----------

def test_rating_map_boundaries():
    caps_full = {"map_sb": 12.0, "map_b": 5.0, "map_s": -5.0, "map_ss": -12.0}
    def key_for(score):
        cfg = dict(caps_full)
        if score >= cfg["map_sb"]: return "sb"
        elif score >= cfg["map_b"]: return "b"
        elif score > cfg["map_s"]: return "n"
        elif score > cfg["map_ss"]: return "s"
        return "ss"
    # 邊界值語意（與 SPEC §4 一致）
    assert key_for(12.0) == "sb" and key_for(11.99) == "b"
    assert key_for(5.0) == "b" and key_for(4.99) == "n"
    assert key_for(-5.0) == "s" and key_for(-4.99) == "n"
    assert key_for(-12.0) == "ss" and key_for(-11.99) == "s"


# ---------- 11. TR1/TR2/TR3 三態 ----------

def test_trend_items_tri_state():
    assert item(rate(mk_data(close=95, ma60=100)), "TR1")["points"] == -2.0
    assert item(rate(mk_data(ma60=None)), "TR1")["status"] == "na"
    assert item(rate(mk_data(ma60=70, ma200=80)), "TR2")["points"] == -2.0
    assert item(rate(mk_data(ma200=None)), "TR2")["status"] == "na"
    r = rate(mk_data(hi252=100, lo252=100))  # 分母 ≤ 0
    assert item(r, "TR3")["status"] == "na"


# ---------- 12. details 必含全部 10 項（含 0 分與 N/A） ----------

def test_details_include_zero_and_na():
    r = rate(mk_data(), None, None)
    assert len(r["details"]) == 10
    statuses = {d["id"]: d["status"] for d in r["details"]}
    assert statuses["PO1"] == "na" and statuses["SE1"] == "na" and statuses["VA1"] == "na"
    assert statuses["TR3"] == "zero"  # pos=0.8 落中段
    assert {"pos", "zero", "na"} <= set(statuses.values())


# ---------- 13.5 rating_history (§6)：冪等 upsert / 400 日裁剪 / VA1 快照 ----------

def test_history_upsert_idempotent_and_trim():
    h = {}
    us.upsert_rating_history(h, "NVDA", {"date": "2026-07-06", "score_norm": 8.5})
    us.upsert_rating_history(h, "NVDA", {"date": "2026-07-06", "score_norm": 9.0})  # 同日重跑覆寫
    assert len(h["NVDA"]) == 1 and h["NVDA"][0]["score_norm"] == 9.0
    for i in range(1, 500):
        us.upsert_rating_history(h, "NVDA", {"date": f"2030-{i:04d}", "score_norm": i})
    assert len(h["NVDA"]) == us.RATING_HISTORY_KEEP == 400
    dates = [e["date"] for e in h["NVDA"]]
    assert dates == sorted(dates)  # 升冪且裁掉最舊


def test_target_price_from_history_lookback():
    entries = [{"date": "2026-05-01", "target_price": 100.0},
               {"date": "2026-06-05", "target_price": 110.0},
               {"date": "2026-07-01", "target_price": 120.0}]
    # cutoff = 2026-06-06 → 取 ≤ cutoff 的最新一筆 = 06-05 的 110
    assert us.target_price_from_history(entries, "2026-07-06") == 110.0
    # 上線未滿 30 日 → None → VA1 N/A
    assert us.target_price_from_history(entries[-1:], "2026-07-06") is None
    assert us.target_price_from_history([], "2026-07-06") is None
    assert us.target_price_from_history(None, "2026-07-06") is None


def test_history_roundtrip(tmp_path):
    p = str(tmp_path / "rating_history.json")
    h = {}
    us.upsert_rating_history(h, "AAPL", {"date": "2026-07-06", "score_norm": 3.2,
                                         "rating_key": "n", "target_price": None,
                                         "buckets": {"TREND": 2.0}, "v1_total": 9.5})
    us.write_rating_history(h, p)
    assert us.load_rating_history(p) == h
    assert us.load_rating_history(str(tmp_path / "missing.json")) == {}


# ---------- 13. NaN 輸入視同缺值 ----------

def test_nan_inputs_treated_as_na():
    r = rate(fund={"inst_pct": float("nan"), "shares_short": float("nan"),
                   "shares_short_prior": 100, "target_price": float("nan")},
             opt={"pcr_oi": float("nan")}, tp30=100.0)
    for iid in ("PO1", "PO2", "SE1", "VA1"):
        assert item(r, iid)["status"] == "na", iid
