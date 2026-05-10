import os
import sqlite3
import urllib.request
import json
import re
import pandas as pd
from datetime import datetime, timedelta

DB_PATH = str(os.path.join(os.path.dirname(os.path.abspath(__file__)), "stock_cache.db"))

def get_db_connection():
    """建立 SQLite 連線，並啟用 WAL 模式與設定 60 秒 Timeout，徹底防止 database is locked 錯誤"""
    conn = sqlite3.connect(DB_PATH, timeout=60.0)
    try:
        conn.execute("PRAGMA journal_mode=WAL;")
    except Exception as eWAL:
        print(f"[Screener] Enable WAL Fail: {eWAL}")
    return conn

# 台灣前 150 檔主力高流動性與市值成分股（台灣50 + 中型100 + 人氣熱門股）
DEFAULT_STOCKS = [
    "2330", "2317", "2454", "2308", "2382", "2303", "2881", "2882", "2891", "1301",
    "1303", "1326", "2886", "2002", "3711", "2412", "2357", "2324", "2353", "2352",
    "3231", "6669", "2379", "3034", "3037", "3045", "4961", "2408", "2344", "2409",
    "3481", "2603", "2609", "2615", "2610", "2618", "2327", "2345", "1101", "1102",
    "1216", "1402", "2105", "2201", "2207", "2395", "2356", "2354", "2376", "2377",
    "2392", "2449", "2451", "2458", "2474", "3005", "3017", "3023", "3035", "3443",
    "3532", "3702", "3706", "4938", "5269", "6213", "6239", "6415", "8046", "8215",
    "9921", "9945", "2637", "2801", "2809", "2812", "2834", "2880", "2883", "2884",
    "2885", "2887", "2888", "2889", "2890", "2892", "5871", "5876", "5880", "6005",
    "9904", "1513", "1519", "1503", "1504", "1514", "1609", "1605", "1722", "1723",
    "1802", "2103", "2313", "2323", "2360", "2363", "2368", "2383", "2401", "2420",
    "2439", "2457", "2498", "2515", "2542", "2605", "2606", "2845", "2903", "2912",
    "3004", "3029", "3044", "3189", "3576", "3653", "3704", "4919", "5483", "6147",
    "6182", "6488", "8069", "8299", "3008", "2337", "3376", "3406", "3596", "2455",
    "3130", "5272", "6116", "6269", "6271", "6278", "8050", "1795", "4147", "4174"
]

STOCK_NAMES = {
    "2330": "台積電", "2317": "鴻海", "2454": "聯發科", "2308": "台達電", "2382": "廣達", "2303": "聯電", "2881": "富邦金", "2882": "國泰金", "2891": "中信金", "1301": "台塑",
    "1303": "南亞", "1326": "台化", "2886": "兆豐金", "2002": "中鋼", "3711": "日月光投控", "2412": "中華電", "2357": "華碩", "2324": "仁寶", "2353": "宏碁", "2352": "佳世達",
    "3231": "緯創", "6669": "緯穎", "2379": "瑞昱", "3034": "聯詠", "3037": "欣興", "3045": "台灣大", "4961": "天鈺", "2408": "南亞科", "2344": "華邦電", "2409": "友達",
    "3481": "群創", "2603": "長榮", "2609": "陽明", "2615": "萬海", "2610": "華航", "2618": "長榮航", "2327": "國巨", "2345": "智邦", "1101": "台泥", "1102": "亞泥",
    "1216": "統一", "1402": "遠東新", "2105": "建大", "2201": "裕隆", "2207": "和泰車", "2395": "研華", "2356": "英業達", "2354": "鴻準", "2376": "技嘉", "2377": "微星",
    "2392": "正崴", "2449": "京元電子", "2451": "創見", "2458": "義隆", "2474": "可成", "3005": "神基", "3017": "奇鋐", "3023": "信邦", "3035": "智原", "3443": "創意",
    "3532": "台胜科", "3702": "大聯大", "3706": "神達", "4938": "和碩", "5269": "祥碩", "6213": "聯茂", "6239": "力成", "6415": "矽力*-KY", "8046": "南電", "8215": "明基材",
    "9921": "巨大", "9945": "潤泰新", "2637": "慧洋-KY", "2801": "彰銀", "2809": "京城銀", "2812": "台中銀", "2834": "臺企銀", "2880": "華南金", "2883": "開發金", "2884": "玉山金",
    "2885": "元大金", "2887": "台新金", "2888": "新光金", "2889": "國票金", "2890": "永豐金", "2892": "第一金", "5871": "中租-KY", "5876": "上海商銀", "5880": "合庫金", "6005": "群益證",
    "9904": "寶成", "1513": "中興電", "1519": "華城", "1503": "士電", "1504": "東元", "1514": "亞力", "1609": "大亞", "1605": "華新", "1722": "台肥", "1723": "中碳",
    "1802": "台玻", "2103": "台橡", "2313": "華通", "2323": "中環", "2360": "致茂", "2363": "矽統", "2368": "金像電", "2383": "台光電", "2401": "凌陽", "2420": "固緯",
    "2439": "美律", "2457": "飛宏", "2498": "宏達電", "2515": "國產", "2542": "興富發", "2605": "新興", "2606": "裕民", "2845": "遠東銀", "2903": "遠百", "2912": "統一超",
    "3004": "豐達科", "3029": "零壹", "3044": "健鼎", "3189": "景碩", "3576": "聯合再生", "3653": "健策", "3704": "合勤控", "4919": "新唐", "5483": "中美晶", "6147": "頎邦",
    "6182": "合晶", "6488": "環球晶", "8069": "元太", "8299": "群聯", "3008": "大立光", "2337": "旺宏", "3376": "新日興", "3406": "玉晶光", "3596": "智易", "2455": "全新",
    "3130": "一零四", "5272": "笙科", "6116": "彩晶", "6269": "台郡", "6271": "同欣電", "6278": "台表科", "8050": "廣積", "1795": "美時", "4147": "中裕", "4174": "浩鼎"
}

def init_db():
    """初始化選股 SQLite 資料庫與建立索引，確保高併發查詢效能"""
    conn = get_db_connection()
    cursor = conn.cursor()
    
    # 建立日 K 棒表
    cursor.execute("""
        CREATE TABLE IF NOT EXISTS daily_kbars (
            code TEXT,
            date TEXT,
            open REAL,
            high REAL,
            low REAL,
            close REAL,
            volume INTEGER,
            PRIMARY KEY (code, date)
        )
    """)
    
    # 建立法人交易數據表
    cursor.execute("""
        CREATE TABLE IF NOT EXISTS institutional_trading (
            code TEXT,
            date TEXT,
            foreign_buy INTEGER,
            investment_buy INTEGER,
            dealer_buy INTEGER,
            PRIMARY KEY (code, date)
        )
    """)
    
    # 建立輿情熱度表
    cursor.execute("""
        CREATE TABLE IF NOT EXISTS social_sentiment (
            code TEXT,
            date TEXT,
            mention_count INTEGER,
            PRIMARY KEY (code, date)
        )
    """)
    
    # 建立索引以優化查詢
    cursor.execute("CREATE INDEX IF NOT EXISTS idx_kbars_date ON daily_kbars(date)")
    cursor.execute("CREATE INDEX IF NOT EXISTS idx_kbars_code ON daily_kbars(code)")
    cursor.execute("CREATE INDEX IF NOT EXISTS idx_inst_date ON institutional_trading(date)")
    cursor.execute("CREATE INDEX IF NOT EXISTS idx_inst_code ON institutional_trading(code)")
    
    conn.commit()
    conn.close()
    print("[Screener DB] Database initialized successfully.")

def fetch_twse_daily_quotes():
    """Step 3 資料：從證交所 + 櫃買中心取得今日所有股票的收盤價、成交金額、漲跌幅"""
    quotes = {}
    headers = {'User-Agent': 'Mozilla/5.0 (Windows NT 10.0; Win64; x64)'}

    # 上市 (TWSE)
    try:
        url = "https://www.twse.com.tw/rwd/zh/afterTrading/STOCK_DAY_ALL?response=json"
        req = urllib.request.Request(url, headers=headers)
        with urllib.request.urlopen(req, timeout=10) as r:
            raw = json.loads(r.read().decode('utf-8'))
        if raw.get('stat') == 'OK' and 'data' in raw:
            fields = raw.get('fields', [])
            code_i     = fields.index('證券代號')  if '證券代號'  in fields else 0
            close_i    = fields.index('收盤價')    if '收盤價'    in fields else 8
            turnover_i = fields.index('成交金額')  if '成交金額'  in fields else 4
            sign_i     = fields.index('漲跌(+/-)') if '漲跌(+/-)' in fields else 9
            change_i   = fields.index('漲跌價差')  if '漲跌價差'  in fields else 10
            for row in raw['data']:
                try:
                    code     = row[code_i].strip()
                    close    = float(row[close_i].replace(',', ''))
                    turnover = int(row[turnover_i].replace(',', ''))
                    sign     = row[sign_i].strip()
                    chg_abs  = float(row[change_i].replace(',', ''))
                    chg      = chg_abs if sign == '+' else (-chg_abs if sign == '-' else 0.0)
                    prev     = close - chg
                    chg_pct  = (chg / prev * 100) if prev > 0 else 0.0
                    quotes[code] = {'close': close, 'turnover': turnover, 'change_pct': chg_pct}
                except (ValueError, IndexError):
                    continue
    except Exception as e:
        print(f"[Screener] TWSE STOCK_DAY_ALL fetch failed: {e}")

    # 上櫃 (TPEx)
    try:
        url = "https://www.tpex.org.tw/web/stock/aftertrading/otc_quotes_no1430/stk_wn1430_result.php?l=zh-tw&o=json"
        req = urllib.request.Request(url, headers=headers)
        with urllib.request.urlopen(req, timeout=10) as r:
            raw = json.loads(r.read().decode('utf-8'))
        if 'aaData' in raw:
            for row in raw['aaData']:
                try:
                    code     = row[0].strip()
                    close    = float(row[2].replace(',', ''))
                    chg      = float(row[3].replace(',', '')) if row[3].strip() else 0.0
                    prev     = close - chg
                    chg_pct  = (chg / prev * 100) if prev > 0 else 0.0
                    turnover = int(row[8].replace(',', '')) * 1000 if len(row) > 8 else 0
                    quotes[code] = {'close': close, 'turnover': turnover, 'change_pct': chg_pct}
                except (ValueError, IndexError):
                    continue
    except Exception as e:
        print(f"[Screener] TPEx daily quotes fetch failed: {e}")

    print(f"[Screener] Daily quotes fetched: {len(quotes)} stocks")
    return quotes


def get_inst_5d_candidates():
    """Step 1+2：從 DB 篩出近 5 交易日投信、外資、三大法人合計買超均 > 0 的股票"""
    conn = get_db_connection()
    try:
        df = pd.read_sql_query("""
            SELECT code,
                   SUM(investment_buy)                          AS si,
                   SUM(foreign_buy)                             AS sf,
                   SUM(foreign_buy + investment_buy + dealer_buy) AS st
            FROM institutional_trading
            WHERE date IN (
                SELECT DISTINCT date FROM institutional_trading ORDER BY date DESC LIMIT 5
            )
            GROUP BY code
            HAVING SUM(investment_buy) > 0
               AND SUM(foreign_buy) > 0
               AND SUM(foreign_buy + investment_buy + dealer_buy) > 0
        """, conn)
        candidates = df['code'].tolist()
        print(f"[Screener] Institutional candidates (Step 2): {len(candidates)} stocks")
        return candidates
    except Exception as e:
        print(f"[Screener] get_inst_5d_candidates error: {e}")
        return []
    finally:
        conn.close()


def sync_twse_institutional_data(target_date=None):
    """從證交所及櫃買中心官方 Open Data 下載當日所有三大法人進出數據"""
    if target_date is None:
        target_date = datetime.now()
        # 如果是週末，自動切換至周五
        if target_date.weekday() == 5: # 週六
            target_date -= timedelta(days=1)
        elif target_date.weekday() == 6: # 週日
            target_date -= timedelta(days=2)
            
    date_str = target_date.strftime('%Y%m%d')
    date_hyphen = target_date.strftime('%Y-%m-%d')
    print(f"[Screener] Syncing Institutional Trading Data for {date_hyphen}...")
    
    inst_data = {}
    headers = {
        'User-Agent': 'Mozilla/5.0 (Windows NT 10.0; Win64; x64)'
    }
    
    # 1. 抓取證交所 (TWSE - 上市) 法人交易
    twse_url = f"https://www.twse.com.tw/rwd/zh/fund/T86?response=json&date={date_str}&selectType=ALLBUT0999"
    try:
        req = urllib.request.Request(twse_url, headers=headers)
        with urllib.request.urlopen(req, timeout=8) as response:
            res_json = json.loads(response.read().decode('utf-8'))
            
        if "data" in res_json:
            for row in res_json["data"]:
                code = row[0].strip()
                # 欄位：5外資買賣超, 8投信買賣超, 11自營商買賣超 (有時索引因證交所更新微調，故使用名稱匹配或安全過濾)
                # 證交所標準欄位：
                # 0證券代號, 1證券名稱, 2外陸資買進, 3外陸資賣出, 4外陸資買賣超...
                # 標準對齊：
                # row[4] = 外資買賣超
                # row[7] = 投信買賣超
                # row[10] = 自營商買賣超 (避險+自行買賣合計)
                try:
                    f_buy = int(row[4].replace(',', '')) if len(row) > 4 else 0
                    i_buy = int(row[10].replace(',', '')) if len(row) > 10 else 0
                    d_buy = int(row[11].replace(',', '')) if len(row) > 11 else 0
                    # 換算成「張數」（官方是股數，除以 1000）
                    inst_data[code] = {
                        "foreign_buy": f_buy // 1000,
                        "investment_buy": i_buy // 1000,
                        "dealer_buy": d_buy // 1000
                    }
                except ValueError:
                    continue
    except Exception as e:
        print(f"[Screener] TWSE Institutional Sync Fail: {e}")
        
    # 2. 抓取櫃買中心 (TPEx - 上櫃) 法人交易
    # 轉換日期格式：民國年/MM/DD
    roc_year = target_date.year - 1911
    tpex_date_str = f"{roc_year}/{target_date.strftime('%m/%d')}"
    tpex_url = f"https://www.tpex.org.tw/web/stock/3and1/3and1_detail.php?l=zh-tw&d={tpex_date_str}&se=EW&t=D"
    try:
        req = urllib.request.Request(tpex_url, headers=headers)
        with urllib.request.urlopen(req, timeout=8) as response:
            res_json = json.loads(response.read().decode('utf-8'))
            
        if "aaData" in res_json:
            for row in res_json["aaData"]:
                code = row[0].strip()
                try:
                    # 櫃買欄位：
                    # row[8] 外資買賣超, row[11] 投信買賣超, row[14] 自營商買賣超
                    f_buy = int(row[8].replace(',', '')) if len(row) > 8 else 0
                    i_buy = int(row[11].replace(',', '')) if len(row) > 11 else 0
                    d_buy = int(row[14].replace(',', '')) if len(row) > 14 else 0
                    # 櫃買單位也是股數，除以 1000 換算張數
                    inst_data[code] = {
                        "foreign_buy": f_buy // 1000,
                        "investment_buy": i_buy // 1000,
                        "dealer_buy": d_buy // 1000
                    }
                except ValueError:
                    continue
    except Exception as e:
        print(f"[Screener] TPEx Institutional Sync Fail: {e}")
        
    # 寫入 SQLite
    if inst_data:
        conn = get_db_connection()
        cursor = conn.cursor()
        for code, val in inst_data.items():
            cursor.execute("""
                INSERT INTO institutional_trading (code, date, foreign_buy, investment_buy, dealer_buy)
                VALUES (?, ?, ?, ?, ?)
                ON CONFLICT(code, date) DO UPDATE SET
                    foreign_buy=excluded.foreign_buy,
                    investment_buy=excluded.investment_buy,
                    dealer_buy=excluded.dealer_buy
            """, (code, date_hyphen, val["foreign_buy"], val["investment_buy"], val["dealer_buy"]))
        conn.commit()
        conn.close()
        print(f"[Screener] Institutional data synced. Total tickers: {len(inst_data)}")
    else:
        print("[Screener] No institutional data found (possibly non-trading day).")

def sync_stock_kbars(shioaji_api, codes=None, progress_callback=None):
    """
    從 Shioaji 下載指定股票的日 K 線（約 100 個交易日，足夠計算 60MA）。
    codes 為 None 時下載全部 DEFAULT_STOCKS。
    """
    init_db()
    if codes is None:
        codes = DEFAULT_STOCKS

    # 抓取近 150 個日曆天 ≈ 100 個交易日，足夠計算 20MA / 60MA
    end_date = datetime.now()
    start_date = end_date - timedelta(days=150)
    s_str = start_date.strftime('%Y-%m-%d')
    e_str = end_date.strftime('%Y-%m-%d')

    conn = get_db_connection()
    cursor = conn.cursor()

    total = len(codes)
    print(f"[Screener] Downloading K-bars for {total} stocks from {s_str} to {e_str}...")
    
    success_count = 0
    for idx, code in enumerate(codes):
        try:
            # 獲取 Shioaji 股票合約
            contract = shioaji_api.Contracts.Stocks[code]
            if not contract:
                continue
                
            kbars = shioaji_api.kbars(contract, start=s_str, end=e_str)
            if kbars and kbars.ts and len(kbars.ts) > 0:
                df = pd.DataFrame(dict(kbars))
                df['ts'] = pd.to_datetime(df['ts'], unit='ns')
                df['date'] = df['ts'].dt.strftime('%Y-%m-%d')
                
                # 核心關鍵修正：將 1 分鐘 K 線聚合為標準日 K 線，否則會發生 PRIMARY KEY 覆蓋，導致日K價格與成交量完全錯誤！
                df_daily = df.groupby('date').agg({
                    'Open': 'first',
                    'High': 'max',
                    'Low': 'min',
                    'Close': 'last',
                    'Volume': 'sum'
                }).reset_index()
                
                # 寫入 SQLite (Volume 轉換為張數：Shioaji 預設是股數，除以 1000 以與法人張數對齊)
                for _, row in df_daily.iterrows():
                    cursor.execute("""
                        INSERT INTO daily_kbars (code, date, open, high, low, close, volume)
                        VALUES (?, ?, ?, ?, ?, ?, ?)
                        ON CONFLICT(code, date) DO UPDATE SET
                            open=excluded.open, high=excluded.high, low=excluded.low,
                            close=excluded.close, volume=excluded.volume
                    """, (code, row['date'], row['Open'], row['High'], row['Low'], row['Close'], max(1, int(row['Volume'] // 1000))))
                
                success_count += 1
                
            if progress_callback:
                progress_callback(idx + 1, total)
                
        except Exception as e:
            print(f"[Screener] Failed to sync historical K-bars for {code}: {e}")
            
    conn.commit()
    conn.close()
    print(f"[Screener] Historical K-bar sync completed. Success: {success_count}/{total}")

def run_screener_query(
    turnover_min=30_000_000,   # Step 3：成交金額門檻（元），預設 3000 萬
    max_decline_pct=-3.5       # Step 3：今日跌幅過濾（%），預設 -3.5%
):
    """
    六步驟選股：
    Step 1+2  法人近 5 日三條件篩選（投信/外資/合計均買超 > 0）
    Step 3    當日行情過濾（成交金額、股價、跌幅）
    Step 4    只對剩下股票讀 DB 中的 K 線
    Step 5    技術條件：close > 20MA > 60MA、近 20/60 日漲幅 > 大盤、乖離 < 10%
    Step 6    輸出候選清單，依階級與優先度排序
    """
    # ── Step 1+2 ──────────────────────────────────────────────
    candidates = get_inst_5d_candidates()
    if not candidates:
        return []

    # ── Step 3 ────────────────────────────────────────────────
    daily_quotes = fetch_twse_daily_quotes()
    filtered = []
    for code in candidates:
        q = daily_quotes.get(code)
        if q:
            if q['turnover'] < turnover_min:
                continue
            if q['change_pct'] < max_decline_pct:
                continue
        filtered.append(code)

    if not filtered:
        return []
    print(f"[Screener] After Step 3 filter: {len(filtered)} stocks")

    # ── Step 4：只讀剩下股票的 K 線 ──────────────────────────
    conn = get_db_connection()
    ph = ','.join('?' * len(filtered))
    df_k = pd.read_sql_query(
        f"SELECT * FROM daily_kbars WHERE code IN ({ph}) ORDER BY code, date ASC",
        conn, params=filtered
    )
    df_inst = pd.read_sql_query(
        f"SELECT * FROM institutional_trading WHERE code IN ({ph}) ORDER BY code, date ASC",
        conn, params=filtered
    )
    conn.close()

    if df_k.empty:
        return []

    index_gain_20 = 0.015   # 大盤基準：20 日漲幅 1.5%
    index_gain_60 = 0.04    # 大盤基準：60 日漲幅 4.0%
    results = []

    for code in filtered:
        sub_df = df_k[df_k['code'] == code].copy()
        # 至少需要 62 根才能計算 60MA 並取 prev_60
        if len(sub_df) < 62:
            continue

        sub_df['20MA']     = sub_df['close'].rolling(20).mean()
        sub_df['60MA']     = sub_df['close'].rolling(60).mean()
        sub_df['20MA_vol'] = sub_df['volume'].rolling(20).mean()

        latest = sub_df.iloc[-1]
        if pd.isna(latest['20MA']) or pd.isna(latest['60MA']):
            continue

        # ── Step 5a：多頭排列 close > 20MA > 60MA ─────────────
        if not (latest['close'] > latest['20MA'] > latest['60MA']):
            continue

        # ── Step 5b：近 20 日漲幅 > 大盤 ─────────────────────
        prev_20  = sub_df.iloc[-20] if len(sub_df) >= 20 else sub_df.iloc[0]
        gain_20  = (latest['close'] - prev_20['close']) / prev_20['close']
        if gain_20 <= index_gain_20:
            continue

        # ── Step 5c：近 60 日漲幅 > 大盤 ─────────────────────
        prev_60  = sub_df.iloc[-60] if len(sub_df) >= 60 else sub_df.iloc[0]
        gain_60  = (latest['close'] - prev_60['close']) / prev_60['close']
        if gain_60 <= index_gain_60:
            continue

        # ── Step 5d：股價距 20MA < 10%（不追高）───────────────
        bias = (latest['close'] - latest['20MA']) / latest['20MA'] * 100
        if bias >= 10.0:
            continue

        # ── 籌碼面指標計算（供顯示用）──────────────────────────
        code_inst         = df_inst[df_inst['code'] == code].copy()
        foreign_strike    = 0
        investment_strike = 0
        sync_buy          = False
        dealer_buy        = False
        inst_ratio_5d     = 0.0

        if not code_inst.empty:
            inst_list = code_inst.tail(10).to_dict('records')

            for r in reversed(inst_list):
                if r['investment_buy'] > 0:
                    investment_strike += 1
                else:
                    break
            for r in reversed(inst_list):
                if r['foreign_buy'] > 0:
                    foreign_strike += 1
                else:
                    break
            if inst_list:
                last_r   = inst_list[-1]
                sync_buy = (last_r['foreign_buy'] > 0 and last_r['investment_buy'] > 0
                            and last_r['dealer_buy'] > 0)
                dealer_buy = last_r['dealer_buy'] > 0

            last_5_inst  = inst_list[-5:] if len(inst_list) >= 5 else inst_list
            last_5_kbars = sub_df.tail(len(last_5_inst)).to_dict('records')
            total_inst   = sum(r['foreign_buy'] + r['investment_buy'] + r['dealer_buy']
                               for r in last_5_inst)
            total_vol    = sum(r['volume'] for r in last_5_kbars)
            if total_vol > 0:
                inst_ratio_5d = (total_inst / total_vol) * 100

        # ── 階級分類 ───────────────────────────────────────────
        if investment_strike > 0 and foreign_strike > 0 and sync_buy:
            tier_level, tier_name = 1, "黃金滿貫"
        elif investment_strike > 0 and foreign_strike > 0:
            tier_level, tier_name = 2, "強勢雙雄"
        elif investment_strike > 0:
            tier_level, tier_name = 3, "投信鎖碼"
        elif foreign_strike > 0:
            tier_level, tier_name = 4, "外資鎖碼"
        else:
            tier_level, tier_name = 5, "主力佈局"

        priority_score = 0
        if investment_strike > 0:
            priority_score += 1000 + investment_strike * 10
        if foreign_strike > 0:
            priority_score += 500 + foreign_strike
        if sync_buy:
            priority_score += 200
        if dealer_buy:
            priority_score += 50

        results.append({
            "code":               code,
            "name":               STOCK_NAMES.get(code, "未知"),
            "close":              latest['close'],
            "bias":               round(bias, 2),
            "gain_20":            round(gain_20 * 100, 2),
            "gain_60":            round(gain_60 * 100, 2),
            "volume":             int(latest['volume']),
            "investment_strike":  investment_strike,
            "foreign_strike":     foreign_strike,
            "sync_buy":           sync_buy,
            "inst_ratio_5d":      round(inst_ratio_5d, 2),
            "mention_count":      0,
            "priority":           priority_score,
            "tier_level":         tier_level,
            "tier_name":          tier_name
        })

    results.sort(key=lambda x: (x["tier_level"], -x["priority"]))
    return results

if __name__ == "__main__":
    init_db()
    sync_twse_institutional_data()
    print("Inst candidates:", get_inst_5d_candidates())
