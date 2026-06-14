"""
fetch_ibkr_nav.py — 以 IBKR Flex Web Service 抓取每日帳戶淨值 (NAV) 並累積成時間序列

背景
----
IBKR 一般介面 (Client Portal Web API / MCP get_account_summary) 只回「當下」NLV 快照，
沒有歷史每日淨值端點。Flex Web Service 是 token 認證的純 HTTP API，不需常駐連線，
適合 GitHub Actions 在收盤結算後每天跑一次，把當日 Ending NAV append 進 nav_history.json。

規格見 IBKR_FLEX_NAV.md。流程為兩段式：
  1. SendRequest  → 取得 ReferenceCode 與 GetStatement 的 Url
  2. GetStatement → 取回報表 XML (有生成延遲，需 sleep + 重試 1019)

前置 (人工，一次):
  - Client Portal 啟用 Flex Web Service、產生 Token (IP 白名單留空)
  - 建 Activity Flex Query (至少含 "Change in NAV" section，period = Last Business Day)
  - 把 token / query id 存成環境變數 IBKR_FLEX_TOKEN / IBKR_FLEX_QUERY_ID

輸出: nav_history.json (預設)
  {"updated_at","account_id","currency","series":[{"date","nav"}, ...]}  (series 依日期升冪)

只依賴 requests + 標準庫 xml.etree.ElementTree。
"""

import os
import sys
import json
import time
import argparse
from datetime import datetime, timezone
import xml.etree.ElementTree as ET

import requests

SEND_URL = "https://ndcdyn.interactivebrokers.com/AccountManagement/FlexWebService/SendRequest"
DEFAULT_GET_URL = "https://ndcdyn.interactivebrokers.com/AccountManagement/FlexWebService/GetStatement"
HEADERS = {"User-Agent": "USstocks-flex-nav/1.0"}  # Flex 要求一定要帶 User-Agent

# 可重試 (暫時性) 與不可重試 (需人工) 的 Flex error code
RETRYABLE = {"1001", "1009", "1018", "1019", "1021"}
FATAL = {"1010", "1012", "1013", "1014", "1015"}


class FlexError(Exception):
    def __init__(self, code, message):
        self.code = (code or "").strip()
        self.message = (message or "").strip()
        super().__init__(f"Flex error {self.code}: {self.message}")


def _get(url, params, timeout):
    """送出請求，回傳 XML 字串 (帶 User-Agent；速率限制由呼叫端控制)。"""
    r = requests.get(url, params=params, headers=HEADERS, timeout=timeout)
    r.raise_for_status()
    return r.text


def _norm_date(s):
    """Flex 日期 (YYYYMMDD 或 yyyy-MM-dd) → yyyy-MM-dd。"""
    s = (s or "").strip()
    if len(s) == 8 and s.isdigit():
        return f"{s[:4]}-{s[4:6]}-{s[6:]}"
    return s


def send_request(token, query_id, timeout=30):
    """Step 1: 觸發報表生成，回傳 (reference_code, statement_url)。"""
    xml = _get(SEND_URL, {"t": token, "q": query_id, "v": "3"}, timeout)
    root = ET.fromstring(xml)
    status = (root.findtext("Status") or "").strip()
    if status != "Success":
        raise FlexError(root.findtext("ErrorCode"), root.findtext("ErrorMessage"))
    ref = (root.findtext("ReferenceCode") or "").strip()
    url = (root.findtext("Url") or "").strip() or DEFAULT_GET_URL
    if not ref:
        raise FlexError("no-ref", "SendRequest 成功但缺 ReferenceCode")
    return ref, url


def get_statement(url, token, reference_code, max_retries=5, base_sleep=6, timeout=60):
    """Step 2: 取回報表 XML；遇暫時性錯誤 backoff 重試，遇致命錯誤直接 raise。"""
    last = None
    for attempt in range(1, max_retries + 1):
        time.sleep(1)  # 速率限制：每 token ≥1 req/sec
        xml = _get(url, {"t": token, "q": reference_code, "v": "3"}, timeout)
        root = ET.fromstring(xml)
        if root.tag == "FlexQueryResponse":
            return xml  # 成功
        # 否則為 FlexStatementResponse (錯誤 / 生成中)
        code = (root.findtext("ErrorCode") or "").strip()
        msg = (root.findtext("ErrorMessage") or "").strip()
        last = FlexError(code, msg)
        if code in FATAL:
            raise last
        if code in RETRYABLE and attempt < max_retries:
            wait = base_sleep * attempt  # 6,12,18,... 秒遞增
            print(f"  ⏳ {code} {msg} → {wait}s 後重試 ({attempt}/{max_retries})")
            time.sleep(wait)
            continue
        raise last
    raise last or FlexError("retries", "超過重試次數仍未取得報表")


def parse_nav_rows(xml_text):
    """從報表 XML 取出 (account_id, currency, [{date, nav, cash?}, ...])。

    優先用 EquitySummaryByReportDateInBase / EquitySummaryInBase 的逐日 total + cash
    (支援策略 B 回補與『持倉比例 = 1 − 現金/NAV』折線)，否則退回 Change in NAV 的 endingValue。
    """
    root = ET.fromstring(xml_text)
    if root.tag != "FlexQueryResponse":
        code = (root.findtext("ErrorCode") or "").strip()
        raise FlexError(code or "bad-xml", root.findtext("ErrorMessage") or f"非預期根節點 {root.tag}")
    stmt = root.find(".//FlexStatement")
    account_id = stmt.get("accountId") if stmt is not None else None
    currency = None
    rows = {}

    # (1) 逐日權益彙總 → 每個交易日一列 NAV (total) 與現金 (cash)
    for tag in ("EquitySummaryByReportDateInBase", "EquitySummaryInBase"):
        for el in root.iter(tag):
            date = _norm_date(el.get("reportDate") or el.get("date"))
            total = el.get("total")
            currency = currency or el.get("currency")
            if date and total:
                try:
                    rec = {"nav": float(total)}
                    cash = el.get("cash")
                    if cash not in (None, ""):
                        rec["cash"] = float(cash)
                    rows[date] = rec
                except ValueError:
                    pass
        if rows:
            break

    # (2) 退回 Change in NAV 的 Ending NAV (單日，無現金資訊)
    if not rows:
        stmt_to = stmt.get("toDate") if stmt is not None else None
        for el in root.iter("ChangeInNAV"):
            ev = el.get("endingValue")
            if not ev:
                continue  # 外層容器無此屬性，跳過
            date = _norm_date(el.get("toDate") or el.get("reportDate") or stmt_to)
            currency = currency or el.get("currency")
            if date:
                try:
                    rows[date] = {"nav": float(ev)}
                except ValueError:
                    pass

    series = []
    for d in sorted(rows):
        rec = {"date": d, "nav": round(rows[d]["nav"], 2)}
        if "cash" in rows[d]:
            rec["cash"] = round(rows[d]["cash"], 2)
        series.append(rec)
    return account_id, (currency or "USD"), series


def _load_existing(path):
    """讀現有累積檔，相容新格式 (dict) 與舊格式 (list[{date,net_liq}])。"""
    try:
        with open(path, "r", encoding="utf-8") as f:
            data = json.load(f)
    except (FileNotFoundError, json.JSONDecodeError):
        return {}
    if isinstance(data, list):  # 舊格式遷移
        series = [{"date": x.get("date"), "nav": x.get("nav", x.get("net_liq"))}
                  for x in data if x.get("date")]
        return {"series": [s for s in series if s["nav"] is not None]}
    return data if isinstance(data, dict) else {}


def merge_and_write(path, account_id, currency, new_rows):
    """把新抓到的 {date,nav,cash?} 併入累積檔 (同日逐欄覆寫，冪等)，依日期升冪寫回。"""
    existing = _load_existing(path)
    bydate = {}
    for r in existing.get("series", []):
        if r.get("date") and r.get("nav") is not None:
            rec = {"nav": r["nav"]}
            if r.get("cash") is not None:
                rec["cash"] = r["cash"]
            bydate[r["date"]] = rec
    for r in new_rows:
        cur = bydate.get(r["date"], {})
        cur["nav"] = r["nav"]            # NAV 一律更新
        if r.get("cash") is not None:    # 現金僅在新資料有值時更新 (不抹掉舊值)
            cur["cash"] = r["cash"]
        bydate[r["date"]] = cur
    series = []
    for d in sorted(bydate):
        rec = {"date": d, "nav": bydate[d]["nav"]}
        if "cash" in bydate[d]:
            rec["cash"] = bydate[d]["cash"]
        series.append(rec)
    out = {
        "updated_at": datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ"),
        "account_id": account_id or existing.get("account_id"),
        "currency": currency or existing.get("currency") or "USD",
        "series": series,
    }
    with open(path, "w", encoding="utf-8") as f:
        json.dump(out, f, ensure_ascii=False, indent=2)
    return out


def main():
    ap = argparse.ArgumentParser(description="Fetch IBKR daily NAV via Flex Web Service")
    ap.add_argument("--out", default="nav_history.json", help="累積輸出檔")
    ap.add_argument("--token", default=os.environ.get("IBKR_FLEX_TOKEN"))
    ap.add_argument("--query-id", default=os.environ.get("IBKR_FLEX_QUERY_ID"))
    ap.add_argument("--xml-dump", default=None, help="把報表 XML 另存一份 (除錯用)")
    ap.add_argument("--from-xml", default=None, help="跳過 API，直接解析既有 XML 檔 (測試用)")
    ap.add_argument("--max-retries", type=int, default=5)
    ap.add_argument("--send-wait", type=float, default=3.0, help="SendRequest 後首次 GetStatement 前等待秒數")
    args = ap.parse_args()

    # 測試模式：直接解析既有 XML
    if args.from_xml:
        with open(args.from_xml, "r", encoding="utf-8") as f:
            xml = f.read()
        account_id, currency, rows = parse_nav_rows(xml)
        if not rows:
            print("✗ XML 中找不到任何 NAV 列"); sys.exit(1)
        out = merge_and_write(args.out, account_id, currency, rows)
        print(f"✓ {args.out}: {len(out['series'])} 日 NAV (帳戶 {out['account_id']} · {out['currency']})")
        return

    if not args.token or not args.query_id:
        # 未設定 secrets → 視為「未啟用此功能」，乾淨略過 (不讓 workflow 變紅)
        print("ℹ️ 未設定 IBKR_FLEX_TOKEN / IBKR_FLEX_QUERY_ID，略過 NAV 抓取")
        return

    try:
        ref, url = send_request(args.token, args.query_id)
        print(f"✓ SendRequest OK · ReferenceCode={ref}")
        time.sleep(max(0.0, args.send_wait))  # 給報表生成緩衝，降低 1019 機率
        xml = get_statement(url, args.token, ref, max_retries=args.max_retries)
    except FlexError as e:
        print(f"✗ {e}")
        if e.code in FATAL:
            print("  → 致命錯誤 (token 過期/無效/IP 限制/query 無效)，需人工處理")
        sys.exit(1)
    except requests.RequestException as e:
        print(f"✗ 網路錯誤: {e}")
        sys.exit(1)

    if args.xml_dump:
        with open(args.xml_dump, "w", encoding="utf-8") as f:
            f.write(xml)
        print(f"  (XML 已存 {args.xml_dump})")

    account_id, currency, rows = parse_nav_rows(xml)
    if not rows:
        print("✗ 報表中找不到 NAV (確認 Flex Query 有勾 Change in NAV / Equity Summary section)")
        sys.exit(1)
    out = merge_and_write(args.out, account_id, currency, rows)
    latest = out["series"][-1]
    print(f"✓ {args.out}: 累積 {len(out['series'])} 日 NAV · 最新 {latest['date']} = {latest['nav']} "
          f"(帳戶 {out['account_id']} · {out['currency']})")


if __name__ == "__main__":
    main()
