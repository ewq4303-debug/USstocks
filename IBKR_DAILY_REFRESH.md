# IBKR 持股快照 — 每日刷新流程 (Runbook)

> 目的：每天把最新的 IBKR 持股與交易更新到 `ibkr_data.json`，
> 讓儀表板的「持股明細」分頁與 K 線圖的買賣進出標記保持最新。
>
> 背景：GitHub Actions 建置環境**無法直連 IBKR**，所以由 **Claude (透過 IBKR MCP) 定期執行本流程**，
> 將快照檔提交到 repo；US-stock 排程 Action 下次建置就會自動帶入。

## 觸發方式（建議）
在 Claude Code on the web 設定一個**每日排程 trigger**（美股交易日盤後，例如台灣時間隔日早上），
讓它開一個 session 並執行：「依 `IBKR_DAILY_REFRESH.md` 更新 IBKR 快照」。
排程/trigger 設定見 https://code.claude.com/docs/en/claude-code-on-the-web

Session 需具備：①IBKR MCP 已連線　②repo 寫入權限（push 到 `main`）。

## 執行步驟（Claude 照做）
1. 透過 IBKR MCP 取得三份原始回應：
   - `get_account_positions`
   - `get_account_trades`（period 用 `DAYS_90`，足夠涵蓋 K 線 ~120 交易日視窗）
   - `get_account_summary`
2. 把三份原始 JSON 分別存成 `positions.json` / `trades.json` / `summary.json`
   （或合併成一個 `raw.json`，內含 `{"positions":..,"trades":..,"summary":..}`）。
3. 產生快照：
   ```bash
   python build_ibkr_snapshot.py \
     --positions positions.json --trades trades.json --summary summary.json
   # 或：python build_ibkr_snapshot.py --raw raw.json
   ```
   產出 `ibkr_data.json`（含 `fetched_at` 時間戳）。
4. 提交並推送到 **main**（網站建置讀取 main 的 `ibkr_data.json`）：
   ```bash
   git add ibkr_data.json
   git commit -m "chore: refresh IBKR snapshot ($(date -u +%F))"
   git push
   ```
5. （可選）若要立即看到網頁更新，手動觸發 US-stock Action 或執行 `python us_stock_dark.py`。

## 驗證
```bash
python -c "import json;d=json.load(open('ibkr_data.json'));print(d['fetched_at'],len(d['positions']),'持股',len(d['trades']),'筆交易')"
```

## 注意
- 只有股票/ETF (STK) 會被保留；已清空的部位不在 positions，但其賣出交易仍會在圖上標記。
- `build_ibkr_snapshot.py` 只是「轉換器」：它**不**連 IBKR，原始資料一律由 Claude 用 MCP 取得後餵給它。
- 快照是某個時間點的靜態檔；網頁上的「資料快照 {fetched_at}」會顯示最後更新時間，方便判斷新鮮度。
