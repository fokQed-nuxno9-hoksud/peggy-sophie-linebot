#!/usr/bin/env python3
"""
Helen 每日晨報 — Render Cron Job
排程：UTC 00:30 = 台北時間 08:30
讀取 Notion「我的任務」資料庫，整理今天 / 昨天未完成 / 明天 / 逾期，
推播到 Peggy LifeOS LINE 帳號。週末與國定假日跳過。
"""
import os
from datetime import datetime, timezone, timedelta, date

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


# ── 格式化 ────────────────────────────────────────────────────────────────────

def format_brief(data: dict, today: date) -> str:
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

    brief = format_brief(data, today)
    print("--- 晨報內容 ---")
    print(brief)
    print("----------------")

    print("LINE push...", flush=True)
    push_line(f"[Helen]\n{brief}")
    print("[Helen] 晨報推播完成", flush=True)


if __name__ == "__main__":
    main()
