"""
投資組合風險調整後報酬評估模組 (Portfolio risk-adjusted return metrics)

排在既有 IBKR Flex NAV pipeline 之後跑 (GitHub Actions)：
  nav_history.json  +  yfinance(SPY/SOXX/QQQ/^IRX)  ──►  docs/data/portfolio_metrics.json
儀表板 (us_stock_dark.py) 於建置時讀此 JSON 並渲染，前端不做數值計算。

指標分工 (定案，勿增刪主指標)：
  Sharpe(年化)            門面 — 風險調整後划不划算
  Information Ratio vs SOXX 選股能力 — 半導體裡有沒有 alpha (附內建 t 檢定)
  Sortino                 加分項 — 只罰下行波動
  Treynor                 僅註腳 — 組合集中時偏樂觀，不得當主指標

年化因子一律 252。所有指標的 Rf 與報酬單位一致 (日報酬 → 年化)。
規格見 SPEC「投資組合風險調整後報酬評估模組」。
"""

import os
import sys
import json
import math
from datetime import datetime, timedelta

import numpy as np
import pandas as pd

# ── 設定 (§10) ─────────────────────────────────────────────────────────────
BENCHMARKS     = ["SPY", "SOXX", "QQQ"]
IR_BENCHMARK   = "SOXX"   # Information Ratio 對標 (類股基準)
BETA_BENCHMARK = "SPY"    # Treynor 的 β (市場基準)
ANNUALIZATION  = 252
ROLLING_WINDOW = 252
RF_MODE        = "tbill_3m"   # "tbill_3m" | "zero"
SORTINO_TARGET = "rf"        # "rf" | "zero"
LOOKBACK_DAYS  = None        # None = 用 nav_daily 全部 (連續段) 歷史

NAV_HISTORY_FILE = "nav_history.json"
OUTPUT_FILE      = "docs/data/portfolio_metrics.json"
GAP_DAYS         = 10        # NAV 序列 >GAP_DAYS 天的間隔 → 視為孤立基準點，取最後連續段
RF_TICKER        = "^IRX"    # 13 週國庫券殖利率 (年化百分比，需 /100)

# ── §4 現金流污染：nav_history.json 來源說明 ──────────────────────────────────
# 經 fetch_ibkr_nav.py 由 IBKR Flex Web Service 的 EquitySummary 累積，
# 每筆只有 {date, nav(=total), cash(=現金餘額)}。
#   * cash 是「現金部位餘額」，不是「當日外部現金流 (入金/出金)」——兩者不可混為一談。
#   * Flex 報表此 query 未輸出時間加權報酬 (TWR) 欄位，也沒有逐日 flow 欄位。
# 因此預設只能走 r_t = NAV_t / NAV_{t-1} - 1，且必須誠實標記 cashflow_adjusted=false、
# 由儀表板顯眼警示 (見 §4)，不要靜默給出可能被入金/出金污染的數字。
#
# 本模組已預留現金流校正路徑，一旦 nav_history.json 的 series entry 帶有下列任一欄位即自動啟用：
#   "twr"  : 該日時間加權日報酬 (小數，例 0.012) → 直接採用 (首選)
#   "flow" : 該日外部現金流 (入金為正、出金為負) → r_t=(NAV_t-flow_t)/NAV_{t-1}-1 (次選)
RET_FIELD_TWR  = "twr"
FLOW_FIELD     = "flow"


# ── NAV → 日報酬 (§4, §5.1) ─────────────────────────────────────────────────
def load_nav_series(path=NAV_HISTORY_FILE):
    """讀 nav_history.json → 依日期升冪的 [{date, nav, cash?, flow?, twr?}]，
    過濾 nav<=0 (Flex 對未報告/未入金日回 0 的垃圾值)。無檔回 []。"""
    if not os.path.exists(path):
        return []
    with open(path, "r", encoding="utf-8") as f:
        data = json.load(f)
    rows = data.get("series", []) if isinstance(data, dict) else (data if isinstance(data, list) else [])
    out = []
    for r in rows:
        d = r.get("date")
        v = r.get("nav", r.get("net_liq"))
        if not d or v is None:
            continue
        try:
            nav = float(v)
        except (TypeError, ValueError):
            continue
        if nav <= 0:
            continue
        rec = {"date": d, "nav": nav}
        for k in ("cash", FLOW_FIELD, RET_FIELD_TWR):
            if r.get(k) is not None:
                try:
                    rec[k] = float(r[k])
                except (TypeError, ValueError):
                    pass
        out.append(rec)
    out.sort(key=lambda x: x["date"])
    return out


def continuous_segment(nav_rows, gap_days=GAP_DAYS):
    """取最後一段連續日資料 (跳過孤立的手動基準點)：找最後一個 >gap_days 天的大間隔，
    從其後一筆起算。與 compute_portfolio_history 的 comp_start 邏輯一致。"""
    if len(nav_rows) < 2:
        return nav_rows
    ds = [datetime.strptime(r["date"], "%Y-%m-%d") for r in nav_rows]
    start = 0
    for i in range(len(ds) - 1, 0, -1):
        if (ds[i] - ds[i - 1]).days > gap_days:
            start = i
            break
    return nav_rows[start:]


def nav_to_returns(nav_rows):
    """NAV 序列 → (port_ret: pd.Series[date→日報酬], nav: pd.Series[date→NAV], cashflow_adjusted)。

    §4 規則 (擇一，依資料可得性)：
      1. 有 twr  欄位 → 直接用 (cashflow_adjusted=True)
      2. 有 flow 欄位 → r_t=(NAV_t-flow_t)/NAV_{t-1}-1 (cashflow_adjusted=True)
      3. 皆無      → r_t=NAV_t/NAV_{t-1}-1 (cashflow_adjusted=False，須警示)
    """
    if len(nav_rows) < 2:
        return pd.Series(dtype=float), pd.Series(dtype=float), False
    idx = pd.to_datetime([r["date"] for r in nav_rows])
    nav = pd.Series([r["nav"] for r in nav_rows], index=idx, dtype=float)

    has_twr  = all(RET_FIELD_TWR in r for r in nav_rows[1:])
    has_flow = any(FLOW_FIELD in r for r in nav_rows)
    if has_twr:
        ret = pd.Series([r[RET_FIELD_TWR] for r in nav_rows[1:]], index=idx[1:], dtype=float)
        return ret, nav, True
    if has_flow:
        flows = np.array([r.get(FLOW_FIELD, 0.0) for r in nav_rows], dtype=float)
        navv  = nav.values
        ret = pd.Series((navv[1:] - flows[1:]) / navv[:-1] - 1.0, index=idx[1:])
        return ret, nav, True
    ret = nav.pct_change().dropna()
    return ret, nav, False


# ── 年化指標 (§5) ───────────────────────────────────────────────────────────
def annualize(ret, rf_annual):
    """日報酬序列 → (ann_return 算術年化, ann_vol, mu_d, sig_d, T, years)。"""
    r = np.asarray(ret, dtype=float)
    T = r.size
    mu_d  = float(np.mean(r)) if T else float("nan")
    sig_d = float(np.std(r, ddof=1)) if T > 1 else float("nan")
    return {
        "ann_return": mu_d * ANNUALIZATION,
        "ann_vol": sig_d * math.sqrt(ANNUALIZATION),
        "mu_d": mu_d, "sig_d": sig_d, "T": T, "years": T / ANNUALIZATION,
    }


def sharpe_with_ci(ret, rf_annual):
    """年化 Sharpe + Lo(2002) iid 信賴帶 (§5.2)。退化 (sig=0/樣本不足) 回 None。"""
    r = np.asarray(ret, dtype=float)
    T = r.size
    if T < 2:
        return {"value": None, "se": None, "ci95": [None, None]}
    rf_d  = rf_annual / ANNUALIZATION
    mu_d  = float(np.mean(r))
    sig_d = float(np.std(r, ddof=1))
    if sig_d == 0 or not math.isfinite(sig_d):
        return {"value": None, "se": None, "ci95": [None, None]}
    sr_d = (mu_d - rf_d) / sig_d
    se_d = math.sqrt((1 + 0.5 * sr_d ** 2) / T)
    sharpe_ann = sr_d * math.sqrt(ANNUALIZATION)
    se_ann     = se_d * math.sqrt(ANNUALIZATION)
    return {
        "value": round(sharpe_ann, 4),
        "se": round(se_ann, 4),
        "ci95": [round(sharpe_ann - 1.96 * se_ann, 4), round(sharpe_ann + 1.96 * se_ann, 4)],
    }


def sharpe_value(ret, rf_annual):
    """只取 Sharpe 數值 (給基準比較表用)。"""
    return sharpe_with_ci(ret, rf_annual)["value"]


def sortino(ret, rf_annual):
    """年化 Sortino (§5.4)。下行偏差對『全樣本』取平均 (非只對負值樣本數)。"""
    r = np.asarray(ret, dtype=float)
    T = r.size
    if T < 2:
        return None
    target = (rf_annual / ANNUALIZATION) if SORTINO_TARGET == "rf" else 0.0
    downside = np.minimum(r - target, 0.0)
    dd_d = math.sqrt(float(np.mean(downside ** 2)))
    if dd_d == 0:
        return None
    ann_return = float(np.mean(r)) * ANNUALIZATION
    return round((ann_return - rf_annual) / (dd_d * math.sqrt(ANNUALIZATION)), 4)


def max_drawdown(nav):
    """以 NAV 累積序列算最大回撤 (§5.7)。"""
    v = np.asarray(nav, dtype=float)
    if v.size < 2:
        return None
    peak = np.maximum.accumulate(v)
    return round(float(np.min(v / peak - 1.0)), 4)


def cagr(ret, T=None):
    """幾何年化 (僅供展示，§5.1)。"""
    r = np.asarray(ret, dtype=float)
    n = T if T is not None else r.size
    if n == 0:
        return None
    growth = float(np.prod(1.0 + r))
    if growth <= 0:
        return None
    return round(growth ** (ANNUALIZATION / n) - 1.0, 4)


def information_ratio(port_ret, bench_ret):
    """IR vs 基準 + 內建 t 檢定 (§5.3)。對齊日期後相減。
    tracking_error=0 (退化) → 走 §8.6 null 分支。"""
    a = (port_ret - bench_ret).dropna()
    T = a.size
    if T < 2:
        return {"ir": None, "t_stat": None, "significant": False,
                "ann_active_return": None, "tracking_error": None, "years": T / ANNUALIZATION}
    mean_a = float(np.mean(a.values))
    std_a  = float(np.std(a.values, ddof=1))
    years  = T / ANNUALIZATION
    ann_active = mean_a * ANNUALIZATION
    te = std_a * math.sqrt(ANNUALIZATION)
    if te == 0 or not math.isfinite(te):          # §8.6 完全相同 → 避免除零
        return {"ir": None, "t_stat": None, "significant": False,
                "ann_active_return": round(ann_active, 4), "tracking_error": 0.0, "years": years}
    ir = ann_active / te
    t_stat = ir * math.sqrt(years)                # ≡ mean(a)/std(a)*sqrt(T)
    return {
        "ir": round(ir, 4), "t_stat": round(t_stat, 4),
        "significant": bool(abs(t_stat) > 2),
        "ann_active_return": round(ann_active, 4),
        "tracking_error": round(te, 4), "years": years,
    }


def treynor(port_ret, mkt_ret, rf_annual):
    """Treynor (§5.5，僅註腳)。β=cov(rp,rm)/var(rm)。"""
    df = pd.concat([port_ret, mkt_ret], axis=1).dropna()
    if df.shape[0] < 2:
        return {"beta": None, "value": None}
    rp = df.iloc[:, 0].values
    rm = df.iloc[:, 1].values
    var_m = float(np.var(rm, ddof=1))
    if var_m == 0:
        return {"beta": None, "value": None}
    beta = float(np.cov(rp, rm, ddof=1)[0, 1] / var_m)
    if beta == 0:
        return {"beta": round(beta, 4), "value": None}
    ann_return = float(np.mean(rp)) * ANNUALIZATION
    return {"beta": round(beta, 4), "value": round((ann_return - rf_annual) / beta, 4)}


def rolling_sharpe(ret, rf_annual, window=ROLLING_WINDOW):
    """滾動年化 Sharpe (§5.6)。前 window-1 日不足窗 → None。
    回 dict{Timestamp→float|None} (刻意不用 pd.Series，避免 None→NaN 洩漏進 JSON)。"""
    rf_d = rf_annual / ANNUALIZATION
    out = {}
    vals = ret.values
    idx = ret.index
    for i in range(len(vals)):
        if i + 1 < window:
            out[idx[i]] = None
            continue
        w = vals[i + 1 - window: i + 1]
        sig = float(np.std(w, ddof=1))
        if sig == 0 or not math.isfinite(sig):
            out[idx[i]] = None
            continue
        sr_d = (float(np.mean(w)) - rf_d) / sig
        out[idx[i]] = round(sr_d * math.sqrt(ANNUALIZATION), 4)
    return out


# ── yfinance 抓取 ───────────────────────────────────────────────────────────
def fetch_adj_close(ticker, start, end):
    """抓 ticker 調整後收盤 → pd.Series[date→close] (tz-naive)。失敗回空 Series。"""
    import yfinance as yf
    try:
        h = yf.Ticker(ticker).history(start=start, end=end, auto_adjust=True)["Close"].dropna()
        if h.empty:
            return pd.Series(dtype=float)
        h.index = pd.to_datetime(h.index).tz_localize(None).normalize()
        return h
    except Exception as e:
        print(f"  ⚠️ 抓取 {ticker} 失敗: {e}")
        return pd.Series(dtype=float)


def fetch_rf_annual(start, end, mode=RF_MODE):
    """Rf 年化利率 (小數)。tbill_3m=^IRX 期間均值/100；抓取失敗或 mode=zero → (0.0, fallback)。
    回 (annual_rate, fallback_used)。"""
    if mode == "zero":
        return 0.0, False
    series = fetch_adj_close(RF_TICKER, start, end)
    if series.empty:
        print("  ⚠️ ^IRX 抓取失敗 → Rf 退回 zero 模式")
        return 0.0, True
    return round(float(series.mean()) / 100.0, 6), False


# ── 主流程 ──────────────────────────────────────────────────────────────────
def build_metrics(nav_rows, bench_closes, rf_annual, rf_mode, rf_fallback, cashflow_adjusted):
    """純函式：給定 NAV 連續段、基準收盤 dict、Rf → portfolio_metrics dict (§6 契約)。
    benchmark 收盤轉日報酬後與組合對齊。"""
    port_ret, nav, _ = nav_to_returns(nav_rows)
    # 基準日報酬：抓取視窗比 NAV 起點早幾天 (供 pct_change 取得前一日收盤)，
    # 算完後裁切到組合報酬的日期跨度，確保比較表與 NAV 同一日曆窗 (§7.2/§8.2)。
    lo, hi = (port_ret.index.min(), port_ret.index.max()) if port_ret.size else (None, None)
    bench_ret = {}
    for tk, s in bench_closes.items():
        if s.empty:
            continue
        br = s.pct_change().dropna()
        if lo is not None:
            br = br[(br.index >= lo) & (br.index <= hi)]
        bench_ret[tk] = br

    start_d = nav_rows[0]["date"]
    end_d   = nav_rows[-1]["date"]
    a = annualize(port_ret, rf_annual)

    portfolio = {
        "ann_return": _r(a["ann_return"]),
        "ann_vol": _r(a["ann_vol"]),
        "cagr": cagr(port_ret),
        "max_drawdown": max_drawdown(nav),
        "sharpe": sharpe_with_ci(port_ret, rf_annual),
        "sortino": sortino(port_ret, rf_annual),
    }

    benchmarks = {}
    for tk in BENCHMARKS:
        br = bench_ret.get(tk)
        if br is None or br.size < 2:
            benchmarks[tk] = {"sharpe": None, "ann_return": None, "ann_vol": None}
            continue
        ba = annualize(br, rf_annual)
        benchmarks[tk] = {
            "sharpe": sharpe_value(br, rf_annual),
            "ann_return": _r(ba["ann_return"]),
            "ann_vol": _r(ba["ann_vol"]),
        }

    # Information Ratio vs SOXX (對齊後相減)
    ir_bench_ret = bench_ret.get(IR_BENCHMARK)
    if ir_bench_ret is not None:
        ir = information_ratio(port_ret, ir_bench_ret)
    else:
        ir = {"ir": None, "t_stat": None, "significant": False,
               "ann_active_return": None, "tracking_error": None, "years": a["years"]}
    information = {
        "benchmark": IR_BENCHMARK,
        "ir": ir["ir"], "t_stat": ir["t_stat"], "significant": ir["significant"],
        "ann_active_return": ir["ann_active_return"], "tracking_error": ir["tracking_error"],
        "note": _ir_note(ir),
    }

    # Treynor (僅註腳)
    beta_bench_ret = bench_ret.get(BETA_BENCHMARK)
    tre = treynor(port_ret, beta_bench_ret, rf_annual) if beta_bench_ret is not None else {"beta": None, "value": None}
    treynor_out = {
        "benchmark": BETA_BENCHMARK, "beta": tre["beta"], "value": tre["value"],
        "warning": "組合未充分分散，β 未涵蓋特異風險，此值偏樂觀，僅供參考",
    }

    # Rolling Sharpe (portfolio + SPY + SOXX)，統一日期軸
    rs_map = {"portfolio": rolling_sharpe(port_ret, rf_annual)}
    for tk in ("SPY", "SOXX"):
        if bench_ret.get(tk) is not None:
            rs_map[tk] = rolling_sharpe(bench_ret[tk], rf_annual)
    all_dates = sorted(set().union(*[set(s.keys()) for s in rs_map.values()])) if rs_map else []
    rolling = {"window_days": ROLLING_WINDOW,
               "dates": [d.strftime("%Y-%m-%d") for d in all_dates]}
    for key, s in rs_map.items():
        rolling[key] = [s.get(d) for d in all_dates]   # 缺值/不足窗一律 None，不洩 NaN

    return {
        "as_of": end_d,
        "lookback": {"start": start_d, "end": end_d, "n_days": a["T"], "years": round(a["years"], 4)},
        "risk_free": {"mode": rf_mode, "annual_rate": rf_annual, "fallback_used": rf_fallback},
        "cashflow_adjusted": cashflow_adjusted,
        "portfolio": portfolio,
        "benchmarks": benchmarks,
        "information_ratio": information,
        "treynor": treynor_out,
        "rolling_sharpe": rolling,
    }


def _r(v, n=4):
    return round(float(v), n) if v is not None and math.isfinite(float(v)) else None


def _ir_note(ir):
    if ir["ir"] is None:
        return "組合與基準報酬完全相同 (tracking error=0)，IR 不適用"
    verdict = "已達顯著 (|t|>2)" if ir["significant"] else "尚在雜訊範圍"
    return f"IR={ir['ir']:.2f}、樣本 {ir['years']:.1f} 年 → t≈{ir['t_stat']:.2f}，{verdict}"


def main():
    print("=== 投資組合風險調整後報酬評估 ===")
    nav_rows_all = load_nav_series()
    if len(nav_rows_all) < 2:
        print("  ⚠️ NAV 歷史不足 (<2 筆)，略過。")
        return
    nav_rows = continuous_segment(nav_rows_all)
    if LOOKBACK_DAYS:
        nav_rows = nav_rows[-LOOKBACK_DAYS:]
    if len(nav_rows) < 2:
        print("  ⚠️ 連續段 NAV 不足 (<2 筆)，略過。")
        return

    start = (datetime.strptime(nav_rows[0]["date"], "%Y-%m-%d") - timedelta(days=7)).strftime("%Y-%m-%d")
    end   = (datetime.strptime(nav_rows[-1]["date"], "%Y-%m-%d") + timedelta(days=1)).strftime("%Y-%m-%d")
    print(f"  區間 {nav_rows[0]['date']} → {nav_rows[-1]['date']} ({len(nav_rows)} 交易日)")

    bench_closes = {tk: fetch_adj_close(tk, start, end) for tk in BENCHMARKS}
    rf_annual, rf_fallback = fetch_rf_annual(start, end, RF_MODE)
    rf_mode = "zero" if (RF_MODE == "zero" or rf_fallback) else RF_MODE
    print(f"  Rf={rf_annual:.4%} (mode={rf_mode}, fallback={rf_fallback})")

    _, _, cashflow_adjusted = nav_to_returns(nav_rows)
    if not cashflow_adjusted:
        print("  ⚠️ NAV 來源無 TWR/flow 欄位 → cashflow_adjusted=false (儀表板將警示)")

    metrics = build_metrics(nav_rows, bench_closes, rf_annual, rf_mode, rf_fallback, cashflow_adjusted)

    os.makedirs(os.path.dirname(OUTPUT_FILE), exist_ok=True)
    with open(OUTPUT_FILE, "w", encoding="utf-8") as f:
        json.dump(metrics, f, ensure_ascii=False, indent=2, allow_nan=False)  # NaN/Inf 會破壞瀏覽器 JSON.parse
    p = metrics["portfolio"]
    sh = p["sharpe"]["value"]
    print(f"  ✓ {OUTPUT_FILE} | Sharpe={sh} IR={metrics['information_ratio']['ir']} "
          f"(t={metrics['information_ratio']['t_stat']}) MDD={p['max_drawdown']}")


# ── 自我驗收 (§9，無需網路) ──────────────────────────────────────────────────
def _selftest():
    print("=== selftest (§9 驗收) ===")
    rng = np.random.default_rng(42)
    idx = pd.bdate_range("2024-01-01", periods=504)
    rp = pd.Series(rng.normal(0.0008, 0.012, 504), index=idx)
    soxx = pd.Series(rng.normal(0.0006, 0.014, 504), index=idx)
    rf = 0.05

    # 數值恆等式 t_stat == ir * sqrt(years)
    ir = information_ratio(rp, soxx)
    lhs = ir["t_stat"]
    rhs = ir["ir"] * math.sqrt(ir["years"])
    assert abs(lhs - rhs) < 1e-3, (lhs, rhs)   # 容許 4-dp 四捨五入誤差 (§9)
    print(f"  ✓ t_stat == ir*sqrt(years): {lhs:.6f} == {rhs:.6f}")

    # r_portfolio == r_SOXX → IR null、tracking_error 0 (§8.6)
    deg = information_ratio(soxx.copy(), soxx.copy())
    assert deg["ir"] is None and deg["tracking_error"] == 0.0, deg
    print(f"  ✓ 退化案例 IR=null, TE=0.0: {deg}")

    # Sharpe SE/CI 結構
    sh = sharpe_with_ci(rp, rf)
    assert sh["ci95"][0] < sh["value"] < sh["ci95"][1], sh
    print(f"  ✓ Sharpe={sh['value']} ci95={sh['ci95']}")

    # NAV→returns 三條路徑
    base = [{"date": "2024-01-02", "nav": 100.0}, {"date": "2024-01-03", "nav": 110.0}]
    r0, _, adj0 = nav_to_returns(base)
    assert adj0 is False and abs(r0.iloc[0] - 0.10) < 1e-9, (adj0, r0.iloc[0])
    flow = [{"date": "2024-01-02", "nav": 100.0}, {"date": "2024-01-03", "nav": 160.0, "flow": 50.0}]
    r1, _, adj1 = nav_to_returns(flow)
    assert adj1 is True and abs(r1.iloc[0] - 0.10) < 1e-9, (adj1, r1.iloc[0])  # (160-50)/100-1=0.10
    twr = [{"date": "2024-01-02", "nav": 100.0}, {"date": "2024-01-03", "nav": 999.0, "twr": 0.02}]
    r2, _, adj2 = nav_to_returns(twr)
    assert adj2 is True and abs(r2.iloc[0] - 0.02) < 1e-9, (adj2, r2.iloc[0])
    print("  ✓ NAV→returns: raw(false) / flow(true) / twr(true) 三路徑正確")

    # build_metrics 端到端 (合成基準收盤) — 確認 JSON 契約欄位齊全
    nav_rows = [{"date": d.strftime("%Y-%m-%d"), "nav": float(v)}
                for d, v in zip(idx, 100 * np.cumprod(1 + rp.values))]
    spy_close = pd.Series(100 * np.cumprod(1 + rng.normal(0.0005, 0.011, 504)), index=idx)
    bench = {"SPY": spy_close, "SOXX": 100 * (1 + soxx).cumprod(), "QQQ": spy_close * 1.0}
    m = build_metrics(nav_rows, bench, rf, "tbill_3m", False, False)
    for key in ("as_of", "lookback", "risk_free", "cashflow_adjusted", "portfolio",
                "benchmarks", "information_ratio", "treynor", "rolling_sharpe"):
        assert key in m, key
    assert m["treynor"]["warning"]
    assert set(m["benchmarks"]) == set(BENCHMARKS)
    json.dumps(m, allow_nan=False)  # 嚴格序列化：任何 NaN/Inf 都應拋例外
    # Rolling Sharpe：前 251 日須為 None，且不得有 NaN 洩漏
    rsp = m["rolling_sharpe"]["portfolio"]
    n_null = sum(1 for v in rsp if v is None)
    assert n_null == ROLLING_WINDOW - 1, (n_null, ROLLING_WINDOW - 1)
    assert all((v is None) or isinstance(v, (int, float)) for v in rsp)
    print(f"  ✓ rolling_sharpe 前 {n_null} 日為 None、無 NaN 洩漏")
    # §9 sanity：SPY 年化波動落在常識範圍 (合成 sigma 0.011 → ~0.17)
    spy_vol = m["benchmarks"]["SPY"]["ann_vol"]
    assert 0.10 < spy_vol < 0.25, spy_vol
    print(f"  ✓ SPY ann_vol={spy_vol} 落在常識範圍 (sanity)")
    print(f"  ✓ build_metrics 契約齊全；Sharpe={m['portfolio']['sharpe']['value']} "
          f"IR={m['information_ratio']['ir']} note={m['information_ratio']['note']}")
    print("=== selftest 全數通過 ===")


if __name__ == "__main__":
    if len(sys.argv) > 1 and sys.argv[1] == "--selftest":
        _selftest()
    else:
        main()
