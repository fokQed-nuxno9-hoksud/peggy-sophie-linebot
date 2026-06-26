import os
import hmac
import hashlib
import base64
import json
import re
import requests
from datetime import datetime
from zoneinfo import ZoneInfo
from flask import Flask, request, abort

app = Flask(__name__)

LINE_CHANNEL_SECRET = os.environ["LINE_CHANNEL_SECRET"]
LINE_CHANNEL_ACCESS_TOKEN = os.environ["LINE_CHANNEL_ACCESS_TOKEN"]
GEMINI_API_KEY = os.environ["GEMINI_API_KEY"]
NOTION_TOKEN = os.environ["NOTION_TOKEN"]
NOTION_TASKS_DB_ID = os.environ["NOTION_TASKS_DB_ID"]
LINE_USER_ID = os.environ.get("LINE_USER_ID", "")

GEMINI_URL = f"https://generativelanguage.googleapis.com/v1beta/models/gemini-2.5-flash:generateContent?key={GEMINI_API_KEY}"
DAILY_SHEET_ID = "14GLya7CgPe9TfEuNx2L30LAznzcFtiGSvU8gHPcOqd4"
WEEKDAYS = ["一", "二", "三", "四", "五", "六", "日"]

# ── System prompts ──────────────────────────────────────────────

SOPHIE_PROMPT = """你是 Sophie，Peggy 的工作 AI 助理。
Peggy 在台灣 JIDIEN（碁電）機器視覺部門擔任業務工程師，負責對接客戶、挑選相機/鏡頭/光源、打報價單、追蹤案件。

你的任務：
- 根據工作日誌試算表整理今日待辦與近期提醒
- 協助起草中英文客戶信件
- 協助整理報價單與技術規格
- 提供工作建議與案件追蹤支援

規則：
- 一律用繁體中文，語氣自然像朋友
- 中文與英文/數字間加半形空格（例：有 3 台相機）
- 專業術語保留英文（Camera, Lens, FOV, GigE 等）
- 日期格式：M/D（週幾），例如 6/26（五）"""

LISA_PROMPT = """你是 Lisa，Peggy 的財經助理，專注美股與台股。

觀察清單：
台股 ETF：0050
台股個股：2330（台積電）、2454（聯發科）、3443、5347、3189、3711、3551、2457、6187、8284
美股 ETF：VOO、QQQ、QQQM、SMH、VT
美股個股：GLW、ASML、MSFT、NVDA、TSLA、RKLB、MRVL、V、GOOGL、AMD、MU、INTC、TXN、AAPL、QCOM、KLAC、AMAT、ORCL

規則：
- 一律用繁體中文，語氣自然像朋友
- 財經數字、漲跌幅用條列式呈現
- 專有名詞（Fed、CPI、ETF 等）保留英文
- 可討論個股分析、市場趨勢、投資觀念
- 說明：我目前沒有即時報價能力，分析以你提供的資訊或近期已知市況為主"""

HELEN_PROMPT = """你是 Helen，Peggy 的生活雜事管家。
負責行程管理、待辦清單、購物清單、帳單提醒、預約事務。

規則：
- 一律用繁體中文，語氣輕鬆親切
- 生活事項用條列式整理
- 可查詢 Notion 待辦清單（工具：get_tasks）
- 可新增任務到 Notion（工具：create_task）

工具格式範例：
查任務：[TOOL: get_tasks]
新增任務：[TOOL: create_task] {"title": "買水果", "date": "2026-06-27"}

若使用者說「Today's plan」，主動查詢並整理今日待辦清單。"""

# ── Agent routing ────────────────────────────────────────────────

def detect_agent(message: str) -> str:
    msg = message.lower()
    if "sophie" in msg or "to do list" in msg:
        return "sophie"
    elif "lisa" in msg or any(w in msg for w in ["股票", "美股", "台股", "財經", "nvda", "tsla", "etf", "fed", "漲跌"]):
        return "lisa"
    elif "helen" in msg or "today's plan" in msg or "today plan" in msg:
        return "helen"
    return "sophie"

AGENT_PROMPTS = {"sophie": SOPHIE_PROMPT, "lisa": LISA_PROMPT, "helen": HELEN_PROMPT}
AGENT_NAMES = {"sophie": "Sophie", "lisa": "Lisa", "helen": "Helen"}

# ── Google Sheet ─────────────────────────────────────────────────

def get_daily_sheet() -> str:
    url = f"https://docs.google.com/spreadsheets/d/{DAILY_SHEET_ID}/export?format=csv&gid=0"
    try:
        resp = requests.get(url, timeout=15, allow_redirects=True)
        ct = resp.headers.get("Content-Type", "")
        if resp.status_code == 200 and ("csv" in ct or not resp.text.strip().startswith("<")):
            return resp.text[:4000]
        return "（試算表尚未設為公開，無法讀取。請設定「知道連結的人可以檢視」。）"
    except Exception as e:
        return f"（讀取失敗：{e}）"

# ── Notion tools ─────────────────────────────────────────────────

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
        title_prop = props.get("Name") or props.get("Task") or {}
        title_list = title_prop.get("title") or title_prop.get("rich_text") or []
        title = "".join(t.get("plain_text", "") for t in title_list) or "（無標題）"
        date_prop = props.get("Date", {}).get("date") or {}
        date_str = date_prop.get("start", "")
        lines.append(f"• {title}" + (f"（{date_str}）" if date_str else ""))
    return "待辦清單：\n" + "\n".join(lines)


def create_task(title: str, date: str = None) -> str:
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
        json={"parent": {"database_id": NOTION_TASKS_DB_ID}, "properties": props},
    )
    if resp.status_code == 200:
        return f"已新增任務：「{title}」" + (f"（{date}）" if date else "")
    return f"新增失敗：{resp.text[:200]}"

# ── LINE helpers ──────────────────────────────────────────────────

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
        json={"replyToken": reply_token, "messages": [{"type": "text", "text": text[:5000]}]},
    )


def push_line(user_id: str, text: str):
    requests.post(
        "https://api.line.me/v2/bot/message/push",
        headers={
            "Authorization": f"Bearer {LINE_CHANNEL_ACCESS_TOKEN}",
            "Content-Type": "application/json",
        },
        json={"to": user_id, "messages": [{"type": "text", "text": text[:5000]}]},
    )

# ── Gemini ────────────────────────────────────────────────────────

def call_gemini(system_prompt: str, user_message: str) -> str:
    payload = {
        "system_instruction": {"parts": [{"text": system_prompt}]},
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
        result = create_task(args.get("title", "新任務"), args.get("date"))
    else:
        result = f"不認識的工具：{tool_name}"

    return f"{before_tool}\n\n{result}".strip() if before_tool else result

# ── Webhook ──────────────────────────────────────────────────────

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

        user_text = event["message"]["text"].strip()
        reply_token = event["replyToken"]
        user_id = event.get("source", {}).get("userId", "")

        # 查詢自己的 LINE User ID
        if user_text.lower() in ["我的line id", "my line id", "line id"]:
            reply_line(reply_token, f"你的 LINE User ID 是：\n{user_id}")
            continue

        agent = detect_agent(user_text)

        # Sophie + To do list → 讀 Google Sheet
        if agent == "sophie" and "to do list" in user_text.lower():
            taipei = ZoneInfo("Asia/Taipei")
            now = datetime.now(taipei)
            today_str = f"{now.month}/{now.day}（{WEEKDAYS[now.weekday()]}）"

            sheet_csv = get_daily_sheet()
            prompt_msg = f"""今天是 {today_str}。

以下是 Peggy 的工作日誌試算表（CSV 格式）：
{sheet_csv}

請根據以上資料整理出：

一、今天 {today_str} 的工作待辦清單（有幾項列幾項，附上備註/細節）
二、近期重要提醒（未來 2 週內，含日期與週幾）

輸出格式：
今天要做的事

1. 事項 — 說明
2. 事項 — 說明

近期重要提醒
- M/D（週幾）— 事項

最後一行：簡短詢問是否需要幫她起草 mail 或整理資料。"""
            ai_reply = call_gemini(SOPHIE_PROMPT, prompt_msg)
            reply_line(reply_token, f"[Sophie]\n{ai_reply}")
            continue

        # 一般 AI 回應
        system_prompt = AGENT_PROMPTS[agent]
        agent_name = AGENT_NAMES[agent]
        ai_reply = call_gemini(system_prompt, user_text)
        final_reply = handle_tool(ai_reply)
        reply_line(reply_token, f"[{agent_name}]\n{final_reply}")

    return "OK"


@app.route("/push_lisa", methods=["POST"])
def push_lisa():
    target_id = LINE_USER_ID or request.json.get("user_id", "")
    message = request.json.get("message", "")
    if not target_id or not message:
        return {"error": "缺少 user_id 或 message"}, 400
    push_line(target_id, message)
    return {"ok": True}


@app.route("/health")
def health():
    return "OK"


if __name__ == "__main__":
    port = int(os.environ.get("PORT", 8080))
    app.run(host="0.0.0.0", port=port)
