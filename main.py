import csv
import io
import os
import hmac
import hashlib
import base64
import json
import re
import requests
import anthropic
from datetime import datetime, date, timedelta
from zoneinfo import ZoneInfo
from flask import Flask, request, abort

app = Flask(__name__)

LINE_CHANNEL_SECRET = os.environ["LINE_CHANNEL_SECRET"]
LINE_CHANNEL_ACCESS_TOKEN = os.environ["LINE_CHANNEL_ACCESS_TOKEN"]
ANTHROPIC_API_KEY = os.environ["ANTHROPIC_API_KEY"]
NOTION_TOKEN = os.environ["NOTION_TOKEN"]
NOTION_TASKS_DB_ID = os.environ["NOTION_TASKS_DB_ID"]
LINE_USER_ID = os.environ.get("LINE_USER_ID", "")

# Eva — JIDIEN_Peggy LINE OA
EVA_LINE_CHANNEL_SECRET = os.environ.get("EVA_LINE_CHANNEL_SECRET", "")
EVA_LINE_CHANNEL_ACCESS_TOKEN = os.environ.get("EVA_LINE_CHANNEL_ACCESS_TOKEN", "")

_claude = anthropic.Anthropic(api_key=ANTHROPIC_API_KEY)
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
- 語言：依 Peggy 使用的語言回覆（繁中 → 繁中、英文 → 英文、日文 → 日文，預設繁中）
- 語氣自然像朋友
- 中文與英文/數字間加半形空格（例：有 3 台相機）
- 專業術語保留英文（Camera, Lens, FOV, GigE 等）
- 日期格式：M/D（週幾），例如 6/26（五）
- LINE 訊息格式：禁止使用 Markdown（#、**、---、表格等），改用純文字、換行、emoji 分區，條列用「・」或數字"""

LISA_PROMPT = """你是 Lisa，Peggy 的財經助理，專注美股與台股。

觀察清單：
台股 ETF：0050
台股個股：2330（台積電）、2454（聯發科）、3443、5347、3189、3711、3551、2457、6187、8284
美股 ETF：VOO、QQQ、QQQM、SMH、VT
美股個股：GLW、ASML、MSFT、NVDA、TSLA、RKLB、MRVL、V、GOOGL、AMD、MU、INTC、TXN、AAPL、QCOM、KLAC、AMAT、ORCL

規則：
- 語言：依 Peggy 使用的語言回覆（繁中 → 繁中、英文 → 英文、日文 → 日文，預設繁中）
- 財經數字、漲跌幅用條列式呈現
- 專有名詞（Fed、CPI、ETF 等）保留英文
- 可討論個股分析、市場趨勢、投資觀念
- 說明：我目前沒有即時報價能力，分析以你提供的資訊或近期已知市況為主
- LINE 訊息格式：禁止使用 Markdown（#、**、---、表格等），改用純文字、換行、emoji 分區，條列用「・」或數字"""

HELEN_PROMPT = """你是 Helen，Peggy 的生活雜事管家。
負責行程管理、待辦清單、購物清單、帳單提醒、預約事務。

規則：
- 語言：依 Peggy 使用的語言回覆（繁中 → 繁中、英文 → 英文、日文 → 日文，預設繁中）
- 語氣輕鬆親切
- 生活事項用條列式整理
- 可查詢 Notion 待辦清單（工具：get_tasks）
- 可新增任務到 Notion（工具：create_task）
- LINE 訊息格式：禁止使用 Markdown（#、**、---、表格等），改用純文字、換行、emoji 分區，條列用「・」或數字

工具格式（嚴格遵守）：
查任務：[TOOL: get_tasks]
新增任務：[TOOL: create_task] {"title": "任務名稱", "date": "YYYY-MM-DD"}

重要：
- create_task 的 JSON 必須緊跟在 [TOOL: create_task] 後面，同一行
- title 欄位不可省略，必須是 Peggy 說的任務名稱原文
- date 欄位填最近一次的日期（每月固定日期取下一個月；若無具體日期可省略）
- 不要在工具指令前後加多餘說明，先輸出說明文字，最後一行才是工具指令

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

# ── Eva system prompt ────────────────────────────────────────────
EVA_PROMPT = """你是 Eva，JIDIEN_Peggy LINE 官方帳號的 AI 客服助理，代表業務工程師 Peggy（王冠懿）在 LINE 上回覆客戶詢問。

【公司背景】
JIDIEN 碁電（www.jidien.com）是台灣機器視覺整合商，代理並銷售：
- 工業相機（Area scan / Line scan / 3D）：Basler、大華、Sony、ALSON、ARTRAY、Imperx、OPT 等
- 鏡頭：CCS lens、Computar、FUJINON、Kragonfly、KOWA、Schneider、TAMRON、ZEISS、視清等
- 光源 & 光控器：CCS、RSEE、ILUMINA、丞基、登順 等（環形、同軸、背光、條型、穹頂）
- 影像擷取卡：Bitflow（Camera Link / CoaXPress）
- 影像分析軟體：MOZI（自有）、Neurocle（AI 深度學習）
- 3D 感測器：深視 SR 系列（3D 雷射輪廓掃描儀）
- IPC 工業電腦、讀碼器（Barcode / QR Code）

【你可以做的事】
A. 品牌/型號查詢：查知識庫回答「有代理 / 沒有代理」
B. 技術問題：解釋機器視覺相關概念（CCD vs CMOS、FOV、WD、光源類型等）
C. 應用場景推薦：根據客戶描述的應用（AOI、尺寸量測、PCB 檢測等）推薦合適的相機/鏡頭/光源類型
D. 初步選型計算：使用精度公式（pixel size × 3）或頻寬公式協助客戶估算需求
E. 品牌特色說明：介紹我們代理品牌的優勢與適用場景

【回覆規則】
1. 語言：依客戶使用的語言回覆（繁中 → 繁中、英文 → 英文、日文 → 日文，其餘語言預設繁中）
2. 語氣：親切但專業，不用太過正式，像在跟熟識的客戶朋友聊天；業界術語（GigE、AOI、FOV、CCD、WD 等）直接用，不刻意翻譯成白話
3. 回覆時一律用「我們」代替公司名稱
4. 推薦產品或解決方案前，先用一兩個問題確認客戶的應用場景或需求，再給建議（例如：「請問您的應用是做外觀檢測還是尺寸量測呢？」）
5. 以下情況一律回覆 [CONFIDENCE: LOW]，不嘗試回答：
   - 任何報價、折扣、價格詢問（絕對不透露任何數字）
   - 庫存數量、是否有現貨（「有現貨嗎」、「庫存多少」）← 注意：「有代理嗎」不屬於此類
   - 交期、出貨時間
   - 客製規格或特殊需求（需工程師評估）
6. 能根據知識庫或技術知識自信回答 → [CONFIDENCE: HIGH]
   ✅ 「有代理 XXX 嗎？」→ 查知識庫，直接回答有/沒有，[CONFIDENCE: HIGH]
   ✅ 技術問題、應用推薦、選型計算 → [CONFIDENCE: HIGH]
7. 超出知識範圍、不確定的技術細節 → [CONFIDENCE: LOW]
8. 若客戶問「你是 AI 嗎？」→ 誠實回答「我是 JIDIEN_Peggy 的 AI 助理 Eva，有技術問題我會盡力協助，需要時轉給業務同仁確認。」[CONFIDENCE: HIGH]
9. 保持完全政治中立，政治/宗教/社會爭議問題 → 「這個問題超出我的服務範圍」[CONFIDENCE: HIGH]
10. 不偽裝成真人

【產品知識庫】
{knowledge}

【回覆格式】
直接給回覆內容（不加任何說明或前綴），最後一行必須是 [CONFIDENCE: HIGH] 或 [CONFIDENCE: LOW]。

【LINE 訊息格式】
禁止使用 Markdown（#、**、---、表格 |---| 等），一律使用純文字：
・標題用換行＋文字取代（可搭配 emoji，例如「📌 規格說明」）
・條列用「・」或數字，不用「-」
・段落之間空一行"""

# ── Eva 知識庫 ────────────────────────────────────────────────────
def _load_eva_knowledge() -> str:
    kb_path = os.path.join(os.path.dirname(__file__), "eva_knowledge.txt")
    try:
        with open(kb_path, encoding="utf-8") as f:
            return f.read()
    except Exception:
        return "（知識庫暫時無法載入，請根據公司背景知識回答）"

EVA_KNOWLEDGE = _load_eva_knowledge()
EVA_PROMPT_FULL = EVA_PROMPT.replace("{knowledge}", EVA_KNOWLEDGE)

# ── Google Sheet ─────────────────────────────────────────────────

def fetch_and_format_today(today_date: date) -> str:
    """Parse Daily.gsheet CSV and format Sophie's reply directly in Python."""
    url = f"https://docs.google.com/spreadsheets/d/{DAILY_SHEET_ID}/export?format=csv&gid=0"
    try:
        resp = requests.get(url, timeout=20, allow_redirects=True)
        if resp.status_code != 200:
            return "（試算表讀取失敗，請確認已設為「知道連結的人可以檢視」）"
        rows = list(csv.reader(io.StringIO(resp.content.decode("utf-8"))))
    except Exception as e:
        return f"（讀取失敗：{e}）"

    year = today_date.year
    today_weekday = WEEKDAYS[today_date.weekday()]

    today_tasks: list[str] = []
    today_note = ""
    diary_yet_past: list[str] = []
    upcoming: list[tuple] = []

    for row in rows[1:]:
        if not row or not row[0].strip():
            continue
        date_str = row[0].strip()
        try:
            m, d = map(int, date_str.split("/"))
            row_date = date(year, m, d)
        except Exception:
            continue

        tasks_raw = row[2].strip() if len(row) > 2 else ""
        extra = row[3].strip() if len(row) > 3 else ""
        note = row[5].strip() if len(row) > 5 else ""

        # Past 10 days: collect diary yet markers
        if row_date < today_date and (today_date - row_date).days <= 10:
            if "diary yet" in extra.lower() or "diary yet" in tasks_raw.lower():
                diary_yet_past.append(f"{m}/{d}")

        # Today
        elif row_date == today_date:
            tasks: list[str] = []
            current = ""
            for line in tasks_raw.split("\n"):
                line = line.strip()
                if not line or line.startswith("--"):
                    continue
                if line.startswith("●"):
                    if current:
                        tasks.append(current)
                    current = line[1:].strip()
                elif current:
                    current += " " + line  # continuation line
            if current:
                tasks.append(current)
            today_tasks = tasks
            today_note = note

        # Upcoming (next 14 days)
        elif today_date < row_date <= today_date + timedelta(days=14):
            sub: list[str] = []
            for line in tasks_raw.split("\n"):
                line = line.strip()
                if not line or line.startswith("--"):
                    continue
                if line.startswith("●"):
                    t = line[1:].strip()
                    if "9點開例會" not in t:
                        sub.append(t)
            if sub:
                upcoming.append((row_date, sub, note))

    # ── Format output ──
    if not today_tasks:
        return f"今天 {today_date.month}/{today_date.day}（{today_weekday}）的日誌還沒有記錄喔！"

    lines: list[str] = ["今天要做的事\n"]
    for i, task in enumerate(today_tasks, 1):
        if "日誌" in task and diary_yet_past:
            task += f"（{', '.join(diary_yet_past)} diary yet，記得一起補）"
        lines.append(f"{i}. {task}")

    if upcoming:
        lines.append("\n────────────────\n近期重要提醒\n")
        for row_date, sub, note in upcoming:
            wday = WEEKDAYS[row_date.weekday()]
            date_label = f"{row_date.month}/{row_date.day}（{wday}）"
            combined = "；".join(sub)
            lines.append(f"・{date_label} — {combined}")

    if today_note:
        lines.append(f"\n────────────────\n小提醒：{today_note}")

    lines.append("\n需要我幫你起草什麼 mail，或整理資料嗎？")
    return "\n".join(lines)

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
        "Task": {"title": [{"text": {"content": title}}]},
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

# ── Claude ────────────────────────────────────────────────────────

_RATE_LIMIT_MSG_CUSTOMER = "感謝您的詢問！已收到您的問題，Peggy將盡快為您確認並回覆＾＾"
_RATE_LIMIT_MSG_INTERNAL = "AI 今日額度暫時達上限，明天恢復後繼續！"
_credit_exhausted_notified = False

def call_claude(system_prompt: str, user_message: str, customer_facing: bool = False) -> str:
    global _credit_exhausted_notified
    try:
        msg = _claude.messages.create(
            model="claude-haiku-4-5",
            max_tokens=1024,
            system=system_prompt,
            messages=[{"role": "user", "content": user_message}],
        )
        return msg.content[0].text
    except anthropic.RateLimitError:
        print("[Claude Error] Rate limit exceeded")
        return _RATE_LIMIT_MSG_CUSTOMER if customer_facing else _RATE_LIMIT_MSG_INTERNAL
    except anthropic.BadRequestError as e:
        if "credit" in str(e).lower() or "balance" in str(e).lower():
            print("[Claude Error] Credit exhausted")
            if not _credit_exhausted_notified:
                _credit_exhausted_notified = True
                push_line(LINE_USER_ID, "⚠️ Anthropic credit 耗盡！請至 console.anthropic.com 補充，目前 Eva / Sophie / Lisa / Helen 均無法回應。")
            return _RATE_LIMIT_MSG_CUSTOMER if customer_facing else "⚠️ AI credit 耗盡，請補充後再試。"
        print(f"[Claude Error] Bad request: {e}")
        return "抱歉，AI 暫時無法回應，請稍後再試。"
    except anthropic.APIError as e:
        print(f"[Claude Error] API error: {e}")
        return "抱歉，AI 暫時無法回應，請稍後再試。"
    except Exception as e:
        print(f"[Claude Error] Exception: {e}")
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
            print(f"[Helen] create_task JSON parse error, raw args: {tool_args_str!r}")
            args = {}
        title = args.get("title", "").strip()
        if not title:
            print(f"[Helen] create_task called with empty title, full ai_reply: {ai_reply!r}")
            result = "❌ 任務新增失敗：未能解析任務名稱，請重新說一次。"
        else:
            result = create_task(title, args.get("date"))
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

        # Sophie + To do list → parse Google Sheet directly
        if agent == "sophie" and "to do list" in user_text.lower():
            taipei = ZoneInfo("Asia/Taipei")
            today_date = datetime.now(taipei).date()
            reply_text = fetch_and_format_today(today_date)
            reply_line(reply_token, f"[Sophie]\n{reply_text}")
            continue

        # 一般 AI 回應
        taipei = ZoneInfo("Asia/Taipei")
        today_str = datetime.now(taipei).strftime("%Y-%m-%d")
        system_prompt = f"今天日期：{today_str}（台北時間）\n\n" + AGENT_PROMPTS[agent]
        agent_name = AGENT_NAMES[agent]
        ai_reply = call_claude(system_prompt, user_text)
        final_reply = handle_tool(ai_reply)
        reply_line(reply_token, f"[{agent_name}]\n{final_reply}")

    return "OK"


# ── Eva LINE helpers ──────────────────────────────────────────────

def eva_reply_line(reply_token: str, text: str):
    requests.post(
        "https://api.line.me/v2/bot/message/reply",
        headers={
            "Authorization": f"Bearer {EVA_LINE_CHANNEL_ACCESS_TOKEN}",
            "Content-Type": "application/json",
        },
        json={"replyToken": reply_token, "messages": [{"type": "text", "text": text[:5000]}]},
    )


def eva_push_line(user_id: str, text: str):
    requests.post(
        "https://api.line.me/v2/bot/message/push",
        headers={
            "Authorization": f"Bearer {EVA_LINE_CHANNEL_ACCESS_TOKEN}",
            "Content-Type": "application/json",
        },
        json={"to": user_id, "messages": [{"type": "text", "text": text[:5000]}]},
    )


def notify_peggy_for_review(customer_id: str, customer_msg: str, draft: str):
    """推播給 Peggy，提醒她親自回覆客戶的敏感問題。"""
    notice = (
        f"【JIDIEN_Peggy OA 客戶詢問】\n"
        f"客戶訊息：{customer_msg}\n\n"
        f"⚠️ 此問題涉及價格／庫存／交期，請你親自回覆。\n"
        f"→ 請至 LINE OA 後台回覆（客戶 ID：{customer_id}）"
    )
    push_line(LINE_USER_ID, notice)


def verify_eva_signature(body: bytes, signature: str) -> bool:
    hash_value = hmac.new(
        EVA_LINE_CHANNEL_SECRET.encode("utf-8"), body, hashlib.sha256
    ).digest()
    expected = base64.b64encode(hash_value).decode("utf-8")
    return hmac.compare_digest(expected, signature)


# ── Eva webhook ───────────────────────────────────────────────────

@app.route("/eva/webhook", methods=["POST"])
def eva_webhook():
    if not EVA_LINE_CHANNEL_SECRET or not EVA_LINE_CHANNEL_ACCESS_TOKEN:
        return "Eva not configured", 503

    signature = request.headers.get("X-Line-Signature", "")
    body = request.get_data()

    if not verify_eva_signature(body, signature):
        abort(400)

    events = request.json.get("events", [])
    for event in events:
        if event.get("type") != "message":
            continue
        if event["message"].get("type") != "text":
            continue

        user_text = event["message"]["text"].strip()
        reply_token = event["replyToken"]
        customer_id = event.get("source", {}).get("userId", "unknown")

        taipei = ZoneInfo("Asia/Taipei")
        today_str = datetime.now(taipei).strftime("%Y-%m-%d")
        eva_prompt_with_date = f"今天日期：{today_str}（台北時間）\n\n" + EVA_PROMPT_FULL
        ai_reply = call_claude(eva_prompt_with_date, user_text, customer_facing=True)

        # 解析 confidence
        confidence = "LOW"
        if "[CONFIDENCE: HIGH]" in ai_reply:
            confidence = "HIGH"
        clean_reply = ai_reply.replace("[CONFIDENCE: HIGH]", "").replace("[CONFIDENCE: LOW]", "").strip()

        if confidence == "HIGH":
            eva_reply_line(reply_token, clean_reply)
        else:
            # 草稿模式：先回覆客戶「稍候」，再通知 Peggy
            eva_reply_line(reply_token, "感謝您的詢問！我們收到您的問題，業務同仁將盡快為您確認並回覆。")
            notify_peggy_for_review(customer_id, user_text, clean_reply)

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
