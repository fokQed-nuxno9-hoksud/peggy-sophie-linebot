#!/usr/bin/env python3
"""
Helen 每日晨報 — Render Cron Job 執行腳本
排程：UTC 00:30 = 台北時間 08:30
讀取 Daily.gsheet「To Do list」分頁，整理今天待辦 / 昨天未完成 / 明天事項 / 注意事項，
推播到 Peggy LifeOS LINE 帳號。
"""
import json
import os
import sys
import urllib.parse
import urllib.request
from datetime import datetime, timezone, timedelta

import anthropic
import requests

TAIPEI = timezone(timedelta(hours=8))

# 台灣國定假日（固定節日，農曆節日只列出常見固定對應）
# 補充：Render Cron 每天跑，遇假日直接 exit，不推播
TW_FIXED_HOLIDAYS = {
    (1, 1),   # 元旦
    (2, 28),  # 和平紀念日
    (4, 4),   # 兒童節
    (4, 5),   # 清明節（固定）
    (5, 1),   # 勞動節
    (6, 10),  # 端午節（農曆五月五，此行每年需手動更新）
    (9, 3),   # 軍人節
    (10, 10), # 國慶日
    (12, 25), # 行憲紀念日
}

def is_taiwan_holiday(dt: datetime) -> bool:
    """週六週日或國定假日回傳 True，農曆假日請手動更新 TW_FIXED_HOLIDAYS。"""
    if dt.weekday() >= 5:  # 0=Mon … 6=Sun，5=Sat, 6=Sun
        return True
    return (dt.month, dt.day) in TW_FIXED_HOLIDAYS

SPREADSHEET_ID = "14GLya7CgPe9TfEuNx2L30LAznzcFtiGSvU8gHPcOqd4"
SHEET_NAME = "To Do list"
TOKEN_URL = "https://oauth2.googleapis.com/token"
LINE_PUSH_URL = "https://api.line.me/v2/bot/message/push"


# ── Google Sheets ─────────────────────────────────────────────────────────────

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


def fetch_sheet_values(access_token: str) -> list[list[str]]:
    range_param = urllib.parse.quote(f"{SHEET_NAME}!A:F")
    url = (
        f"https://sheets.googleapis.com/v4/spreadsheets/"
        f"{SPREADSHEET_ID}/values/{range_param}"
    )
    resp = requests.get(url, headers={"Authorization": f"Bearer {access_token}"}, timeout=20)
    resp.raise_for_status()
    return resp.json().get("values", [])


# ── 日期解析 ──────────────────────────────────────────────────────────────────

def date_key(dt: datetime) -> str:
    """把 datetime 轉成 gsheet 裡 A 欄的格式，例如 6/28"""
    return f"{dt.month}/{dt.day}"


def parse_rows(rows: list[list[str]], now: datetime) -> dict:
    """
    從所有 rows 找出 today / yesterday / tomorrow 的資料。
    回傳 dict: { 'today': [...], 'yesterday': [...], 'tomorrow': [...] }
    每筆是 { 'project': str, 'tasks': str, 'note': str }
    """
    today_key = date_key(now)
    yesterday_key = date_key(now - timedelta(days=1))
    tomorrow_key = date_key(now + timedelta(days=1))

    result = {"today": [], "yesterday_incomplete": [], "tomorrow": []}
    current_date = None
    current_project = ""

    for row in rows[1:]:  # skip header
        date_cell = (row[0].strip() if len(row) > 0 else "").strip()
        project_cell = (row[1].strip() if len(row) > 1 else "")
        task_cell = (row[2].strip() if len(row) > 2 else "")
        note_cell = (row[5].strip() if len(row) > 5 else "")

        if date_cell:
            current_date = date_cell
            current_project = project_cell

        if current_date == today_key:
            result["today"].append({
                "project": current_project,
                "tasks": task_cell,
                "note": note_cell,
            })
        elif current_date == yesterday_key:
            # 只保留未打 ✓ 完成的段落
            if task_cell and "✓" not in task_cell:
                result["yesterday_incomplete"].append({
                    "project": current_project,
                    "tasks": task_cell,
                    "note": note_cell,
                })
        elif current_date == tomorrow_key:
            result["tomorrow"].append({
                "project": current_project,
                "tasks": task_cell,
                "note": note_cell,
            })

    return result


# ── Claude 整理格式 ───────────────────────────────────────────────────────────

def format_brief(data: dict, now: datetime) -> str:
    date_str = now.strftime("%m/%d（%a）")
    weekday_map = {"Mon": "一", "Tue": "二", "Wed": "三", "Thu": "四",
                   "Fri": "五", "Sat": "六", "Sun": "日"}
    for en, zh in weekday_map.items():
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
                parts.append(f"📌 Note：{r['note']}")
        return "\n".join(parts) if parts else "（無）"

    raw_today = rows_to_text(data["today"])
    raw_yesterday = rows_to_text(data["yesterday_incomplete"])
    raw_tomorrow = rows_to_text(data["tomorrow"])

    prompt = f"""你是 Helen，Peggy 的生活＆工作助理。
以下是從 Daily.gsheet 讀到的原始資料，請整理成每日晨報，直接輸出 LINE 訊息內容。

格式規則：
- 純文字，不使用 Markdown（禁止 ##、**、---、表格）
- 條列用「・」
- 用 emoji 分區
- 段落間空一行
- 語氣像朋友，簡潔口語

今日日期：{date_str}

【今天待辦】
{raw_today}

【昨天未完成】
{raw_yesterday}

【明天預覽】
{raw_tomorrow}

請把以上整理成清楚好讀的晨報，移除重複或空白內容，若有 ✓ 已完成的項目可簡短提一下（用「✅」標記），未完成的用「・」條列。最後加一句輕鬆的打氣話。"""

    client = anthropic.Anthropic(api_key=os.environ["ANTHROPIC_API_KEY"])
    msg = client.messages.create(
        model="claude-haiku-4-5-20251001",
        max_tokens=1000,
        messages=[{"role": "user", "content": prompt}],
    )
    return msg.content[0].text.strip()


# ── LINE push ─────────────────────────────────────────────────────────────────

def push_line(text: str) -> None:
    user_id = os.environ["LINE_USER_ID"]
    token = os.environ["LINE_CHANNEL_ACCESS_TOKEN"]
    payload = {
        "to": user_id,
        "messages": [{"type": "text", "text": text}],
    }
    resp = requests.post(
        LINE_PUSH_URL,
        headers={"Authorization": f"Bearer {token}", "Content-Type": "application/json"},
        json=payload,
        timeout=20,
    )
    resp.raise_for_status()
    print(f"LINE push OK: {resp.status_code}")


# ── 主流程 ────────────────────────────────────────────────────────────────────

def main():
    now = datetime.now(TAIPEI)
    print(f"[Helen] {now.strftime('%Y-%m-%d %H:%M')} 台北時間 開始執行", flush=True)

    if is_taiwan_holiday(now):
        print(f"[Helen] 今天是假日（{now.strftime('%Y-%m-%d %a')}），跳過推播。", flush=True)
        return

    print("讀取 Google Sheets...", flush=True)
    access_token = get_sheets_access_token()
    rows = fetch_sheet_values(access_token)
    print(f"讀到 {len(rows)} 列", flush=True)

    data = parse_rows(rows, now)
    print(f"今天 {len(data['today'])} 筆 / 昨天未完成 {len(data['yesterday_incomplete'])} 筆 / 明天 {len(data['tomorrow'])} 筆", flush=True)

    print("Claude 整理格式...", flush=True)
    brief = format_brief(data, now)
    print("--- 晨報內容 ---")
    print(brief)
    print("----------------")

    print("LINE push...", flush=True)
    push_line(f"[Helen]\n{brief}")
    print("[Helen] 晨報推播完成", flush=True)


if __name__ == "__main__":
    main()
