import os
import ssl
import json
import sqlite3
import urllib.request
import pandas as pd
from datetime import datetime, timedelta

try:
    import certifi
    _SSL_CTX = ssl.create_default_context(cafile=certifi.where())
except Exception:
    _SSL_CTX = ssl.create_default_context()

_BASE_DIR = os.path.dirname(os.path.abspath(__file__))
_DB_PATH  = os.path.join(_BASE_DIR, "stock_cache.db")


def _get_conn():
    conn = sqlite3.connect(_DB_PATH, timeout=60.0)
    try:
        conn.execute("PRAGMA journal_mode=WAL;")
    except Exception:
        pass
    return conn


def _init_table():
    conn = _get_conn()
    conn.execute("""
        CREATE TABLE IF NOT EXISTS market_index_daily (
            date TEXT PRIMARY KEY,
            open REAL, high REAL, low REAL, close REAL,
            volume REAL, amount REAL,
            ma20 REAL, ma60 REAL,
            created_at TEXT, updated_at TEXT
        )
    """)
    conn.commit()
    conn.close()


def fetch_market_index_daily():
    """Fetch TAIEX OHLCV from Yahoo Finance and cache in stock_cache.db.
    Returns a DataFrame sorted by date, or None on total failure.
    """
    _init_table()

    conn = _get_conn()
    try:
        df_cached = pd.read_sql_query(
            "SELECT * FROM market_index_daily ORDER BY date ASC", conn
        )
    except Exception:
        df_cached = pd.DataFrame()
    conn.close()

    today_str = datetime.now().strftime('%Y-%m-%d')

    if not df_cached.empty:
        latest_date = df_cached.iloc[-1]['date']
        days_old = (
            datetime.strptime(today_str, '%Y-%m-%d')
            - datetime.strptime(latest_date, '%Y-%m-%d')
        ).days
        if days_old <= 1 and len(df_cached) >= 62:
            return df_cached

    try:
        headers = {'User-Agent': 'Mozilla/5.0 (Windows NT 10.0; Win64; x64)'}
        url = "https://query1.finance.yahoo.com/v8/finance/chart/%5ETWII?interval=1d&range=6mo"
        req = urllib.request.Request(url, headers=headers)
        with urllib.request.urlopen(req, timeout=12, context=_SSL_CTX) as r:
            raw = json.loads(r.read().decode('utf-8'))

        result   = raw['chart']['result'][0]
        stamps   = result['timestamp']
        q        = result['indicators']['quote'][0]
        vol_list = q.get('volume') or [None] * len(stamps)

        rows = []
        for i, ts in enumerate(stamps):
            try:
                dt = datetime.utcfromtimestamp(ts) + timedelta(hours=8)
                rows.append({
                    'date':   dt.strftime('%Y-%m-%d'),
                    'open':   q['open'][i],
                    'high':   q['high'][i],
                    'low':    q['low'][i],
                    'close':  q['close'][i],
                    'volume': vol_list[i] or 0,
                    'amount': 0.0,
                })
            except Exception:
                continue

        df_new = pd.DataFrame(rows).dropna(subset=['close'])
        if not df_new.empty:
            now_str = datetime.now().strftime('%Y-%m-%d %H:%M:%S')
            conn = _get_conn()
            cursor = conn.cursor()
            for _, row in df_new.iterrows():
                cursor.execute("""
                    INSERT INTO market_index_daily
                        (date, open, high, low, close, volume, amount, created_at, updated_at)
                    VALUES (?,?,?,?,?,?,?,?,?)
                    ON CONFLICT(date) DO UPDATE SET
                        open=excluded.open, high=excluded.high, low=excluded.low,
                        close=excluded.close, volume=excluded.volume,
                        updated_at=excluded.updated_at
                """, (row['date'], row['open'], row['high'], row['low'],
                      row['close'], row['volume'], row['amount'], now_str, now_str))
            conn.commit()
            conn.close()
            print(f"[MarketStatus] TAIEX {len(df_new)} 天 K 線已更新")

            conn = _get_conn()
            df_cached = pd.read_sql_query(
                "SELECT * FROM market_index_daily ORDER BY date ASC", conn
            )
            conn.close()

    except Exception as e:
        print(f"[MarketStatus] Yahoo Finance 取得 TAIEX 失敗: {e}")

    return df_cached if not df_cached.empty else None


def calculate_hot_stock_ratio():
    """Return (hot_ratio, very_hot_ratio, surge_5d_ratio) as percentages.
    hot      = bias from 20MA > 10%
    very_hot = bias from 20MA > 15%
    surge_5d = 5-day return > 15%
    """
    conn = _get_conn()
    try:
        df = pd.read_sql_query(
            "SELECT code, date, close FROM daily_kbars ORDER BY code, date ASC", conn
        )
    except Exception:
        return 0.0, 0.0, 0.0
    finally:
        conn.close()

    if df.empty:
        return 0.0, 0.0, 0.0

    hot = very_hot = surge = valid = 0
    for _, group in df.groupby('code'):
        g = group.sort_values('date').reset_index(drop=True)
        if len(g) < 21:
            continue
        ma20  = g['close'].rolling(20).mean().iloc[-1]
        close = g['close'].iloc[-1]
        if pd.isna(ma20) or ma20 == 0:
            continue
        bias = (close - ma20) / ma20 * 100
        prev5 = g['close'].iloc[-6] if len(g) >= 6 else g['close'].iloc[0]
        r5    = (close - prev5) / prev5 * 100 if prev5 > 0 else 0.0

        valid += 1
        if bias > 10:  hot      += 1
        if bias > 15:  very_hot += 1
        if r5   > 15:  surge    += 1

    if valid == 0:
        return 0.0, 0.0, 0.0
    return (
        round(hot      / valid * 100, 1),
        round(very_hot / valid * 100, 1),
        round(surge    / valid * 100, 1),
    )


_MARKET_STATUS_CACHE: dict = {}
_MARKET_STATUS_DATE:  str  = ""


def calculate_market_status(index_df=None, margin_df=None):
    """
    Classify market into one of four states:
        normal_bull | hot_bull | overheated_bull | weak_market

    Returns a dict:
        status, label, score, description, suggestion, metrics
    """
    global _MARKET_STATUS_CACHE, _MARKET_STATUS_DATE

    today_str = datetime.now().strftime('%Y-%m-%d')
    if _MARKET_STATUS_CACHE and _MARKET_STATUS_DATE == today_str:
        return _MARKET_STATUS_CACHE

    _FALLBACK = {
        "status": "normal_bull", "label": "正常多頭", "score": 50,
        "description": "大盤資料不足，採用預設正常多頭狀態。",
        "suggestion": "依照原本策略操作。",
        "metrics": {
            "index_close": 0, "index_ma20": 0, "index_ma60": 0,
            "bias_ma20_pct": 0, "bias_ma60_pct": 0,
            "market_amount_ratio": 0, "margin_5d_change": None,
            "hot_stock_ratio": 0, "very_hot_stock_ratio": 0,
            "surge_5d_ratio": 0, "data_available": False
        }
    }

    if index_df is None:
        index_df = fetch_market_index_daily()

    if index_df is None or len(index_df) < 62:
        print(f"[MarketStatus] 大盤 K 線不足（{len(index_df) if index_df is not None else 0} 天），使用預設狀態")
        return _FALLBACK

    df     = index_df.copy().sort_values('date').reset_index(drop=True)
    closes = df['close'].astype(float)
    ma20   = closes.rolling(20).mean().iloc[-1]
    ma60   = closes.rolling(60).mean().iloc[-1]
    close  = closes.iloc[-1]

    if pd.isna(ma20) or pd.isna(ma60) or ma20 == 0 or ma60 == 0:
        return _FALLBACK

    bias_ma20 = (close - ma20) / ma20 * 100
    bias_ma60 = (close - ma60) / ma60 * 100

    margin_5d_change = None
    if margin_df is not None and not margin_df.empty and len(margin_df) >= 6:
        try:
            margin_5d_change = float(
                margin_df['margin_balance'].iloc[-1] - margin_df['margin_balance'].iloc[-6]
            )
        except Exception:
            pass

    try:
        hot_ratio, very_hot_ratio, surge_ratio = calculate_hot_stock_ratio()
    except Exception:
        hot_ratio = very_hot_ratio = surge_ratio = 0.0

    metrics = {
        "index_close":          round(float(close),    2),
        "index_ma20":           round(float(ma20),     2),
        "index_ma60":           round(float(ma60),     2),
        "bias_ma20_pct":        round(float(bias_ma20),2),
        "bias_ma60_pct":        round(float(bias_ma60),2),
        "market_amount_ratio":  0,
        "margin_5d_change":     margin_5d_change,
        "hot_stock_ratio":      float(hot_ratio),
        "very_hot_stock_ratio": float(very_hot_ratio),
        "surge_5d_ratio":       float(surge_ratio),
        "data_available":       True
    }

    # ── 1. 轉弱盤 ─────────────────────────────────────────────────────
    if close < ma60 or ma20 < ma60:
        result = {
            "status": "weak_market", "label": "轉弱盤", "score": 0,
            "description": "大盤跌破季線或均線轉弱，暫停新增個股買進，僅保留觀察名單。",
            "suggestion": "不開新倉。若持有個股，優先檢查是否跌破停損或月線。",
            "metrics": metrics
        }
        _MARKET_STATUS_CACHE = result; _MARKET_STATUS_DATE = today_str
        return result

    # ── 2. 過熱多頭 ───────────────────────────────────────────────────
    overheated = (
        bias_ma20 > 10.0
        or bias_ma60 > 15.0
        or hot_ratio > 30.0
        or very_hot_ratio > 15.0
        or (margin_5d_change is not None and margin_5d_change > 300e8)
    )
    if overheated:
        result = {
            "status": "overheated_bull", "label": "過熱多頭", "score": 20,
            "description": "市場短線過熱，容易出現急殺或高檔震盪，不追新倉，只等待明確回測。",
            "suggestion": "只允許：回測20MA轉強（停損<=5%）。禁止突破整理、高檔續強、乖離>3%的買點。",
            "metrics": metrics
        }
        _MARKET_STATUS_CACHE = result; _MARKET_STATUS_DATE = today_str
        return result

    # ── 3. 偏熱多頭 ───────────────────────────────────────────────────
    hot = (
        (6.0 < bias_ma20 <= 10.0)
        or (12.0 < bias_ma60 <= 15.0)
        or (20.0 < hot_ratio <= 30.0)
        or (margin_5d_change is not None and margin_5d_change > 150e8)
    )
    if hot:
        result = {
            "status": "hot_bull", "label": "偏熱多頭", "score": 50,
            "description": "大盤仍是多頭，但短線乖離偏高，不適合追高，只適合等回測止跌。",
            "suggestion": "只允許：回測20MA轉強、回測前高不破。禁止突破整理、高檔續強、今日急漲追價。",
            "metrics": metrics
        }
        _MARKET_STATUS_CACHE = result; _MARKET_STATUS_DATE = today_str
        return result

    # ── 4. 正常多頭 ───────────────────────────────────────────────────
    result = {
        "status": "normal_bull", "label": "正常多頭", "score": 100,
        "description": "大盤仍在多頭排列，乖離可接受，可依照原本策略尋找回測與突破機會。",
        "suggestion": "可尋找回測20MA轉強、回測前高不破、突破整理、高檔續強等買點。",
        "metrics": metrics
    }
    _MARKET_STATUS_CACHE = result; _MARKET_STATUS_DATE = today_str
    return result


def determine_buy_method(stock, market_status):
    """
    Recommend a buy method for one stock given the current market status.

    Parameters
    ----------
    stock : dict  — fields: entryPattern, bias20, stopLossPercent, todayChangePercent,
                             return5, ma20, stopLossPrice
    market_status : dict returned by calculate_market_status()

    Returns
    -------
    dict : action, label, reason, allowed, entry_condition, stop_loss_rule, position_suggestion
    """
    ms       = (market_status or {}).get('status', 'normal_bull')
    pattern  = stock.get('entryPattern', '')
    bias20   = float(stock.get('bias20', 0) or 0)
    abs_sl   = abs(float(stock.get('stopLossPercent', 0) or 0))
    today_ch = float(stock.get('todayChangePercent', 0) or 0)
    return5  = float(stock.get('return5', 0) or 0)
    ma20_val = float(stock.get('ma20', 0) or 0)
    sl_price = float(stock.get('stopLossPrice', 0) or 0)

    is_ma20       = ('回測 20MA' in pattern or '回測20MA' in pattern)
    is_prior_high = '回測前高' in pattern
    is_breakout   = '突破整理' in pattern
    is_high_cont  = '高檔續強' in pattern or '創高後高檔整理' in pattern
    is_bad        = '過熱' in pattern or '等回測' in pattern or '乖離過大' in pattern

    # ── 轉弱盤：全部不交易 ──────────────────────────────────────────
    if ms == 'weak_market':
        return {
            "action": "no_trade", "label": "暫不交易", "allowed": False,
            "reason": "大盤轉弱，個股勝率下降，先等大盤重新站回20MA與60MA。",
            "entry_condition": "等待大盤收復20MA及60MA後再評估",
            "stop_loss_rule": "無進場規劃", "position_suggestion": "空倉等待"
        }

    # ── 個股本身過熱型態 ────────────────────────────────────────────
    if is_bad:
        return {
            "action": "avoid", "label": "過熱等回測", "allowed": False,
            "reason": f"個股乖離過大（{bias20:+.1f}%）或短線過熱，需充分回測。",
            "entry_condition": "等回測至20MA附近，低點不破後再評估",
            "stop_loss_rule": "未進場", "position_suggestion": "觀察名單"
        }

    # ── 正常多頭 ─────────────────────────────────────────────────────
    if ms == 'normal_bull':
        if is_ma20:
            return {
                "action": "buy", "label": "回測月線止跌買", "allowed": True,
                "reason": "正常多頭，靠近20MA止跌轉強，標準回測進場機會。",
                "entry_condition": f"靠近20MA（{ma20_val:.2f}），低點不破20MA附近，今日收紅K或站回短均",
                "stop_loss_rule": f"跌破20MA或近5日低點（停損{abs_sl:.1f}%）",
                "position_suggestion": "正常倉位"
            }
        if is_prior_high:
            return {
                "action": "buy", "label": "前高回測不破買", "allowed": True,
                "reason": "正常多頭，回測前高不破，突破後回測轉強，標準進場。",
                "entry_condition": f"回測前高附近不破，收盤守住前高區（停損{abs_sl:.1f}%）",
                "stop_loss_rule": f"跌破前高回測區（{sl_price:.2f}）",
                "position_suggestion": "正常倉位"
            }
        if is_breakout:
            return {
                "action": "watch_breakout", "label": "帶量突破買", "allowed": True,
                "reason": "正常多頭，突破整理區且量能配合，可積極進場。",
                "entry_condition": "突破10或20日高點，量能>20日均量1.2倍",
                "stop_loss_rule": f"跌破突破K低點（{sl_price:.2f}）",
                "position_suggestion": "正常倉位"
            }
        if is_high_cont:
            return {
                "action": "buy", "label": "強勢續抱或小倉試單", "allowed": True,
                "reason": "正常多頭，高檔整理幅度小，可小倉試單或續抱。",
                "entry_condition": "位於20日高點附近，整理幅度<8%，無長上影或爆量不漲",
                "stop_loss_rule": f"跌破近10日低點（{sl_price:.2f}）",
                "position_suggestion": "小倉或持倉續抱"
            }

    # ── 偏熱多頭 ─────────────────────────────────────────────────────
    elif ms == 'hot_bull':
        if is_ma20:
            if bias20 <= 3.0:
                return {
                    "action": "buy", "label": "等待回測月線後買", "allowed": True,
                    "reason": f"偏熱多頭，距20MA {bias20:.1f}%，屬可接受的回測買點。",
                    "entry_condition": f"距20MA {bias20:.1f}%（需<=3%），低點不破20MA×0.98，今日轉強",
                    "stop_loss_rule": f"跌破20MA或近5日低點（停損{abs_sl:.1f}%）",
                    "position_suggestion": "減半倉位"
                }
            return {
                "action": "wait_pullback", "label": "不追，等距20MA更近", "allowed": False,
                "reason": f"偏熱多頭，距20MA {bias20:.1f}%，需等回測至3%以內再買。",
                "entry_condition": "等待距20MA降至3%以內再評估",
                "stop_loss_rule": "未進場", "position_suggestion": "觀察名單"
            }
        if is_prior_high:
            if abs_sl <= 6.0:
                return {
                    "action": "buy", "label": "只買前高回測不破", "allowed": True,
                    "reason": f"偏熱多頭，前高回測不破，停損{abs_sl:.1f}%可接受（<=6%）。",
                    "entry_condition": f"回測前高區，收盤守住前高×0.97，停損{abs_sl:.1f}%",
                    "stop_loss_rule": f"跌破前高×0.97（{sl_price:.2f}）",
                    "position_suggestion": "減半倉位"
                }
            return {
                "action": "wait_pullback", "label": "停損距離過大，不追", "allowed": False,
                "reason": f"偏熱多頭，停損{abs_sl:.1f}%超過6%，不符保守買法。",
                "entry_condition": "等停損距離降至6%以內再評估",
                "stop_loss_rule": "未進場", "position_suggestion": "觀察名單"
            }
        # breakout / high continuation → not allowed
        return {
            "action": "wait_pullback", "label": "不追，等回測", "allowed": False,
            "reason": f"偏熱多頭，{pattern}型態不適合追，容易買在短線高點。",
            "entry_condition": "等回測至20MA或前高附近再評估",
            "stop_loss_rule": "未進場", "position_suggestion": "觀察名單"
        }

    # ── 過熱多頭 ─────────────────────────────────────────────────────
    elif ms == 'overheated_bull':
        if is_ma20 and bias20 <= 3.0 and abs_sl <= 5.0:
            return {
                "action": "buy", "label": "小倉等回測止跌", "allowed": True,
                "reason": f"過熱多頭，距20MA {bias20:.1f}%且停損{abs_sl:.1f}%符合嚴格條件。",
                "entry_condition": f"距20MA {bias20:.1f}%（需<=3%），停損{abs_sl:.1f}%（需<=5%），今日收紅K或站回5MA",
                "stop_loss_rule": f"跌破20MA或近5日低點，嚴格執行（{sl_price:.2f}）",
                "position_suggestion": "輕倉（1/3以下）"
            }
        if is_prior_high and abs_sl <= 5.0 and today_ch <= 3.0:
            return {
                "action": "wait_pullback", "label": "觀察，不主動追", "allowed": False,
                "reason": f"過熱多頭，前高回測停損{abs_sl:.1f}%尚可，但過熱市場不主動追。",
                "entry_condition": f"停損<=5%且非今日急漲（今日{today_ch:+.1f}%）才考慮",
                "stop_loss_rule": f"跌破前高×0.97（{sl_price:.2f}）",
                "position_suggestion": "觀察名單"
            }
        return {
            "action": "no_trade", "label": "不買，等回測", "allowed": False,
            "reason": f"過熱多頭，{pattern}型態在過熱市場禁止交易，防止追高假突破。",
            "entry_condition": "等急殺後回測20MA轉強再評估",
            "stop_loss_rule": "未進場", "position_suggestion": "場外觀望"
        }

    # Fallback
    return {
        "action": "no_trade", "label": "暫不交易", "allowed": False,
        "reason": "市場狀態或型態不明確，暫不建議進場。",
        "entry_condition": "等待更明確信號",
        "stop_loss_rule": "未進場", "position_suggestion": "場外觀望"
    }
