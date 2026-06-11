"""
build_ibkr_snapshot.py — 將 IBKR API 原始回應轉成 ibkr_data.json (儀表板讀取的快照格式)

用途
----
us_stock_dark.py 在建置 HTML 時讀取 ibkr_data.json,用來:
  1. 顯示「持股明細」分頁
  2. 在個股 K 線圖標記買賣進出點

GitHub Actions 建置環境無法直連券商,所以採「快照檔」機制:
在「能連到 IBKR 的環境」定期執行本程式產生 ibkr_data.json 並提交,
儀表板下次建置就會自動帶入最新持股與交易標記。

輸入 (擇一)
----------
1. 三個檔案: --positions positions.json --trades trades.json --summary summary.json
   (各為對應 IBKR API 工具的原始 JSON 回應)
2. 單一合併檔: --raw raw.json  內含 {"positions":..., "trades":..., "summary":...}

輸出
----
ibkr_data.json (預設) 或 --out 指定路徑。
"""

import json
import argparse
from datetime import datetime, timezone, timedelta

try:
    from zoneinfo import ZoneInfo
    ET = ZoneInfo("America/New_York")
except Exception:
    ET = timezone(timedelta(hours=-4))


def _num(v, default=None):
    try:
        return round(float(v), 6)
    except (TypeError, ValueError):
        return default


def transform_positions(raw):
    """IBKR get_account_positions 原始回應 → 精簡持股清單"""
    out = []
    for p in (raw or {}).get("positions", []):
        sym = (p.get("contract_description") or p.get("symbol") or "").upper()
        if not sym or (p.get("asset_class") and p["asset_class"] != "STK"):
            # 只保留股票/ETF
            if p.get("asset_class") and p["asset_class"] != "STK":
                continue
        out.append({
            "symbol": sym,
            "position": _num(p.get("position"), 0),
            "average_price": _num(p.get("average_price"), 0),
            "market_price": _num(p.get("market_price"), 0),
            "market_value": _num(p.get("market_value"), 0),
            "unrealized_pnl": _num(p.get("unrealized_pnl"), 0),
            "daily_pnl": _num(p.get("daily_pnl")),
            "currency": p.get("currency", "USD"),
        })
    out.sort(key=lambda x: x.get("market_value") or 0, reverse=True)
    return out


def transform_trades(raw):
    """IBKR get_account_trades 原始回應 → 精簡交易清單 (僅保留標記所需欄位)"""
    out = []
    for t in (raw or {}).get("trades", []):
        side = t.get("side")
        if side not in ("BUY", "SELL"):
            continue
        out.append({
            "symbol": (t.get("symbol") or "").upper(),
            "side": side,
            "size": _num(t.get("size"), 0),
            "price": _num(t.get("price"), 0),
            "trade_time": t.get("trade_time"),
        })
    return out


def transform_summary(raw):
    raw = raw or {}
    keys = ["currency", "net_liquidation", "gross_position_value", "total_cash_value",
            "available_funds", "buying_power", "dividends", "leverage"]
    out = {}
    for k in keys:
        v = raw.get(k)
        out[k] = v if k in ("currency", "leverage") else _num(v)
    return out


def build_snapshot(positions_raw, trades_raw, summary_raw, fetched_at=None):
    if fetched_at is None:
        fetched_at = datetime.now(ET).strftime("%Y-%m-%d %H:%M ET")
    return {
        "fetched_at": fetched_at,
        "summary": transform_summary(summary_raw),
        "positions": transform_positions(positions_raw),
        "trades": transform_trades(trades_raw),
    }


def _load(path):
    if not path:
        return {}
    with open(path, "r", encoding="utf-8") as f:
        return json.load(f)


def main():
    ap = argparse.ArgumentParser(description="Build ibkr_data.json snapshot from IBKR API responses")
    ap.add_argument("--positions")
    ap.add_argument("--trades")
    ap.add_argument("--summary")
    ap.add_argument("--raw", help="single file containing positions/trades/summary")
    ap.add_argument("--out", default="ibkr_data.json")
    ap.add_argument("--fetched-at", default=None)
    args = ap.parse_args()

    if args.raw:
        raw = _load(args.raw)
        positions_raw, trades_raw, summary_raw = raw.get("positions"), raw.get("trades"), raw.get("summary")
        # 容許 positions 直接是 list 或包成 {"positions":[...]}
        if isinstance(positions_raw, list):
            positions_raw = {"positions": positions_raw}
        if isinstance(trades_raw, list):
            trades_raw = {"trades": trades_raw}
    else:
        positions_raw = _load(args.positions)
        trades_raw = _load(args.trades)
        summary_raw = _load(args.summary)

    snap = build_snapshot(positions_raw, trades_raw, summary_raw, args.fetched_at)
    with open(args.out, "w", encoding="utf-8") as f:
        json.dump(snap, f, ensure_ascii=False, indent=2)
    print(f"✓ {args.out}: {len(snap['positions'])} 持股, {len(snap['trades'])} 筆交易, 快照時間 {snap['fetched_at']}")


if __name__ == "__main__":
    main()
