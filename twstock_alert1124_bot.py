import os, json, logging, time, threading, requests
from datetime import datetime, timezone, timedelta

logging.basicConfig(level=logging.INFO)

TOKEN   = os.environ.get('TELEGRAM_TOKEN', '')
CHAT_ID = int(os.environ.get('TELEGRAM_CHAT_ID', '0'))
API     = 'https://api.telegram.org/bot' + TOKEN

# 儲存要追蹤的股票代號
TRACK_LIST = []
# 記錄每檔股票當日的狀態 (最高價、最大量、已觸發的推播等)
INTRADAY_STATE = {}

def send(chat_id, text):
    try:
        requests.post(API + '/sendMessage',
            json={'chat_id': chat_id, 'text': text}, timeout=10)
    except Exception as e:
        logging.error('Telegram send error: ' + str(e))

def get_stock_data(symbol):
    """
    從 Yahoo Finance 抓取當日 1 分鐘 K 線資料
    自動嘗試 .TW (上市) 與 .TWO (上櫃)，並特別處理加權指數 ^TWII
    """
    headers = {'User-Agent': 'Mozilla/5.0 (Windows NT 10.0; Win64; x64)'}
    
    # 判斷是否為大盤加權指數
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

    # 解析 Yahoo Finance 資料結構
    timestamps = data['timestamp']
    indicators = data['indicators']['quote'][0]
    opens = indicators['open']
    highs = indicators['high']
    closes = indicators['close']
    volumes = indicators['volume']
    
    # 初始化當日狀態
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

    # 1. 09:00 開盤價推播
    if current_time_str >= '09:00' and not state['reported_open']:
        alerts.append(f"🎯 【開盤】{symbol} 開盤價: {state['open_price']:.2f}")
        state['reported_open'] = True
        state['day_high'] = highs[0] if highs[0] else current_price
        state['max_vol'] = volumes[0] if volumes[0] else 0

    # 2. 09:20 最大交易量價格 (排除開盤第一分鐘)
    if current_time_str >= '09:20' and not state['reported_0920']:
        max_v = 0
        max_v_price = 0
        # 尋找 09:01 ~ 09:20 之間的最大量
        for i, ts in enumerate(timestamps):
            dt = datetime.fromtimestamp(ts, timezone(timedelta(hours=8)))
            if '09:01' <= dt.strftime('%H:%M') <= '09:20':
                vol = volumes[i] if volumes[i] else 0
                if vol > max_v:
                    max_v = vol
                    max_v_price = closes[i]
        
        if max_v > 0:
            alerts.append(f"📊 【09:20 結算】{symbol} 早盤最大量價格: {max_v_price:.2f} (單分鐘量: {max_v})")
        state['reported_0920'] = True

    # 3. 每 30 分鐘定期推播 (09:30, 10:00, 10:30 ... 13:00)
    if current_minute in [0, 30] and '09:30' <= current_time_str <= '13:00':
        if state['last_30m_report'] != current_time_str:
            alerts.append(f"⏱ 【定時回報】{symbol} 目前股價: {current_price:.2f}")
            state['last_30m_report'] = current_time_str

    # 4. 盤中創新高價
    if current_price > state['day_high'] and current_time_str > '09:00':
        alerts.append(f"🔥 【創新高】{symbol} 突破本日新高價: {current_price:.2f}")
        state['day_high'] = current_price

    # 5. 盤中創新爆量 (單分鐘成交量大於今日最高單分鐘量，排除開盤量)
    if current_vol > state['max_vol'] and current_time_str > '09:00':
        alerts.append(f"💥 【爆大量】{symbol} 出現單分鐘新天量: {current_vol} 張，股價: {current_price:.2f}")
        state['max_vol'] = current_vol

    # 6. 13:30 收盤價
    if current_time_str >= '13:30' and not state['reported_close']:
        alerts.append(f"🏁 【收盤】{symbol} 收盤價: {current_price:.2f}")
        state['reported_close'] = True

    # 如果有任何警報，發送推播
    if alerts:
        msg = f"[{current_time_str}]\n" + "\n".join(alerts)
        send(CHAT_ID, msg)

def handle(update):
    msg = update.get('message', {})
    chat_id = msg.get('chat', {}).get('id')
    text = msg.get('text', '')
    if chat_id != CHAT_ID:
        return
        
    # 指令：設定今日追蹤清單 (例如: /track 2330 2603)
    if text.startswith('/track'):
        parts = text.strip().split()
        if len(parts) > 1:
            global TRACK_LIST
            # 限制最多 20 檔
            TRACK_LIST = parts[1:21] 
            INTRADAY_STATE.clear() # 清空昨天的狀態
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

    # 新增指令：主動查詢即時股價 (例如: /price 或 /price 2330)
    elif text.startswith('/price'):
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
        
        # --- 1. 優先抓取大盤加權指數 ---
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
                        alerts.append(f"📌 {sym}: {current_price:.2f} ({chg:+.2f}, {pct:+.2f}%)")
                    else:
                        alerts.append(f"📌 {sym}: {current_price:.2f}")
                else:
                    alerts.append(f"⚠️ {sym}: 暫無今日報價資料")
            else:
                alerts.append(f"❌ {sym}: 查詢失敗 (請確認代號正確)")
                
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
        
        # 每天早上 08:30 自動清空昨天的盤中紀錄狀態，準備迎接新的一天
        if time_str == '08:30' and tw_time.second == 0:
            INTRADAY_STATE.clear()
            logging.info("Intraday state cleared for the new day.")
            time.sleep(1)
            
        # 判斷是否為交易時間 (09:00 ~ 13:35) 且為平日
        is_trading_hours = '09:00' <= time_str <= '13:35'
        is_weekday = tw_time.weekday() < 5
        
        if TRACK_LIST and is_trading_hours and is_weekday:
            for symbol in TRACK_LIST:
                analyze_and_alert(symbol, tw_time)
                # 稍微暫停避免頻繁呼叫 API 被封鎖
                time.sleep(1)
                
        # 每分鐘執行一次檢查
        secs_to_next_minute = 60 - datetime.now(timezone(timedelta(hours=8))).second
        time.sleep(secs_to_next_minute)

if __name__ == '__main__':
    logging.info('twstock_alert1124_bot started!')
    t = threading.Thread(target=market_monitor_loop, daemon=True)
    t.start()
    polling_loop()
