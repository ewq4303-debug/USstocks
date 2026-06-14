# USstocks — 美股監控儀表板

`us_stock_dark.py` 產生 `docs/us.html`（深色終端風儀表板），由 GitHub Actions
(`.github/workflows/us-stock.yml`) 於美股交易日排程建置並推送到 GitHub Pages。
資料來源：yfinance + CNN Fear & Greed + IBKR 持股快照。

## 主要區塊
- 大盤總覽：指數、VIX、CNN F&G、10Y、DXY、殖利率、類股輪動
  - 總經副圖 (VIX/F&G/10Y/DXY) 共用 `build_macro_axis()` 的交易日軸，缺值留空白
- 綜合評等、追蹤個股分析（K 線 + MA/Supertrend + 殘差動能 + KD/MACD + 選擇權）
  - K 線上方自訂指標 chip 列（`toggleKChip`，每指標一鈕整組顯示/隱藏，ECharts 原生
    legend 隱藏改用 `legendSelect/UnSelect` dispatch）；每鈕附 `i` 鈕（`showKInfo`/`KINFO`）
    彈出定義 / 計算 / 進出判斷依據
  - tooltip 只顯示游標所屬副圖的指標：保留跨圖 crosshair（`axisPointer.link`），
    以 zrender mousemove + `containPixel` 記錄 `kc._hg`，自訂 formatter 依 `axisIndex`
    過濾；OHLC 存 2 位小數、α年化以 %、量千分位
  - 殘差動能（KD 之前的副圖）：`compute_residual_momentum()` 滾動雙因子回歸
    （SPY + 類股 ETF）剃 Beta 取殘差 ε；左軸 21 日 Z 值線 + rMOM(12-1月) 線
    + ±2 淡色陰影區 + 0 軸虛線、右軸「α年化」柱（半透明分色，軸/ tooltip 統一年化 %），
    標題顯示 β / R² / α年化 / rMOM / 訊號；
    `rolling_alpha` 內部一律存 mean(ε)，對外顯示一律 mean(ε)×252×100（標籤「α年化」）；
    防 look-ahead（係數只用 t-1 前資料）、ε 不扣 α̂
  - `write_residual_series_json()` 輸出 `docs/data/series/{TICKER}.json`
    （dates/cum_alpha/ma20/ma60/rolling_alpha/z_short/rmom/price/beta_mkt/r2，NaN→null）
- 持股明細：讀 `ibkr_data.json` 顯示 IBKR 帳戶總覽與持股表
  - 每日持股淨值 vs S&P 500 折線（`compute_portfolio_history()` 以目前持股數量回溯
    估值 Σ 股數×收盤，與 SPX 同期累積報酬 %；快照無歷史淨值故為回溯估值，網路失敗則略過）
  - 持倉比例圓餅（`build_alloc_data()`，依市值排序，超過前 14 檔併為「其他」）
  - 圖表 JS 由 `generate_holdings_chart_script()` 產生，div 隱藏於分頁，靠 `switchTab` 的
    `resizeAllCharts` 觸發 resize
- K 線進出標記：依 `ibkr_data.json` 的交易，按「每日×方向」VWAP 標在圖上，可勾選顯示/隱藏

## IBKR 快照
- `ibkr_data.json`：IBKR 持股/交易快照（含 `fetched_at`）。Actions 無法直連券商，故用快照檔。
- `build_ibkr_snapshot.py`：把 IBKR API 原始回應轉成 `ibkr_data.json` 的轉換器（不連線）。
- **每日更新**：由 Claude 透過 IBKR MCP 定期執行 —— 步驟見 `IBKR_DAILY_REFRESH.md`。

## 開發慣例
- 綠漲紅跌 (US convention)。
- 改完用 `python -c "import ast;ast.parse(open('us_stock_dark.py').read())"` 檢查語法；
  可用合成資料對 `build_macro_axis` / `build_trade_markers` / `generate_holdings_section` 做單元測試。
