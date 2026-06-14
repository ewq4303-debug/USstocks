# IBKR 每日帳戶淨值抓取 — Flex Web Service 規格

> 目的:在 GitHub Actions 環境下,以無狀態方式抓取 IBKR 帳戶的每日 NAV(Net Liquidation Value),
> 累積成時間序列 JSON,供 ECharts 暗色儀表板畫淨值曲線使用。
> 限制:純雲端、無常駐連線。**禁止**使用 TWS API / IB Gateway / Client Portal Gateway(皆需常駐 session)。

---

## 1. 為什麼用 Flex Web Service

- 一般 IBKR 介面(Client Portal Web API、MCP `get_account_summary`)只回傳「當下」的 NLV 快照,**沒有歷史每日淨值端點**,無法直接畫時間序列。
- Flex Web Service 是 token 認證的純 HTTP API,不需要登入憑證、不需要常駐連線,適合 GitHub Actions。
- Activity Flex 為日終資料,一天更新一次(收盤後結算)。歷史日報表可回溯前兩年 + 年初至今。

---

## 2. 前置設定(在 Client Portal 手動完成一次)

這部分人工做,不寫進程式:

1. **啟用 Flex Web Service 並產生 Token**
   - Settings → Account Report 區塊 → Flex Web Service
   - 勾選 Flex Web Service Status 啟用
   - Generate New Token,選 lifespan(最長 1 年)
   - **Valid for IP Address 欄位留空**(GitHub Actions runner IP 是動態的,設白名單會回 error 1013)
   - 記下 token 數字

2. **建立 Activity Flex Query 並取得 Query ID**
   - Performance & Reports → Flex Queries → 新增 **Activity Flex Query**
   - 至少勾選能產出 NAV 的 section:**Change in NAV**(會輸出該期間的 Starting / Ending NAV)
   - Date Period 設為 **Last Business Day**(見第 5 節資料策略)
   - 建好後點 Info 圖示,記下 **Query ID**

3. **存成 GitHub Secrets**
   - `IBKR_FLEX_TOKEN`
   - `IBKR_FLEX_QUERY_ID`

---

## 3. API 流程(兩段式)

所有請求都必須帶 `User-Agent` header(值任意,但不能缺)。

### Step 1 — 觸發產生報表 `/SendRequest`

```
GET https://ndcdyn.interactivebrokers.com/AccountManagement/FlexWebService/SendRequest?t={TOKEN}&q={QUERY_ID}&v=3
```

參數:
- `t` = token(必填)
- `q` = Query ID(必填)
- `v` = `3`(必填,固定值)

成功回應(XML):
```xml
<FlexStatementResponse timestamp="...">
  <Status>Success</Status>
  <ReferenceCode>1234567890</ReferenceCode>
  <Url>https://ndcdyn.interactivebrokers.com/AccountManagement/FlexWebService/GetStatement</Url>
</FlexStatementResponse>
```
- **要從回應裡讀 `<Url>` 當作 Step 2 的 base URL**,不要硬寫死。
- `<Status>` 為 `Fail` 時,讀 `<ErrorCode>` / `<ErrorMessage>` 處理(見第 4 節)。

### Step 2 — 取回報表 `/GetStatement`

```
GET {Url}?t={TOKEN}&q={REFERENCE_CODE}&v=3
```

參數:
- `t` = 同一個 token
- `q` = Step 1 拿到的 **ReferenceCode**(注意:這裡的 `q` 是 reference code,不是 query id)
- `v` = `3`

成功回應為報表 XML(`<FlexQueryResponse>`)。

> ⚠️ 報表生成有延遲。Step 1 之後**立刻**打 Step 2 常會拿到 error 1019(generation in progress)。
> 必須在兩步之間 sleep 數秒,並對 Step 2 做重試。

---

## 4. 重試與錯誤處理

**速率限制:每 token 1 req/sec、10 req/min**(超過回 1018)。請求之間至少間隔 1 秒。

可重試(暫時性,backoff 後重打,建議 3~5 次,間隔 5~15 秒遞增):
- `1001` Statement could not be generated, try again
- `1009` Server under heavy load
- `1018` Too many requests(此時加大間隔)
- `1019` Statement generation in progress
- `1021` Statement could not be retrieved

**不可重試,直接 fail 並讓 workflow 報錯**(需人工處理):
- `1012` Token has expired → 需重新產 token 並更新 secret
- `1013` IP restriction → token 設了 IP 白名單,需移除
- `1014` Query is invalid
- `1015` Token is invalid
- `1010` Legacy Flex 不再支援 → 需改用 Activity Flex

---

## 5. 資料策略

### 策略 A — 每日快照累積(主要,建議)

- Flex Query 的 period 設 **Last Business Day**。
- 每天 workflow 跑一次,從報表 XML 取**單一個 Ending NAV** 值。
- 把 `{date, nav}` append 進累積 JSON;同日期已存在則覆蓋(冪等)。
- 優點:邏輯簡單、冪等、完全貼合既有「每日 append JSON」架構;不依賴複雜的 per-day section。
- 缺點:無法回補啟用前的歷史。

### 策略 B — 一次性歷史回補(選用)

- 另建一個 Query,period 設 **Last 365 Calendar Days** 或自訂日期區間,勾選能產出**逐日** equity 的 section(在 Flex Query builder 裡確認確切名稱,目標是拿到「每個交易日一列」的 NAV/equity)。
- 跑一次,把所有日期列一次寫進 JSON,之後改回策略 A 日常累積。

> 預設先做策略 A。策略 B 視需要再做。

---

## 6. XML 解析重點

從 `<FlexStatement>` 取出:
- `accountId` 屬性
- 報表日期(`toDate` 屬性,或 NAV section 裡的 `reportDate`)
- NAV 數值(從 Change in NAV section 的 Ending NAV;欄位實際名稱以該帳戶產出的 XML 為準,實作時先 dump 一份 XML 確認 tag/屬性名再寫解析)

實作建議:先用 token + query id 手動跑一次,把回傳 XML 存檔、印出結構,**根據真實 XML 寫解析**,不要憑空假設欄位名。

---

## 7. 輸出 JSON 格式(給 ECharts 用)

維持既有 repo JSON 風格,例如:

```json
{
  "updated_at": "2026-06-14T08:00:00Z",
  "account_id": "U1234567",
  "currency": "USD",
  "series": [
    { "date": "2026-06-12", "nav": 123456.78, "cash": 7401.63 },
    { "date": "2026-06-13", "nav": 124001.22, "cash": 6800.10 }
  ]
}
```

- `series` 依日期升冪排序。
- ECharts 直接吃 `[date, nav]`,可在前端 map 成 `series.map(d => [d.date, d.nav])`。
- `cash` 為當日現金 (EquitySummary 的 `cash` 欄位),供畫「持倉比例 = 1 − cash/nav」折線;
  若報表只有 Change in NAV (無逐日現金),該日省略 `cash`,折線該段留空白。

---

## 8. GitHub Actions 注意事項

- Secrets:`IBKR_FLEX_TOKEN`、`IBKR_FLEX_QUERY_ID`。
- 排程:在**美股收盤結算後**(隔天早上更穩,確保前一日數值結算完)觸發,例如台灣時間早上。對應 cron 用 UTC 設定。
- 把累積 JSON commit 回 repo(或寫進 GitHub Pages 輸出),沿用現有 commit-back 流程。
- Token 最長 1 年會到期(error 1012),排一個到期前更新 secret 的提醒。
- 整個流程只需 `requests` + 標準庫 `xml.etree.ElementTree`,無需額外重依賴。

---

## 9. 驗收標準

1. 本機/Actions 手動觸發一次,能成功完成 SendRequest → GetStatement 兩步並存到 XML。
2. 能正確解析出 `account_id`、`date`、`nav` 並寫入 JSON。
3. 重複跑同一天不會產生重複列(冪等)。
4. 模擬 1019 回應時會自動重試;遇 1012/1015 會明確 fail 並印出原因。
5. 輸出 JSON 能被現有 ECharts 儀表板讀取並畫出淨值曲線。
