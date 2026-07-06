#!/usr/bin/env python3
"""SPEC_rating_v2 §8 — 綜合評等 v2 replay 回測驗證腳本。

對監控清單全部 tickers 拉 3 年日線（多抓 1 年做指標 warm-up），
逐日以主程式的指標函式（us_stock_dark.compute_indicator_frame /
indicators_from_row，非複製邏輯）重建 indicators，對每個交易日同時計算
v1（calculate_rating）與 v2（calculate_rating_v2）評等，做 forward
5/20/60 日報酬 event study。

*** 限制（不得宣稱驗證了全部十項） ***
fund / opt 無歷史可重放 → replay 全程 N/A：本回測只驗證
TREND + MOMENTUM（±11）的日頻部分；POSITIONING / SENTIMENT / VALUATION
未被驗證。v1 對照組同樣只吃技術面（chip 恆 0），故 v1 的評等分布整體
下移（v1 技術滿分 10 → 最高只能到「買進」），比較時以「同一資訊集」
解讀，不代表 v1 全資料下的分布。
評估日僅取指標完備日（MA200 / RSI / Vol_MA20 皆非 NaN），排除 warm-up。

通過標準（§8.3，不得自行放寬）：
1. v2 forward 20d median 依 sb > b > n > s > ss 單調（允許相鄰 ties，不允許逆序）
2. sb 與 ss 桶 forward 20d median 差 ≥ 2%
3. v2 月均評等翻轉 ≤ 4 次/檔

輸出：docs/rating_validation.html（ECharts dark 報告）、
docs/data/rating_validation.json（結構化結果）。
exit code：0 = 全部通過、2 = 任一未過、1 = 執行錯誤。
"""

import argparse
import json
import os
import sys

import numpy as np
import pandas as pd

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
import us_stock_dark as us  # noqa: E402  (需先補 sys.path)
import yfinance as yf  # noqa: E402

HORIZONS = (5, 20, 60)
KEY_ORDER = ["sb", "b", "n", "s", "ss"]  # 多方 → 空方
KEY_ZH = {"sb": "強力買進", "b": "買進", "n": "中性", "s": "減碼", "ss": "賣出", "na": "資料不足"}
MONO_HORIZON = 20        # 單調性檢定用 forward 20d
SPREAD_MIN_PP = 2.0      # sb−ss median 差 ≥ 2%（百分點）
FLIP_MAX_PER_MONTH = 4.0
TRADING_DAYS_PER_MONTH = 21
EVAL_YEARS = 2           # 只評估最近 2 年（前 1 年做指標 warm-up）


def replay_ticker(tk: str, period: str = "3y"):
    """回傳 per-day 紀錄 DataFrame（date, v1_key, v2_key, fwd{h}…）；失敗回 None。"""
    try:
        df = yf.Ticker(tk).history(period=period)
    except Exception as e:
        print(f"  ⚠️ {tk} 抓取失敗: {e}")
        return None
    if df is None or df.empty or len(df) < 60:
        print(f"  ⚠️ {tk} 資料不足，跳過")
        return None
    df = us.compute_indicator_frame(df[df["Close"] > 0].copy())
    close = df["Close"]
    fwd = {h: (close.shift(-h) / close - 1) * 100 for h in HORIZONS}

    cutoff = df.index[-1] - pd.Timedelta(days=EVAL_YEARS * 365)
    rows = []
    for i in range(1, len(df)):
        latest, prev = df.iloc[i], df.iloc[i - 1]
        if df.index[i] < cutoff:
            continue
        # 指標完備日才評估（排除 warm-up，兩套系統吃同一資訊集）
        if pd.isna(latest["SMA_200"]) or pd.isna(latest["RSI_14"]) or pd.isna(latest["Vol_MA20"]):
            continue
        data = {
            "latest": {"close": float(latest["Close"]),
                       "volume": int(latest["Volume"]) if pd.notna(latest["Volume"]) else 0,
                       "prev_close": float(prev["Close"])},
            "prev": {"close": float(prev["Close"])},
            "indicators": us.indicators_from_row(latest, prev),
        }
        v1 = us.calculate_rating(data, {}, None)          # fund/opt 無歷史 → 空
        v2 = us.calculate_rating_v2(data, {}, None)       # 同上 → 籌碼桶全 N/A
        rec = {"date": df.index[i].strftime("%Y-%m-%d"), "ticker": tk,
               "v1_key": v1["rating_key"], "v2_key": v2["rating_key"]}
        for h in HORIZONS:
            v = fwd[h].iloc[i]
            rec[f"fwd{h}"] = round(float(v), 4) if pd.notna(v) else None
        rows.append(rec)
    if not rows:
        return None
    return pd.DataFrame(rows)


def dist_table(df: pd.DataFrame, key_col: str, horizon: int) -> dict:
    """各評等桶 forward return 分布：n / mean / median / p25 / p75（%）。"""
    col = f"fwd{horizon}"
    out = {}
    for k in KEY_ORDER + ["na"]:
        s = df.loc[df[key_col] == k, col].dropna()
        if s.empty:
            out[k] = {"n": 0, "mean": None, "median": None, "p25": None, "p75": None}
        else:
            out[k] = {"n": int(len(s)), "mean": round(float(s.mean()), 3),
                      "median": round(float(s.median()), 3),
                      "p25": round(float(s.quantile(0.25)), 3),
                      "p75": round(float(s.quantile(0.75)), 3)}
    return out


def check_monotonic(table: dict):
    """sb ≥ b ≥ n ≥ s ≥ ss（median，允許 ties、不允許逆序）；空桶跳過。"""
    meds = [(k, table[k]["median"]) for k in KEY_ORDER if table[k]["n"] > 0]
    inversions = [f"{a}({va:+.2f}%) < {b}({vb:+.2f}%)"
                  for (a, va), (b, vb) in zip(meds, meds[1:]) if va < vb]
    return len(inversions) == 0, inversions, meds


def flip_stats(df: pd.DataFrame, key_col: str) -> dict:
    """每檔平均每月評等變更次數。"""
    out = {}
    for tk, g in df.sort_values("date").groupby("ticker"):
        keys = g[key_col].tolist()
        flips = sum(1 for a, b in zip(keys, keys[1:]) if a != b)
        months = max(len(keys) / TRADING_DAYS_PER_MONTH, 1e-9)
        out[tk] = round(flips / months, 2)
    return out


def bucket_share_series(df: pd.DataFrame, key_col: str):
    """每日各評等桶佔比（堆疊圖用）。"""
    keys = KEY_ORDER + ["na"]
    piv = df.pivot_table(index="date", columns=key_col, aggfunc="size", fill_value=0)
    piv = piv.reindex(columns=keys, fill_value=0)
    share = piv.div(piv.sum(axis=1), axis=0).round(4)
    return share.index.tolist(), {k: share[k].tolist() for k in keys}


# ---------------- 報告 ----------------

COLORS = {"sb": "#22d39a", "b": "#4d7fff", "n": "#8b95a5", "s": "#e0a83c", "ss": "#ff525b", "na": "#3a4353"}


def html_dist_table(v1: dict, v2: dict, horizon: int) -> str:
    rows = ""
    for k in KEY_ORDER + ["na"]:
        a, b = v1[k], v2[k]
        def cell(t):
            if t["n"] == 0:
                return "<td class='num dim'>-</td>" * 4 + "<td class='num dim'>0</td>"
            return (f"<td class='num'>{t['mean']:+.2f}</td><td class='num'><b>{t['median']:+.2f}</b></td>"
                    f"<td class='num dim'>{t['p25']:+.2f}</td><td class='num dim'>{t['p75']:+.2f}</td>"
                    f"<td class='num dim'>{t['n']}</td>")
        rows += (f"<tr><td><span class='dot' style='background:{COLORS[k]}'></span>{KEY_ZH[k]}</td>"
                 f"{cell(a)}{cell(b)}</tr>")
    return f"""<table class="t"><tr><th rowspan="2">評等</th>
<th colspan="5">v1 · forward {horizon}d (%)</th><th colspan="5">v2 · forward {horizon}d (%)</th></tr>
<tr>{'<th>mean</th><th>median</th><th>p25</th><th>p75</th><th>n</th>' * 2}</tr>{rows}</table>"""


def build_report(result: dict, share_v1, share_v2, out_html: str):
    dates1, s1 = share_v1
    dates2, s2 = share_v2
    verdicts = result["verdicts"]
    chips = "".join(
        f"<span class='chip {'ok' if v['passed'] else 'fail'}'>{'✓' if v['passed'] else '✗'} {v['name']}</span>"
        for v in verdicts.values())
    overall = all(v["passed"] for v in verdicts.values())
    tables = "".join(f"<h3>Forward {h} 日報酬分布（v1 vs v2）</h3>" +
                     html_dist_table(result["dist"]["v1"][str(h)], result["dist"]["v2"][str(h)], h)
                     for h in HORIZONS)
    mono_txt = "；".join(result["verdicts"]["monotonic"]["inversions"]) or "無逆序"
    flip_rows = "".join(
        f"<tr><td>{tk}</td><td class='num'>{result['flips']['v1'].get(tk, '-')}</td>"
        f"<td class='num'>{result['flips']['v2'][tk]}</td></tr>"
        for tk in sorted(result["flips"]["v2"], key=lambda t: -result["flips"]["v2"][t]))

    def stack_series(sdict):
        return json.dumps([{"name": KEY_ZH[k], "type": "line", "stack": "s", "areaStyle": {},
                            "showSymbol": False, "lineStyle": {"width": 0},
                            "itemStyle": {"color": COLORS[k]}, "data": sdict[k]}
                           for k in KEY_ORDER + ["na"]], ensure_ascii=False)

    html = f"""<!DOCTYPE html><html lang="zh-Hant"><head><meta charset="utf-8">
<meta name="viewport" content="width=device-width,initial-scale=1">
<title>綜合評等 v2 · replay 回測驗證</title>
<script src="https://cdn.jsdelivr.net/npm/echarts@5.5.1/dist/echarts.min.js"></script>
<style>
body{{background:#0a0e15;color:#dde3ec;font:14px/1.6 'SF Mono',Consolas,monospace;margin:0;padding:24px;max-width:1200px;margin:auto}}
h1{{font-size:20px}} h2{{font-size:16px;color:#8b95a5;border-bottom:1px solid #1e2632;padding-bottom:6px;margin-top:32px}}
h3{{font-size:14px;color:#8b95a5}}
.chip{{display:inline-block;padding:4px 12px;border-radius:14px;margin:4px 6px 4px 0;font-size:13px}}
.chip.ok{{background:rgba(34,211,154,.15);color:#22d39a;border:1px solid #22d39a}}
.chip.fail{{background:rgba(255,82,91,.15);color:#ff525b;border:1px solid #ff525b}}
.banner{{padding:12px 16px;border-radius:8px;margin:16px 0;font-weight:bold}}
.banner.ok{{background:rgba(34,211,154,.12);border:1px solid #22d39a;color:#22d39a}}
.banner.fail{{background:rgba(255,82,91,.12);border:1px solid #ff525b;color:#ff525b}}
.note{{background:#10161f;border-left:3px solid #e0a83c;padding:10px 14px;color:#8b95a5;font-size:13px}}
table.t{{border-collapse:collapse;width:100%;margin:12px 0;font-size:13px}}
table.t th,table.t td{{border:1px solid #1e2632;padding:5px 8px;text-align:left}}
table.t th{{background:#10161f;color:#8b95a5}} .num{{text-align:right}} .dim{{color:#5d6675}}
.dot{{display:inline-block;width:8px;height:8px;border-radius:50%;margin-right:6px}}
.chart{{height:320px;margin:12px 0}}
</style></head><body>
<h1>綜合評等 v2 · replay 回測驗證報告</h1>
<div class="note"><b>限制：</b>fund/opt 無歷史可重放 → 本回測只驗證 TREND + MOMENTUM（±11）的日頻部分；
POSITIONING / SENTIMENT / VALUATION 未被驗證。v1 對照組同樣只吃技術面（籌碼 0 分），
故 v1 分布整體下移，應以「同一資訊集」解讀。評估日僅取指標完備日（排除 warm-up）。</div>
<p>執行：{result['run_at']} · tickers {result['n_tickers']} · 樣本 {result['n_obs']:,} 個 (ticker, day) ·
區間 {result['date_range'][0]} ~ {result['date_range'][1]}</p>
<div class="banner {'ok' if overall else 'fail'}">{'✅ 驗收通過 — v2 可設為前端預設' if overall else '❌ 驗收未通過 — v2 不得設為前端預設（SPEC §8.3）'}</div>
<div>{chips}</div>
<h2>1. 通過標準明細</h2>
<table class="t"><tr><th>標準</th><th>要求</th><th>實際</th><th>結果</th></tr>
<tr><td>單調性</td><td>v2 forward {MONO_HORIZON}d median：sb ≥ b ≥ n ≥ s ≥ ss（允許 ties）</td>
<td>{mono_txt}</td><td>{'✓' if verdicts['monotonic']['passed'] else '✗'}</td></tr>
<tr><td>區分力</td><td>sb − ss median 差 ≥ {SPREAD_MIN_PP}%</td>
<td>{verdicts['spread']['detail']}</td><td>{'✓' if verdicts['spread']['passed'] else '✗'}</td></tr>
<tr><td>翻轉頻率</td><td>v2 月均評等變更 ≤ {FLIP_MAX_PER_MONTH} 次/檔</td>
<td>{verdicts['flips']['detail']}</td><td>{'✓' if verdicts['flips']['passed'] else '✗'}</td></tr></table>
<h2>2. 各評等桶 forward return 分布（v1 / v2 並排）</h2>{tables}
<h2>3. 評等桶佔比時序</h2>
<p class="note">重點檢查：回檔段前 v1 的買進佔比是否異常偏高、v2 是否提前出現減碼訊號。</p>
<h3>v2 每日評等佔比</h3><div id="c2" class="chart"></div>
<h3>v1 每日評等佔比（對照組）</h3><div id="c1" class="chart"></div>
<h2>4. 翻轉頻率（次/月）</h2>
<table class="t"><tr><th>Ticker</th><th>v1</th><th>v2</th></tr>{flip_rows}</table>
<script>
var AX={{type:'category',axisLabel:{{color:'#8b95a5'}},axisLine:{{lineStyle:{{color:'#2a323e'}}}}}};
var VAX={{type:'value',max:1,axisLabel:{{color:'#8b95a5',formatter:function(v){{return (v*100)+'%';}}}},splitLine:{{lineStyle:{{color:'#171e29'}}}}}};
function mk(id,dates,series){{
  var c=echarts.init(document.getElementById(id));
  c.setOption({{backgroundColor:'transparent',tooltip:{{trigger:'axis',backgroundColor:'rgba(10,14,21,.95)',borderColor:'#1e2632',textStyle:{{color:'#dde3ec'}},valueFormatter:function(v){{return (v*100).toFixed(1)+'%';}}}},
    legend:{{textStyle:{{color:'#8b95a5'}}}},grid:{{left:50,right:16,top:36,bottom:24}},
    xAxis:Object.assign({{data:dates}},AX),yAxis:VAX,series:series}});
  window.addEventListener('resize',function(){{c.resize();}});
}}
mk('c2',{json.dumps(dates2)},{stack_series(s2)});
mk('c1',{json.dumps(dates1)},{stack_series(s1)});
</script></body></html>"""
    with open(out_html, "w", encoding="utf-8") as f:
        f.write(html)


def main():
    ap = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    ap.add_argument("--period", default="3y", help="yfinance 抓取區間（預設 3y，前 1 年 warm-up）")
    ap.add_argument("--tickers", default=None, help="逗號分隔，預設 us_stocks.txt 清單")
    ap.add_argument("--out", default="docs/rating_validation.html")
    ap.add_argument("--json", default="docs/data/rating_validation.json")
    args = ap.parse_args()

    tickers = ([t.strip().upper() for t in args.tickers.split(",") if t.strip()]
               if args.tickers else list(dict.fromkeys(us.STOCKS)))
    print(f"=== 綜合評等 v2 replay 驗證 · {len(tickers)} tickers ===")

    frames = []
    for tk in tickers:
        print(f"  replay {tk}...")
        df = replay_ticker(tk, args.period)
        if df is not None:
            frames.append(df)
    if not frames:
        print("❌ 無任何 ticker 完成 replay")
        return 1
    all_df = pd.concat(frames, ignore_index=True)

    dist = {"v1": {str(h): dist_table(all_df, "v1_key", h) for h in HORIZONS},
            "v2": {str(h): dist_table(all_df, "v2_key", h) for h in HORIZONS}}

    # ---- §8.3 通過標準 ----
    t20 = dist["v2"][str(MONO_HORIZON)]
    mono_ok, inversions, meds = check_monotonic(t20)
    sb_m, ss_m = t20["sb"]["median"], t20["ss"]["median"]
    if sb_m is None or ss_m is None:
        spread_ok, spread_detail = False, "sb 或 ss 桶無樣本"
    else:
        spread = sb_m - ss_m
        spread_ok = spread >= SPREAD_MIN_PP
        spread_detail = f"sb {sb_m:+.2f}% − ss {ss_m:+.2f}% = {spread:.2f}pp"
    flips_v1, flips_v2 = flip_stats(all_df, "v1_key"), flip_stats(all_df, "v2_key")
    flip_avg = round(sum(flips_v2.values()) / len(flips_v2), 2)
    flips_ok = flip_avg <= FLIP_MAX_PER_MONTH
    flip_detail = f"平均 {flip_avg} 次/月（max {max(flips_v2.values()):.2f}）"

    result = {
        "run_at": us.now_et().strftime("%Y-%m-%d %H:%M ET"),
        "n_tickers": int(all_df["ticker"].nunique()),
        "n_obs": int(len(all_df)),
        "date_range": [all_df["date"].min(), all_df["date"].max()],
        "limitation": "fund/opt 無歷史 → 只驗證 TREND+MOMENTUM（±11）；v1 對照組同為技術面-only",
        "dist": dist,
        "flips": {"v1": flips_v1, "v2": flips_v2},
        "verdicts": {
            "monotonic": {"name": "forward 20d 單調性", "passed": bool(mono_ok),
                          "medians": [[k, m] for k, m in meds], "inversions": inversions},
            "spread": {"name": f"sb−ss 區分力 ≥ {SPREAD_MIN_PP}pp", "passed": bool(spread_ok),
                       "detail": spread_detail},
            "flips": {"name": f"月均翻轉 ≤ {FLIP_MAX_PER_MONTH}", "passed": bool(flips_ok),
                      "detail": flip_detail},
        },
    }
    result["passed"] = all(v["passed"] for v in result["verdicts"].values())

    os.makedirs(os.path.dirname(args.json), exist_ok=True)
    with open(args.json, "w", encoding="utf-8") as f:
        json.dump(result, f, ensure_ascii=False, indent=1, allow_nan=False)
    build_report(result, bucket_share_series(all_df, "v1_key"),
                 bucket_share_series(all_df, "v2_key"), args.out)

    print(f"\n  樣本 {result['n_obs']:,} · {result['date_range'][0]} ~ {result['date_range'][1]}")
    print(f"  [1] 單調性: {'PASS' if mono_ok else 'FAIL'} " +
          " > ".join(f"{k} {m:+.2f}%" for k, m in meds) +
          (f" · 逆序: {inversions}" if inversions else ""))
    print(f"  [2] 區分力: {'PASS' if spread_ok else 'FAIL'} {spread_detail}")
    print(f"  [3] 翻轉:   {'PASS' if flips_ok else 'FAIL'} {flip_detail}")
    print(f"  報告 → {args.out}\n  JSON → {args.json}")
    print(f"\n{'✅ 驗收通過：v2 可設為前端預設' if result['passed'] else '❌ 驗收未通過：v2 不得設為前端預設（SPEC §8.3）'}")
    return 0 if result["passed"] else 2


if __name__ == "__main__":
    sys.exit(main())
