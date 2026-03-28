import os, json, logging, time, threading, requests
import urllib.parse
from datetime import datetime, timezone, timedelta

logging.basicConfig(level=logging.INFO)

TOKEN   = os.environ.get('TELEGRAM_TOKEN', '')
CHAT_ID = int(os.environ.get('TELEGRAM_CHAT_ID', '0'))
API     = 'https://api.telegram.org/bot' + TOKEN

TRACK_LIST = []
INTRADAY_STATE = {}
STOCK_NAMES = {}

# 偽裝成一般使用者的瀏覽器標頭，並開啟 GZIP 壓縮大幅降低傳輸大小
HEADERS_WEB = {
    'User-Agent': 'Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/122.0.0.0 Safari/537.36',
    'Accept': 'application/json, text/javascript, */*; q=0.01',
    'Accept-Encoding': 'gzip, deflate, br',
    'Referer': 'https://www.twse.com.tw/',
    'X-Requested-With': 'XMLHttpRequest'
}

def get_proxy_urls(target_url):
    """產生多條代理跳板路徑，對抗 IP 封鎖"""
    encoded = urllib.parse.quote(target_url, safe='')
    return [
        target_url, # 1. 嘗試直連
        f"https://api.allorigins.win/raw?url={encoded}", # 2. AllOrigins
        f"https://corsproxy.io/?{encoded}",              # 3. CorsProxy
        f"https://api.codetabs.com/v1/proxy?quest={encoded}" # 4. CodeTabs
    ]

def update_stock_names():
    global STOCK_NAMES
    logging.info("正在更新全台股票名稱清單...")
    
    try:
        urls = get_proxy_urls('https://openapi.twse.com.tw/v1/exchangeReport/STOCK_DAY_ALL')
        for url in urls:
            try:
                r = requests.get(url, headers=HEADERS_WEB, timeout=15)
                if r.status_code == 200:
                    for item in r.json():
                        STOCK_NAMES[str(item.get('Code', ''))] = item.get('Name', '')
                    if STOCK_NAMES: break
            except: pass
    except: pass

    try:
        urls = get_proxy_urls('https://www.tpex.org.tw/openapi/v1/t187ap03_L')
        for url in urls:
            try:
                r = requests.get(url, headers=HEADERS_WEB, timeout=15)
                if r.status_code == 200:
                    for item in r.json():
                        STOCK_NAMES[str(item.get('SecuritiesCompanyCode', ''))] = item.get('CompanyName', '')
                    if STOCK_NAMES: break
            except: pass
    except: pass

def fetch_bulk_closing_prices():
    prices = {}
    try:
        for url in get_proxy_urls('https://openapi.twse.com.tw/v1/exchangeReport/STOCK_DAY_ALL'):
            try:
                r1 = requests.get(url, headers=HEADERS_WEB, timeout=15)
                if r1.status_code == 200:
                    for item in r1.json():
                        prices[item.get('Code', '')] = item.get('ClosingPrice', '')
                    break
            except: pass
    except: pass
    
    try:
        for url in get_proxy_urls('https://www.tpex.org.tw/openapi/v1/t187ap03_L'):
            try:
                r2 = requests.get(url, headers=HEADERS_WEB, timeout=15)
                if r2.status_code == 200:
                    for item in r2.json():
                        prices[item.get('SecuritiesCompanyCode', '')] = item.get('ClosingPrice', '')
                    break
            except: pass
    except: pass
    return prices

def send(chat_id, text):
    try:
        requests.post(API + '/sendMessage', json={'chat_id': chat_id, 'text': text}, timeout=10)
    except Exception as e:
        logging.error('Telegram send error: ' + str(e))

def get_stock_data(symbol):
    if symbol == '^TWII':
        urls = [f"https://query1.finance.yahoo.com/v8/finance/chart/^TWII?interval=1m&range=1d"]
    else:
        urls = [f"https://query1.finance.yahoo.com/v8/finance/chart/{symbol}{suffix}?interval=1m&range=1d" for suffix in ['.TW', '.TWO']]
    
    for url in urls:
        try:
            r = requests.get(url, headers=HEADERS_WEB, timeout=5)
            if r.status_code == 200:
                data = r.json()
                result = data.get('chart', {}).get('result', [])
                if result and result[0].get('timestamp'):
                    return result[0]
        except:
            pass
    return None

def get_last_trading_date():
    tw_time = datetime.now(timezone(timedelta(hours=8)))
    trade_time = tw_time
    if trade_time.hour < 15:
        trade_time -= timedelta(days=1)
    while trade_time.weekday() > 4: 
        trade_time -= timedelta(days=1)
    return trade_time

def fetch_twse_rwd_api(url_template, date_obj, max_attempts=5):
    """加入了多重跳板的證交所連線機制"""
    current_date = date_obj
    last_error = "連線失敗"
    
    for _ in range(max_attempts):
        date_str = current_date.strftime('%Y%m%d')
        target_url = url_template.format(date_str)
        
        # 輪詢跳板 (超時時間加長至 25 秒，確保代理伺服器有時間下載)
        for url in get_proxy_urls(target_url):
            try:
                r = requests.get(url, headers=HEADERS_WEB, timeout=25)
                if r.status_code == 200:
                    data = r.json()
                    if data.get('stat') == 'OK':
                        return data, current_date, "OK"
            except:
                continue 
                
        # 若當天無資料或跳板全失敗，往前推一天
        current_date -= timedelta(days=1)
        while current_date.weekday() > 4:
            current_date -= timedelta(days=1)
        time.sleep(1) 
        
    return None, None, "海外IP遭封鎖且所有跳板失效"

def generate_post_market_msg():
    msgs = []
    tw_time = datetime.now(timezone(timedelta(hours=8)))
    trade_time = get_last_trading_date()

    msgs.append(f"📊 【盤後籌碼總結】 查詢時間: {tw_time.strftime('%m-%d %H:%M')}")
    msgs.append("-------------------------")

    # 1. 三大法人買賣超金額
    url_bfi = "https://www.twse.com.tw/rwd/zh/fund/BFI82U?date={}&response=json"
    data_bfi, actual_date, err_bfi = fetch_twse_rwd_api(url_bfi, trade_time)
    
    if data_bfi:
        try:
            f_net, i_net, d_net = 0.0, 0.0, 0.0
            idx_name = data_bfi['fields'].index('單位名稱')
            idx_diff = data_bfi['fields'].index('買賣差額')
            for row in data_bfi['data']:
                name = row[idx_name]
                amt = float(row[idx_diff].replace(',', ''))
                if '外資' in name: f_net += amt
                elif '投信' in name: i_net += amt
                elif '自營' in name: d_net += amt
            total_net = f_net + i_net + d_net
            date_display = actual_date.strftime('%Y-%m-%d')
            msgs.append(f"💰 三大法人買賣超 ({date_display}): {total_net/1e8:+.2f} 億")
            msgs.append(f"   外資: {f_net/1e8:+.2f} 億")
            msgs.append(f"   投信: {i_net/1e8:+.2f} 億")
            msgs.append(f"   自營商: {d_net/1e8:+.2f} 億")
            trade_time = actual_date 
        except Exception:
            msgs.append("⚠️ 三大法人金額: 解析錯誤")
    else:
        msgs.append(f"⚠️ 三大法人金額: 失敗 ({err_bfi})")

    msgs.append("-------------------------")

    # 2. 外資期貨未平倉 (加強版：嘗試 TX 與 TXF 兩種代號，並加入代理跳板)
    try:
        start_date = (tw_time - timedelta(days=30)).strftime('%Y-%m-%d')
        fi_data = []
        
        for data_id in ['TX', 'TXF']:
            target_fm_url = f"https://api.finmindtrade.com/api/v4/data?dataset=TaiwanFuturesInstitutionalInvestors&data_id={data_id}&start_date={start_date}"
            for url in get_proxy_urls(target_fm_url):
                try:
                    r = requests.get(url, headers=HEADERS_WEB, timeout=15)
                    if r.status_code == 200:
                        json_resp = r.json()
                        if json_resp.get('status') == 200:
                            data = json_resp.get('data', [])
                            # 篩選外資
                            current_fi = [d for d in data if '外資' in d.get('name', '')]
                            if current_fi:
                                fi_data = current_fi
                                break
                except: pass
            if fi_data: break # 成功抓到就不再試下一個代號
            
        if len(fi_data) >= 2:
            fi_data.sort(key=lambda x: x['date']) 
            recent_date = fi_data[-1].get('date', '')
            today_oi = fi_data[-1].get('long_short_oi_net_volume', 0)
            yest_oi = fi_data[-2].get('long_short_oi_net_volume', 0)
            diff = today_oi - yest_oi
            msgs.append(f"📈 外資台指淨未平倉 ({recent_date}):")
            msgs.append(f"   {today_oi:,} 口 (較前日 {diff:+,} 口)")
        else:
            msgs.append("📈 外資台指淨未平倉: 近30日無足夠資料 (可能達免費API次數上限)")
    except Exception:
        msgs.append("⚠️ 期貨未平倉: 抓取發生錯誤")

    msgs.append("-------------------------")

    # 3. 三大法人買賣超前20名 (關鍵修正：ALLBUT0999 剔除權證，大幅降低檔案大小防斷線)
    url_t86 = "https://www.twse.com.tw/rwd/zh/fund/T86?date={}&selectType=ALLBUT0999&response=json"
    data_t86, _, err_t86 = fetch_twse_rwd_api(url_t86, trade_time)
    
    if data_t86:
        try:
            idx_code = data_t86['fields'].index('證券代號')
            idx_name = data_t86['fields'].index('證券名稱')
            idx_diff = -1
            for i, field in enumerate(data_t86['fields']):
                if '三大法人買賣超股數' in field:
                    idx_diff = i; break
                    
            if idx_diff != -1:
                parsed_data = []
                for row in data_t86['data']:
                    code = row[idx_code]
                    name = row[idx_name].strip()
                    diff_str = row[idx_diff].replace(',', '')
                    try:
                        parsed_data.append({'Code': code, 'Name': name, 'Diff_int': int(diff_str)})
                    except: pass
                
                sorted_data = sorted(parsed_data, key=lambda x: x['Diff_int'], reverse=True)
                top_buy = sorted_data[:20]
                top_sell = sorted_data[-20:]
                top_sell.reverse()
                
                bulk_prices = fetch_bulk_closing_prices()

                def format_stock_list(stock_list, is_buy):
                    lines = []
                    for idx, item in enumerate(stock_list):
                        sym = item.get('Code', '')
                        name = item.get('Name', '').strip()
                        lots = abs(item.get('Diff_int', 0)) // 1000 
                        price_str = bulk_prices.get(sym, "")
                        price_disp = f"({price_str})" if price_str else "(無報價)"
                        action = "買" if is_buy else "賣"
                        lines.append(f"{idx+1}. {sym} {name} {price_disp} | {action} {lots:,} 張")
                    return lines

                msgs.append("🔥 【法人買超前 20 名】")
                msgs.extend(format_stock_list(top_buy, True))
                msgs.append("-------------------------")
                msgs.append("🩸 【法人賣超前 20 名】")
                msgs.extend(format_stock_list(top_sell, False))
            else:
                msgs.append("⚠️ 買賣超排行: 找不到對應欄位")
        except Exception:
            msgs.append("⚠️ 買賣超排行: 解析錯誤")
    else:
        msgs.append(f"⚠️ 買賣超排行: 失敗 ({err_t86})")

    return "\n".join(msgs)

def post_market_job(chat_id):
    try:
        send(chat_id, "⏳ 正在透過全球代理伺服器極速抓取籌碼資料，請稍候...")
        msg = generate_post_market_msg()
        if len(msg) > 4000:
            send(chat_id, msg[:4000])
            send(chat_id, msg[4000:])
        else:
            send(chat_id, msg)
    except Exception as e:
        send(chat_id, f"❌ 系統發生預期外錯誤: {str(e)[:50]}")

def analyze_and_alert(symbol, tw_time):
    data = get_stock_data(symbol)
    if not data: return

    if not STOCK_NAMES: update_stock_names()
    name = STOCK_NAMES.get(symbol, '')
    disp_sym = f"{symbol} {name}".strip()

    timestamps = data['timestamp']
    indicators = data['indicators']['quote'][0]
    opens = indicators['open']
    highs = indicators['high']
    closes = indicators['close']
    volumes = indicators['volume']
    
    if symbol not in INTRADAY_STATE:
        INTRADAY_STATE[symbol] = {
            'reported_open': False, 'reported_0920': False, 'reported_close': False,
            'last_30m_report': None, 'day_high': 0, 'max_vol': 0,
            'open_price': opens[0] if opens else 0
        }
    
    state = INTRADAY_STATE[symbol]
    current_time_str = tw_time.strftime('%H:%M')
    current_minute = tw_time.minute
    
    if not timestamps or not closes[-1]: return

    current_price = closes[-1]
    current_vol = volumes[-1] if volumes[-1] is not None else 0
    alerts = []

    if current_time_str >= '09:00' and not state['reported_open']:
        alerts.append(f"🎯 【開盤】{disp_sym} 開盤價: {state['open_price']:.2f}")
        state['reported_open'] = True
        state['day_high'] = highs[0] if highs[0] else current_price
        state['max_vol'] = volumes[0] if volumes[0] else 0

    if current_time_str >= '09:20' and not state['reported_0920']:
        max_v, max_v_price = 0, 0
        for i, ts in enumerate(timestamps):
            dt = datetime.fromtimestamp(ts, timezone(timedelta(hours=8)))
            if '09:01' <= dt.strftime('%H:%M') <= '09:20':
                vol = volumes[i] if volumes[i] else 0
                if vol > max_v:
                    max_v = vol
                    max_v_price = closes[i]
        if max_v > 0:
            alerts.append(f"📊 【09:20 結算】{disp_sym} 早盤最大量價格: {max_v_price:.2f} (單分鐘量: {max_v})")
        state['reported_0920'] = True

    if current_minute in [0, 30] and '09:30' <= current_time_str <= '13:00':
        if state['last_30m_report'] != current_time_str:
            alerts.append(f"⏱ 【定時回報】{disp_sym} 目前股價: {current_price:.2f}")
            state['last_30m_report'] = current_time_str

    if current_price > state['day_high'] and current_time_str > '09:00':
        alerts.append(f"🔥 【創新高】{disp_sym} 突破本日新高價: {current_price:.2f}")
        state['day_high'] = current_price

    if current_vol > state['max_vol'] and current_time_str > '09:00':
        alerts.append(f"💥 【爆大量】{disp_sym} 出現單分鐘新天量: {current_vol} 張，股價: {current_price:.2f}")
        state['max_vol'] = current_vol

    if current_time_str >= '13:30' and not state['reported_close']:
        alerts.append(f"🏁 【收盤】{disp_sym} 收盤價: {current_price:.2f}")
        state['reported_close'] = True

    if alerts:
        send(CHAT_ID, f"[{current_time_str}]\n" + "\n".join(alerts))

def handle(update):
    msg = update.get('message', {})
    chat_id = msg.get('chat', {}).get('id')
    text = msg.get('text', '')
    if chat_id != CHAT_ID: return
        
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
        if TRACK_LIST: send(chat_id, f"📋 目前追蹤清單：{', '.join(TRACK_LIST)}")
        else: send(chat_id, "目前沒有追蹤任何股票。請使用 /track 新增。")
            
    elif text.startswith('/clear'):
        TRACK_LIST.clear()
        INTRADAY_STATE.clear()
        send(chat_id, "🗑 追蹤清單已清空。")

    elif text.startswith('/postmarket'):
        threading.Thread(target=post_market_job, args=(chat_id,), daemon=True).start()

    elif text.startswith('/price'):
        if not STOCK_NAMES: update_stock_names()
        parts = text.strip().split()
        
        if len(parts) > 1: symbols_to_check = parts[1:]
        else:
            if not TRACK_LIST:
                send(chat_id, "目前沒有追蹤清單。請使用 /track 新增，或輸入 /price [代號] 查詢。")
                return
            symbols_to_check = TRACK_LIST
            
        send(chat_id, "🔍 正在查詢即時股價，請稍候...")
        alerts = []
        tw_time = datetime.now(timezone(timedelta(hours=8)))
        time_str = tw_time.strftime('%H:%M')
        
        twii_data = get_stock_data('^TWII')
        if twii_data and twii_data.get('indicators', {}).get('quote', []):
            meta = twii_data.get('meta', {})
            prev_close = meta.get('chartPreviousClose') or meta.get('previousClose')
            closes = twii_data['indicators']['quote'][0].get('close', [])
            valid_closes = [c for c in closes if c is not None]
            
            if valid_closes and prev_close:
                curr = valid_closes[-1]
                chg = curr - prev_close
                pct = (chg / prev_close) * 100
                alerts.append(f"📈 加權指數: {curr:.2f} ({chg:+.2f}, {pct:+.2f}%)")
                alerts.append("-------------------------")
        
        for sym in symbols_to_check:
            name = STOCK_NAMES.get(sym, '')
            disp_sym = f"{sym} {name}".strip()
            data = get_stock_data(sym)
            if data and data.get('indicators', {}).get('quote', []):
                meta = data.get('meta', {})
                prev_close = meta.get('chartPreviousClose') or meta.get('previousClose')
                closes = data['indicators']['quote'][0].get('close', [])
                valid_closes = [c for c in closes if c is not None]
                if valid_closes:
                    current_price = valid_closes[-1]
                    if prev_close:
                        chg = current_price - prev_close
                        pct = (chg / prev_close) * 100
                        alerts.append(f"📌 {disp_sym}: {current_price:.2f} ({chg:+.2f}, {pct:+.2f}%)")
                    else:
                        alerts.append(f"📌 {disp_sym}: {current_price:.2f}")
                else:
                    alerts.append(f"⚠️ {disp_sym}: 暫無今日報價資料")
            else:
                alerts.append(f"❌ {disp_sym}: 查詢失敗 (請確認代號正確)")
                
        if alerts: send(chat_id, f"[{time_str} 即時報價]\n" + "\n".join(alerts))

def polling_loop():
    offset = 0
    logging.info('Telegram Polling started')
    while True:
        try:
            r = requests.get(API + '/getUpdates', params={'offset': offset, 'timeout': 30}, timeout=35)
            updates = r.json().get('result', [])
            for u in updates:
                offset = u['update_id'] + 1
                handle(u)
        except Exception as e:
            logging.error('Polling error: ' + str(e))
            time.sleep(5)

def market_monitor_loop():
    logging.info('Market Monitor started')
    while True:
        tw_time = datetime.now(timezone(timedelta(hours=8)))
        time_str = tw_time.strftime('%H:%M')
        
        if time_str == '08:30' and tw_time.second == 0:
            INTRADAY_STATE.clear()
            update_stock_names()
            time.sleep(1)
            
        if time_str == '16:30' and tw_time.second == 0:
            if tw_time.weekday() < 5:
                threading.Thread(target=post_market_job, args=(CHAT_ID,), daemon=True).start()
            time.sleep(1)
            
        if TRACK_LIST and '09:00' <= time_str <= '13:35' and tw_time.weekday() < 5:
            for symbol in TRACK_LIST:
                analyze_and_alert(symbol, tw_time)
                time.sleep(1)
                
        secs_to_next_minute = 60 - datetime.now(timezone(timedelta(hours=8))).second
        time.sleep(secs_to_next_minute)

if __name__ == '__main__':
    logging.info('twstock_alert1124_bot started!')
    update_stock_names()
    t = threading.Thread(target=market_monitor_loop, daemon=True)
    t.start()
    polling_loop()
