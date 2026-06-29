# Dashboard Enhancements

本文件記錄本次新增的功能與後續維運方式。

## 1. 資料新鮮度與 Pipeline 狀態

儀表板 header 下方新增 **Data Freshness** 狀態列，顯示：

- 行情頁面建置時間
- NAV 最後日期
- IBKR 持股快照時間
- 組合績效 as-of 日期
- GitHub Actions 中 NAV / 績效 pipeline 的成功或失敗狀態

GitHub Actions 透過 `scripts/write_build_status.py` 寫入 `docs/data/build_status.json`。若狀態檔不存在，前端會自動略過，不影響本地建置。

## 2. 部位風險總覽

「持股明細」分頁新增 **部位風險總覽**：

- 持倉比例
- 現金比例
- 前三大持股集中度
- 市值加權 Beta
- 前 8 大持倉曝險
- 產業集中度
- 簡行情境壓力測試

壓力測試為 Beta 近似估算，未納入個股特異風險、選擇權非線性與盤中流動性，只作風控提醒。

## 3. 交易檢討

「持股明細」分頁新增 **交易檢討**：

- 由 IBKR trades 聚合後的每日 VWAP 交易紀錄產生
- 對齊個股 K 線收盤價
- 顯示交易後 +1 / +5 / +20 交易日報酬

SELL 後報酬仍顯示標的後續漲跌，方便檢討是否賣早或賣晚。

## 4. 評分透明度

個股卡片新增「評分明細」摺疊區，列出本期技術與籌碼加分項，讓綜合評等可追溯。

## 5. 現金流校正 Roadmap

`compute_metrics.py` 已支援 `twr` 與 `flow` 欄位：

1. 若 `nav_history.json.series[*].twr` 存在，直接以 TWR 作為日報酬。
2. 若 `flow` 存在，使用 `(NAV_t - flow_t) / NAV_{t-1} - 1`。
3. 若兩者皆無，維持 raw NAV return 並顯示未校正警示。

後續若能從 IBKR Flex Query 補入 TWR 或外部現金流，即可啟用校正路徑。

## 6. 驗證

建議每次修改後執行：

```bash
python compute_metrics.py --selftest
python -c "import ast; ast.parse(open('us_stock_dark.py', encoding='utf-8').read())"
python -m pytest tests/test_dashboard_enhancements.py
```
