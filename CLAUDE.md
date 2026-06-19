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
    訊號分類（pullback/overheat/strong/weak/neutral/no_signal）純由 rMOM×Z_short 客觀推導，
    進出邏輯規則寫在殘差動能 `i` 說明（`KINFO.resid`），系統不做手動標記/動作推導。
- 持股明細：讀 `ibkr_data.json` 顯示 IBKR 帳戶總覽與持股表
  - 帳戶淨值折線（`compute_portfolio_history()` 讀 `nav_history.json` 真實每日 NAV，
    從有記錄第一天起取**累積對數報酬** 100·ln(Vt/V0)（`_cum_ln`，起點 0）；IBKR 無歷史
    NAV 端點故由 Flex Web Service 每日累積，早期未累積區間留空白不回溯估值，需 ≥2 交易日才畫，
    `connectNulls:false`；`load_nav_history` 過濾 nav≤0（Flex 對未入金/未報告日回 0 的垃圾值）
  - 右上「比較大盤 / NASDAQ / 費半」切換鈕（`toggleNavCompare`）：切到 `compare` 視圖，從
    「最近一段連續日資料」起點（跳過孤立手動基準點，偵測 >10 天大間隔；例 6/4）起，
    帳戶淨值 / S&P500(^GSPC) / NASDAQ(^IXIC) / 費半(^SOX) 的累積對數報酬，起點皆 0、單位 %；
    `optSingle`/`optCompare` 兩組 option 以 `setOption(...,true)` 切換）
  - 持倉比例(1−現金) 折線（`pos_ratio = 1 − cash/nav`，逐日 cash 來自 Flex 報表；需 ≥2 個有 cash
    的交易日才畫，無 cash 的日子留空白）
  - `nav_history.json`：每日帳戶淨值累積檔 `{updated_at, account_id, currency, series:[{date, nav, cash?}]}`，
    由 `fetch_ibkr_nav.py` 經 IBKR Flex Web Service 每日 append（同日逐欄覆寫，series 依日期升冪）
  - `fetch_ibkr_nav.py`：Flex Web Service NAV 抓取器（SendRequest→GetStatement 兩段式，
    重試 1019 等暫時性錯誤、致命錯誤 1012/1015 直接 fail）；解析 EquitySummary 逐日 total+cash，
    退回 ChangeInNAV endingValue。規格見 `IBKR_FLEX_NAV.md`。
    GitHub Actions 用 secrets `IBKR_FLEX_TOKEN` / `IBKR_FLEX_QUERY_ID`，於建置前 best-effort 執行
  - 持倉配置圓餅（`build_alloc_data()`，依市值排序，超過前 14 檔併為「其他」）
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
