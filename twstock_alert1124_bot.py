FINMIND_TOKEN = os.environ.get('FINMIND_TOKEN', '')

def fetch_finmind_institutional(trade_date_str):
    """
    一次抓取全市場個股三大法人資料
    回傳: (全市場加總dict, 個股list)
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
        return None, None, f"連線異常: {str(e)[:30]}"


def generate_post_market_msg():
    msgs = []
    tw_time = datetime.now(timezone(timedelta(hours=8)))
    trade_time = get_last_trading_date()
    trade_date_str = trade_time.strftime('%Y-%m-%d')

    msgs.append(f"📊 【盤後籌碼總結】 查詢時間: {tw_time.strftime('%m-%d %H:%M')}")
    msgs.append("-------------------------")

    # 一次抓取所有個股三大法人資料
    inst_data, _, err = fetch_finmind_institutional(trade_date_str)

    # === 1. 三大法人金額加總 ===
    if inst_data:
        f_net = i_net = d_net = 0.0
        for row in inst_data:
            name = row.get('name', '')
            buy = float(row.get('buy', 0) or 0)
            sell = float(row.get('sell', 0) or 0)
            net = buy - sell
            if '外資' in name:
                f_net += net
            elif '投信' in name:
                i_net += net
            elif '自營' in name:
                d_net += net
        total_net = f_net + i_net + d_net
        # FinMind 單位是「股」，換算成億元需要搭配股價，這裡改顯示「張」
        msgs.append(f"💰 三大法人買賣超 ({trade_date_str}):")
        msgs.append(f"   合計: {total_net/1000:+,.0f} 張")
        msgs.append(f"   外資: {f_net/1000:+,.0f} 張")
        msgs.append(f"   投信: {i_net/1000:+,.0f} 張")
        msgs.append(f"   自營商: {d_net/1000:+,.0f} 張")
    else:
        msgs.append(f"⚠️ 三大法人金額: 失敗 ({err})")

    msgs.append("-------------------------")

    # === 2. 外資期貨未平倉（加上 token）===
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
            data = r.json().get('data', [])
            fi_data = [d for d in data if '外資' in d.get('name', '')]
            if len(fi_data) >= 2:
                fi_data.sort(key=lambda x: x['date'])
                recent_date = fi_data[-1].get('date', '')
                today_oi = fi_data[-1].get('long_short_oi_net_volume', 0)
                yest_oi = fi_data[-2].get('long_short_oi_net_volume', 0)
                diff = today_oi - yest_oi
                msgs.append(f"📈 外資台指淨未平倉 ({recent_date}):")
                msgs.append(f"   {today_oi:,} 口 (較前日 {diff:+,} 口)")
            else:
                msgs.append("📈 外資台指淨未平倉: 資料不足")
        else:
            msgs.append(f"⚠️ 期貨未平倉: HTTP {r.status_code}")
    except Exception as e:
        msgs.append(f"⚠️ 期貨未平倉: {str(e)[:30]}")

    msgs.append("-------------------------")

    # === 3. 個股買賣超前20名 ===
    if inst_data:
        # 按個股加總三大法人
        stock_net = {}
        stock_name_map = {}
        for row in inst_data:
            code = row.get('stock_id', '')
            buy = float(row.get('buy', 0) or 0)
            sell = float(row.get('sell', 0) or 0)
            stock_net[code] = stock_net.get(code, 0) + (buy - sell)

        bulk_prices = fetch_bulk_closing_prices()
        sorted_stocks = sorted(stock_net.items(), key=lambda x: x[1], reverse=True)

        def format_stock_list(stock_list, is_buy):
            lines = []
            for idx, (code, net_shares) in enumerate(stock_list):
                name = STOCK_NAMES.get(code, '')
                lots = abs(int(net_shares)) // 1000
                price = bulk_prices.get(code, '')
                price_disp = f"({price})" if price else "(無報價)"
                action = "買" if is_buy else "賣"
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
