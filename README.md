# USstocks — 美股監控儀表板

`us_stock_dark.py` 產生 `docs/us.html`（深色終端風儀表板），由 GitHub Actions
（`.github/workflows/us-stock.yml`）於美股交易日排程建置並推送到 GitHub Pages。
資料來源：yfinance + CNN Fear & Greed + IBKR 持股快照。

開發慣例與區塊說明見 `CLAUDE.md`。

## 敘事模式（Narrative Mode）

`narrative.json`（repo 根目錄）為**唯一真實來源（single source of truth）**，
手動標記每檔股票的敘事狀態，與客觀的殘差動能 `signal` 正交疊加，產出進出建議 `action`。

```json
{
  "NVDA": "hold",
  "AMD": "watch",
  "INTC": "exit"
}
```

三種模式：
- `hold`：敘事成立、順風 → 允許在強勢回檔時亮綠色加碼點。
- `watch`：敘事存疑、回檢中（**未列於檔案的股票一律預設此值**，安全預設，不亮綠色買點）。
- `exit`：敘事轉弱、退出中 → 抑制所有買方動作。

`action` 由 `(narrative_mode × signal)` 在 **Python 端**推導（前端只讀 `action` 上色，不重做邏輯）：

| narrative_mode | signal=pullback | signal=overheat | signal=weak | 其他 (strong/neutral/no_signal) |
|---|---|---|---|---|
| hold  | add    | trim   | review | hold |
| watch | watch  | trim   | review | watch |
| exit  | exit_pending（抑制） | reduce | reduce | exit_pending |

`signal` 欄位維持純 rMOM/Z_short 客觀分類，**不受敘事模式影響**。

### 編輯流程

1. 直接在 GitHub 網頁編輯 `narrative.json`（或本機改後 push）。
2. 等次日 cron 自動建置套用，或手動觸發 workflow_dispatch 立即重算（約 1 分鐘）。
3. 前端為靜態 GitHub Pages，無法寫回 repo，**不提供模式切換 UI**；模式一律改 JSON。

非法值或不在追蹤清單的 ticker → 視為 `watch` 並記入建置 log，不中斷流程。
