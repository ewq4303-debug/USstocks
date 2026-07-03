"""宏觀 Regime 層 v2（sector_rMOM 單獨判定 + 雙 Breadth + 跨板塊 + 塌陷警報）
— 見 SPEC_macro_regime_v2（取代 v1）。

v2 設計原則：
1. Regime 判定只用 sector_rMOM；Breadth 全面退出狀態機，降級為展示層指標與單向警報。
2. Breadth 只看變化率不看水位：絕對門檻 (0.30/0.45) 廢除，唯一規則化用途 = 快速塌陷警報。
3. 雙 Breadth 對照：手選清單版 (own) + 規則型對照 universe 版 (ref)，差值 Divergence 為獨立訊號
   （正值 = 選股 alpha；持續轉負 = 選股風格失效，與宏觀敘事無關）。
4. 跨板塊層只做 sector 級迴歸（各板塊 ETF vs SPY 單因子），不對非 AI 板塊個股跑殘差。
5. v1 不變：個股層決策規則零改動；BEAR 只凍結 BUY、永不產生賣出；checkpoints 與狀態機解耦。

本模組不做網路 I/O、不依賴 yfinance；所有迴歸（sector/跨板塊/ref universe 個股）由呼叫端
以既有 compute_residual_momentum 計算後把序列傳入。
"""

import os
import json
import math
import datetime as _dt

import numpy as np
import pandas as pd

# ===== v2 狀態機：三態 WARMUP / NORMAL / BEAR，只用 sector_rMOM =====
REGIME_CONFIG_V2 = {
    "warmup_days":            60,    # 序列（回填起點）前 60 個交易日一律 WARMUP，不判定
    "bear_enter_sector_rmom": 1.0,   # 進 BEAR：sector_rMOM(SOXX) < 1.0
    "bear_exit_sector_rmom":  1.3,   # 出 BEAR：sector_rMOM(SOXX) > 1.3
    "confirm_days":           3,     # 連續 N 交易日成立才翻轉
}

# 跨板塊 sector_rMOM 面板：各自對 SPY 做單因子滾動 OLS（SOXX 兼任 regime 判定）
CROSS_SECTOR_CONFIG = {
    "sectors": {
        "SOXX": "半導體",
        "XLF":  "金融",
        "XLI":  "工業",
        "XLV":  "醫療",
        "XLE":  "能源",
    }
}

# Breadth 塌陷警報（取代絕對門檻）：純警示，不改變 regime、不改變任何 action
BREADTH_ALERT_CONFIG = {
    "diff_window":   20,     # 20 個交易日差分
    "crash_pp":     -0.15,   # 差分 ≤ −15 個百分點觸發
    "scope":        "both",  # both = own 與 ref 同時觸發才算（降誤報）
    "cooldown_days": 10,     # 觸發後 10 個交易日內不重複發報
}

BREADTH_THRESHOLD = 1.0     # ticker.rMOM >= 1 視為寬度分子
REF_MIN_TICKERS = 15        # ref_universe tickers < 15 → 對照層整體輸出 null
MAX_STALE_DAYS = 5          # sector 連續 stale > 5 日 → warning（regime 自然凍結）

# 本系統的個股 action 以訊號鍵表示（us_stock_dark 的 signal → RM_ACTION_ZH）：
# "pullback"（加碼黃金點）為唯一買入訊號；"weak"（減碼/退出）為賣出訊號。
BUY_ACTIONS = frozenset({"pullback"})
ACTION_FROZEN = "frozen"


def compute_breadth_series(rmom_by_ticker: dict, threshold: float = BREADTH_THRESHOLD):
    """Breadth(t) = count(rMOM >= threshold) / count(當日 rMOM 非 NaN 的 ticker)。

    歷史不足（rMOM 為 NaN）的 ticker 不進分母，避免新股稀釋讀數。
    回傳 (breadth Series [0-1, 4 位小數], valid_count Series)。"""
    if not rmom_by_ticker:
        return pd.Series(dtype=float), pd.Series(dtype=int)
    df = pd.DataFrame(rmom_by_ticker)
    valid = df.notna().sum(axis=1)
    hits = (df >= threshold).sum(axis=1)  # NaN 比較為 False，不進分子
    breadth = (hits / valid).where(valid > 0).round(4)
    return breadth, valid


def run_regime_state_machine(sector_rmom: pd.Series, config: dict = None,
                             max_stale_days: int = MAX_STALE_DAYS):
    """三態 WARMUP / NORMAL / BEAR 狀態機 v2（遲滯 + confirm_days，只用 sector_rMOM）。

    - WARMUP：回填起點（第一個有效 sector_rMOM 日）後前 warmup_days 個交易日強制此狀態，
      不做 gating（買入正常顯示）；起點之前的前導 NaN 亦視為 WARMUP
    - WARMUP 結束當日：以當日 sector_rMOM 判定初始狀態（< enter → BEAR，否則 NORMAL）；
      該日缺值則順延至下一個有效日
    - NORMAL→BEAR：sector_rMOM < enter 連續 confirm_days 日
    - BEAR→NORMAL：sector_rMOM > exit 連續 confirm_days 日
    - 緩衝帶（enter–exit 之間）維持前狀態；條件中斷（有效日不滿足）→ 計數歸零
    - 缺漏（stale）日：不計入連續天數、計數不歸零；連續 stale > max_stale_days → warning
    - Breadth 不是任何轉移條件（v1 的 AND 條件全數刪除）
    - 禁止任何以單日跌幅、VIX 等快變數觸發翻轉的邏輯
    """
    cfg = dict(REGIME_CONFIG_V2)
    cfg.update(config or {})
    idx = sector_rmom.index
    first = sector_rmom.first_valid_index()
    first_pos = idx.get_loc(first) if first is not None else None

    state = "WARMUP"
    initialized = False
    states = []
    changed_at = None
    changed_readings = None
    cnt = 0
    stale_run = 0
    warned_this_streak = False
    warnings = []

    for i, d in enumerate(idx):
        v = sector_rmom.iloc[i]
        day_stale = bool(pd.isna(v))
        # stale 計數只在回填起點之後（warmup 前導 NaN 是「資料累積中」，不是 stale）
        if first_pos is not None and i > first_pos:
            if day_stale:
                stale_run += 1
                if stale_run > max_stale_days and not warned_this_streak:
                    warnings.append(
                        f"sector_rMOM 連續 stale {stale_run} 日 (>{max_stale_days})，"
                        f"regime 凍結於 {state}")
                    warned_this_streak = True
            else:
                stale_run = 0
                warned_this_streak = False

        in_warmup = first_pos is None or i < first_pos + cfg["warmup_days"]
        if in_warmup:
            states.append("WARMUP")
            continue
        if day_stale:  # 缺漏日不計入連續天數、計數不歸零、狀態不變
            states.append(state)
            continue
        val = float(v)
        if not initialized:  # WARMUP 結束當日（或其後第一個有效日）判定初始狀態
            state = "BEAR" if val < cfg["bear_enter_sector_rmom"] else "NORMAL"
            initialized = True
            states.append(state)
            continue

        if state == "NORMAL":
            cond = val < cfg["bear_enter_sector_rmom"]
        else:
            cond = val > cfg["bear_exit_sector_rmom"]
        cnt = cnt + 1 if cond else 0
        if cnt >= cfg["confirm_days"]:
            state = "BEAR" if state == "NORMAL" else "NORMAL"
            changed_at = d
            changed_readings = {"sector_rmom": round(val, 4)}
            cnt = 0
        states.append(state)

    latest_stale = bool(first_pos is not None and len(idx)
                        and pd.isna(sector_rmom.iloc[-1]))
    return {
        "states": pd.Series(states, index=idx, dtype=object),
        "regime": states[-1] if states else None,
        "regime_changed_at": changed_at,
        "changed_readings": changed_readings,
        "stale": latest_stale,
        "warnings": warnings,
    }


def load_ref_universe(path: str = "ref_universe.json", min_tickers: int = REF_MIN_TICKERS):
    """讀規則型對照 universe 定義檔（人工維護，如 SOXX 成分前 30 名）。

    不用 yfinance 動態抓 ETF 成分股（不可靠），一律讀此靜態檔。
    檔案缺失 / 格式錯誤 / tickers < min_tickers → None（對照層整體輸出 null，不中斷）。"""
    if not os.path.exists(path):
        return None
    try:
        with open(path, "r", encoding="utf-8") as f:
            data = json.load(f)
        if not isinstance(data, dict):
            return None
        tickers = []
        for t in (data.get("tickers") or []):
            t = str(t).strip().upper()
            if t and t not in tickers:
                tickers.append(t)
        if len(tickers) < min_tickers:
            return None
        return {"name": data.get("name"), "as_of": data.get("as_of"), "tickers": tickers}
    except Exception:
        return None


def detect_breadth_crash(breadth_own, breadth_ref, config: dict = None):
    """Breadth 快速塌陷警報（單向：只有塌陷發報，回升不發報）。

    BreadthCrash(t) = [own(t) − own(t−w) ≤ crash_pp] AND [ref(t) − ref(t−w) ≤ crash_pp]
    （scope="both"；ref 缺席時整體輸出 None）。觸發後 cooldown_days 個交易日內不重複發報。
    純警示：不改變 regime、不改變任何 action。"""
    cfg = dict(BREADTH_ALERT_CONFIG)
    cfg.update(config or {})
    if breadth_own is None or len(breadth_own) == 0:
        return None
    if cfg["scope"] == "both" and (breadth_ref is None or len(breadth_ref) == 0):
        return None
    w = int(cfg["diff_window"])
    d_own = breadth_own - breadth_own.shift(w)
    hits = d_own <= cfg["crash_pp"]
    d_ref = None
    if cfg["scope"] == "both":
        ref = breadth_ref.reindex(breadth_own.index)
        d_ref = ref - ref.shift(w)
        hits = hits & (d_ref <= cfg["crash_pp"])
    alerts = []
    last_pos = None
    for i, hit in enumerate(hits.to_numpy()):
        if not bool(hit):
            continue
        if last_pos is not None and (i - last_pos) <= int(cfg["cooldown_days"]):
            continue
        d = breadth_own.index[i]
        alerts.append({
            "type": "BREADTH_CRASH",
            "date": d.strftime("%Y-%m-%d") if hasattr(d, "strftime") else str(d),
            "own_diff": round(float(d_own.iloc[i]), 4),
            "ref_diff": round(float(d_ref.iloc[i]), 4) if d_ref is not None else None,
        })
        last_pos = i
    return alerts


def breadth_percentile_bands(breadth_own, q_lo: float = 0.2, q_hi: float = 0.6):
    """Breadth_own 全歷史 20/60 分位數（每日隨資料更新）。展示層參考，不進規則。"""
    if breadth_own is None:
        return None
    v = breadth_own.dropna()
    if v.empty:
        return None
    return {"p20": round(float(v.quantile(q_lo)), 4),
            "p60": round(float(v.quantile(q_hi)), 4)}


def compute_systemic(latest_rmom_by_sector: dict):
    """Systemic = count(sector_rMOM_i < 0) ÷ 板塊總數；stale（NaN/None）板塊排除出分母。"""
    vals = [v for v in latest_rmom_by_sector.values()
            if v is not None and not (isinstance(v, float) and math.isnan(v))]
    if not vals:
        return None
    return round(sum(1 for v in vals if v < 0) / len(vals), 4)


def load_checkpoints(path: str = "macro_checkpoints.json"):
    """讀敘事檢查點檔（人工維護）。只計 evidence_count 並原樣傳遞，不參與狀態機。

    檔案不存在 / 格式錯誤 → evidence_count = None，pipeline 不中斷。
    欄位驗證失敗（triggered 非 bool、date 非 ISO/None）→ 跳過該筆並記 warning。"""
    out = {"narrative": None, "updated_at": None, "checkpoints": [],
           "evidence_count": None, "warnings": []}
    if not os.path.exists(path):
        return out
    try:
        with open(path, "r", encoding="utf-8") as f:
            data = json.load(f)
        if not isinstance(data, dict):
            raise ValueError("root 必須是 object")
    except Exception as e:
        out["warnings"].append(f"macro_checkpoints 讀取失敗，忽略: {e}")
        return out
    out["narrative"] = data.get("narrative")
    out["updated_at"] = data.get("updated_at")
    valid = []
    for row in (data.get("checkpoints") or []):
        if not isinstance(row, dict) or not isinstance(row.get("triggered"), bool):
            out["warnings"].append(f"checkpoint 欄位驗證失敗，跳過: {row!r}")
            continue
        d = row.get("date")
        if d is not None:
            try:
                _dt.date.fromisoformat(str(d))
            except Exception:
                out["warnings"].append(f"checkpoint date 非 ISO 格式，跳過: {row!r}")
                continue
        valid.append(row)
    out["checkpoints"] = valid
    out["evidence_count"] = sum(1 for r in valid if r["triggered"])
    return out


def gate_action(action: str, regime: str, narrative_mode: str = None):
    """action gating（v1 §6 沿用，WARMUP 視同 NORMAL）。
    優先序：narrative exit 模式 > regime FROZEN > 技術訊號。

    - narrative exit 模式：禁止顯示任何買入訊號（買入 → 觀望）
    - regime BEAR：買入訊號改寫為 FROZEN（個股符合加碼條件但宏觀凍結買入）
    - 賣出 / 其餘 action 一律不變 —— regime 永不新增自動賣出。"""
    if action in BUY_ACTIONS:
        if narrative_mode == "exit":
            return "neutral"
        if regime == "BEAR":
            return ACTION_FROZEN
    return action


def _pairs(series: pd.Series, dec: int = 4):
    """Series → [[iso_date, value|null], ...]"""
    if series is None:
        return []
    out = []
    for d, v in series.items():
        ds = d.strftime("%Y-%m-%d") if hasattr(d, "strftime") else str(d)
        out.append([ds, round(float(v), dec) if pd.notna(v) else None])
    return out


def _last_valid(series: pd.Series, dec: int = 4):
    if series is None or len(series) == 0 or pd.isna(series.iloc[-1]):
        return None
    return round(float(series.iloc[-1]), dec)


def build_macro_regime(sector_rmom_series, sector_cum_alpha_series,
                       rmom_own_by_ticker, rmom_ref_by_ticker=None,
                       cross_sector=None, ref_name=None,
                       checkpoints_path: str = "macro_checkpoints.json",
                       config: dict = None, alert_config: dict = None):
    """組裝 macro_regime.json payload（schema v2）。

    - rmom_ref_by_ticker=None → 對照層（breadth_ref / divergence / alerts）輸出 null
    - cross_sector: [{"ticker","label","rmom_series"}, ...]（各板塊 vs SPY 單因子）
    - sector 層資料不足（序列缺失或全 NaN）→ 回傳 None（前端顯示「資料累積中」）。"""
    if (sector_rmom_series is None or len(sector_rmom_series) == 0
            or sector_rmom_series.dropna().empty):
        return None

    machine = run_regime_state_machine(sector_rmom_series, config=config)
    warnings = list(machine["warnings"])
    ck = load_checkpoints(checkpoints_path)
    warnings += ck["warnings"]

    breadth_own, valid_own = compute_breadth_series(rmom_own_by_ticker)
    ref_ok = bool(rmom_ref_by_ticker)
    breadth_ref = valid_ref = None
    if ref_ok:
        breadth_ref, valid_ref = compute_breadth_series(rmom_ref_by_ticker)
    div_series = None
    if ref_ok:
        div_series = (breadth_own - breadth_ref.reindex(breadth_own.index)).round(4)
    alerts = detect_breadth_crash(breadth_own, breadth_ref, alert_config) if ref_ok else None
    bands = breadth_percentile_bands(breadth_own)

    # 跨板塊面板 + Systemic（stale 板塊排除出分母，各板塊互不影響）
    cross_out = []
    latest_by_sector = {}
    for c in (cross_sector or []):
        s = c.get("rmom_series")
        last = _last_valid(s)
        latest_by_sector[c["ticker"]] = last
        cross_out.append({"ticker": c["ticker"], "label": c.get("label", c["ticker"]),
                          "rmom": last, "series": _pairs(s)})
    systemic = compute_systemic(latest_by_sector) if cross_out else None

    # 序列裁到第一個有效日，控制輸出體積
    starts = [s.first_valid_index() for s in (sector_rmom_series, breadth_own)
              if s is not None and len(s) and s.first_valid_index() is not None]
    start = min(starts) if starts else None

    def trim(s):
        if s is None:
            return None
        return s[s.index >= start] if start is not None else s

    idx = machine["states"].index
    sr_ffill = sector_rmom_series.ffill()
    as_of = idx[-1] if len(idx) else None

    def iso(d):
        return d.strftime("%Y-%m-%d") if d is not None and hasattr(d, "strftime") else (str(d) if d else None)

    own_last = _last_valid(breadth_own)
    ref_last = _last_valid(breadth_ref) if ref_ok else None
    payload = {
        "as_of": iso(as_of),
        "schema_version": 2,
        "regime": machine["regime"],
        "regime_changed_at": iso(machine["regime_changed_at"]),
        "regime_changed_readings": machine["changed_readings"],
        "sector_rmom": _last_valid(sr_ffill),
        "sector_rmom_series": _pairs(trim(sector_rmom_series)),
        "sector_cum_alpha_series": _pairs(trim(sector_cum_alpha_series)),
        "breadth_own": own_last,
        "breadth_ref": ref_last,
        "divergence": (round(own_last - ref_last, 4)
                       if own_last is not None and ref_last is not None else None),
        "breadth_own_series": _pairs(trim(breadth_own)),
        "breadth_ref_series": _pairs(trim(breadth_ref)) if ref_ok else None,
        "divergence_series": _pairs(trim(div_series)) if ref_ok else None,
        "breadth_pctile_bands": bands,
        "alerts": alerts,
        "cross_sector": cross_out,
        "systemic": systemic,
        "universe_own": {"valid": int(valid_own.iloc[-1]) if len(valid_own) else 0,
                         "total": len(rmom_own_by_ticker or {})},
        "universe_ref": ({"name": ref_name,
                          "valid": int(valid_ref.iloc[-1]) if len(valid_ref) else 0,
                          "total": len(rmom_ref_by_ticker)} if ref_ok else None),
        "narrative": ck["narrative"],
        "checkpoints_updated_at": ck["updated_at"],
        "evidence_count": ck["evidence_count"],
        "checkpoints": ck["checkpoints"],
        "flags": {"stale": machine["stale"], "ref_universe_ok": ref_ok,
                  "warmup": machine["regime"] == "WARMUP"},
        "config": dict(REGIME_CONFIG_V2, **(config or {})),
        "alert_config": dict(BREADTH_ALERT_CONFIG, **(alert_config or {})),
        "warnings": warnings,
    }
    return payload


def write_macro_regime_json(payload, path: str):
    """輸出 macro_regime.json；payload=None 時寫入「資料累積中」占位。"""
    os.makedirs(os.path.dirname(path) or ".", exist_ok=True)
    data = payload if payload is not None else {"schema_version": 2, "regime": None, "available": False}
    with open(path, "w", encoding="utf-8") as f:
        json.dump(data, f, ensure_ascii=False, separators=(",", ":"), allow_nan=False)
    return path
