import os
import hmac
import hashlib
import base64
import json
import re
import subprocess
from datetime import datetime
from zoneinfo import ZoneInfo

import requests
from flask import Flask, request, abort

app = Flask(__name__)

LINE_CHANNEL_SECRET = os.environ["LINE_CHANNEL_SECRET"]
LINE_CHANNEL_ACCESS_TOKEN = os.environ["LINE_CHANNEL_ACCESS_TOKEN"]
GEMINI_API_KEY = os.environ["GEMINI_API_KEY"]
NOTION_TOKEN = os.environ["NOTION_TOKEN"]
NOTION_TASKS_DB_ID = os.environ["NOTION_TASKS_DB_ID"]

GEMINI_URL = f"https://generativelanguage.googleapis.com/v1beta/models/gemini-2.5-flash:generateContent?key={GEMINI_API_KEY}"

SYSTEM_PROMPT = """你是 Sophie，Peggy 的 AI 助理。你也可以扮演 Helen（生活管家）或 Lisa（財經助理）。
現在透過 LINE 跟 Peggy 對話。

規則：
- 一律用繁體中文，語氣自然像朋友
- 中文與英文/數字間加半形空格（例：有 3 件事、iPhone 15）
- 可以查詢 Notion 任務（工具：get_tasks）
- 可以新增 Notion 任務（工具：create_task，需要 title 和可選的 date）
- 若要執行工具，回覆格式：[TOOL: tool_name] 參數JSON

工具格式範例：
查任務：[TOOL: get_tasks]
新增任務：[TOOL: create_task] {"title": "買水果", "date": "2026-06-27"}
"""


def verify_signature(body: bytes, signature: str) -> bool:
    hash_value = hmac.new(
        LINE_CHANNEL_SECRET.encode("utf-8"), body, hashlib.sha256
    ).digest()
    expected = base64.b64encode(hash_value).decode("utf-8")
    return hmac.compare_digest(expected, signature)


def reply_line(reply_token: str, text: str):
    requests.post(
        "https://api.line.me/v2/bot/message/reply",
        headers={
            "Authorization": f"Bearer {LINE_CHANNEL_ACCESS_TOKEN}",
            "Content-Type": "application/json",
        },
        json={
            "replyToken": reply_token,
            "messages": [{"type": "text", "text": text[:5000]}],
        },
    )


def get_tasks() -> str:
    resp = requests.post(
        f"https://api.notion.com/v1/databases/{NOTION_TASKS_DB_ID}/query",
        headers={
            "Authorization": f"Bearer {NOTION_TOKEN}",
            "Notion-Version": "2022-06-28",
            "Content-Type": "application/json",
        },
        json={
            "filter": {
                "and": [
                    {"property": "Done", "checkbox": {"equals": False}},
                    {"property": "Inbox (To Do Dump)", "checkbox": {"equals": True}},
                ]
            },
            "sorts": [{"property": "Date", "direction": "ascending"}],
            "page_size": 20,
        },
    )
    results = resp.json().get("results", [])
    if not results:
        return "目前沒有待辦事項。"
    lines = []
    for r in results:
        props = r.get("properties", {})
        title_prop = props.get("Name") or props.get("Task") or props.get("title") or {}
        title_list = (title_prop.get("title") or title_prop.get("rich_text") or [])
        title = "".join(t.get("plain_text", "") for t in title_list) or "（無標題）"
        date_prop = props.get("Date", {}).get("date") or {}
        date_str = date_prop.get("start", "")
        lines.append(f"• {title}" + (f"（{date_str}）" if date_str else ""))
    return "待辦清單：\n" + "\n".join(lines)


def create_task(title: str, date: str = None) -> str:
    tz = ZoneInfo("Asia/Taipei")
    props = {
        "Name": {"title": [{"text": {"content": title}}]},
        "Inbox (To Do Dump)": {"checkbox": True},
    }
    if date:
        props["Date"] = {"date": {"start": date}}
    resp = requests.post(
        "https://api.notion.com/v1/pages",
        headers={
            "Authorization": f"Bearer {NOTION_TOKEN}",
            "Notion-Version": "2022-06-28",
            "Content-Type": "application/json",
        },
        json={
            "parent": {"database_id": NOTION_TASKS_DB_ID},
            "properties": props,
        },
    )
    if resp.status_code == 200:
        return f"已新增任務：「{title}」" + (f"（{date}）" if date else "")
    return f"新增失敗：{resp.text[:200]}"


def call_gemini(user_message: str) -> str:
    payload = {
        "system_instruction": {"parts": [{"text": SYSTEM_PROMPT}]},
        "contents": [{"role": "user", "parts": [{"text": user_message}]}],
    }
    resp = requests.post(GEMINI_URL, json=payload)
    data = resp.json()
    if "candidates" in data:
        return data["candidates"][0]["content"]["parts"][0]["text"]
    return "抱歉，AI 暫時無法回應，請稍後再試。"


def handle_tool(ai_reply: str) -> str:
    tool_match = re.search(r"\[TOOL:\s*(\w+)\](.*?)(?=\[TOOL:|$)", ai_reply, re.DOTALL)
    if not tool_match:
        return ai_reply

    tool_name = tool_match.group(1).strip()
    tool_args_str = tool_match.group(2).strip()
    before_tool = ai_reply[: tool_match.start()].strip()

    if tool_name == "get_tasks":
        result = get_tasks()
    elif tool_name == "create_task":
        try:
            args = json.loads(tool_args_str) if tool_args_str else {}
        except json.JSONDecodeError:
            args = {}
        title = args.get("title", "新任務")
        date = args.get("date")
        result = create_task(title, date)
    else:
        result = f"不認識的工具：{tool_name}"

    combined = f"{before_tool}\n\n{result}".strip() if before_tool else result
    return combined


@app.route("/webhook", methods=["POST"])
def webhook():
    signature = request.headers.get("X-Line-Signature", "")
    body = request.get_data()

    if not verify_signature(body, signature):
        abort(400)

    events = request.json.get("events", [])
    for event in events:
        if event.get("type") != "message":
            continue
        if event["message"].get("type") != "text":
            continue

        user_text = event["message"]["text"]
        reply_token = event["replyToken"]

        ai_reply = call_gemini(user_text)
        final_reply = handle_tool(ai_reply)
        reply_line(reply_token, final_reply)

    return "OK"


@app.route("/health")
def health():
    return "OK"


if __name__ == "__main__":
    port = int(os.environ.get("PORT", 8080))
    app.run(host="0.0.0.0", port=port)
