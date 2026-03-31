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
PREV_CLOSE     = {}

HEADERS_WEB = {
    'User-Agent': 'Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/122.0.0.0 Safari/537.36',
    'Accept': 'application/json, text/javascript, */*; q=0.01',
    'Referer': 'https://www.twse.com.tw/',
    'X-Requested-With': 'XMLHttpRequest'
}

# ────────────────────────────────────────────────
# Proxy（所有對外請求都走這裡）
# ────────────────────────────────────────────────
def proxy_get(target_url, timeout=15):
    if PROXY_URL:
        return requests.get(PROXY_URL, params={'url': target_url}, timeout=timeout)
    return requests.get(target_url, headers=HEADERS_WEB, timeout=timeout)

# ────────────────────────────────────────────────
# 股票名稱 + 昨收價快取
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
                    v = item.get('ClosingPrice', '').replace(',', '')
                    if v:
                        PREV_CLOSE[code] = float(v)
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
                    v = item.get('ClosingPrice', '').replace(',', '')
                    if v:
                        PREV_CLOSE[code] = float(v)
                except Exception:
                    pass
    except Exception:
        pass
    logging.info(f"名稱 {len(STOCK_NAMES)} 筆，昨收 {len(PREV_CLOSE)} 筆")

# ────────────────────────────────────────────────
# 全市場收盤價（盤後用）
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
# TWSE MIS 即時報價（每次查單一檔，避免被限速）
# ────────────────────────────────────────────────
def query_mis_single(code):
    """
    查單一股票，先試上市(tse)，失敗再試上櫃(otc)。
    回傳 {'price': float, 'open': float, 'name': str} 或 None
    """
    for ex in ['tse', 'otc']:
        url = f'https://mis.twse.com.tw/stock/api/getStockInfo.jsp?ex_ch={ex}_{code}.tw&json=1&delay=0'
        try:
            r = proxy_get(url, timeout=8)
            if r.status_code == 200:
                arr = r.json().get('msgArray', [])
                if arr:
                    d = arr[0]
                    z = d.get('z', '-')
                    o = d.get('o', '-')
                    y = d.get('y', '-')   # 昨收
                    n = d.get('n', '')
                    price = float(z) if z not in ('-', '', None) else None
                    open_ = float(o) if o not in ('-', '', None) else None
                    mis_y = float(y) if y not in ('-', '', None) else None
                    if price is not None:
                        # 用 MIS y 欄補充昨收快取
                        if mis_y and code not in PREV_CLOSE:
                            PREV_CLOSE[code] = mis_y
                        return {'price': price, 'open': open_, 'name': n, 'prev': mis_y}
        except Exception:
            pass
    return None

def get_realtime_prices(symbols):
    """批次查詢，每檔間隔 0.2 秒避免被限速"""
    result = {}
    for sym in symbols:
        info = query_mis_single(sym)
        if info:
            result[sym] = info
        time.sleep(0.2)
    return result

def get_twii_realtime():
    url = 'https://mis.twse.com.tw/stock/api/getStockInfo.jsp?ex_ch=tse_t00.tw&json=1&delay=0'
    try:
        r = proxy_get(url, timeout=10)
        if r.status_code == 200:
            arr = r.json().get('msgArray', [])
            if arr:
                d = arr[0]
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
    return PREV_CLOSE.get(code, 0)

def fmt_chg(price, prev):
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

def send_md(chat_id, text):
    """用 Markdown 格式傳送（支援等寬字體）"""
    try:
        requests.post(API + '/sendMessage', json={
            'chat_id': chat_id, 'text': text, 'parse_mode': 'Markdown'
        }, timeout=10)
    except Exception as e:
        logging.error('send_md error: ' + str(e))

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
        fut_url = f'https://api.finmindtrade.com/api/v4/data?dataset=TaiwanFuturesInstitutionalInvestors&data_id=TX&start_date={start_date}'
        r = proxy_get(fut_url, timeout=15)
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

    bulk_prices = fetch_bulk_closing_prices()

    def parse_ranking(data, diff_field_keyword):
        """解析買賣超排行，回傳 sorted list"""
        fields   = data['fields']
        idx_code = fields.index('證券代號')
        idx_name = fields.index('證券名稱')
        idx_diff = next((i for i, f in enumerate(fields) if diff_field_keyword in f), None)
        if idx_diff is None:
            return None
        parsed = []
        for row in data['data']:
            try:
                parsed.append({
                    'code': row[idx_code],
                    'name': row[idx_name].strip(),
                    'net':  int(row[idx_diff].replace(',', ''))
                })
            except Exception:
                pass
        return sorted(parsed, key=lambda x: x['net'], reverse=True)

    def fmt_ranking(stocks, is_buy):
        """緊湊格式：代號 名稱 張數（個位數對齊）"""
        rows = []
        max_lots = max((abs(i['net'])//1000 for i in stocks), default=1)
        # 決定張數欄寬（依最大值位數）
        lots_w = len(f"{max_lots:,}") + 1
        for item in stocks:
            code = pad_str(item['code'][:6], 7)
            name = pad_str(item['name'][:4], 7)   # 名稱寬7，個位數才能對齊
            lots = abs(item['net']) // 1000
            rows.append(f"{code}{name}{lots:{lots_w},}")
        return ["```\n" + "\n".join(rows) + "\n```"]

    # 外資買賣超排行（TWT38U）
    url_foreign = "https://www.twse.com.tw/rwd/zh/fund/TWT38U?date={}&response=json"
    data_foreign, _, err_foreign = fetch_twse_rwd(url_foreign, trade_date)
    if data_foreign:
        try:
            sorted_f = parse_ranking(data_foreign, '買賣超')
            if sorted_f:
                msgs.append("🔥 【外資買超前 20 名】")
                msgs.extend(fmt_ranking(sorted_f[:20], True))
                msgs.append("-------------------------")
                msgs.append("🩸 【外資賣超前 20 名】")
                msgs.extend(fmt_ranking(list(reversed(sorted_f[-20:])), False))
            else:
                msgs.append("⚠️ 外資排行: 找不到欄位")
        except Exception as e:
            msgs.append(f"⚠️ 外資排行: 解析錯誤 ({e})")
    else:
        msgs.append(f"⚠️ 外資排行: 失敗 ({err_foreign})")

    msgs.append("-------------------------")

    # 投信買賣超排行（TWT44U）
    url_trust = "https://www.twse.com.tw/rwd/zh/fund/TWT44U?date={}&response=json"
    data_trust, _, err_trust = fetch_twse_rwd(url_trust, trade_date)
    if data_trust:
        try:
            sorted_t = parse_ranking(data_trust, '買賣超')
            if sorted_t:
                msgs.append("🔥 【投信買超前 20 名】")
                msgs.extend(fmt_ranking(sorted_t[:20], True))
                msgs.append("-------------------------")
                msgs.append("🩸 【投信賣超前 20 名】")
                msgs.extend(fmt_ranking(list(reversed(sorted_t[-20:])), False))
            else:
                msgs.append("⚠️ 投信排行: 找不到欄位")
        except Exception as e:
            msgs.append(f"⚠️ 投信排行: 解析錯誤 ({e})")
    else:
        msgs.append(f"⚠️ 投信排行: 失敗 ({err_trust})")

    msgs.append("-------------------------")

    # 自營商買賣超排行（TWT43U）
    url_dealer = "https://www.twse.com.tw/rwd/zh/fund/TWT43U?date={}&response=json"
    data_dealer, _, err_dealer = fetch_twse_rwd(url_dealer, trade_date)
    if data_dealer:
        try:
            sorted_d = parse_ranking(data_dealer, '買賣超')
            if sorted_d:
                msgs.append("🔥 【自營商買超前 20 名】")
                msgs.extend(fmt_ranking(sorted_d[:20], True))
                msgs.append("-------------------------")
                msgs.append("🩸 【自營商賣超前 20 名】")
                msgs.extend(fmt_ranking(list(reversed(sorted_d[-20:])), False))
            else:
                msgs.append("⚠️ 自營商排行: 找不到欄位")
        except Exception as e:
            msgs.append(f"⚠️ 自營商排行: 解析錯誤 ({e})")
    else:
        msgs.append(f"⚠️ 自營商排行: 失敗 ({err_dealer})")

    # 把含 ``` 的 code block 獨立成一則，避免被截斷
    full = "\n".join(msgs)
    sections = []
    current = []
    in_code = False
    for line in full.split("\n"):
        if line.startswith("```"):
            if not in_code:
                # 開始 code block：先把前面的普通訊息送出
                if current:
                    sections.append("\n".join(current))
                    current = []
                in_code = True
                current.append(line)
            else:
                # 結束 code block
                current.append(line)
                sections.append("\n".join(current))
                current = []
                in_code = False
        else:
            current.append(line)
    if current:
        sections.append("\n".join(current))
    return sections


def post_market_job(chat_id):
    try:
        send(chat_id, "⏳ 正在彙整盤後籌碼，約需 20～30 秒，請稍候...")
        sections = generate_post_market_msg()
        for section in sections:
            if section.strip():
                send_md(chat_id, section)
    except Exception as e:
        logging.error(f"post_market_job crashed: {e}")
        send(chat_id, f"❌ 系統錯誤: {str(e)[:80]}")

# ────────────────────────────────────────────────
# 盤中監控（每檔獨立查詢，避免限速）
# ────────────────────────────────────────────────
def str_width(s):
    """計算字串顯示寬度（中文字算2，英數算1）"""
    w = 0
    for c in s:
        w += 2 if ord(c) > 0x7F else 1
    return w

def pad_str(s, width):
    """補空白到指定顯示寬度"""
    return s + ' ' * max(0, width - str_width(s))

def fmt_stock_row(symbol, name, price, prev, label=""):
    """
    格式：代號 名稱  股價  漲跌  漲跌幅
    2330 台積電 1780 -20 -1.11%
    """
    name_s = (name or '')[:4]
    code_col  = pad_str(symbol[:6], 7)
    name_col  = pad_str(name_s, 6)
    price_col = f"{price:>8.2f}"
    if prev and prev > 0:
        chg = price - prev
        pct = chg / prev * 100
        chg_col = f"{chg:>+7.2f}"
        pct_col = f"{pct:>+6.2f}%"
    else:
        chg_col = "       "
        pct_col = "      "
    row = f"{code_col}{name_col}{price_col}{chg_col}{pct_col}"
    return "```\n" + row + "\n```"

def fmt_price(price):
    """股價最多6位（含小數點），自動調整精度"""
    s = f"{price:.2f}"
    if len(s) > 7:   # 如 10700.00 → 10700
        s = f"{price:.0f}"
    return s

def make_stock_line(symbol, name, price, prev):
    """單行等寬格式：代號 名稱 股價 漲跌 漲跌幅"""
    name_s  = pad_str((name or '')[:3], 5)   # 名稱最多3字，寬5
    code_s  = pad_str(symbol[:6], 7)          # 代號寬7
    price_s = f"{fmt_price(price):>7}"        # 股價最多7字元
    if prev and prev > 0:
        chg   = price - prev
        pct   = chg / prev * 100
        chg_s = f"{chg:>+7.2f}"
        pct_s = f"{pct:>+6.2f}%"
    else:
        chg_s = "       "
        pct_s = "      "
    return f"{code_s}{name_s}{price_s}{chg_s}{pct_s}"

def analyze_stock(symbol, tw_time):
    """分析單一股票，回傳 dict"""
    info = query_mis_single(symbol)
    if not info or info.get('price') is None:
        return None

    price = info['price']
    open_ = info.get('open')
    name  = info.get('name') or STOCK_NAMES.get(symbol, '')
    prev  = get_prev_close(symbol)

    if symbol not in INTRADAY_STATE:
        INTRADAY_STATE[symbol] = {
            'reported_open':   False,
            'reported_close':  False,
            'last_30m_report': None,
            'day_high':        0,
            'pending_high':    None,   # 暫存創新高，5分鐘後彙總
        }

    state  = INTRADAY_STATE[symbol]
    t      = tw_time.strftime('%H:%M')
    result = {'open': None, 'timed': None, 'high': None, 'close': None}

    # 開盤（只在 09:00-09:10 內報，之後不再追）
    if '09:00' <= t <= '09:10' and not state['reported_open'] and open_ and open_ > 0:
        result['open'] = make_stock_line(symbol, name, open_, prev)
        state['reported_open'] = True
        state['day_high']      = open_
    elif t > '09:10' and not state['reported_open']:
        # 超過 09:10 還沒開盤就直接標記，不再回報
        state['reported_open'] = True
        if open_ and open_ > 0:
            state['day_high'] = open_

    # 定時
    if tw_time.minute in [0, 30] and '09:30' <= t <= '13:00':
        if state['last_30m_report'] != t:
            result['timed'] = make_stock_line(symbol, name, price, prev)
            state['last_30m_report'] = t

    # 創新高 → 暫存，由 market_monitor_loop 每5分鐘彙總
    if price > state['day_high'] > 0 and t >= '09:00':
        state['pending_high'] = make_stock_line(symbol, name, price, prev)
        state['day_high']     = price

    # 收盤
    if t >= '13:30' and not state['reported_close']:
        result['close'] = make_stock_line(symbol, name, price, prev)
        state['reported_close'] = True

    return result

def send_table(chat_id, title, lines):
    """彙整多行成一則等寬表格訊息"""
    if not lines:
        return
    body = "\n".join(lines)
    send_md(chat_id, f"{title}\n```\n{body}\n```")

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
            # 立即更新昨收快取，確保漲跌幅正確
            if not PREV_CLOSE:
                threading.Thread(target=update_stock_names, daemon=True).start()
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

        send(chat_id, "🔍 正在查詢即時股價，請稍候...")
        tw_time  = datetime.now(timezone(timedelta(hours=8)))
        time_str = tw_time.strftime('%H:%M')

        header_lines = [f"📊 【{time_str} 即時報價】"]

        # 加權指數
        twii = get_twii_realtime()
        if twii:
            curr    = twii['price']
            prev_tw = twii['prev']
            chg_str = fmt_chg(curr, prev_tw)
            header_lines.append(f"📈 加權指數: {curr:,.2f}{chg_str}")

        send_md(chat_id, "\n".join(header_lines))

        # 個股表格（每 10 檔一組）
        rows = []
        for sym in symbols:
            name = STOCK_NAMES.get(sym, '')
            info = query_mis_single(sym)
            if info and info.get('price') is not None:
                curr = info['price']
                prev = get_prev_close(sym)
                code_col  = pad_str(sym[:6], 7)
                name_col  = pad_str((name or '')[:4], 6)
                price_col = f"{curr:>8.2f}"
                if prev and prev > 0:
                    chg = curr - prev
                    pct = chg / prev * 100
                    chg_col = f"{chg:>+7.2f}"
                    pct_col = f"{pct:>+6.2f}%"
                else:
                    chg_col = "       "
                    pct_col = "      "
                rows.append(f"{code_col}{name_col}{price_col}{chg_col}{pct_col}")
            else:
                rows.append(pad_str(sym, 7) + "查詢失敗")
            time.sleep(0.2)
            # 每 10 筆送出一則
            if len(rows) >= 10:
                send_md(chat_id, "```\n" + "\n".join(rows) + "\n```")
                rows = []

        if rows:
            send_md(chat_id, "```\n" + "\n".join(rows) + "\n```")

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
            open_lines  = []
            timed_lines = []
            close_lines = []

            for symbol in TRACK_LIST:
                r = analyze_stock(symbol, tw_time)
                if r:
                    if r['open']:  open_lines.append(r['open'])
                    if r['timed']: timed_lines.append(r['timed'])
                    if r['close']: close_lines.append(r['close'])
                time.sleep(0.5)

            t = tw_time.strftime('%H:%M')
            send_table(CHAT_ID, f"🎯 【開盤 {t}】", open_lines)
            send_table(CHAT_ID, f"⏱ 【定時 {t}】", timed_lines)
            send_table(CHAT_ID, f"🏁 【收盤 {t}】", close_lines)

            # 每5分鐘彙總創新高
            if tw_time.minute % 5 == 0:
                high_lines = []
                for symbol in TRACK_LIST:
                    state = INTRADAY_STATE.get(symbol, {})
                    if state.get('pending_high'):
                        high_lines.append(state['pending_high'])
                        state['pending_high'] = None
                send_table(CHAT_ID, f"🔥 【創新高彙總 {t}】", high_lines)

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
