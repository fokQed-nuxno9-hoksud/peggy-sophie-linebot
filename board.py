"""Peggy LifeOS — Agent 團隊看板（/board）

獨立 Blueprint，main.py 只負責掛載。功能：
- GET  /board                 看板頁面（需 ?key=BOARD_KEY）
- GET  /board/api/data        看板內容 + 伺服器狀態 + Render 排程即時查證
- POST /board/api/todo        待確認事項打勾（跨裝置同步）
- POST /board/api/message     留言給 Sophie → 即時推播到 Peggy LifeOS LINE
- POST /board/api/content     Sophie 從本機更新看板內容（不用重新 deploy）

儲存：board_content.json（看板內容，repo 有 baseline，可在執行期覆寫）、
     board_state.json（打勾與留言，執行期資料，redeploy 會重置；留言以 LINE 推播為準）。
"""

import hmac
import json
import os
import re
import threading
import time
from datetime import datetime, timezone, timedelta

import requests
from flask import Blueprint, request, jsonify, send_file

board_bp = Blueprint("board", __name__)

BOARD_KEY = os.environ.get("BOARD_KEY", "")
RENDER_API_KEY = os.environ.get("RENDER_API_KEY", "")
LINE_CHANNEL_ACCESS_TOKEN = os.environ.get("LINE_CHANNEL_ACCESS_TOKEN", "")
LINE_USER_ID = os.environ.get("LINE_USER_ID", "")

_DIR = os.path.dirname(__file__)
CONTENT_PATH = os.path.join(_DIR, "board_content.json")
STATE_PATH = os.path.join(_DIR, "board_state.json")
PAGE_PATH = os.path.join(_DIR, "board.html")

TAIPEI = timezone(timedelta(hours=8))

# Render cron services（即時查證用）
CRON_SERVICES = [
    {"agent": "sophie", "label": "08:30 工作晨報", "id": "crn-d90mtivavr4c738sbj20"},
    {"agent": "helen", "label": "08:30 生活晨報", "id": "crn-d9095mb7uimc7396dbn0"},
    {"agent": "lisa", "label": "07:00 財經日報", "id": "crn-d906n2gk1i2s73fd5eu0"},
]

_lock = threading.Lock()


# ── auth ──────────────────────────────────────────────────────────

def _authorized() -> bool:
    key = request.args.get("key", "") or request.headers.get("X-Board-Key", "")
    return bool(BOARD_KEY) and hmac.compare_digest(key, BOARD_KEY)


# ── storage ───────────────────────────────────────────────────────

def _load_json(path, default):
    try:
        with open(path, encoding="utf-8") as f:
            return json.load(f)
    except Exception:
        return default


def _save_json(path, data):
    with open(path, "w", encoding="utf-8") as f:
        json.dump(data, f, ensure_ascii=False, indent=1)


def _load_state():
    return _load_json(STATE_PATH, {"todos_done": [], "messages": []})


# ── LINE push（自帶，不從 main 匯入以免循環引用）──────────────────

def _push_line(text: str):
    requests.post(
        "https://api.line.me/v2/bot/message/push",
        headers={
            "Authorization": f"Bearer {LINE_CHANNEL_ACCESS_TOKEN}",
            "Content-Type": "application/json",
        },
        json={"to": LINE_USER_ID, "messages": [{"type": "text", "text": text[:5000]}]},
        timeout=15,
    )


# ── Render 排程即時查證（60 秒快取）────────────────────────────────

_live_cache = {"ts": 0.0, "data": None}


def _fmt_taipei(ts_str: str) -> str:
    ts_str = re.sub(r"\.\d+", "", ts_str).replace("Z", "+00:00")
    try:
        dt = datetime.fromisoformat(ts_str).astimezone(TAIPEI)
        return dt.strftime("%m/%d %H:%M")
    except Exception:
        return ts_str


def _fetch_live_status() -> dict:
    now = time.time()
    if _live_cache["data"] is not None and now - _live_cache["ts"] < 60:
        return _live_cache["data"]

    live = {"checked_at": datetime.now(TAIPEI).strftime("%H:%M"), "crons": {}}
    headers = {"Authorization": f"Bearer {RENDER_API_KEY}", "Accept": "application/json"}
    for svc in CRON_SERVICES:
        entry = {"label": svc["label"], "status": "unknown", "time": "", "ok": None}
        try:
            r = requests.get(
                f"https://api.render.com/v1/services/{svc['id']}/events?limit=10",
                headers=headers, timeout=10,
            )
            for e in r.json():
                d = e.get("event", e)
                if d.get("type") == "cron_job_run_ended":
                    status = d.get("details", {}).get("status", "unknown")
                    entry["status"] = status
                    entry["time"] = _fmt_taipei(d.get("timestamp", ""))
                    entry["ok"] = status == "successful"
                    break
        except Exception:
            pass
        live["crons"][svc["agent"]] = entry

    # Eva 跑在本服務上：這支 API 有回應就代表服務活著
    live["crons"]["eva"] = {
        "label": "LINE 客服服務", "status": "running",
        "time": datetime.now(TAIPEI).strftime("%m/%d %H:%M"), "ok": True,
    }
    _live_cache["data"] = live
    _live_cache["ts"] = now
    return live


# ── routes ────────────────────────────────────────────────────────

@board_bp.route("/board")
def board_page():
    if not _authorized():
        return "Not found", 404
    return send_file(PAGE_PATH)


@board_bp.route("/board/api/data")
def board_data():
    if not _authorized():
        return jsonify({"error": "unauthorized"}), 401
    content = _load_json(CONTENT_PATH, {})
    state = _load_state()
    return jsonify({"content": content, "state": state, "live": _fetch_live_status()})


@board_bp.route("/board/api/todo", methods=["POST"])
def board_todo():
    if not _authorized():
        return jsonify({"error": "unauthorized"}), 401
    body = request.get_json(silent=True) or {}
    todo_id = str(body.get("id", "")).strip()
    done = bool(body.get("done"))
    if not todo_id:
        return jsonify({"error": "缺少 id"}), 400
    with _lock:
        state = _load_state()
        done_list = state.get("todos_done", [])
        if done and todo_id not in done_list:
            done_list.append(todo_id)
        if not done and todo_id in done_list:
            done_list.remove(todo_id)
        state["todos_done"] = done_list
        _save_json(STATE_PATH, state)
    return jsonify({"ok": True, "todos_done": done_list})


@board_bp.route("/board/api/message", methods=["POST"])
def board_message():
    if not _authorized():
        return jsonify({"error": "unauthorized"}), 401
    body = request.get_json(silent=True) or {}
    text = str(body.get("text", "")).strip()
    if not text:
        return jsonify({"error": "留言內容是空的"}), 400
    ts = datetime.now(TAIPEI).strftime("%Y/%m/%d %H:%M")
    with _lock:
        state = _load_state()
        state.setdefault("messages", []).append({"text": text, "ts": ts})
        _save_json(STATE_PATH, state)
    try:
        _push_line(f"[Sophie]\n📋 看板留言收到\n\n・{text}\n\n已記下，處理後回報。")
        pushed = True
    except Exception as e:
        print(f"[Board] LINE push error: {e}")
        pushed = False
    return jsonify({"ok": True, "pushed": pushed, "ts": ts})


@board_bp.route("/board/api/message/remove", methods=["POST"])
def board_message_remove():
    if not _authorized():
        return jsonify({"error": "unauthorized"}), 401
    body = request.get_json(silent=True) or {}
    ts = str(body.get("ts", ""))
    text = str(body.get("text", ""))
    with _lock:
        state = _load_state()
        state["messages"] = [
            m for m in state.get("messages", [])
            if not (m.get("ts") == ts and m.get("text") == text)
        ]
        _save_json(STATE_PATH, state)
    return jsonify({"ok": True})


@board_bp.route("/board/api/content", methods=["POST"])
def board_content_update():
    """Sophie 從本機推新內容（例如每日更新），不用重新 deploy。"""
    if not _authorized():
        return jsonify({"error": "unauthorized"}), 401
    body = request.get_json(silent=True)
    if not isinstance(body, dict) or "agents" not in body:
        return jsonify({"error": "內容格式不對，需要含 agents 的 JSON"}), 400
    with _lock:
        _save_json(CONTENT_PATH, body)
    return jsonify({"ok": True})
