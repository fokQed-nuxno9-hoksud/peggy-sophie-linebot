#!/usr/bin/env python3
"""
產生 Gmail「唯讀」授權 refresh token 的小工具。
用途：讓 Helen 能讀取 peggywang9527 / peggyforevent 兩個信箱（只讀，不改不刪）。

用法：
    cd /Users/user/Downloads/Peggy_agent/line_bot
    python get_gmail_token.py

執行後會自動開瀏覽器，請用「要授權的那個 Gmail 帳號」登入並按同意。
完成後終端機會印出 refresh token，把它貼進 .env：
    - 登入 peggywang9527 → 設成 GMAIL_WANG_REFRESH_TOKEN
    - 登入 peggyforevent → 設成 GMAIL_EVENT_REFRESH_TOKEN

要授權兩個帳號就跑兩次（每次登入不同帳號）。

注意：scope 只要 gmail.readonly（唯讀），不會有任何修改、刪除信件的權限。
"""
import os
import sys

try:
    from google_auth_oauthlib.flow import InstalledAppFlow
except ImportError:
    print("缺少套件，請先安裝：")
    print("  pip install google-auth-oauthlib")
    sys.exit(1)

SCOPES = ["https://www.googleapis.com/auth/gmail.readonly"]


def load_env(path):
    env = {}
    with open(path) as f:
        for line in f:
            line = line.strip()
            if "=" in line and not line.startswith("#"):
                k, _, v = line.partition("=")
                env[k.strip()] = v.strip().strip('"').strip("'")
    return env


def main():
    env_path = os.path.join(os.path.dirname(os.path.abspath(__file__)), "..", ".env")
    env_path = os.path.abspath(env_path)
    env = load_env(env_path)

    client_id = env.get("GMAIL_CLIENT_ID")
    client_secret = env.get("GMAIL_CLIENT_SECRET")
    if not client_id or not client_secret:
        print("ERROR: .env 缺 GMAIL_CLIENT_ID / GMAIL_CLIENT_SECRET")
        sys.exit(1)

    client_config = {
        "installed": {
            "client_id": client_id,
            "client_secret": client_secret,
            "auth_uri": "https://accounts.google.com/o/oauth2/auth",
            "token_uri": "https://oauth2.googleapis.com/token",
            "redirect_uris": ["http://localhost"],
        }
    }

    flow = InstalledAppFlow.from_client_config(client_config, SCOPES)
    creds = flow.run_local_server(port=0, prompt="consent")

    print("\n" + "=" * 60)
    print("授權成功！這個帳號是：", creds.id_token if hasattr(creds, "id_token") else "(看你剛剛登入的帳號)")
    print("=" * 60)
    print("\n你的 refresh token（請複製整段，貼進 .env）：\n")
    print(creds.refresh_token)
    print("\n提醒：")
    print("  登入 peggywang9527 → 在 .env 加一行 GMAIL_WANG_REFRESH_TOKEN=上面那串")
    print("  登入 peggyforevent → 在 .env 加一行 GMAIL_EVENT_REFRESH_TOKEN=上面那串")


if __name__ == "__main__":
    main()
