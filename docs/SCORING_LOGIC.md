# 個股評分邏輯（綜合評等）整理

> 本文件把 `us_stock_dark.py` 內「綜合評等」的計分規則、輸入來源、門檻與已知盲點
> 完整攤開，方便交給 Claude / 他人評估檢討。所有內容忠實對應現行程式碼，
> 核心函式為 `calculate_rating(data, fund, opt)`（`us_stock_dark.py:1113`）。

> **狀態更新（2026-07-06）**：本文件 §5 的盲點已由 SPEC_rating_v2 回應——
> `calculate_rating_v2()` 五桶對稱計分已實作並與 v1 並行（後端每日輸出
> `docs/data/rating_history.json`），但 replay 回測驗收（§8.3）三項全數未過
> （見 `docs/rating_validation.html`），故 **前端仍以本文件的 v1 為預設**，
> v2 前端凍結至門檻調整後重驗通過。本文件保留為 v1 的現況紀錄。

---

## 0. 一句話總結

每檔股票以 **技術面（滿分 10）+ 籌碼面（滿分 10）= 總分 20** 打分，
再依總分落點映射到 5 級評等（強力買進 / 買進 / 中性 / 減碼 / 賣出）。
**所有計分項都是「符合條件就加分」，沒有任何扣分項**——這是最需要被檢討的設計特性（見 §5）。

---

## 1. 資料來源與輸入

三組輸入全部來自 **yfinance**，於每檔股票分析時即時抓取：

| 輸入 | 建構函式 | 內容 | 來源欄位 |
|---|---|---|---|
| `data`（含 `indicators`, `latest`） | 技術指標計算區塊 `us_stock_dark.py:600-641` | 收盤、量、MA/RSI/MACD/量能/52週高 | yfinance 歷史 K 線 |
| `fund` | `get_fundamentals()` `:646` | 法人持股、放空、目標價、PE 等 | yfinance `Ticker.info` |
| `opt` | `get_options_data()` `:693` | Put/Call OI 比等 | yfinance option chain |

### 1.1 技術指標公式（helper 函式）

- **SMA**：`series.rolling(period).mean()`（`:198`），用到 MA20 / MA60 / MA200。
- **RSI(14)**：標準 Wilder 近似，`gain/loss` 各取 14 日**簡單**均值（非 EMA）(`:201`)。
- **MACD(12,26,9)**：EMA 快慢線差，signal 為其 9 日 EMA；`macd_hist = MACD − Signal`(`:208`)。
- **Stochastic KD(14,3,3)**、**Supertrend(10,3, ATR)**：有計算但**未進入評分**（僅畫圖用）。
- **量能**：`Vol_MA20 = Volume.rolling(20).mean()`。
- **52 週高**：`High_252 = Close.rolling(252).max()`（注意：用**收盤**而非最高價）。

### 1.2 缺值預設（重要）

`indicators` 以 `_f(v, d)` 轉數值，缺值時給預設：

- `rsi` 缺 → **預設 50**（落在健康區，等於自動 +2 分，見 §5.3）
- `macd_hist`、各 MA 缺 → 預設 0（→ 該項 0 分）
- `fund` 各欄缺 → `None`（→ 該籌碼項 0 分）
- `opt` 為 `None`（抓取失敗/無選擇權）→ Put/Call 項 0 分

---

## 2. 技術面計分（滿分 10）

程式碼 `us_stock_dark.py:1116-1124`：

| # | 條件 | 加分 | 判斷式 |
|---|---|---|---|
| T1 | 站上季線 | **+1.5** | `close > ma60 > 0` |
| T2 | 季線高於年線（黃金交叉排列） | **+2** | `ma60 > ma200 > 0` |
| T3 | 逼近 52 週高 | **+1.5** | `close >= high_252 * 0.95` |
| T4 | 量增 | **+1** | `volume > vol_ma20 > 0` |
| T5 | MACD 多頭 | **+2** | `macd_hist > 0` |
| T6 | RSI 健康區 | **+2 / +1** | `45≤rsi≤70` 得 2；`40≤rsi<45` 或 `70<rsi≤78` 得 1；其餘 0 |

技術面最高 = 1.5+2+1.5+1+2+2 = **10**。

---

## 3. 籌碼面計分（滿分 10）

程式碼 `us_stock_dark.py:1126-1140`：

| # | 條件 | 加分 | 判斷式 |
|---|---|---|---|
| C1 | 法人持股比例 | **+2 / +1** | `inst_pct≥0.6` 得 2；`≥0.4` 得 1 |
| C2 | 空單月減少 | **+2** | `shares_short < shares_short_prior`（且兩者非空、prior>0） |
| C3 | 低放空比 | **+1.5** | `short_pct_float < 0.05` |
| C4 | Put/Call(OI) 偏多 | **+1.5 / +0.75** | `pcr_oi<0.7` 得 1.5；`<1.0` 得 0.75（需有 `opt`） |
| C5 | 分析師目標價有上檔 | **+1.5** | `target_price > close * 1.05` |
| C6 | 預估 PE 改善 | **+1.5** | `0 < forward_pe < trailing_pe` |

籌碼面最高 = 2+2+1.5+1.5+1.5+1.5 = **10**。

---

## 4. 總分 → 評等映射

程式碼 `us_stock_dark.py:1142-1147`（`total = tech + chip`）：

| 總分區間 | 評等 | key |
|---|---|---|
| ≥ 14 | 強力買進 | `sb` |
| 10 – 13.x | 買進 | `b` |
| 6 – 9.x | 中性 | `n` |
| 3 – 5.x | 減碼 | `s` |
| ≤ 2.x | 賣出 | `ss` |

輸出 `{tech, chip, total, rating, rating_key, details}`；`details` 逐項列出加分，
前端「評分明細」摺疊區只顯示 `points>0` 的項目。評等只用於**顯示**（分組看板 +
個股卡片 badge），**不驅動任何自動下單或部位調整**。

> 注意：評等與 K 線副圖的「殘差動能訊號 / 建議」以及「宏觀 Regime gating」
> 是**兩套獨立系統**，彼此不互通。本文件只涵蓋綜合評等計分。

---

## 5. 送審重點 / 已知盲點（請 Claude 特別檢討）

### 5.1 只有加分、沒有扣分 → 評等偏多
所有 12 個計分項都是「利多才給分」。因此：
- 「賣出/減碼」的真正語意是**「利多訊號很少」**，而非「出現利空/破線/死叉」。
- 一檔正在破線、MACD 死叉、跌破年線的弱勢股，只要沒有任何利多，最多也只是低分，
  但**同樣一檔剛跌破卻法人持股高、PE 改善**的股票仍可能落在「中性」甚至「買進」。
- 缺乏對稱的空方訊號（如跌破季線扣分、死亡交叉扣分、RSI>80 過熱扣分）。

### 5.2 技術/籌碼權重各半，但籌碼多為「慢變數」
籌碼面 6 項多來自 `Ticker.info` 的**低頻/延遲**資料（法人持股、月空單、目標價），
與技術面的**日頻**訊號同權重相加，可能稀釋當日技術訊號的時效性。

### 5.3 缺值處理會系統性灌水
- **RSI 缺值預設 50 → 自動 +2**。新上市/資料不足個股會平白拿到技術分。
- 反之 `fund`/`opt` 缺值一律 0 分，使**資料完整的大型股**在籌碼面天然占優，
  **資料稀疏的中小型股**被結構性低估（非因基本面差，而是欄位抓不到）。

### 5.4 個別條件的合理性
- **T3 逼近 52 週高**用**收盤序列**的 rolling max（非 `High`），門檻 95%。
- **T2 與 T1**部分重疊（都反映多頭排列），是否重複計分值得討論。
- **C4 Put/Call**：`opt.get("pcr_oi", 1)` 預設 1 → 無資料時落在「不加分」，尚屬中性；
  但 PCR 作為反向指標的門檻（0.7 / 1.0）是硬編碼，未依個股歷史分布標準化。
- **C6 預估 PE 改善**只比較 forward vs trailing，未考慮產業別或成長性（高成長股 PE 天生高）。

### 5.5 門檻為絕對值、未做橫斷面/歷史標準化
所有門檻（0.6 法人、0.05 放空、0.7 PCR、95% 52 週高…）皆為**全市場單一絕對值**，
未依類股、市值或個股自身歷史分布正規化 → 不同產業可比性存疑。

### 5.6 評等門檻的邊界
- 總分理論上限 20，但「強力買進」門檻僅 14（70%），「買進」10（50%）。
- 因 §5.1 幾乎不會出現極低分，實務上「賣出/減碼」桶極少觸發（可對照 `docs/us.html`
  現況：多數落在買進/中性，賣出多為資料極缺者）。

---

## 6. 原始程式碼（核心函式）

```python
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
    # ... 以下組 details 陣列供前端「評分明細」顯示
```
