#!/usr/bin/env python3
"""
Helen 每日晨報 — Render Cron Job
排程：UTC 00:30 = 台北時間 08:30
讀取 Notion「我的任務」資料庫，整理今天 / 昨天未完成 / 明天 / 逾期，
推播到 Peggy LifeOS LINE 帳號。週末與國定假日跳過。
"""
import os
from datetime import datetime, timezone, timedelta, date

import anthropic
import requests

TAIPEI = timezone(timedelta(hours=8))

TW_FIXED_HOLIDAYS = {
    (1, 1), (2, 28), (4, 4), (4, 5), (5, 1),
    (6, 10), (9, 3), (10, 10), (12, 25),
}

def is_taiwan_holiday(dt: datetime) -> bool:
    if dt.weekday() >= 5:
        return True
    return (dt.month, dt.day) in TW_FIXED_HOLIDAYS

LINE_PUSH_URL = "https://api.line.me/v2/bot/message/push"


# ── Notion ───────────────────────────────────────────────────────────────────

def notion_query(filter_body: dict) -> list:
    token = os.environ["NOTION_TOKEN"]
    db_id = os.environ["NOTION_TASKS_DB_ID"]
    resp = requests.post(
        f"https://api.notion.com/v1/databases/{db_id}/query",
        headers={
            "Authorization": f"Bearer {token}",
            "Notion-Version": "2022-06-28",
            "Content-Type": "application/json",
        },
        json={"filter": filter_body, "sorts": [{"property": "Date", "direction": "ascending"}], "page_size": 30},
    )
    return resp.json().get("results", [])


def extract_task(r: dict) -> tuple:
    props = r.get("properties", {})
    title_prop = props.get("Task") or props.get("Name") or {}
    title_list = title_prop.get("title") or title_prop.get("rich_text") or []
    title = "".join(t.get("plain_text", "") for t in title_list) or "（無標題）"
    done = props.get("Done", {}).get("checkbox", False)
    date_start = (props.get("Date", {}).get("date") or {}).get("start", "")
    return title, done, date_start


def fetch_notion_data(today: date) -> dict:
    yesterday = today - timedelta(days=1)
    tomorrow = today + timedelta(days=1)

    today_tasks = [extract_task(r) for r in notion_query(
        {"property": "Date", "date": {"equals": str(today)}})]
    yesterday_undone = [extract_task(r) for r in notion_query({"and": [
        {"property": "Date", "date": {"equals": str(yesterday)}},
        {"property": "Done", "checkbox": {"equals": False}},
    ]})]
    tomorrow_tasks = [extract_task(r) for r in notion_query(
        {"property": "Date", "date": {"equals": str(tomorrow)}})]
    overdue = [extract_task(r) for r in notion_query({"and": [
        {"property": "Date", "date": {"before": str(yesterday)}},
        {"property": "Done", "checkbox": {"equals": False}},
    ]})]

    return {
        "today": today_tasks,
        "yesterday_undone": yesterday_undone,
        "tomorrow": tomorrow_tasks,
        "overdue": overdue,
    }


# ── Gmail（唯讀）────────────────────────────────────────────────────────────────

# 只抓收件匣最近 1 天、排除「促銷／社交」分類的信（廣告、推播那類自動濾掉）
GMAIL_QUERY = "in:inbox newer_than:1d -category:promotions -category:social"


def gmail_access_token(refresh_token: str) -> str:
    resp = requests.post(
        "https://oauth2.googleapis.com/token",
        data={
            "client_id": os.environ["GMAIL_CLIENT_ID"],
            "client_secret": os.environ["GMAIL_CLIENT_SECRET"],
            "refresh_token": refresh_token,
            "grant_type": "refresh_token",
        },
        timeout=20,
    )
    resp.raise_for_status()
    return resp.json()["access_token"]


def _parse_sender(from_header: str) -> str:
    # "顯示名稱 <a@b.com>" → 顯示名稱；沒有顯示名稱就回信箱
    from_header = from_header.strip()
    if "<" in from_header:
        name = from_header.split("<")[0].strip().strip('"')
        return name or from_header.split("<")[1].rstrip(">")
    return from_header


def fetch_gmail(refresh_token: str, max_results: int = 12) -> list:
    """回傳最近重要信件 [{sender, subject}]，失敗則回空清單（不中斷晨報）。"""
    try:
        token = gmail_access_token(refresh_token)
        headers = {"Authorization": f"Bearer {token}"}
        listing = requests.get(
            "https://gmail.googleapis.com/gmail/v1/users/me/messages",
            headers=headers,
            params={"q": GMAIL_QUERY, "maxResults": max_results},
            timeout=20,
        ).json()
        msgs = listing.get("messages", []) or []

        out = []
        for m in msgs:
            detail = requests.get(
                f"https://gmail.googleapis.com/gmail/v1/users/me/messages/{m['id']}",
                headers=headers,
                params={"format": "metadata", "metadataHeaders": ["From", "Subject"]},
                timeout=20,
            ).json()
            hdrs = {h["name"]: h["value"] for h in detail.get("payload", {}).get("headers", [])}
            out.append({
                "sender": _parse_sender(hdrs.get("From", "（不明寄件者）")),
                "subject": hdrs.get("Subject", "（無主旨）"),
            })
        return out
    except Exception as e:
        print(f"Gmail 讀取失敗：{e}", flush=True)
        return []


def fetch_all_gmail() -> dict:
    """讀取 wang / event 兩個信箱，回傳 {'wang': [...], 'event': [...]}。"""
    result = {"wang": [], "event": []}
    wang_token = os.environ.get("GMAIL_WANG_REFRESH_TOKEN")
    event_token = os.environ.get("GMAIL_EVENT_REFRESH_TOKEN")
    if wang_token:
        result["wang"] = fetch_gmail(wang_token)
    if event_token:
        result["event"] = fetch_gmail(event_token)
    return result


def summarize_gmail(gmail: dict) -> str:
    """用 Claude Haiku 從兩個信箱挑出重要信件，回傳純文字摘要；都沒重要信則回空字串。"""
    if not gmail.get("wang") and not gmail.get("event"):
        return ""

    def to_text(mails):
        if not mails:
            return "（無信件）"
        return "\n".join(f"- {m['sender']}｜{m['subject']}" for m in mails)

    prompt = f"""你是 Helen，幫 Peggy 看信。以下是兩個信箱最近一天的收件清單。

請只挑出「需要 Peggy 注意」的信：
帳單 / 繳費單、預約 / 掛號、活動邀請、快遞 / 包裹通知、銀行 / 政府 / 公司的重要通知、私人或工作往來信件。

務必丟掉這些雜訊（不要列出）：
Google 安全性警示、Google Payments 自動通知、求職媒合（LinkedIn）、電子報 / 週報 / Digest（Medium、vocus、方格子、Reddit 等）、廣告促銷、社群通知、系統自動信。

【wang 信箱（peggywang9527）】
{to_text(gmail.get("wang", []))}

【event 信箱（peggyforevent）】
{to_text(gmail.get("event", []))}

輸出規則：
- 純文字，不用 Markdown
- 分「📧 信箱 wang」和「📧 信箱 event」兩區，各自條列重要信件（用「・」），格式：・寄件者｜主旨（精簡）
- 某個信箱若沒有重要信件，該區寫「・無重要信件」
- 不要任何開場白、結語或說明，只輸出這兩區"""

    client = anthropic.Anthropic(api_key=os.environ["ANTHROPIC_API_KEY"])
    msg = client.messages.create(
        model="claude-haiku-4-5-20251001",
        max_tokens=800,
        messages=[{"role": "user", "content": prompt}],
    )
    return msg.content[0].text.strip()


# ── 格式化 ────────────────────────────────────────────────────────────────────

def format_brief(data: dict, today: date, gmail_summary: str = "") -> str:
    weekdays = ["週一", "週二", "週三", "週四", "週五", "週六", "週日"]
    wday = weekdays[today.weekday()]
    lines = [f"早安 Peggy！今天是 {today.month}/{today.day}（{wday}）☀️", ""]

    lines.append("📋 今日待辦")
    if data["today"]:
        for title, done, _ in data["today"]:
            mark = "✅" if done else "・"
            lines.append(f"{mark} {title}")
    else:
        lines.append("・今天沒有安排的任務")
    lines.append("")

    if data["yesterday_undone"]:
        lines.append("⚠️ 昨日未完成")
        for title, _, _ in data["yesterday_undone"]:
            lines.append(f"・{title}")
        lines.append("")

    if data["tomorrow"]:
        lines.append("📅 明日預計")
        for title, done, _ in data["tomorrow"]:
            mark = "✅" if done else "・"
            lines.append(f"{mark} {title}")
        lines.append("")

    if data["overdue"]:
        lines.append("🔔 逾期未完成")
        for title, _, date_str in data["overdue"]:
            lines.append(f"・{title}（{date_str}）")
        lines.append("")

    if gmail_summary:
        lines.append(gmail_summary)
        lines.append("")

    lines.append("有什麼需要幫忙的隨時說 💪")
    return "\n".join(lines)


# ── LINE push ─────────────────────────────────────────────────────────────────

def push_line(text: str) -> None:
    user_id = os.environ["LINE_USER_ID"]
    token = os.environ["LINE_CHANNEL_ACCESS_TOKEN"]
    resp = requests.post(
        LINE_PUSH_URL,
        headers={"Authorization": f"Bearer {token}", "Content-Type": "application/json"},
        json={"to": user_id, "messages": [{"type": "text", "text": text}]},
        timeout=20,
    )
    resp.raise_for_status()
    print(f"LINE push OK: {resp.status_code}")


# ── 主流程 ────────────────────────────────────────────────────────────────────

def main():
    now = datetime.now(TAIPEI)
    today = now.date()
    print(f"[Helen] {now.strftime('%Y-%m-%d %H:%M')} 台北時間 開始執行", flush=True)

    if is_taiwan_holiday(now):
        print(f"[Helen] 今天是假日，跳過推播。", flush=True)
        return

    print("讀取 Notion 任務...", flush=True)
    data = fetch_notion_data(today)
    print(f"今天 {len(data['today'])} 筆 / 昨日未完成 {len(data['yesterday_undone'])} 筆 / 明天 {len(data['tomorrow'])} 筆 / 逾期 {len(data['overdue'])} 筆", flush=True)

    print("讀取 Gmail（wang / event）...", flush=True)
    gmail = fetch_all_gmail()
    print(f"Gmail wang {len(gmail['wang'])} 封 / event {len(gmail['event'])} 封", flush=True)
    print("Claude 挑出重要信件...", flush=True)
    gmail_summary = summarize_gmail(gmail)

    brief = format_brief(data, today, gmail_summary)
    print("--- 晨報內容 ---")
    print(brief)
    print("----------------")

    print("LINE push...", flush=True)
    push_line(f"[Helen]\n{brief}")
    print("[Helen] 晨報推播完成", flush=True)


if __name__ == "__main__":
    main()
