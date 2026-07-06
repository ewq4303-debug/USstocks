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
    （dates/cum_alpha/ma20/ma60/rolling_alpha/z_short/rmom/price/beta_mkt/r2 + signal/action，NaN→null；
    在 regime gating 之後輸出，action 為 gating 後最終值）
    訊號分類（pullback/overheat/strong/weak/neutral/no_signal）純由 rMOM×Z_short 客觀推導；
    標題在 `[訊號]` 後以富文本 `{a|→ 建議}`（`RM_ACTION_ZH`/`RM_ACTION_COLOR`）顯示客觀建議
    （加碼黃金點/部分調節/順勢續抱/減碼退出/觀望/不採信回歸），完整規則見 `i` 說明（`KINFO.resid`）；
    系統不做手動標記/動作推導。
- 綜合評等 v2（SPEC_rating_v2，與 v1 並行、v1 零改動）：`calculate_rating_v2()` 五桶對稱計分
  （TREND±6/MOMENTUM±5/POSITIONING±4/SENTIMENT±3/VALUATION±2，桶內 clamp）、
  每項三態 +/0/−/N/A、缺值重正規化（`score_norm = earned/available_max×20`，
  available<10 → `na` 資料不足不映射）；門檻集中 `RATING_V2_CONFIG`
  （環境變數 `RATING_V2_<KEY大寫>` 覆寫）；v1 現況與盲點見 `docs/SCORING_LOGIC.md`
  - 指標層（Phase 1）：`High_252`/`Low_252` 改用真實 High/Low（v1 T3 同步受惠）；
    `latest.prev_close`；`compute_indicator_frame()`/`indicators_from_row()` 可 import
    （replay 復用，禁止複製邏輯）；`rsi_raw`/`macd_hist_raw` 缺值保留 None 供 v2 判 N/A
    （v1 仍沿用預設 50/0）
  - 評等歷史 `docs/data/rating_history.json`：main 步驟 [1.5/5] 每日 upsert
    （同 ticker 同日覆寫冪等）、400 交易日裁剪、`target_price` 每日快照供 VA1
    30 日修訂方向（歷史未滿 30 日 VA1=N/A，靠重正規化吸收）；同時記 `v1_total` 供對照
  - **驗收 gate（SPEC §8.3）**：`scripts/validate_rating_v2.py` replay 回測
    （fund/opt 無歷史 → 只驗證 TREND+MOMENTUM ±11，v1 對照組同為技術面-only）→
    `docs/rating_validation.html` + `docs/data/rating_validation.json`。
    **2026-07-06 首驗三項全 FAIL**（30 檔×2年 14,795 樣本：20d median 單調性逆序
    sb<b、s<ss；sb−ss 區分力 0.39pp<2pp；月均翻轉 6.17>4）→ **v2 不得設為前端預設，
    Phase 5 前端凍結**；fwd5d 呈完美反序（ss 最強 +1.49%）= 多頭樣本短線均值回歸主導。
    調整門檻後以 `.github/workflows/rating-validation.yml`（合入 main 後可
    workflow_dispatch）重跑驗證，通過才做前端
  - pytest：`python -m pytest tests/test_rating_v2.py`（無網路，三態/死區邊界/clamp/
    重正規化/MIN_AVAILABLE/歷史冪等）
- 宏觀 Regime 層 v2（SPEC_macro_regime_v2，`macro_regime.py` 純計算模組 + `us_stock_dark.py` 膠水）：
  - 掛在殘差 pipeline 之後（main 步驟 2.5）：個股殘差（手選 ∪ ref_universe 共用快取）→
    Breadth_own/ref/Divergence → SOXX+跨板塊 sector 迴歸 → regime 狀態機 v2（含 WARMUP）→
    塌陷警報 → 讀 `macro_checkpoints.json` → action gating → `docs/data/macro_regime.json`（schema v2）
  - **regime 判定只用 sector_rMOM**（SOXX 對 SPY 單因子滾動回歸，重用
    `compute_residual_momentum(soxx, spy, None)`，shift(1) 防前視）；**Breadth 全面退出狀態機**
  - **狀態機 v2**（`REGIME_CONFIG_V2`）三態 WARMUP/NORMAL/BEAR：回填起點後前 60 交易日
    WARMUP（不判定、不 gating），結束當日以當日值定初始狀態（<1.0→BEAR 否則 NORMAL）；
    NORMAL→BEAR <1.0 連續 3 日、BEAR→NORMAL >1.3 連續 3 日（遲滯緩衝帶維持前狀態）；
    缺漏日不計入連續天數且計數不歸零；連續 stale>5 日 warning（warmup 前導 NaN 不算 stale）
  - **雙 Breadth**：own=手選清單、ref=`ref_universe.json`（人工維護，如 SOXX 前 30 大；
    **不用 yfinance 動態抓成分股**；缺檔或 <15 檔 → 對照層輸出 null 不中斷）；重疊 ticker
    共用殘差快取不重跑（`compute_ref_rmom`）；Divergence=own−ref（正=選股 alpha、
    持續轉負=選股風格失效，與宏觀敘事無關）
  - **塌陷警報**（`BREADTH_ALERT_CONFIG`，取代 0.30/0.45 絕對門檻）：own 與 ref 的 20 日差分
    同時 ≤−15pp 觸發 `BREADTH_CRASH`（10 交易日冷卻、單向發報），純警示不改 regime/action；
    展示層改畫 own 全歷史 p20/p60 分位帶（不進規則）
  - **跨板塊面板**（`CROSS_SECTOR_CONFIG`：SOXX/XLF/XLI/XLV/XLE 各對 SPY 單因子）：
    Systemic = count(sector_rMOM<0)÷板塊數（stale 板塊排除出分母，各板塊互不影響）
  - **action gating**：沿用 v1（個股規則零改動；BEAR 下 pullback→`frozen` 🔒，賣出永不被擋；
    優先序 `narrative.json` exit > regime FROZEN > 技術訊號）；**WARMUP 視同 NORMAL**
  - **敘事檢查點** `macro_checkpoints.json`（人工維護、可證偽清單）：只計 `evidence_count`
    並原樣傳遞，不參與狀態機；缺檔/格式錯誤 → evidence_count=null、pipeline 不中斷；
    triggered 非 bool 或 date 非 ISO → 跳過該筆記 warning
  - 前端（大盤總覽頂部）：Regime 橫幅（BEAR=暗紅、WARMUP=灰「資料累積中，gating 未啟動」、
    警報 chip）、三格共用 dataZoom 圖（sector_rMOM 1.0/1.3 門檻+緩衝帶 / 雙 Breadth+分位帶
    +🚨 警報垂直線（hover 顯示 own/ref 差分）/ Divergence 柱）、跨板塊共圖（SOXX 加粗+
    右側排序列表+Systemic chip ≥60% 轉紅）、§3.2 四象限判讀矩陣（當前組合高亮）；
    說明見 `KINFO.regime`
  - pytest 驗收：`python -m pytest tests/test_macro_regime.py`（無網路，涵蓋 v2 §8：
    WARMUP/Breadth 退出狀態機/遲滯/警報 AND+冷卻/Divergence/共用快取/ref 降級/
    跨板塊獨立性/schema v2）
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
- 組合績效評估（分頁「組合績效」）：風險調整後報酬比較自有組合 vs 大盤/類股基準
  - `compute_metrics.py`：排在 NAV pipeline 之後跑（Actions），讀 `nav_history.json` 真實 NAV 日報酬
    + yfinance（SPY/SOXX/QQQ/^IRX）→ 輸出 `docs/data/portfolio_metrics.json`。前端純讀 JSON 不算數值。
    指標分工：**Sharpe(年化)** 門面（+ Lo 2002 iid 信賴帶 ci95）、**Information Ratio vs SOXX** 選股能力
    （t_stat≡IR×√年數，|t|>2 顯著）、Sortino 加分、**Treynor 僅註腳**（強制帶警語、不得做主卡片）。
    年化因子一律 252；Rf 用 ^IRX/100（`rf_mode` 可切 `tbill_3m`/`zero`，抓取失敗退回 zero 並標 `fallback_used`）。
  - **§4 現金流污染（最高優先）**：`nav_history.json` 只有 nav+cash 餘額、**無外部 flow/TWR 欄位**，
    故 `r_t=NAV_t/NAV_{t-1}-1` 會被入金/出金污染 → 預設 `cashflow_adjusted:false`，儀表板顯眼警示。
    `nav_to_returns` 已預留校正路徑：series entry 一旦帶 `twr`（首選）或 `flow`（次選）即自動啟用並標 true。
  - 渲染 `generate_perf_section()`（卡片列 Sharpe/IR/CAGR/MDD + 比較表 + Treynor 摺疊註腳）
    + `generate_perf_chart_script()`（Rolling Sharpe 252 日滾動，portfolio/SPY/SOXX 多序列；不足窗回 null，
      全 null 時改顯「資料不足」註記）；NaN 一律轉 None（`allow_nan=False`）以免破壞瀏覽器 `JSON.parse`。
  - 自我驗收：`python compute_metrics.py --selftest`（無需網路，涵蓋 §9：t_stat 恆等式、IR 退化 null 分支、
    NAV→報酬三路徑、rolling null 數、SPY 波動 sanity）。

## IBKR 快照
- `ibkr_data.json`：IBKR 持股/交易快照（含 `fetched_at`）。Actions 無法直連券商，故用快照檔。
- `build_ibkr_snapshot.py`：把 IBKR API 原始回應轉成 `ibkr_data.json` 的轉換器（不連線）。
- **每日更新**：由 Claude 透過 IBKR MCP 定期執行 —— 步驟見 `IBKR_DAILY_REFRESH.md`。

## 開發慣例
- 綠漲紅跌 (US convention)。
- 改完用 `python -c "import ast;ast.parse(open('us_stock_dark.py').read())"` 檢查語法；
  可用合成資料對 `build_macro_axis` / `build_trade_markers` / `generate_holdings_section` 做單元測試。
  績效模組另有 `python compute_metrics.py --selftest`（無網路）驗證數值正確性。
