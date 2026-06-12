"""
美股監控機器人 - 深色終端版 (US Market Dark Terminal)
資料來源: yfinance + CNN Fear & Greed 內部 API (皆免費)
慣例: 綠漲紅跌 (US convention)
輸出: docs/us.html  (可與台股版 docs/index.html 並存於同一 repo)
"""

import os
import json
import math
import requests
import subprocess
from datetime import datetime, timedelta, timezone
import yfinance as yf
import pandas as pd
import numpy as np
import concurrent.futures

try:
    from zoneinfo import ZoneInfo
    ET = ZoneInfo("America/New_York")
except Exception:
    ET = timezone(timedelta(hours=-4))

_gemini_quota_exhausted = False

def now_et():
    return datetime.now(ET)

# ===== 設定 =====
def load_stocks():
    if os.path.exists("us_stocks.txt"):
        with open("us_stocks.txt", "r", encoding="utf-8") as f:
            stocks = [line.strip().upper() for line in f if line.strip() and not line.startswith("#")]
        return stocks if stocks else ["AAPL", "NVDA", "MSFT"]
    return [s.strip().upper() for s in os.getenv("US_STOCKS", "AAPL,NVDA,MSFT").split(",")]

STOCKS = load_stocks()
ANTHROPIC_API_KEY = os.getenv("ANTHROPIC_API_KEY", "")
TELEGRAM_BOT_TOKEN = os.getenv("TELEGRAM_BOT_TOKEN", "")
TELEGRAM_CHAT_ID = os.getenv("TELEGRAM_CHAT_ID", "")
AI_PROVIDER    = os.getenv("AI_PROVIDER", "claude").lower()
CLAUDE_MODEL   = os.getenv("CLAUDE_MODEL", "claude-3-5-sonnet-20240620")
GEMINI_MODEL   = os.getenv("GEMINI_MODEL", "gemini-1.5-flash")
GEMINI_API_KEY = os.getenv("GEMINI_API_KEY", "")

# Google Apps Script 端點 (與台股版相同機制；若要在頁面上新增/刪除股票與觸發重跑, 填入你的 US repo 對應 URL)
GAS_URL = os.getenv("US_GAS_URL", "")
TRIGGER_URL = os.getenv("US_TRIGGER_URL", "")

OUTPUT_DIR = "docs"
OUTPUT_FILE = f"{OUTPUT_DIR}/us.html"

SECTOR_ETFS = {
    "XLK": "科技", "XLF": "金融", "XLV": "醫療", "XLY": "非必需消費",
    "XLP": "必需消費", "XLE": "能源", "XLI": "工業", "XLU": "公用事業",
    "XLB": "原物料", "XLRE": "房地產", "XLC": "通訊服務",
}

# ===== 殘差動能 (Residual Momentum) 參數 — 見 SPEC_residual_momentum =====
RM_FETCH_PERIOD   = "3y"   # 需覆蓋回歸視窗 + 指標歷史
RM_REG_WINDOW     = 252    # 滾動回歸視窗 (交易日)
RM_MIN_WINDOW     = 120    # 視窗內有效樣本低於此數 → 該日殘差 NaN
RM_OSC_WINDOW     = 20     # 滾動 Alpha 震盪平均視窗
RM_MOM_FORM_START = 252    # rMOM 形成期起點 (t-252)
RM_MOM_FORM_END   = 21     # rMOM 形成期終點 (t-21)，即 12-1 月動能
RM_MOM_MIN_OBS    = 150    # 形成期有效殘差數低於此 → rMOM NaN
RM_Z_SHORT_WINDOW = 21     # 短期 Z-Score 視窗
RM_Z_STD_WINDOW   = 252    # Z-Score 分母波動率估計視窗
RM_MARKET_ETF     = "SPY"  # 市場因子

# yfinance info['sector'] → SPDR 類股 ETF (半導體業另以 SOXX 覆寫)
YF_SECTOR_ETF = {
    "Technology": "XLK", "Financial Services": "XLF", "Healthcare": "XLV",
    "Consumer Cyclical": "XLY", "Consumer Defensive": "XLP", "Energy": "XLE",
    "Industrials": "XLI", "Utilities": "XLU", "Basic Materials": "XLB",
    "Real Estate": "XLRE", "Communication Services": "XLC",
}

RM_SIGNAL_ZH = {
    "overheat": "強勢過熱", "pullback": "強勢回檔", "strong": "強勢",
    "weak": "弱勢", "neutral": "中性", "no_signal": "無訊號",
}

# =========================================================
# IBKR 持股 / 交易快照
# 由 IBKR API (MCP) 取得後存於 ibkr_data.json，於建置時讀取。
# (GitHub Actions 排程建置環境無法直連券商，故採快照檔)
# =========================================================
IBKR_DATA_FILE = "ibkr_data.json"

def load_ibkr_data():
    if not os.path.exists(IBKR_DATA_FILE):
        return {}
    try:
        with open(IBKR_DATA_FILE, "r", encoding="utf-8") as f:
            return json.load(f)
    except Exception as e:
        print(f"  ⚠️ 讀取 IBKR 快照失敗: {e}")
        return {}

def _trade_et_date(trade_time):
    """UTC ISO 字串 → ET 日期物件 (對齊 K 線的交易日)"""
    try:
        dt = datetime.fromisoformat(str(trade_time).replace("Z", "+00:00"))
        return dt.astimezone(ET).date()
    except Exception:
        return None

def build_trade_markers(ibkr):
    """聚合每檔股票「每日 × 買賣方向」的成交 (以量加權均價 VWAP)。
    回傳 {symbol: [ {et_date, side, price, size}, ... ]}"""
    agg = {}  # (symbol, date, side) -> [sum(price*size), sum(size)]
    for t in ibkr.get("trades", []):
        sym = (t.get("symbol") or "").upper()
        d = _trade_et_date(t.get("trade_time", ""))
        side = t.get("side")
        try:
            sz = float(t.get("size") or 0)
            pr = float(t.get("price") or 0)
        except (TypeError, ValueError):
            continue
        if not sym or not d or side not in ("BUY", "SELL") or sz <= 0:
            continue
        a = agg.setdefault((sym, d, side), [0.0, 0.0])
        a[0] += pr * sz
        a[1] += sz
    markers = {}
    for (sym, d, side), (pv, sz) in agg.items():
        if sz <= 0:
            continue
        markers.setdefault(sym, []).append({
            "et_date": d, "side": side, "price": round(pv / sz, 2), "size": round(sz, 4),
        })
    return markers

# =========================================================
# 技術指標計算
# =========================================================
def calculate_sma(series, period):
    return series.rolling(window=period).mean()

def calculate_rsi(series, period=14):
    delta = series.diff()
    gain = (delta.where(delta > 0, 0)).rolling(window=period).mean()
    loss = (-delta.where(delta < 0, 0)).rolling(window=period).mean()
    rs = gain / loss
    return 100 - (100 / (1 + rs))

def calculate_macd(series, fast=12, slow=26, signal=9):
    ema_fast = series.ewm(span=fast, adjust=False).mean()
    ema_slow = series.ewm(span=slow, adjust=False).mean()
    macd_line = ema_fast - ema_slow
    signal_line = macd_line.ewm(span=signal, adjust=False).mean()
    return macd_line, signal_line

def calculate_stochastic(high, low, close, period=14, smooth_k=3, smooth_d=3):
    lowest_low = low.rolling(window=period).min()
    highest_high = high.rolling(window=period).max()
    k = 100 * ((close - lowest_low) / (highest_high - lowest_low))
    k = k.rolling(window=smooth_k).mean()
    d = k.rolling(window=smooth_d).mean()
    return k, d

def calculate_atr(high, low, close, period=10):
    hl = high - low
    hc = (high - close.shift(1)).abs()
    lc = (low - close.shift(1)).abs()
    tr = pd.concat([hl, hc, lc], axis=1).max(axis=1)
    atr = [float('nan')] * len(tr)
    tr_vals = tr.tolist()
    sma = tr.rolling(window=period).mean().tolist()
    for i in range(len(tr_vals)):
        if pd.notna(sma[i]) and pd.isna(atr[i-1] if i > 0 else float('nan')):
            atr[i] = sma[i]
        elif i > 0 and pd.notna(atr[i-1]):
            atr[i] = (tr_vals[i] + (period - 1) * atr[i-1]) / period
    return pd.Series(atr, index=tr.index)

def calculate_supertrend(df, period=10, multiplier=3):
    high, low, close = df['High'].tolist(), df['Low'].tolist(), df['Close'].tolist()
    atr = calculate_atr(df['High'], df['Low'], df['Close'], period).tolist()
    n = len(df)
    basic_upper = [float('nan')] * n
    basic_lower = [float('nan')] * n
    for i in range(n):
        if pd.notna(atr[i]):
            hl2 = (high[i] + low[i]) / 2
            basic_upper[i] = hl2 + multiplier * atr[i]
            basic_lower[i] = hl2 - multiplier * atr[i]
    final_upper = [float('nan')] * n
    final_lower = [float('nan')] * n
    supertrend = [float('nan')] * n
    direction = [0] * n
    for i in range(1, n):
        if pd.isna(atr[i]):
            continue
        if pd.isna(final_upper[i-1]):
            final_upper[i] = basic_upper[i]; final_lower[i] = basic_lower[i]
            supertrend[i] = basic_upper[i]; direction[i] = -1
            continue
        final_upper[i] = basic_upper[i] if (basic_upper[i] < final_upper[i-1] or close[i-1] > final_upper[i-1]) else final_upper[i-1]
        final_lower[i] = basic_lower[i] if (basic_lower[i] > final_lower[i-1] or close[i-1] < final_lower[i-1]) else final_lower[i-1]
        if supertrend[i-1] == final_upper[i-1]:
            if close[i] > final_upper[i]: direction[i] = 1; supertrend[i] = final_lower[i]
            else: direction[i] = -1; supertrend[i] = final_upper[i]
        elif supertrend[i-1] == final_lower[i-1]:
            if close[i] < final_lower[i]: direction[i] = -1; supertrend[i] = final_upper[i]
            else: direction[i] = 1; supertrend[i] = final_lower[i]
    return pd.Series(supertrend, index=df.index), pd.Series(direction, index=df.index)

# =========================================================
# 殘差動能 (Residual Momentum)
# 滾動雙因子回歸剃除「大盤 + 類股」Beta，取純個股殘差 ε 計算動能。
# =========================================================
def sector_etf_for(fund: dict):
    """個股 → 類股因子 ETF。半導體業用 SOXX，其餘依 yfinance sector 對應 SPDR；
    無法對應 (含 ETF 本身) → None，退化為單因子 (只對 SPY 回歸)。"""
    if "semiconductor" in (fund.get("industry") or "").lower():
        return "SOXX"
    return YF_SECTOR_ETF.get(fund.get("sector"))

def _naive_daily_index(s: pd.Series) -> pd.Series:
    """tz-aware 日線索引 → 無時區日期，供跨標的對齊"""
    s = s.copy()
    if getattr(s.index, "tz", None) is not None:
        s.index = s.index.tz_localize(None)
    s.index = s.index.normalize()
    return s[~s.index.duplicated(keep="last")]

def _log_returns(close: pd.Series, calendar: pd.DatetimeIndex, label: str = "") -> np.ndarray:
    """對齊主日曆 (不 forward-fill) 後取對數報酬；|r|>1 視為資料異常設 NaN"""
    p = close.reindex(calendar)
    r = np.log(p / p.shift(1)).to_numpy()
    bad = np.abs(r) > 1.0
    if bad.any():
        print(f"    [Warn] {label} 有 {int(np.nansum(bad))} 筆 |r|>100% 異常報酬，已設 NaN")
        r[bad] = np.nan
    return r

def fetch_factor_closes(sector_etfs: set) -> dict:
    """抓 SPY + 所有需要的類股 ETF 還原收盤價"""
    out = {}
    for sym in [RM_MARKET_ETF] + sorted(sector_etfs):
        try:
            df = yf.Ticker(sym).history(period=RM_FETCH_PERIOD)
            if df is None or df.empty:
                continue
            out[sym] = _naive_daily_index(df["Close"].dropna())
        except Exception as e:
            print(f"    [Warn] 因子 {sym} 抓取失敗: {e}")
    return out

def compute_residual_momentum(stock_close: pd.Series, mkt_close: pd.Series, sec_close=None):
    """r_stock = α + β_mkt·r_SPY + β_sec·r_SECTOR + ε
    防 look-ahead: 第 t 日係數只用 [t-252, t-1] 估計。
    殘差刻意不扣 α̂ (保留特異性漂移)，勿改。"""
    mkt_close = mkt_close.dropna()
    cal = mkt_close.index  # 主日曆 = SPY 交易日
    r_y = _log_returns(_naive_daily_index(stock_close), cal, "stock")
    r_m = _log_returns(mkt_close, cal, RM_MARKET_ETF)
    r_s = _log_returns(sec_close, cal, "sector") if sec_close is not None else None
    n = len(cal)
    eps = np.full(n, np.nan)
    beta_m = np.full(n, np.nan)
    beta_s = np.full(n, np.nan)
    r2 = np.full(n, np.nan)
    for t in range(1, n):
        lo = max(0, t - RM_REG_WINDOW)
        yy, mm = r_y[lo:t], r_m[lo:t]
        mask = ~(np.isnan(yy) | np.isnan(mm))
        if r_s is not None:
            ss = r_s[lo:t]
            mask &= ~np.isnan(ss)
        if int(mask.sum()) < RM_MIN_WINDOW:
            continue
        cols = [np.ones(int(mask.sum())), mm[mask]]
        if r_s is not None:
            cols.append(ss[mask])
        X = np.column_stack(cols)
        yv = yy[mask]
        coef, *_ = np.linalg.lstsq(X, yv, rcond=None)
        res = yv - X @ coef
        ss_tot = float(((yv - yv.mean()) ** 2).sum())
        r2[t] = 1 - float((res ** 2).sum()) / ss_tot if ss_tot > 0 else np.nan
        beta_m[t] = coef[1]
        if r_s is not None:
            beta_s[t] = coef[2]
        if not (np.isnan(r_y[t]) or np.isnan(r_m[t]) or (r_s is not None and np.isnan(r_s[t]))):
            eps[t] = r_y[t] - coef[1] * r_m[t] - (coef[2] * r_s[t] if r_s is not None else 0.0)

    e = pd.Series(eps, index=cal)
    # 滾動計算: 視窗內剔 NaN，有效樣本低於視窗 70% → NaN
    rolling_alpha = e.rolling(RM_OSC_WINDOW, min_periods=int(math.ceil(RM_OSC_WINDOW * 0.7))).mean() * 252
    z_num = e.rolling(RM_Z_SHORT_WINDOW, min_periods=int(math.ceil(RM_Z_SHORT_WINDOW * 0.7))).sum()
    z_den = e.rolling(RM_Z_STD_WINDOW, min_periods=int(math.ceil(RM_Z_STD_WINDOW * 0.7))).std(ddof=1) * math.sqrt(RM_Z_SHORT_WINDOW)
    z_short = z_num / z_den

    # rMOM (12-1 月，IR 標準化): 形成期 [t-252, t-21]，分母 std×√N_eff (無因次 Z 值)
    rmom = float("nan")
    t = n - 1
    lo, hi = t - RM_MOM_FORM_START, t - RM_MOM_FORM_END
    if t >= 0 and lo >= 0:
        form = eps[lo:hi + 1]
        valid = form[~np.isnan(form)]
        if len(valid) >= RM_MOM_MIN_OBS:
            sd = float(valid.std(ddof=1))
            if sd > 0:
                rmom = float(valid.sum()) / (sd * math.sqrt(len(valid)))

    latest_r2 = float(r2[t]) if n else float("nan")
    z_last = float(z_short.iloc[-1]) if n and pd.notna(z_short.iloc[-1]) else float("nan")
    # 訊號分類 (依序判斷，先中先得)
    if math.isnan(rmom) or math.isnan(latest_r2) or latest_r2 < 0.20:
        signal = "no_signal"
    elif rmom >= 1.0 and z_last > 2.0:
        signal = "overheat"
    elif rmom >= 1.0 and z_last < -2.0:
        signal = "pullback"
    elif rmom >= 1.0:
        signal = "strong"
    elif rmom <= -1.0:
        signal = "weak"
    else:
        signal = "neutral"

    return {
        "dates": cal,
        "rolling_alpha": rolling_alpha,
        "z_short": z_short,
        "rmom": rmom,
        "z_last": z_last,
        "r2": latest_r2,
        "beta_mkt": float(beta_m[t]) if n and pd.notna(beta_m[t]) else float("nan"),
        "beta_sec": float(beta_s[t]) if n and pd.notna(beta_s[t]) else float("nan"),
        "signal": signal,
    }

# =========================================================
# 個股資料 (yfinance)
# =========================================================
def get_stock_data(ticker: str, period: str = RM_FETCH_PERIOD):  # 3y: 覆蓋殘差動能 252 日回歸視窗
    try:
        t = yf.Ticker(ticker)
        df = t.history(period=period)
        if df is None or df.empty:
            return None
        df = df[df["Close"] > 0].copy()
        df['SMA_20']  = calculate_sma(df['Close'], 20)
        df['SMA_60']  = calculate_sma(df['Close'], 60)
        df['SMA_200'] = calculate_sma(df['Close'], 200)
        df['RSI_14']  = calculate_rsi(df['Close'], 14)
        df['MACD'], df['MACD_Signal'] = calculate_macd(df['Close'])
        df['MACD_Hist'] = df['MACD'] - df['MACD_Signal']
        df['K'], df['D'] = calculate_stochastic(df['High'], df['Low'], df['Close'])
        df['ST'], df['ST_DIR'] = calculate_supertrend(df, 10, 3)
        df['Vol_MA20'] = df['Volume'].rolling(20).mean()
        df['High_252'] = df['Close'].rolling(min(252, len(df))).max()
        df['Low_252']  = df['Close'].rolling(min(252, len(df))).min()

        df90 = df.tail(120)
        latest = df.iloc[-1]
        prev = df.iloc[-2] if len(df) > 1 else latest

        def _f(v, d=0.0):
            return float(v) if pd.notna(v) else d

        try:
            info = t.info or {}
        except Exception:
            info = {}

        return {
            "ticker": ticker,
            "df": df90,
            "close_full": df["Close"].copy(),  # 完整還原收盤序列 (殘差動能回歸用)
            "latest": {"close": _f(latest["Close"]), "volume": int(latest["Volume"]) if pd.notna(latest["Volume"]) else 0,
                       "high": _f(latest["High"]), "low": _f(latest["Low"]), "open": _f(latest["Open"])},
            "prev": {"close": _f(prev["Close"])},
            "indicators": {
                "ma20": _f(latest.get("SMA_20")), "ma60": _f(latest.get("SMA_60")), "ma200": _f(latest.get("SMA_200")),
                "rsi": _f(latest.get("RSI_14"), 50), "macd": _f(latest.get("MACD")), "macd_signal": _f(latest.get("MACD_Signal")),
                "macd_hist": _f(latest.get("MACD_Hist")), "macd_hist_prev": _f(prev.get("MACD_Hist")),
                "supertrend": _f(latest.get("ST")), "supertrend_dir": int(latest.get("ST_DIR")) if pd.notna(latest.get("ST_DIR")) else 0,
                "vol_ma20": _f(latest.get("Vol_MA20")), "high_252": _f(latest.get("High_252")), "low_252": _f(latest.get("Low_252")),
            },
            "info": info,
        }
    except Exception as e:
        print(f"    [Warn] {ticker} 抓取失敗: {e}")
        return None

def get_fundamentals(info: dict):
    def g(*keys):
        for k in keys:
            v = info.get(k)
            if v is not None:
                return v
        return None
    return {
        "name": g("shortName", "longName") or "",
        "sector": g("sector") or "-",
        "industry": g("industry") or "",
        "trailing_pe": g("trailingPE"),
        "forward_pe": g("forwardPE"),
        "peg": g("trailingPegRatio", "pegRatio"),
        "eps_ttm": g("trailingEps", "epsTrailingTwelveMonths"),
        "roe": g("returnOnEquity"),
        "dividend_yield": g("dividendYield"),
        "target_price": g("targetMeanPrice", "targetMedianPrice"),
        "market_cap": g("marketCap"),
        "beta": g("beta"),
        "profit_margin": g("profitMargins"),
        "revenue_growth": g("revenueGrowth"),
        "inst_pct": g("heldPercentInstitutions"),
        "insider_pct": g("heldPercentInsiders"),
        "short_ratio": g("shortRatio"),
        "short_pct_float": g("shortPercentOfFloat"),
        "shares_short": g("sharesShort"),
        "shares_short_prior": g("sharesShortPriorMonth"),
    }

def compute_max_pain(calls: pd.DataFrame, puts: pd.DataFrame):
    try:
        strikes = sorted(set(calls["strike"].tolist()) | set(puts["strike"].tolist()))
        call_oi = {r["strike"]: (r["openInterest"] if pd.notna(r["openInterest"]) else 0) for _, r in calls.iterrows()}
        put_oi  = {r["strike"]: (r["openInterest"] if pd.notna(r["openInterest"]) else 0) for _, r in puts.iterrows()}
        best_strike, best_loss = None, None
        for p in strikes:
            loss = 0.0
            for s in strikes:
                if p > s: loss += (p - s) * call_oi.get(s, 0)
                if p < s: loss += (s - p) * put_oi.get(s, 0)
            if best_loss is None or loss < best_loss:
                best_loss, best_strike = loss, p
        return best_strike
    except Exception:
        return None

def get_options_data(ticker: str, current_price: float):
    try:
        t = yf.Ticker(ticker)
        exps = t.options
        if not exps:
            return None
        # 選擇最近且距今 > 3 天的到期日, 否則取第一個
        target = exps[0]
        today = now_et().date()
        for e in exps:
            try:
                d = datetime.strptime(e, "%Y-%m-%d").date()
                if (d - today).days >= 3:
                    target = e; break
            except Exception:
                continue
        chain = t.option_chain(target)
        calls, puts = chain.calls.copy(), chain.puts.copy()
        for dfo in (calls, puts):
            dfo["openInterest"] = pd.to_numeric(dfo["openInterest"], errors="coerce").fillna(0)
            dfo["volume"] = pd.to_numeric(dfo["volume"], errors="coerce").fillna(0)
            dfo["impliedVolatility"] = pd.to_numeric(dfo["impliedVolatility"], errors="coerce")

        call_vol, put_vol = calls["volume"].sum(), puts["volume"].sum()
        call_oi, put_oi = calls["openInterest"].sum(), puts["openInterest"].sum()
        pcr_vol = (put_vol / call_vol) if call_vol else 0
        pcr_oi = (put_oi / call_oi) if call_oi else 0

        # ATM 附近平均 IV
        near = pd.concat([
            calls.assign(_d=(calls["strike"] - current_price).abs()),
            puts.assign(_d=(puts["strike"] - current_price).abs()),
        ])
        near = near[near["_d"] <= current_price * 0.10]
        avg_iv = float(near["impliedVolatility"].mean()) if not near.empty and pd.notna(near["impliedVolatility"].mean()) else None

        max_pain = compute_max_pain(calls, puts)

        # OI by strike (取 current price 上下各約 12 檔)
        lo, hi = current_price * 0.80, current_price * 1.20
        c_oi = calls[(calls["strike"] >= lo) & (calls["strike"] <= hi)][["strike", "openInterest"]]
        p_oi = puts[(puts["strike"] >= lo) & (puts["strike"] <= hi)][["strike", "openInterest"]]
        strike_set = sorted(set(c_oi["strike"].tolist()) | set(p_oi["strike"].tolist()))
        c_map = {r["strike"]: int(r["openInterest"]) for _, r in c_oi.iterrows()}
        p_map = {r["strike"]: int(r["openInterest"]) for _, r in p_oi.iterrows()}
        oi_strikes = [round(float(s), 2) for s in strike_set]
        oi_calls = [c_map.get(s, 0) for s in strike_set]
        oi_puts = [p_map.get(s, 0) for s in strike_set]

        return {
            "expiry": target, "pcr_vol": round(pcr_vol, 2), "pcr_oi": round(pcr_oi, 2),
            "avg_iv": avg_iv, "max_pain": max_pain,
            "call_oi": int(call_oi), "put_oi": int(put_oi),
            "oi_strikes": oi_strikes, "oi_calls": oi_calls, "oi_puts": oi_puts,
        }
    except Exception as e:
        print(f"    [Warn] {ticker} 選擇權抓取失敗: {e}")
        return None

def get_news(ticker: str, limit: int = 5):
    try:
        t = yf.Ticker(ticker)
        news = t.news or []
        out = []
        for n in news[:limit]:
            content = n.get("content", n)
            title = content.get("title") or n.get("title", "")
            link = ""
            cu = content.get("canonicalUrl") or content.get("clickThroughUrl")
            if isinstance(cu, dict): link = cu.get("url", "")
            link = link or n.get("link", "#")
            pub = content.get("pubDate") or content.get("displayTime") or ""
            out.append({"title": title, "link": link, "date": str(pub)[:10]})
        return out
    except Exception:
        return []

# =========================================================
# 大盤 / 總經資料
# =========================================================
def get_index_series(symbol: str, period: str = "6mo"):
    try:
        df = yf.Ticker(symbol).history(period=period)
        if df is None or df.empty: return []
        df = df[df["Close"] > 0].copy()
        df["SMA_20"] = calculate_sma(df["Close"], 20)
        out = []
        for idx, row in df.tail(120).iterrows():
            sma = row["SMA_20"]
            out.append({"date": idx.strftime("%Y-%m-%d"), "open": float(row["Open"]), "high": float(row["High"]),
                        "low": float(row["Low"]), "close": float(row["Close"]), "volume": int(row["Volume"] or 0),
                        "ma20": round(float(sma), 2) if pd.notna(sma) else None})
        return out
    except Exception:
        return []

def get_quote(symbol: str):
    try:
        df = yf.Ticker(symbol).history(period="5d")
        if df is None or df.empty: return None
        c = float(df["Close"].iloc[-1])
        p = float(df["Close"].iloc[-2]) if len(df) > 1 else c
        return {"close": c, "prev": p, "change": c - p, "change_pct": ((c - p) / p * 100) if p else 0}
    except Exception:
        return None

def get_fear_greed():
    # CNN 已將 endpoint 從 production.fear-and-greed-cnn.com 遷移至 production.dataviz.cnn.io
    # 且對預設 UA 會回 403/418，需帶完整瀏覽器 headers (Origin/Referer)
    start = (now_et().date() - timedelta(days=400)).strftime("%Y-%m-%d")
    endpoints = [
        f"https://production.dataviz.cnn.io/index/fearandgreed/graphdata/{start}",  # 新版
        "https://production.fear-and-greed-cnn.com/graphdata",                       # 舊版備援
    ]
    headers = {
        "User-Agent": "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36 "
                      "(KHTML, like Gecko) Chrome/124.0.0.0 Safari/537.36",
        "Accept": "application/json, text/plain, */*",
        "Accept-Language": "en-US,en;q=0.9",
        "Origin": "https://www.cnn.com",
        "Referer": "https://www.cnn.com/",
    }
    for url in endpoints:
        try:
            r = requests.get(url, headers=headers, timeout=20)
            r.raise_for_status()
            j = r.json()
            fg = j.get("fear_and_greed", {})
            score = round(float(fg.get("score", 0)), 1)
            rating = fg.get("rating", "")
            hist = []
            for d in (j.get("fear_and_greed_historical", {}).get("data", []))[-400:]:
                try:
                    ts = int(d.get("x", 0)) / 1000
                    hist.append({"date": datetime.utcfromtimestamp(ts).strftime("%Y-%m-%d"),
                                 "score": round(float(d.get("y", 0)), 1)})
                except Exception:
                    continue
            if score or hist:
                print(f"  ✓ Fear & Greed: {score} ({rating}) via {url.split('/')[2]}")
                return {"score": score, "rating": rating, "history": hist,
                        "prev_close": round(float(fg.get("previous_close", 0)), 1)}
        except Exception as e:
            print(f"    [Warn] Fear & Greed {url.split('/')[2]} 失敗: {e}")
            continue
    return {"score": 0, "rating": "n/a", "history": [], "prev_close": 0}

def get_sector_performance():
    out = []
    for sym, name in SECTOR_ETFS.items():
        try:
            df = yf.Ticker(sym).history(period="3mo")
            if df is None or df.empty: continue
            closes = df["Close"].dropna()
            c = float(closes.iloc[-1])
            d1 = ((c / float(closes.iloc[-2])) - 1) * 100 if len(closes) > 1 else 0
            w1 = ((c / float(closes.iloc[-6])) - 1) * 100 if len(closes) > 6 else 0
            m1 = ((c / float(closes.iloc[-22])) - 1) * 100 if len(closes) > 22 else 0
            out.append({"sym": sym, "name": name, "d1": round(d1, 2), "w1": round(w1, 2), "m1": round(m1, 2)})
        except Exception:
            continue
    out.sort(key=lambda x: -x["m1"])
    return out

def get_sector_daily_history(days: int = 30):
    """近 N 個交易日各類股每日累積報酬 (%, 相對於 N 天前的起始值)"""
    series_dict = {}
    for sym, name in SECTOR_ETFS.items():
        try:
            df = yf.Ticker(sym).history(period="3mo")
            if df is None or df.empty: continue
            s = df["Close"].dropna()
            if not s.empty:
                series_dict[name] = s
        except Exception:
            continue
    if not series_dict:
        return []
    # 對齊所有類股的日期 (取交集)
    aligned = pd.concat(series_dict, axis=1).dropna()
    if len(aligned) < 2:
        return []
    # 取最後 days+1 筆 (多 1 筆作為 baseline)
    tail = aligned.tail(days + 1) if len(aligned) > days else aligned
    base = tail.iloc[0]  # 起始基準
    out = []
    for i in range(1, len(tail)):  # 從第 1 個開始 (第 0 個是 baseline)
        row = tail.iloc[i]
        dt = tail.index[i].strftime("%m-%d")
        values = {}
        for name in series_dict.keys():
            if name in row.index and pd.notna(row[name]) and pd.notna(base[name]) and base[name] > 0:
                values[name] = round(float(row[name] / base[name] - 1) * 100, 2)
        out.append({"date": dt, "values": values})
    return out

def get_yields():
    syms = {"^IRX": "13週", "^FVX": "5年", "^TNX": "10年", "^TYX": "30年"}
    out = []
    for sym, label in syms.items():
        q = get_quote(sym)
        if q:
            out.append({"label": label, "value": round(q["close"], 2), "change": round(q["change"], 3)})
    return out

def get_index_returns(symbol: str):
    """回傳指數 1日/5日/20日/YTD 漲幅 (%)"""
    try:
        df = yf.Ticker(symbol).history(period="1y")
        if df is None or df.empty:
            return None
        closes = df[df["Close"] > 0]["Close"].dropna()
        if closes.empty:
            return None
        c = float(closes.iloc[-1])

        def ret(n):
            return round(((c / float(closes.iloc[-1 - n])) - 1) * 100, 2) if len(closes) > n else None

        # YTD: 以去年最後一個交易日收盤為基準 (更精確)，否則用今年首個交易日
        this_year = closes.index[-1].year
        prev_year = closes[closes.index.year < this_year]
        ytd_base = closes[closes.index.year == this_year]
        if len(prev_year) > 0:
            ytd = round((c / float(prev_year.iloc[-1]) - 1) * 100, 2)
        elif len(ytd_base) > 0:
            ytd = round((c / float(ytd_base.iloc[0]) - 1) * 100, 2)
        else:
            ytd = None
        return {"close": c, "d1": ret(1), "d5": ret(5), "d20": ret(20), "ytd": ytd}
    except Exception as e:
        print(f"    [Warn] {symbol} 漲幅抓取失敗: {e}")
        return None

def get_index_full_series(symbol: str, period: str = "1y"):
    """完整指數序列 (K線 + 量 + MA20 + Supertrend + KD + MACD)，回傳近 120 筆字典清單"""
    try:
        df = yf.Ticker(symbol).history(period=period)
        if df is None or df.empty:
            return []
        df = df[df["Close"] > 0].copy()
        df["SMA_20"] = calculate_sma(df["Close"], 20)
        df["MACD"], df["MACD_Signal"] = calculate_macd(df["Close"])
        df["MACD_Hist"] = df["MACD"] - df["MACD_Signal"]
        df["K"], df["D"] = calculate_stochastic(df["High"], df["Low"], df["Close"])
        df["ST"], df["ST_DIR"] = calculate_supertrend(df, 10, 3)

        def _n(v, dec=2):
            return round(float(v), dec) if pd.notna(v) else None

        out = []
        for idx, r in df.tail(120).iterrows():
            out.append({
                "date": idx.strftime("%Y-%m-%d"),
                "open": _n(r["Open"]), "high": _n(r["High"]), "low": _n(r["Low"]), "close": _n(r["Close"]),
                "volume": int(r["Volume"]) if pd.notna(r["Volume"]) else 0,
                "ma20": _n(r.get("SMA_20")),
                "st": _n(r.get("ST")), "st_dir": int(r.get("ST_DIR")) if pd.notna(r.get("ST_DIR")) else 0,
                "k": _n(r.get("K")), "d": _n(r.get("D")),
                "macd": _n(r.get("MACD"), 3), "macd_sig": _n(r.get("MACD_Signal"), 3), "macd_hist": _n(r.get("MACD_Hist"), 3),
            })
        return out
    except Exception as e:
        print(f"    [Warn] {symbol} 完整序列抓取失敗: {e}")
        return []

def get_yield_curve_series(period: str = "6mo"):
    """美債 2/10/30 年殖利率歷史線圖資料 (對齊日期)"""
    # 2年期: Yahoo 無原生指數，改用 CBOT 2-Year Yield 期貨 2YY=F
    syms = [("2YY=F", "2年"), ("^TNX", "10年"), ("^TYX", "30年")]
    raw = {}
    for sym, label in syms:
        try:
            df = yf.Ticker(sym).history(period=period)
            if df is None or df.empty:
                continue
            s = df["Close"].dropna()
            raw[label] = {d.strftime("%Y-%m-%d"): round(float(v), 3) for d, v in s.items()}
        except Exception as e:
            print(f"    [Warn] 殖利率 {sym} 抓取失敗: {e}")
            continue
    if not raw:
        return {"dates": [], "series": {}}
    all_dates = sorted(set().union(*[set(v.keys()) for v in raw.values()]))
    series = {label: [raw[label].get(dt) for dt in all_dates] for label in raw}
    return {"dates": [d[-5:] for d in all_dates], "series": series}

def build_macro_axis(md):
    """建立總經副圖 (VIX / CNN F&G / 10Y / DXY) 共用的日期軸。
    以 VIX/10Y/DXY 的交易日聯集為主軸，再把各指標對齊到同一時間軸；
    某日無值則填 None (圖上顯示空白)，使各圖時間完全對齊。"""
    def _map(series, key):
        return {d["date"]: d.get(key) for d in (series or []) if d.get("date")}
    vix_c = _map(md.get("vix_series"), "close")
    tnx_c = _map(md.get("tnx_series"), "close")
    dxy_c = _map(md.get("dxy_series"), "close")
    dxy_ma = _map(md.get("dxy_series"), "ma20")
    fg_map = {d["date"]: d.get("score")
              for d in md.get("fear_greed", {}).get("history", []) if d.get("date")}
    trade_dates = sorted(set(vix_c) | set(tnx_c) | set(dxy_c))
    if not trade_dates:
        return {}

    def col(mp, dec):
        out = []
        for d in trade_dates:
            v = mp.get(d)
            out.append(round(float(v), dec) if v is not None else None)
        return out

    return {
        "dates": [d[-5:] for d in trade_dates],
        "full_dates": trade_dates,
        "vix": col(vix_c, 2),
        "tnx": col(tnx_c, 3),
        "dxy": col(dxy_c, 2),
        "dxy_ma20": col(dxy_ma, 2),
        # CNN F&G：對齊到交易日軸，缺值留空白 (None)
        "fg": [fg_map.get(d) for d in trade_dates],
    }

def get_market_overview():
    md = {}
    print("  抓取指數漲幅 (道瓊/S&P/那斯達克/費半)...")
    md["index_returns"] = {
        "道瓊": get_index_returns("^DJI"),
        "S&P 500": get_index_returns("^GSPC"),
        "那斯達克": get_index_returns("^IXIC"),
        "費城半導體": get_index_returns("^SOX"),
    }
    md["spx"] = get_index_series("^GSPC")
    md["indices"] = {
        "S&P 500": get_quote("^GSPC"),
        "Nasdaq": get_quote("^IXIC"),
        "道瓊": get_quote("^DJI"),
        "VIX": get_quote("^VIX"),
    }
    print("  抓取那斯達克 / 費半 K線...")
    md["nasdaq_series"] = get_index_full_series("^IXIC")
    md["sox_series"] = get_index_full_series("^SOX")
    md["vix_series"] = get_index_series("^VIX")
    md["vix_quote"] = get_quote("^VIX")
    md["fear_greed"] = get_fear_greed()
    print("  抓取美債殖利率 / DXY...")
    md["yield_curve"] = get_yield_curve_series()
    md["dxy_series"] = get_index_series("DX-Y.NYB")
    md["dxy_quote"] = get_quote("DX-Y.NYB")
    md["tnx_series"] = get_index_series("^TNX")  # 10Y 殖利率歷史 (宏觀情緒對比用)
    md["sectors"] = get_sector_performance()
    md["sectors_daily"] = get_sector_daily_history(30)
    md["yields"] = get_yields()
    # 總經副圖共用日期軸 (VIX / F&G / 10Y / DXY 對齊時間)
    md["macro_axis"] = build_macro_axis(md)
    return md

# =========================================================
# AI 分析
# =========================================================
def _call_claude(prompt: str) -> str:
    import anthropic
    client = anthropic.Anthropic(api_key=ANTHROPIC_API_KEY)
    return client.messages.create(model=CLAUDE_MODEL, max_tokens=1000, messages=[{"role": "user", "content": prompt}]).content[0].text

def _call_gemini(prompt: str) -> str:
    from google import genai
    from google.genai import types
    client = genai.Client(api_key=GEMINI_API_KEY)
    return client.models.generate_content(model=GEMINI_MODEL, contents=prompt, config=types.GenerateContentConfig(max_output_tokens=4096, temperature=0.7)).text

def generate_ai_analysis(ticker, name, data, fund, opt):
    global _gemini_quota_exhausted
    if AI_PROVIDER == "gemini":
        if not GEMINI_API_KEY: return "未設定 API", "未設定 API"
        if _gemini_quota_exhausted: return "配額用完", "配額用完"
        import time; time.sleep(4)
    else:
        if not ANTHROPIC_API_KEY: return "未設定 API", "未設定 API"

    latest, ind = data["latest"], data["indicators"]
    def pct(v): return f"{v*100:.1f}%" if v is not None else "n/a"
    opt_str = "n/a"
    if opt:
        opt_str = f"Put/Call(OI) {opt.get('pcr_oi','n/a')}, IV {pct(opt.get('avg_iv'))}, Max Pain ${opt.get('max_pain','n/a')}"
    prompt = f"""Analyze {ticker} ({name}) for a swing trader. Respond in 繁體中文.

Technical:
- Price: {latest['close']:.2f} | MA20/60/200: {ind['ma20']:.2f}/{ind['ma60']:.2f}/{ind['ma200']:.2f}
- RSI: {ind['rsi']:.1f} | MACD hist: {ind['macd_hist']:+.3f} | Supertrend: {'up' if ind['supertrend_dir']==1 else 'down'}
- 52W high/low: {ind['high_252']:.2f}/{ind['low_252']:.2f}

Fundamentals / Positioning:
- Fwd PE: {fund.get('forward_pe')} | PEG: {fund.get('peg')} | Target: {fund.get('target_price')}
- Institutional held: {pct(fund.get('inst_pct'))} | Short % float: {pct(fund.get('short_pct_float'))}
- Options: {opt_str}

請嚴格用以下格式輸出, 不要多餘說明:
=== 技術面 ===
[60字內]
=== 操作建議 ===
[60字內]"""
    try:
        text = _call_gemini(prompt) if AI_PROVIDER == "gemini" else _call_claude(prompt)
        sections = {"技術面": "", "操作建議": ""}
        cur = None
        for line in text.split("\n"):
            s = line.strip()
            if "===" in s:
                for k in sections:
                    if k in s: cur = k; break
            elif cur and s:
                sections[cur] += line + "\n"
        return sections["技術面"].strip() or text, sections["操作建議"].strip()
    except Exception as e:
        if AI_PROVIDER == "gemini" and ("429" in str(e) or "quota" in str(e).lower()):
            _gemini_quota_exhausted = True
        return "分析失敗", "分析失敗"

# =========================================================
# 評等計算
# =========================================================
def calculate_rating(data, fund, opt):
    ind, latest = data["indicators"], data["latest"]
    close = latest.get("close", 0)
    tech = 0.0
    if close > ind.get("ma60", 0) > 0: tech += 1.5
    if ind.get("ma60", 0) > ind.get("ma200", 0) > 0: tech += 2
    if ind.get("high_252", 0) > 0 and close >= ind.get("high_252", 0) * 0.95: tech += 1.5
    if latest.get("volume", 0) > ind.get("vol_ma20", 0) > 0: tech += 1
    if ind.get("macd_hist", 0) > 0: tech += 2
    rsi = ind.get("rsi", 50)
    if 45 <= rsi <= 70: tech += 2
    elif 40 <= rsi < 45 or 70 < rsi <= 78: tech += 1

    chip = 0.0
    inst_pct = fund.get("inst_pct") or 0
    if inst_pct >= 0.6: chip += 2
    elif inst_pct >= 0.4: chip += 1
    ss, ssp = fund.get("shares_short"), fund.get("shares_short_prior")
    if ss is not None and ssp is not None and ssp > 0 and ss < ssp: chip += 2
    spf = fund.get("short_pct_float")
    if spf is not None and spf < 0.05: chip += 1.5
    if opt:
        pcr = opt.get("pcr_oi", 1)
        if pcr and pcr < 0.7: chip += 1.5
        elif pcr and pcr < 1.0: chip += 0.75
    tp, fp = fund.get("target_price"), fund.get("forward_pe")
    if tp and close and tp > close * 1.05: chip += 1.5
    if fp and fund.get("trailing_pe") and 0 < fp < fund.get("trailing_pe"): chip += 1.5

    total = tech + chip
    if total >= 14: rating, rk = "強力買進", "sb"
    elif total >= 10: rating, rk = "買進", "b"
    elif total >= 6: rating, rk = "中性", "n"
    elif total >= 3: rating, rk = "減碼", "s"
    else: rating, rk = "賣出", "ss"
    return {"tech": round(tech, 1), "chip": round(chip, 1), "total": round(total, 1), "rating": rating, "rating_key": rk}

# =========================================================
# 前端 (深色終端, 綠漲紅跌)
# =========================================================
THEME = {
    "up": "#22d39a", "down": "#ff525b", "ma20": "#e0a83c", "ma50": "#4d7fff", "ma200": "#b07bff",
    "axis_label": "#8b95a5", "axis_line": "#2a323e", "split_line": "#171e29",
    "title": "#8b95a5", "legend": "#8b95a5",
    "tooltip_bg": "rgba(10,14,21,.95)", "tooltip_border": "#1e2632", "tooltip_text": "#dde3ec",
    "dz_border": "#1e2632", "dz_filler": "rgba(77,127,255,.18)", "dz_handle": "#4d7fff",
    "dz_text": "#8b95a5", "dz_bg_line": "#2a323e", "dz_bg_area": "#151b24",
    "call": "#22d39a", "put": "#ff525b", "vix": "#e0a83c", "rsi": "#6f9bff", "neutral": "#8b95a5",
}

def fmt_num(v, suffix="", dec=2):
    if v is None or (isinstance(v, float) and pd.isna(v)): return "-"
    try: return f"{float(v):,.{dec}f}{suffix}"
    except Exception: return "-"

def fmt_pct(v, dec=2):
    if v is None or (isinstance(v, float) and pd.isna(v)): return "-"
    try: return f"{float(v)*100:.{dec}f}%"
    except Exception: return "-"

def fmt_cap(v):
    if not v: return "-"
    v = float(v)
    if v >= 1e12: return f"${v/1e12:.2f}T"
    if v >= 1e9: return f"${v/1e9:.2f}B"
    if v >= 1e6: return f"${v/1e6:.2f}M"
    return f"${v:,.0f}"

def generate_rating_table(stocks_data: dict) -> str:
    groups = {"sb": {"label": "強力買進", "stocks": []}, "b": {"label": "買進", "stocks": []},
              "n": {"label": "中性", "stocks": []}, "s": {"label": "減碼", "stocks": []}, "ss": {"label": "賣出", "stocks": []}}
    for tk, data in stocks_data.items():
        r = data["rating"]; key = r.get("rating_key", "n")
        if key in groups:
            groups[key]["stocks"].append({"ticker": tk, "name": data.get("name", ""), "change": data.get("change_pct", 0),
                                          "tech": r.get("tech", 0), "chip": r.get("chip", 0), "total": r.get("total", 0)})
    for g in groups.values():
        g["stocks"].sort(key=lambda s: -s["total"])
    cols = ""
    for key, g in groups.items():
        chips = ""
        for s in g["stocks"]:
            cls = "up" if s["change"] >= 0 else "down"
            sign = "+" if s["change"] >= 0 else ""
            chips += f'<div class="schip"><div class="schip-top"><span class="schip-name">{s["ticker"]} · {s["name"][:14]}</span><span class="schip-chg {cls}">{sign}{s["change"]:.2f}%</span></div><div class="schip-meta"><span class="tag t">技 {s["tech"]:g}</span><span class="tag c">籌 {s["chip"]:g}</span></div></div>'
        if not g["stocks"]: chips = '<div class="empty">無</div>'
        cols += f'<div class="rcol" data-k="{key}"><div class="rcol-head"><span class="rcol-label">{g["label"]}</span><span class="rcol-count">{len(g["stocks"])}</span></div>{chips}</div>'
    return f"""<div class="rating-wrap">
  <div class="rating-top"><h3>個股操作建議 · 綜合評等</h3><span class="upd">更新於 {now_et().strftime("%Y-%m-%d %H:%M")} ET</span></div>
  <div class="rating-grid">{cols}</div>
  <details class="legend"><summary>評分邏輯與門檻</summary><div class="legend-body">
    <div class="legend-row"><span class="lbadge t">技術 10</span><span>站季線 +1.5 · 季&gt;年(黃金交叉) +2 · 逼近52週高 +1.5 · 量增 +1 · MACD 多頭 +2 · RSI 健康 +2</span></div>
    <div class="legend-row"><span class="lbadge c">籌碼 10</span><span>法人持股高 +2 · 空單減少 +2 · 低放空比 +1.5 · Put/Call偏多 +1.5 · 目標價有上檔 +1.5 · 預估PE改善 +1.5</span></div>
    <div class="legend-row"><span class="lbadge tt">總分</span><span>≥14 強力買進 · 10–13 買進 · 6–9 中性 · 3–5 減碼 · ≤2 賣出</span></div>
  </div></details>
</div>"""

def generate_stock_card(ticker: str, data: dict, opt: dict, fund: dict) -> str:
    latest, ind, r = data["latest"], data["indicators"], data["rating"]
    cpct = data.get("change_pct", 0)
    c_cls = "up" if cpct >= 0 else "down"
    c_sign = "+" if cpct >= 0 else ""
    change_str = f"{c_sign}{cpct:.2f}%"
    close_price = latest.get("close", 0)
    rk = r.get("rating_key", "n")

    tp = fund.get("target_price")
    upside = ((tp / close_price - 1) * 100) if (tp and close_price) else None
    target_str = f"${tp:,.2f}" + (f" ({upside:+.1f}%)" if upside is not None else "") if tp else "-"

    fund_strip = f"""<div class="fund-strip">
      <div class="fund-cell"><div class="k">市值</div><div class="v">{fmt_cap(fund.get('market_cap'))}</div></div>
      <div class="fund-cell"><div class="k">本益比 TTM</div><div class="v">{fmt_num(fund.get('trailing_pe'))}</div></div>
      <div class="fund-cell"><div class="k">預估 PE</div><div class="v">{fmt_num(fund.get('forward_pe'))}</div></div>
      <div class="fund-cell"><div class="k">PEG</div><div class="v">{fmt_num(fund.get('peg'))}</div></div>
      <div class="fund-cell"><div class="k">EPS TTM</div><div class="v">{fmt_num(fund.get('eps_ttm'))}</div></div>
      <div class="fund-cell"><div class="k">Beta</div><div class="v">{fmt_num(fund.get('beta'))}</div></div>
    </div>"""

    # 法人 / 放空 表
    ss, ssp = fund.get("shares_short"), fund.get("shares_short_prior")
    short_trend = "-"
    if ss is not None and ssp is not None and ssp > 0:
        dd = (ss - ssp) / ssp * 100
        st_cls = "down" if dd > 0 else "up"  # 空單增加=利空(紅)
        short_trend = f'<span class="{st_cls}">{dd:+.1f}%</span>'

    chip_table = f"""<table class="dtable">
      <tr><th>項目</th><th>數值</th></tr>
      <tr><td>法人持股比例</td><td class="num">{fmt_pct(fund.get('inst_pct'))}</td></tr>
      <tr><td>內部人持股</td><td class="num">{fmt_pct(fund.get('insider_pct'))}</td></tr>
      <tr><td>放空比例(% Float)</td><td class="num">{fmt_pct(fund.get('short_pct_float'))}</td></tr>
      <tr><td>Short Ratio(回補天數)</td><td class="num">{fmt_num(fund.get('short_ratio'),'',1)}</td></tr>
      <tr><td>空單張數</td><td class="num">{f'{int(ss):,}' if ss else '-'}</td></tr>
      <tr><td>空單月變化</td><td class="num">{short_trend}</td></tr>
    </table>"""

    # 選擇權
    if opt:
        def pcr_cls(v): return "up" if (v and v < 1) else ("down" if v else "")
        opt_block = f"""<div class="opt-grid">
          <div class="opt-cell"><div class="k">到期日</div><div class="v">{opt.get('expiry','-')}</div></div>
          <div class="opt-cell"><div class="k">Put/Call (量)</div><div class="v {pcr_cls(opt.get('pcr_vol'))}">{fmt_num(opt.get('pcr_vol'),'',2)}</div></div>
          <div class="opt-cell"><div class="k">Put/Call (OI)</div><div class="v {pcr_cls(opt.get('pcr_oi'))}">{fmt_num(opt.get('pcr_oi'),'',2)}</div></div>
          <div class="opt-cell"><div class="k">隱含波動 IV</div><div class="v">{fmt_pct(opt.get('avg_iv'))}</div></div>
          <div class="opt-cell"><div class="k">Max Pain</div><div class="v">${fmt_num(opt.get('max_pain'),'',2)}</div></div>
          <div class="opt-cell"><div class="k">Call/Put OI</div><div class="v"><span class="up">{opt.get('call_oi',0):,}</span> / <span class="down">{opt.get('put_oi',0):,}</span></div></div>
        </div>
        <div id="opt_{ticker}" class="chart-box" style="height:300px;margin-top:12px"></div>"""
    else:
        opt_block = '<div style="color:var(--ink-3);font-size:12px;padding:10px">查無選擇權資料</div>'

    news_html = ""
    for n in data.get("news", [])[:5]:
        news_html += f'<div class="news-item"><div class="d">{n.get("date","")}</div><a href="{n.get("link","#")}" target="_blank">{n.get("title","")}</a></div>'
    if not news_html: news_html = "<div class='news-item' style='color:var(--ink-3)'>近期無相關新聞</div>"

    return f"""
    <div class="stock-card {'active' if data.get('_first') else ''}" id="card_{ticker}">
      <div class="sc-body">
        <div class="sc-header" onclick="toggleCard('{ticker}')">
          <span class="chevron mobile-only" id="chev_{ticker}">▶</span>
          <div><div class="sc-id">{ticker} · {fund.get('sector','')}</div>
            <div class="sc-name">{data.get('name','')[:24]} <span class="sc-price {c_cls}">${close_price:,.2f} <span style="font-size:13px">({change_str})</span></span></div></div>
          <div class="sc-meta">
            <div class="block"><div class="k">分析師目標價</div><div class="v target">{target_str}</div></div>
            <div class="block"><div class="k">綜合評等</div><div class="v"><span class="rbadge {rk}">★ {r.get('rating','')}</span></div></div>
            <div class="block"><div class="k">技 / 籌</div><div class="v num">{r.get('tech',0):g} / {r.get('chip',0):g}</div></div>
          </div>
        </div>

        <div class="sc-detail">
          {fund_strip}
          <div id="kline_{ticker}" class="chart-box" style="height:760px;"></div>

          <details class="fold" open>
            <summary>選擇權分析 (Put/Call · IV · Max Pain)</summary>
            <div style="padding:14px">{opt_block}</div>
          </details>

          <details class="fold" open>
            <summary>法人持股與放空</summary>
            <div style="padding:14px">{chip_table}</div>
          </details>

          <details class="fold" open>
            <summary>AI 分析與建議</summary>
            <div class="ai-grid" style="padding:14px">
              <div class="ai-box"><div class="ai-head"><span class="ai-mono">AI</span>技術面</div>
                <div class="ai-text">{data.get('ai_tech','').replace(chr(10),'<br>')}</div></div>
              <div class="ai-box oper"><div class="ai-head"><span class="ai-mono">AI</span>操作建議</div>
                <div class="ai-text">{data.get('ai_oper','').replace(chr(10),'<br>')}</div></div>
            </div>
          </details>

          <details class="news"><summary><span>近期相關新聞</span><span style="font-size:11.5px;color:var(--ink-3);font-weight:500">▾</span></summary>
            <div class="news-list">{news_html}</div></details>
        </div>
      </div>
    </div>"""

def generate_market_section(md: dict):
    idx_ret = md.get("index_returns", {})
    fg = md.get("fear_greed", {})

    def ret_cell(v):
        if v is None:
            return '<div class="rc"><div class="rcv num">-</div></div>'
        cls = "up" if v >= 0 else "down"
        return f'<div class="rc"><div class="rcv num {cls}">{v:+.2f}%</div></div>'

    def index_return_card(name, r, accent=False):
        if not r:
            return f'<div class="metric"><div class="label">{name}</div><div class="value num">-</div></div>'
        d1 = r.get("d1")
        cls = "up" if (d1 is not None and d1 >= 0) else "down"
        extra = "accent" if accent else cls
        cells = "".join([
            f'<div class="rc-head"><div class="rcl">1日</div>{ret_cell(r.get("d1"))}</div>',
            f'<div class="rc-head"><div class="rcl">5日</div>{ret_cell(r.get("d5"))}</div>',
            f'<div class="rc-head"><div class="rcl">20日</div>{ret_cell(r.get("d20"))}</div>',
            f'<div class="rc-head"><div class="rcl">YTD</div>{ret_cell(r.get("ytd"))}</div>',
        ])
        return f'''<div class="metric {extra}">
          <div class="label">{name}</div>
          <div class="value num {cls}">{r["close"]:,.2f}</div>
          <div class="ret-row">{cells}</div>
        </div>'''

    metrics = (index_return_card("道瓊", idx_ret.get("道瓊"))
               + index_return_card("S&P 500", idx_ret.get("S&P 500"), accent=True)
               + index_return_card("那斯達克", idx_ret.get("那斯達克"))
               + index_return_card("費城半導體", idx_ret.get("費城半導體")))

    # VIX 即時數值 (高 VIX = 恐慌 = 紅)
    vq = md.get("vix_quote")
    vix_badge = ""
    if vq:
        vcls = "down" if vq["change"] >= 0 else "up"
        arrow = "▲" if vq["change"] >= 0 else "▼"
        vix_badge = f'<span class="vix-now num">{vq["close"]:.2f} <span class="{vcls}">{arrow} {vq["change"]:+.2f}</span></span>'

    # 即時殖利率快照
    yields_html = ""
    for y in md.get("yields", []):
        ycls = "up" if y["change"] >= 0 else "down"
        yields_html += f'<div class="yield-cell"><div class="yl">{y["label"]}</div><div class="yv num">{y["value"]:.2f}%</div><div class="yc num {ycls}">{y["change"]:+.3f}</div></div>'

    # DXY 即時數值
    dq = md.get("dxy_quote")
    dxy_badge = ""
    if dq:
        dcls = "up" if dq["change"] >= 0 else "down"
        arrow = "▲" if dq["change"] >= 0 else "▼"
        dxy_badge = f'<span class="vix-now num">{dq["close"]:.2f} <span class="{dcls}">{arrow} {dq["change"]:+.2f} ({dq["change_pct"]:+.2f}%)</span></span>'

    return f"""<div class="section-head"><span class="eyebrow">US Market</span><h2>大盤總覽</h2></div>
    <div class="metrics metrics-4">{metrics}</div>

    <div class="ctrl">
      <span class="ctrl-lbl">指數</span>
      <div class="seg" id="seg">
        <button class="on" data-idx="ndx" onclick="switchIndex(this)">那斯達克</button>
        <button data-idx="sox" onclick="switchIndex(this)">費城半導體</button>
      </div>
      <span class="sep"></span>
      <span class="ctrl-lbl">指標</span>
      <div class="chips" id="chips">
        <span class="chip on" data-k="kd" onclick="toggleChip(this)"><span class="dot"></span>KD</span>
        <span class="chip on" data-k="macd" onclick="toggleChip(this)"><span class="dot"></span>MACD</span>
        <span class="chip on" data-k="vix" onclick="toggleChip(this)"><span class="dot"></span>VIX</span>
        <span class="chip" data-k="fg" onclick="toggleChip(this)"><span class="dot"></span>CNN F&amp;G</span>
        <span class="chip" data-k="tnx" onclick="toggleChip(this)"><span class="dot"></span>10Y</span>
        <span class="chip" data-k="dxy" onclick="toggleChip(this)"><span class="dot"></span>DXY</span>
      </div>
      <span class="cnt" id="cnt">3 個</span>
    </div>

    <div class="stack">
      <div class="pane index-card" data-idx="ndx">
        <div id="nasdaq_chart" class="chart-box" style="height:300px;"></div>
      </div>
      <div class="pane index-card" data-idx="sox" style="display:none">
        <div id="sox_chart" class="chart-box" style="height:300px;"></div>
      </div>

      <div class="pane sub-panel kd-panel index-sub" data-idx="ndx">
        <div id="kd_ndx_chart" class="chart-box" style="height:115px;"></div>
      </div>
      <div class="pane sub-panel kd-panel index-sub" data-idx="sox" style="display:none">
        <div id="kd_sox_chart" class="chart-box" style="height:115px;"></div>
      </div>

      <div class="pane sub-panel macd-panel index-sub" data-idx="ndx">
        <div id="nasdaq_macd" class="chart-box" style="height:115px;"></div>
      </div>
      <div class="pane sub-panel macd-panel index-sub" data-idx="sox" style="display:none">
        <div id="sox_macd" class="chart-box" style="height:115px;"></div>
      </div>

      <div class="pane sub-panel vix-panel">
        <div id="vix_chart" class="chart-box" style="height:115px;"></div>
      </div>

      <div class="pane sub-panel fg-panel" style="display:none">
        <div id="fg_chart" class="chart-box" style="height:115px;"></div>
      </div>

      <div class="pane sub-panel tnx-panel" style="display:none">
        <div id="tnx_chart" class="chart-box" style="height:115px;"></div>
      </div>

      <div class="pane sub-panel dxy-panel" style="display:none">
        <div id="dxy_chart" class="chart-box" style="height:115px;"></div>
      </div>
    </div>

    <div class="card"><div class="card-title"><span>美債殖利率走勢 · 2 / 10 / 30 年</span></div>
      <div id="yield_chart" class="chart-box" style="height:340px;"></div></div>

    <div class="card"><div class="card-title"><span>類股輪動表現</span>
        <div class="sector-ctrl">
          <button class="sector-btn" data-period="d1" onclick="switchSectorPeriod(this)">1日</button>
          <button class="sector-btn" data-period="w1" onclick="switchSectorPeriod(this)">5日</button>
          <button class="sector-btn on" data-period="m1" onclick="switchSectorPeriod(this)">1月</button>
          <button class="sector-btn" data-period="trend" onclick="switchSectorPeriod(this)">📈 近30日走勢</button>
          <span class="sector-sep"></span>
          <button class="sector-btn sector-play" id="sectorPlayBtn" onclick="toggleSectorPlay(this)">▶ 播放</button>
        </div>
      </div>
      <div id="sector_chart" class="chart-box" style="height:380px;"></div></div>"""

# =========================================================
# 圖表腳本
# =========================================================
def _index_kline_script(div_id: str, var: str, series: list, T: dict) -> str:
    """產生指數 K線圖 script: K線 + MA20 + 成交量 + Supertrend (主圖 only, KD/MACD 獨立)"""
    if not series:
        return f"""
var {var} = echarts.init(document.getElementById('{div_id}'));
{var}.setOption({{ title: {{ text: '查無資料', left: 'center', top: 'center', textStyle: {{color: '{T["axis_label"]}', fontSize: 13}} }} }});
"""
    dates = [d["date"][-5:] for d in series]
    ohlc = [[d["open"], d["close"], d["low"], d["high"]] for d in series]
    vol = [d["volume"] for d in series]
    vol_color = [T["up"] if d["close"] >= d["open"] else T["down"] for d in series]
    ma20 = [d["ma20"] for d in series]
    has_vol = any(v and v > 0 for v in vol)

    # Supertrend 拆多空兩條 (切換點補值避免斷裂)
    st_up, st_dn = [], []
    for i, d in enumerate(series):
        cur, v = d["st_dir"], d["st"]
        prev = series[i - 1]["st_dir"] if i > 0 else cur
        if cur == 1:
            st_up.append(v); st_dn.append(v if prev == -1 else None)
        elif cur == -1:
            st_dn.append(v); st_up.append(v if prev == 1 else None)
        else:
            st_up.append(None); st_dn.append(None)

    vol_series = (f""",
    {{ name: '成交量', type: 'bar', yAxisIndex: 1, data: {json.dumps(vol)}, itemStyle: {{color: function(p){{return {json.dumps(vol_color)}[p.dataIndex];}}}} }}""" if has_vol else "")

    return f"""
var {var} = echarts.init(document.getElementById('{div_id}'));
{var}.group = 'market';
{var}.setOption({{
  title: [{{ text: 'K線 · MA20 · Supertrend', left: '6%', top: '1%', textStyle: {{fontSize: 12, color: '{T["title"]}'}} }}],
  legend: {{ data: ['MA20','Supertrend↑','Supertrend↓'], top: '1%', right: '6%', textStyle: {{fontSize: 10, color: '{T["legend"]}'}}, itemWidth: 12, itemHeight: 8 }},
  tooltip: {{ trigger: 'axis', axisPointer: {{ type: 'cross', lineStyle: {{color: '#3a4658'}}, crossStyle: {{color: '#3a4658'}} }},
    backgroundColor: '{T["tooltip_bg"]}', borderColor: '{T["tooltip_border"]}', borderWidth: 1,
    textStyle: {{color: '{T["tooltip_text"]}', fontSize: 12, fontFamily: 'IBM Plex Mono'}} }},
  grid: [{{ left: '6%', right: '6%', top: '8%', bottom: '14%' }}],
  xAxis: [{{ type: 'category', data: {json.dumps(dates)}, boundaryGap: true, axisLabel: {{show: true, fontSize: 10, color: '{T["axis_label"]}'}}, axisLine: {{lineStyle: {{color: '{T["axis_line"]}'}}}} }}],
  yAxis: [
    {{ scale: true, splitNumber: 5, splitArea: {{show: false}}, splitLine: {{lineStyle: {{color: '{T["split_line"]}'}}}}, axisLabel: {{fontSize: 9, color: '{T["axis_label"]}', formatter: function(v){{return v.toFixed(0);}}}} }},
    {{ scale: true, show: false, max: function(v){{return Math.max(v.max*6,1);}} }}
  ],
  dataZoom: [
    {{ type: 'inside', start: 40, end: 100 }},
    {{ show: true, type: 'slider', bottom: 4, height: 14, start: 40, end: 100, borderColor: '{T["dz_border"]}', fillerColor: '{T["dz_filler"]}', handleStyle: {{color: '{T["dz_handle"]}'}}, textStyle: {{color: '{T["dz_text"]}'}}, dataBackground: {{lineStyle: {{color: '{T["dz_bg_line"]}'}}, areaStyle: {{color: '{T["dz_bg_area"]}'}}}} }}
  ],
  series: [
    {{ name: 'K線', type: 'candlestick', yAxisIndex: 0, data: {json.dumps(ohlc)}, itemStyle: {{color: '{T["up"]}', color0: '{T["down"]}', borderColor: '{T["up"]}', borderColor0: '{T["down"]}'}} }},
    {{ name: 'MA20', type: 'line', yAxisIndex: 0, data: {json.dumps(ma20)}, smooth: true, showSymbol: false, lineStyle: {{width: 1, color: '{T["ma20"]}'}} }},
    {{ name: 'Supertrend↑', type: 'line', yAxisIndex: 0, data: {json.dumps(st_up)}, connectNulls: false, showSymbol: false, lineStyle: {{width: 2, color: '{T["up"]}'}} }},
    {{ name: 'Supertrend↓', type: 'line', yAxisIndex: 0, data: {json.dumps(st_dn)}, connectNulls: false, showSymbol: false, lineStyle: {{width: 2, color: '{T["down"]}'}} }}{vol_series}
  ]
}});
window.addEventListener('resize', function(){{ {var}.resize(); }});
"""

def generate_chart_scripts(stocks_data, options_data, md, trade_markers=None):
    T = THEME
    trade_markers = trade_markers or {}
    scripts = []

    for tk, data in stocks_data.items():
        df = data["df"]
        dates = [d.strftime("%m-%d") for d in df.index]

        # 進出標記 (買/賣) — 對齊到 K 線交易日
        mk_data = []
        if tk in trade_markers:
            idx_dates = [d.date() if hasattr(d, "date") else d for d in df.index]
            for m in sorted(trade_markers[tk], key=lambda x: x["et_date"]):
                td = m["et_date"]
                label = None
                if td in idx_dates:
                    label = dates[idx_dates.index(td)]
                else:  # 非交易日 / 盤後夜盤 → 貼近最近一個已存在的交易日
                    prior = [i for i, dd in enumerate(idx_dates) if dd <= td]
                    if prior:
                        label = dates[prior[-1]]
                    elif idx_dates:
                        label = dates[0]
                if label is None:
                    continue
                is_buy = m["side"] == "BUY"
                mk_data.append({
                    "coord": [label, m["price"]],
                    "lab": "B" if is_buy else "S",
                    "side": "買進" if is_buy else "賣出",
                    "price": m["price"],
                    "size": m["size"],
                    "itemStyle": {"color": T["up"] if is_buy else T["down"],
                                  "borderColor": "#000", "borderWidth": 1},
                    # 買標籤貼近、賣標籤抬高 → B/S 不重疊
                    "label": {"position": "top", "distance": 8 if is_buy else 24},
                })
        mk_json = json.dumps(mk_data, ensure_ascii=False)
        ohlc = [[float(r["Open"]), float(r["Close"]), float(r["Low"]), float(r["High"])] for _, r in df.iterrows()]
        vol = [int(r["Volume"]) if pd.notna(r["Volume"]) else 0 for _, r in df.iterrows()]
        vol_color = [T["up"] if r["Close"] >= r["Open"] else T["down"] for _, r in df.iterrows()]
        ma20 = [round(v, 2) if pd.notna(v) else None for v in df["SMA_20"].tolist()]
        ma60 = [round(v, 2) if pd.notna(v) else None for v in df["SMA_60"].tolist()]
        ma200 = [round(v, 2) if pd.notna(v) else None for v in df["SMA_200"].tolist()]
        k_vals = [round(v, 2) if pd.notna(v) else None for v in df["K"].tolist()]
        d_vals = [round(v, 2) if pd.notna(v) else None for v in df["D"].tolist()]
        macd = [round(v, 3) if pd.notna(v) else None for v in df["MACD"].tolist()]
        macd_sig = [round(v, 3) if pd.notna(v) else None for v in df["MACD_Signal"].tolist()]
        macd_hist = [round(v, 3) if pd.notna(v) else None for v in df["MACD_Hist"].tolist()]
        macd_hist_color = [T["up"] if (v is not None and v >= 0) else T["down"] for v in macd_hist]    
        # Supertrend 拆多空兩條 (切換點補值避免斷裂)
        st_vals = [round(v, 2) if pd.notna(v) else None for v in df["ST"].tolist()]
        st_dir = [int(v) if pd.notna(v) else 0 for v in df["ST_DIR"].tolist()]
        st_up, st_dn = [], []
        for i in range(len(st_vals)):
            cur, v = st_dir[i], st_vals[i]
            prev = st_dir[i - 1] if i > 0 else cur
            if cur == 1:
                st_up.append(v); st_dn.append(v if prev == -1 else None)
            elif cur == -1:
                st_dn.append(v); st_up.append(v if prev == 1 else None)
            else:
                st_up.append(None); st_dn.append(None)

        # 殘差動能副圖 — 對齊到顯示中的 K 線交易日 (殘差序列在 SPY 主日曆上)
        resid = data.get("resid")
        disp_dates = [d.date() if hasattr(d, "date") else d for d in df.index]
        ra_vals = [None] * len(dates)
        z_vals = [None] * len(dates)
        resid_title = "殘差動能 (資料不足)"
        if resid:
            ra_map = {d.date(): v for d, v in resid["rolling_alpha"].items()}
            z_map = {d.date(): v for d, v in resid["z_short"].items()}
            ra_vals = [round(float(ra_map[d]), 4) if pd.notna(ra_map.get(d)) else None for d in disp_dates]
            z_vals = [round(float(z_map[d]), 2) if pd.notna(z_map.get(d)) else None for d in disp_dates]
            fct = "SPY+" + resid["sector_etf"] if resid.get("sector_etf") else "SPY"
            b_txt = f"β {resid['beta_mkt']:.2f}" if pd.notna(resid["beta_mkt"]) else "β -"
            r2_txt = f"R² {resid['r2']:.2f}" if pd.notna(resid["r2"]) else "R² -"
            rm_txt = f"rMOM {resid['rmom']:.2f}" if pd.notna(resid["rmom"]) else "rMOM -"
            resid_title = f"殘差動能 vs {fct} · {b_txt} {r2_txt} · {rm_txt} [{RM_SIGNAL_ZH[resid['signal']]}]"
        ra_color = [T["up"] if (v is not None and v >= 0) else T["down"] for v in ra_vals]

        scripts.append(f"""
var kc_{tk} = echarts.init(document.getElementById('kline_{tk}'));
kc_{tk}.setOption({{
  title: [
    {{ text: 'K線 · 均線 · 成交量', left: '6%', top: '1%', textStyle: {{fontSize: 12, color: '{T["title"]}'}} }},
    {{ text: '{resid_title}', left: '6%', top: '45%', textStyle: {{fontSize: 11, color: '{T["title"]}'}} }},
    {{ text: 'KD(14,3,3)', left: '6%', top: '60.5%', textStyle: {{fontSize: 11, color: '{T["title"]}'}} }},
    {{ text: 'MACD(12,26,9)', left: '6%', top: '76%', textStyle: {{fontSize: 11, color: '{T["title"]}'}} }}
  ],
  legend: {{ type: 'scroll', data: ['MA20','MA60','MA200','Supertrend↑','Supertrend↓','成交量','α20日年化','Z(21日)','K','D','MACD','Signal','Hist'],
    selected: {{'MA60': false, 'MA200': false}},
    top: '1%', left: '32%', right: '6%', textStyle: {{fontSize: 10, color: '{T["legend"]}'}}, itemWidth: 12, itemHeight: 8,
    pageIconColor: '{T["legend"]}', pageIconInactiveColor: '{T["axis_line"]}', pageIconSize: 10, pageTextStyle: {{color: '{T["legend"]}', fontSize: 9}} }},
  tooltip: {{ trigger: 'axis', axisPointer: {{ type: 'cross', lineStyle: {{color: '#3a4658'}}, crossStyle: {{color: '#3a4658'}} }},
    backgroundColor: '{T["tooltip_bg"]}', borderColor: '{T["tooltip_border"]}', borderWidth: 1,
    textStyle: {{color: '{T["tooltip_text"]}', fontSize: 12, fontFamily: 'IBM Plex Mono'}} }},
  axisPointer: {{ link: {{xAxisIndex: 'all'}} }},
  grid: [
    {{ left: '6%', right: '6%', top: '6%', height: '36%' }},
    {{ left: '6%', right: '6%', top: '64%', height: '10.5%' }},
    {{ left: '6%', right: '6%', top: '79.5%', height: '10.5%' }},
    {{ left: '6%', right: '6%', top: '48.5%', height: '10.5%' }}
  ],
  xAxis: [
    {{ type: 'category', gridIndex: 0, data: {json.dumps(dates)}, boundaryGap: true, axisLabel: {{show: true, fontSize: 10, color: '{T["axis_label"]}'}}, axisLine: {{lineStyle: {{color: '{T["axis_line"]}'}}}} }},
    {{ type: 'category', gridIndex: 1, data: {json.dumps(dates)}, boundaryGap: true, axisLabel: {{show: false}}, axisLine: {{lineStyle: {{color: '{T["axis_line"]}'}}}} }},
    {{ type: 'category', gridIndex: 2, data: {json.dumps(dates)}, boundaryGap: true, axisLabel: {{show: true, fontSize: 10, color: '{T["axis_label"]}'}}, axisLine: {{lineStyle: {{color: '{T["axis_line"]}'}}}} }},
    {{ type: 'category', gridIndex: 3, data: {json.dumps(dates)}, boundaryGap: true, axisLabel: {{show: false}}, axisLine: {{lineStyle: {{color: '{T["axis_line"]}'}}}} }}
  ],
  yAxis: [
    {{ scale: true, gridIndex: 0, splitNumber: 5, splitArea: {{show: false}}, splitLine: {{lineStyle: {{color: '{T["split_line"]}'}}}}, axisLabel: {{fontSize: 9, color: '{T["axis_label"]}', formatter: function(v){{return v.toFixed(0);}}}} }},
    {{ scale: true, gridIndex: 0, show: false, max: function(v){{return Math.max(v.max*6,1);}} }},
    {{ scale: false, gridIndex: 1, min: 0, max: 100, splitNumber: 3, axisLabel: {{fontSize: 9, color: '{T["axis_label"]}'}}, splitLine: {{lineStyle: {{color: '{T["split_line"]}'}}}} }},
    {{ scale: true, gridIndex: 2, splitNumber: 3, axisLabel: {{fontSize: 9, color: '{T["axis_label"]}'}}, splitLine: {{lineStyle: {{color: '{T["split_line"]}'}}}} }},
    {{ scale: true, gridIndex: 3, splitNumber: 2, axisLabel: {{fontSize: 9, color: '{T["axis_label"]}', formatter: function(v){{return (v*100).toFixed(0)+'%';}}}}, splitLine: {{lineStyle: {{color: '{T["split_line"]}'}}}} }},
    {{ scale: true, gridIndex: 3, show: false, splitLine: {{show: false}} }}
  ],
  dataZoom: [
    {{ type: 'inside', xAxisIndex: [0,1,2,3], start: 40, end: 100 }},
    {{ show: true, type: 'slider', xAxisIndex: [0,1,2,3], bottom: 8, height: 14, start: 40, end: 100, borderColor: '{T["dz_border"]}', fillerColor: '{T["dz_filler"]}', handleStyle: {{color: '{T["dz_handle"]}'}}, textStyle: {{color: '{T["dz_text"]}'}}, dataBackground: {{lineStyle: {{color: '{T["dz_bg_line"]}'}}, areaStyle: {{color: '{T["dz_bg_area"]}'}}}} }}
  ],
  series: [
    {{ name: 'K線', type: 'candlestick', xAxisIndex: 0, yAxisIndex: 0, data: {json.dumps(ohlc)}, itemStyle: {{color: '{T["up"]}', color0: '{T["down"]}', borderColor: '{T["up"]}', borderColor0: '{T["down"]}'}},
       markPoint: {{ symbol: 'circle', symbolSize: 6, animation: false,
         label: {{ show: true, position: 'top', distance: 10, color: '#fff', fontSize: 11, fontWeight: 'bold',
           padding: [2, 4], borderRadius: 3, backgroundColor: 'inherit', borderColor: '#fff', borderWidth: 1,
           formatter: function(p){{return p.data.lab;}} }},
         tooltip: {{ trigger: 'item', formatter: function(p){{return p.data.side + ' @ ' + p.data.price + ' × ' + p.data.size + ' 股';}} }},
         data: {mk_json} }} }},
    {{ name: 'MA20', type: 'line', xAxisIndex: 0, yAxisIndex: 0, data: {json.dumps(ma20)}, smooth: true, showSymbol: false, lineStyle: {{width: 1, color: '{T["ma20"]}'}} }},
    {{ name: 'MA60', type: 'line', xAxisIndex: 0, yAxisIndex: 0, data: {json.dumps(ma60)}, smooth: true, showSymbol: false, lineStyle: {{width: 1, color: '{T["ma50"]}'}} }},
    {{ name: 'MA200', type: 'line', xAxisIndex: 0, yAxisIndex: 0, data: {json.dumps(ma200)}, smooth: true, showSymbol: false, lineStyle: {{width: 1, color: '{T["ma200"]}'}} }},
    {{ name: 'Supertrend↑', type: 'line', xAxisIndex: 0, yAxisIndex: 0, data: {json.dumps(st_up)}, connectNulls: false, showSymbol: false, lineStyle: {{width: 2, color: '{T["up"]}'}} }},
    {{ name: 'Supertrend↓', type: 'line', xAxisIndex: 0, yAxisIndex: 0, data: {json.dumps(st_dn)}, connectNulls: false, showSymbol: false, lineStyle: {{width: 2, color: '{T["down"]}'}} }},
    {{ name: '成交量', type: 'bar', xAxisIndex: 0, yAxisIndex: 1, data: {json.dumps(vol)}, itemStyle: {{color: function(p){{return {json.dumps(vol_color)}[p.dataIndex];}}}} }},
    {{ name: 'K', type: 'line', xAxisIndex: 1, yAxisIndex: 2, data: {json.dumps(k_vals)}, smooth: true, showSymbol: false, lineStyle: {{width: 1.2, color: '{T["rsi"]}'}},
       markLine: {{ silent: true, symbol: 'none', data: [{{yAxis: 80, lineStyle: {{color: '{T["down"]}', type: 'dashed', width: 0.8}}}}, {{yAxis: 20, lineStyle: {{color: '{T["up"]}', type: 'dashed', width: 0.8}}}}], label: {{show: false}} }} }},
    {{ name: 'D', type: 'line', xAxisIndex: 1, yAxisIndex: 2, data: {json.dumps(d_vals)}, smooth: true, showSymbol: false, lineStyle: {{width: 1.2, color: '{T["ma20"]}'}} }},
    {{ name: 'MACD', type: 'line', xAxisIndex: 2, yAxisIndex: 3, data: {json.dumps(macd)}, smooth: true, showSymbol: false, lineStyle: {{width: 1, color: '{T["ma50"]}'}} }},
    {{ name: 'Signal', type: 'line', xAxisIndex: 2, yAxisIndex: 3, data: {json.dumps(macd_sig)}, smooth: true, showSymbol: false, lineStyle: {{width: 1, color: '{T["ma20"]}'}} }},
    {{ name: 'Hist', type: 'bar', xAxisIndex: 2, yAxisIndex: 3, data: {json.dumps(macd_hist)}, itemStyle: {{color: function(p){{return {json.dumps(macd_hist_color)}[p.dataIndex];}}}} }},
    {{ name: 'α20日年化', type: 'bar', xAxisIndex: 3, yAxisIndex: 4, data: {json.dumps(ra_vals)}, itemStyle: {{color: function(p){{return {json.dumps(ra_color)}[p.dataIndex];}}}} }},
    {{ name: 'Z(21日)', type: 'line', xAxisIndex: 3, yAxisIndex: 5, data: {json.dumps(z_vals)}, smooth: true, showSymbol: false, lineStyle: {{width: 1.2, color: '{T["ma200"]}'}},
       markLine: {{ silent: true, symbol: 'none', data: [{{yAxis: 2, lineStyle: {{color: '{T["down"]}', type: 'dashed', width: 0.8}}}}, {{yAxis: -2, lineStyle: {{color: '{T["up"]}', type: 'dashed', width: 0.8}}}}], label: {{show: false}} }} }}
  ]
}});
window._klineCharts = window._klineCharts || {{}};
window._klineCharts['{tk}'] = kc_{tk};
window._tradeMarks = window._tradeMarks || {{}};
window._tradeMarks['{tk}'] = {mk_json};
window.addEventListener('resize', function(){{ kc_{tk}.resize(); }});
""")

        opt = options_data.get(tk)
        if opt and opt.get("oi_strikes"):
            mp = opt.get("max_pain")
            cur = data["latest"]["close"]
            scripts.append(f"""
var oc_{tk} = echarts.init(document.getElementById('opt_{tk}'));
oc_{tk}.setOption({{
  title: {{ text: '未平倉量 OI by Strike', left: '5%', top: '2%', textStyle: {{fontSize: 11, color: '{T["title"]}'}} }},
  legend: {{ data: ['Call OI','Put OI'], top: '2%', right: '5%', textStyle: {{fontSize: 10, color: '{T["legend"]}'}}, itemWidth: 12, itemHeight: 8 }},
  tooltip: {{ trigger: 'axis', axisPointer: {{type: 'shadow'}}, backgroundColor: '{T["tooltip_bg"]}', borderColor: '{T["tooltip_border"]}', borderWidth: 1, textStyle: {{color: '{T["tooltip_text"]}', fontSize: 11, fontFamily: 'IBM Plex Mono'}} }},
  grid: {{ left: '6%', right: '4%', top: '18%', bottom: '12%' }},
  xAxis: {{ type: 'category', data: {json.dumps(opt["oi_strikes"])}, axisLabel: {{fontSize: 9, color: '{T["axis_label"]}', rotate: 45}}, axisLine: {{lineStyle: {{color: '{T["axis_line"]}'}}}} }},
  yAxis: {{ type: 'value', splitNumber: 4, axisLabel: {{fontSize: 9, color: '{T["axis_label"]}', formatter: function(v){{return (v/1000).toFixed(0)+'K';}}}}, splitLine: {{lineStyle: {{color: '{T["split_line"]}'}}}} }},
  series: [
    {{ name: 'Call OI', type: 'bar', data: {json.dumps(opt["oi_calls"])}, itemStyle: {{color: '{T["call"]}'}},
       markLine: {{ silent: true, symbol: 'none', data: [
         {{ xAxis: '{mp}', lineStyle: {{color: '{T["ma20"]}', width: 1.5}}, label: {{formatter: 'Max Pain', color: '{T["ma20"]}', fontSize: 10}} }},
         {{ xAxis: '{round(cur,2)}', lineStyle: {{color: '{T["rsi"]}', width: 1.5, type: 'dashed'}}, label: {{formatter: '現價', color: '{T["rsi"]}', fontSize: 10}} }}
       ] }} }},
    {{ name: 'Put OI', type: 'bar', data: {json.dumps(opt["oi_puts"])}, itemStyle: {{color: '{T["put"]}'}} }}
  ]
}});
window.addEventListener('resize', function(){{ oc_{tk}.resize(); }});
""")

    # 那斯達克 / 費城半導體 主圖 (K線+MA20+Supertrend+量)
    ndx_s = md.get("nasdaq_series", [])
    sox_s = md.get("sox_series", [])
    scripts.append(_index_kline_script("nasdaq_chart", "ndxc", ndx_s, T))
    scripts.append(_index_kline_script("sox_chart", "soxc", sox_s, T))

    # --- MACD 獨立小圖 (那斯達克 + 費半各一個) ---
    def _macd_script(div_id, var, series):
        if not series: return ""
        dates = [d["date"][-5:] for d in series]
        macd = [d["macd"] for d in series]
        macd_sig = [d["macd_sig"] for d in series]
        macd_hist = [d["macd_hist"] for d in series]
        mhc = [T["up"] if (v is not None and v >= 0) else T["down"] for v in macd_hist]
        return f"""
var {var} = echarts.init(document.getElementById('{div_id}'));
{var}.group = 'market';
{var}.setOption({{
  title: [{{ text: 'MACD(12,26,9)', left: '6%', top: '4%', textStyle: {{fontSize: 10, color: '{T["title"]}'}} }}],
  legend: {{ data: ['DIF','DEA','Hist'], top: '4%', right: '6%', textStyle: {{fontSize: 9, color: '{T["legend"]}'}}, itemWidth: 10, itemHeight: 7 }},
  tooltip: {{ trigger: 'axis', backgroundColor: '{T["tooltip_bg"]}', borderColor: '{T["tooltip_border"]}', borderWidth: 1, textStyle: {{color: '{T["tooltip_text"]}', fontSize: 11, fontFamily: 'IBM Plex Mono'}} }},
  grid: {{ left: '6%', right: '6%', top: '28%', bottom: '20%' }},
  xAxis: {{ type: 'category', data: {json.dumps(dates)}, boundaryGap: true, axisLabel: {{fontSize: 9, color: '{T["axis_label"]}'}}, axisLine: {{lineStyle: {{color: '{T["axis_line"]}'}}}} }},
  yAxis: {{ scale: true, splitNumber: 3, axisLabel: {{fontSize: 9, color: '{T["axis_label"]}'}}, splitLine: {{lineStyle: {{color: '{T["split_line"]}'}}}} }},
  dataZoom: [{{ type: 'inside', start: 40, end: 100 }}],
  series: [
    {{ name: 'DIF', type: 'line', data: {json.dumps(macd)}, smooth: true, showSymbol: false, lineStyle: {{width: 1, color: '{T["ma50"]}'}} }},
    {{ name: 'DEA', type: 'line', data: {json.dumps(macd_sig)}, smooth: true, showSymbol: false, lineStyle: {{width: 1, color: '{T["ma20"]}'}} }},
    {{ name: 'Hist', type: 'bar', data: {json.dumps(macd_hist)}, itemStyle: {{color: function(p){{return {json.dumps(mhc)}[p.dataIndex];}}}} }}
  ]
}});
window.addEventListener('resize', function(){{ {var}.resize(); }});
"""
    scripts.append(_macd_script("nasdaq_macd", "ndxm", ndx_s))
    scripts.append(_macd_script("sox_macd", "soxm", sox_s))

    # --- 各指數獨立 KD 圖 (per-index, K/D 兩條線 + 80/20 參考線) ---
    def _kd_script(div_id, var, series):
        if not series: return ""
        dates = [d["date"][-5:] for d in series]
        k_vals = [d["k"] for d in series]
        d_vals = [d["d"] for d in series]
        return f"""
var {var} = echarts.init(document.getElementById('{div_id}'));
{var}.group = 'market';
{var}.setOption({{
  title: [{{ text: 'KD(14,3,3)', left: '6%', top: '4%', textStyle: {{fontSize: 10, color: '{T["title"]}'}} }}],
  legend: {{ data: ['K','D'], top: '4%', right: '6%', textStyle: {{fontSize: 9, color: '{T["legend"]}'}}, itemWidth: 10, itemHeight: 7 }},
  tooltip: {{ trigger: 'axis', backgroundColor: '{T["tooltip_bg"]}', borderColor: '{T["tooltip_border"]}', borderWidth: 1, textStyle: {{color: '{T["tooltip_text"]}', fontSize: 11, fontFamily: 'IBM Plex Mono'}} }},
  grid: {{ left: '6%', right: '6%', top: '28%', bottom: '20%' }},
  xAxis: {{ type: 'category', data: {json.dumps(dates)}, boundaryGap: false, axisLabel: {{fontSize: 9, color: '{T["axis_label"]}'}}, axisLine: {{lineStyle: {{color: '{T["axis_line"]}'}}}} }},
  yAxis: {{ min: 0, max: 100, splitNumber: 4, axisLabel: {{fontSize: 9, color: '{T["axis_label"]}'}}, splitLine: {{lineStyle: {{color: '{T["split_line"]}'}}}} }},
  dataZoom: [{{ type: 'inside', start: 40, end: 100 }}],
  series: [
    {{ name: 'K', type: 'line', data: {json.dumps(k_vals)}, smooth: true, showSymbol: false, lineStyle: {{width: 1.4, color: '{T["rsi"]}'}},
       markLine: {{ silent: true, symbol: 'none', data: [{{yAxis: 80, lineStyle: {{color: '{T["down"]}', type: 'dashed', width: 0.8}}}}, {{yAxis: 20, lineStyle: {{color: '{T["up"]}', type: 'dashed', width: 0.8}}}}], label: {{show: false}} }} }},
    {{ name: 'D', type: 'line', data: {json.dumps(d_vals)}, smooth: true, showSymbol: false, lineStyle: {{width: 1.2, color: '{T["ma20"]}'}} }}
  ]
}});
window.addEventListener('resize', function(){{ {var}.resize(); }});
"""
    scripts.append(_kd_script("kd_ndx_chart", "ndxk", ndx_s))
    scripts.append(_kd_script("kd_sox_chart", "soxk", sox_s))

    # --- VIX 獨立線圖 ---
    fg = md.get("fear_greed", {})
    macro = md.get("macro_axis") or {}
    macro_dates = macro.get("dates")
    vix_series = md.get("vix_series", [])
    if vix_series:
        if macro_dates:
            vdates, vvals = macro_dates, macro["vix"]
        else:
            vdates = [d["date"][-5:] for d in vix_series]
            vvals = [round(d["close"], 2) for d in vix_series]
        scripts.append(f"""
var vixc = echarts.init(document.getElementById('vix_chart'));
vixc.group = 'market';
vixc.setOption({{
  title: [{{ text: 'VIX 波動率', left: '6%', top: '4%', textStyle: {{fontSize: 10, color: '{T["title"]}'}} }}],
  tooltip: {{ trigger: 'axis', axisPointer: {{type: 'cross', lineStyle: {{color: '#3a4658'}}, crossStyle: {{color: '#3a4658'}}}}, backgroundColor: '{T["tooltip_bg"]}', borderColor: '{T["tooltip_border"]}', borderWidth: 1, textStyle: {{color: '{T["tooltip_text"]}', fontSize: 11, fontFamily: 'IBM Plex Mono'}} }},
  grid: {{ left: '6%', right: '6%', top: '28%', bottom: '20%' }},
  xAxis: {{ type: 'category', data: {json.dumps(vdates)}, boundaryGap: false, axisLabel: {{fontSize: 9, color: '{T["axis_label"]}'}}, axisLine: {{lineStyle: {{color: '{T["axis_line"]}'}}}} }},
  yAxis: {{ scale: true, splitNumber: 4, axisLabel: {{fontSize: 9, color: '{T["axis_label"]}'}}, splitLine: {{lineStyle: {{color: '{T["split_line"]}'}}}} }},
  dataZoom: [{{ type: 'inside', start: 40, end: 100 }}],
  series: [{{ name: 'VIX', type: 'line', data: {json.dumps(vvals)}, smooth: true, showSymbol: false, connectNulls: true, lineStyle: {{width: 1.6, color: '{T["vix"]}'}}, areaStyle: {{color: 'rgba(224,168,60,0.12)'}},
    markLine: {{ silent: true, symbol: 'none', data: [{{yAxis: 20, lineStyle: {{color: '{T["neutral"]}', type: 'dashed', width: 0.8}}, label: {{formatter: '20', color: '{T["neutral"]}', fontSize: 9, position: 'end'}}}}, {{yAxis: 30, lineStyle: {{color: '{T["down"]}', type: 'dashed', width: 0.8}}, label: {{formatter: '30 恐慌', color: '{T["down"]}', fontSize: 9, position: 'end'}}}}] }} }}]
}});
window.addEventListener('resize', function(){{ vixc.resize(); }});
""")

    # --- CNN F&G 歷史線 (gauge 另外處理) ---
    fg_hist = fg.get("history", [])
    # 對齊到總經共用日期軸；某交易日無 F&G 值則留空白 (None)
    if macro_dates and any(v is not None for v in macro.get("fg", [])):
        fgdates, fgvals = macro_dates, macro["fg"]
    elif fg_hist:
        fgdates = [d["date"][-5:] for d in fg_hist]
        fgvals = [d["score"] for d in fg_hist]
    else:
        fgdates, fgvals = None, None
    if fgdates:
        scripts.append(f"""
var fghc = echarts.init(document.getElementById('fg_chart'));
fghc.group = 'market';
fghc.setOption({{
  title: [{{ text: 'CNN Fear & Greed', left: '6%', top: '4%', textStyle: {{fontSize: 10, color: '{T["title"]}'}} }}],
  tooltip: {{ trigger: 'axis', backgroundColor: '{T["tooltip_bg"]}', borderColor: '{T["tooltip_border"]}', borderWidth: 1, textStyle: {{color: '{T["tooltip_text"]}', fontSize: 11, fontFamily: 'IBM Plex Mono'}} }},
  grid: {{ left: '6%', right: '6%', top: '28%', bottom: '22%' }},
  xAxis: {{ type: 'category', data: {json.dumps(fgdates)}, boundaryGap: false, axisLabel: {{fontSize: 9, color: '{T["axis_label"]}', interval: 14}}, axisLine: {{lineStyle: {{color: '{T["axis_line"]}'}}}} }},
  yAxis: {{ min: 0, max: 100, splitNumber: 4, axisLabel: {{fontSize: 9, color: '{T["axis_label"]}'}}, splitLine: {{lineStyle: {{color: '{T["split_line"]}'}}}} }},
  dataZoom: [{{ type: 'inside', start: 40, end: 100 }}],
  series: [{{ type: 'line', data: {json.dumps(fgvals)}, smooth: true, showSymbol: false, connectNulls: false, lineStyle: {{width: 1.5, color: '{T["rsi"]}'}}, areaStyle: {{color: 'rgba(111,155,255,0.10)'}},
    markLine: {{ silent: true, symbol: 'none', data: [{{yAxis: 25, lineStyle: {{color: '{T["down"]}', type: 'dashed', width: 0.6}}, label: {{formatter: '25', color: '{T["down"]}', fontSize: 9, position: 'end'}}}}, {{yAxis: 75, lineStyle: {{color: '{T["up"]}', type: 'dashed', width: 0.6}}, label: {{formatter: '75', color: '{T["up"]}', fontSize: 9, position: 'end'}}}}] }} }}]
}});
window.addEventListener('resize', function(){{ fghc.resize(); }});
""")

    # --- 10Y 美債殖利率獨立線圖 ---
    tnx_series = md.get("tnx_series", [])
    if tnx_series:
        if macro_dates:
            tdates, tvals = macro_dates, macro["tnx"]
        else:
            tdates = [d["date"][-5:] for d in tnx_series]
            tvals = [round(d["close"], 3) for d in tnx_series]
        scripts.append(f"""
var tnxc = echarts.init(document.getElementById('tnx_chart'));
tnxc.group = 'market';
tnxc.setOption({{
  title: [{{ text: '10Y 美債殖利率', left: '6%', top: '4%', textStyle: {{fontSize: 10, color: '{T["title"]}'}} }}],
  tooltip: {{ trigger: 'axis', axisPointer: {{type: 'cross', lineStyle: {{color: '#3a4658'}}, crossStyle: {{color: '#3a4658'}}}}, backgroundColor: '{T["tooltip_bg"]}', borderColor: '{T["tooltip_border"]}', borderWidth: 1, textStyle: {{color: '{T["tooltip_text"]}', fontSize: 11, fontFamily: 'IBM Plex Mono'}}, valueFormatter: function(v){{return v==null?'-':v.toFixed(3)+'%';}} }},
  grid: {{ left: '6%', right: '6%', top: '28%', bottom: '20%' }},
  xAxis: {{ type: 'category', data: {json.dumps(tdates)}, boundaryGap: false, axisLabel: {{fontSize: 9, color: '{T["axis_label"]}'}}, axisLine: {{lineStyle: {{color: '{T["axis_line"]}'}}}} }},
  yAxis: {{ scale: true, splitNumber: 4, axisLabel: {{fontSize: 9, color: '{T["axis_label"]}', formatter: '{{value}}%'}}, splitLine: {{lineStyle: {{color: '{T["split_line"]}'}}}} }},
  dataZoom: [{{ type: 'inside', start: 40, end: 100 }}],
  series: [{{ name: '10Y 殖利率', type: 'line', data: {json.dumps(tvals)}, smooth: true, showSymbol: false, connectNulls: true, lineStyle: {{width: 1.6, color: '{T["ma20"]}'}}, areaStyle: {{color: 'rgba(224,168,60,0.08)'}} }}]
}});
window.addEventListener('resize', function(){{ tnxc.resize(); }});
""")

    # 美債殖利率 2/10/30 年走勢 (共用單一左軸, 每條線右端標示最新值)
    yc = md.get("yield_curve", {})
    if yc.get("dates"):
        yseries = yc.get("series", {})
        line_colors = {"2年": T["rsi"], "10年": T["ma20"], "30年": T["ma200"]}
        labels = list(yseries.keys())
        # 共用單一 yAxis; 每條線使用 endLabel 在最右端顯示最新值
        yseries_arr = []
        for label, vals in yseries.items():
            color = line_colors.get(label, T["rsi"])
            yseries_arr.append(
                f"""{{ name: '{label}', type: 'line', data: {json.dumps(vals)}, smooth: true, showSymbol: false, connectNulls: true, """
                f"""lineStyle: {{width: 1.6, color: '{color}'}}, itemStyle: {{color: '{color}'}}, """
                f"""endLabel: {{show: true, formatter: function(p){{return '{label}: ' + (p.value==null?'-':p.value.toFixed(2)) + '%';}}, """
                f"""color: '#ffffff', backgroundColor: '{color}', padding: [4, 7], borderRadius: 5, """
                f"""fontSize: 11, fontWeight: 'bold', fontFamily: 'IBM Plex Mono', distance: 8}} }}"""
            )
        yseries_js = ",\n    ".join(yseries_arr)
        # right padding 加大給 endLabel 留空間
        scripts.append(f"""
var yldc = echarts.init(document.getElementById('yield_chart'));
yldc.setOption({{
  legend: {{ data: {json.dumps(labels)}, top: '4%', right: '6%', textStyle: {{fontSize: 10, color: '{T["legend"]}'}}, itemWidth: 12, itemHeight: 8 }},
  tooltip: {{ trigger: 'axis', axisPointer: {{type: 'cross', lineStyle: {{color: '#3a4658'}}, crossStyle: {{color: '#3a4658'}}}}, backgroundColor: '{T["tooltip_bg"]}', borderColor: '{T["tooltip_border"]}', borderWidth: 1, textStyle: {{color: '{T["tooltip_text"]}', fontSize: 11, fontFamily: 'IBM Plex Mono'}}, valueFormatter: function(v){{return v==null?'-':v.toFixed(3)+'%';}} }},
  grid: {{ left: '6%', right: '13%', top: '18%', bottom: '12%' }},
  xAxis: {{ type: 'category', data: {json.dumps(yc["dates"])}, boundaryGap: false, axisLabel: {{fontSize: 9, color: '{T["axis_label"]}'}}, axisLine: {{lineStyle: {{color: '{T["axis_line"]}'}}}} }},
  yAxis: {{ type: 'value', scale: true, splitNumber: 5, axisLine: {{show: true, lineStyle: {{color: '{T["axis_line"]}'}}}}, axisTick: {{lineStyle: {{color: '{T["axis_line"]}'}}}}, axisLabel: {{fontSize: 9, color: '{T["axis_label"]}', formatter: '{{value}}%'}}, splitLine: {{lineStyle: {{color: '{T["split_line"]}'}}}} }},
  dataZoom: [{{ type: 'inside', start: 0, end: 100 }}],
  series: [
    {yseries_js}
  ]
}});
window.addEventListener('resize', function(){{ yldc.resize(); }});
""")

    # 美元指數 DXY
    dxy_series = md.get("dxy_series", [])
    if dxy_series:
        if macro_dates:
            ddates, dvals, dma20 = macro_dates, macro["dxy"], macro["dxy_ma20"]
        else:
            ddates = [d["date"][-5:] for d in dxy_series]
            dvals = [round(d["close"], 3) for d in dxy_series]
            dma20 = [d.get("ma20") for d in dxy_series]
        scripts.append(f"""
var dxyc = echarts.init(document.getElementById('dxy_chart'));
dxyc.group = 'market';
dxyc.setOption({{
  title: [{{ text: '美元指數 DXY', left: '6%', top: '4%', textStyle: {{fontSize: 10, color: '{T["title"]}'}} }}],
  legend: {{ data: ['DXY','MA20'], top: '4%', right: '6%', textStyle: {{fontSize: 9, color: '{T["legend"]}'}}, itemWidth: 10, itemHeight: 7 }},
  tooltip: {{ trigger: 'axis', axisPointer: {{type: 'cross', lineStyle: {{color: '#3a4658'}}, crossStyle: {{color: '#3a4658'}}}}, backgroundColor: '{T["tooltip_bg"]}', borderColor: '{T["tooltip_border"]}', borderWidth: 1, textStyle: {{color: '{T["tooltip_text"]}', fontSize: 11, fontFamily: 'IBM Plex Mono'}} }},
  grid: {{ left: '6%', right: '6%', top: '28%', bottom: '20%' }},
  xAxis: {{ type: 'category', data: {json.dumps(ddates)}, boundaryGap: false, axisLabel: {{fontSize: 9, color: '{T["axis_label"]}'}}, axisLine: {{lineStyle: {{color: '{T["axis_line"]}'}}}} }},
  yAxis: {{ scale: true, splitNumber: 4, splitLine: {{lineStyle: {{color: '{T["split_line"]}'}}}}, axisLabel: {{fontSize: 9, color: '{T["axis_label"]}', formatter: function(v){{return v.toFixed(1);}}}} }},
  dataZoom: [{{ type: 'inside', start: 40, end: 100 }}],
  series: [
    {{ name: 'DXY', type: 'line', data: {json.dumps(dvals)}, smooth: true, showSymbol: false, connectNulls: true, lineStyle: {{width: 1.6, color: '{T["accent"] if "accent" in T else T["rsi"]}'}}, areaStyle: {{color: 'rgba(77,127,255,0.10)'}} }},
    {{ name: 'MA20', type: 'line', data: {json.dumps(dma20)}, smooth: true, showSymbol: false, connectNulls: true, lineStyle: {{width: 1, color: '{T["ma20"]}'}} }}
  ]
}});
window.addEventListener('resize', function(){{ dxyc.resize(); }});
""")

    # 類股輪動 (支援 1日 / 5日 / 1月 長條 + 近30日累積折線 + 播放動畫)
    sectors = md.get("sectors", [])
    if sectors:
        sec_payload = [{"name": s["name"], "d1": s.get("d1", 0), "w1": s.get("w1", 0), "m1": s.get("m1", 0)} for s in sectors]
        fixed_order = [s["name"] for s in sectors][::-1]  # 長條圖固定順序 (m1 由小到大)
        daily_history = md.get("sectors_daily", [])
        # 11 個類股的固定配色 (依 SECTOR_ETFS 順序)
        sector_palette = [
            "#6f9bff", "#22d39a", "#ff8a6b", "#e0a83c", "#b07bff",
            "#ff525b", "#5fd3ff", "#8ec63f", "#f0c060", "#ff6bb5", "#4d7fff"
        ]
        all_names = list(SECTOR_ETFS.values())
        color_map = {name: sector_palette[i % len(sector_palette)] for i, name in enumerate(all_names)}
        scripts.append(f"""
window._sectorData = {json.dumps(sec_payload)};
window._sectorDaily = {json.dumps(daily_history)};
window._sectorFixedOrder = {json.dumps(fixed_order)};
window._sectorColorMap = {json.dumps(color_map)};
window._sectorAllNames = {json.dumps(all_names)};
window._sectorChart = echarts.init(document.getElementById('sector_chart'));
window._sectorPlayTimer = null;
window._sectorPlayIdx = 0;
window._sectorCurrentMode = 'm1';  // 當前顯示模式: d1 / w1 / m1 / trend

// ===== 長條圖模式 (1日 / 5日 / 1月) =====
window.renderSectorBar = function(period) {{
  var data = window._sectorData.slice();
  data.sort(function(a, b){{ return a[period] - b[period]; }});
  var names = data.map(function(s){{ return s.name; }});
  var vals = data.map(function(s){{ return s[period]; }});
  var colors = vals.map(function(v){{ return v >= 0 ? '{T["up"]}' : '{T["down"]}'; }});
  var seriesData = vals.map(function(v, i){{ return {{value: v, itemStyle: {{color: colors[i]}}}}; }});
  var pLabel = period === 'd1' ? '近1日' : (period === 'w1' ? '近5日' : '近1月');

  window._sectorChart.setOption({{
    title: {{ text: pLabel + ' 表現', left: 'right', top: '2%', textStyle: {{fontSize: 13, color: '{T["title"]}', fontWeight: 700}} }},
    legend: {{ show: false }},
    tooltip: {{ trigger: 'axis', axisPointer: {{type: 'shadow'}}, backgroundColor: '{T["tooltip_bg"]}', borderColor: '{T["tooltip_border"]}', borderWidth: 1, textStyle: {{color: '{T["tooltip_text"]}', fontSize: 11, fontFamily: 'IBM Plex Mono'}}, formatter: function(p){{return p[0].name + ': <b>' + p[0].value.toFixed(2) + '%</b>';}} }},
    grid: {{ left: '14%', right: '8%', top: '12%', bottom: '6%' }},
    xAxis: {{ type: 'value', axisLabel: {{fontSize: 9, color: '{T["axis_label"]}', formatter: '{{value}}%'}}, splitLine: {{lineStyle: {{color: '{T["split_line"]}'}}}} }},
    yAxis: {{ type: 'category', data: names, axisLabel: {{fontSize: 11, color: '{T["axis_label"]}'}}, axisLine: {{lineStyle: {{color: '{T["axis_line"]}'}}}} }},
    series: [{{ type: 'bar', data: seriesData, barWidth: '60%', label: {{show: true, position: 'right', fontSize: 10, color: '{T["axis_label"]}', formatter: function(p){{return p.value.toFixed(1)+'%';}}}} }}],
    animationDuration: 300
  }}, true);
}};

// ===== 近30日累積報酬折線圖模式 =====
// progress: null=顯示完整;否則只顯示到第 progress 天 (1-based)
window.renderSectorTrend = function(progress) {{
  var daily = window._sectorDaily;
  if (!daily || daily.length === 0) {{
    window._sectorChart.setOption({{
      title: {{ text: '無近30日資料', left: 'center', top: 'center', textStyle: {{color: '{T["axis_label"]}'}} }},
      legend: {{show:false}}, series: [], xAxis: {{show: false}}, yAxis: {{show: false}}
    }}, true);
    return;
  }}
  var dates = daily.map(function(d){{ return d.date; }});
  var names = window._sectorAllNames;
  var colorMap = window._sectorColorMap;
  var limit = progress != null ? progress : daily.length;

  var series = names.map(function(name) {{
    var data = daily.map(function(d, i) {{
      if (i >= limit) return null;
      return d.values[name] != null ? d.values[name] : null;
    }});
    return {{
      name: name,
      type: 'line',
      data: data,
      smooth: true,
      showSymbol: false,
      connectNulls: false,
      lineStyle: {{ width: 1.6, color: colorMap[name] }},
      itemStyle: {{ color: colorMap[name] }},
      emphasis: {{ focus: 'series', lineStyle: {{width: 2.4}} }}
    }};
  }});

  var titleText = progress != null
    ? '近30日累積報酬 · 第 ' + progress + ' / ' + daily.length + ' 天 (' + dates[Math.min(progress-1, dates.length-1)] + ')'
    : '近30日累積報酬';

  window._sectorChart.setOption({{
    title: {{ text: titleText, left: 'right', top: '2%', textStyle: {{fontSize: 13, color: '{T["ma50"]}', fontWeight: 700, fontFamily: 'IBM Plex Mono'}} }},
    legend: {{ show: true, data: names, top: '2%', left: '2%', textStyle: {{fontSize: 10, color: '{T["legend"]}'}}, itemWidth: 10, itemHeight: 8, type: 'scroll', width: '60%' }},
    tooltip: {{ trigger: 'axis', axisPointer: {{type: 'cross', lineStyle: {{color: '#3a4658'}}, crossStyle: {{color: '#3a4658'}}}}, backgroundColor: '{T["tooltip_bg"]}', borderColor: '{T["tooltip_border"]}', borderWidth: 1, textStyle: {{color: '{T["tooltip_text"]}', fontSize: 11, fontFamily: 'IBM Plex Mono'}}, valueFormatter: function(v){{return v==null?'-':v.toFixed(2)+'%';}} }},
    grid: {{ left: '6%', right: '6%', top: '15%', bottom: '8%' }},
    xAxis: {{ type: 'category', data: dates, boundaryGap: false, axisLabel: {{fontSize: 9, color: '{T["axis_label"]}'}}, axisLine: {{lineStyle: {{color: '{T["axis_line"]}'}}}} }},
    yAxis: {{ type: 'value', scale: true, axisLabel: {{fontSize: 9, color: '{T["axis_label"]}', formatter: '{{value}}%'}}, splitLine: {{lineStyle: {{color: '{T["split_line"]}'}}}}, axisLine: {{show: false}} }},
    series: series,
    animationDuration: 250,
    animationDurationUpdate: 200
  }}, true);
}};

// ===== 統一切換入口 =====
window.renderSectorChart = function(period) {{
  window._sectorCurrentMode = period;
  if (period === 'trend') {{
    window.renderSectorTrend(null);  // 顯示完整 30 天
  }} else {{
    window.renderSectorBar(period);
  }}
}};

window.switchSectorPeriod = function(btn) {{
  // 切換期別時停止播放
  if (window._sectorPlayTimer) {{
    clearInterval(window._sectorPlayTimer);
    window._sectorPlayTimer = null;
    var pb = document.getElementById('sectorPlayBtn');
    if (pb) {{ pb.innerHTML = '▶ 播放'; pb.classList.remove('playing'); }}
  }}
  var btns = document.querySelectorAll('.sector-btn[data-period]');
  for (var i = 0; i < btns.length; i++) btns[i].classList.remove('on');
  btn.classList.add('on');
  window.renderSectorChart(btn.getAttribute('data-period'));
}};

window.toggleSectorPlay = function(btn) {{
  if (window._sectorPlayTimer) {{
    // 停止播放, 顯示完整折線圖
    clearInterval(window._sectorPlayTimer);
    window._sectorPlayTimer = null;
    btn.innerHTML = '▶ 播放';
    btn.classList.remove('playing');
    window.renderSectorTrend(null);
    return;
  }}
  if (!window._sectorDaily || window._sectorDaily.length === 0) {{
    alert('無近30日資料可播放');
    return;
  }}
  // 若當前不在 trend 模式, 自動切換過去
  if (window._sectorCurrentMode !== 'trend') {{
    var btns = document.querySelectorAll('.sector-btn[data-period]');
    for (var i = 0; i < btns.length; i++) {{
      btns[i].classList.remove('on');
      if (btns[i].getAttribute('data-period') === 'trend') btns[i].classList.add('on');
    }}
    window._sectorCurrentMode = 'trend';
  }}
  btn.innerHTML = '⏸ 停止';
  btn.classList.add('playing');
  window._sectorPlayIdx = 1;
  var total = window._sectorDaily.length;
  function step() {{
    window.renderSectorTrend(window._sectorPlayIdx);
    window._sectorPlayIdx++;
    if (window._sectorPlayIdx > total) {{
      // 播完一輪後停在完整圖, 並還原按鈕
      clearInterval(window._sectorPlayTimer);
      window._sectorPlayTimer = null;
      btn.innerHTML = '▶ 播放';
      btn.classList.remove('playing');
      window.renderSectorTrend(null);
    }}
  }}
  step();
  window._sectorPlayTimer = setInterval(step, 400);
}};

// 預設顯示 1月
window.renderSectorChart('m1');
window.addEventListener('resize', function(){{ if(window._sectorChart) window._sectorChart.resize(); }});
""")
    # 連動所有 market group 圖表的 dataZoom 與十字線 (主圖/MACD/KD/VIX/F&G/TNX/DXY)
    scripts.append("setTimeout(function(){ if (typeof echarts !== 'undefined') echarts.connect('market'); }, 100);")

    return "\n".join(scripts)

def get_css():
    return """
:root{
  --bg:#0a0e15; --surface:#121823; --surface-2:#0f141d; --surface-3:#1a2230;
  --ink:#dde3ec; --ink-2:#8b95a5; --ink-3:#5d6675; --line:#1e2632; --line-2:#171e29;
  --accent:#4d7fff; --accent-2:#6f9bff; --accent-dim:#2a3a64;
  --up:#22d39a; --up-soft:rgba(34,211,154,.12); --down:#ff525b; --down-soft:rgba(255,82,91,.12);
  --gold:#e0a83c; --radius:13px; --shadow:0 2px 8px rgba(0,0,0,.4),0 12px 32px rgba(0,0,0,.35);
  --mono:'IBM Plex Mono',ui-monospace,monospace;
  --sans:'Manrope','Noto Sans TC',-apple-system,BlinkMacSystemFont,sans-serif;
}
*{margin:0;padding:0;box-sizing:border-box}
html{-webkit-font-smoothing:antialiased;text-rendering:optimizeLegibility}
body{font-family:var(--sans);color:var(--ink);line-height:1.5;padding:20px;min-height:100vh;scroll-behavior:smooth;
  background:linear-gradient(rgba(77,127,255,.025) 1px,transparent 1px),
    radial-gradient(900px 500px at 85% -8%,rgba(34,211,154,.08),transparent 60%),var(--bg);
  background-size:100% 28px,100% 100%,100% 100%;}
.num{font-family:var(--mono);font-variant-numeric:tabular-nums;letter-spacing:-.02em}
.up{color:var(--up)} .down{color:var(--down)}
.container{max-width:1480px;margin:0 auto}
.header{background:linear-gradient(120deg,#101622 0%,#141d2e 55%,#16203a 100%);border:1px solid var(--line);
  border-radius:var(--radius);padding:18px 24px;display:flex;justify-content:space-between;align-items:center;
  position:relative;overflow:hidden;box-shadow:var(--shadow)}
.header::before{content:"";position:absolute;left:0;top:0;right:0;height:2px;background:linear-gradient(90deg,transparent,var(--up),#5fd3ff,transparent);opacity:.7}
.header h1{font-size:18px;font-weight:800;color:#fff;letter-spacing:-.01em;position:relative;z-index:1}
.update-time{font-size:11.5px;color:#7e8aa0;margin-top:3px;display:flex;align-items:center;gap:8px;position:relative;z-index:1}
.update-time::before{content:"";width:6px;height:6px;border-radius:50%;background:var(--up);box-shadow:0 0 8px var(--up)}
.btn-run{font-family:var(--sans);font-size:12.5px;font-weight:600;color:#cdd6e6;background:rgba(77,127,255,.1);
  border:1px solid rgba(77,127,255,.32);padding:9px 15px;border-radius:9px;cursor:pointer;transition:.18s;position:relative;z-index:1}
.btn-run:hover{background:rgba(77,127,255,.2);border-color:var(--accent);transform:translateY(-1px)}
.tabs-container{display:flex;gap:3px;margin:18px 0;background:var(--surface);padding:4px;border-radius:11px;border:1px solid var(--line);width:fit-content;overflow-x:auto;scrollbar-width:none}
.tabs-container::-webkit-scrollbar{display:none}
.tab-btn{font-family:var(--sans);font-size:13px;font-weight:600;color:var(--ink-2);padding:8px 16px;border:none;background:transparent;border-radius:8px;cursor:pointer;transition:.18s;white-space:nowrap}
.tab-btn:hover{color:var(--ink);background:var(--surface-3)}
.tab-btn.active{background:linear-gradient(135deg,#3a63d8,#4d7fff);color:#fff;box-shadow:0 0 16px rgba(77,127,255,.4)}
.tab-content{display:none;animation:fade .35s ease}
.tab-content.active{display:block}
@keyframes fade{from{opacity:0;transform:translateY(6px)}to{opacity:1;transform:none}}
.section-head{display:flex;align-items:baseline;gap:11px;margin:2px 0 14px}
.section-head h2{font-size:14px;font-weight:700;letter-spacing:.02em}
.section-head .eyebrow{font-size:10px;font-weight:600;color:var(--up);text-transform:uppercase;letter-spacing:.16em}
.card{background:var(--surface);border:1px solid var(--line);border-radius:var(--radius);padding:18px;margin-bottom:16px}
.card-title{font-size:13.5px;font-weight:700;margin-bottom:13px;display:flex;justify-content:space-between;align-items:center;flex-wrap:wrap;gap:10px}
.chart-box{width:100%;border:1px solid var(--line);border-radius:11px;background:var(--surface-2);overflow:hidden}
.grid-2-market{display:grid;grid-template-columns:2fr 1fr;gap:16px;margin-bottom:16px}
.fg-card{display:flex;flex-direction:column}
.fg-meta{display:flex;justify-content:space-around;align-items:center;font-size:12px;color:var(--ink-2);margin-top:6px}
.fg-meta b{font-size:18px;color:var(--ink)}
.fg-rating{text-transform:capitalize;font-weight:700;color:var(--gold)}
.yields-row{display:grid;grid-template-columns:repeat(4,1fr);gap:12px}
.yield-cell{background:var(--surface-2);border:1px solid var(--line);border-radius:10px;padding:12px 14px;text-align:center}
.yield-cell .yl{font-size:11px;color:var(--ink-3);font-weight:600}
.yield-cell .yv{font-size:20px;font-weight:600;margin:4px 0}
.yield-cell .yc{font-size:11px}
.metrics{display:grid;grid-template-columns:repeat(4,1fr);gap:12px;margin-bottom:16px}
.metric{background:var(--surface);border:1px solid var(--line);border-radius:var(--radius);padding:14px 16px 13px;position:relative;overflow:hidden;transition:.2s}
.metric:hover{border-color:#2b3645;transform:translateY(-2px)}
.metric::before{content:"";position:absolute;left:0;top:0;bottom:0;width:3px;background:var(--ink-3);opacity:.6}
.metric.accent::before{background:linear-gradient(var(--accent),#5fd3ff);opacity:1;box-shadow:0 0 12px rgba(77,127,255,.6)}
.metric.up::before{background:var(--up);box-shadow:0 0 12px var(--up-soft)}
.metric.down::before{background:var(--down);box-shadow:0 0 12px var(--down-soft)}
.metric .label{font-size:10px;font-weight:600;color:var(--ink-3);text-transform:uppercase;letter-spacing:.1em;margin-bottom:8px}
.metric .value{font-size:21px;font-weight:600;letter-spacing:-.02em;line-height:1}
.metric .change{font-size:11.5px;font-weight:600;margin-top:7px;display:inline-flex;align-items:center;gap:5px}
.metric .chip{display:inline-flex;align-items:center;gap:3px;padding:2px 6px;border-radius:5px;font-size:10.5px}
.chip-up{background:var(--up-soft);color:var(--up)} .chip-down{background:var(--down-soft);color:var(--down)}
.ret-row{display:grid;grid-template-columns:repeat(4,1fr);gap:4px;margin-top:9px;padding-top:9px;border-top:1px solid var(--line-2)}
.rc-head{text-align:center}
.rc-head .rcl{font-size:9px;color:var(--ink-3);font-weight:600;letter-spacing:.04em;margin-bottom:2px}
.rc .rcv{font-size:11.5px;font-weight:600}
.vix-now{font-size:13px;font-weight:600;color:var(--ink)}
.vix-now .up{color:var(--up)} .vix-now .down{color:var(--down)}
.ctrl{background:var(--surface);border:1px solid var(--line);border-radius:var(--radius);padding:9px 14px;margin-bottom:10px;display:flex;align-items:center;gap:11px;flex-wrap:wrap}
.ctrl-lbl{font-size:9.5px;color:var(--ink-3);font-weight:600;text-transform:uppercase;letter-spacing:.08em}
.sep{width:1px;height:18px;background:var(--line)}
.seg{display:inline-flex;background:var(--surface-2);border:1px solid var(--line);border-radius:6px;padding:2px;gap:2px}
.seg button{background:transparent;border:0;color:var(--ink-2);font-family:inherit;font-size:11px;font-weight:600;padding:5px 12px;border-radius:4px;cursor:pointer;transition:.15s}
.seg button.on{background:var(--accent);color:#fff;box-shadow:0 0 8px rgba(77,127,255,.35)}
.seg button:not(.on):hover{color:var(--ink);background:rgba(255,255,255,.03)}
.chips{display:flex;flex-wrap:wrap;gap:5px;flex:1;min-width:0}
.chip{display:inline-flex;align-items:center;gap:4px;background:var(--surface-2);border:1px solid var(--line);color:var(--ink-2);font-family:inherit;font-size:10.5px;font-weight:600;padding:5px 10px;border-radius:13px;cursor:pointer;transition:.15s;user-select:none}
.chip:hover{border-color:#2b3645;color:var(--ink)}
.chip.on{background:rgba(77,127,255,.14);border-color:#3a63d8;color:var(--accent-2)}
.chip .dot{width:5px;height:5px;border-radius:50%;background:#3a4658}
.chip.on .dot{background:var(--up);box-shadow:0 0 4px var(--up)}
.cnt{font-size:10px;color:var(--ink-3);font-family:var(--mono)}
.stack{background:var(--surface);border:1px solid var(--line);border-radius:var(--radius);overflow:hidden;margin-bottom:11px}
.pane{position:relative;background:transparent}
.pane+.pane{border-top:1px solid var(--line-2)}
.sub-panel{transition:opacity .2s ease}
.rating-wrap{background:var(--surface);border:1px solid var(--line);border-radius:var(--radius);padding:20px}
.rating-top{display:flex;justify-content:space-between;align-items:center;margin-bottom:16px;padding-bottom:13px;border-bottom:1px solid var(--line)}
.rating-top h3{font-size:14px;font-weight:700}
.rating-top .upd{font-size:11px;color:var(--ink-3);font-family:var(--mono)}
.rating-grid{display:grid;grid-template-columns:repeat(5,1fr);gap:11px}
.rcol{background:var(--surface-2);border:1px solid var(--line);border-radius:11px;padding:11px;border-top:2px solid}
.rcol[data-k=sb]{border-top-color:#22d39a} .rcol[data-k=b]{border-top-color:#8ec63f}
.rcol[data-k=n]{border-top-color:#6b7686} .rcol[data-k=s]{border-top-color:#ff8a6b} .rcol[data-k=ss]{border-top-color:#ff525b}
.rcol-head{display:flex;justify-content:space-between;align-items:center;margin-bottom:10px}
.rcol-label{font-size:12px;font-weight:700}
.rcol[data-k=sb] .rcol-label{color:#22d39a} .rcol[data-k=b] .rcol-label{color:#8ec63f}
.rcol[data-k=n] .rcol-label{color:#8b95a5} .rcol[data-k=s] .rcol-label{color:#ff8a6b} .rcol[data-k=ss] .rcol-label{color:#ff525b}
.rcol-count{font-size:10.5px;font-weight:600;color:var(--ink-2);background:var(--surface-3);border:1px solid var(--line);border-radius:20px;padding:1px 8px}
.schip{background:var(--surface-3);border:1px solid var(--line);border-radius:8px;padding:7px 9px;margin-bottom:6px;transition:.16s}
.schip:hover{border-color:var(--accent-dim);transform:translateX(2px)}
.schip-top{display:flex;justify-content:space-between;align-items:baseline}
.schip-name{font-size:11.5px;font-weight:600}
.schip-chg{font-size:11px;font-weight:600;font-family:var(--mono)}
.schip-meta{display:flex;gap:6px;margin-top:5px}
.tag{font-size:9.5px;font-weight:600;padding:1px 6px;border-radius:5px}
.tag.t{background:rgba(77,127,255,.14);color:#7f9fff} .tag.c{background:var(--up-soft);color:var(--up)}
.empty{font-size:11px;color:var(--ink-3);text-align:center;padding:14px 0}
.legend{margin-top:15px;border-top:1px dashed var(--line);padding-top:13px}
.legend summary{cursor:pointer;font-size:12px;color:var(--ink-2);font-weight:600;list-style:none}
.legend summary::-webkit-details-marker{display:none}
.legend-body{margin-top:11px;display:flex;flex-direction:column;gap:8px;font-size:11.5px;color:var(--ink-2);background:var(--surface-2);border:1px solid var(--line);border-radius:10px;padding:13px}
.legend-row{display:flex;gap:10px;align-items:flex-start;line-height:1.5}
.lbadge{font-size:9.5px;font-weight:700;padding:2px 7px;border-radius:5px;white-space:nowrap}
.lbadge.t{background:rgba(77,127,255,.14);color:#7f9fff} .lbadge.c{background:var(--up-soft);color:var(--up)} .lbadge.tt{background:rgba(224,168,60,.15);color:var(--gold)}
.dtable{width:100%;font-size:12px;border-collapse:collapse}
.dtable th{text-align:right;padding:7px 10px;background:var(--surface-2);color:var(--ink-2);font-weight:600;font-size:10.5px;border-bottom:1px solid var(--line);text-transform:uppercase;letter-spacing:.03em}
.dtable th:first-child{text-align:left}
.dtable td{padding:7px 10px;border-bottom:1px solid var(--line-2);text-align:right;font-family:var(--mono);font-weight:500}
.dtable td:first-child{text-align:left;font-family:var(--sans);font-weight:600;color:var(--ink)}
.dtable tr:last-child td{border-bottom:none}
.holdings-table td:nth-child(2),.holdings-table th:nth-child(2){text-align:right}
.holdings-table .total-row td{border-top:2px solid var(--line);border-bottom:none;font-weight:700;background:var(--surface-2)}
.holdings-table .total-row td:first-child{color:var(--ink)}
.trade-toggle{display:flex;align-items:center;gap:18px;flex-wrap:wrap;background:var(--surface);border:1px solid var(--line);border-radius:var(--radius);padding:11px 16px;margin-bottom:14px}
.tm-switch{display:inline-flex;align-items:center;gap:8px;font-size:13px;font-weight:600;color:var(--ink);cursor:pointer;user-select:none}
.tm-switch input{width:16px;height:16px;accent-color:var(--accent);cursor:pointer}
.tm-legend{display:inline-flex;align-items:center;gap:6px;font-size:11.5px;color:var(--ink-2)}
.tm-pin{display:inline-grid;place-items:center;width:18px;height:18px;border-radius:50%;border:1px solid #000;color:#fff;font-size:10px;font-weight:700;margin-right:3px}
.tm-pin.buy{background:var(--up)} .tm-pin.sell{background:var(--down)}
.ai-grid{display:grid;grid-template-columns:1fr 1fr;gap:14px}
.ai-box{border:1px solid var(--line);border-radius:11px;padding:16px;position:relative;overflow:hidden;background:linear-gradient(180deg,var(--surface),var(--surface-2))}
.ai-box::before{content:"";position:absolute;left:0;top:0;bottom:0;width:3px;background:linear-gradient(var(--accent),#5fd3ff)}
.ai-box.oper::before{background:linear-gradient(var(--gold),#f0c060)}
.ai-head{display:flex;align-items:center;gap:8px;margin-bottom:10px;font-size:13px;font-weight:700;color:var(--accent-2)}
.ai-box.oper .ai-head{color:var(--gold)}
.ai-mono{width:21px;height:21px;border-radius:6px;background:linear-gradient(135deg,#3a63d8,#4d7fff);color:#fff;font-size:9.5px;font-weight:700;display:grid;place-items:center;font-family:var(--mono)}
.ai-box.oper .ai-mono{background:linear-gradient(135deg,var(--gold),#f0c060)}
.ai-text{font-size:12.5px;color:var(--ink);line-height:1.65}
.fund-strip{display:grid;grid-template-columns:repeat(6,1fr);gap:1px;background:var(--line);border:1px solid var(--line);border-radius:11px;overflow:hidden;margin-bottom:16px}
.fund-cell{background:var(--surface-2);padding:12px 14px}
.fund-cell .k{font-size:10px;color:var(--ink-3);font-weight:600;text-transform:uppercase;letter-spacing:.05em}
.fund-cell .v{font-size:15px;font-weight:600;margin-top:5px;font-family:var(--mono);color:var(--ink)}
.opt-grid{display:grid;grid-template-columns:repeat(3,1fr);gap:1px;background:var(--line);border:1px solid var(--line);border-radius:11px;overflow:hidden}
.opt-cell{background:var(--surface-2);padding:11px 13px}
.opt-cell .k{font-size:10px;color:var(--ink-3);font-weight:600}
.opt-cell .v{font-size:15px;font-weight:600;margin-top:4px;font-family:var(--mono)}
.sector-ctrl{display:inline-flex;align-items:center;background:var(--surface-2);border:1px solid var(--line);border-radius:6px;padding:2px;gap:2px}
.sector-btn{background:transparent;border:0;color:var(--ink-2);font-family:inherit;font-size:11px;font-weight:600;padding:5px 12px;border-radius:4px;cursor:pointer;transition:.15s;white-space:nowrap}
.sector-btn.on{background:var(--accent);color:#fff;box-shadow:0 0 8px rgba(77,127,255,.35)}
.sector-btn:not(.on):hover{color:var(--ink);background:rgba(255,255,255,.03)}
.sector-sep{width:1px;height:14px;background:var(--line);margin:0 4px}
.sector-play.playing{background:var(--gold);color:#0a0e15;box-shadow:0 0 10px rgba(224,168,60,.45)}
.sector-play:not(.playing):hover{color:var(--gold)}
.news{margin-top:4px;border:1px solid var(--line);border-radius:11px;overflow:hidden}
.news summary{padding:12px 15px;background:var(--surface-2);cursor:pointer;font-size:12.5px;font-weight:700;color:var(--ink-2);list-style:none;display:flex;justify-content:space-between;align-items:center}
.news summary::-webkit-details-marker{display:none}
.news-list{padding:4px 15px 8px}
.news-item{padding:10px 0;border-bottom:1px dashed var(--line-2)}
.news-item:last-child{border-bottom:none}
.news-item .d{font-size:10.5px;color:var(--ink-3);font-family:var(--mono);margin-bottom:3px}
.news-item a{font-size:12.5px;font-weight:500;color:var(--ink);text-decoration:none}
.news-item a:hover{color:var(--accent-2)}
.app-layout{display:flex;gap:16px;align-items:flex-start}
.sidebar{width:280px;flex-shrink:0;position:sticky;top:20px;display:flex;flex-direction:column;gap:8px}
.sidebar-title{font-size:12px;font-weight:700;color:var(--ink-2);text-transform:uppercase;letter-spacing:.08em;padding:2px 2px 4px;display:flex;justify-content:space-between;align-items:center}
.side-add{display:flex;gap:5px}
.side-add input{width:72px;padding:6px 8px;border:1px solid var(--line);border-radius:7px;font-size:12px;font-family:var(--mono);outline:none;background:var(--surface-2);color:var(--ink)}
.side-add input::placeholder{color:var(--ink-3)}
.side-add input:focus{border-color:var(--accent);box-shadow:0 0 0 3px rgba(77,127,255,.15)}
.side-add button{background:linear-gradient(135deg,#3a63d8,#4d7fff);color:#fff;border:none;padding:6px 11px;border-radius:7px;font-size:12px;font-weight:600;cursor:pointer}
.sidebar-item{background:var(--surface);border:1px solid var(--line);border-radius:11px;padding:11px 13px;cursor:pointer;transition:.16s;position:relative}
.sidebar-item::before{content:"";position:absolute;left:0;top:13px;bottom:13px;width:3px;border-radius:3px;background:transparent;transition:.16s}
.sidebar-item:hover{border-color:#2b3645}
.sidebar-item.active{border-color:var(--accent-dim);background:linear-gradient(var(--surface),var(--surface-3))}
.sidebar-item.active::before{background:linear-gradient(var(--accent),#5fd3ff)}
.nav-top{display:flex;justify-content:space-between;align-items:baseline;font-size:13.5px;font-weight:700}
.nav-top .pct{font-family:var(--mono);font-size:12.5px}
.nav-bottom{display:flex;justify-content:space-between;font-size:11px;color:var(--ink-2);margin-top:5px}
.nav-bottom .score{font-family:var(--mono)}
.side-del{position:absolute;top:8px;right:8px;width:20px;height:20px;text-align:center;line-height:20px;border-radius:4px;color:var(--ink-3);font-size:12px;font-weight:bold;cursor:pointer;z-index:5}
.side-del:hover{color:var(--ink);background:var(--surface-3)}
.rbadge{display:inline-flex;align-items:center;gap:4px;font-size:10.5px;font-weight:600;padding:2px 7px;border-radius:6px}
.rbadge.sb{background:var(--up-soft);color:var(--up)} .rbadge.b{background:rgba(142,198,63,.15);color:#8ec63f}
.rbadge.n{background:var(--surface-3);color:var(--ink-2)} .rbadge.s{background:rgba(255,138,107,.14);color:#ff8a6b} .rbadge.ss{background:var(--down-soft);color:var(--down)}
.main-content{flex:1;min-width:0}
.stock-card{display:none;background:var(--surface);border:1px solid var(--line);border-radius:var(--radius);overflow:hidden;box-shadow:var(--shadow)}
.stock-card.active{display:block}
.sc-body{padding:22px}
.sc-header{display:flex;justify-content:space-between;align-items:flex-start;flex-wrap:wrap;gap:14px;padding-bottom:16px;border-bottom:1px solid var(--line);margin-bottom:16px}
.sc-id{font-size:11px;font-weight:600;color:var(--accent);font-family:var(--mono);letter-spacing:.05em}
.sc-name{font-size:21px;font-weight:800;letter-spacing:-.01em;margin-top:2px;display:flex;align-items:baseline;gap:12px;flex-wrap:wrap}
.sc-price{font-family:var(--mono);font-size:18px;font-weight:600}
.sc-meta{display:flex;gap:22px;align-items:center}
.sc-meta .block{text-align:right}
.sc-meta .k{font-size:10px;color:var(--ink-3);text-transform:uppercase;letter-spacing:.07em;margin-bottom:4px}
.sc-meta .v{font-size:15px;font-weight:700}
.sc-meta .v.target{font-family:var(--mono);color:var(--accent-2)}
.grid-2{display:grid;grid-template-columns:1fr 1fr;gap:16px}
.fold{border:1px solid var(--line);border-radius:11px;margin-bottom:12px;overflow:hidden;margin-top:12px}
.fold>summary{padding:11px 14px;background:var(--surface-2);cursor:pointer;font-size:12.5px;font-weight:700;color:var(--ink-2);list-style:none;display:flex;justify-content:space-between;align-items:center;user-select:none}
.fold>summary::-webkit-details-marker{display:none}
.fold>summary::after{content:"▾";font-size:11px;color:var(--ink-3);transition:.2s}
.fold:not([open])>summary::after{transform:rotate(-90deg)}
.fold>summary:hover{color:var(--accent-2)}
.chevron{font-size:10px;color:var(--up);margin-right:8px;transition:.2s;flex-shrink:0}
.stock-card.expanded .chevron{transform:rotate(90deg)}
.mobile-only{display:none}.desktop-only{display:block}
@media(max-width:980px){
  .metrics{grid-template-columns:repeat(2,1fr)}
  .grid-2-market{grid-template-columns:1fr}
  .fund-strip,.opt-grid{grid-template-columns:repeat(3,1fr)}
  .yields-row{grid-template-columns:repeat(2,1fr)}
  .grid-2,.ai-grid{grid-template-columns:1fr}
  .rating-grid{grid-template-columns:repeat(2,1fr)}
  .sidebar{display:none}
  .main-content{width:100%}
  .desktop-only{display:none !important}
  .mobile-only{display:flex}
  .stock-card{display:block;margin-bottom:8px;border-radius:11px}
  .sc-body{padding:0}
  .sc-header{padding:14px;cursor:pointer}
  .sc-detail{display:none;padding:14px;padding-top:0;border-top:1px solid var(--line);background:var(--surface-2)}
  .stock-card.expanded .sc-detail{display:block}
  .sc-name{font-size:16px}
}
"""

def generate_holdings_section(ibkr):
    """IBKR 持股明細頁：帳戶總覽 + 持股表"""
    positions = sorted(ibkr.get("positions", []),
                       key=lambda p: p.get("market_value", 0), reverse=True)
    summ = ibkr.get("summary", {})
    fetched = ibkr.get("fetched_at", "")

    total_mv = sum(p.get("market_value", 0) for p in positions)
    total_upnl = sum(p.get("unrealized_pnl", 0) for p in positions)
    total_cost = total_mv - total_upnl
    total_ret = (total_upnl / total_cost * 100) if total_cost else 0

    def money(v):
        try:
            return f"${v:,.2f}"
        except (TypeError, ValueError):
            return "-"

    def metric_card(label, value, cls=""):
        return (f'<div class="metric {cls}"><div class="label">{label}</div>'
                f'<div class="value num {cls}">{value}</div></div>')

    upnl_cls = "up" if total_upnl >= 0 else "down"
    cards = (
        metric_card("淨清算價值 Net Liq", money(summ.get("net_liquidation")), "accent")
        + metric_card("持倉市值 Positions", money(summ.get("gross_position_value") or total_mv))
        + metric_card("總未實現損益", f'{total_upnl:+,.2f} ({total_ret:+.2f}%)', upnl_cls)
        + metric_card("現金 / 股利", f'{money(summ.get("total_cash_value"))} · 股利 {money(summ.get("dividends"))}')
    )

    rows = ""
    for p in positions:
        sym = p.get("symbol", "")
        qty = p.get("position", 0)
        avg = p.get("average_price", 0)
        mp = p.get("market_price", 0)
        mv = p.get("market_value", 0)
        upnl = p.get("unrealized_pnl", 0)
        cost = mv - upnl
        ret = (upnl / cost * 100) if cost else 0
        cls = "up" if upnl >= 0 else "down"
        rows += (
            f'<tr><td>{sym}</td>'
            f'<td>{qty:g}</td>'
            f'<td>${avg:,.2f}</td>'
            f'<td>${mp:,.2f}</td>'
            f'<td>${mv:,.2f}</td>'
            f'<td class="{cls}">{upnl:+,.2f}</td>'
            f'<td class="{cls}">{ret:+.2f}%</td></tr>'
        )
    total_cls = "up" if total_upnl >= 0 else "down"
    rows += (
        f'<tr class="total-row"><td>合計 ({len(positions)} 檔)</td><td></td><td></td><td></td>'
        f'<td>${total_mv:,.2f}</td>'
        f'<td class="{total_cls}">{total_upnl:+,.2f}</td>'
        f'<td class="{total_cls}">{total_ret:+.2f}%</td></tr>'
    )

    return f"""<div class="section-head"><span class="eyebrow">IBKR Portfolio</span><h2>持股明細</h2></div>
    <div class="metrics metrics-4">{cards}</div>
    <div class="card">
      <div class="card-title"><span>持股部位</span>
        <span style="font-size:11px;color:var(--ink-3);font-weight:500">資料快照 {fetched}</span></div>
      <div style="overflow-x:auto">
      <table class="dtable holdings-table">
        <tr><th>代號</th><th>股數</th><th>均價</th><th>現價</th><th>市值</th><th>未實現損益</th><th>報酬率</th></tr>
        {rows}
      </table>
      </div>
      <div style="font-size:11px;color:var(--ink-3);margin-top:12px;line-height:1.6">
        綠漲紅跌 (US convention)。資料為 IBKR 帳戶快照，於建置時讀取 <code>ibkr_data.json</code>。
        個股 K 線圖上可勾選「顯示進出標記」檢視實際買賣點。
      </div>
    </div>"""

def generate_html(stocks_data, options_data, fund_data, md):
    update_time = now_et().strftime("%Y-%m-%d %H:%M")
    market_section = generate_market_section(md)
    rating_table = generate_rating_table(stocks_data)

    ibkr = load_ibkr_data()
    trade_markers = build_trade_markers(ibkr)
    has_ibkr = bool(ibkr.get("positions"))
    holdings_section = generate_holdings_section(ibkr) if has_ibkr else ""

    sidebar_items, stock_cards = "", ""
    first = True
    for tk, data in stocks_data.items():
        data["_first"] = first
        stock_cards += generate_stock_card(tk, data, options_data.get(tk), fund_data.get(tk, {}))
        r = data["rating"]; rk = r.get("rating_key", "n")
        cls = "up" if data.get("change_pct", 0) >= 0 else "down"
        sign = "+" if data.get("change_pct", 0) >= 0 else ""
        sidebar_items += f'''
        <div class="sidebar-item {"active" if first else ""}" id="nav_{tk}" onclick="showStock('{tk}')">
            <div class="side-del" onclick="confirmDelete(event,'{tk}',this)" title="移除 {tk}">✖</div>
            <div class="nav-top"><span>{tk}</span><span class="pct {cls}">{sign}{data.get("change_pct",0):.2f}%</span></div>
            <div class="nav-bottom"><span class="rbadge {rk}">★ {r.get("rating","")}</span><span class="score">技{r.get("tech",0):g}/籌{r.get("chip",0):g}</span></div>
        </div>'''
        first = False

    chart_scripts = generate_chart_scripts(stocks_data, options_data, md, trade_markers)

    holdings_tab_btn = ('<button class="tab-btn" onclick="switchTab(\'tab-holdings\', this)">持股明細</button>'
                        if has_ibkr else "")
    holdings_tab_content = (f'<div id="tab-holdings" class="tab-content">{holdings_section}</div>'
                            if has_ibkr else "")
    trade_toggle = ('''
        <div class="trade-toggle">
          <label class="tm-switch"><input type="checkbox" id="tmChk" checked onchange="toggleTradeMarkers(this)"> 顯示進出標記</label>
          <span class="tm-legend"><span class="tm-pin buy">B</span>買進　<span class="tm-pin sell">S</span>賣出</span>
        </div>''' if has_ibkr else "")

    return f"""<!DOCTYPE html>
<html lang="zh-TW">
<head>
<meta charset="UTF-8">
<meta name="viewport" content="width=device-width, initial-scale=1.0">
<title>美股監控儀表板</title>
<link rel="preconnect" href="https://fonts.googleapis.com">
<link rel="preconnect" href="https://fonts.gstatic.com" crossorigin>
<link href="https://fonts.googleapis.com/css2?family=Manrope:wght@400;500;600;700;800&family=IBM+Plex+Mono:wght@400;500;600&family=Noto+Sans+TC:wght@400;500;700&display=swap" rel="stylesheet">
<style>
{get_css()}
</style>
</head>
<body>
<div class="container">
  <div class="header" id="top">
    <div><h1>🇺🇸 美股監控儀表板</h1><div class="update-time">最後更新 {update_time} ET · 即時連線</div></div>
    <button id="runBtn" class="btn-run" onclick="triggerAction(this)">⟳ 重新抓取最新資料</button>
  </div>
  <div class="tabs-container">
      <button class="tab-btn active" onclick="switchTab('tab-market', this)">大盤總覽</button>
      <button class="tab-btn" onclick="switchTab('tab-rating', this)">綜合評等</button>
      <button class="tab-btn" onclick="switchTab('tab-stocks', this)">追蹤個股分析</button>
      {holdings_tab_btn}
  </div>
  <div id="tab-market" class="tab-content active">{market_section}</div>
  <div id="tab-rating" class="tab-content">{rating_table}</div>
  <div id="tab-stocks" class="tab-content">
      <div class="app-layout">
        <div class="sidebar desktop-only">
            <div class="sidebar-title"><span>追蹤清單</span>
                <div class="side-add"><input type="text" id="stockInput" class="num" placeholder="如 TSLA"><button onclick="manageStock('add',null,'stockInput',this)">新增</button></div>
            </div>
            {sidebar_items}
        </div>
        <div class="main-content">{trade_toggle}{stock_cards}</div>
      </div>
  </div>
  {holdings_tab_content}
</div>
<button id="backToTop" onclick="window.scrollTo({{top:0,behavior:'smooth'}});" style="display:none;position:fixed;bottom:30px;right:30px;background:linear-gradient(135deg,#3a63d8,#4d7fff);color:#fff;border:none;border-radius:50px;padding:10px 18px;cursor:pointer;box-shadow:0 0 18px rgba(77,127,255,.5);z-index:9999;font-weight:bold;font-size:13px">↑ 返回頂部</button>
<script src="https://cdn.jsdelivr.net/npm/echarts@5.4.3/dist/echarts.min.js"></script>
<script>
var GAS_URL = '{GAS_URL}';
var TRIGGER_URL = '{TRIGGER_URL}';
{chart_scripts}

function resizeAllCharts(){{ setTimeout(function(){{ window.dispatchEvent(new Event('resize')); }}, 50); }}

function _resizeMarketCharts() {{
  setTimeout(function() {{
    var boxes = document.querySelectorAll('.chart-box');
    for (var j = 0; j < boxes.length; j++) {{
      var inst = typeof echarts !== 'undefined' && echarts.getInstanceByDom(boxes[j]);
      if (inst) inst.resize();
    }}
  }}, 120);
}}

function _activeIdx() {{
  var b = document.querySelector('#seg button.on');
  return b ? b.getAttribute('data-idx') : 'ndx';
}}

function switchIndex(btn) {{
  var idx = btn.getAttribute('data-idx');
  var segs = document.querySelectorAll('#seg button');
  for (var i = 0; i < segs.length; i++) segs[i].classList.remove('on');
  btn.classList.add('on');
  // 切換主圖
  var cards = document.querySelectorAll('.index-card');
  for (var i = 0; i < cards.length; i++) {{
    cards[i].style.display = (cards[i].getAttribute('data-idx') === idx) ? 'block' : 'none';
  }}
  // 切換 per-index 副圖 (KD / MACD), 依對應 chip 啟用狀態
  var subs = document.querySelectorAll('.index-sub');
  for (var i = 0; i < subs.length; i++) {{
    var p = subs[i];
    var key = p.classList.contains('kd-panel') ? 'kd' : (p.classList.contains('macd-panel') ? 'macd' : null);
    if (!key) continue;
    var chip = document.querySelector('.chip[data-k="' + key + '"]');
    var chipOn = chip && chip.classList.contains('on');
    p.style.display = (chipOn && p.getAttribute('data-idx') === idx) ? 'block' : 'none';
  }}
  _resizeMarketCharts();
}}

function toggleChip(el) {{
  el.classList.toggle('on');
  var k = el.getAttribute('data-k');
  var on = el.classList.contains('on');
  var panels = document.querySelectorAll('.' + k + '-panel');
  var idx = _activeIdx();
  for (var i = 0; i < panels.length; i++) {{
    var p = panels[i];
    if (p.classList.contains('index-sub')) {{
      // per-index 副圖: 須匹配當前指數
      p.style.display = (on && p.getAttribute('data-idx') === idx) ? 'block' : 'none';
    }} else {{
      p.style.display = on ? 'block' : 'none';
    }}
  }}
  // 更新計數
  var cnt = document.querySelectorAll('#chips .chip.on').length;
  var cntEl = document.getElementById('cnt');
  if (cntEl) cntEl.textContent = cnt + ' 個';
  _resizeMarketCharts();
}}

function switchTab(tabId, btn){{
    document.querySelectorAll('.tab-btn').forEach(function(b){{b.classList.remove('active');}});
    btn.classList.add('active');
    document.querySelectorAll('.tab-content').forEach(function(c){{c.classList.remove('active');}});
    document.getElementById(tabId).classList.add('active');
    resizeAllCharts();
}}

function showStock(tk){{
    if (window.innerWidth <= 980) return;
    document.querySelectorAll('.sidebar-item').forEach(function(el){{el.classList.remove('active');}});
    document.querySelectorAll('.stock-card').forEach(function(el){{el.classList.remove('active');}});
    document.getElementById('nav_'+tk).classList.add('active');
    document.getElementById('card_'+tk).classList.add('active');
    resizeAllCharts();
    window.scrollTo({{top: document.querySelector('.app-layout').offsetTop - 20, behavior: 'smooth'}});
}}

function toggleCard(tk){{
    if (window.innerWidth > 980) return;
    document.querySelectorAll('.stock-card').forEach(function(c){{
        if (c.id === 'card_'+tk) c.classList.toggle('expanded');
        else c.classList.remove('expanded');
    }});
    var card = document.getElementById('card_'+tk);
    if (card && card.classList.contains('expanded')){{
        resizeAllCharts();
        setTimeout(function(){{ card.scrollIntoView({{behavior:'smooth',block:'start'}}); }}, 100);
    }}
}}

function showCountdownToast(message, totalSeconds){{
    var existing = document.getElementById('custom-toast'); if (existing) existing.remove();
    var toast = document.createElement("div"); toast.id = 'custom-toast';
    toast.style.cssText = "position:fixed;top:20px;left:50%;transform:translateX(-50%);background:rgba(10,14,21,.96);color:#dde3ec;padding:20px 30px;border-radius:12px;z-index:9999;font-size:16px;box-shadow:0 8px 30px rgba(0,0,0,.5);text-align:center;min-width:280px;backdrop-filter:blur(6px);border:1px solid #1e2632;";
    document.body.appendChild(toast);
    var s = totalSeconds;
    var upd = function(){{ toast.innerHTML = '<div style="margin-bottom:10px;line-height:1.5;">'+message+'</div><div style="font-size:36px;font-weight:900;color:#22d39a;font-variant-numeric:tabular-nums;">'+s+'</div><div style="font-size:13px;color:#8b95a5;">秒後自動重新整理...</div>'; }};
    upd();
    var timer = setInterval(function(){{ s--; if(s<=0){{clearInterval(timer); toast.innerHTML="<div style='font-size:18px;font-weight:bold;color:#22d39a;'>🔄 重新整理中...</div>"; window.location.reload();}} else upd(); }}, 1000);
}}

function manageStock(action, tkOverride, inputId, btn){{
    if (!GAS_URL){{ alert("尚未設定 GAS_URL (環境變數 US_GAS_URL)，無法線上新增/刪除。"); return; }}
    var tk = tkOverride;
    if (!tk){{ var f = document.getElementById(inputId); if (f) tk = f.value.trim().toUpperCase(); }}
    if (!tk){{ alert("請輸入股票代號！"); return; }}
    var orig = btn ? btn.innerText : "";
    if (btn){{ btn.innerText = "⏳"; btn.style.pointerEvents = "none"; }}
    fetch(GAS_URL, {{method:'POST', body: JSON.stringify({{action:action, stock:tk}}), headers:{{"Content-Type":"text/plain;charset=utf-8"}}}})
    .then(function(r){{return r.text();}})
    .then(function(text){{
        if (text.indexOf("Error")>=0){{ alert("❌ 伺服器錯誤：\\n"+text); if(btn){{btn.innerText=orig;btn.style.pointerEvents="auto";}} }}
        else {{
            if(action==='add' && document.getElementById('stockInput')) document.getElementById('stockInput').value='';
            if (TRIGGER_URL) fetch(TRIGGER_URL, {{method:'POST', mode:'no-cors'}}).catch(function(e){{}});
            showCountdownToast("✅ "+tk+" 已"+(action==='add'?'新增':'刪除')+"！系統觸發重新抓取", 150);
        }}
    }})
    .catch(function(err){{ alert("❌ 網路請求失敗："+err.message); if(btn){{btn.innerText=orig;btn.style.pointerEvents="auto";}} }});
}}

function confirmDelete(event, tk, btn){{ event.stopPropagation(); if (confirm("確定要取消追蹤 "+tk+" 嗎？")) manageStock('remove', tk, null, btn); }}

function triggerAction(btn){{
    if (!TRIGGER_URL){{ alert("尚未設定 TRIGGER_URL (環境變數 US_TRIGGER_URL)。"); return; }}
    btn.innerText = "⏳ 觸發中..."; btn.style.pointerEvents = "none"; btn.style.opacity = "0.7";
    fetch(TRIGGER_URL, {{method:'POST', mode:'no-cors'}})
    .then(function(){{ showCountdownToast("✅ 重新執行指令已發送！系統正在抓取最新資料", 150); }})
    .catch(function(err){{ alert("❌ 發生錯誤，請檢查網路。"); }})
    .finally(function(){{ btn.innerText = "⟳ 重新抓取最新資料"; btn.style.pointerEvents = "auto"; btn.style.opacity = "1"; }});
}}

function toggleTradeMarkers(cb){{
    var show = cb.checked;
    if (!window._klineCharts) return;
    Object.keys(window._klineCharts).forEach(function(tk){{
        var ch = window._klineCharts[tk];
        if (!ch) return;
        var data = show ? ((window._tradeMarks && window._tradeMarks[tk]) || []) : [];
        ch.setOption({{ series: [{{ name: 'K線', markPoint: {{ data: data }} }}] }});
    }});
}}

window.onscroll = function(){{ document.getElementById('backToTop').style.display = (document.body.scrollTop>400||document.documentElement.scrollTop>400)?'block':'none'; }};
</script>
</body>
</html>"""

# =========================================================
# 主流程
# =========================================================
def send_telegram(text: str):
    if not TELEGRAM_BOT_TOKEN or not TELEGRAM_CHAT_ID: return
    try:
        requests.post(f"https://api.telegram.org/bot{TELEGRAM_BOT_TOKEN}/sendMessage",
                      data={"chat_id": TELEGRAM_CHAT_ID, "text": text[:4000], "parse_mode": "Markdown"}, timeout=10)
    except Exception:
        pass

def process_single_stock(ticker):
    print(f"  處理 {ticker}...")
    sd = get_stock_data(ticker)
    if not sd:
        return None, None, None, None
    fund = get_fundamentals(sd["info"])
    opt = get_options_data(ticker, sd["latest"]["close"])
    news = get_news(ticker)
    name = fund.get("name") or ticker

    prev_close = sd["prev"]["close"]
    change_pct = ((sd["latest"]["close"] - prev_close) / prev_close * 100) if prev_close else 0

    ai_tech, ai_oper = generate_ai_analysis(ticker, name, sd, fund, opt)

    record = {
        "ticker": ticker, "name": name, "df": sd["df"], "latest": sd["latest"], "prev": sd["prev"],
        "close_full": sd["close_full"],
        "indicators": sd["indicators"], "change_pct": change_pct, "news": news,
        "ai_tech": ai_tech, "ai_oper": ai_oper,
    }
    record["rating"] = calculate_rating(sd, fund, opt)
    print(f"    ✓ {ticker} {record['rating']['rating']} (技{record['rating']['tech']:g}/籌{record['rating']['chip']:g})")
    return ticker, record, opt, fund

def main():
    print(f'=== 美股監控機器人 ({now_et().strftime("%Y-%m-%d %H:%M")} ET) ===\n')
    os.makedirs(OUTPUT_DIR, exist_ok=True)

    print("[1/5] 平行抓取個股...")
    stocks_data, options_data, fund_data = {}, {}, {}
    with concurrent.futures.ThreadPoolExecutor(max_workers=3) as ex:
        futures = {ex.submit(process_single_stock, tk): tk for tk in STOCKS}
        for fut in concurrent.futures.as_completed(futures):
            tk = futures[fut]
            try:
                rid, record, opt, fund = fut.result()
                if rid and record:
                    stocks_data[rid] = record
                    if opt: options_data[rid] = opt
                    if fund: fund_data[rid] = fund
            except Exception as exc:
                print(f"  ⚠️ {tk} 錯誤: {exc}")

    print("\n[2/5] 計算殘差動能 (滾動雙因子回歸 vs SPY/類股 ETF)...")
    needed_etfs = {sector_etf_for(fund_data.get(tk, {})) for tk in stocks_data}
    needed_etfs.discard(None)
    factors = fetch_factor_closes(needed_etfs)
    mkt_close = factors.get(RM_MARKET_ETF)
    for tk, record in stocks_data.items():
        record["resid"] = None
        if mkt_close is None:
            continue
        try:
            sec_sym = sector_etf_for(fund_data.get(tk, {}))
            if tk == sec_sym:  # 類股 ETF 本身不對自己回歸
                sec_sym = None
            rm = compute_residual_momentum(record["close_full"], mkt_close, factors.get(sec_sym))
            rm["sector_etf"] = sec_sym if sec_sym in factors else None
            record["resid"] = rm
            rm_txt = f"{rm['rmom']:.2f}" if pd.notna(rm["rmom"]) else "-"
            print(f"  ✓ {tk} vs SPY{'+' + sec_sym if rm['sector_etf'] else ''} | rMOM {rm_txt} · {RM_SIGNAL_ZH[rm['signal']]}")
        except Exception as e:
            print(f"  ⚠️ {tk} 殘差動能計算失敗: {e}")

    print("\n[3/5] 抓取大盤/總經...")
    md = get_market_overview()
    print(f"  ✓ S&P {len(md.get('spx',[]))} 筆 · 類股 {len(md.get('sectors',[]))} · F&G {md.get('fear_greed',{}).get('score')}")

    ORDER = {"sb": 0, "b": 1, "n": 2, "s": 3, "ss": 4}
    stocks_data = dict(sorted(stocks_data.items(), key=lambda kv: (ORDER.get(kv[1]["rating"]["rating_key"], 9), -kv[1]["rating"]["total"])))

    print("\n[4/5] 生成 HTML...")
    html = generate_html(stocks_data, options_data, fund_data, md)
    with open(OUTPUT_FILE, "w", encoding="utf-8") as f:
        f.write(html)
    print(f"  ✓ {OUTPUT_FILE}")

    print("\n[5/5] 更新 GitHub Pages...")
    try:
        subprocess.run(["git", "config", "user.name", "github-actions[bot]"], check=True)
        subprocess.run(["git", "config", "user.email", "41898282+github-actions[bot]@users.noreply.github.com"], check=True)
        subprocess.run(["git", "add", OUTPUT_DIR], check=True)
        if subprocess.run(["git", "diff", "--staged", "--quiet"]).returncode == 0:
            print("  無變動")
        else:
            subprocess.run(["git", "commit", "-m", f"US update {now_et().strftime('%Y-%m-%d %H:%M')}"], check=True)
            subprocess.run(["git", "push"], check=True)
            print("  ✓ 已更新")
    except Exception as e:
        print(f"  ⚠️ Git 失敗: {e}")

    tg = f"🇺🇸 *美股監控* ({now_et().strftime('%m-%d')})\n\n"
    for tk, data in stocks_data.items():
        tg += f"*{tk}* ${data['latest']['close']:.2f} ({data['change_pct']:+.2f}%) | {data['rating']['rating']}\n"
    send_telegram(tg)
    print("\n✅ 完成！")

if __name__ == "__main__":
    main()
