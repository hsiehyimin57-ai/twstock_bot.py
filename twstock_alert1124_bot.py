import os, logging, time, threading, requests
from datetime import datetime, timezone, timedelta

logging.basicConfig(level=logging.INFO)

TOKEN     = os.environ.get('TELEGRAM_TOKEN', '')
CHAT_ID   = int(os.environ.get('TELEGRAM_CHAT_ID', '0'))
PROXY_URL = os.environ.get('PROXY_URL', '')   # Cloudflare Worker URL
API       = 'https://api.telegram.org/bot' + TOKEN

TRACK_LIST     = []
INTRADAY_STATE = {}
STOCK_NAMES    = {}

HEADERS_WEB = {
    'User-Agent': 'Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/122.0.0.0 Safari/537.36',
    'Accept': 'application/json, text/javascript, */*; q=0.01',
    'Referer': 'https://www.twse.com.tw/',
    'X-Requested-With': 'XMLHttpRequest'
}

# ────────────────────────────────────────────────
# Proxy 請求（走 Cloudflare Worker 繞過 IP 封鎖）
# ────────────────────────────────────────────────
def proxy_get(target_url, timeout=15):
    if PROXY_URL:
        return requests.get(PROXY_URL, params={'url': target_url}, timeout=timeout)
    return requests.get(target_url, headers=HEADERS_WEB, timeout=timeout)

# ────────────────────────────────────────────────
# 股票名稱快取
# ────────────────────────────────────────────────
def update_stock_names():
    global STOCK_NAMES
    logging.info("正在更新全台股票名稱清單...")
    try:
        r = requests.get(
            'https://openapi.twse.com.tw/v1/exchangeReport/STOCK_DAY_ALL',
            headers=HEADERS_WEB, timeout=10
        )
        if r.status_code == 200:
            for item in r.json():
                STOCK_NAMES[str(item.get('Code', ''))] = item.get('Name', '')
    except Exception:
        pass
    try:
        r = requests.get(
            'https://www.tpex.org.tw/openapi/v1/t187ap03_L',
            headers=HEADERS_WEB, timeout=10
        )
        if r.status_code == 200:
            for item in r.json():
                STOCK_NAMES[str(item.get('SecuritiesCompanyCode', ''))] = item.get('CompanyName', '')
    except Exception:
        pass

# ────────────────────────────────────────────────
# 全市場收盤價（OpenAPI，Railway 可直連）
# ────────────────────────────────────────────────
def fetch_bulk_closing_prices():
    prices = {}
    try:
        r = requests.get(
            'https://openapi.twse.com.tw/v1/exchangeReport/STOCK_DAY_ALL',
            headers=HEADERS_WEB, timeout=10
        )
        if r.status_code == 200:
            for item in r.json():
                prices[item.get('Code', '')] = item.get('ClosingPrice', '')
    except Exception:
        pass
    try:
        r = requests.get(
            'https://www.tpex.org.tw/openapi/v1/t187ap03_L',
            headers=HEADERS_WEB, timeout=10
        )
        if r.status_code == 200:
            for item in r.json():
                prices[item.get('SecuritiesCompanyCode', '')] = item.get('ClosingPrice', '')
    except Exception:
        pass
    return prices

# ────────────────────────────────────────────────
# TWSE MIS 即時報價（透過 proxy）
# 支援上市(tse)與上櫃(otc)，一次最多 50 檔
# ────────────────────────────────────────────────
def get_realtime_prices(symbols):
    """
    回傳 dict: { '2330': {'price': 870.0, 'prev': 875.0, 'open': 868.0, 'name': '台積電'}, ... }
    """
    result = {}
    if not symbols:
        return result

    # 先嘗試上市(tse)，再嘗試上櫃(otc)
    def query_mis(ex, syms):
        ex_ch = '|'.join(f'{ex}_{s}.tw' for s in syms)
        url = f'https://mis.twse.com.tw/stock/api/getStockInfo.jsp?ex_ch={ex_ch}&json=1&delay=0'
        try:
            r = proxy_get(url, timeout=10)
            if r.status_code == 200:
                data = r.json().get('msgArray', [])
                for d in data:
                    code = d.get('c', '')
                    try:
                        price = float(d.get('z', '-') if d.get('z', '-') != '-' else d.get('y', '0'))
                        prev  = float(d.get('y', '0') or '0')
                        open_ = float(d.get('o', '-') if d.get('o', '-') != '-' else '0')
                        name  = d.get('n', '')
                        result[code] = {'price': price, 'prev': prev, 'open': open_, 'name': name}
                    except Exception:
                        pass
        except Exception:
            pass

    # 分批，每批 20 檔避免 URL 過長
    batch_size = 20
    for i in range(0, len(symbols), batch_size):
        batch = symbols[i:i+batch_size]
        query_mis('tse', batch)
        # 沒抓到的試 otc
        missing = [s for s in batch if s not in result]
        if missing:
            query_mis('otc', missing)
        time.sleep(0.3)

    return result

def get_twii_realtime():
    """加權指數即時報價"""
    try:
        url = 'https://mis.twse.com.tw/stock/api/getStockInfo.jsp?ex_ch=tse_t00.tw&json=1&delay=0'
        r = proxy_get(url, timeout=10)
        if r.status_code == 200:
            data = r.json().get('msgArray', [])
            if data:
                d = data[0]
                price = float(d.get('z', '0') if d.get('z', '-') != '-' else d.get('y', '0'))
                prev  = float(d.get('y', '0') or '0')
                return {'price': price, 'prev': prev}
    except Exception:
        pass
    return None

# ────────────────────────────────────────────────
# TWSE RWD API（透過 proxy）
# ────────────────────────────────────────────────
def fetch_twse_rwd(url_template, date_obj, max_attempts=5):
    current  = date_obj
    last_err = "未知錯誤"
    for _ in range(max_attempts):
        date_str = current.strftime('%Y%m%d')
        url = url_template.format(date_str)
        try:
            r = proxy_get(url, timeout=15)
            if r.status_code == 200:
                data = r.json()
                if data.get('stat') == 'OK':
                    return data, current, "OK"
                else:
                    last_err = f"API狀態: {data.get('stat', '無資料')}"
            else:
                last_err = f"HTTP {r.status_code}"
        except Exception as e:
            last_err = f"連線異常: {str(e)[:30]}"
        current -= timedelta(days=1)
        while current.weekday() > 4:
            current -= timedelta(days=1)
        time.sleep(1)
    return None, None, last_err

# ────────────────────────────────────────────────
# Telegram 傳訊
# ────────────────────────────────────────────────
def send(chat_id, text):
    try:
        requests.post(API + '/sendMessage', json={'chat_id': chat_id, 'text': text}, timeout=10)
    except Exception as e:
        logging.error('Telegram send error: ' + str(e))

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
    msgs       = []
    tw_time    = datetime.now(timezone(timedelta(hours=8)))
    trade_date = get_last_trading_date()

    msgs.append(f"📊 【盤後籌碼總結】 查詢時間: {tw_time.strftime('%m-%d %H:%M')}")
    msgs.append("-------------------------")

    # 1. 三大法人買賣超金額（BFI82U）
    url_bfi = "https://www.twse.com.tw/rwd/zh/fund/BFI82U?date={}&response=json"
    data_bfi, actual_date, err_bfi = fetch_twse_rwd(url_bfi, trade_date)
    if data_bfi:
        try:
            f_net = i_net = d_net = 0.0
            idx_name = data_bfi['fields'].index('單位名稱')
            idx_diff = data_bfi['fields'].index('買賣差額')
            for row in data_bfi['data']:
                name = row[idx_name]
                amt  = float(row[idx_diff].replace(',', ''))
                if '外資' in name:   f_net += amt
                elif '投信' in name: i_net += amt
                elif '自營' in name: d_net += amt
            total = f_net + i_net + d_net
            msgs.append(f"💰 三大法人買賣超 ({actual_date.strftime('%Y-%m-%d')}):")
            msgs.append(f"   合計: {total/1e8:+.2f} 億")
            msgs.append(f"   外資: {f_net/1e8:+.2f} 億")
            msgs.append(f"   投信: {i_net/1e8:+.2f} 億")
            msgs.append(f"   自營商: {d_net/1e8:+.2f} 億")
            trade_date = actual_date
        except Exception as e:
            msgs.append(f"⚠️ 三大法人金額: 解析錯誤 ({e})")
    else:
        msgs.append(f"⚠️ 三大法人金額: 失敗 ({err_bfi})")

    msgs.append("-------------------------")

    # 2. 外資台指期貨淨未平倉
    try:
        start_date = (tw_time - timedelta(days=30)).strftime('%Y-%m-%d')
        r = requests.get(
            'https://api.finmindtrade.com/api/v4/data',
            params={'dataset': 'TaiwanFuturesInstitutionalInvestors', 'data_id': 'TX', 'start_date': start_date},
            headers=HEADERS_WEB, timeout=15
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

    # 3. 個股三大法人買賣超排行（T86）
    url_t86 = "https://www.twse.com.tw/rwd/zh/fund/T86?date={}&selectType=ALL&response=json"
    data_t86, _, err_t86 = fetch_twse_rwd(url_t86, trade_date)
    if data_t86:
        try:
            fields   = data_t86['fields']
            idx_code = fields.index('證券代號')
            idx_name = fields.index('證券名稱')
            idx_diff = next((i for i, f in enumerate(fields) if '三大法人買賣超股數' in f), None)
            if idx_diff is None:
                msgs.append("⚠️ 買賣超排行: 找不到對應欄位")
            else:
                parsed = []
                for row in data_t86['data']:
                    try:
                        parsed.append({
                            'code': row[idx_code],
                            'name': row[idx_name].strip(),
                            'net':  int(row[idx_diff].replace(',', ''))
                        })
                    except Exception:
                        pass
                sorted_data = sorted(parsed, key=lambda x: x['net'], reverse=True)
                bulk_prices = fetch_bulk_closing_prices()

                def fmt(stocks, is_buy):
                    lines = []
                    for i, item in enumerate(stocks):
                        price = bulk_prices.get(item['code'], '')
                        pd    = f"({price})" if price else "(無報價)"
                        lots  = abs(item['net']) // 1000
                        act   = "買" if is_buy else "賣"
                        lines.append(f"{i+1}. {item['code']} {item['name']} {pd} | {act} {lots:,} 張")
                    return lines

                msgs.append("🔥 【法人買超前 20 名】")
                msgs.extend(fmt(sorted_data[:20], True))
                msgs.append("-------------------------")
                msgs.append("🩸 【法人賣超前 20 名】")
                msgs.extend(fmt(list(reversed(sorted_data[-20:])), False))
        except Exception as e:
            msgs.append(f"⚠️ 買賣超排行: 解析錯誤 ({e})")
    else:
        msgs.append(f"⚠️ 買賣超排行: 失敗 ({err_t86})")

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
# 盤中監控（改用 TWSE MIS API）
# ────────────────────────────────────────────────
def analyze_and_alert(symbol, tw_time):
    if not STOCK_NAMES:
        update_stock_names()

    prices = get_realtime_prices([symbol])
    info   = prices.get(symbol)
    if not info or not info.get('price'):
        return

    price = info['price']
    open_ = info['open']
    name  = info.get('name') or STOCK_NAMES.get(symbol, '')
    disp  = f"{symbol} {name}".strip()

    if symbol not in INTRADAY_STATE:
        INTRADAY_STATE[symbol] = {
            'reported_open':   False,
            'reported_close':  False,
            'last_30m_report': None,
            'day_high':        0,
            'day_high_reported': 0,
            'open_price':      0,
        }

    state  = INTRADAY_STATE[symbol]
    t      = tw_time.strftime('%H:%M')
    alerts = []

    # 開盤
    if t >= '09:00' and not state['reported_open'] and open_ > 0:
        alerts.append(f"🎯 【開盤】{disp} 開盤: {open_:.2f}")
        state['reported_open'] = True
        state['open_price']    = open_
        state['day_high']      = open_

    # 定時 30 分鐘回報
    if tw_time.minute in [0, 30] and '09:30' <= t <= '13:00':
        if state['last_30m_report'] != t:
            prev = info.get('prev', 0)
            if prev > 0:
                chg = price - prev
                pct = chg / prev * 100
                alerts.append(f"⏱ 【定時】{disp} 現價: {price:.2f} ({chg:+.2f}, {pct:+.2f}%)")
            else:
                alerts.append(f"⏱ 【定時】{disp} 現價: {price:.2f}")
            state['last_30m_report'] = t

    # 創新高
    if price > state['day_high'] and t >= '09:00' and state['day_high'] > 0:
        if price != state['day_high_reported']:
            alerts.append(f"🔥 【創新高】{disp} 新高: {price:.2f}")
            state['day_high']          = price
            state['day_high_reported'] = price

    # 收盤
    if t >= '13:30' and not state['reported_close']:
        prev = info.get('prev', 0)
        if prev > 0:
            chg = price - prev
            pct = chg / prev * 100
            alerts.append(f"🏁 【收盤】{disp} 收盤: {price:.2f} ({chg:+.2f}, {pct:+.2f}%)")
        else:
            alerts.append(f"🏁 【收盤】{disp} 收盤: {price:.2f}")
        state['reported_close'] = True

    if alerts:
        send(CHAT_ID, f"[{t}]\n" + "\n".join(alerts))

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
        parts   = text.strip().split()
        symbols = parts[1:] if len(parts) > 1 else TRACK_LIST
        if not symbols:
            send(chat_id, "目前無追蹤清單，請用 /track 新增，或輸入 /price [代號]。")
            return

        send(chat_id, "🔍 正在查詢即時股價...")
        lines    = []
        tw_time  = datetime.now(timezone(timedelta(hours=8)))
        time_str = tw_time.strftime('%H:%M')

        # 加權指數
        twii = get_twii_realtime()
        if twii and twii['prev'] > 0:
            curr = twii['price']
            prev = twii['prev']
            chg  = curr - prev
            pct  = chg / prev * 100
            lines.append(f"📈 加權指數: {curr:.2f} ({chg:+.2f}, {pct:+.2f}%)")
            lines.append("-------------------------")

        # 個股
        prices = get_realtime_prices(symbols)
        for sym in symbols:
            name = STOCK_NAMES.get(sym, '')
            disp = f"{sym} {name}".strip()
            info = prices.get(sym)
            if info and info.get('price'):
                curr = info['price']
                prev = info.get('prev', 0)
                if prev > 0:
                    chg = curr - prev
                    pct = chg / prev * 100
                    lines.append(f"📌 {disp}: {curr:.2f} ({chg:+.2f}, {pct:+.2f}%)")
                else:
                    lines.append(f"📌 {disp}: {curr:.2f}")
            else:
                lines.append(f"⚠️ {disp}: 查詢失敗")

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
                time.sleep(0.5)

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
