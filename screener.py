import os
import ssl
import sqlite3
import urllib.request
import json
import re
import pandas as pd
from datetime import datetime, timedelta
import time
import market_status as _ms

try:
    import certifi
    _SSL_CTX = ssl.create_default_context(cafile=certifi.where())
except Exception:
    _SSL_CTX = ssl.create_default_context()

DB_PATH = str(os.path.join(os.path.dirname(os.path.abspath(__file__)), "stock_cache.db"))

# ─── 商品類型過濾設定 ─────────────────────────────────────────────────
INCLUDE_ETF     = False   # ETF 放入獨立 etf_candidates，不混入個股可買清單
INCLUDE_ETN     = False   # ETN 直接排除
INCLUDE_WARRANT = False   # 權證直接排除

# ─── 候選數量上限 ─────────────────────────────────────────────────────
_MAX_BUY_CANDIDATES      = 20   # 明日可買上限
_MAX_HIGH_PRIORITY_WATCH = 50   # 高優先觀察上限

# ─── 流動性門檻 ───────────────────────────────────────────────────────
_LIQ_AMOUNT_THRESHOLD = 30_000_000  # 近20日均成交金額 3000萬
_LIQ_VOLUME_THRESHOLD = 1_000       # 近20日均成交量（張）

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

# 台灣證交所 / 櫃買中心產業分類代碼 → 中文名稱
_INDUSTRY_MAP = {
    "00": "ETF",
    "01": "水泥工業",   "02": "食品工業",   "03": "塑膠工業",   "04": "紡織纖維",
    "05": "電機機械",   "06": "電器電纜",   "07": "化學工業",   "08": "玻璃陶瓷",
    "09": "造紙工業",   "10": "鋼鐵工業",   "11": "橡膠工業",   "12": "汽車工業",
    "14": "建材營建",   "15": "航運業",     "16": "觀光餐旅",   "17": "金融保險",
    "18": "貿易百貨",   "19": "綜合",       "20": "其他",       "21": "化學工業",
    "22": "生技醫療",   "23": "油電燃氣",   "24": "半導體業",   "25": "電腦週邊",
    "26": "光電業",     "27": "通信網路",   "28": "電子零組件", "29": "電子通路",
    "30": "資訊服務",   "31": "其他電子",   "32": "文化創意",   "33": "農業科技",
    "34": "電子商務",   "35": "綠能環保",   "36": "數位雲端",
}

def _resolve_industry(raw: str) -> str:
    """將 Shioaji 回傳的產業代碼（如 '24'）轉為中文名稱，已是中文則直接回傳"""
    if not raw:
        return ''
    key = raw.strip().zfill(2)
    return _INDUSTRY_MAP.get(key, raw)

def _detect_stock_type(code: str, name: str, category: str) -> str:
    """偵測股票類型：'etf', 'etn', 'warrant', 'preferred', 'ky', 'common'"""
    if not code:
        return 'common'
    # ETF：代號 00 開頭（0050/0056/00965/006208...），或產業分類為 ETF
    if code.startswith('00') or (category or '').upper() == 'ETF':
        return 'etf'
    # ETN：名稱含 ETN
    if name and 'ETN' in (name or '').upper():
        return 'etn'
    # 權證：6位純數字且不以 00 開頭
    if len(code) >= 6 and code.isdigit():
        return 'warrant'
    # 特別股：名稱含「特別」或「優先」
    if name and ('特別' in name or '優先' in name):
        return 'preferred'
    # KY：海外掛牌，保留但標記
    if name and '-KY' in name:
        return 'ky'
    return 'common'


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
    
    # 建立股票名稱快取表
    cursor.execute("""
        CREATE TABLE IF NOT EXISTS stock_names (
            code TEXT PRIMARY KEY,
            name TEXT NOT NULL,
            category TEXT DEFAULT ''
        )
    """)
    # 舊版資料庫升級：補上 category 欄位
    try:
        cursor.execute("ALTER TABLE stock_names ADD COLUMN category TEXT DEFAULT ''")
    except Exception:
        pass
    # 將已存的數字代碼（如 '24'）轉為中文名稱
    rows = cursor.execute("SELECT code, category FROM stock_names WHERE category != ''").fetchall()
    for r_code, r_cat in rows:
        resolved = _resolve_industry(r_cat)
        if resolved != r_cat:
            cursor.execute("UPDATE stock_names SET category=? WHERE code=?", (resolved, r_code))

    # 建立索引以優化查詢
    cursor.execute("CREATE INDEX IF NOT EXISTS idx_kbars_date ON daily_kbars(date)")
    cursor.execute("CREATE INDEX IF NOT EXISTS idx_kbars_code ON daily_kbars(code)")
    cursor.execute("CREATE INDEX IF NOT EXISTS idx_inst_date ON institutional_trading(date)")
    cursor.execute("CREATE INDEX IF NOT EXISTS idx_inst_code ON institutional_trading(code)")
    
    conn.commit()
    conn.close()
    print("[Screener DB] Database initialized successfully.")

def fetch_twse_daily_quotes():
    """Step 3 資料：從證交所 + 櫃買中心取得今日所有股票的收盤價、成交金額、漲跌幅。
    回傳 (quotes dict, data_date str)，data_date 格式為 'YYYYMMDD'；抓不到時為 None。"""
    quotes = {}
    data_date = None
    headers = {'User-Agent': 'Mozilla/5.0 (Windows NT 10.0; Win64; x64)'}

    def _safe_float(s, fallback=None):
        try:
            return float(s.replace(',', ''))
        except (ValueError, AttributeError):
            return fallback

    # 上市 (TWSE)
    try:
        url = "https://www.twse.com.tw/rwd/zh/afterTrading/STOCK_DAY_ALL?response=json"
        req = urllib.request.Request(url, headers=headers)
        with urllib.request.urlopen(req, timeout=10, context=_SSL_CTX) as r:
            raw = json.loads(r.read().decode('utf-8'))
        if raw.get('stat') == 'OK' and 'data' in raw:
            data_date = raw.get('date')  # 'YYYYMMDD'，確認資料實際日期
            fields = raw.get('fields', [])
            code_i     = fields.index('證券代號')  if '證券代號'  in fields else 0
            name_i     = fields.index('證券名稱')  if '證券名稱'  in fields else 1
            open_i     = fields.index('開盤價')    if '開盤價'    in fields else 5
            high_i     = fields.index('最高價')    if '最高價'    in fields else 6
            low_i      = fields.index('最低價')    if '最低價'    in fields else 7
            close_i    = fields.index('收盤價')    if '收盤價'    in fields else 8
            turnover_i = fields.index('成交金額')  if '成交金額'  in fields else 4
            vol_i      = fields.index('成交股數')  if '成交股數'  in fields else 2
            sign_i     = fields.index('漲跌(+/-)') if '漲跌(+/-)' in fields else 9
            change_i   = fields.index('漲跌價差')  if '漲跌價差'  in fields else 10
            for row in raw['data']:
                try:
                    code     = row[code_i].strip()
                    name     = row[name_i].strip()
                    close    = float(row[close_i].replace(',', ''))
                    open_p   = _safe_float(row[open_i], close)
                    high_p   = _safe_float(row[high_i], close)
                    low_p    = _safe_float(row[low_i],  close)
                    turnover = int(row[turnover_i].replace(',', ''))
                    vol_lots = int(row[vol_i].replace(',', '')) // 1000
                    sign     = row[sign_i].strip()
                    chg_abs  = float(row[change_i].replace(',', ''))
                    chg      = chg_abs if sign == '+' else (-chg_abs if sign == '-' else 0.0)
                    prev_c   = close - chg
                    chg_pct  = (chg / prev_c * 100) if prev_c > 0 else 0.0
                    quotes[code] = {
                        'name': name, 'close': close, 'open': open_p, 'high': high_p, 'low': low_p,
                        'volume': vol_lots, 'turnover': turnover, 'change_pct': chg_pct
                    }
                except (ValueError, IndexError):
                    continue
    except Exception as e:
        print(f"[Screener] TWSE STOCK_DAY_ALL fetch failed: {e}")

    # 上櫃 (TPEx) — 使用 OpenAPI v1
    try:
        url = "https://www.tpex.org.tw/openapi/v1/tpex_mainboard_quotes"
        req = urllib.request.Request(url, headers=headers)
        with urllib.request.urlopen(req, timeout=15, context=_SSL_CTX) as r:
            raw = json.loads(r.read().decode('utf-8'))
        if isinstance(raw, list):
            for row in raw:
                try:
                    code     = row.get('SecuritiesCompanyCode', '').strip()
                    if not code:
                        continue
                    name     = row.get('CompanyName', '').strip()
                    close    = _safe_float(row.get('Close', ''), None)
                    if close is None:
                        continue
                    open_p   = _safe_float(row.get('Open',   ''), close)
                    high_p   = _safe_float(row.get('High',   ''), close)
                    low_p    = _safe_float(row.get('Low',    ''), close)
                    chg      = _safe_float(row.get('Change', ''), 0.0)
                    shares   = _safe_float(row.get('TradingShares', '0').replace(',', ''), 0)
                    vol_lots = int(shares) // 1000
                    turnover = int(_safe_float(row.get('TransactionAmount', '0').replace(',', ''), 0))
                    prev_c   = close - chg
                    chg_pct  = (chg / prev_c * 100) if prev_c > 0 else 0.0
                    quotes[code] = {
                        'name': name, 'close': close, 'open': open_p, 'high': high_p, 'low': low_p,
                        'volume': vol_lots, 'turnover': turnover, 'change_pct': chg_pct
                    }
                except (ValueError, KeyError):
                    continue
    except Exception as e:
        print(f"[Screener] TPEx daily quotes fetch failed: {e}")

    print(f"[Screener] Daily quotes fetched: {len(quotes)} stocks, data_date={data_date}")
    return quotes, data_date


def get_inst_5d_candidates():
    """Step 1：近5日三大法人合計>0，且外資或投信至少一方>0"""
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
            HAVING SUM(foreign_buy + investment_buy + dealer_buy) > 0
               AND (SUM(foreign_buy) > 0 OR SUM(investment_buy) > 0)
        """, conn)
        candidates = df['code'].tolist()
        print(f"[Screener] Step 1 candidates (total>0 & foreign>0|invest>0): {len(candidates)} stocks")
        return candidates
    except Exception as e:
        print(f"[Screener] get_inst_5d_candidates error: {e}")
        return []
    finally:
        conn.close()


def sync_twse_institutional_data(target_date=None):
    """從證交所及櫃買中心官方 Open Data 下載當日所有三大法人進出數據"""
    init_db()  # <-- 確保本地資料庫與相關資料表均已建立
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
        with urllib.request.urlopen(req, timeout=8, context=_SSL_CTX) as response:
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
    # 改用全新櫃買中心三大法人買賣超 JSON API 介面
    tpex_url = f"https://www.tpex.org.tw/web/stock/3insti/daily_trade/3itrade_hedge_result.php?l=zh-tw&o=json&se=EW&t=D&d={tpex_date_str}"
    try:
        req = urllib.request.Request(tpex_url, headers=headers)
        with urllib.request.urlopen(req, timeout=8, context=_SSL_CTX) as response:
            res_json = json.loads(response.read().decode('utf-8'))
            
        rows = []
        is_new_format = False
        
        if "tables" in res_json and len(res_json["tables"]) > 0:
            is_new_format = True
            rows = res_json["tables"][0].get("data", [])
        elif "aaData" in res_json:
            rows = res_json["aaData"]
            
        for row in rows:
            try:
                code = row[0].strip()
                if is_new_format:
                    # 新版欄位索引：row[10]=外資及陸資合計買賣超, row[13]=投信買賣超, row[22]=自營商合計買賣超
                    f_buy = int(str(row[10]).replace(',', '')) if len(row) > 10 else 0
                    i_buy = int(str(row[13]).replace(',', '')) if len(row) > 13 else 0
                    d_buy = int(str(row[22]).replace(',', '')) if len(row) > 22 else 0
                else:
                    # 舊版欄位索引
                    f_buy = int(str(row[8]).replace(',', '')) if len(row) > 8 else 0
                    i_buy = int(str(row[11]).replace(',', '')) if len(row) > 11 else 0
                    d_buy = int(str(row[14]).replace(',', '')) if len(row) > 14 else 0
                
                # 櫃買單位也是股數，除以 1000 換算張數
                inst_data[code] = {
                    "foreign_buy": f_buy // 1000,
                    "investment_buy": i_buy // 1000,
                    "dealer_buy": d_buy // 1000
                }
            except (ValueError, IndexError):
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
    使用 3 條執行緒並行下載，並以批次 INSERT 取代逐行寫入。
    """
    from concurrent.futures import ThreadPoolExecutor, as_completed
    import threading

    init_db()
    if codes is None:
        codes = DEFAULT_STOCKS
    if shioaji_api is None:
        print("[Screener] ERROR: shioaji_api is None, cannot sync K-bars")
        return

    end_date = datetime.now()
    full_start = end_date - timedelta(days=115)  # 78 交易日 + 假日 buffer，MACD EWM 充分暖機
    today_str = end_date.strftime('%Y-%m-%d')
    total = len(codes)

    # ── 一次查出所有股票的最新日期（取代迴圈內逐筆 SELECT）──
    conn0 = get_db_connection()
    ph = ','.join('?' * len(codes))
    latest_dates = dict(conn0.execute(
        f"SELECT code, MAX(date) FROM daily_kbars WHERE code IN ({ph}) GROUP BY code", codes
    ).fetchall())
    conn0.close()

    codes_to_fetch = [c for c in codes if (latest_dates.get(c) or '') < today_str]
    skip_count = total - len(codes_to_fetch)
    print(f"[Screener] Incremental K-bar sync: {len(codes_to_fetch)} to fetch, {skip_count} already up-to-date (total {total})")

    # ── 全局 rate limiter：不管幾條 thread，API 呼叫間距固定 ≥ 0.15s（≈6~7 req/s）──
    _api_lock  = threading.Lock()
    _last_call = [0.0]
    _MIN_INTERVAL = 0.15

    def _call_kbars(contract, start, end):
        with _api_lock:
            gap = _MIN_INTERVAL - (time.time() - _last_call[0])
            if gap > 0:
                time.sleep(gap)
            result = shioaji_api.kbars(contract, start=start, end=end)
            _last_call[0] = time.time()
        return result

    _lock = threading.Lock()
    done_count = [0]

    def fetch_one(code):
        contract = None
        for market in ('TSE', 'OTC'):
            try:
                c = getattr(shioaji_api.Contracts.Stocks, market)[code]
                if c:
                    contract = c
                    break
            except Exception:
                pass
        if not contract:
            return code, None, None, None

        stock_name     = getattr(contract, 'name', '') or getattr(contract, 'chinese_name', '')
        stock_category = _resolve_industry(getattr(contract, 'category', '') or '')

        latest_in_db = latest_dates.get(code)
        if latest_in_db:
            chunk_start = datetime.strptime(latest_in_db, '%Y-%m-%d') + timedelta(days=1)
            if chunk_start < full_start:
                chunk_start = full_start
        else:
            chunk_start = full_start

        all_chunks = []
        while chunk_start <= end_date:
            chunk_end = min(chunk_start + timedelta(days=29), end_date)
            try:
                chunk_kbars = _call_kbars(
                    contract,
                    start=chunk_start.strftime('%Y-%m-%d'),
                    end=chunk_end.strftime('%Y-%m-%d')
                )
                if chunk_kbars and chunk_kbars.ts and len(chunk_kbars.ts) > 0:
                    all_chunks.append(pd.DataFrame(dict(chunk_kbars)))
            except Exception as e:
                print(f"[Screener] kbars chunk error {code}: {e}")
            chunk_start = chunk_end + timedelta(days=1)

        if not all_chunks:
            return code, stock_name, stock_category, None

        df = pd.concat(all_chunks).drop_duplicates(subset=['ts'])
        df['ts']  = pd.to_datetime(df['ts'], unit='ns')
        df['date'] = df['ts'].dt.strftime('%Y-%m-%d')
        df_daily = df.groupby('date').agg(
            Open=('Open', 'first'), High=('High', 'max'),
            Low=('Low', 'min'),   Close=('Close', 'last'), Volume=('Volume', 'sum')
        ).reset_index()
        return code, stock_name, stock_category, df_daily

    # ── 並行下載 ──
    all_kbar_rows = []
    all_name_rows = []
    success_count = 0
    fail_count    = 0

    with ThreadPoolExecutor(max_workers=3) as executor:
        futures = {executor.submit(fetch_one, code): code for code in codes_to_fetch}
        for future in as_completed(futures):
            try:
                code, name, category, df_daily = future.result()
            except Exception as e:
                fail_count += 1
                print(f"[Screener] Worker exception: {e}")
                continue

            with _lock:
                done_count[0] += 1
                cur_done = done_count[0]

            if df_daily is not None:
                success_count += 1
                for _, row in df_daily.iterrows():
                    all_kbar_rows.append((
                        code, row['date'], row['Open'], row['High'],
                        row['Low'], row['Close'], max(1, int(row['Volume']))
                    ))
                if name:
                    all_name_rows.append((code, name, category))
            elif name is None:
                pass  # contract not found, skip silently
            else:
                fail_count += 1

            if progress_callback:
                progress_callback(cur_done + skip_count, total)
            if cur_done % 50 == 0 or cur_done == len(codes_to_fetch):
                print(f"[Screener] K-bar sync progress: {cur_done + skip_count}/{total} "
                      f"(✓{success_count} ✗{fail_count} skip{skip_count})")

    # ── 批次寫入 SQLite（一次 executemany + 一次 commit）──
    conn = get_db_connection()
    cursor = conn.cursor()
    if all_name_rows:
        cursor.executemany(
            "INSERT INTO stock_names (code, name, category) VALUES (?, ?, ?) "
            "ON CONFLICT(code) DO UPDATE SET name=excluded.name, category=excluded.category",
            all_name_rows
        )
    if all_kbar_rows:
        cursor.executemany("""
            INSERT INTO daily_kbars (code, date, open, high, low, close, volume)
            VALUES (?, ?, ?, ?, ?, ?, ?)
            ON CONFLICT(code, date) DO UPDATE SET
                open=excluded.open, high=excluded.high, low=excluded.low,
                close=excluded.close, volume=excluded.volume
        """, all_kbar_rows)
    conn.commit()
    conn.close()
    print(f"[Screener] K-bar sync completed. Fetched: {success_count}, "
          f"Skipped(up-to-date): {skip_count}, Failed: {fail_count} / Total: {total}")

def compute_macd(close_series):
    """計算標準 MACD (12, 26, 9)"""
    ema12 = close_series.ewm(span=12, adjust=False).mean()
    ema26 = close_series.ewm(span=26, adjust=False).mean()
    dif = ema12 - ema26
    dem = dif.ewm(span=9, adjust=False).mean()
    osc = dif - dem
    return osc

def run_screener_query(
    max_decline_pct=-3.5,        # Step 3：今日跌幅過濾（%），預設 -3.5%
    trace_code = None  # 單股追蹤代號，None 則不輸出 TRACE
):
    """
    升級版六步驟選股與交易決策系統：
    Step 1+2  法人近5日篩選：三大合計>0 且 (外資>0 OR 投信>0)
    Step 3    當日行情過濾（成交金額、股價、跌幅）
    Step 4    只對剩下股票讀 DB 中的 K 線
    Step 5    技術與決策大腦：多頭排列、相對強度、MACD負柱收斂、明日交易分數、策略狀態、買點型態、停損計算
    Step 6    輸出候選清單，依交易優先度與明日交易分數排序
    """
    # ── 單股追蹤 Debug（由呼叫端傳入 trace_code，None 則關閉）──
    TRACE_CODE = trace_code

    def _trace(msg):
        if TRACE_CODE:
            print(f"[TRACE {TRACE_CODE}] {msg}")

    # ── Step 1+2 ──────────────────────────────────────────────
    candidates = get_inst_5d_candidates()
    if not candidates:
        _trace("step1Passed=false reason=get_inst_5d_candidates 回傳空清單")
        return []

    _trace(f"inUniverse={TRACE_CODE in DEFAULT_STOCKS}")
    _trace(f"step1Passed={TRACE_CODE in candidates} reason={'通過新Step1：三大合計>0且外資或投信至少一方>0' if TRACE_CODE in candidates else '未通過新Step1：三大法人合計須>0，且外資或投信至少一方>0'}")

    # ── Step 3 ────────────────────────────────────────────────
    daily_quotes, twse_data_date = fetch_twse_daily_quotes()
    twse_is_today = (twse_data_date == datetime.now().strftime('%Y%m%d'))
    if not twse_is_today:
        print(f"[Screener] TWSE data_date={twse_data_date} != today, today's injection will be skipped")

    _q6 = daily_quotes.get(TRACE_CODE)
    _trace(f"inDailyQuotes={_q6 is not None}"
           + (f" close={_q6['close']} changePct={_q6['change_pct']:.2f}%" if _q6 else " reason=不在今日 TWSE/TPEx 報價清單"))

    # 從 stock_names 快取補充名稱（優先度：daily_quotes > stock_names > STOCK_NAMES > "未知"）
    conn_names = get_db_connection()
    db_names = {}
    db_categories = {}
    for r in conn_names.execute("SELECT code, name, category FROM stock_names").fetchall():
        db_names[r[0]] = r[1]
        db_categories[r[0]] = _resolve_industry(r[2] or '')
    conn_names.close()
    filtered = []
    for code in candidates:
        q = daily_quotes.get(code)
        if q and q['change_pct'] < max_decline_pct:
            if code == TRACE_CODE:
                _trace(f"step3Passed=false reason=今日跌幅 {q['change_pct']:.2f}% < 門檻 {max_decline_pct}%")
            continue
        filtered.append(code)

    if TRACE_CODE:
        _trace(f"step3Passed={TRACE_CODE in filtered}"
               + ('' if TRACE_CODE in filtered else ' reason=被今日跌幅或不在候選名單過濾'))

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
        _trace("hasKbars=false reason=df_k 完全為空")
        return []

    # Step 4 kbars TRACE
    _sub6 = df_k[df_k['code'] == TRACE_CODE] if TRACE_CODE else None
    if TRACE_CODE:
        if _sub6 is not None and len(_sub6) > 0:
            _trace(f"hasKbars=true kbarCount={len(_sub6)} latestDate={_sub6.iloc[-1]['date']} "
                   f"close={_sub6.iloc[-1]['close']} volume={int(_sub6.iloc[-1]['volume'])}張")
        else:
            _trace("hasKbars=false reason=DB 中無此股票的 daily_kbars 記錄，請先執行同步選股數據")

    index_gain_20 = 1.5   # 大盤基準：20 日漲幅 1.5%
    index_gain_60 = 4.0   # 大盤基準：60 日漲幅 4.0%
    today_str = datetime.now().strftime('%Y-%m-%d')

    # ── 計算大盤狀態（用於調整明日優先條件與個股建議買法）──────────────
    try:
        _market_status = _ms.calculate_market_status()
        print(f"[Screener] 大盤狀態：{_market_status['label']}  "
              f"距20MA {_market_status['metrics']['bias_ma20_pct']:+.1f}%  "
              f"距60MA {_market_status['metrics']['bias_ma60_pct']:+.1f}%  "
              f"過熱個股 {_market_status['metrics']['hot_stock_ratio']:.1f}%")
    except Exception as _ms_err:
        print(f"[Screener] 大盤狀態計算失敗: {_ms_err}，使用預設正常多頭")
        _market_status = {"status": "normal_bull", "label": "正常多頭", "score": 50,
                          "description": "", "suggestion": "",
                          "metrics": {"index_close": 0, "index_ma20": 0, "index_ma60": 0,
                                      "bias_ma20_pct": 0, "bias_ma60_pct": 0,
                                      "market_amount_ratio": 0, "margin_5d_change": None,
                                      "hot_stock_ratio": 0, "very_hot_stock_ratio": 0,
                                      "surge_5d_ratio": 0, "data_available": False}}
    _ms_code = _market_status.get('status', 'normal_bull')

    results = []

    for code in filtered:
        liquidity_reason = ""  # 初始化，防止在 liq trace 中 NameError
        q = daily_quotes.get(code)
        sub_df = df_k[df_k['code'] == code].copy()

        # Shioaji kbars 通常 T+1 才有今日資料；若 DB 最新日期 < 今天，
        # 且 TWSE 資料確認是今日，才補一筆，避免用昨日收盤冒充今日。
        if q and twse_is_today and (sub_df.empty or sub_df.iloc[-1]['date'] < today_str):
            today_row = pd.DataFrame([{
                'code':   code,
                'date':   today_str,
                'open':   q.get('open',   q['close']),
                'high':   q.get('high',   q['close']),
                'low':    q.get('low',    q['close']),
                'close':  q['close'],
                'volume': q.get('volume', 0),
            }])
            sub_df = pd.concat([sub_df, today_row], ignore_index=True)

        # 至少需要 62 根才能計算 60MA 並取得歷史基準
        if len(sub_df) < 62:
            if code == TRACE_CODE:
                _trace(f"liquidityChecked=false finalIncluded=false "
                       f"reason=K線不足62根（目前{len(sub_df)}根），無法計算60MA，請重新同步K線數據")
            continue

        # 計算均線與指標
        sub_df['ma5']      = sub_df['close'].rolling(5).mean()
        sub_df['ma10']     = sub_df['close'].rolling(10).mean()
        sub_df['ma20']     = sub_df['close'].rolling(20).mean()
        sub_df['ma60']     = sub_df['close'].rolling(60).mean()
        sub_df['volume_ma20'] = sub_df['volume'].rolling(20).mean()
        
        # MACD 柱狀體
        sub_df['macd_hist'] = compute_macd(sub_df['close'])
        
        # 成交金額：volume 單位為張（1張=1000股），close 為每股價格
        sub_df['amount']      = sub_df['close'] * sub_df['volume'] * 1000
        sub_df['amount_ma5']  = sub_df['amount'].rolling(5).mean()
        sub_df['amount_ma20'] = sub_df['amount'].rolling(20).mean()

        latest = sub_df.iloc[-1]
        prev   = sub_df.iloc[-2]
        
        if pd.isna(latest['ma20']) or pd.isna(latest['ma60']):
            if code == TRACE_CODE:
                _trace(f"liquidityChecked=false finalIncluded=false "
                       f"reason=ma20={latest.get('ma20')} 或 ma60={latest.get('ma60')} 為 NaN，K線數量不足")
            continue

        # ── 流動性必要篩選 ────────────────────────────────────────────────
        # volume 單位：張（lot）；amount = close * volume * 1000（已於上方計算）
        _VOLUME_UNIT = "lot"
        _AMOUNT_UNIT = "calculatedFromVolume"
        _LIQ_THRESHOLD = _LIQ_AMOUNT_THRESHOLD  # 3000萬元

        def _safe_float(v):
            try:
                f = float(v)
                return None if pd.isna(f) else f
            except (TypeError, ValueError):
                return None

        _ama5  = _safe_float(latest.get('amount_ma5',  None))
        _ama20 = _safe_float(latest.get('amount_ma20', None))
        _vm20_raw = latest.get('volume_ma20', float('nan'))
        _vol_ma20_val = float(_vm20_raw) if not pd.isna(_vm20_raw) else 0.0
        _liq_data_missing = (_ama5 is None) and (_ama20 is None)

        # 通過條件：amountMa20 >= 3000萬 OR volumeMa20 >= 1000張
        if _liq_data_missing:
            _liq_passed = False
            liquidity_reason = "成交金額資料缺失：amountMa5 / amountMa20 均無效"
        else:
            _amt_passed = (
                (_ama5  is not None and _ama5  >= _LIQ_THRESHOLD)
                or (_ama20 is not None and _ama20 >= _LIQ_THRESHOLD)
            )
            _vol_passed = (_vol_ma20_val >= _LIQ_VOLUME_THRESHOLD)
            _liq_passed = _amt_passed or _vol_passed
            if _liq_passed:
                if _amt_passed:
                    liquidity_reason = f"通過：amountMa20={(_ama20 or 0):,.0f} >= {_LIQ_THRESHOLD:,}"
                else:
                    liquidity_reason = f"通過(量)：volumeMa20={_vol_ma20_val:.0f}張 >= {_LIQ_VOLUME_THRESHOLD}張"
            else:
                liquidity_reason = (f"未通過：amountMa5={(_ama5 or 0):,.0f}，"
                                    f"amountMa20={(_ama20 or 0):,.0f}，"
                                    f"volumeMa20={_vol_ma20_val:.0f}張，均未達門檻")

        # server-side debug log
        _amt_today = float(latest['close'] * latest['volume'] * 1000)
        print(
            f"[LIQ] {code:6s}  close={latest['close']:.2f}  vol={int(latest['volume'])}張"
            f"  amtToday={_amt_today/1e8:.3f}億"
            f"  ama5={(_ama5 or 0)/1e8:.3f}億  ama20={(_ama20 or 0)/1e8:.3f}億"
            f"  volMa20={_vol_ma20_val:.0f}張  passed={_liq_passed}"
        )

        if code == TRACE_CODE:
            _trace(f"liquidityChecked=true "
                   f"ama5={(_ama5 or 0)/1e6:.0f}萬 ama20={(_ama20 or 0)/1e6:.0f}萬 "
                   f"threshold={_LIQ_THRESHOLD/1e6:.0f}萬 passed={_liq_passed} reason={liquidity_reason}")
        if not _liq_passed:
            if code == TRACE_CODE:
                _trace(f"finalIncluded=false reason=流動性未通過")
            continue

        # ── Step 5a：多頭排列 close > 20MA > 60MA ─────────────
        if not (latest['close'] > latest['ma20'] > latest['ma60']):
            if code == TRACE_CODE:
                _trace(f"finalIncluded=false reason=多頭排列不符 "
                       f"close={latest['close']} ma20={latest['ma20']:.2f} ma60={latest['ma60']:.2f}")
            continue

        # 計算各項漲幅
        prev_5  = sub_df.iloc[-6] if len(sub_df) >= 6 else sub_df.iloc[0]
        prev_20 = sub_df.iloc[-21] if len(sub_df) >= 21 else sub_df.iloc[0]
        prev_60 = sub_df.iloc[-61] if len(sub_df) >= 61 else sub_df.iloc[0]
        
        return5  = ((latest['close'] - prev_5['close']) / prev_5['close']) * 100
        return20 = ((latest['close'] - prev_20['close']) / prev_20['close']) * 100
        return60 = ((latest['close'] - prev_60['close']) / prev_60['close']) * 100
        
        bias20 = ((latest['close'] - latest['ma20']) / latest['ma20']) * 100

        # ── Step 5b：20日強度硬篩（保留）────────────────────────
        if return20 <= index_gain_20:
            if code == TRACE_CODE:
                _trace(f"finalIncluded=false reason=20日強度未達大盤基準 "
                       f"return20={return20:.2f}%（需>{index_gain_20}%）")
            continue
        # ── Step 5c：60日強度改為加分條件（不再硬篩）────────────
        if code == TRACE_CODE:
            if return60 > index_gain_60:
                _trace(f"return60={return60:.2f}% passed=true 將加10分")
            else:
                _trace(f"return60={return60:.2f}% scoreOnly=true reason=return60未達{index_gain_60}%，不排除只是不加分")

        # 歷史區間高低點 (不含今日)
        recentHigh10 = sub_df.iloc[-11:-1]['high'].max() if len(sub_df) >= 11 else sub_df['high'].max()
        recentHigh20 = sub_df.iloc[-21:-1]['high'].max() if len(sub_df) >= 21 else sub_df['high'].max()
        recentLow10  = sub_df.iloc[-11:-1]['low'].min() if len(sub_df) >= 11 else sub_df['low'].min()
        recentLow20  = sub_df.iloc[-21:-1]['low'].min() if len(sub_df) >= 21 else sub_df['low'].min()
        recentHigh60 = sub_df.iloc[-61:-1]['high'].max() if len(sub_df) >= 61 else sub_df['high'].max()

        # 上影線比例
        high_low_diff = latest['high'] - latest['low']
        body_max = max(latest['open'], latest['close'])
        candleUpperShadowRatio = ((latest['high'] - body_max) / high_low_diff * 100) if high_low_diff > 0 else 0.0

        # 今日漲幅
        todayChangePercent = q['change_pct'] if q else ((latest['close'] - prev['close']) / prev['close'] * 100)

        # MACD 歷史資料與收斂判斷
        macdHistogram     = latest['macd_hist']
        macdHistogramPrev1 = sub_df.iloc[-2]['macd_hist']
        macdHistogramPrev2 = sub_df.iloc[-3]['macd_hist']
        macdHistogramPrev3 = sub_df.iloc[-4]['macd_hist']
        
        # 負柱連續 2~3 日收斂 (即負柱越來越接近 0)
        macd_shrinking = (macdHistogram < 0) and (macdHistogram > macdHistogramPrev1 > macdHistogramPrev2)

        # 籌碼面指標計算
        code_inst = df_inst[df_inst['code'] == code].copy()
        foreign_strike    = 0
        investment_strike = 0
        sync_buy          = False
        dealer_buy        = False
        inst_ratio_5d     = 0.0
        
        foreignBuy5 = 0
        investmentTrustBuy5 = 0
        dealerBuy5 = 0
        totalInstitutionBuy5 = 0

        if not code_inst.empty:
            inst_list = code_inst.tail(10).to_dict('records')
            
            # 連續買超天數
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
                sync_buy = (last_r['foreign_buy'] > 0 and last_r['investment_buy'] > 0 and last_r['dealer_buy'] > 0)
                dealer_buy = last_r['dealer_buy'] > 0

            # 近 5 日法人買賣超 (張)
            last_5_inst  = inst_list[-5:] if len(inst_list) >= 5 else inst_list
            foreignBuy5  = sum(r['foreign_buy'] for r in last_5_inst)
            investmentTrustBuy5 = sum(r['investment_buy'] for r in last_5_inst)
            dealerBuy5   = sum(r['dealer_buy'] for r in last_5_inst)
            totalInstitutionBuy5 = foreignBuy5 + investmentTrustBuy5 + dealerBuy5
            
            # Shioaji 日K volume 單位為張；法人買超在 sync 時已除以 1000 轉換為張
            # 法人佔比 = 近5日法人買超張數 / 近5日成交量張數 * 100
            last_5_kbars = sub_df.tail(len(last_5_inst)).to_dict('records')
            total_vol_lots = sum(r['volume'] for r in last_5_kbars)  # 已是張
            if total_vol_lots > 0:
                inst_ratio_5d = (totalInstitutionBuy5 / total_vol_lots) * 100

        if code == TRACE_CODE:
            _trace(f"institutionBuyRatio5={inst_ratio_5d:.2f}")
            if inst_ratio_5d > 30.0:
                _trace(f"highInstRatioWarning=true warning=法人佔比偏高，請確認成交金額與流動性")

        # 原有籌碼階級劃分
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

        # ── 新增功能二 & 七：買點型態偵測與停損風險管理 ──────────────────────
        is_breakout = (latest['close'] > recentHigh10) or (latest['close'] > recentHigh20)
        is_near_ma20 = (abs(latest['close'] - latest['ma20']) / latest['ma20'] < 0.03) and (latest['low'] >= latest['ma20'] * 0.98)
        is_near_prior_high = (abs(latest['close'] - recentHigh20) / recentHigh20 < 0.03)
        is_high_consolidation = (latest['close'] >= recentHigh20 * 0.97) and ((recentHigh10 - recentLow10) / recentLow10 < 0.08)

        # 預設買點型態與停損價
        entry_pattern = "創高後高檔整理"
        entry_pattern_label = "🚀 高檔續強"
        stopLossPrice = latest['ma20']

        if bias20 > 15.0 or return5 > 15.0 or candleUpperShadowRatio > 40.0:
            entry_pattern = "過熱不交易"
            entry_pattern_label = "⚠️ 過熱"
            stopLossPrice = 0.0
        elif bias20 > 10.0:
            entry_pattern = "乖離過大等回測"
            entry_pattern_label = "⏳ 等回測"
            stopLossPrice = latest['ma20']
        elif is_near_ma20 and (latest['close'] > latest['open'] or latest['close'] > latest['ma5'] or latest['close'] > latest['ma10']):
            entry_pattern = "回測 20MA 轉強"
            entry_pattern_label = "📍 回測20MA"
            stopLossPrice = min(latest['ma20'], sub_df.iloc[-5:]['low'].min())
        elif is_near_prior_high and latest['close'] >= recentHigh20 * 0.97:
            entry_pattern = "回測前高不破"
            entry_pattern_label = "📍 回測前高"
            stopLossPrice = recentHigh20 * 0.97
        elif is_breakout and latest['volume'] > latest['volume_ma20'] * 1.2:
            entry_pattern = "突破整理區"
            entry_pattern_label = "🚀 突破整理"
            stopLossPrice = latest['low']
        elif is_high_consolidation and latest['close'] >= recentHigh20 * 0.98:
            entry_pattern = "創高後高檔整理"
            entry_pattern_label = "🚀 高檔續強"
            stopLossPrice = sub_df.iloc[-10:]['low'].min()

        # 停損距離 (負值百分比，例如 -4.5%)
        if stopLossPrice > 0:
            stopLossPercent = ((stopLossPrice - latest['close']) / latest['close']) * 100
        else:
            stopLossPercent = 0.0

        # ── ScoreEngine：明日交易分數（含明細） ────────────────────────────
        breakdown = []  # [{label, passed, delta, detail}]
        score = 0
        abs_sl = abs(stopLossPercent)

        def _item(label, passed, delta, detail=""):
            breakdown.append({"label": label, "passed": bool(passed), "delta": int(delta), "detail": str(detail)})
            return delta if passed else 0

        # ── 加分項目 ──
        # 1. 趨勢多頭 +20
        score += _item("趨勢多頭", latest['close'] > latest['ma20'] > latest['ma60'], 20,
                       "收盤價 > 20MA > 60MA，中短期多頭排列")

        # 2. 20日相對強度 +10
        rs20 = return20 - index_gain_20
        score += _item("20日相對強度", rs20 > 1.5, 10,
                       f"近20日漲幅超越大盤 {rs20:+.1f}%（需 > +1.5%）")

        # 3. 60日相對強度 +10（加分條件，非硬篩）
        score += _item("60日相對強度", return60 > index_gain_60, 10,
                       f"近60日漲幅 {return60:+.1f}%（> {index_gain_60}% 加分，否則不扣分）")

        # ── 法人分數組合（上限 25 分，防止過度加權）────────────────────────
        _inst_raw = 0
        # 4. 投信近5日買超 +10
        v = _item("投信近5日買超", investmentTrustBuy5 > 0, 10,
                  f"投信近5日合計 {investmentTrustBuy5:+} 張")
        _inst_raw += v; score += v

        # 5. 外資近5日買超 +8
        v = _item("外資近5日買超", foreignBuy5 > 0, 8,
                  f"外資近5日合計 {foreignBuy5:+} 張")
        _inst_raw += v; score += v

        # 6. 三大法人合計買超 +7
        v = _item("三大法人買超", totalInstitutionBuy5 > 0, 7,
                  f"三大法人近5日合計 {totalInstitutionBuy5:+} 張")
        _inst_raw += v; score += v

        # 6b. 外資與投信同步買超 +5
        _both_inst = (foreignBuy5 > 0 and investmentTrustBuy5 > 0)
        v = _item("外資投信同步買超", _both_inst, 5,
                  f"外資{foreignBuy5:+}張、投信{investmentTrustBuy5:+}張，雙向同步進場")
        _inst_raw += v; score += v

        # 法人分數上限截斷（最高 25 分）
        _INST_CAP = 25
        if _inst_raw > _INST_CAP:
            _inst_excess = _inst_raw - _INST_CAP
            breakdown.append({"label": "法人分數上限截斷", "passed": False,
                               "delta": -_inst_excess,
                               "detail": f"法人小計{_inst_raw}分超過上限{_INST_CAP}分，截斷{_inst_excess}分"})
            score -= _inst_excess
        _inst_score_final = min(_inst_raw, _INST_CAP)

        # 主力特徵標籤（依近5日合計判斷）
        if foreignBuy5 > 0 and investmentTrustBuy5 > 0 and totalInstitutionBuy5 > 0:
            institution_label = "三人同買"
        elif foreignBuy5 > 0 and investmentTrustBuy5 <= 0 and totalInstitutionBuy5 > 0:
            institution_label = "外資主導"
        elif investmentTrustBuy5 > 0 and foreignBuy5 <= 0 and totalInstitutionBuy5 > 0:
            institution_label = "投信主導"
        else:
            institution_label = "--"

        # 7. 乖離位置分數（漸進式）
        if 0.0 <= bias20 <= 3.0:
            bias_delta, bias_passed, bias_detail = 10, True, f"乖離 {bias20:.1f}%，0%~3% 最佳位置"
        elif 3.0 < bias20 <= 6.0:
            bias_delta, bias_passed, bias_detail = 8, True, f"乖離 {bias20:.1f}%，3%~6% 安全位置"
        elif 6.0 < bias20 <= 10.0:
            bias_delta, bias_passed, bias_detail = 4, True, f"乖離 {bias20:.1f}%，6%~10% 偏高"
        elif 10.0 < bias20 <= 15.0:
            bias_delta, bias_passed, bias_detail = -15, False, f"乖離 {bias20:.1f}%，10%~15% 過高扣分"
        elif bias20 > 15.0:
            bias_delta, bias_passed, bias_detail = -25, False, f"乖離 {bias20:.1f}%，>15% 嚴重過熱扣分"
        else:
            bias_delta, bias_passed, bias_detail = 0, False, f"乖離 {bias20:.1f}%，低於0% 不加分"
        breakdown.append({"label": "乖離20MA位置", "passed": bias_passed, "delta": bias_delta, "detail": bias_detail})
        score += bias_delta

        # 8. MACD 負柱連續收斂 +10
        score += _item("MACD負柱收斂", macd_shrinking, 10,
                       "MACD histogram 為負且連續2~3日縮短，空方動能減弱")

        # 9. K線轉強 +10
        candle_strong = (latest['close'] > latest['open']) or (latest['close'] > latest['ma5']) or \
                        (latest['close'] > latest['ma10']) or (latest['close'] > prev['high'])
        score += _item("K線轉強", candle_strong, 10,
                       "今日收紅K、站回5MA/10MA，或突破昨日高點")

        # 10. 成交金額流動性（漸進式加分，最高 +10）
        _ama5_val  = _ama5  or 0.0
        _ama20_val = _ama20 or 0.0
        if _liq_data_missing:
            liq_delta       = 0
            liq_detail      = "成交金額資料缺失，無法計算流動性"
            liquidity_label  = "成交金額資料缺失"
            liquidity_reason = f"amountMa5=None，amountMa20=None，門檻={_LIQ_THRESHOLD:,}"
        elif _ama5_val >= 300_000_000:
            liq_delta       = 10
            liq_detail      = f"5日均額 {_ama5_val/1e8:.2f}億，流動性充足"
            liquidity_label  = "流動性充足"
            liquidity_reason = f"通過：amountMa5={_ama5_val:,.0f} >= {_LIQ_THRESHOLD:,}"
        elif _ama5_val >= 100_000_000:
            liq_delta       = 8
            liq_detail      = f"5日均額 {_ama5_val/1e8:.2f}億，流動性良好"
            liquidity_label  = "流動性良好"
            liquidity_reason = f"通過：amountMa5={_ama5_val:,.0f} >= {_LIQ_THRESHOLD:,}"
        elif _ama5_val >= 50_000_000:
            liq_delta       = 5
            liq_detail      = f"5日均額 {_ama5_val/1e7:.1f}千萬，流動性普通，注意滑價"
            liquidity_label  = "流動性普通，注意滑價"
            liquidity_reason = f"通過：amountMa5={_ama5_val:,.0f} >= {_LIQ_THRESHOLD:,}"
        elif _ama5_val >= _LIQ_THRESHOLD or _ama20_val >= _LIQ_THRESHOLD:
            liq_delta       = 4
            liq_detail      = f"5日均額 {_ama5_val/1e6:.0f}萬（20日均額 {_ama20_val/1e6:.0f}萬），流動性偏低"
            liquidity_label  = "流動性偏低，注意滑價"
            liquidity_reason = (f"通過(金額)：amountMa5={_ama5_val:,.0f}，"
                                f"amountMa20={_ama20_val:,.0f}，門檻={_LIQ_THRESHOLD:,}")
        elif _vol_ma20_val >= _LIQ_VOLUME_THRESHOLD:
            liq_delta       = 3
            liq_detail      = f"近20日均量 {_vol_ma20_val:.0f}張，金額偏低但量能尚可"
            liquidity_label  = "量能普通，金額偏低"
            liquidity_reason = f"通過(量)：volumeMa20={_vol_ma20_val:.0f}張 >= {_LIQ_VOLUME_THRESHOLD}張"
        else:
            liq_delta       = 0
            liq_detail      = "流動性不足（此處不應出現，已由前置篩選排除）"
            liquidity_label  = "流動性不足"
            liquidity_reason = "通過路徑不明"
        score += _item("成交金額流動性", liq_delta > 0, liq_delta, liq_detail)

        # 11. 停損距離分數（漸進式）
        if abs_sl <= 3.0:
            sl_delta, sl_passed, sl_detail = 10, True, f"停損距離 {abs_sl:.1f}%，0%~3% 低風險"
        elif abs_sl <= 5.0:
            sl_delta, sl_passed, sl_detail = 7, True, f"停損距離 {abs_sl:.1f}%，3%~5% 可接受"
        elif abs_sl <= 6.0:
            sl_delta, sl_passed, sl_detail = 3, True, f"停損距離 {abs_sl:.1f}%，5%~6% 偏高"
        elif abs_sl <= 8.0:
            sl_delta, sl_passed, sl_detail = -10, False, f"停損距離 {abs_sl:.1f}%，6%~8% 扣分"
        else:
            sl_delta, sl_passed, sl_detail = -20, False, f"停損距離 {abs_sl:.1f}%，>8% 不建議"
        breakdown.append({"label": "停損距離", "passed": sl_passed, "delta": sl_delta, "detail": sl_detail})
        score += sl_delta

        # ── 扣分項目 ──
        if return5 > 15.0:
            score += _item("近5日漲幅過大", True, -10, f"近5日漲幅 {return5:.1f}% > 15%，短線可能過熱")
        else:
            _item("近5日漲幅過大", False, -10, f"近5日漲幅 {return5:.1f}%，正常")

        if todayChangePercent > 6.0:
            score += _item("今日急漲扣分", True, -10, f"今日漲幅 {todayChangePercent:.1f}% > 6%，隔日追價風險高")
        else:
            _item("今日急漲扣分", False, -10, f"今日漲幅 {todayChangePercent:.1f}%，正常")

        if candleUpperShadowRatio > 40.0:
            score += _item("長上影線", True, -10, f"上影線比例 {candleUpperShadowRatio:.0f}% > 40%，上方賣壓重")
        else:
            _item("長上影線", False, -10, f"上影線比例 {candleUpperShadowRatio:.0f}%，正常")

        if totalInstitutionBuy5 > 0 and todayChangePercent <= 0:
            score += _item("法人買超但股價不漲", True, -10, "法人買超但今日未上漲，可能籌碼被吸收")
        else:
            _item("法人買超但股價不漲", False, -10, "無此狀況")

        score = max(0, min(100, score))

        # ── 主力特徵加分（輔助加分，不覆蓋風險條件，上限 +8）─────────────
        major_features = []
        major_bonus_raw = 0

        # 近5日法人標籤加入主力特徵
        if institution_label in ("三人同買", "外資主導", "投信主導"):
            major_features.append(institution_label)
            if institution_label == "三人同買":
                major_bonus_raw += 3
            else:
                major_bonus_raw += 1
        elif sync_buy:
            major_features.append("三人同買")
            major_bonus_raw += 3
        if tier_name == "黃金滿貫":
            major_features.append("黃金滿貫")
            major_bonus_raw += 3
        elif tier_name == "強勢雙雄":
            major_features.append("強勢雙雄")
            major_bonus_raw += 2
        if investment_strike >= 5:
            major_features.append(f"投信連買{investment_strike}D")
            major_bonus_raw += 3
        elif investment_strike >= 3:
            major_features.append(f"投信連買{investment_strike}D")
            major_bonus_raw += 2
        if foreign_strike >= 5:
            major_features.append(f"外資連買{foreign_strike}D")
            major_bonus_raw += 3
        elif foreign_strike >= 3:
            major_features.append(f"外資連買{foreign_strike}D")
            major_bonus_raw += 2

        major_bonus = min(8, major_bonus_raw)

        if major_features:
            cap_note = f"（原始+{major_bonus_raw}，上限截至+{major_bonus}）" if major_bonus < major_bonus_raw else ""
            breakdown.append({
                "label": "主力特徵加分",
                "passed": True,
                "delta": major_bonus,
                "detail": f"觸發：{'、'.join(major_features)}{cap_note}"
            })
        else:
            breakdown.append({
                "label": "主力特徵加分",
                "passed": False,
                "delta": 0,
                "detail": "無主力特徵"
            })

        # ── 策略狀態分類（依計畫書優先順序） ──────────────────────────────
        # 過熱警戒先行判定（不依賴分數，基於風險條件）
        is_overheat = (bias20 > 15.0) or (return5 > 20.0) or (todayChangePercent > 7.0) or \
                      (candleUpperShadowRatio > 40.0) or (abs_sl > 8.0)

        # 主力特徵加分：過熱警戒時僅顯示，不加成分數與策略狀態
        if not is_overheat:
            score = min(100, score + major_bonus)

        is_pullback = (bias20 > 10.0) or (abs_sl > 6.0 and not is_overheat)
        is_breakout_cond = is_breakout and (latest['volume'] > latest['volume_ma20'] * 1.2) and \
                           (todayChangePercent <= 7.0) and (bias20 < 12.0)

        if is_overheat:
            strategy_state = "過熱警戒"
            strategy_state_label = "🔴 過熱警戒"
            entry_pattern_label = "⚠️ 過熱"
            entry_pattern = "過熱不交易"
            stopLossPrice = 0.0
            stopLossPercent = 0.0
        elif is_pullback:
            strategy_state = "等回測"
            strategy_state_label = "🟡 等回測"
            entry_pattern_label = "⏳ 等回測"
            entry_pattern = "乖離過大等回測"
        elif _ms_code == 'weak_market':
            # 轉弱盤不產生明日優先，直接往下走
            pass
        elif (_ms_code == 'overheated_bull'
              and score >= 85 and bias20 <= 3.0 and abs_sl <= 5.0 and _liq_passed
              and latest['close'] > latest['ma20'] > latest['ma60']
              and entry_pattern in ("回測 20MA 轉強",)
              and todayChangePercent <= 3.0 and return5 <= 10.0):
            strategy_state = "明日優先"
            strategy_state_label = "🟢 明日優先"
        elif (_ms_code == 'hot_bull'
              and score >= 82 and bias20 <= 4.0 and abs_sl <= 6.0 and _liq_passed
              and latest['close'] > latest['ma20'] > latest['ma60']
              and entry_pattern in ("回測 20MA 轉強", "回測前高不破")):
            strategy_state = "明日優先"
            strategy_state_label = "🟢 明日優先"
        elif (_ms_code == 'normal_bull'
              and score >= 80 and bias20 <= 6.0 and abs_sl <= 6.0 and _liq_passed
              and latest['close'] > latest['ma20'] > latest['ma60']):
            strategy_state = "明日優先"
            strategy_state_label = "🟢 明日優先"
        elif (score >= 65 or is_breakout_cond) and _liq_passed:
            strategy_state = "突破觀察"
            strategy_state_label = "🔵 突破觀察"
        elif score >= 50:
            strategy_state = "等回測"
            strategy_state_label = "🟡 等回測"
            entry_pattern_label = "⏳ 等回測"
            entry_pattern = "乖離過大等回測"
        else:
            strategy_state = "暫不交易"
            strategy_state_label = "⚪ 暫不交易"

        # ── 新增功能六：明日操作計畫與說明生成 ─────────────────────────────────
        strategy_reason = ""
        action_plan = {}

        # 具體數值：供操作文案嵌入實際價格
        _prev_high = round(float(prev['high']), 2)
        _ma5  = round(float(latest['ma5']),  2) if not pd.isna(latest.get('ma5',  float('nan'))) else 0.0
        _ma10 = round(float(latest['ma10']), 2) if not pd.isna(latest.get('ma10', float('nan'))) else 0.0
        _ma20 = round(float(latest['ma20']), 2)
        _sl   = round(stopLossPrice, 2)
        _rh10 = round(float(recentHigh10), 2) if not pd.isna(recentHigh10) else round(float(latest['high']), 2)
        _rh20 = round(float(recentHigh20), 2) if not pd.isna(recentHigh20) else round(float(latest['high']), 2)

        if strategy_state == "明日優先":
            strategy_reason = f"股價均線多頭，且月線乖離率僅 {bias20:.2f}%（處於安全區）；近5日法人連續鎖碼，MACD 負柱狀連續收斂，且停損風險僅 {stopLossPercent:.2f}%，極具明日交易價值。"
            if entry_pattern in ("高檔續強", "創高後高檔整理"):
                action_plan = {
                    "strategy": "高檔續強：優先觀察 5MA 支撐與前高突破",
                    "conservative": f"拉回 5MA（{_ma5}）至 10MA（{_ma10}）區間不破，重新站回 5MA 後進場",
                    "aggressive": f"放量突破前一交易日高點 {_prev_high} 時積極進場",
                    "avoid": f"開盤跳空過高停損距離 > 6%、跌破 5MA（{_ma5}）無法站回、突破失敗出現長上影線",
                    "stopLoss": f"跌破 10MA（{_ma10}）即停損，最終守位 20MA（{_ma20}）"
                }
            elif entry_pattern == "回測 20MA 轉強":
                action_plan = {
                    "strategy": "回測20MA轉強：等待20MA支撐確認",
                    "conservative": f"拉回 20MA（{_ma20}）不破，重新站回 20MA 或 5MA（{_ma5}）後進場",
                    "aggressive": f"突破前一交易日高點 {_prev_high} 時積極進場",
                    "avoid": f"跌破 20MA（{_ma20}）後收盤無法站回、或停損距離 > 6%",
                    "stopLoss": f"跌破 20MA（{_ma20}）或回測低點 {_sl} 停損"
                }
            elif entry_pattern == "回測前高不破":
                action_plan = {
                    "strategy": "回測前高：等待前高支撐轉強確認",
                    "conservative": f"回測前高（{_rh20}）不破，站回 5MA（{_ma5}）後進場",
                    "aggressive": f"突破前一交易日高點 {_prev_high} 時積極進場",
                    "avoid": f"跌破前高支撐 {_rh20} 收盤不站回、或停損距離 > 6%",
                    "stopLoss": f"跌破前高 {_rh20} 或停損價 {_sl} 停損"
                }
            elif entry_pattern == "突破整理區":
                action_plan = {
                    "strategy": "突破整理：明日確認突破有效性後進場",
                    "conservative": f"回測近10日整理高點（{_rh10}）不破，量能維持時進場",
                    "aggressive": f"放量再次突破前一交易日高點 {_prev_high} 時積極進場",
                    "avoid": f"突破後縮量回跌破整理區（{_rh10} 以下）、或停損距離 > 6%",
                    "stopLoss": f"跌回整理區或跌破突破K線低點 {_sl} 停損"
                }
            else:
                action_plan = {
                    "strategy": "可優先觀察進場",
                    "conservative": f"拉回 5MA（{_ma5}）至 10MA（{_ma10}）不破，站回 5MA 後進場",
                    "aggressive": f"突破前一交易日高點 {_prev_high} 時積極進場",
                    "avoid": f"開盤跳空過高停損距離 > 6%、跌破 5MA（{_ma5}）無法站回",
                    "stopLoss": f"跌破停損價 {_sl}（或 20MA {_ma20}）停損"
                }
        elif strategy_state == "突破觀察":
            strategy_reason = f"股價帶量突破近 10-20 日整理平台，主力買氣強烈。但突破當日不宜過度追高，需明日確認突破之有效性。"
            if entry_pattern == "突破整理區":
                action_plan = {
                    "strategy": "突破整理：等待突破有效確認後進場",
                    "conservative": f"明日回測近10日整理高點（{_rh10}）不破，量能維持時進場",
                    "aggressive": f"放量再次突破前一交易日高點 {_prev_high} 時積極進場",
                    "avoid": f"突破後縮量回跌破整理區（{_rh10} 以下）、不可直接追高",
                    "stopLoss": f"跌回整理區平台或跌破突破K線低點 {_sl} 停損"
                }
            else:
                action_plan = {
                    "strategy": "等待突破有效確認",
                    "conservative": f"明日回測 5MA（{_ma5}）至 10MA（{_ma10}）不破，量能維持時進場",
                    "aggressive": f"放量突破前一交易日高點 {_prev_high} 時積極進場",
                    "avoid": f"突破後縮量、跌破 5MA（{_ma5}）無法站回、需觀察五檔買氣及開盤強弱",
                    "stopLoss": f"跌破突破K線低點 {_sl}（或 10MA {_ma10}）停損"
                }
        elif strategy_state == "等回測":
            strategy_reason = f"主力籌碼與技術趨勢強勁，但目前月線乖離率高達 {bias20:.2f}%（大於 10%）。此時追價極易套牢，應放自選等待縮量回試支撐。"
            action_plan = {
                "strategy": "不追價，放入自選監控等待拉回",
                "conservative": f"等待拉回 20MA（{_ma20}）量縮整理後，站回 5MA（{_ma5}）再進場",
                "aggressive": "暫不建議積極追價",
                "avoid": f"目前乖離偏高（{bias20:.1f}%），嚴禁追高，等回測不破再評估",
                "stopLoss": f"若進場則守 20MA（{_ma20}），跌破 {_sl} 停損"
            }
        elif strategy_state == "過熱警戒":
            strategy_reason = f"月線乖離率已達 {bias20:.2f}% 且近5日漲幅過巨（{return5:.2f}%），或今日出現長上影線。短線多頭極度消耗，隨時有獲利回吐賣壓。"
            action_plan = {
                "strategy": "暫不交易，全面避開",
                "conservative": "無進場規劃",
                "aggressive": "無進場規劃",
                "avoid": f"短線過熱（乖離 {bias20:.1f}%、近5日漲幅 {return5:.1f}%），強制限制所有交易，耐心等待整理冷卻",
                "stopLoss": "無進場規劃，故無停損點"
            }
        else:
            strategy_reason = "技術或籌碼動能暫時未達最佳可交易狀態，建議於場外觀望。"
            action_plan = {
                "strategy": "場外觀望",
                "conservative": "暫不符合進場條件",
                "aggressive": "暫不符合進場條件",
                "avoid": "此標的暫不符合強勢起漲策略，建議場外觀望",
                "stopLoss": "無進場規劃"
            }

        if code == TRACE_CODE:
            _trace(f"finalIncluded=true reason=通過所有篩選條件 score={score} strategyState={strategy_state}")

        # ── 建議買法（依大盤狀態）────────────────────────────────────
        _bm_input = {
            'entryPattern':       entry_pattern,
            'bias20':             bias20,
            'stopLossPercent':    stopLossPercent,
            'todayChangePercent': todayChangePercent,
            'return5':            return5,
            'ma20':               float(latest['ma20']),
            'stopLossPrice':      stopLossPrice,
        }
        buy_method = _ms.determine_buy_method(_bm_input, _market_status)

        # ── 新增 Debug / 候選分類欄位 ─────────────────────────────────────
        # 近3日是否曾回測20MA並站回（低點 <= 20MA×1.005 且收盤 > 20MA×0.99）
        _recently_tested_ma20 = False
        for _ri in range(-4, -1):
            if len(sub_df) > abs(_ri):
                _past = sub_df.iloc[_ri]
                _m20i = sub_df['ma20'].iloc[_ri]
                if pd.notna(_m20i) and float(_past['low']) <= float(_m20i) * 1.005 and float(_past['close']) > float(_m20i) * 0.99:
                    _recently_tested_ma20 = True
                    break

        # 距60MA乖離
        _dist_cost60_pct = round(((float(latest['close']) - float(latest['ma60'])) / float(latest['ma60'])) * 100, 2)

        # 近60日壓力位與風報比
        _rh60 = float(recentHigh60) if not pd.isna(recentHigh60) else float(latest['close'])
        _is_near_resistance60 = float(latest['close']) >= _rh60 * 0.98
        if _is_near_resistance60 or stopLossPrice <= 0:
            _risk_reward = None
        else:
            _rr_reward = _rh60 - float(latest['close'])
            _rr_risk   = float(latest['close']) - stopLossPrice
            _risk_reward = round(_rr_reward / _rr_risk, 2) if (_rr_risk > 0 and _rr_reward > 0) else None

        # MACD 狀態
        if macdHistogram > 0:
            _macd_status = 'positive_expanding' if macdHistogram >= macdHistogramPrev1 else 'positive_converging'
        else:
            _macd_status = 'negative_converging' if macd_shrinking else 'negative_expanding'

        # 成交量狀態
        _vm20v = _vol_ma20_val  # 已於流動性篩選計算
        _vr = float(latest['volume']) / _vm20v if _vm20v > 0 else 1.0
        if   _vr > 1.5: _vol_status = 'heavy'
        elif _vr > 1.1: _vol_status = 'expanding'
        elif _vr < 0.7: _vol_status = 'contracting'
        else:           _vol_status = 'normal'

        # 股票類型
        _sn_tmp = (q.get('name') if (q and q.get('name')) else None) or db_names.get(code) or STOCK_NAMES.get(code, "")
        _stock_type = _detect_stock_type(code, _sn_tmp, db_categories.get(code, ''))

        # 股票等級：A（近20MA）/ B1（守60整理）/ B2（偏高）
        _is_near_cost20 = (abs(bias20) <= 3.0) or _recently_tested_ma20
        _stock_grade = 'A' if _is_near_cost20 else ('B1' if abs(bias20) <= 10.0 else 'B2')

        # 裝填完整 40+ 欄位資料
        results.append({
            "stockCode": code,
            "stockName": (q.get('name') if (q and q.get('name')) else None) or db_names.get(code) or STOCK_NAMES.get(code, "未知"),
            "industry": db_categories.get(code, ''),
            "closePrice": float(latest['close']),
            "openPrice": float(latest['open']),
            "highPrice": float(latest['high']),
            "lowPrice": float(latest['low']),
            "previousClosePrice": float(prev['close']),
            "previousHighPrice": float(prev['high']),
            "ma5": float(latest['ma5']),
            "ma10": float(latest['ma10']),
            "ma20": float(latest['ma20']),
            "ma60": float(latest['ma60']),
            "bias20": round(float(bias20), 2),
            "return5": round(float(return5), 2),
            "return20": round(float(return20), 2),
            "return60": round(float(return60), 2),
            "marketReturn20": float(index_gain_20),
            "marketReturn60": float(index_gain_60),
            "foreignBuy5": int(foreignBuy5),
            "investmentTrustBuy5": int(investmentTrustBuy5),
            "dealerBuy5": int(dealerBuy5),
            "totalInstitutionBuy5": int(totalInstitutionBuy5),
            "institutionBuyRatio5": round(float(inst_ratio_5d), 2),
            "foreignConsecutiveBuyDays": int(foreign_strike),
            "investmentTrustConsecutiveBuyDays": int(investment_strike),
            "volume": int(latest['volume']),
            "volume5": int(sub_df.iloc[-5:]['volume'].sum()),
            "volumeMa20": int(latest['volume_ma20']) if not pd.isna(latest.get('volume_ma20', float('nan'))) else 0,
            "amountToday": _amt_today,
            "amount": _amt_today,
            "amountMa5": _ama5_val,
            "amountMa20": _ama20_val,
            "amountUnit": _AMOUNT_UNIT,
            "volumeMa5": float(sub_df.iloc[-5:]['volume'].mean()),
            "volumeUnit": _VOLUME_UNIT,
            "liquidityThreshold": _LIQ_THRESHOLD,
            "liquidityLabel": liquidity_label,
            "liquidityPassed": _liq_passed,
            "liquidityReason": liquidity_reason,
            "todayChangePercent": round(float(todayChangePercent), 2),
            "candleUpperShadowRatio": round(float(candleUpperShadowRatio), 2),
            "macdHistogram": round(float(macdHistogram), 4),
            "macdHistogramPrev1": round(float(macdHistogramPrev1), 4),
            "macdHistogramPrev2": round(float(macdHistogramPrev2), 4),
            "macdHistogramPrev3": round(float(macdHistogramPrev3), 4),
            "recentHigh10": float(recentHigh10) if not pd.isna(recentHigh10) else float(latest['high']),
            "recentHigh20": float(recentHigh20) if not pd.isna(recentHigh20) else float(latest['high']),
            "recentLow10": float(recentLow10) if not pd.isna(recentLow10) else float(latest['low']),
            "recentLow20": float(recentLow20) if not pd.isna(recentLow20) else float(latest['low']),
            "supportPrice": float(recentHigh20) if not pd.isna(recentHigh20) else float(latest['close']),
            "stopLossPrice": float(stopLossPrice),
            "stopLossPercent": round(float(stopLossPercent), 2),
            
            # 策略大腦決策衍生資料
            "score": int(score),
            "strategyState": strategy_state,
            "strategyStateLabel": strategy_state_label,
            "entryPattern": entry_pattern,
            "entryPatternLabel": entry_pattern_label,
            "strategyReason": strategy_reason,
            "actionPlan": action_plan,
            
            # 原有欄位保持相容性，以防前端未修改部分出錯
            "bias": round(float(bias20), 2),
            "gain_20": round(float(return20), 2),
            "gain_60": round(float(return60), 2),
            "investment_strike": int(investment_strike),
            "foreign_strike": int(foreign_strike),
            "sync_buy": bool(sync_buy),
            "inst_ratio_5d": round(float(inst_ratio_5d), 2),
            "mention_count": 0,
            "priority": int(score), # 改用交易分數作為總體優先度指標
            "tier_level": int(tier_level),
            "tier_name": str(tier_name),
            "majorFeatures": major_features,
            "majorBonus": int(major_bonus),
            "scoreBreakdown": breakdown,
            "institutionLabel": institution_label,
            "institutionScoreFinal": int(_inst_score_final),
            "highInstRatioWarning": bool(inst_ratio_5d > 30.0),
            # 大盤狀態與建議買法
            "buy_method": buy_method,
            "marketStatus": _ms_code,
            "marketStatusLabel": _market_status.get('label', ''),
            # ── Debug / 候選分類欄位（七）────────────────────────────────
            "stockType": _stock_type,
            "stockGrade": _stock_grade,
            "isNearCost20": _is_near_cost20,
            "recentlyTestedMa20": _recently_tested_ma20,
            "distCost20Pct": round(float(bias20), 2),
            "distCost60Pct": _dist_cost60_pct,
            "cost20": round(float(latest['ma20']), 2),
            "cost60": round(float(latest['ma60']), 2),
            "resistance60d": round(_rh60, 2) if not _is_near_resistance60 else None,
            "isNearResistance60": _is_near_resistance60,
            "riskRewardRatio": _risk_reward,
            "macdStatus": _macd_status,
            "volumeStatus": _vol_status,
            "volumeMa20Debug": int(_vm20v) if _vm20v > 0 else 0,
            "isOverheated": is_overheat,
            "candidateType": "",
            "includeReason": "",
            "excludeReason": "",
        })

    # ── 排序前計算產業共振 ────────────────────────────────────────────
    if results:
        compute_industry_rankings(results)

    # ── 分離商品類型 ──────────────────────────────────────────────────
    _excluded_types = {'warrant', 'etn', 'preferred'}
    etf_results    = [s for s in results if s.get('stockType') == 'etf']
    type_excluded  = [s for s in results if s.get('stockType') in _excluded_types]
    common_results = [s for s in results if s.get('stockType') not in _excluded_types | {'etf'}]

    # ── 改進排序鍵 ────────────────────────────────────────────────────
    # 1.分數高 2.風報比高 3.距20MA近 4.MACD負柱收斂優先 5.量縮優先 6.成交金額 7.停損小
    _sp_map = {"明日優先": 1, "突破觀察": 2, "等回測": 3, "過熱警戒": 4, "暫不交易": 5}
    _macd_ord  = {'negative_converging': 0, 'positive_converging': 1, 'flat': 2,
                  'negative_expanding': 3, 'positive_expanding': 4}
    _vol_ord   = {'contracting': 0, 'normal': 1, 'expanding': 2, 'heavy': 3}

    def _sort_key(x):
        return (
            0 if (x.get("buy_method") or {}).get("allowed") else 1,
            _sp_map.get(x.get("strategyState", ""), 99),
            -x.get("score", 0),
            0 if x.get("hasIndustryResonance") else 1,
            -(x.get("riskRewardRatio") or 0),
            abs(x.get("bias20", 0)),
            _macd_ord.get(x.get("macdStatus", ""), 2),
            _vol_ord.get(x.get("volumeStatus", ""), 1),
            -(x.get("amountMa5") or x.get("amountMa20") or 0),
            abs(x.get("stopLossPercent", 0)),
        )

    common_results.sort(key=_sort_key)
    etf_results.sort(key=_sort_key)

    # ── 候選分類（candidate_type）────────────────────────────────────
    buy_candidates      = []
    high_priority_watch = []
    other_watch         = []
    excluded_list       = []

    for s in common_results:
        _ss    = s.get('strategyState', '')
        _b20   = float(s.get('bias20', 0))
        _rtnm  = s.get('recentlyTestedMa20', False)
        _ms_s  = s.get('macdStatus', '')
        _is_ov = s.get('isOverheated', False)

        # 過熱警戒 → 排除
        if _ss == '過熱警戒' or _is_ov:
            s['candidateType'] = '排除'
            s['excludeReason'] = f"過熱警戒 bias20={_b20:.1f}%"
            excluded_list.append(s)
            continue

        if _ss == '明日優先':
            # bias > 8% → 過熱排除
            if _b20 > 8.0:
                s['candidateType'] = '排除'
                s['excludeReason'] = f"bias20={_b20:.1f}%>8%，強多延伸盤過熱排除"
                excluded_list.append(s)
            elif _ms_code == 'normal_bull':
                # 強多延伸盤嚴格條件
                if _b20 > 5.0:
                    # 5-8%：強多盤不進可買，降為高優先觀察
                    s['includeReason'] = f"normal_bull bias20={_b20:.1f}%>5%，不列可買"
                    if len(high_priority_watch) < _MAX_HIGH_PRIORITY_WATCH:
                        s['candidateType'] = '高優先觀察'; high_priority_watch.append(s)
                    else:
                        s['candidateType'] = '其他觀察'; other_watch.append(s)
                elif _ms_s == 'positive_expanding' and _b20 > 3.0:
                    # MACD正柱放大且偏離3% → 不算可買
                    s['includeReason'] = f"MACD正柱放大且bias={_b20:.1f}%>3%，追高觀察"
                    if len(high_priority_watch) < _MAX_HIGH_PRIORITY_WATCH:
                        s['candidateType'] = '高優先觀察'; high_priority_watch.append(s)
                    else:
                        s['candidateType'] = '其他觀察'; other_watch.append(s)
                elif _b20 <= 3.0 or (_rtnm and _b20 <= 5.0):
                    # A級（bias<=3%）或近期回測站回（bias<=5%）→ 可買
                    if len(buy_candidates) < _MAX_BUY_CANDIDATES:
                        s['candidateType'] = '明日可買'
                        s['includeReason'] = f"A級 bias={_b20:.1f}% grade={s.get('stockGrade')}"
                        buy_candidates.append(s)
                    elif len(high_priority_watch) < _MAX_HIGH_PRIORITY_WATCH:
                        s['candidateType'] = '高優先觀察'
                        s['includeReason'] = "明日優先（可買已達上限20）"
                        high_priority_watch.append(s)
                    else:
                        s['candidateType'] = '其他觀察'; other_watch.append(s)
                else:
                    # B1 純（bias 3-5%，無近期回測）→ 其他觀察
                    s['candidateType'] = '其他觀察'
                    s['includeReason'] = f"B1守60整理 bias={_b20:.1f}%，無近期回測"
                    other_watch.append(s)
            else:
                # hot_bull / overheated_bull / weak_market：維持原邏輯
                if len(buy_candidates) < _MAX_BUY_CANDIDATES:
                    s['candidateType'] = '明日可買'
                    s['includeReason'] = f"明日優先 bias={_b20:.1f}%"
                    buy_candidates.append(s)
                elif len(high_priority_watch) < _MAX_HIGH_PRIORITY_WATCH:
                    s['candidateType'] = '高優先觀察'
                    s['includeReason'] = "明日優先（可買已達上限20）"
                    high_priority_watch.append(s)
                else:
                    s['candidateType'] = '其他觀察'; other_watch.append(s)

        elif _ss == '突破觀察':
            # normal_bull下，B1（bias>3%）且無近期回測 → 其他觀察
            if _ms_code == 'normal_bull' and _b20 > 3.0 and not _rtnm:
                s['candidateType'] = '其他觀察'
                s['includeReason'] = f"B1 bias={_b20:.1f}%未回測20MA，其他觀察"
                other_watch.append(s)
            else:
                s['includeReason'] = "突破觀察"
                if len(high_priority_watch) < _MAX_HIGH_PRIORITY_WATCH:
                    s['candidateType'] = '高優先觀察'; high_priority_watch.append(s)
                else:
                    s['candidateType'] = '其他觀察'; other_watch.append(s)

        elif _ss in ('等回測', '暫不交易'):
            s['candidateType'] = '其他觀察'
            s['includeReason'] = _ss
            other_watch.append(s)

        else:
            s['candidateType'] = '其他觀察'
            other_watch.append(s)

    # ETF 候選標記
    for s in etf_results:
        s['candidateType'] = 'ETF候選'
        s['includeReason'] = f"ETF（代號 {s.get('stockCode','')}）"

    # 排除類型（權證/ETN/特別股）
    for s in type_excluded:
        s['candidateType'] = '排除'
        s['excludeReason'] = f"商品類型 {s.get('stockType')} 不在篩選範圍"

    # ── 統計回報 ─────────────────────────────────────────────────────
    _summary = {
        "buy_count":           len(buy_candidates),
        "etf_count":           len(etf_results),
        "high_priority_count": len(high_priority_watch),
        "other_watch_count":   len(other_watch),
        "excluded_count":      len(excluded_list) + len(type_excluded),
    }
    print(f"[Screener] ═══════════ 選股結果統計 ═══════════")
    print(f"[Screener]   普通股明日可買：{_summary['buy_count']} 檔")
    print(f"[Screener]   ETF 候選：{_summary['etf_count']} 檔")
    print(f"[Screener]   高優先觀察：{_summary['high_priority_count']} 檔")
    print(f"[Screener]   其他觀察：{_summary['other_watch_count']} 檔")
    print(f"[Screener]   排除：{_summary['excluded_count']} 檔")
    print(f"[Screener] ════════════════════════════════════")

    # 向後相容：stocks = 全部（前端使用此欄位）
    all_results = buy_candidates + high_priority_watch + other_watch + excluded_list + etf_results

    return {
        "stocks":              all_results,
        "buy_candidates":      buy_candidates,
        "high_priority_watch": high_priority_watch,
        "other_watch":         other_watch,
        "excluded":            excluded_list,
        "etf_candidates":      etf_results,
        "summary":             _summary,
        "market_status":       _market_status,
    }

def compute_industry_rankings(screener_results):
    """
    將 run_screener_query() 的結果按產業分組，計算每個產業的綜合分數與狀態。
    回傳按產業分數降序排列的清單，每個元素含該產業所有候選股。
    """
    from collections import defaultdict

    groups = defaultdict(list)
    for s in screener_results:
        ind = (s.get('industry') or '').strip()
        if not ind or ind == 'ETF':
            ind = 'ETF/其他'
        groups[ind].append(s)

    def _strength_score(val):
        """將20D/60D漲幅轉為0~100分（0%→50分，每+1%→+3分）"""
        return max(0.0, min(100.0, 50.0 + val * 3.0))

    result = []
    for ind_name, stocks in groups.items():
        n = len(stocks)
        avg_score    = sum(s['score'] for s in stocks) / n
        avg_r20      = sum(s.get('return20', 0) for s in stocks) / n
        avg_r60      = sum(s.get('return60', 0) for s in stocks) / n
        avg_inst     = sum(s.get('institutionBuyRatio5', 0) for s in stocks) / n

        priority_count  = sum(1 for s in stocks if s.get('strategyState') == '明日優先')
        breakout_count  = sum(1 for s in stocks if s.get('strategyState') in ('突破觀察', '明日優先'))
        overheat_count  = sum(1 for s in stocks if s.get('strategyState') == '過熱警戒')
        pullback_count  = sum(1 for s in stocks if s.get('entryPattern') in ('回測 20MA 轉強', '回測前高不破'))
        breakout_ratio  = breakout_count / n
        overheat_ratio  = overheat_count / n

        s20           = _strength_score(avg_r20)
        s60           = _strength_score(avg_r60)
        inst_score    = min(100.0, avg_inst * 5.0)
        breakout_s    = breakout_ratio * 100.0
        overheat_pen  = overheat_ratio * 100.0

        industry_score = max(0.0, min(100.0,
            avg_score    * 0.40
            + s20        * 0.25
            + s60        * 0.15
            + inst_score * 0.15
            + breakout_s * 0.05
            - overheat_pen * 0.10
        ))
        industry_score = round(industry_score, 1)

        # 產業狀態判定
        if overheat_ratio >= 0.30:
            status = '過熱警戒'
        elif industry_score >= 90 and n >= 3:
            status = '強勢主流'
        elif industry_score >= 80:
            status = '健康偏強'
        elif industry_score >= 75 and pullback_count >= 1:
            status = '回測機會'
        elif breakout_ratio >= 0.30:
            status = '突破集中'
        elif industry_score >= 60:
            status = '中性觀察'
        else:
            status = '弱勢產業'

        # 產業共振標籤：個股分數 >= 75 且產業分數 >= 80
        for s in stocks:
            s['industryScore']     = industry_score
            s['industryStatus']    = status
            s['hasIndustryResonance'] = (s['score'] >= 75 and industry_score >= 80)

        result.append({
            'industryName':   ind_name,
            'industryScore':  industry_score,
            'candidateCount': n,
            'avgScore':       round(avg_score, 1),
            'avgReturn20':    round(avg_r20, 2),
            'avgReturn60':    round(avg_r60, 2),
            'avgInstRatio':   round(avg_inst, 2),
            'priorityCount':  priority_count,
            'breakoutCount':  breakout_count,
            'breakoutRatio':  round(breakout_ratio, 2),
            'overheatCount':  overheat_count,
            'overheatRatio':  round(overheat_ratio, 2),
            'pullbackCount':  pullback_count,
            'status':         status,
            'stocks':         sorted(stocks, key=lambda x: (-x['score'], -x.get('institutionBuyRatio5', 0)))
        })

    result.sort(key=lambda x: -x['industryScore'])
    return result


def trace_stock_filters(code: str) -> dict:
    """
    完整單股篩選追蹤，逐步顯示每個篩選階段的通過/排除原因。
    前端個股追蹤 Debug 面板使用此函式。
    """
    _LIQ_THRESHOLD = 50_000_000
    index_gain_20  = 1.5
    index_gain_60  = 4.0
    today_str = datetime.now().strftime('%Y-%m-%d')

    def _log(msg):
        print(f"[TRACE {code}] {msg}")

    def _sf(v):
        try:
            f = float(v)
            return None if pd.isna(f) else f
        except (TypeError, ValueError):
            return None

    result = {
        "code": code,
        "name": STOCK_NAMES.get(code, ""),
        "inUniverse": code in DEFAULT_STOCKS,
        # daily quote
        "hasDailyQuote": False,
        "closePrice": None,
        "changePercent": None,
        # kbars
        "hasKbars": False,
        "kbarCount": 0,
        "latestKbarDate": None,
        # step results
        "step1": {
            "passed": False, "foreignBuy5": 0, "investmentBuy5": 0,
            "dealerBuy5": 0, "totalBuy5": 0, "hasForeignBuy": False,
            "hasInvestmentBuy": False, "hasTotalBuy": False,
            "instScore": 0, "instLabel": "--", "reason": "", "instDays": []
        },
        "step2Liquidity": {
            "passed": False, "amountMa5": 0.0, "amountMa20": 0.0,
            "threshold": _LIQ_THRESHOLD, "dataMissing": False, "reason": ""
        },
        "step3Technical": {
            "passed": False, "close": None, "ma20": None, "ma60": None,
            "trendPassed": False, "reason": ""
        },
        "step4Strength": {
            "return20": None, "return20Passed": False, "return20Threshold": index_gain_20,
            "return60": None, "return60Passed": False, "return60ScoreOnly": True,
            "return60Threshold": index_gain_60, "reason": ""
        },
        # score
        "scoreBreakdown": [],
        "totalScore": 0,
        "majorBonus": 0,
        "majorFeatures": [],
        # decision
        "bias20": None,
        "return5": None,
        "entryPattern": "",
        "entryPatternLabel": "",
        "stopLossPrice": 0.0,
        "stopLossPercent": 0.0,
        "strategyState": "",
        "strategyStateLabel": "",
        # final
        "finalIncluded": False,
        "excludedAtStep": None,
        "excludedReason": "",
        "highInstRatioWarning": False,
        "messages": []
    }

    _log(f"inUniverse={result['inUniverse']}")

    # ── DB 查詢（法人 + K線基本資訊）──────────────────────────────────
    conn = get_db_connection()
    try:
        row = conn.execute("SELECT name, category FROM stock_names WHERE code=?", (code,)).fetchone()
        if row and row[0]:
            result["name"] = row[0]

        # Step 1: 法人
        days = conn.execute(
            "SELECT DISTINCT date FROM institutional_trading ORDER BY date DESC LIMIT 5"
        ).fetchall()
        if days:
            date_list = [d[0] for d in days]
            ph = ','.join('?' * len(date_list))
            rows = conn.execute(
                f"SELECT date, foreign_buy, investment_buy, dealer_buy "
                f"FROM institutional_trading WHERE code=? AND date IN ({ph}) ORDER BY date DESC",
                [code] + date_list
            ).fetchall()
            if rows:
                f5 = sum(r[1] for r in rows)
                i5 = sum(r[2] for r in rows)
                d5 = sum(r[3] for r in rows)
                t5 = f5 + i5 + d5
                hf, hi, ht = f5 > 0, i5 > 0, t5 > 0
                s1_passed = ht and (hf or hi)
                fail_parts = []
                if not ht:
                    fail_parts.append(f"三大合計 {t5:+} 張 <= 0")
                elif not hf and not hi:
                    fail_parts.append(f"外資 {f5:+} 張 且 投信 {i5:+} 張 均 <= 0（需至少一方>0）")
                _is = 0
                if ht: _is += 7
                if hf: _is += 8
                if hi: _is += 10
                if hf and hi: _is += 5
                inst_score_t = min(25, _is)
                if hf and hi and ht:   inst_label_t = "三人同買"
                elif hf and not hi and ht: inst_label_t = "外資主導"
                elif hi and not hf and ht: inst_label_t = "投信主導"
                else: inst_label_t = "--"
                result["step1"] = {
                    "passed": s1_passed,
                    "foreignBuy5": int(f5), "investmentBuy5": int(i5),
                    "dealerBuy5": int(d5), "totalBuy5": int(t5),
                    "hasForeignBuy": hf, "hasInvestmentBuy": hi, "hasTotalBuy": ht,
                    "instScore": inst_score_t, "instLabel": inst_label_t,
                    "reason": "通過：三大合計>0且外資或投信至少一方>0" if s1_passed else "未通過：" + "、".join(fail_parts),
                    "instDays": [{"date": r[0], "foreign": int(r[1]), "invest": int(r[2]), "dealer": int(r[3])} for r in rows]
                }
                _log(f"Step1 法人條件 passed={s1_passed}")
                _log(f"foreignBuy5={f5} investmentTrustBuy5={i5} dealerBuy5={d5} totalInstitutionBuy5={t5}")
                _log(f"reason={result['step1']['reason']}")
            else:
                result["step1"]["reason"] = "institutional_trading 中無此股票記錄"
                result["messages"].append("法人數據不存在，請先執行同步選股數據")
                _log("Step1 法人條件 passed=false reason=無法人記錄")
        else:
            result["step1"]["reason"] = "institutional_trading 表為空，尚未同步法人數據"
            result["messages"].append("法人數據表為空，請先執行同步選股數據")
            _log("Step1 法人條件 passed=false reason=法人表為空")

        # K線基本資訊
        kbar_count  = conn.execute("SELECT COUNT(*) FROM daily_kbars WHERE code=?", (code,)).fetchone()[0]
        latest_date = conn.execute("SELECT MAX(date) FROM daily_kbars WHERE code=?", (code,)).fetchone()[0]
        result["hasKbars"]       = kbar_count > 0
        result["kbarCount"]      = int(kbar_count)
        result["latestKbarDate"] = latest_date
        _log(f"hasKbars={result['hasKbars']} latestDate={latest_date} kbarsCount={kbar_count}")

        # 讀 K 線資料
        sub_df   = pd.read_sql_query("SELECT * FROM daily_kbars WHERE code=? ORDER BY date ASC", conn, params=[code])
        df_inst2 = pd.read_sql_query("SELECT * FROM institutional_trading WHERE code=? ORDER BY date ASC", conn, params=[code])
    finally:
        conn.close()

    if not result["inUniverse"]:
        result["messages"].insert(0, "此股票不在選股名單中（DEFAULT_STOCKS），不會被納入篩選")
    if not result["step1"]["passed"]:
        result["messages"].append("未通過 Step1 法人篩選，不會被選入候選名單")
        result["excludedAtStep"]  = "Step1_institution"
        result["excludedReason"]  = result["step1"]["reason"]

    if not result["hasKbars"]:
        result["messages"].append("DB 中無此股票的 daily_kbars，請重新執行同步選股數據")
        result["excludedAtStep"]  = result["excludedAtStep"] or "Step_kbars"
        result["excludedReason"]  = result["excludedReason"]  or "無 K 線數據"
        return result

    if kbar_count < 62:
        result["messages"].append(f"K線僅 {kbar_count} 根，不足 62 根無法計算60MA，請重新執行同步選股數據")
        result["excludedAtStep"]  = result["excludedAtStep"] or "Step_kbars_count"
        result["excludedReason"]  = result["excludedReason"]  or f"K線不足62根（{kbar_count}根）"

    # ── 取得今日行情（外部 API，失敗時靜默處理）──────────────────────
    q = None
    try:
        daily_quotes, _ = fetch_twse_daily_quotes()
        q = daily_quotes.get(code)
    except Exception as eq:
        result["messages"].append(f"今日行情擷取失敗（{eq}），部分技術數值將以 DB 最新 K 線為準")

    result["hasDailyQuote"] = q is not None
    if q:
        result["closePrice"]    = q["close"]
        result["changePercent"] = round(q["change_pct"], 2)
    _log(f"hasDailyQuote={result['hasDailyQuote']}" +
         (f" close={q['close']} changePct={q['change_pct']:.2f}%" if q else ""))

    # 若 K 線不足，停在此
    if kbar_count < 62:
        return result

    # ── 補注入今日 K 線 ────────────────────────────────────────────
    if q and (sub_df.empty or sub_df.iloc[-1]["date"] < today_str):
        today_row = pd.DataFrame([{
            "code": code, "date": today_str,
            "open": q.get("open", q["close"]), "high": q.get("high", q["close"]),
            "low":  q.get("low",  q["close"]), "close": q["close"],
            "volume": q.get("volume", 0)
        }])
        sub_df = pd.concat([sub_df, today_row], ignore_index=True)

    # ── 計算均線 / 指標 ────────────────────────────────────────────
    sub_df["ma5"]         = sub_df["close"].rolling(5).mean()
    sub_df["ma10"]        = sub_df["close"].rolling(10).mean()
    sub_df["ma20"]        = sub_df["close"].rolling(20).mean()
    sub_df["ma60"]        = sub_df["close"].rolling(60).mean()
    sub_df["volume_ma20"] = sub_df["volume"].rolling(20).mean()
    sub_df["macd_hist"]   = compute_macd(sub_df["close"])
    sub_df["amount"]      = sub_df["close"] * sub_df["volume"] * 1000
    sub_df["amount_ma5"]  = sub_df["amount"].rolling(5).mean()
    sub_df["amount_ma20"] = sub_df["amount"].rolling(20).mean()

    latest = sub_df.iloc[-1]
    prev   = sub_df.iloc[-2]

    if pd.isna(latest["ma20"]) or pd.isna(latest["ma60"]):
        result["excludedAtStep"] = result["excludedAtStep"] or "Step_ma_nan"
        result["excludedReason"] = result["excludedReason"] or "ma20/ma60 為 NaN，K線數量不足"
        result["messages"].append("ma20/ma60 計算失敗，K 線數量可能不足")
        return result

    # ── Step 2: 流動性 ─────────────────────────────────────────────
    _ama5  = _sf(latest.get("amount_ma5",  None))
    _ama20 = _sf(latest.get("amount_ma20", None))
    _liq_data_missing = (_ama5 is None) and (_ama20 is None)
    _ama5_val  = _ama5  or 0.0
    _ama20_val = _ama20 or 0.0

    if _liq_data_missing:
        _liq_passed  = False
        liq_reason   = "成交金額資料缺失：amountMa5 / amountMa20 均無效"
    else:
        _liq_passed = (
            (_ama5  is not None and _ama5  >= _LIQ_THRESHOLD)
            or (_ama20 is not None and _ama20 >= _LIQ_THRESHOLD)
        )
        if _liq_passed:
            if _ama5 is not None and _ama5 >= _LIQ_THRESHOLD:
                liq_reason = f"通過：amountMa5={_ama5_val:,.0f} >= {_LIQ_THRESHOLD:,}"
            else:
                liq_reason = f"通過(20日)：amountMa5={_ama5_val:,.0f} < {_LIQ_THRESHOLD:,}，amountMa20={_ama20_val:,.0f} >= {_LIQ_THRESHOLD:,}"
        else:
            liq_reason = f"未通過：amountMa5={_ama5_val:,.0f}，amountMa20={_ama20_val:,.0f}，均未達{_LIQ_THRESHOLD:,}"

    result["step2Liquidity"] = {
        "passed": _liq_passed,
        "amountMa5":   _ama5_val,
        "amountMa20":  _ama20_val,
        "threshold":   _LIQ_THRESHOLD,
        "dataMissing": _liq_data_missing,
        "reason":      liq_reason
    }
    _log(f"Step2 流動性 passed={_liq_passed}")
    _log(f"amountMa5={_ama5_val:,.0f} amountMa20={_ama20_val:,.0f} threshold={_LIQ_THRESHOLD:,}")
    _log(f"reason={liq_reason}")

    if not _liq_passed:
        if not result["excludedAtStep"]:
            result["excludedAtStep"] = "Step2_liquidity"
            result["excludedReason"] = liq_reason
        result["messages"].append(f"流動性未通過：{liq_reason}")
        return result

    # ── Step 3: 技術條件（多頭排列）────────────────────────────────
    trend_passed = bool(latest["close"] > latest["ma20"] > latest["ma60"])
    result["step3Technical"] = {
        "passed":      trend_passed,
        "close":       float(latest["close"]),
        "ma20":        round(float(latest["ma20"]), 2),
        "ma60":        round(float(latest["ma60"]), 2),
        "trendPassed": trend_passed,
        "reason":      ("close > 20MA > 60MA 多頭排列通過" if trend_passed
                        else f"多頭排列不符 close={latest['close']:.2f} ma20={latest['ma20']:.2f} ma60={latest['ma60']:.2f}")
    }
    _log(f"Step3 技術條件 passed={trend_passed}")
    _log(f"close={latest['close']:.2f} ma20={latest['ma20']:.2f} ma60={latest['ma60']:.2f} trendPassed={trend_passed}")

    if not trend_passed:
        if not result["excludedAtStep"]:
            result["excludedAtStep"] = "Step3_trend"
            result["excludedReason"] = result["step3Technical"]["reason"]
        result["messages"].append(result["step3Technical"]["reason"])
        return result

    # ── Step 4: 相對強度 ────────────────────────────────────────────
    prev_5  = sub_df.iloc[-6]  if len(sub_df) >= 6  else sub_df.iloc[0]
    prev_20 = sub_df.iloc[-21] if len(sub_df) >= 21 else sub_df.iloc[0]
    prev_60 = sub_df.iloc[-61] if len(sub_df) >= 61 else sub_df.iloc[0]
    return5  = ((latest["close"] - prev_5["close"])  / prev_5["close"])  * 100
    return20 = ((latest["close"] - prev_20["close"]) / prev_20["close"]) * 100
    return60 = ((latest["close"] - prev_60["close"]) / prev_60["close"]) * 100
    r20_passed = return20 > index_gain_20

    result["return5"]  = round(float(return5),  2)
    result["return20"] = round(float(return20), 2)
    result["return60"] = round(float(return60), 2)
    result["step4Strength"] = {
        "return20":          round(return20, 2),
        "return20Passed":    r20_passed,
        "return20Threshold": index_gain_20,
        "return60":          round(return60, 2),
        "return60Passed":    return60 > index_gain_60,
        "return60ScoreOnly": True,
        "return60Threshold": index_gain_60,
        "reason": (f"return20={return20:.2f}%（需>{index_gain_20}%，{'通過' if r20_passed else '未通過'}）"
                   f" | return60={return60:.2f}%（{'加10分' if return60 > index_gain_60 else '不加分，不排除'}）")
    }
    _log(f"Step4 相對強度")
    _log(f"return20={return20:.2f} passed={r20_passed}")
    _log(f"return60={return60:.2f} scoreOnly=true reason={'通過，加10分' if return60 > index_gain_60 else f'return60未達{index_gain_60}%，不排除只是不加分'}")

    if not r20_passed:
        if not result["excludedAtStep"]:
            result["excludedAtStep"] = "Step4_return20"
            result["excludedReason"] = f"return20={return20:.2f}% <= {index_gain_20}%（20日強度未達大盤基準）"
        result["messages"].append(result["excludedReason"])
        return result

    # ── 籌碼面指標 ────────────────────────────────────────────────
    bias20 = ((latest["close"] - latest["ma20"]) / latest["ma20"]) * 100
    todayChangePercent = q["change_pct"] if q else ((latest["close"] - prev["close"]) / prev["close"] * 100)
    high_low_diff = latest["high"] - latest["low"]
    body_max = max(latest["open"], latest["close"])
    candleUpperShadowRatio = ((latest["high"] - body_max) / high_low_diff * 100) if high_low_diff > 0 else 0.0
    macdHistogram  = latest["macd_hist"]
    macdHistPrev1  = sub_df.iloc[-2]["macd_hist"]
    macdHistPrev2  = sub_df.iloc[-3]["macd_hist"]
    macd_shrinking = (macdHistogram < 0) and (macdHistogram > macdHistPrev1 > macdHistPrev2)
    result["bias20"] = round(float(bias20), 2)

    foreign_strike = investment_strike = 0
    sync_buy       = False
    foreignBuy5 = investmentTrustBuy5 = dealerBuy5 = totalInstitutionBuy5 = 0
    inst_ratio_5d = 0.0

    if not df_inst2.empty:
        inst_list = df_inst2.tail(10).to_dict("records")
        for r in reversed(inst_list):
            if r["investment_buy"] > 0: investment_strike += 1
            else: break
        for r in reversed(inst_list):
            if r["foreign_buy"] > 0: foreign_strike += 1
            else: break
        if inst_list:
            last_r   = inst_list[-1]
            sync_buy = (last_r["foreign_buy"] > 0 and last_r["investment_buy"] > 0 and last_r["dealer_buy"] > 0)
        last_5 = inst_list[-5:] if len(inst_list) >= 5 else inst_list
        foreignBuy5          = sum(r["foreign_buy"]    for r in last_5)
        investmentTrustBuy5  = sum(r["investment_buy"] for r in last_5)
        dealerBuy5           = sum(r["dealer_buy"]     for r in last_5)
        totalInstitutionBuy5 = foreignBuy5 + investmentTrustBuy5 + dealerBuy5
        last_5_k = sub_df.tail(len(last_5)).to_dict("records")
        total_vol = sum(r["volume"] for r in last_5_k)
        if total_vol > 0:
            inst_ratio_5d = (totalInstitutionBuy5 / total_vol) * 100

    result["highInstRatioWarning"] = inst_ratio_5d > 30.0

    if investment_strike > 0 and foreign_strike > 0 and sync_buy: tier_name = "黃金滿貫"
    elif investment_strike > 0 and foreign_strike > 0:             tier_name = "強勢雙雄"
    elif investment_strike > 0:                                    tier_name = "投信鎖碼"
    elif foreign_strike > 0:                                       tier_name = "外資鎖碼"
    else:                                                          tier_name = "主力佈局"

    if   foreignBuy5 > 0 and investmentTrustBuy5 > 0 and totalInstitutionBuy5 > 0: institution_label = "三人同買"
    elif foreignBuy5 > 0 and investmentTrustBuy5 <= 0 and totalInstitutionBuy5 > 0: institution_label = "外資主導"
    elif investmentTrustBuy5 > 0 and foreignBuy5 <= 0 and totalInstitutionBuy5 > 0: institution_label = "投信主導"
    else: institution_label = "--"

    # ── 歷史高低點 / 買點型態 / 停損 ────────────────────────────────
    recentHigh10 = sub_df.iloc[-11:-1]["high"].max() if len(sub_df) >= 11 else sub_df["high"].max()
    recentHigh20 = sub_df.iloc[-21:-1]["high"].max() if len(sub_df) >= 21 else sub_df["high"].max()
    recentLow10  = sub_df.iloc[-11:-1]["low"].min()  if len(sub_df) >= 11 else sub_df["low"].min()

    is_breakout          = (latest["close"] > recentHigh10) or (latest["close"] > recentHigh20)
    is_near_ma20         = (abs(latest["close"] - latest["ma20"]) / latest["ma20"] < 0.03) and (latest["low"] >= latest["ma20"] * 0.98)
    is_near_prior_high   = (abs(latest["close"] - recentHigh20) / recentHigh20 < 0.03)
    is_high_consolidation = (latest["close"] >= recentHigh20 * 0.97) and ((recentHigh10 - recentLow10) / recentLow10 < 0.08)

    entry_pattern = "創高後高檔整理"; entry_pattern_label = "🚀 高檔續強"; stopLossPrice = latest["ma20"]

    if bias20 > 15.0 or return5 > 15.0 or candleUpperShadowRatio > 40.0:
        entry_pattern = "過熱不交易";      entry_pattern_label = "⚠️ 過熱";   stopLossPrice = 0.0
    elif bias20 > 10.0:
        entry_pattern = "乖離過大等回測"; entry_pattern_label = "⏳ 等回測"; stopLossPrice = latest["ma20"]
    elif is_near_ma20 and (latest["close"] > latest["open"] or latest["close"] > latest["ma5"] or latest["close"] > latest["ma10"]):
        entry_pattern = "回測 20MA 轉強"; entry_pattern_label = "📍 回測20MA"; stopLossPrice = min(latest["ma20"], sub_df.iloc[-5:]["low"].min())
    elif is_near_prior_high and latest["close"] >= recentHigh20 * 0.97:
        entry_pattern = "回測前高不破";   entry_pattern_label = "📍 回測前高"; stopLossPrice = recentHigh20 * 0.97
    elif is_breakout and latest["volume"] > latest["volume_ma20"] * 1.2:
        entry_pattern = "突破整理區";     entry_pattern_label = "🚀 突破整理"; stopLossPrice = latest["low"]
    elif is_high_consolidation and latest["close"] >= recentHigh20 * 0.98:
        entry_pattern = "創高後高檔整理"; entry_pattern_label = "🚀 高檔續強"; stopLossPrice = sub_df.iloc[-10:]["low"].min()

    stopLossPercent = ((stopLossPrice - latest["close"]) / latest["close"]) * 100 if stopLossPrice > 0 else 0.0
    abs_sl = abs(stopLossPercent)

    # ── 分數計算 ───────────────────────────────────────────────────
    breakdown = []
    score = 0

    def _item(label, passed, delta, detail=""):
        breakdown.append({"label": label, "passed": bool(passed), "delta": int(delta), "detail": str(detail)})
        return delta if passed else 0

    score += _item("趨勢多頭", True, 20, "已通過多頭排列硬篩")
    rs20 = return20 - index_gain_20
    score += _item("20日相對強度", rs20 > 1.5, 10, f"近20日漲幅超越大盤 {rs20:+.1f}%（需 > +1.5%）")
    score += _item("60日相對強度", return60 > index_gain_60, 10,
                   f"近60日漲幅 {return60:+.1f}%（> {index_gain_60}% 加分，否則不扣分）")

    _inst_raw = 0
    for label, cond, pts, det in [
        ("投信近5日買超",   investmentTrustBuy5 > 0,  10, f"投信近5日合計 {investmentTrustBuy5:+} 張"),
        ("外資近5日買超",   foreignBuy5 > 0,           8,  f"外資近5日合計 {foreignBuy5:+} 張"),
        ("三大法人買超",    totalInstitutionBuy5 > 0,  7,  f"三大法人近5日合計 {totalInstitutionBuy5:+} 張"),
        ("外資投信同步買超", foreignBuy5 > 0 and investmentTrustBuy5 > 0, 5, f"外資{foreignBuy5:+}張、投信{investmentTrustBuy5:+}張"),
    ]:
        v = _item(label, cond, pts, det); _inst_raw += v; score += v

    _INST_CAP = 25
    if _inst_raw > _INST_CAP:
        _excess = _inst_raw - _INST_CAP
        breakdown.append({"label": "法人分數上限截斷", "passed": False, "delta": -_excess, "detail": f"截斷{_excess}分"})
        score -= _excess

    if   0.0 <= bias20 <= 3.0:   bd, bp = 10,  True
    elif 3.0 < bias20 <= 6.0:    bd, bp = 8,   True
    elif 6.0 < bias20 <= 10.0:   bd, bp = 4,   True
    elif 10.0 < bias20 <= 15.0:  bd, bp = -15, False
    elif bias20 > 15.0:          bd, bp = -25, False
    else:                        bd, bp = 0,   False
    breakdown.append({"label": "乖離20MA位置", "passed": bp, "delta": bd, "detail": f"乖離 {bias20:.1f}%"})
    score += bd

    score += _item("MACD負柱收斂", macd_shrinking, 10, "histogram < 0 且連續2~3日縮短")
    candle_strong = (latest["close"] > latest["open"]) or (latest["close"] > latest["ma5"]) or \
                    (latest["close"] > latest["ma10"]) or (latest["close"] > prev["high"])
    score += _item("K線轉強", candle_strong, 10, "收紅K、站回5/10MA，或突破昨高")

    if   _ama5_val >= 300_000_000: ld = 10
    elif _ama5_val >= 100_000_000: ld = 8
    elif _ama5_val >= 50_000_000:  ld = 5
    else:                          ld = 5
    score += _item("成交金額流動性", ld > 0, ld, f"5日均額 {_ama5_val/1e8:.2f}億")

    if   abs_sl <= 3.0: sld, slp = 10, True
    elif abs_sl <= 5.0: sld, slp = 7,  True
    elif abs_sl <= 6.0: sld, slp = 3,  True
    elif abs_sl <= 8.0: sld, slp = -10, False
    else:               sld, slp = -20, False
    breakdown.append({"label": "停損距離", "passed": slp, "delta": sld, "detail": f"停損距離 {abs_sl:.1f}%"})
    score += sld

    for label, cond, pts, det in [
        ("近5日漲幅過大",      return5 > 15.0,                              -10, f"近5日漲幅 {return5:.1f}%"),
        ("今日急漲扣分",       todayChangePercent > 6.0,                    -10, f"今日漲幅 {todayChangePercent:.1f}%"),
        ("長上影線",           candleUpperShadowRatio > 40.0,               -10, f"上影線比例 {candleUpperShadowRatio:.0f}%"),
        ("法人買超但股價不漲", totalInstitutionBuy5 > 0 and todayChangePercent <= 0, -10, "法人買超但今日未上漲"),
    ]:
        v = _item(label, cond, pts, det)
        if cond: score += v

    score = max(0, min(100, score))

    # 主力特徵加分
    major_features = []; major_bonus_raw = 0
    if institution_label in ("三人同買", "外資主導", "投信主導"):
        major_features.append(institution_label)
        major_bonus_raw += 3 if institution_label == "三人同買" else 1
    elif sync_buy:
        major_features.append("三人同買"); major_bonus_raw += 3
    if tier_name == "黃金滿貫":
        major_features.append("黃金滿貫"); major_bonus_raw += 3
    elif tier_name == "強勢雙雄":
        major_features.append("強勢雙雄"); major_bonus_raw += 2
    if investment_strike >= 5:
        major_features.append(f"投信連買{investment_strike}D"); major_bonus_raw += 3
    elif investment_strike >= 3:
        major_features.append(f"投信連買{investment_strike}D"); major_bonus_raw += 2
    if foreign_strike >= 5:
        major_features.append(f"外資連買{foreign_strike}D"); major_bonus_raw += 3
    elif foreign_strike >= 3:
        major_features.append(f"外資連買{foreign_strike}D"); major_bonus_raw += 2
    major_bonus = min(8, major_bonus_raw)

    # 策略狀態
    is_overheat = (bias20 > 15.0) or (return5 > 20.0) or (todayChangePercent > 7.0) or \
                  (candleUpperShadowRatio > 40.0) or (abs_sl > 8.0)
    if not is_overheat:
        score = min(100, score + major_bonus)
    is_pullback      = (bias20 > 10.0) or (abs_sl > 6.0 and not is_overheat)
    is_breakout_cond = is_breakout and (latest["volume"] > latest["volume_ma20"] * 1.2) and \
                       (todayChangePercent <= 7.0) and (bias20 < 12.0)

    if is_overheat:
        strategy_state, strategy_state_label = "過熱警戒", "🔴 過熱警戒"
    elif is_pullback:
        strategy_state, strategy_state_label = "等回測",   "🟡 等回測"
    elif score >= 80 and bias20 <= 6.0 and abs_sl <= 6.0 and _liq_passed and \
         latest["close"] > latest["ma20"] > latest["ma60"]:
        strategy_state, strategy_state_label = "明日優先", "🟢 明日優先"
    elif score >= 65 or is_breakout_cond:
        strategy_state, strategy_state_label = "突破觀察", "🔵 突破觀察"
    elif score >= 50:
        strategy_state, strategy_state_label = "等回測",   "🟡 等回測"
    else:
        strategy_state, strategy_state_label = "暫不交易", "⚪ 暫不交易"

    # 彙整結果
    result.update({
        "scoreBreakdown":       breakdown,
        "totalScore":           int(score),
        "majorBonus":           int(major_bonus),
        "majorFeatures":        major_features,
        "institutionLabel":     institution_label,
        "institutionBuyRatio5": round(float(inst_ratio_5d), 2),
        "entryPattern":         entry_pattern,
        "entryPatternLabel":    entry_pattern_label,
        "stopLossPrice":        round(float(stopLossPrice), 2),
        "stopLossPercent":      round(float(stopLossPercent), 2),
        "strategyState":        strategy_state,
        "strategyStateLabel":   strategy_state_label,
        "bias20":               round(float(bias20), 2),
        "ma5":   round(float(latest["ma5"]),  2) if not pd.isna(latest.get("ma5",  float("nan"))) else None,
        "ma10":  round(float(latest["ma10"]), 2) if not pd.isna(latest.get("ma10", float("nan"))) else None,
        "ma20":  round(float(latest["ma20"]), 2),
        "ma60":  round(float(latest["ma60"]), 2),
        "finalIncluded":        True,
    })

    _log(f"Score total={score}")
    _log(f"trendScore=20 return20Score={10 if rs20 > 1.5 else 0} return60Score={10 if return60 > index_gain_60 else 0}")
    _log(f"institutionScore={min(_inst_raw, _INST_CAP)} mainForceScore={major_bonus if not is_overheat else 0}")
    _log(f"liquidityScore={ld} biasScore={bd} riskScore={sld}")
    _log(f"EntryPattern={entry_pattern}")
    _log(f"StrategyStatus={strategy_state}")
    _log(f"finalIncluded=true")

    return result


if __name__ == "__main__":
    init_db()
    sync_twse_institutional_data()
    print("Inst candidates:", get_inst_5d_candidates())
