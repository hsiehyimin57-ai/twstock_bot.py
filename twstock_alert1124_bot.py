import os, logging, time, threading, requests
from datetime import datetime, timezone, timedelta

logging.basicConfig(level=logging.INFO)

TOKEN     = os.environ.get('TELEGRAM_TOKEN', '')
CHAT_ID   = int(os.environ.get('TELEGRAM_CHAT_ID', '0'))
PROXY_URL = os.environ.get('PROXY_URL', '')
API       = 'https://api.telegram.org/bot' + TOKEN

TRACK_LIST     = []
INTRADAY_STATE = {}
STOCK_NAMES    = {}
PREV_CLOSE     = {}   # 昨收價快取，從 STOCK_DAY_ALL 取得

HEADERS_WEB = {
    'User-Agent': 'Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/122.0.0.0 Safari/537.36',
    'Accept': 'application/json, text/javascript, */*; q=0.01',
    'Referer': 'https://www.twse.com.tw/',
    'X-Requested-With': 'XMLHttpRequest'
}

# ────────────────────────────────────────────────
# Proxy
# ────────────────────────────────────────────────
def proxy_get(target_url, timeout=15):
    if PROXY_URL:
        return requests.get(PROXY_URL, params={'url': target_url}, timeout=timeout)
    return requests.get(target_url, headers=HEADERS_WEB, timeout=timeout)

# ────────────────────────────────────────────────
# 股票名稱 + 昨收價快取（每日 08:30 更新）
# ────────────────────────────────────────────────
def update_stock_names():
    global STOCK_NAMES, PREV_CLOSE
    logging.info("正在更新股票名稱與昨收價...")
    try:
        r = requests.get(
            'https://openapi.twse.com.tw/v1/exchangeReport/STOCK_DAY_ALL',
            headers=HEADERS_WEB, timeout=10
        )
        if r.status_code == 200:
            for item in r.json():
                code = str(item.get('Code', ''))
                STOCK_NAMES[code] = item.get('Name', '')
                try:
                    PREV_CLOSE[code] = float(item.get('ClosingPrice', '0').replace(',', '') or '0')
                except Exception:
                    pass
    except Exception:
        pass
    try:
        r = requests.get(
            'https://www.tpex.org.tw/openapi/v1/t187ap03_L',
            headers=HEADERS_WEB, timeout=10
        )
        if r.status_code == 200:
            for item in r.json():
                code = str(item.get('SecuritiesCompanyCode', ''))
                STOCK_NAMES[code] = item.get('CompanyName', '')
                try:
                    PREV_CLOSE[code] = float(item.get('ClosingPrice', '0').replace(',', '') or '0')
                except Exception:
                    pass
    except Exception:
        pass
    logging.info(f"股票名稱載入 {len(STOCK_NAMES)} 筆，昨收價 {len(PREV_CLOSE)} 筆")

# ────────────────────────────────────────────────
# 全市場收盤價（盤後報告用）
# ────────────────────────────────────────────────
def fetch_bulk_closing_prices():
    prices = {}
    try:
        r = requests.get('https://openapi.twse.com.tw/v1/exchangeReport/STOCK_DAY_ALL', headers=HEADERS_WEB, timeout=10)
        if r.status_code == 200:
            for item in r.json():
                prices[item.get('Code', '')] = item.get('ClosingPrice', '')
    except Exception:
        pass
    try:
        r = requests.get('https://www.tpex.org.tw/openapi/v1/t187ap03_L', headers=HEADERS_WEB, timeout=10)
        if r.status_code == 200:
            for item in r.json():
                prices[item.get('SecuritiesCompanyCode', '')] = item.get('ClosingPrice', '')
    except Exception:
        pass
    return prices

# ────────────────────────────────────────────────
# TWSE MIS 即時報價
# ────────────────────────────────────────────────
def get_realtime_prices(symbols):
    """
    回傳 { code: {'price': float, 'name': str} }
    昨收從 PREV_CLOSE 快取取得（比 MIS API 的 y 欄位更準確）
    """
    result = {}
    if not symbols:
        return result

    def query_mis(ex, syms):
        ex_ch = '|'.join(f'{ex}_{s}.tw' for s in syms)
        url   = f'https://mis.twse.com.tw/stock/api/getStockInfo.jsp?ex_ch={ex_ch}&json=1&delay=0'
        try:
            r = proxy_get(url, timeout=10)
            if r.status_code == 200:
                for d in r.json().get('msgArray', []):
                    code = d.get('c', '')
                    z    = d.get('z', '-')   # 最新成交價
                    o    = d.get('o', '-')   # 開盤價
                    y    = d.get('y', '-')   # 昨收（MIS）
                    n    = d.get('n', '')    # 股名
                    try:
                        price = float(z) if z not in ('-', '', None) else None
                        open_ = float(o) if o not in ('-', '', None) else None
                        mis_y = float(y) if y not in ('-', '', None) else None
                        if price is not None:
                            result[code] = {
                                'price': price,
                                'open':  open_,
                                'mis_y': mis_y,
                                'name':  n,
                            }
                    except Exception:
                        pass
        except Exception:
            pass

    batch_size = 20
    for i in range(0, len(symbols), batch_size):
        batch   = symbols[i:i+batch_size]
        query_mis('tse', batch)
        missing = [s for s in batch if s not in result]
        if missing:
            query_mis('otc', missing)
        time.sleep(0.3)

    return result

def get_twii_realtime():
    try:
        url = 'https://mis.twse.com.tw/stock/api/getStockInfo.jsp?ex_ch=tse_t00.tw&json=1&delay=0'
        r   = proxy_get(url, timeout=10)
        if r.status_code == 200:
            data = r.json().get('msgArray', [])
            if data:
                d = data[0]
                z = d.get('z', '-')
                y = d.get('y', '-')
                price = float(z) if z not in ('-', '', None) else None
                prev  = float(y) if y not in ('-', '', None) else None
                if price and prev:
                    return {'price': price, 'prev': prev}
    except Exception:
        pass
    return None

def get_prev_close(code):
    """優先用快取昨收，fallback 用 MIS y 欄位"""
    return PREV_CLOSE.get(code, 0)

def fmt_chg(price, prev):
    """格式化漲跌"""
    if not prev or prev == 0:
        return ""
    chg = price - prev
    pct = chg / prev * 100
    return f" ({chg:+.2f}, {pct:+.2f}%)"

# ────────────────────────────────────────────────
# TWSE RWD API（透過 proxy）
# ────────────────────────────────────────────────
def fetch_twse_rwd(url_template, date_obj, max_attempts=5):
    current  = date_obj
    last_err = "未知錯誤"
    for _ in range(max_attempts):
        url = url_template.format(current.strftime('%Y%m%d'))
        try:
            r = proxy_get(url, timeout=15)
            if r.status_code == 200:
                data = r.json()
                if data.get('stat') == 'OK':
                    return data, current, "OK"
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
        logging.error('send error: ' + str(e))

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
# 盤中監控
# ────────────────────────────────────────────────
def analyze_and_alert(symbol, tw_time):
    if not STOCK_NAMES:
        update_stock_names()

    prices = get_realtime_prices([symbol])
    info   = prices.get(symbol)
    if not info or info.get('price') is None:
        return

    price = info['price']
    open_ = info.get('open')
    name  = info.get('name') or STOCK_NAMES.get(symbol, '')
    disp  = f"{symbol} {name}".strip()
    prev  = get_prev_close(symbol)   # 從昨日收盤快取取得，比 MIS y 欄更準

    if symbol not in INTRADAY_STATE:
        INTRADAY_STATE[symbol] = {
            'reported_open':   False,
            'reported_close':  False,
            'last_30m_report': None,
            'day_high':        0,
        }

    state  = INTRADAY_STATE[symbol]
    t      = tw_time.strftime('%H:%M')
    alerts = []

    # 開盤
    if t >= '09:00' and not state['reported_open'] and open_ and open_ > 0:
        chg_str = fmt_chg(open_, prev)
        alerts.append(f"🎯 【開盤】{disp} 開盤: {open_:.2f}{chg_str}")
        state['reported_open'] = True
        state['day_high']      = open_

    # 定時 30 分鐘
    if tw_time.minute in [0, 30] and '09:30' <= t <= '13:00':
        if state['last_30m_report'] != t:
            chg_str = fmt_chg(price, prev)
            alerts.append(f"⏱ 【定時】{disp} 現價: {price:.2f}{chg_str}")
            state['last_30m_report'] = t

    # 創新高（加漲跌幅）
    if price > state['day_high'] > 0 and t >= '09:00':
        chg_str = fmt_chg(price, prev)
        alerts.append(f"🔥 【創新高】{disp} 新高: {price:.2f}{chg_str}")
        state['day_high'] = price

    # 收盤
    if t >= '13:30' and not state['reported_close']:
        chg_str = fmt_chg(price, prev)
        alerts.append(f"🏁 【收盤】{disp} 收盤: {price:.2f}{chg_str}")
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
        if twii:
            curr    = twii['price']
            prev_tw = twii['prev']
            chg_str = fmt_chg(curr, prev_tw)
            lines.append(f"📈 加權指數: {curr:.2f}{chg_str}")
            lines.append("-------------------------")

        # 個股
        prices = get_realtime_prices(symbols)
        for sym in symbols:
            name = STOCK_NAMES.get(sym, '')
            disp = f"{sym} {name}".strip()
            info = prices.get(sym)
            if info and info.get('price') is not None:
                curr    = info['price']
                prev    = get_prev_close(sym)
                chg_str = fmt_chg(curr, prev)
                lines.append(f"📌 {disp}: {curr:.2f}{chg_str}")
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

        # 每日 08:30 重置狀態並更新名稱與昨收
        if time_str == '08:30' and tw_time.second == 0:
            INTRADAY_STATE.clear()
            update_stock_names()
            time.sleep(1)

        # 16:30 自動送盤後報告
        if time_str == '16:30' and tw_time.second == 0 and tw_time.weekday() < 5:
            threading.Thread(target=post_market_job, args=(CHAT_ID,), daemon=True).start()
            time.sleep(1)

        # 盤中逐分監控
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
