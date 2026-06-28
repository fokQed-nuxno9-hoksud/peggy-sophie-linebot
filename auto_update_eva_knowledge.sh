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

{
  echo "===== $(date '+%Y-%m-%d %H:%M:%S') 開始自動更新 Eva 知識庫 ====="

  cd "$REPO" || { echo "❌ 無法進入 $REPO"; echo "===== 中止 ====="; exit 1; }

  # 1. 重建知識庫（讀牌價表 + 公司資訊 + 自動抓商城分類）
  "$PYTHON" build_eva_knowledge.py
  if [ $? -ne 0 ]; then
    echo "❌ build_eva_knowledge.py 執行失敗，不 push"
    echo "===== 中止 $(date '+%H:%M:%S') ====="
    exit 1
  fi

  # 2. 有變動才 commit + push
  if git diff --quiet eva_knowledge.txt; then
    echo "ℹ️ 知識庫無變動，略過 commit / push"
  else
    git add eva_knowledge.txt
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
    fi
  fi

  echo "===== 完成 $(date '+%H:%M:%S') ====="
  echo ""
} >> "$LOG" 2>&1
