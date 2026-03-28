import os, logging, time, threading, requests
from datetime import datetime, timezone, timedelta

logging.basicConfig(level=logging.INFO)

TOKEN         = os.environ.get('TELEGRAM_TOKEN', '')
CHAT_ID       = int(os.environ.get('TELEGRAM_CHAT_ID', '0'))
FINMIND_TOKEN = os.environ.get('FINMIND_TOKEN', '')   # finmindtrade.com 登入後取得
API           = 'https://api.telegram.org/bot' + TOKEN

TRACK_LIST     = []
INTRADAY_STATE = {}
STOCK_NAMES    = {}

HEADERS_WEB = {
    'User-Agent': 'Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/122.0.0.0 Safari/537.36',
    'Accept': 'application/json, text/javascript, */*; q=0.01',
    'Referer': 'https://www.twse.com.tw/',
    'X-Requested-With': 'XMLHttpRequest'
}

def finmind_headers():
    """FinMind 需要 Bearer token 放在 Authorization header，不是 query string"""
    return {**HEADERS_WEB, 'Authorization': f'Bearer {FINMIND_TOKEN}'}

# ────────────────────────────────────────────────
# 股票名稱快取
# ────────────────────────────────────────────────
def update_stock_names():
    global STOCK_NAMES
    logging.info("正在更新全台股票名稱清單...")
    try:
        r = requests.get(
            'https://api.finmindtrade.com/api/v4/data?dataset=TaiwanStockInfo',
            headers=finmind_headers(), timeout=10
        )
        if r.status_code == 200:
            for item in r.json().get('data', []):
                STOCK_NAMES[str(item.get('stock_id', ''))] = item.get('stock_name', '')
            if STOCK_NAMES:
                return
    except Exception:
        pass

    try:
        r1 = requests.get(
            'https://openapi.twse.com.tw/v1/exchangeReport/STOCK_DAY_ALL',
            headers=HEADERS_WEB, timeout=10
        )
        if r1.status_code == 200:
            for item in r1.json():
                STOCK_NAMES[str(item.get('Code', ''))] = item.get('Name', '')
        r2 = requests.get(
            'https://www.tpex.org.tw/openapi/v1/t187ap03_L',
            headers=HEADERS_WEB, timeout=10
        )
        if r2.status_code == 200:
            for item in r2.json():
                STOCK_NAMES[str(item.get('SecuritiesCompanyCode', ''))] = item.get('CompanyName', '')
    except Exception:
        pass

# ────────────────────────────────────────────────
# 全市場收盤價（TWSE + TPEx OpenAPI，Railway 可連）
# ────────────────────────────────────────────────
def fetch_bulk_closing_prices():
    prices = {}
    try:
        r1 = requests.get(
            'https://openapi.twse.com.tw/v1/exchangeReport/STOCK_DAY_ALL',
            headers=HEADERS_WEB, timeout=10
        )
        if r1.status_code == 200:
            for item in r1.json():
                prices[item.get('Code', '')] = item.get('ClosingPrice', '')
    except Exception:
        pass
    try:
        r2 = requests.get(
            'https://www.tpex.org.tw/openapi/v1/t187ap03_L',
            headers=HEADERS_WEB, timeout=10
        )
        if r2.status_code == 200:
            for item in r2.json():
                prices[item.get('SecuritiesCompanyCode', '')] = item.get('ClosingPrice', '')
    except Exception:
        pass
    return prices

# ────────────────────────────────────────────────
# FinMind：個股三大法人（Bearer header 認證）
# ────────────────────────────────────────────────
def fetch_finmind_institutional(trade_date_str):
    """
    回傳 (data_list, "OK") 或 (None, 錯誤訊息)
    欄位：date, stock_id, name (外資/投信/自營), buy, sell
    """
    try:
        r = requests.get(
            'https://api.finmindtrade.com/api/v4/data',
            params={
                'dataset':    'TaiwanStockInstitutionalInvestorsBuySell',
                'start_date': trade_date_str,
            },
            headers=finmind_headers(),
            timeout=30
        )
        if r.status_code != 200:
            return None, f"HTTP {r.status_code}: {r.text[:80]}"
        data = r.json().get('data', [])
        if not data:
            return None, "無資料（假日 / token 無效 / 尚未更新）"
        # 只取當天
        day_data = [d for d in data if d.get('date', '') == trade_date_str]
        if not day_data:
            return None, f"當日({trade_date_str})無法人資料，可能尚未更新"
        return day_data, "OK"
    except Exception as e:
        return None, f"連線異常: {str(e)[:60]}"

# ────────────────────────────────────────────────
# Telegram 傳訊
# ────────────────────────────────────────────────
def send(chat_id, text):
    try:
        requests.post(
            API + '/sendMessage',
            json={'chat_id': chat_id, 'text': text},
            timeout=10
        )
    except Exception as e:
        logging.error('Telegram send error: ' + str(e))

# ────────────────────────────────────────────────
# Yahoo Finance 即時 / 盤中資料
# ────────────────────────────────────────────────
def get_stock_data(symbol):
    if symbol == '^TWII':
        urls = ['https://query1.finance.yahoo.com/v8/finance/chart/^TWII?interval=1m&range=1d']
    else:
        urls = [
            f'https://query1.finance.yahoo.com/v8/finance/chart/{symbol}{suffix}?interval=1m&range=1d'
            for suffix in ['.TW', '.TWO']
        ]
    for url in urls:
        try:
            r = requests.get(url, headers=HEADERS_WEB, timeout=5)
            if r.status_code == 200:
                result = r.json().get('chart', {}).get('result', [])
                if result and result[0].get('timestamp'):
                    return result[0]
        except Exception:
            pass
    return None

# ────────────────────────────────────────────────
# 日期工具
# ────────────────────────────────────────────────
def get_last_trading_date():
    d = datetime.now(timezone(timedelta(hours=8)))
    if d.hour < 15:
        d -= timedelta(days=1)
    while d.weekday() > 4:
        d -= timedelta(days=1)
    return d

# ────────────────────────────────────────────────
# 盤後籌碼報告
# ────────────────────────────────────────────────
def generate_post_market_msg():
    msgs           = []
    tw_time        = datetime.now(timezone(timedelta(hours=8)))
    trade_date     = get_last_trading_date()
    trade_date_str = trade_date.strftime('%Y-%m-%d')

    msgs.append(f"📊 【盤後籌碼總結】 查詢時間: {tw_time.strftime('%m-%d %H:%M')}")
    msgs.append("-------------------------")

    inst_data, err = fetch_finmind_institutional(trade_date_str)

    # 1. 三大法人金額加總
    if inst_data:
        f_net = i_net = d_net = 0.0
        for row in inst_data:
            name = row.get('name', '')
            buy  = float(row.get('buy',  0) or 0)
            sell = float(row.get('sell', 0) or 0)
            net  = buy - sell
            if '外資' in name:
                f_net += net
            elif '投信' in name:
                i_net += net
            elif '自營' in name:
                d_net += net
        total = f_net + i_net + d_net
        msgs.append(f"💰 三大法人買賣超 ({trade_date_str}):")
        msgs.append(f"   合計: {total/1000:+,.0f} 張")
        msgs.append(f"   外資: {f_net/1000:+,.0f} 張")
        msgs.append(f"   投信: {i_net/1000:+,.0f} 張")
        msgs.append(f"   自營商: {d_net/1000:+,.0f} 張")
    else:
        msgs.append(f"⚠️ 三大法人金額: 失敗 ({err})")

    msgs.append("-------------------------")

    # 2. 外資台指期貨淨未平倉
    try:
        start_date = (tw_time - timedelta(days=30)).strftime('%Y-%m-%d')
        r = requests.get(
            'https://api.finmindtrade.com/api/v4/data',
            params={'dataset': 'TaiwanFuturesInstitutionalInvestors', 'data_id': 'TX', 'start_date': start_date},
            headers=finmind_headers(),
            timeout=15
        )
        if r.status_code == 200:
            fi_data = [d for d in r.json().get('data', []) if '外資' in d.get('name', '')]
            if len(fi_data) >= 2:
                fi_data.sort(key=lambda x: x['date'])
                today_oi = fi_data[-1].get('long_short_oi_net_volume', 0)
                yest_oi  = fi_data[-2].get('long_short_oi_net_volume', 0)
                diff     = today_oi - yest_oi
                msgs.append(f"📈 外資台指淨未平倉 ({fi_data[-1]['date']}):")
                msgs.append(f"   {today_oi:,} 口 (較前日 {diff:+,} 口)")
            else:
                msgs.append("📈 外資台指淨未平倉: 近30日資料不足")
        else:
            msgs.append(f"⚠️ 期貨未平倉: HTTP {r.status_code}")
    except Exception as e:
        msgs.append(f"⚠️ 期貨未平倉: {str(e)[:60]}")

    msgs.append("-------------------------")

    # 3. 個股買賣超排行
    if inst_data:
        if not STOCK_NAMES:
            update_stock_names()

        stock_net = {}
        for row in inst_data:
            code = row.get('stock_id', '')
            buy  = float(row.get('buy',  0) or 0)
            sell = float(row.get('sell', 0) or 0)
            stock_net[code] = stock_net.get(code, 0) + (buy - sell)

        bulk_prices   = fetch_bulk_closing_prices()
        sorted_stocks = sorted(stock_net.items(), key=lambda x: x[1], reverse=True)

        def fmt(stocks, is_buy):
            lines = []
            for i, (code, net) in enumerate(stocks):
                name  = STOCK_NAMES.get(code, '')
                lots  = abs(int(net)) // 1000
                price = bulk_prices.get(code, '')
                pd    = f'({price})' if price else '(無報價)'
                act   = '買' if is_buy else '賣'
                lines.append(f"{i+1}. {code} {name} {pd} | {act} {lots:,} 張")
            return lines

        msgs.append("🔥 【法人買超前 20 名】")
        msgs.extend(fmt(sorted_stocks[:20], True))
        msgs.append("-------------------------")
        msgs.append("🩸 【法人賣超前 20 名】")
        msgs.extend(fmt(list(reversed(sorted_stocks[-20:])), False))
    else:
        msgs.append(f"⚠️ 買賣超排行: 失敗 ({err})")

    return "\n".join(msgs)


def post_market_job(chat_id):
    try:
        send(chat_id, "⏳ 正在彙整盤後籌碼，約需 20～30 秒，請稍候...")
        msg = generate_post_market_msg()
        if len(msg) > 4000:
            send(chat_id, msg[:4000])
            send(chat_id, msg[4000:])
        else:
            send(chat_id, msg)
    except Exception as e:
        logging.error(f"post_market_job crashed: {e}")
        send(chat_id, f"❌ 系統錯誤: {str(e)[:80]}")

# ────────────────────────────────────────────────
# 盤中監控
# ────────────────────────────────────────────────
def analyze_and_alert(symbol, tw_time):
    data = get_stock_data(symbol)
    if not data:
        return

    if not STOCK_NAMES:
        update_stock_names()
    name     = STOCK_NAMES.get(symbol, '')
    disp_sym = f"{symbol} {name}".strip()

    timestamps = data['timestamp']
    q          = data['indicators']['quote'][0]
    opens, highs, closes, volumes = q['open'], q['high'], q['close'], q['volume']

    if symbol not in INTRADAY_STATE:
        INTRADAY_STATE[symbol] = {
            'reported_open': False, 'reported_0920': False, 'reported_close': False,
            'last_30m_report': None, 'day_high': 0, 'max_vol': 0,
            'open_price': opens[0] if opens else 0,
        }

    state        = INTRADAY_STATE[symbol]
    current_time = tw_time.strftime('%H:%M')
    current_min  = tw_time.minute

    if not timestamps or not closes[-1]:
        return

    price   = closes[-1]
    vol     = volumes[-1] if volumes[-1] is not None else 0
    alerts  = []

    if current_time >= '09:00' and not state['reported_open']:
        alerts.append(f"🎯 【開盤】{disp_sym} 開盤: {state['open_price']:.2f}")
        state['reported_open'] = True
        state['day_high']      = highs[0] if highs[0] else price
        state['max_vol']       = volumes[0] if volumes[0] else 0

    if current_time >= '09:20' and not state['reported_0920']:
        max_v, max_p = 0, 0
        for i, ts in enumerate(timestamps):
            dt = datetime.fromtimestamp(ts, timezone(timedelta(hours=8)))
            if '09:01' <= dt.strftime('%H:%M') <= '09:20':
                v = volumes[i] if volumes[i] else 0
                if v > max_v:
                    max_v, max_p = v, closes[i]
        if max_v > 0:
            alerts.append(f"📊 【09:20結算】{disp_sym} 早盤最大量: {max_p:.2f} (量: {max_v})")
        state['reported_0920'] = True

    if current_min in [0, 30] and '09:30' <= current_time <= '13:00':
        if state['last_30m_report'] != current_time:
            alerts.append(f"⏱ 【定時】{disp_sym} 現價: {price:.2f}")
            state['last_30m_report'] = current_time

    if price > state['day_high'] and current_time > '09:00':
        alerts.append(f"🔥 【創新高】{disp_sym} 新高: {price:.2f}")
        state['day_high'] = price

    if vol > state['max_vol'] and current_time > '09:00':
        alerts.append(f"💥 【爆量】{disp_sym} 單分鐘天量: {vol} 張，現價: {price:.2f}")
        state['max_vol'] = vol

    if current_time >= '13:30' and not state['reported_close']:
        alerts.append(f"🏁 【收盤】{disp_sym} 收盤: {price:.2f}")
        state['reported_close'] = True

    if alerts:
        send(CHAT_ID, f"[{current_time}]\n" + "\n".join(alerts))

# ────────────────────────────────────────────────
# Telegram 指令處理
# ────────────────────────────────────────────────
def handle(update):
    msg     = update.get('message', {})
    chat_id = msg.get('chat', {}).get('id')
    text    = msg.get('text', '')
    if chat_id != CHAT_ID:
        return

    if text.startswith('/track'):
        parts = text.strip().split()
        if len(parts) > 1:
            global TRACK_LIST
            TRACK_LIST = parts[1:21]
            INTRADAY_STATE.clear()
            send(chat_id, f"✅ 已更新盯盤清單：{', '.join(TRACK_LIST)}")
        else:
            send(chat_id, "請輸入股票代號，例如：/track 2330 2603")

    elif text.startswith('/list'):
        if TRACK_LIST:
            send(chat_id, f"📋 追蹤清單：{', '.join(TRACK_LIST)}")
        else:
            send(chat_id, "目前無追蹤股票，請用 /track 新增。")

    elif text.startswith('/clear'):
        TRACK_LIST.clear()
        INTRADAY_STATE.clear()
        send(chat_id, "🗑 追蹤清單已清空。")

    elif text.startswith('/postmarket'):
        threading.Thread(target=post_market_job, args=(chat_id,), daemon=True).start()

    elif text.startswith('/price'):
        if not STOCK_NAMES:
            update_stock_names()
        parts = text.strip().split()

        symbols = parts[1:] if len(parts) > 1 else TRACK_LIST
        if not symbols:
            send(chat_id, "目前無追蹤清單，請用 /track 新增，或輸入 /price [代號]。")
            return

        send(chat_id, "🔍 正在查詢即時股價...")
        lines    = []
        tw_time  = datetime.now(timezone(timedelta(hours=8)))
        time_str = tw_time.strftime('%H:%M')

        twii = get_stock_data('^TWII')
        if twii and twii.get('indicators', {}).get('quote', []):
            meta  = twii.get('meta', {})
            prev  = meta.get('chartPreviousClose') or meta.get('previousClose')
            valid = [c for c in twii['indicators']['quote'][0].get('close', []) if c is not None]
            if valid and prev:
                curr = valid[-1]
                chg  = curr - prev
                pct  = chg / prev * 100
                lines.append(f"📈 加權指數: {curr:.2f} ({chg:+.2f}, {pct:+.2f}%)")
                lines.append("-------------------------")

        for sym in symbols:
            name     = STOCK_NAMES.get(sym, '')
            disp     = f"{sym} {name}".strip()
            data     = get_stock_data(sym)
            if data and data.get('indicators', {}).get('quote', []):
                meta  = data.get('meta', {})
                prev  = meta.get('chartPreviousClose') or meta.get('previousClose')
                valid = [c for c in data['indicators']['quote'][0].get('close', []) if c is not None]
                if valid:
                    curr = valid[-1]
                    if prev:
                        chg = curr - prev
                        pct = chg / prev * 100
                        lines.append(f"📌 {disp}: {curr:.2f} ({chg:+.2f}, {pct:+.2f}%)")
                    else:
                        lines.append(f"📌 {disp}: {curr:.2f}")
                else:
                    lines.append(f"⚠️ {disp}: 暫無今日報價")
            else:
                lines.append(f"❌ {disp}: 查詢失敗（請確認代號）")

        if lines:
            send(chat_id, f"[{time_str} 即時報價]\n" + "\n".join(lines))

# ────────────────────────────────────────────────
# Long-polling
# ────────────────────────────────────────────────
def polling_loop():
    offset = 0
    logging.info('Telegram polling started')
    while True:
        try:
            r       = requests.get(API + '/getUpdates', params={'offset': offset, 'timeout': 30}, timeout=35)
            updates = r.json().get('result', [])
            for u in updates:
                offset = u['update_id'] + 1
                handle(u)
        except Exception as e:
            logging.error('Polling error: ' + str(e))
            time.sleep(5)

# ────────────────────────────────────────────────
# 盤中監控排程
# ────────────────────────────────────────────────
def market_monitor_loop():
    logging.info('Market monitor started')
    while True:
        tw_time  = datetime.now(timezone(timedelta(hours=8)))
        time_str = tw_time.strftime('%H:%M')

        if time_str == '08:30' and tw_time.second == 0:
            INTRADAY_STATE.clear()
            update_stock_names()
            time.sleep(1)

        if time_str == '16:30' and tw_time.second == 0 and tw_time.weekday() < 5:
            threading.Thread(target=post_market_job, args=(CHAT_ID,), daemon=True).start()
            time.sleep(1)

        if TRACK_LIST and '09:00' <= time_str <= '13:35' and tw_time.weekday() < 5:
            for symbol in TRACK_LIST:
                analyze_and_alert(symbol, tw_time)
                time.sleep(1)

        secs = 60 - datetime.now(timezone(timedelta(hours=8))).second
        time.sleep(secs)

# ────────────────────────────────────────────────
# 入口
# ────────────────────────────────────────────────
if __name__ == '__main__':
    logging.info('twstock_bot started!')
    update_stock_names()
    threading.Thread(target=market_monitor_loop, daemon=True).start()
    polling_loop()
