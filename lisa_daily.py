#!/usr/bin/env python3
"""
Lisa 財經日報 — Render Cron Job 執行腳本
排程：UTC 23:00 = 台北時間 07:00
"""
import base64
import json
import os
import sys
import time
import urllib.parse
import urllib.request
from datetime import datetime, timezone, timedelta
from email.mime.text import MIMEText

import anthropic
import requests

TAIPEI = timezone(timedelta(hours=8))

TWSE_STOCKS = ['2330', '2454', '3443', '3189', '3711', '2457', '0050']
TPEX_STOCKS = ['5347', '3551', '6187', '8284']
US_INDICES  = ['^DJI', '^GSPC', '^IXIC']
US_ETF      = ['VOO', 'QQQ', 'QQQM', 'SMH', 'VT', 'NASA']
US_STOCKS   = ['GLW', 'ASML', 'MSFT', 'NVDA', 'TSLA', 'RKLB', 'MRVL', 'V',
               'GOOGL', 'AMD', 'MU', 'INTC', 'TXN', 'AAPL', 'QCOM', 'KLAC',
               'AMAT', 'SPCX', 'QNT', 'ORCL']
INDEX_NAMES = {'^DJI': 'Dow Jones', '^GSPC': 'S&P 500', '^IXIC': 'Nasdaq'}


# ── 數據抓取 ─────────────────────────────────────────────────────────────────

def fetch_json(url, headers=None):
    resp = requests.get(url, headers=headers or {'User-Agent': 'Mozilla/5.0'},
                        timeout=20, verify=False)
    resp.raise_for_status()
    return resp.json()


def get_twse_index():
    d = fetch_json('https://openapi.twse.com.tw/v1/exchangeReport/FMTQIK')
    x = d[-1]
    y = int(x['Date'][:3]) + 1911
    val = float(x['TAIEX'].replace(',', ''))
    chg = float(x['Change'].replace(',', ''))
    return {
        'name': '台股加權指數',
        'value': val,
        'change': chg,
        'change_pct': round(chg / (val - chg) * 100, 2) if (val - chg) else 0,
        'date': f"{y}/{x['Date'][3:5]}/{x['Date'][5:7]}",
    }


def get_twse_stocks():
    d = fetch_json('https://openapi.twse.com.tw/v1/exchangeReport/STOCK_DAY_AVG_ALL')
    result = {}
    target = set(TWSE_STOCKS)
    for item in d:
        code = item.get('Code', '')
        if code in target:
            rd = item.get('Date', '')
            try:
                ds = f"{int(rd[:3]) + 1911}/{rd[3:5]}/{rd[5:7]}"
            except Exception:
                ds = rd
            result[code] = {
                'code': code,
                'name': item.get('Name', code),
                'close': float((item.get('ClosingPrice', '0') or '0').replace(',', '')),
                'date': ds,
                'market': '上市',
            }
    return result


def get_tpex_stocks():
    d = fetch_json('https://www.tpex.org.tw/openapi/v1/tpex_mainboard_quotes')
    result = {}
    target = set(TPEX_STOCKS)
    for item in d:
        code = item.get('SecuritiesCompanyCode', '')
        if code in target:
            rd = item.get('Date', '')
            try:
                ds = f"{int(rd[:3]) + 1911}/{rd[3:5]}/{rd[5:7]}"
            except Exception:
                ds = rd
            close = float((item.get('Close', '0') or '0').replace(',', ''))
            chg = float((item.get('Change', '0') or '0').replace(',', '').replace('+', ''))
            result[code] = {
                'code': code,
                'name': item.get('CompanyName', code),
                'close': close,
                'change': chg,
                'change_pct': round(chg / (close - chg) * 100, 2) if (close - chg) else 0,
                'date': ds,
                'market': '上櫃',
            }
    return result


def finnhub_quote(symbol, token):
    url = f'https://finnhub.io/api/v1/quote?symbol={symbol}&token={token}'
    try:
        d = fetch_json(url)
        if not d.get('c'):
            return {'symbol': symbol, 'error': 'no data'}
        return {
            'symbol': symbol,
            'price': round(d['c'], 2),
            'prev_close': round(d['pc'], 2),
            'change': round(d['d'], 2),
            'change_pct': round(d['dp'], 2),
            'high': round(d['h'], 2),
            'low': round(d['l'], 2),
        }
    except Exception as e:
        return {'symbol': symbol, 'error': str(e)}


def yf_index(symbol):
    url = f'https://query1.finance.yahoo.com/v8/finance/chart/{symbol}?interval=1d&range=5d'
    try:
        d = fetch_json(url)
        r = d['chart']['result'][0]
        meta = r['meta']
        closes = [c for c in r['indicators']['quote'][0]['close'] if c is not None]
        curr = meta['regularMarketPrice']
        prev = closes[-2] if len(closes) >= 2 else curr
        chg = curr - prev
        chg_pct = chg / prev * 100 if prev else 0
        return {
            'symbol': symbol,
            'name': INDEX_NAMES.get(symbol, symbol),
            'price': round(curr, 2),
            'change': round(chg, 2),
            'change_pct': round(chg_pct, 2),
        }
    except Exception as e:
        return {'symbol': symbol, 'name': INDEX_NAMES.get(symbol, symbol), 'error': str(e)}


def get_finnhub_news(token):
    url = f'https://finnhub.io/api/v1/news?category=general&minId=0&token={token}'
    try:
        items = fetch_json(url)
        return [
            {
                'headline': i.get('headline', ''),
                'source': i.get('source', ''),
                'summary': (i.get('summary', '') or '')[:300],
            }
            for i in items[:10]
        ]
    except Exception as e:
        return [{'error': str(e)}]


def fetch_all_data():
    token = os.environ.get('FINNHUB_TOKEN', '')
    now_tp = datetime.now(TAIPEI)
    data = {
        'generated_at': now_tp.strftime('%Y/%m/%d %H:%M 台北時間'),
        'tw_index': None,
        'tw_stocks': {},
        'us_indices': {},
        'us_etf': {},
        'us_stocks': {},
        'news': [],
    }

    try:
        data['tw_index'] = get_twse_index()
        print('  ✓ 台股加權指數')
    except Exception as e:
        data['tw_index'] = {'error': str(e)}
        print(f'  ✗ 台股加權指數：{e}', file=sys.stderr)

    try:
        data['tw_stocks'].update(get_twse_stocks())
        print('  ✓ 台股上市個股')
    except Exception as e:
        data['tw_stocks']['_err_twse'] = str(e)
        print(f'  ✗ 台股上市：{e}', file=sys.stderr)

    try:
        data['tw_stocks'].update(get_tpex_stocks())
        print('  ✓ 台股上櫃個股')
    except Exception as e:
        data['tw_stocks']['_err_tpex'] = str(e)
        print(f'  ✗ 台股上櫃：{e}', file=sys.stderr)

    for sym in US_INDICES:
        data['us_indices'][sym] = yf_index(sym)
        time.sleep(0.1)
    print(f'  ✓ 美股大盤指數 ({len(US_INDICES)} 筆)')

    for sym in US_ETF:
        data['us_etf'][sym] = finnhub_quote(sym, token)
        time.sleep(0.1)
    print(f'  ✓ 美股 ETF ({len(US_ETF)} 筆)')

    for sym in US_STOCKS:
        data['us_stocks'][sym] = finnhub_quote(sym, token)
        time.sleep(0.1)
    print(f'  ✓ 美股個股 ({len(US_STOCKS)} 筆)')

    data['news'] = get_finnhub_news(token)
    print(f'  ✓ 財經新聞 ({len(data["news"])} 條)')

    return data


# ── Claude 生成報告 ──────────────────────────────────────────────────────────

LISA_SYSTEM = """你是 Lisa，Peggy 的財經助理。根據提供的市場數據，生成以下格式的日報。

格式（嚴格遵守，純文字，不使用 Markdown）：

════════════════════════════════════════════════════
  LISA MARKET DAILY  |  [日期]
════════════════════════════════════════════════════
數據截至：[generated_at]
數據來源：TWSE OpenAPI（官方）/ TPEX OpenAPI（官方）/
         Finnhub API（官方授權）/ Yahoo Finance（交易所）

一、大盤表現

  [美股三大指數表格，含收盤價、漲跌點、漲跌幅]
  [台股加權指數，含收盤價、漲跌點、漲跌幅]

二、觀察清單

  美股 ETF
  [每筆格式：SYMBOL  收盤價  漲跌幅  (H:xx.xx / L:xx.xx)]

  美股個股（▲▼ 標示漲跌幅 ±3%）
  [每筆格式：SYMBOL  收盤價  漲跌幅]

  台股 ETF
  [每筆格式：代號 名稱  收盤價]

  台股個股（上市）
  [每筆格式：代號 名稱  收盤價]

  台股個股（上櫃）
  [每筆格式：代號 名稱  收盤價  漲跌幅]

三、重點異動說明

  [漲跌幅 ≥ ±3% 的個股，附原因說明（根據新聞推斷）]
  [若無重大異動，寫「今日無 ±3% 以上異動」]

四、市場脈絡

  [根據提供的新聞整理 3-4 個市場主軸]
  ・[論點 1，1-2 句]。（來源：媒體名稱）
  ・[論點 2，1-2 句]。（來源：媒體名稱）
  ・[論點 3，1-2 句]。（來源：媒體名稱）

五、今日財經新聞

  [媒體名稱] 標題文字
  [列出 4-6 條，來自提供的新聞]

六、Lisa 解讀

  📌 短線（本週）：[本週盤勢方向與操作重點，2-3 句]
  📌 中線（1-3 個月）：[中期趨勢研判，2-3 句]
  📌 長線（6 個月以上）：[長期結構性趨勢研判，2-3 句]
  ⚠️ 本週注意：[需特別留意的風險或事件，1-2 句]

────────────────────────────────────────────────────
數據來源聲明：市場數據來自官方 API，新聞摘要來自 Finnhub。
本報告僅供參考，不構成投資建議。
════════════════════════════════════════════════════

規則：
- 繁體中文
- 數字直接用提供的數據，不捏造
- 若 symbol 有 error 欄位，標記「數據取得失敗」
- 台股收盤時間為台北 13:30，美股為台北隔日 05:00，日期標示注意前後關係"""


def generate_report(data):
    client = anthropic.Anthropic(api_key=os.environ['ANTHROPIC_API_KEY'])
    today = datetime.now(TAIPEI).strftime('%Y-%m-%d')

    user_content = f"""今天是 {today}，請根據以下市場數據生成 Lisa 財經日報。

{json.dumps(data, ensure_ascii=False, indent=2)}
"""
    msg = client.messages.create(
        model='claude-haiku-4-5-20251001',
        max_tokens=4096,
        system=LISA_SYSTEM,
        messages=[{'role': 'user', 'content': user_content}],
    )
    return msg.content[0].text


# ── Email 寄送 ───────────────────────────────────────────────────────────────

TOKEN_URL = 'https://oauth2.googleapis.com/token'
SEND_URL = 'https://gmail.googleapis.com/gmail/v1/users/me/messages/send'


def get_access_token():
    data = urllib.parse.urlencode({
        'client_id': os.environ['GMAIL_CLIENT_ID'],
        'client_secret': os.environ['GMAIL_CLIENT_SECRET'],
        'refresh_token': os.environ['GMAIL_REFRESH_TOKEN'],
        'grant_type': 'refresh_token',
    }).encode('utf-8')
    resp = urllib.request.urlopen(
        urllib.request.Request(TOKEN_URL, data=data, method='POST')
    )
    return json.loads(resp.read())['access_token']


def send_email(subject, body, to_addrs):
    sender = os.environ['GMAIL_ADDRESS']
    access_token = get_access_token()
    msg = MIMEText(body, 'plain', 'utf-8')
    msg['Subject'] = subject
    msg['From'] = sender
    msg['To'] = ', '.join(to_addrs)
    raw = base64.urlsafe_b64encode(msg.as_bytes()).decode('utf-8')
    payload = json.dumps({'raw': raw}).encode('utf-8')
    req = urllib.request.Request(
        SEND_URL, data=payload, method='POST',
        headers={
            'Authorization': f'Bearer {access_token}',
            'Content-Type': 'application/json',
        },
    )
    urllib.request.urlopen(req)


# ── LINE 推播 ────────────────────────────────────────────────────────────────

def make_line_summary(data):
    today = datetime.now(TAIPEI).strftime('%m/%d')
    lines = [f'📊 Lisa 財經摘要 {today}', '']

    lines.append('🇺🇸 美股大盤')
    for sym in ['^DJI', '^GSPC', '^IXIC']:
        idx = data['us_indices'].get(sym, {})
        if 'error' in idx:
            continue
        name = idx.get('name', sym)
        price = idx.get('price', 0)
        chg_pct = idx.get('change_pct', 0)
        arrow = '▲' if chg_pct >= 0 else '▼'
        lines.append(f'・{name}：{price:,.2f} {arrow}{abs(chg_pct):.2f}%')

    lines.append('')
    tw = data.get('tw_index') or {}
    if 'error' not in tw and tw.get('value'):
        val = tw['value']
        chg_pct = tw.get('change_pct', 0)
        arrow = '▲' if chg_pct >= 0 else '▼'
        lines.append(f'🇹🇼 台股加權：{val:,.2f} {arrow}{abs(chg_pct):.2f}%')
        lines.append('')

    lines.append('詳細日報請見 Email ✉️')
    return '\n'.join(lines)


def send_line(message):
    token = os.environ.get('LINE_CHANNEL_ACCESS_TOKEN', '')
    user_id = os.environ.get('LINE_USER_ID', '')
    if not token or not user_id:
        print('[WARN] LINE env vars not set, skipping', file=sys.stderr)
        return
    resp = requests.post(
        'https://api.line.me/v2/bot/message/push',
        headers={'Authorization': f'Bearer {token}', 'Content-Type': 'application/json'},
        json={'to': user_id, 'messages': [{'type': 'text', 'text': message[:5000]}]},
    )
    if resp.status_code != 200:
        print(f'[WARN] LINE push failed: {resp.status_code} {resp.text[:100]}', file=sys.stderr)


# ── 主程式 ───────────────────────────────────────────────────────────────────

def main():
    now_str = datetime.now(TAIPEI).strftime('%Y/%m/%d %H:%M')
    print(f'[{now_str}] Lisa daily report starting...')

    print('Fetching market data...')
    data = fetch_all_data()

    print('Generating report with Claude...')
    report = generate_report(data)

    today = datetime.now(TAIPEI).strftime('%Y-%m-%d')
    subject = f'【Lisa 財經日報】{today}'

    print('Sending email...')
    to_addrs = [a.strip() for a in os.environ.get('GMAIL_TO', '').split(',') if a.strip()]
    if to_addrs:
        send_email(subject, report, to_addrs)
        print(f'  ✓ Email sent to {to_addrs}')
    else:
        print('[WARN] GMAIL_TO not set, skipping email', file=sys.stderr)

    print('Pushing to LINE...')
    line_msg = make_line_summary(data)
    send_line(f"[Lisa]\n{line_msg}")
    print('  ✓ LINE pushed')

    print(f'[{now_str}] Done.')


if __name__ == '__main__':
    main()
