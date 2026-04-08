import os, logging, time, threading, requests
import urllib3
urllib3.disable_warnings(urllib3.exceptions.InsecureRequestWarning)
from datetime import datetime, timezone, timedelta

logging.basicConfig(level=logging.INFO)

TOKEN     = os.environ.get('TELEGRAM_TOKEN', '')
CHAT_ID   = int(os.environ.get('TELEGRAM_CHAT_ID', '0'))
PROXY_URL = os.environ.get('PROXY_URL', '')
GITHUB_PREV_CLOSE_URL = os.environ.get('GITHUB_PREV_CLOSE_URL', '')
API = 'https://api.telegram.org/bot' + TOKEN

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
# Proxy
# ────────────────────────────────────────────────
def proxy_get(target_url, timeout=15):
    if PROXY_URL:
        return requests.get(PROXY_URL, params={'url': target_url}, timeout=timeout)
    return requests.get(target_url, headers=HEADERS_WEB, timeout=timeout)

# ────────────────────────────────────────────────
# 字元寬度工具（中文字寬修正）
# ────────────────────────────────────────────────
def str_width(s):
    w = 0
    for c in s:
        w += 2 if ord(c) > 0x7F else 1
    return w

def pad_str(s, width):
    return s + ' ' * max(0, width - str_width(s))

# ────────────────────────────────────────────────
# 昨收價：優先 GitHub，fallback API
# ────────────────────────────────────────────────
def load_prev_close_from_github():
    global PREV_CLOSE
    if not GITHUB_PREV_CLOSE_URL:
        logging.warning("GITHUB_PREV_CLOSE_URL 未設定")
        return False
    try:
        r = requests.get(GITHUB_PREV_CLOSE_URL, timeout=15, verify=False)
        logging.info(f"GitHub HTTP {r.status_code}, 內容長度 {len(r.text)} bytes")
        if r.status_code == 200 and r.text.strip():
            data = r.json()
            if data:
                PREV_CLOSE.update(data)
                logging.info(f"GitHub 昨收載入 {len(data)} 筆")
                return True
            else:
                logging.warning("GitHub prev_close.json 為空 JSON")
        else:
            logging.warning(f"GitHub 載入失敗: HTTP {r.status_code}")
    except Exception as e:
        logging.error(f"GitHub 昨收載入錯誤: {e}")
    return False

def load_prev_close_from_api():
    global PREV_CLOSE
    count = 0
    try:
        r = requests.get(
            'https://openapi.twse.com.tw/v1/exchangeReport/STOCK_DAY_ALL',
            headers=HEADERS_WEB, timeout=15, verify=False
        )
        if r.status_code == 200:
            for item in r.json():
                code = str(item.get('Code', ''))
                v = item.get('ClosingPrice', '').replace(',', '')
                if code and v:
                    try:
                        PREV_CLOSE[code] = float(v)
                        count += 1
                    except Exception:
                        pass
        else:
            logging.error(f"TWSE 昨收失敗: HTTP {r.status_code}")
    except Exception as e:
        logging.error(f"TWSE 昨收失敗: {e}")
    try:
        r = requests.get(
            'https://www.tpex.org.tw/openapi/v1/t187ap03_L',
            headers=HEADERS_WEB, timeout=15, verify=False
        )
        if r.status_code == 200 and r.text.strip():
            for item in r.json():
                code = str(item.get('SecuritiesCompanyCode', ''))
                v = item.get('ClosingPrice', '').replace(',', '')
                if code and v:
                    try:
                        PREV_CLOSE[code] = float(v)
                        count += 1
                    except Exception:
                        pass
        elif r.status_code != 200:
            logging.error(f"TPEx 昨收失敗: HTTP {r.status_code}")
    except Exception as e:
        logging.error(f"TPEx 昨收失敗: {e}")
    logging.info(f"API 昨收載入 {count} 筆")
    return count > 0

def update_prev_close():
    """優先 GitHub，失敗則 fallback 到 API"""
    logging.info("開始更新昨收價...")
    if not load_prev_close_from_github():
        logging.info("GitHub 載入失敗，改用 API fallback")
        load_prev_close_from_api()
    logging.info(f"昨收價共 {len(PREV_CLOSE)} 筆")

def get_prev_close(code):
    return PREV_CLOSE.get(code, 0)

# ────────────────────────────────────────────────
# 股票名稱快取
# ────────────────────────────────────────────────
def update_stock_names():
    global STOCK_NAMES
    logging.info("正在更新股票名稱...")
    try:
        r = requests.get(
            'https://openapi.twse.com.tw/v1/exchangeReport/STOCK_DAY_ALL',
            headers=HEADERS_WEB, timeout=15, verify=False
        )
        if r.status_code == 200:
            for item in r.json():
                STOCK_NAMES[str(item.get('Code', ''))] = item.get('Name', '')
    except Exception as e:
        logging.error(f"名稱更新失敗(TWSE): {e}")
    try:
        r = requests.get(
            'https://www.tpex.org.tw/openapi/v1/t187ap03_L',
            headers=HEADERS_WEB, timeout=15, verify=False
        )
        if r.status_code == 200 and r.text.strip():
            for item in r.json():
                STOCK_NAMES[str(item.get('SecuritiesCompanyCode', ''))] = item.get('CompanyName', '')
    except Exception as e:
        logging.error(f"名稱更新失敗(TPEx): {e}")
    logging.info(f"股票名稱載入 {len(STOCK_NAMES)} 筆")

# ────────────────────────────────────────────────
# 全市場收盤價（盤後報告用）
# ────────────────────────────────────────────────
def fetch_bulk_closing_prices():
    prices = {}
    try:
        r = requests.get('https://openapi.twse.com.tw/v1/exchangeReport/STOCK_DAY_ALL', headers=HEADERS_WEB, timeout=15, verify=False)
        if r.status_code == 200:
            for item in r.json():
                prices[item.get('Code', '')] = item.get('ClosingPrice', '')
    except Exception:
        pass
    try:
        r = requests.get('https://www.tpex.org.tw/openapi/v1/t187ap03_L', headers=HEADERS_WEB, timeout=15, verify=False)
        if r.status_code == 200 and r.text.strip():
            for item in r.json():
                prices[item.get('SecuritiesCompanyCode', '')] = item.get('ClosingPrice', '')
    except Exception:
        pass
    return prices

# ────────────────────────────────────────────────
# TWSE MIS 即時報價
# ────────────────────────────────────────────────
def query_mis_single(code):
    for ex in ['tse', 'otc']:
        url = f'https://mis.twse.com.tw/stock/api/getStockInfo.jsp?ex_ch={ex}_{code}.tw&json=1&delay=0'
        try:
            r = proxy_get(url, timeout=8)
            if r.status_code == 200:
                arr = r.json().get('msgArray', [])
                if arr:
                    d     = arr[0]
                    z     = d.get('z', '-')
                    o     = d.get('o', '-')
                    n     = d.get('n', '')
                    price = float(z) if z not in ('-', '', None) else None
                    open_ = float(o) if o not in ('-', '', None) else None
                    if price is not None:
                        return {'price': price, 'open': open_, 'name': n}
        except Exception:
            pass
    return None

def get_twii_realtime():
    url = 'https://mis.twse.com.tw/stock/api/getStockInfo.jsp?ex_ch=tse_t00.tw&json=1&delay=0'
    try:
        r = proxy_get(url, timeout=10)
        if r.status_code == 200:
            arr = r.json().get('msgArray', [])
            if arr:
                d     = arr[0]
                z     = d.get('z', '-')
                y     = d.get('y', '-')
                price = float(z) if z not in ('-', '', None) else None
                prev  = float(y) if y not in ('-', '', None) else None
                if price and prev:
                    return {'price': price, 'prev': prev}
    except Exception:
        pass
    return None

# ────────────────────────────────────────────────
# 格式工具
# ────────────────────────────────────────────────
def fmt_price(price):
    """
    股價格式化（最多7位含小數點）：
    ≥1000 → 無小數（1860, 10700）
    <1000 → 2位小數（557.00, 89.40）
    """
    if price >= 1000:
        return f"{price:.0f}"
    else:
        return f"{price:.2f}"

def fmt_chg(price, prev):
    if not prev or prev == 0:
        return ""
    chg = price - prev
    pct = chg / prev * 100
    return f" ({chg:+.2f}, {pct:+.2f}%)"

def make_stock_line(symbol, name, price, prev):
    """
    手機優化等寬格式，前綴 ▲▼─ 標示漲跌平：
    ▲3008大立光   2225   55 +2.5%
    ▼2059川湖     3175  170 -5.1%
    ─2002中鋼    19.90    0  0.0%
    """
    code_s  = symbol[:5]
    name_s  = pad_str((name or '')[:3], 5)
    price_s = f"{fmt_price(price):>7}"
    if prev and prev > 0:
        chg = price - prev
        pct = chg / prev * 100
        if chg > 0:
            arrow = "▲"
        elif chg < 0:
            arrow = "▼"
        else:
            arrow = "─"
        # 漲跌金額不顯示 +/-，絕對值
        chg_abs = abs(chg)
        # 有小數尾數保留一位，純整數則無小數
        if chg_abs == int(chg_abs):
            chg_s = f"{chg_abs:>6.0f}"
        else:
            chg_s = f"{chg_abs:>6.2f}"
        pct_s = f"{pct:>+5.1f}%"
    else:
        arrow = "─"
        chg_s = f"{'--':>6}"
        pct_s = f"{'--':>6}"
    return f"{arrow}{code_s}{name_s}{price_s}{chg_s}{pct_s}"

def send_table(chat_id, title, lines):
    if not lines:
        return
    send_md(chat_id, f"{title}\n```\n" + "\n".join(lines) + "\n```")

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

    # 三大法人買賣超金額
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

    # 外資台指期貨淨未平倉
    try:
        start_date = (tw_time - timedelta(days=30)).strftime('%Y-%m-%d')
        r = proxy_get(
            f'https://api.finmindtrade.com/api/v4/data?dataset=TaiwanFuturesInstitutionalInvestors&data_id=TX&start_date={start_date}',
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

    def parse_ranking(data, keyword):
        fields   = data['fields']
        idx_code = fields.index('證券代號')
        idx_name = fields.index('證券名稱')
        idx_diff = next((i for i, f in enumerate(fields) if keyword in f), None)
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

    def fmt_ranking(stocks):
        rows     = []
        max_lots = max((abs(i['net'])//1000 for i in stocks), default=1)
        lots_w   = len(f"{max_lots:,}") + 1
        for item in stocks:
            code = pad_str(item['code'][:6], 7)
            name = pad_str(item['name'][:4], 7)
            lots = abs(item['net']) // 1000
            rows.append(f"{code}{name}{lots:{lots_w},}")
        return ["```\n" + "\n".join(rows) + "\n```"]

    for label, url_tmpl in [
        ("外資",   "https://www.twse.com.tw/rwd/zh/fund/TWT38U?date={}&response=json"),
        ("投信",   "https://www.twse.com.tw/rwd/zh/fund/TWT44U?date={}&response=json"),
        ("自營商", "https://www.twse.com.tw/rwd/zh/fund/TWT43U?date={}&response=json"),
    ]:
        data, _, err = fetch_twse_rwd(url_tmpl, trade_date)
        if data:
            try:
                s = parse_ranking(data, '買賣超')
                if s:
                    msgs.append(f"🔥 【{label}買超前 20 名】")
                    msgs.extend(fmt_ranking(s[:20]))
                    msgs.append("-------------------------")
                    msgs.append(f"🩸 【{label}賣超前 20 名】")
                    msgs.extend(fmt_ranking(list(reversed(s[-20:]))))
                    msgs.append("-------------------------")
            except Exception as e:
                msgs.append(f"⚠️ {label}排行錯誤: {e}")
                msgs.append("-------------------------")
        else:
            msgs.append(f"⚠️ {label}排行失敗: {err}")
            msgs.append("-------------------------")

    # 切割 sections
    full     = "\n".join(msgs)
    sections = []
    current  = []
    in_code  = False
    for line in full.split("\n"):
        if line.startswith("```"):
            if not in_code:
                if current:
                    sections.append("\n".join(current))
                    current = []
                in_code = True
                current.append(line)
            else:
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
        for section in generate_post_market_msg():
            if section.strip():
                send_md(chat_id, section)
    except Exception as e:
        logging.error(f"post_market_job crashed: {e}")
        send(chat_id, f"❌ 系統錯誤: {str(e)[:80]}")

# ────────────────────────────────────────────────
# 盤中分析
# ────────────────────────────────────────────────
def analyze_stock(symbol, tw_time):
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
            'pending_high':    None,
        }

    state  = INTRADAY_STATE[symbol]
    t      = tw_time.strftime('%H:%M')
    result = {'open': None, 'timed': None, 'close': None}

    # 開盤（09:00~09:10 彙整，之後不再追）
    if '09:00' <= t <= '09:10' and not state['reported_open'] and open_ and open_ > 0:
        result['open']         = make_stock_line(symbol, name, open_, prev)
        state['reported_open'] = True
        state['day_high']      = open_
    elif t > '09:10' and not state['reported_open']:
        state['reported_open'] = True
        if open_ and open_ > 0:
            state['day_high'] = open_

    # 定時 30 分鐘
    if tw_time.minute in [0, 30] and '09:30' <= t <= '13:00':
        if state['last_30m_report'] != t:
            result['timed']          = make_stock_line(symbol, name, price, prev)
            state['last_30m_report'] = t

    # 創新高（暫存，每 5 分鐘彙總）
    if price > state['day_high'] > 0 and t >= '09:00':
        state['pending_high'] = make_stock_line(symbol, name, price, prev)
        state['day_high']     = price

    # 收盤
    if t >= '13:30' and not state['reported_close']:
        result['close']         = make_stock_line(symbol, name, price, prev)
        state['reported_close'] = True

    return result

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
            TRACK_LIST = parts[1:]
            INTRADAY_STATE.clear()
            send(chat_id, f"✅ 已更新盯盤清單（{len(TRACK_LIST)} 檔）：{', '.join(TRACK_LIST)}")
        else:
            send(chat_id, "請輸入股票代號，例如：/track 2330 2603")

    elif text.startswith('/add'):
        parts = text.strip().split()
        if len(parts) > 1:
            added = []
            for s in parts[1:]:
                if s not in TRACK_LIST:
                    TRACK_LIST.append(s)
                    added.append(s)
            if added:
                send(chat_id, f"✅ 已新增：{', '.join(added)}\n📋 目前清單（{len(TRACK_LIST)} 檔）：{', '.join(TRACK_LIST)}")
            else:
                send(chat_id, "⚠️ 指定代號已在清單中")
        else:
            send(chat_id, "請輸入要新增的代號，例如：/add 2330")

    elif text.startswith('/remove'):
        parts = text.strip().split()
        if len(parts) > 1:
            removed = []
            for s in parts[1:]:
                if s in TRACK_LIST:
                    TRACK_LIST.remove(s)
                    removed.append(s)
            if removed:
                remain = f"：{', '.join(TRACK_LIST)}" if TRACK_LIST else "（空）"
                send(chat_id, f"✅ 已移除：{', '.join(removed)}\n📋 目前清單（{len(TRACK_LIST)} 檔）{remain}")
            else:
                send(chat_id, "⚠️ 指定代號不在清單中")
        else:
            send(chat_id, "請輸入要移除的代號，例如：/remove 2330")

    elif text.startswith('/list'):
        if TRACK_LIST:
            send(chat_id, f"📋 追蹤清單（{len(TRACK_LIST)} 檔）：{', '.join(TRACK_LIST)}")
        else:
            send(chat_id, "目前無追蹤股票，請用 /track 新增。")

    elif text.startswith('/clear'):
        TRACK_LIST.clear()
        INTRADAY_STATE.clear()
        send(chat_id, "🗑 追蹤清單已清空。")

    elif text.startswith('/postmarket'):
        threading.Thread(target=post_market_job, args=(chat_id,), daemon=True).start()

    elif text.startswith('/price'):
        # 若昨收為空先強制更新一次
        if not PREV_CLOSE:
            logging.info("/price 觸發昨收更新")
            update_prev_close()
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

        header = [f"📊 【{time_str} 即時報價】"]
        twii   = get_twii_realtime()
        if twii:
            curr = twii['price']
            prev = twii['prev']
            header.append(f"📈 加權指數: {curr:,.2f}{fmt_chg(curr, prev)}")
        # 顯示昨收載入狀態
        header.append(f"ℹ️ 昨收價已載入 {len(PREV_CLOSE)} 筆")
        send_md(chat_id, "\n".join(header))

        rows = []
        for sym in symbols:
            info = query_mis_single(sym)
            if info and info.get('price') is not None:
                curr = info['price']
                name = info.get('name') or STOCK_NAMES.get(sym, '')
                prev = get_prev_close(sym)
                rows.append(make_stock_line(sym, name, curr, prev))
            else:
                rows.append(pad_str(sym, 7) + "查詢失敗")
            time.sleep(0.3)
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

        # 08:30 重置 + 更新名稱與昨收
        if time_str == '08:30' and tw_time.second == 0:
            INTRADAY_STATE.clear()
            update_stock_names()
            update_prev_close()
            time.sleep(1)

        # 16:30 自動盤後報告
        if time_str == '16:30' and tw_time.second == 0 and tw_time.weekday() < 5:
            threading.Thread(target=post_market_job, args=(CHAT_ID,), daemon=True).start()
            time.sleep(1)

        # 盤中監控
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
            send_table(CHAT_ID, f"🎯 【開盤 {t}】",  open_lines)
            send_table(CHAT_ID, f"⏱ 【定時 {t}】",  timed_lines)
            send_table(CHAT_ID, f"🏁 【收盤 {t}】",  close_lines)

            # 每 5 分鐘彙總創新高
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
    update_prev_close()
    threading.Thread(target=market_monitor_loop, daemon=True).start()
    polling_loop()
