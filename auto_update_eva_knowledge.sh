#!/bin/bash
# ──────────────────────────────────────────────────────────────────
# Eva 知識庫每週自動更新
# 由 launchd（com.peggy.eva-knowledge-update）每週一 20:00 觸發
#
# 流程：重建 eva_knowledge.txt → 有變動才 commit/push → 觸發 Render 部署
# 全部輸出寫到 logs/auto_update_eva.log
# ──────────────────────────────────────────────────────────────────

REPO="/Users/user/Downloads/Peggy_agent/line_bot"
PYTHON="/opt/anaconda3/bin/python3"
WEB_ID="srv-d8v523uq1p3s73bffdg0"
LOGDIR="$REPO/logs"
LOG="$LOGDIR/auto_update_eva.log"
mkdir -p "$LOGDIR"

# 失敗時推 LINE 通知 Peggy 本人；成功不通知。
# 明確定義通知對象：
#   發送 bot = 「Peggy LifeOS」官方帳號（Sophie/Lisa/Helen 共用），用 LINE_CHANNEL_ACCESS_TOKEN
#   接收人 = Peggy 本人，用 LINE_USER_ID（晨報就是推到這裡）
#   訊息前綴 [Sophie]，出現在「Peggy LifeOS」聊天室
#   ★ 絕不使用 Eva 的「JIDIN_Peggy」客戶帳號（EVA_LINE_*），客戶看不到此通知
notify_sophie() {
  local MSG="$1"
  local TOKEN UID_
  TOKEN=$(grep '^LINE_CHANNEL_ACCESS_TOKEN=' "$REPO/../.env" | cut -d= -f2- | tr -d "\"' ")
  UID_=$(grep '^LINE_USER_ID=' "$REPO/../.env" | cut -d= -f2- | tr -d "\"' ")
  [ -z "$TOKEN" ] && return
  curl -s -o /dev/null -X POST "https://api.line.me/v2/bot/message/push" \
    -H "Authorization: Bearer $TOKEN" -H "Content-Type: application/json" \
    -d "{\"to\":\"$UID_\",\"messages\":[{\"type\":\"text\",\"text\":\"$MSG\"}]}"
}

{
  echo "===== $(date '+%Y-%m-%d %H:%M:%S') 開始自動更新 Eva 知識庫 ====="

  cd "$REPO" || { echo "❌ 無法進入 $REPO"; echo "===== 中止 ====="; exit 1; }

  # 1. 重建知識庫（讀牌價表 + 公司資訊 + 自動抓商城分類）
  "$PYTHON" build_eva_knowledge.py
  if [ $? -ne 0 ]; then
    echo "❌ build_eva_knowledge.py 執行失敗，不 push"
    notify_sophie "[Sophie] ⚠️ Eva 知識庫週更失敗：build_eva_knowledge.py 執行錯誤（可能牌價表沒同步）。請查 logs/auto_update_eva.log"
    echo "===== 中止 $(date '+%H:%M:%S') ====="
    exit 1
  fi

  # 2. 有變動才 commit + push（知識庫 + 完整型號索引）
  if git diff --quiet eva_knowledge.txt eva_models_index.txt; then
    echo "ℹ️ 知識庫與型號索引皆無變動，略過 commit / push"
  else
    git add eva_knowledge.txt eva_models_index.txt
    git commit -m "chore: Eva 知識庫每週自動更新 $(date '+%Y-%m-%d')"
    if git push origin main; then
      echo "✅ 已 push 更新"
      # 3. 觸發 Render web 服務部署
      RENDER_KEY=$(grep '^RENDER_API_KEY=' "$REPO/../.env" | cut -d= -f2- | tr -d '"'"'"' ')
      curl -s -o /dev/null -w "Render 部署觸發 HTTP %{http_code}\n" -X POST \
        "https://api.render.com/v1/services/$WEB_ID/deploys" \
        -H "Authorization: Bearer $RENDER_KEY" \
        -H "Content-Type: application/json" -d '{}'
    else
      echo "❌ git push 失敗（可能遠端有更新或網路問題），請手動處理"
      notify_sophie "[Sophie] ⚠️ Eva 知識庫週更：git push 失敗（遠端可能有更新或網路問題），請手動處理。詳見 logs/auto_update_eva.log"
    fi
  fi

  echo "===== 完成 $(date '+%H:%M:%S') ====="
  echo ""
} >> "$LOG" 2>&1
