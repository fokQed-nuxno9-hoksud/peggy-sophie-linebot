#!/usr/bin/env python3
"""
Sophie 每日晨報 — Render Cron Job
排程：UTC 00:30 = 台北時間 08:30
讀取 Daily.gsheet「To Do list」分頁，整理今天工作待辦＋案件追蹤（(track) 標記），
推播到 Peggy LifeOS LINE 帳號。週末與國定假日跳過。
"""
import json
import os
import urllib.parse
import urllib.request
from datetime import datetime, timezone, timedelta

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

SPREADSHEET_ID = "14GLya7CgPe9TfEuNx2L30LAznzcFtiGSvU8gHPcOqd4"
SHEET_NAME = "To Do list"
TOKEN_URL = "https://oauth2.googleapis.com/token"
LINE_PUSH_URL = "https://api.line.me/v2/bot/message/push"


def get_sheets_access_token() -> str:
    data = urllib.parse.urlencode({
        "client_id": os.environ["GMAIL_CLIENT_ID"],
        "client_secret": os.environ["GMAIL_CLIENT_SECRET"],
        "refresh_token": os.environ["SHEETS_REFRESH_TOKEN"],
        "grant_type": "refresh_token",
    }).encode("utf-8")
    req = urllib.request.Request(TOKEN_URL, data=data, method="POST")
    resp = urllib.request.urlopen(req)
    return json.loads(resp.read())["access_token"]


def fetch_sheet_values(access_token: str) -> list:
    range_param = urllib.parse.quote(f"{SHEET_NAME}!A:F")
    url = f"https://sheets.googleapis.com/v4/spreadsheets/{SPREADSHEET_ID}/values/{range_param}"
    resp = requests.get(url, headers={"Authorization": f"Bearer {access_token}"}, timeout=20)
    resp.raise_for_status()
    return resp.json().get("values", [])


def date_key(dt: datetime) -> str:
    return f"{dt.month}/{dt.day}"


def parse_rows(rows: list, now: datetime) -> dict:
    today_key = date_key(now)
    yesterday_key = date_key(now - timedelta(days=1))
    tomorrow_key = date_key(now + timedelta(days=1))

    result = {"today": [], "yesterday": [], "tomorrow": []}
    current_date = None
    current_project = ""

    for row in rows[1:]:
        date_cell = (row[0].strip() if len(row) > 0 else "")
        project_cell = (row[1].strip() if len(row) > 1 else "")
        task_cell = (row[2].strip() if len(row) > 2 else "")
        note_cell = (row[5].strip() if len(row) > 5 else "")

        if date_cell:
            current_date = date_cell
            current_project = project_cell

        if current_date == today_key:
            result["today"].append({"project": current_project, "tasks": task_cell, "note": note_cell})
        elif current_date == yesterday_key:
            if task_cell:
                result["yesterday"].append({"project": current_project, "tasks": task_cell, "note": note_cell})
        elif current_date == tomorrow_key:
            if task_cell:
                result["tomorrow"].append({"project": current_project, "tasks": task_cell, "note": note_cell})

    return result


def parse_track_cases(rows: list) -> list:
    """掃全表找出結尾有 (track) 且未完成（無 ✓）的案件追蹤項目。"""
    cases = []
    current_date = None
    current_project = ""

    for row in rows[1:]:
        date_cell = (row[0].strip() if len(row) > 0 else "")
        project_cell = (row[1].strip() if len(row) > 1 else "")
        task_cell = (row[2].strip() if len(row) > 2 else "")

        if date_cell:
            current_date = date_cell
            current_project = project_cell

        if not task_cell:
            continue

        for line in task_cell.split("\n"):
            line = line.strip()
            if not line:
                continue
            task_text = line[1:].strip() if line.startswith("●") else line
            if task_text.lower().endswith("(track)") and "✓" not in line:
                clean = task_text[:-7].strip()  # remove "(track)"
                cases.append({
                    "date": current_date or "?",
                    "project": current_project,
                    "task": clean,
                })

    return cases


def format_brief(data: dict, track_cases: list, now: datetime) -> str:
    date_str = now.strftime("%m/%d（%a）")
    for en, zh in {"Mon": "一", "Tue": "二", "Wed": "三", "Thu": "四",
                   "Fri": "五", "Sat": "六", "Sun": "日"}.items():
        date_str = date_str.replace(en, zh)

    def rows_to_text(rows):
        if not rows:
            return "（無）"
        parts = []
        for r in rows:
            proj = f"[{r['project']}] " if r["project"] else ""
            if r["tasks"]:
                parts.append(f"{proj}{r['tasks']}")
            if r["note"]:
                parts.append(f"📌 {r['note']}")
        return "\n".join(parts) if parts else "（無）"

    if track_cases:
        track_lines = [f"・{c['date']} {'[' + c['project'] + '] ' if c['project'] else ''}{c['task']}"
                       for c in track_cases]
        track_text = "\n".join(track_lines)
    else:
        track_text = "（無待追蹤案件）"

    prompt = f"""你是 Sophie，Peggy 的工作 AI 助理。
以下是從 Daily.gsheet 讀到的原始資料，請整理成工作晨報，直接輸出 LINE 訊息內容。

格式規則：
- 純文字，不使用 Markdown（禁止 ##、**、---、表格）
- 條列用「・」
- 用 emoji 分區（例如 📋 🔔 ✅）
- 段落間空一行
- 語氣像朋友，簡潔口語

今日日期：{date_str}

【昨日工作回顧】
{rows_to_text(data["yesterday"])}

【今日工作待辦】
{rows_to_text(data["today"])}

【明天預覽】
{rows_to_text(data["tomorrow"])}

【案件追蹤（標記 track 的未完成項目）】
{track_text}

請整理成清楚的工作晨報，四個區塊都要呈現。昨日回顧中已完成的用「✅」標記，未完成用「・」。案件追蹤若有項目請特別提醒。最後加一句簡短打氣。"""

    client = anthropic.Anthropic(api_key=os.environ["ANTHROPIC_API_KEY"])
    msg = client.messages.create(
        model="claude-haiku-4-5-20251001",
        max_tokens=1000,
        messages=[{"role": "user", "content": prompt}],
    )
    return msg.content[0].text.strip()


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


def main():
    now = datetime.now(TAIPEI)
    print(f"[Sophie] {now.strftime('%Y-%m-%d %H:%M')} 台北時間 開始執行", flush=True)

    if is_taiwan_holiday(now):
        print(f"[Sophie] 今天是假日，跳過推播。", flush=True)
        return

    print("讀取 Google Sheets...", flush=True)
    access_token = get_sheets_access_token()
    rows = fetch_sheet_values(access_token)
    print(f"讀到 {len(rows)} 列", flush=True)

    data = parse_rows(rows, now)
    track_cases = parse_track_cases(rows)
    print(f"今天 {len(data['today'])} 筆 / 昨天 {len(data['yesterday'])} 筆 / 明天 {len(data['tomorrow'])} 筆 / 追蹤案件 {len(track_cases)} 件", flush=True)

    print("Claude 整理格式...", flush=True)
    brief = format_brief(data, track_cases, now)
    print("--- 晨報內容 ---")
    print(brief)
    print("----------------")

    print("LINE push...", flush=True)
    push_line(f"[Sophie]\n{brief}")
    print("[Sophie] 晨報推播完成", flush=True)


if __name__ == "__main__":
    main()
