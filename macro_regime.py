"""宏觀 Regime 層（sector_rMOM + Breadth + 敘事檢查點）— 見 SPEC_macro_regime。

掛在既有殘差動能 pipeline 之後執行。設計原則（最高優先級約束）：
1. 個股層 rMOM/Z_short 決策規則零改動。
2. regime 只影響「加碼許可」與「總曝險建議」，永遠不產生個股賣出訊號；
   BEAR 下唯一行為改變 = 凍結買入訊號的顯示（action gating）。
3. regime 判定只用慢變數（sector_rMOM、Breadth），單日價格變動不得直接翻轉。
4. 遲滯（hysteresis）：進出 BEAR 門檻不對稱，緩衝帶維持前狀態。
5. 敘事檢查點（macro_checkpoints.json）與量化 regime 解耦：只做展示與計數，
   不參與狀態機。

本模組不做網路 I/O、不依賴 yfinance；sector 迴歸（SOXX vs SPY 單因子）由呼叫端
以既有 compute_residual_momentum(SOXX, SPY, None) 計算後把序列傳入。
"""

import os
import json
import datetime as _dt

import numpy as np
import pandas as pd

REGIME_CONFIG = {
    "bear_enter_sector_rmom": 1.0,   # 進 BEAR：sector_rMOM < 1.0
    "bear_enter_breadth":     0.30,  # 且 Breadth < 0.30（AND 條件）
    "bear_exit_sector_rmom":  1.3,   # 出 BEAR：sector_rMOM > 1.3
    "bear_exit_breadth":      0.45,  # 且 Breadth > 0.45（AND 條件）
    "confirm_days":           3,     # 連續 N 個交易日滿足條件才翻轉
}

BREADTH_THRESHOLD = 1.0    # ticker.rMOM >= 1 視為寬度分子
MIN_UNIVERSE = 10          # 有效 ticker < 10 → low_sample，狀態機暫停翻轉
MAX_STALE_DAYS = 5         # sector 連續 stale > 5 日 → regime 凍結 + warning

# 本系統的個股 action 以訊號鍵表示（us_stock_dark 的 signal → RM_ACTION_ZH）：
# "pullback"（加碼黃金點）為唯一買入訊號；"weak"（減碼/退出）為賣出訊號。
BUY_ACTIONS = frozenset({"pullback"})
ACTION_FROZEN = "frozen"


def compute_breadth_series(rmom_by_ticker: dict, threshold: float = BREADTH_THRESHOLD,
                           min_universe: int = MIN_UNIVERSE):
    """Breadth(t) = count(rMOM >= threshold) / count(當日 rMOM 非 NaN 的 ticker)。

    歷史不足（rMOM 為 NaN）的 ticker 不進分母，避免新股稀釋讀數。
    回傳 (breadth Series [0-1, 4 位小數], valid_count Series, low_sample bool Series)。"""
    if not rmom_by_ticker:
        empty = pd.Series(dtype=float)
        return empty, pd.Series(dtype=int), pd.Series(dtype=bool)
    df = pd.DataFrame(rmom_by_ticker)
    valid = df.notna().sum(axis=1)
    hits = (df >= threshold).sum(axis=1)  # NaN 比較為 False，不進分子
    breadth = (hits / valid).where(valid > 0).round(4)
    low_sample = (valid > 0) & (valid < min_universe)
    return breadth, valid, low_sample


def run_regime_state_machine(sector_rmom: pd.Series, breadth: pd.Series,
                             config: dict = None, low_sample: pd.Series = None,
                             max_stale_days: int = MAX_STALE_DAYS):
    """兩態 NORMAL / BEAR 狀態機（遲滯 + confirm_days）。

    - NORMAL→BEAR：sector_rMOM < enter 且 Breadth < enter，連續 confirm_days 日
    - BEAR→NORMAL：sector_rMOM > exit 且 Breadth > exit，連續 confirm_days 日
    - 緩衝帶 / 條件中斷 → 計數歸零、狀態不變
    - 缺漏（stale）日：沿用前值展示、不計入連續天數、計數不歸零
    - sector 連續 stale > max_stale_days → regime 凍結 + warning
    - low_sample 日：Breadth 照算但暫停翻轉（計數不歸零）
    - 初始狀態（第一個有效日）：以當日條件判定，緩衝帶 → NORMAL
    - 禁止任何以單日跌幅、VIX 等快變數觸發翻轉的邏輯
    """
    cfg = dict(REGIME_CONFIG)
    cfg.update(config or {})
    idx = sector_rmom.index.union(breadth.index)
    sr_raw = sector_rmom.reindex(idx)
    sr = sr_raw.ffill()  # stale 日沿用前值（僅供展示/flags，不參與計數）
    br = breadth.reindex(idx)
    ls = (low_sample.reindex(idx).fillna(False).astype(bool)
          if low_sample is not None else pd.Series(False, index=idx))

    state = None
    states = []
    changed_at = None
    changed_readings = None
    cnt = 0
    stale_run = 0
    warned_this_streak = False
    seen_valid = False  # 形成期 warmup 的前導 NaN 是「資料累積中」，不是 stale
    warnings = []

    for i, d in enumerate(idx):
        day_stale = bool(pd.isna(sr_raw.iloc[i]))
        missing = day_stale or pd.isna(br.iloc[i])
        if day_stale:
            if seen_valid:
                stale_run += 1
                if stale_run > max_stale_days and not warned_this_streak:
                    warnings.append(
                        f"sector_rMOM 連續 stale {stale_run} 日 (>{max_stale_days})，"
                        f"regime 凍結於 {state or 'NORMAL'}")
                    warned_this_streak = True
        else:
            seen_valid = True
            stale_run = 0
            warned_this_streak = False

        if missing or (state is not None and bool(ls.iloc[i])):
            # 缺漏日不計入連續天數、計數不歸零；low_sample 日暫停翻轉
            states.append(state)
            continue

        s_val = float(sr.iloc[i])
        b_val = float(br.iloc[i])
        if state is None:  # 歷史回填第一天：以當日條件判定，緩衝帶 → NORMAL
            state = ("BEAR" if (s_val < cfg["bear_enter_sector_rmom"]
                                and b_val < cfg["bear_enter_breadth"]) else "NORMAL")
            states.append(state)
            continue

        if state == "NORMAL":
            cond = (s_val < cfg["bear_enter_sector_rmom"]
                    and b_val < cfg["bear_enter_breadth"])
        else:
            cond = (s_val > cfg["bear_exit_sector_rmom"]
                    and b_val > cfg["bear_exit_breadth"])
        cnt = cnt + 1 if cond else 0
        if cnt >= cfg["confirm_days"]:
            state = "BEAR" if state == "NORMAL" else "NORMAL"
            changed_at = d
            changed_readings = {"sector_rmom": round(s_val, 4), "breadth": round(b_val, 4)}
            cnt = 0
        states.append(state)

    latest_stale = bool(len(idx)) and bool(pd.isna(sr_raw.iloc[-1]))
    latest_low_sample = bool(len(idx)) and bool(ls.iloc[-1])
    return {
        "states": pd.Series(states, index=idx, dtype=object),
        "regime": state,
        "regime_changed_at": changed_at,
        "changed_readings": changed_readings,
        "stale": latest_stale,
        "low_sample": latest_low_sample,
        "warnings": warnings,
    }


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
    """action gating（§6）。優先序：narrative exit 模式 > regime FROZEN > 技術訊號。

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
    out = []
    for d, v in series.items():
        ds = d.strftime("%Y-%m-%d") if hasattr(d, "strftime") else str(d)
        out.append([ds, round(float(v), dec) if pd.notna(v) else None])
    return out


def build_macro_regime(sector_rmom_series, sector_cum_alpha_series, rmom_by_ticker,
                       checkpoints_path: str = "macro_checkpoints.json",
                       config: dict = None):
    """組裝 macro_regime.json payload（§8 schema）。

    sector 層資料不足（序列缺失或全 NaN）→ 回傳 None（前端顯示「資料累積中」）。"""
    breadth, valid_cnt, low_sample = compute_breadth_series(rmom_by_ticker)
    if (sector_rmom_series is None or len(sector_rmom_series) == 0
            or sector_rmom_series.dropna().empty):
        return None

    machine = run_regime_state_machine(sector_rmom_series, breadth,
                                       config=config, low_sample=low_sample)
    warnings = list(machine["warnings"])
    ck = load_checkpoints(checkpoints_path)
    warnings += ck["warnings"]

    # 序列裁到第一個有效日，控制輸出體積
    starts = [s.first_valid_index() for s in (sector_rmom_series, breadth)
              if s is not None and s.first_valid_index() is not None]
    start = min(starts) if starts else None

    def trim(s):
        if s is None:
            return pd.Series(dtype=float)
        return s[s.index >= start] if start is not None else s

    idx = machine["states"].index
    sr_last = sector_rmom_series.reindex(idx).ffill()
    changed_at = machine["regime_changed_at"]
    as_of = idx[-1] if len(idx) else None

    def iso(d):
        return d.strftime("%Y-%m-%d") if d is not None and hasattr(d, "strftime") else (str(d) if d else None)

    br_last = breadth.reindex(idx)
    payload = {
        "as_of": iso(as_of),
        "regime": machine["regime"],
        "regime_changed_at": iso(changed_at),
        "regime_changed_readings": machine["changed_readings"],
        "sector_rmom": (round(float(sr_last.iloc[-1]), 4)
                        if len(idx) and pd.notna(sr_last.iloc[-1]) else None),
        "sector_rmom_series": _pairs(trim(sector_rmom_series)),
        "sector_cum_alpha_series": _pairs(trim(sector_cum_alpha_series)),
        "breadth": (round(float(br_last.iloc[-1]), 4)
                    if len(idx) and pd.notna(br_last.iloc[-1]) else None),
        "breadth_series": _pairs(trim(breadth)),
        "universe_valid": int(valid_cnt.iloc[-1]) if len(valid_cnt) else 0,
        "universe_total": len(rmom_by_ticker),
        "narrative": ck["narrative"],
        "checkpoints_updated_at": ck["updated_at"],
        "evidence_count": ck["evidence_count"],
        "checkpoints": ck["checkpoints"],
        "flags": {"stale": machine["stale"], "low_sample": machine["low_sample"]},
        "config": dict(REGIME_CONFIG, **(config or {})),
        "warnings": warnings,
    }
    return payload


def write_macro_regime_json(payload, path: str):
    """輸出 macro_regime.json；payload=None 時寫入「資料累積中」占位。"""
    os.makedirs(os.path.dirname(path) or ".", exist_ok=True)
    data = payload if payload is not None else {"regime": None, "available": False}
    with open(path, "w", encoding="utf-8") as f:
        json.dump(data, f, ensure_ascii=False, separators=(",", ":"), allow_nan=False)
    return path
