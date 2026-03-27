import os, json, logging, time, threading, requests
from datetime import datetime, timezone, timedelta

logging.basicConfig(level=logging.INFO)

TOKEN   = os.environ.get('TELEGRAM_TOKEN', '')
CHAT_ID = int(os.environ.get('TELEGRAM_CHAT_ID', '0'))
API     = 'https://api.telegram.org/bot' + TOKEN

# 儲存要追蹤的股票代號
TRACK_LIST = []
# 記錄每檔股票當日的狀態
INTRADAY_STATE = {}
# 儲存全台股票代號與名稱的對應表
STOCK_NAMES = {}

def update_stock_names():
    """獲取最新的股票名稱對應表 (加入多重來源與海外 IP 防阻擋機制)"""
    global STOCK_NAMES
    headers = {'User-Agent': 'Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36'}
    logging.info("正在更新全台股票名稱清單...")
    
    # 優先來源: FinMind (對海外雲端 IP 較友善)
    try:
        r = requests.get("https://api.finmindtrade.com/api/v4/data?dataset=TaiwanStockInfo", headers=headers, timeout=10)
        if r.status_code == 200:
            data = r.json().get('data', [])
            for item in data:
                STOCK_NAMES[str(item.get('stock_id', ''))] = item.get('stock_name', '')
            if STOCK_NAMES:
                logging.info(f"成功從 FinMind 取得 {len(STOCK_NAMES)} 檔股票名稱。")
                return
    except Exception as e:
        logging.error(f"FinMind API 取得失敗: {e}")

    # 備用來源: 台灣證交所與櫃買中心 Open API
    try:
        r_twse = requests.get('https://openapi.twse.com.tw/v1/exchangeReport/STOCK_DAY_ALL', headers=headers, timeout=10)
        if r_twse.status_code == 200:
            for item in r_twse.json():
                STOCK_NAMES[str(item.get('Code', ''))] = item.get('Name', '')
                
        r_tpex = requests.get('https://www.tpex.org.tw/openapi/v1/t187ap03_L', headers=headers, timeout=10)
        if r_tpex.status_code == 200:
            for item in r_tpex.json():
                STOCK_NAMES[str(item.get('SecuritiesCompanyCode', ''))] = item.get('CompanyName', '')
                
        logging.info("成功從政府 Open API 取得股票名稱。")
    except Exception as e:
        logging.error(f"政府 Open API 取得失敗: {e}")

def send(chat_id, text):
    try:
        requests.post(API + '/sendMessage',
            json={'chat_id': chat_id, 'text': text}, timeout=10)
    except Exception as e:
        logging.error('Telegram send error: ' + str(e))

def get_stock_data(symbol):
    """從 Yahoo Finance 抓取當日 1 分鐘 K 線資料"""
    headers = {'User-Agent': 'Mozilla/5.0 (Windows NT 10.0; Win64; x64)'}
    
    if symbol == '^TWII':
        urls = [f"https://query1.finance.yahoo.com/v8/finance/chart/^TWII?interval=1m&range=1d"]
    else:
        urls = [f"https://query1.finance.yahoo.com/v8/finance/chart/{symbol}{suffix}?interval=1m&range=1d" for suffix in ['.TW', '.TWO']]
    
    for url in urls:
        try:
            r = requests.get(url, headers=headers, timeout=5)
            if r.status_code == 200:
                data = r.json()
                result = data.get('chart', {}).get('result', [])
                if result and result[0].get('timestamp'):
                    return result[0]
        except Exception as e:
            logging.error(f"Error fetching {symbol}: {e}")
    return None

def analyze_and_alert(symbol, tw_time):
    """分析單檔股票並判斷是否需要推播"""
    data = get_stock_data(symbol)
    if not data:
        return

    # 確保有名字庫，若無則即時抓取
    if not STOCK_NAMES:
        update_stock_names()

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
            'reported_open': False,
            'reported_0920': False,
            'reported_close': False,
            'last_30m_report': None,
            'day_high': 0,
            'max_vol': 0,
            'open_price': opens[0] if opens else 0
        }
    
    state = INTRADAY_STATE[symbol]
    current_time_str = tw_time.strftime('%H:%M')
    current_minute = tw_time.minute
    
    if not timestamps or not closes[-1]:
        return

    current_price = closes[-1]
    current_vol = volumes[-1] if volumes[-1] is not None else 0

    alerts = []

    if current_time_str >= '09:00' and not state['reported_open']:
        alerts.append(f"🎯 【開盤】{disp_sym} 開盤價: {state['open_price']:.2f}")
        state['reported_open'] = True
        state['day_high'] = highs[0] if highs[0] else current_price
        state['max_vol'] = volumes[0] if volumes[0] else 0

    if current_time_str >= '09:20' and not state['reported_0920']:
        max_v = 0
        max_v_price = 0
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
        msg = f"[{current_time_str}]\n" + "\n".join(alerts)
        send(CHAT_ID, msg)

def handle(update):
    msg = update.get('message', {})
    chat_id = msg.get('chat', {}).get('id')
    text = msg.get('text', '')
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

    elif text.startswith('/price'):
        # 如果名字庫是空的，立刻補抓一次
        if not STOCK_NAMES:
            update_stock_names()
            
        parts = text.strip().split()
        symbols_to_check = []
        
        if len(parts) > 1:
            symbols_to_check = parts[1:]
        else:
            if not TRACK_LIST:
                send(chat_id, "目前沒有追蹤清單。請使用 /track 新增，或輸入 /price [代號] 查詢。")
                return
            symbols_to_check = TRACK_LIST
            
        send(chat_id, "🔍 正在查詢即時股價，請稍候...")
        alerts = []
        tw_time = datetime.now(timezone(timedelta(hours=8)))
        time_str = tw_time.strftime('%H:%M')
        
        # --- 1. 抓取大盤加權指數 ---
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
        
        # --- 2. 抓取個股資料 ---
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
                
        if alerts:
            msg_text = f"[{time_str} 即時報價]\n" + "\n".join(alerts)
            send(chat_id, msg_text)

def polling_loop():
    offset = 0
    logging.info('Telegram Polling started')
    while True:
        try:
            r = requests.get(API + '/getUpdates',
                params={'offset': offset, 'timeout': 30},
                timeout=35)
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
            logging.info("Intraday state cleared and stock names updated.")
            time.sleep(1)
            
        is_trading_hours = '09:00' <= time_str <= '13:35'
        is_weekday = tw_time.weekday() < 5
        
        if TRACK_LIST and is_trading_hours and is_weekday:
            for symbol in TRACK_LIST:
                analyze_and_alert(symbol, tw_time)
                time.sleep(1)
                
        secs_to_next_minute = 60 - datetime.now(timezone(timedelta(hours=8))).second
        time.sleep(secs_to_next_minute)

if __name__ == '__main__':
    logging.info('twstock_alert1124_bot started!')
    # 程式剛啟動時，先抓取一次全台股票名稱
    update_stock_names()
    
    t = threading.Thread(target=market_monitor_loop, daemon=True)
    t.start()
    polling_loop()
