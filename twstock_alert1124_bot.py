import os, json, logging, time, threading, requests
from datetime import datetime, timezone, timedelta

logging.basicConfig(level=logging.INFO)

TOKEN          = os.environ.get('TELEGRAM_TOKEN', '')
CHAT_ID        = int(os.environ.get('TELEGRAM_CHAT_ID', '0'))
FINMIND_TOKEN  = os.environ.get('FINMIND_TOKEN', '')   # ← 新增：去 finmindtrade.com 免費註冊後取得
API            = 'https://api.telegram.org/bot' + TOKEN

TRACK_LIST      = []
INTRADAY_STATE  = {}
STOCK_NAMES     = {}

# 偽裝成一般使用者的瀏覽器標頭
HEADERS_WEB = {
    'User-Agent': 'Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/122.0.0.0 Safari/537.36',
    'Accept': 'application/json, text/javascript, */*; q=0.01',
    'Referer': 'https://www.twse.com.tw/',
    'X-Requested-With': 'XMLHttpRequest'
}

# ────────────────────────────────────────────────
# 股票名稱快取
# ────────────────────────────────────────────────
def update_stock_names():
    global STOCK_NAMES
    logging.info("正在更新全台股票名稱清單...")
    try:
        r = requests.get(
            "https://api.finmindtrade.com/api/v4/data?dataset=TaiwanStockInfo",
            headers=HEADERS_WEB, timeout=10
        )
        if r.status_code == 200:
            for item in r.json().get('data', []):
                STOCK_NAMES[str(item.get('stock_id', ''))] = item.get('stock_name', '')
            if STOCK_NAMES:
                return
    except Exception:
        pass

    try:
        r_twse = requests.get(
            'https://openapi.twse.com.tw/v1/exchangeReport/STOCK_DAY_ALL',
            headers=HEADERS_WEB, timeout=10
        )
        if r_twse.status_code == 200:
            for item in r_twse.json():
                STOCK_NAMES[str(item.get('Code', ''))] = item.get('Name', '')

        r_tpex = requests.get(
            'https://www.tpex.org.tw/openapi/v1/t187ap03_L',
            headers=HEADERS_WEB, timeout=10
        )
        if r_tpex.status_code == 200:
            for item in r_tpex.json():
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
# FinMind：個股三大法人買賣超（取代 TWSE RWD BFI82U / T86）
# ────────────────────────────────────────────────
def fetch_finmind_institutional(trade_date_str):
    """
    一次抓取全市場個股三大法人資料。
    回傳: (data_list, None, "OK")  或  (None, None, 錯誤訊息)
    單位：股（需 ÷1000 換算張）
    """
    url = (
        f"https://api.finmindtrade.com/api/v4/data"
        f"?dataset=TaiwanStockInstitutionalInvestorsBuySell"
        f"&start_date={trade_date_str}"
        f"&end_date={trade_date_str}"
        f"&token={FINMIND_TOKEN}"
    )
    try:
        r = requests.get(url, headers=HEADERS_WEB, timeout=30)
        if r.status_code != 200:
            return None, None, f"HTTP {r.status_code}"
        data = r.json().get('data', [])
        if not data:
            return None, None, "無資料（可能假日或 token 無效）"
        return data, None, "OK"
    except Exception as e:
        return None, None, f"連線異常: {str(e)[:40]}"

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
# Yahoo Finance 即時/盤中資料
# ────────────────────────────────────────────────
def get_stock_data(symbol):
    if symbol == '^TWII':
        urls = [f"https://query1.finance.yahoo.com/v8/finance/chart/^TWII?interval=1m&range=1d"]
    else:
        urls = [
            f"https://query1.finance.yahoo.com/v8/finance/chart/{symbol}{suffix}?interval=1m&range=1d"
            for suffix in ['.TW', '.TWO']
        ]
    for url in urls:
        try:
            r = requests.get(url, headers=HEADERS_WEB, timeout=5)
            if r.status_code == 200:
                data = r.json()
                result = data.get('chart', {}).get('result', [])
                if result and result[0].get('timestamp'):
                    return result[0]
        except Exception:
            pass
    return None

# ────────────────────────────────────────────────
# 日期工具
# ────────────────────────────────────────────────
def get_last_trading_date():
    tw_time = datetime.now(timezone(timedelta(hours=8)))
    trade_time = tw_time
    if trade_time.hour < 15:
        trade_time -= timedelta(days=1)
    while trade_time.weekday() > 4:
        trade_time -= timedelta(days=1)
    return trade_time

# ────────────────────────────────────────────────
# 盤後籌碼報告
# ────────────────────────────────────────────────
def generate_post_market_msg():
    msgs = []
    tw_time    = datetime.now(timezone(timedelta(hours=8)))
    trade_time = get_last_trading_date()
    trade_date_str = trade_time.strftime('%Y-%m-%d')

    msgs.append(f"📊 【盤後籌碼總結】 查詢時間: {tw_time.strftime('%m-%d %H:%M')}")
    msgs.append("-------------------------")

    # ── 一次抓取全市場個股三大法人資料 ──
    inst_data, _, err = fetch_finmind_institutional(trade_date_str)

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
        total_net = f_net + i_net + d_net
        # 單位：股 → 張（÷1000）
        msgs.append(f"💰 三大法人買賣超 ({trade_date_str}):")
        msgs.append(f"   合計: {total_net/1000:+,.0f} 張")
        msgs.append(f"   外資: {f_net/1000:+,.0f} 張")
        msgs.append(f"   投信: {i_net/1000:+,.0f} 張")
        msgs.append(f"   自營商: {d_net/1000:+,.0f} 張")
    else:
        msgs.append(f"⚠️ 三大法人金額: 失敗 ({err})")

    msgs.append("-------------------------")

    # 2. 外資台指期貨淨未平倉（加上 token）
    try:
        start_date = (tw_time - timedelta(days=30)).strftime('%Y-%m-%d')
        fm_url = (
            f"https://api.finmindtrade.com/api/v4/data"
            f"?dataset=TaiwanFuturesInstitutionalInvestors"
            f"&data_id=TX"
            f"&start_date={start_date}"
            f"&token={FINMIND_TOKEN}"
        )
        r = requests.get(fm_url, headers=HEADERS_WEB, timeout=15)
        if r.status_code == 200:
            data    = r.json().get('data', [])
            fi_data = [d for d in data if '外資' in d.get('name', '')]
            if len(fi_data) >= 2:
                fi_data.sort(key=lambda x: x['date'])
                recent_date = fi_data[-1].get('date', '')
                today_oi    = fi_data[-1].get('long_short_oi_net_volume', 0)
                yest_oi     = fi_data[-2].get('long_short_oi_net_volume', 0)
                diff        = today_oi - yest_oi
                msgs.append(f"📈 外資台指淨未平倉 ({recent_date}):")
                msgs.append(f"   {today_oi:,} 口 (較前日 {diff:+,} 口)")
            else:
                msgs.append("📈 外資台指淨未平倉: 近30日資料不足")
        else:
            msgs.append(f"⚠️ 期貨未平倉: HTTP {r.status_code}")
    except Exception as e:
        msgs.append(f"⚠️ 期貨未平倉: {str(e)[:40]}")

    msgs.append("-------------------------")

    # 3. 個股買賣超排行
    if inst_data:
        if not STOCK_NAMES:
            update_stock_names()

        # 按個股加總三大法人淨買賣
        stock_net = {}
        for row in inst_data:
            code = row.get('stock_id', '')
            buy  = float(row.get('buy',  0) or 0)
            sell = float(row.get('sell', 0) or 0)
            stock_net[code] = stock_net.get(code, 0) + (buy - sell)

        bulk_prices  = fetch_bulk_closing_prices()
        sorted_stocks = sorted(stock_net.items(), key=lambda x: x[1], reverse=True)

        def format_stock_list(stock_list, is_buy):
            lines = []
            for idx, (code, net_shares) in enumerate(stock_list):
                name       = STOCK_NAMES.get(code, '')
                lots       = abs(int(net_shares)) // 1000
                price      = bulk_prices.get(code, '')
                price_disp = f"({price})" if price else "(無報價)"
                action     = "買" if is_buy else "賣"
                lines.append(f"{idx+1}. {code} {name} {price_disp} | {action} {lots:,} 張")
            return lines

        msgs.append("🔥 【法人買超前 20 名】")
        msgs.extend(format_stock_list(sorted_stocks[:20], True))
        msgs.append("-------------------------")
        msgs.append("🩸 【法人賣超前 20 名】")
        msgs.extend(format_stock_list(list(reversed(sorted_stocks[-20:])), False))
    else:
        msgs.append(f"⚠️ 買賣超排行: 失敗 ({err})")

    return "\n".join(msgs)


def post_market_job(chat_id):
    """有終極安全網，不會已讀不回"""
    try:
        send(chat_id, "⏳ 正在彙整盤後籌碼與個股現價，約需 20～30 秒，請稍候...")
        msg = generate_post_market_msg()
        if len(msg) > 4000:
            send(chat_id, msg[:4000])
            send(chat_id, msg[4000:])
        else:
            send(chat_id, msg)
    except Exception as e:
        logging.error(f"Post market job crashed: {e}")
        send(chat_id, f"❌ 系統發生預期外錯誤。\n錯誤內容: {str(e)[:80]}")

# ────────────────────────────────────────────────
# 盤中監控警報
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
    indicators = data['indicators']['quote'][0]
    opens      = indicators['open']
    highs      = indicators['high']
    closes     = indicators['close']
    volumes    = indicators['volume']

    if symbol not in INTRADAY_STATE:
        INTRADAY_STATE[symbol] = {
            'reported_open':   False,
            'reported_0920':   False,
            'reported_close':  False,
            'last_30m_report': None,
            'day_high':        0,
            'max_vol':         0,
            'open_price':      opens[0] if opens else 0
        }

    state        = INTRADAY_STATE[symbol]
    current_time = tw_time.strftime('%H:%M')
    current_min  = tw_time.minute

    if not timestamps or not closes[-1]:
        return

    current_price = closes[-1]
    current_vol   = volumes[-1] if volumes[-1] is not None else 0
    alerts        = []

    if current_time >= '09:00' and not state['reported_open']:
        alerts.append(f"🎯 【開盤】{disp_sym} 開盤價: {state['open_price']:.2f}")
        state['reported_open'] = True
        state['day_high'] = highs[0] if highs[0] else current_price
        state['max_vol']  = volumes[0] if volumes[0] else 0

    if current_time >= '09:20' and not state['reported_0920']:
        max_v, max_v_price = 0, 0
        for i, ts in enumerate(timestamps):
            dt = datetime.fromtimestamp(ts, timezone(timedelta(hours=8)))
            if '09:01' <= dt.strftime('%H:%M') <= '09:20':
                vol = volumes[i] if volumes[i] else 0
                if vol > max_v:
                    max_v       = vol
                    max_v_price = closes[i]
        if max_v > 0:
            alerts.append(f"📊 【09:20 結算】{disp_sym} 早盤最大量價格: {max_v_price:.2f} (單分鐘量: {max_v})")
        state['reported_0920'] = True

    if current_min in [0, 30] and '09:30' <= current_time <= '13:00':
        if state['last_30m_report'] != current_time:
            alerts.append(f"⏱ 【定時回報】{disp_sym} 目前股價: {current_price:.2f}")
            state['last_30m_report'] = current_time

    if current_price > state['day_high'] and current_time > '09:00':
        alerts.append(f"🔥 【創新高】{disp_sym} 突破本日新高價: {current_price:.2f}")
        state['day_high'] = current_price

    if current_vol > state['max_vol'] and current_time > '09:00':
        alerts.append(f"💥 【爆大量】{disp_sym} 出現單分鐘新天量: {current_vol} 張，股價: {current_price:.2f}")
        state['max_vol'] = current_vol

    if current_time >= '13:30' and not state['reported_close']:
        alerts.append(f"🏁 【收盤】{disp_sym} 收盤價: {current_price:.2f}")
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
            send(chat_id, f"✅ 已更新今日盯盤清單：{', '.join(TRACK_LIST)}")
        else:
            send(chat_id, "請輸入要追蹤的股票代號，例如：/track 2330 2603")

    elif text.startswith('/list'):
        if TRACK_LIST:
            send(chat_id, f"📋 目前追蹤清單：{', '.join(TRACK_LIST)}")
        else:
            send(chat_id, "目前沒有追蹤任何股票。請使用 /track 新增。")

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

        if len(parts) > 1:
            symbols_to_check = parts[1:]
        else:
            if not TRACK_LIST:
                send(chat_id, "目前沒有追蹤清單。請使用 /track 新增，或輸入 /price [代號] 查詢。")
                return
            symbols_to_check = TRACK_LIST

        send(chat_id, "🔍 正在查詢即時股價，請稍候...")
        alerts   = []
        tw_time  = datetime.now(timezone(timedelta(hours=8)))
        time_str = tw_time.strftime('%H:%M')

        # 加權指數
        twii_data = get_stock_data('^TWII')
        if twii_data and twii_data.get('indicators', {}).get('quote', []):
            meta       = twii_data.get('meta', {})
            prev_close = meta.get('chartPreviousClose') or meta.get('previousClose')
            closes     = twii_data['indicators']['quote'][0].get('close', [])
            valid      = [c for c in closes if c is not None]
            if valid and prev_close:
                curr = valid[-1]
                chg  = curr - prev_close
                pct  = (chg / prev_close) * 100
                alerts.append(f"📈 加權指數: {curr:.2f} ({chg:+.2f}, {pct:+.2f}%)")
                alerts.append("-------------------------")

        # 個股
        for sym in symbols_to_check:
            name     = STOCK_NAMES.get(sym, '')
            disp_sym = f"{sym} {name}".strip()
            data     = get_stock_data(sym)
            if data and data.get('indicators', {}).get('quote', []):
                meta       = data.get('meta', {})
                prev_close = meta.get('chartPreviousClose') or meta.get('previousClose')
                closes     = data['indicators']['quote'][0].get('close', [])
                valid      = [c for c in closes if c is not None]
                if valid:
                    curr = valid[-1]
                    if prev_close:
                        chg = curr - prev_close
                        pct = (chg / prev_close) * 100
                        alerts.append(f"📌 {disp_sym}: {curr:.2f} ({chg:+.2f}, {pct:+.2f}%)")
                    else:
                        alerts.append(f"📌 {disp_sym}: {curr:.2f}")
                else:
                    alerts.append(f"⚠️ {disp_sym}: 暫無今日報價資料")
            else:
                alerts.append(f"❌ {disp_sym}: 查詢失敗 (請確認代號正確)")

        if alerts:
            send(chat_id, f"[{time_str} 即時報價]\n" + "\n".join(alerts))

# ────────────────────────────────────────────────
# Long-polling 主迴圈
# ────────────────────────────────────────────────
def polling_loop():
    offset = 0
    logging.info('Telegram Polling started')
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
    logging.info('Market Monitor started')
    while True:
        tw_time  = datetime.now(timezone(timedelta(hours=8)))
        time_str = tw_time.strftime('%H:%M')

        # 每日 08:30 重置狀態 & 更新股票名稱
        if time_str == '08:30' and tw_time.second == 0:
            INTRADAY_STATE.clear()
            update_stock_names()
            time.sleep(1)

        # 每日 16:30 自動送出盤後報告（平日）
        if time_str == '16:30' and tw_time.second == 0:
            if tw_time.weekday() < 5:
                threading.Thread(target=post_market_job, args=(CHAT_ID,), daemon=True).start()
            time.sleep(1)

        # 盤中追蹤
        if TRACK_LIST and '09:00' <= time_str <= '13:35' and tw_time.weekday() < 5:
            for symbol in TRACK_LIST:
                analyze_and_alert(symbol, tw_time)
                time.sleep(1)

        # 等到下一整分鐘
        secs_to_next = 60 - datetime.now(timezone(timedelta(hours=8))).second
        time.sleep(secs_to_next)

# ────────────────────────────────────────────────
# 入口
# ────────────────────────────────────────────────
if __name__ == '__main__':
    logging.info('twstock_alert_bot started!')
    update_stock_names()
    t = threading.Thread(target=market_monitor_loop, daemon=True)
    t.start()
    polling_loop()
