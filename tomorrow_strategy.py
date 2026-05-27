"""
tomorrow_strategy.py
──────────────────────────────────────────────────────────────────────────────
大盤狀態 × 明日策略選股（第三版）

核心邏輯：
1. 從 market_status.fetch_market_index_daily() 取得加權指數日 K
2. 計算「成本線」(Donchian 中位線)：cost_n = (n 日最高 + n 日最低) / 2
3. 用 pandas EWM 現算 MACD histogram（ema12 / ema26 / dif / dea / hist）
4. 判斷大盤五狀態：強多延伸 / 健康回測 / 高檔過熱 / 弱勢反彈 / 空頭破60
5. 讀取 stock_cache.db → daily_kbars，對每檔股票分級（A / B1 / B2 / C）
6. 商品類型分類，過濾 ETF / 反向ETF / 槓桿ETF / ETN / 權證
7. 流動性過濾（volume_ma20 >= 1000 張 OR amount_ma20 >= 3000 萬元）
8. 計算分數（0~100，§11 評分邏輯）
9. 分類候選：明日可買 / ETF候選 / 高優先觀察 / 其他觀察 / 排除
"""

import os
import sqlite3
import pandas as pd
from datetime import datetime
from typing import Optional, Tuple

# ── 路徑 ──────────────────────────────────────────────────────────────────────
_BASE_DIR = os.path.dirname(os.path.abspath(__file__))
_DB_PATH  = os.path.join(_BASE_DIR, "stock_cache.db")

# ── 常數 ──────────────────────────────────────────────────────────────────────
_MIN_BARS               = 62    # 最少需要 62 根日 K（確保 cost60 可計算）
_RR_THRESHOLD           = 1.5   # 最低風報比門檻（進入可買名單）
_DIST_MAX_BUY           = 8.0   # 距 20 日成本線距離上限（一般可買條件）
_DIST_MAX_BUY_SB        = 3.0   # strong_bull 下更嚴格的距離上限
_DIST_EXCLUDE           = 12.0  # 距 20 日成本線超過此值 → 直接排除

# ── 流動性分層門檻 ────────────────────────────────────────────────────────────
_LIQ_HIGH_VOL           = 3000        # high：vol_ma20 >= 3000 張
_LIQ_HIGH_AMOUNT        = 100_000_000 # high：amount_ma20 >= 1 億元
_LIQ_NORM_VOL           = 1000        # normal：vol_ma20 >= 1000 張
_LIQ_NORM_AMOUNT        = 50_000_000  # normal/low_amount_pass：amount_ma20 >= 5000 萬元
# 舊別名保留，避免其他地方 import 失敗
_LIQ_VOL_THRESHOLD      = _LIQ_NORM_VOL
_LIQ_AMOUNT_THRESHOLD   = _LIQ_NORM_AMOUNT

# ── 商品類型過濾開關 ──────────────────────────────────────────────────────────
INCLUDE_ETF             = False
INCLUDE_ETN             = False
INCLUDE_WARRANT         = False
INCLUDE_REVERSE_ETF     = False

# ── 結果數量上限 ──────────────────────────────────────────────────────────────
_MAX_BUY_COUNT          = 20
_MAX_ETF_COUNT          = 20
_MAX_HIGH_WATCH_COUNT   = 50
_MAX_OTHER_WATCH_COUNT  = 100

# ── 大盤狀態常數 ──────────────────────────────────────────────────────────────
REGIME_STRONG_BULL  = "strong_bull"
REGIME_HEALTHY_PB   = "healthy_pullback"
REGIME_OVERHEATED   = "high_overheated"
REGIME_WEAK_BOUNCE  = "weak_bounce"
REGIME_BEAR_BREAK60 = "bear_break60"

_REGIME_META = {
    REGIME_STRONG_BULL:  ("強多延伸",  "#26de81"),
    REGIME_HEALTHY_PB:   ("健康回測",  "#4facfe"),
    REGIME_OVERHEATED:   ("高檔過熱",  "#ff9f43"),
    REGIME_WEAK_BOUNCE:  ("弱勢反彈",  "#ffd233"),
    REGIME_BEAR_BREAK60: ("空頭破60",  "#ff4444"),
}

_REGIME_STRATEGY = {
    REGIME_STRONG_BULL: {
        "strategy":  "A級強勢股回測優先，距20日成本線≤3%嚴格條件；B1只觀察不買",
        "can_buy":   "A級距20日成本線≤3%，MACD收斂，風報比≥1.5，非近60日高點",
        "forbidden": "B級、C級、距20日成本線>8%、高乖離噴出、ETF混入普通股",
        "position":  "正常倉位",
    },
    REGIME_HEALTHY_PB: {
        "strategy":  "A級回測優先，B1守60轉強可觀察",
        "can_buy":   "A級回測（距20日成本線≤8%）、B1站回20日成本線",
        "forbidden": "B2弱反彈、C級跌破60日成本線、高乖離",
        "position":  "正常偏保守",
    },
    REGIME_OVERHEATED: {
        "strategy":  "只找低乖離A級，不追噴出股",
        "can_buy":   "低乖離A級（距20日成本線≤3%）",
        "forbidden": "高乖離、噴出、爆量長上影",
        "position":  "輕倉（1/3以下）",
    },
    REGIME_WEAK_BOUNCE: {
        "strategy":  "原則不主動買，逆勢強A級可小倉觀察",
        "can_buy":   "原則上不開新倉",
        "forbidden": "B級、C級、跌深反彈",
        "position":  "空倉等待",
    },
    REGIME_BEAR_BREAK60: {
        "strategy":  "不開新倉，只輸出觀察名單",
        "can_buy":   "無",
        "forbidden": "大多數股票",
        "position":  "空倉",
    },
}


# ── DB 連線 ───────────────────────────────────────────────────────────────────
def _get_conn():
    conn = sqlite3.connect(_DB_PATH, timeout=60.0)
    conn.row_factory = sqlite3.Row
    try:
        conn.execute("PRAGMA journal_mode=WAL;")
    except Exception:
        pass
    return conn


# ── 商品類型分類 ──────────────────────────────────────────────────────────────

def classify_instrument(symbol: str, name: str, industry: str) -> Tuple[str, bool]:
    """
    判斷商品類型與是否為 KY 股。
    Returns (instrument_type, is_ky)

    instrument_type:
        common_stock | etf | reverse_etf | leveraged_etf | etn | warrant | preferred_stock | other
    """
    sym = str(symbol   or "").strip()
    nm  = str(name     or "").strip()
    ind = str(industry or "").strip()

    is_ky     = "KY" in nm
    nm_upper  = nm.upper()
    ind_upper = ind.upper()

    # ETN
    if "ETN" in nm_upper or "ETN" in ind_upper:
        return "etn", is_ky

    # 權證：名稱包含購 / 售 / 權證
    if any(kw in nm for kw in ["購", "售", "權證"]):
        return "warrant", is_ky

    # 特別股：名稱以甲特/乙特/丙特結尾，或含「特別股」，或最後一字是「特」
    if (nm.endswith("甲特") or nm.endswith("乙特") or nm.endswith("丙特")
            or "特別股" in nm
            or (len(nm) >= 2
                and nm[-1] == "特"
                and not nm.endswith("特化")
                and not nm.endswith("特材"))):
        return "preferred_stock", is_ky

    # ETF 判斷：代號以「00」開頭且長度 ≤ 7，或名稱/產業含「ETF」
    is_etf_by_sym  = sym.startswith("00") and len(sym) <= 7
    is_etf_by_name = "ETF" in nm_upper or "ETF" in ind_upper

    if is_etf_by_sym or is_etf_by_name:
        # 反向 ETF：名稱含反向關鍵字，或代號以 R 結尾
        if (any(kw in nm for kw in ["反向", "反1", "放空", "空方"])
                or sym.upper().endswith("R")):
            return "reverse_etf", is_ky
        # 槓桿 ETF：名稱含正向放大相關關鍵字
        if any(kw in nm for kw in ["正2", "2倍", "槓桿", "2X", "正向2", "兩倍"]):
            return "leveraged_etf", is_ky
        return "etf", is_ky

    return "common_stock", is_ky


# ── 技術指標工具 ──────────────────────────────────────────────────────────────

def _cost_line(highs: pd.Series, lows: pd.Series, n: int) -> pd.Series:
    """Donchian 中位線：(n 日最高 + n 日最低) / 2。不等於 MA。"""
    return (highs.rolling(n).max() + lows.rolling(n).min()) / 2


def _compute_macd(closes: pd.Series):
    """
    計算 MACD。
    Returns (dif, dea, hist) — 全部為 pd.Series。
    """
    ema12 = closes.ewm(span=12, adjust=False).mean()
    ema26 = closes.ewm(span=26, adjust=False).mean()
    dif   = ema12 - ema26
    dea   = dif.ewm(span=9, adjust=False).mean()
    hist  = dif - dea
    return dif, dea, hist


def _macd_status(hist: pd.Series) -> str:
    """根據最新三根 histogram 判斷 MACD 狀態文字。"""
    if len(hist) < 3:
        return "資料不足"
    h0 = float(hist.iloc[-1])
    h1 = float(hist.iloc[-2])
    h2 = float(hist.iloc[-3])
    if h0 < 0 and h1 < 0 and h2 < 0 and h0 > h1 and h1 > h2:
        return "負柱收斂"
    if h0 > 0 and h0 > h1:
        return "正柱放大"
    if h0 > 0 and h0 < h1:
        return "正柱收斂"
    if h0 < 0 and h0 < h1:
        return "負柱擴大"
    if h0 >= 0:
        return "正柱"
    return "負柱"


# ── 大盤狀態判斷 ──────────────────────────────────────────────────────────────

def calculate_market_regime(taiex_df: Optional[pd.DataFrame]) -> dict:
    """
    從加權指數日 K DataFrame 判斷大盤五狀態。
    taiex_df：columns = date, open, high, low, close, volume
    """

    def _build(status: str, basis: str, metrics: dict) -> dict:
        label, color = _REGIME_META[status]
        strat = _REGIME_STRATEGY[status]
        return {
            "status":    status,
            "label":     label,
            "color":     color,
            "strategy":  strat["strategy"],
            "can_buy":   strat["can_buy"],
            "forbidden": strat["forbidden"],
            "position":  strat["position"],
            "basis":     basis,
            "metrics":   metrics,
        }

    _no_data = {"data_available": False}

    if taiex_df is None or len(taiex_df) < _MIN_BARS:
        n = len(taiex_df) if taiex_df is not None else 0
        return _build(
            REGIME_HEALTHY_PB,
            f"大盤 K 線不足（{n} 根），使用預設健康回測狀態",
            _no_data,
        )

    df = taiex_df.copy().sort_values("date").reset_index(drop=True)

    closes  = df["close"].astype(float)
    highs   = df["high"].astype(float)
    lows    = df["low"].astype(float)
    volumes = df["volume"].astype(float)

    # 成本線
    cost20_s = _cost_line(highs, lows, 20)
    cost60_s = _cost_line(highs, lows, 60)
    c20 = float(cost20_s.iloc[-1])
    c60 = float(cost60_s.iloc[-1])

    if pd.isna(c20) or pd.isna(c60) or c20 <= 0 or c60 <= 0:
        return _build(REGIME_HEALTHY_PB, "成本線計算失敗，使用預設狀態", _no_data)

    # MACD
    try:
        _, _, hist_s = _compute_macd(closes)
        macd_ok = not hist_s.iloc[-3:].isna().any()
    except Exception:
        hist_s  = pd.Series(dtype=float)
        macd_ok = False

    close    = float(closes.iloc[-1])
    vol_now  = float(volumes.iloc[-1])
    vol_ma20_raw = float(volumes.rolling(20).mean().iloc[-1])

    # ── 大盤量能資料不足保護 ─────────────────────────────────────────────
    _vol_data_ok = (vol_now > 0 and vol_ma20_raw > 0
                    and not pd.isna(vol_now) and not pd.isna(vol_ma20_raw))
    if _vol_data_ok:
        vol_shrinking        = vol_now < vol_ma20_raw
        market_volume_status = "量縮" if vol_shrinking else "量未縮"
    else:
        vol_shrinking        = None
        market_volume_status = "量能資料不足"

    _vol_str = "量能資料不足，未納入量能判斷" if vol_shrinking is None else (
        "量縮" if vol_shrinking else "量未縮"
    )

    dist_cost20_pct = (close - c20) / c20 * 100
    dist_cost60_pct = (close - c60) / c60 * 100
    cost20_slope_5d = (
        float(cost20_s.iloc[-1] - cost20_s.iloc[-5])
        if len(cost20_s) >= 6 else 0.0
    )

    macd_status_str = _macd_status(hist_s) if macd_ok else "未知"
    hist_val        = float(hist_s.iloc[-1]) if macd_ok and len(hist_s) > 0 else 0.0
    macd_neg_conv   = macd_status_str == "負柱收斂"
    macd_pos_expand = macd_status_str == "正柱放大"
    macd_neg_expand = macd_status_str == "負柱擴大"

    metrics = {
        "data_available":       True,
        "index_close":          round(close, 2),
        "cost20":               round(c20, 2),
        "cost60":               round(c60, 2),
        "dist_cost20_pct":      round(dist_cost20_pct, 2),
        "dist_cost60_pct":      round(dist_cost60_pct, 2),
        "cost20_slope_5d":      round(cost20_slope_5d, 2),
        "macd_status":          macd_status_str,
        "macd_hist":            round(hist_val, 4),
        "vol_shrinking":        vol_shrinking,
        "market_volume_status": market_volume_status,
        "vol_ma20":             round(vol_ma20_raw, 0) if _vol_data_ok else 0,
        "vol_now":              round(vol_now, 0)      if _vol_data_ok else 0,
    }

    # ── Priority 1: 空頭破60 ──────────────────────────────────────────────
    if close < c60 or (c20 < c60 and macd_neg_expand):
        parts = []
        if close < c60:
            parts.append(f"收盤({close:.0f})跌破60日成本線({c60:.0f})")
        if c20 < c60:
            parts.append(f"20日成本線({c20:.0f}) < 60日成本線({c60:.0f})")
        if macd_neg_expand:
            parts.append("MACD負柱擴大")
        return _build(REGIME_BEAR_BREAK60, "；".join(parts), metrics)

    # ── Priority 2: 弱勢反彈 ──────────────────────────────────────────────
    if (close < c20 and close >= c60
            and cost20_slope_5d < 0
            and hist_val < 0
            and not macd_neg_conv):
        basis = (
            f"收盤({close:.0f})跌破20日成本線({c20:.0f})，"
            f"20日成本線下彎（5日斜率{cost20_slope_5d:+.0f}），"
            f"{macd_status_str}，{_vol_str}"
        )
        return _build(REGIME_WEAK_BOUNCE, basis, metrics)

    # ── Priority 3: 高檔過熱 ──────────────────────────────────────────────
    if close > c20 and dist_cost20_pct > 8.0:
        basis = (
            f"收盤({close:.0f})距20日成本線({c20:.0f}) +{dist_cost20_pct:.1f}%，"
            f"乖離過大，{macd_status_str}"
        )
        return _build(REGIME_OVERHEATED, basis, metrics)

    # ── Priority 4: 強多延伸 ──────────────────────────────────────────────
    if close > c20 and c20 > c60 and (macd_pos_expand or macd_neg_conv):
        basis = (
            f"收盤({close:.0f})站上20日({c20:.0f})與60日({c60:.0f})成本線，"
            f"20日 > 60日成本線，{macd_status_str}"
            + (f"，{_vol_str}" if vol_shrinking is None else "")
        )
        return _build(REGIME_STRONG_BULL, basis, metrics)

    # ── Priority 5: 健康回測 ──────────────────────────────────────────────
    near_cost20 = -5.0 <= dist_cost20_pct <= 5.0
    if close >= c60 and near_cost20 and macd_neg_conv:
        basis = (
            f"收盤({close:.0f})站上60日成本線({c60:.0f})，"
            f"距20日成本線{dist_cost20_pct:+.1f}%，"
            f"MACD負柱收斂，{_vol_str}"
        )
        return _build(REGIME_HEALTHY_PB, basis, metrics)

    # ── Fallback ──────────────────────────────────────────────────────────
    if close > c20 and close > c60 and c20 > c60:
        return _build(REGIME_STRONG_BULL,
                      f"收盤站上雙成本線（{macd_status_str}）", metrics)
    if close >= c60:
        return _build(REGIME_HEALTHY_PB,
                      f"收盤站上60日成本線，距20日{dist_cost20_pct:+.1f}%（{macd_status_str}）",
                      metrics)
    return _build(REGIME_BEAR_BREAK60,
                  f"收盤({close:.0f})低於60日成本線({c60:.0f})", metrics)


# ── 個股分析 ──────────────────────────────────────────────────────────────────

def _analyze_stock(code: str, name: str, industry: str, sub_df: pd.DataFrame) -> dict:
    """
    分析單一股票，回傳分析結果 dict。
    資料不足或計算失敗時 raise ValueError，由主函式攔截並寫入排除清單。
    """
    if len(sub_df) < _MIN_BARS:
        raise ValueError(f"資料不足 {_MIN_BARS} 根（僅 {len(sub_df)} 根）")

    df = sub_df.sort_values("date").reset_index(drop=True)

    closes  = df["close"].astype(float)
    highs   = df["high"].astype(float)
    lows    = df["low"].astype(float)
    volumes = df["volume"].astype(float)
    opens   = df["open"].astype(float)

    # 成本線
    cost20_s = _cost_line(highs, lows, 20)
    cost60_s = _cost_line(highs, lows, 60)
    c20 = float(cost20_s.iloc[-1])
    c60 = float(cost60_s.iloc[-1])

    if pd.isna(c20) or pd.isna(c60) or c20 <= 0 or c60 <= 0:
        raise ValueError("成本線計算結果無效（NaN 或 <= 0）")

    # MACD
    try:
        _, _, hist_s = _compute_macd(closes)
        if len(hist_s) < 3 or hist_s.iloc[-3:].isna().any():
            raise ValueError("histogram 含 NaN")
        macd_status_str = _macd_status(hist_s)
        hist_val        = float(hist_s.iloc[-1])
        h0, h1, h2      = hist_val, float(hist_s.iloc[-2]), float(hist_s.iloc[-3])
        macd_neg_conv   = (h0 < 0 and h1 < 0 and h2 < 0 and h0 > h1 and h1 > h2)
        macd_pos_expand = (h0 > 0 and h0 > h1)
        macd_neg_expand = (h0 < 0 and h0 < h1)
    except Exception as exc:
        raise ValueError(f"MACD 無法計算：{exc}")

    close    = float(closes.iloc[-1])
    open_val = float(opens.iloc[-1])
    high_val = float(highs.iloc[-1])
    low_val  = float(lows.iloc[-1])
    vol_now  = float(volumes.iloc[-1])
    vol_ma20 = float(volumes.rolling(20).mean().iloc[-1]) if len(volumes) >= 20 else vol_now

    # 成交金額估算（volume 單位：張；1 張 = 1000 股）
    amount_ma20 = close * vol_ma20 * 1000

    # cost20 五日斜率
    cost20_slope = (
        float(cost20_s.iloc[-1] - cost20_s.iloc[-5])
        if len(cost20_s) >= 6 else 0.0
    )

    # 前一日高點 / 今日紅K / 近3日觸及cost20後彈升
    prev_high             = float(highs.iloc[-2]) if len(highs) >= 2 else close
    today_is_red_up       = close > open_val
    today_above_prev_high = close > prev_high
    if len(closes) >= 4:
        _pc   = closes.iloc[-4:-1].values.astype(float)
        _pc20 = cost20_s.iloc[-4:-1].values.astype(float)
        _touched = any(c <= cv * 1.005 for c, cv in zip(_pc, _pc20))
        cost20_bounce = bool(_touched) and (close > c20)
    else:
        cost20_bounce = False

    dist_cost20_pct = (close - c20) / c20 * 100
    dist_cost60_pct = (close - c60) / c60 * 100

    # 量能狀態
    vol_shrinking = vol_now < vol_ma20
    down_candle   = close < open_val
    down_vol      = down_candle and vol_now > vol_ma20 * 1.2

    candle_range        = high_val - low_val
    upper_shadow        = high_val - max(open_val, close)
    upper_shadow_ratio  = upper_shadow / candle_range if candle_range > 0 else 0.0
    high_vol_upper_shad = vol_now > vol_ma20 * 1.5 and upper_shadow_ratio > 0.4

    if high_vol_upper_shad:
        volume_status = "高檔爆量長上影"
    elif down_vol:
        volume_status = "下跌放量"
    elif vol_shrinking:
        volume_status = "量縮"
    elif vol_now > vol_ma20 * 1.2:
        volume_status = "放量"
    else:
        volume_status = "量平"

    # 近 60 日最高（壓力基礎）
    high60           = float(highs.rolling(60).max().iloc[-1])
    is_near_60d_high = close >= high60 * 0.98
    is_new_60d_high  = close >= high60
    resistance_status = "接近60日高點，壓力未知" if is_near_60d_high else "正常"

    # 停損 = 近 3 日最低
    recent_lows = lows.iloc[-3:]
    stop_price  = float(recent_lows.min()) if len(recent_lows) >= 3 else low_val

    # 風報比（接近60日高點時標記為無效）
    if is_near_60d_high:
        risk_reward = None
        rr_valid    = False
    else:
        risk   = close - stop_price
        reward = high60 - close
        if risk <= 0 or pd.isna(risk):
            risk_reward = None
            rr_valid    = False
        else:
            risk_reward = round(reward / risk, 2)
            rr_valid    = True

    # ── 個股分級 ────────────────────────────────────────────────────────
    above_c20     = close > c20
    above_c60     = close > c60
    c20_above_c60 = c20 > c60

    is_b2 = (
        above_c60
        and not above_c20
        and cost20_slope < 0
        and (down_vol or macd_neg_expand)
    )

    if above_c20 and above_c60 and c20_above_c60:
        grade = "A";  grade_label = "A級強勢股"; grade_color = "#26de81"
    elif is_b2:
        grade = "B2"; grade_label = "B2弱反彈";  grade_color = "#ff9f43"
    elif above_c60:
        grade = "B1"; grade_label = "B1守60整理"; grade_color = "#4facfe"
    else:
        grade = "C";  grade_label = "C級跌破60"; grade_color = "#ff4444"

    return {
        "symbol":                 code,
        "name":                   name,
        "industry":               industry or "其他",
        "grade":                  grade,
        "grade_label":            grade_label,
        "grade_color":            grade_color,
        "close":                  round(close, 2),
        "cost_20":                round(c20, 2),
        "cost_60":                round(c60, 2),
        "dist_cost20_pct":        round(dist_cost20_pct, 2),
        "dist_cost60_pct":        round(dist_cost60_pct, 2),
        "cost20_slope":           round(cost20_slope, 2),
        "macd_status":            macd_status_str,
        "macd_hist":              round(hist_val, 4),
        "macd_neg_converging":    macd_neg_conv,
        "macd_pos_expanding":     macd_pos_expand,
        "macd_neg_expanding":     macd_neg_expand,
        "volume_status":          volume_status,
        "vol_shrinking":          vol_shrinking,
        "down_vol":               down_vol,
        "high_vol_upper_shadow":  high_vol_upper_shad,
        "volume_ma20":            round(vol_ma20, 1),
        "amount_ma20":            round(amount_ma20, 0),
        "volume_unit_assumption": "lot",
        "stop_price":             round(stop_price, 2),
        "resistance_price":       round(high60, 2),
        "resistance_status":      resistance_status,
        "is_near_60d_high":       is_near_60d_high,
        "is_new_60d_high":        is_new_60d_high,
        "risk_reward":            risk_reward,
        "rr_valid":               rr_valid,
        "today_is_red_up":        today_is_red_up,
        "today_above_prev_high":  today_above_prev_high,
        "cost20_bounce":          cost20_bounce,
    }


# ── 分數計算（§11）────────────────────────────────────────────────────────────

def _calculate_score(s: dict) -> int:
    """§11 評分邏輯，滿分 100，最低 0。"""
    score = 0

    # §11.1 成本線分數（最多 +40）
    if s["close"] > s["cost_60"]:   score += 15
    if s["close"] > s["cost_20"]:   score += 15
    if s["cost_20"] > s["cost_60"]: score += 10

    # §11.2 回測型態（最多 +20，最低 −30）
    d = s["dist_cost20_pct"]
    if -2.0 <= d <= 5.0:
        score += 10
    elif d < -2.0 and s["close"] > s["cost_60"]:
        score -= 5
    if s["close"] < s["cost_60"]:
        score -= 30
    if d > 10.0:
        score -= 10
    d60 = s["dist_cost60_pct"]
    if 0.0 <= d60 <= 3.0:
        score += 8

    # §11.3 量能（最多 +15，最低 −10）
    vs = s["volume_status"]
    if vs == "量縮":
        score += 8
    elif vs == "放量" and s["close"] > s["cost_20"]:
        score += 7
    elif vs == "下跌放量":
        score -= 10
    elif vs == "高檔爆量長上影":
        score -= 10

    # §11.4 MACD（最多 +15，最低 −10）
    ms = s["macd_status"]
    if ms == "負柱收斂":
        score += 10
    elif ms in ("正柱放大", "正柱"):
        score += 8
    elif ms == "正柱收斂":
        score += 4
    elif ms == "負柱擴大":
        score -= 10

    # §11.5 風報比（最多 +10，最低 −10）
    if s["rr_valid"] and s["risk_reward"] is not None:
        rr = s["risk_reward"]
        if rr >= 2.0:
            score += 10
        elif rr >= 1.5:
            score += 6
        elif rr < 1.0:
            score -= 10

    return max(0, min(100, score))


# ── 候選分類 ──────────────────────────────────────────────────────────────────

def _classify_candidate(s: dict, regime: dict):
    """
    判斷個股是「明日可買」「高優先觀察」「其他觀察」或「排除」。
    Returns:
        (category, buy_method, entry_condition, include_reasons, exclude_reasons)
    category: "明日可買" | "高優先觀察" | "其他觀察" | "排除"
    """
    regime_status = regime["status"]
    grade         = s["grade"]
    incl: list = []
    excl: list = []

    # ── 硬排除：C / B2 / 高檔爆量長上影 / 距離過遠 ──────────────────────
    if grade == "C":
        return ("排除", "", "", [],
                [f"C級：收盤({s['close']})跌破60日成本線({s['cost_60']})"])
    if grade == "B2":
        return ("排除", "", "", [],
                ["B2：弱反彈，20日成本線下彎且下跌放量或MACD擴大"])
    if s["high_vol_upper_shadow"]:
        return ("排除", "", "", [],
                ["高檔爆量長上影，收盤遠低於當日高點，籌碼疑問"])
    if s["dist_cost20_pct"] > _DIST_EXCLUDE:
        return ("排除", "", "", [],
                [f"距20日成本線過遠（+{s['dist_cost20_pct']:.1f}%），超過{_DIST_EXCLUDE}%排除門檻"])

    # ── 動態排除因子 ──────────────────────────────────────────────────────
    if s["macd_neg_expanding"]:
        excl.append("MACD負柱擴大，下行動能持續")
    if s["down_vol"]:
        excl.append("下跌放量，賣壓明顯")
    if s["rr_valid"] and s["risk_reward"] is not None and s["risk_reward"] < 1.0:
        excl.append(f"風報比不足（{s['risk_reward']:.2f} < 1.0）")

    if len(excl) >= 2:
        return ("排除", "", "", [], excl)

    hard_excl = len(excl) == 1

    # ── 大盤空頭破60：不可買 ──────────────────────────────────────────────
    if regime_status == REGIME_BEAR_BREAK60:
        if grade == "A" and not hard_excl:
            return ("高優先觀察", "", "大盤空頭破60，等待大盤重新站上60日成本線",
                    [f"收盤({s['close']})站上60日成本線({s['cost_60']})，逆勢強勢"],
                    ["大盤空頭破60，不主動建倉"])
        excl.append("大盤空頭破60，不開新倉")
        return ("排除", "", "", [], excl)

    # ── 弱勢反彈：只保留A級觀察 ──────────────────────────────────────────
    if regime_status == REGIME_WEAK_BOUNCE and grade != "A":
        excl.append("弱勢反彈市場，僅保留逆勢強A級")
        return ("排除", "", "", [], excl)

    # ── 建立入選理由 ──────────────────────────────────────────────────────
    if s["close"] > s["cost_60"]:
        incl.append(f"收盤({s['close']})站上60日成本線({s['cost_60']})")
    if s["close"] > s["cost_20"]:
        incl.append(f"收盤站上20日成本線({s['cost_20']})")
    if s["cost_20"] > s["cost_60"]:
        incl.append("20日成本線高於60日成本線（多頭排列）")
    if s["macd_neg_converging"]:
        incl.append("MACD負柱收斂，動能轉強")
    if s["macd_pos_expanding"]:
        incl.append("MACD正柱放大，強勢確認")
    if s["vol_shrinking"]:
        incl.append("量縮回測，籌碼沉澱")
    if s["rr_valid"] and s["risk_reward"] is not None and s["risk_reward"] >= 1.5:
        incl.append(f"風報比 {s['risk_reward']:.1f}（≥ 1.5）")

    # ── 常用條件速查 ──────────────────────────────────────────────────────
    above_c20     = s["close"] > s["cost_20"]
    c20_above_c60 = s["cost_20"] > s["cost_60"]
    near_cost20   = abs(s["dist_cost20_pct"]) <= _DIST_MAX_BUY
    very_near_20  = abs(s["dist_cost20_pct"]) <= _DIST_MAX_BUY_SB
    macd_ok       = s["macd_neg_converging"] or s["macd_pos_expanding"]
    rr_ok         = (
        not s["rr_valid"]
        or (s["risk_reward"] is not None and s["risk_reward"] >= _RR_THRESHOLD)
    )
    sb_rr_ok      = (
        s["rr_valid"]
        and s["risk_reward"] is not None
        and s["risk_reward"] >= _RR_THRESHOLD
    )

    can_buy    = False
    buy_method = ""
    entry_cond = ""

    if not hard_excl:
        if grade == "A":
            if regime_status == REGIME_STRONG_BULL:
                # 強多延伸嚴格條件：距 <= 3%、必須有有效風報比、非近高點
                no_near_high_block = not (
                    s.get("is_near_60d_high", False) and s["dist_cost20_pct"] > 5.0
                )
                if (above_c20 and c20_above_c60
                        and very_near_20
                        and macd_ok
                        and sb_rr_ok
                        and no_near_high_block
                        and s["volume_status"] != "高檔爆量長上影"):
                    can_buy    = True
                    buy_method = "A級強勢回測（強多嚴格）"
                    entry_cond = (
                        f"距20日成本線{s['dist_cost20_pct']:+.1f}%（≤{_DIST_MAX_BUY_SB}%），"
                        f"明日不破近3日低點({s['stop_price']:.2f})可進場，"
                        f"停損：{s['stop_price']:.2f}"
                    )

            elif regime_status == REGIME_HEALTHY_PB:
                if near_cost20 and macd_ok and rr_ok:
                    can_buy    = True
                    buy_method = "A級強勢回測"
                    entry_cond = (
                        f"回測至20日成本線({s['cost_20']:.2f})附近，"
                        f"明日不破近3日低點({s['stop_price']:.2f})可進場，"
                        f"停損：{s['stop_price']:.2f}"
                    )

            elif regime_status == REGIME_OVERHEATED:
                if very_near_20 and macd_ok and rr_ok:
                    can_buy    = True
                    buy_method = "A級低乖離回測（高檔過熱，輕倉）"
                    entry_cond = (
                        f"距20日成本線{s['dist_cost20_pct']:+.1f}%，"
                        f"明日不破{s['stop_price']:.2f}，輕倉進場"
                    )

        elif grade == "B1":
            if regime_status == REGIME_STRONG_BULL:
                # B1 在 strong_bull 不得進入 buy_candidates
                if above_c20:
                    return ("高優先觀察", "",
                            f"B1站回20日成本線({s['cost_20']:.2f})，強多市場升級觀察，等MACD收斂後評估",
                            incl, ["強多市場B1不進buy，列高優先觀察"])
                else:
                    return ("其他觀察", "",
                            f"B1未站回20日成本線({s['cost_20']:.2f})，強多市場列低優先觀察",
                            incl, excl or ["強多市場B1未站回cost20，列其他觀察"])

            elif regime_status == REGIME_HEALTHY_PB:
                if above_c20 and macd_ok and s["vol_shrinking"] and rr_ok and not hard_excl:
                    can_buy    = True
                    buy_method = "B1站回20日成本線"
                    entry_cond = (
                        f"已站回20日成本線({s['cost_20']:.2f})，"
                        f"量縮MACD收斂，停損近3日低點({s['stop_price']:.2f})"
                    )

    if can_buy:
        return ("明日可買", buy_method, entry_cond, incl, [])

    # ── 觀察條件 ──────────────────────────────────────────────────────────
    watch_cond = ""

    if grade == "A":
        # 近60日高點且距 cost20 > 5% → 高優先觀察（不可買）
        if s.get("is_near_60d_high", False) and s["dist_cost20_pct"] > 5.0:
            watch_cond = (
                f"接近60日高點壓力區（收盤{s['close']:.2f} ≥ 60日高點×98%），"
                f"風報比失效，距20日成本線{s['dist_cost20_pct']:+.1f}% > 5%，等待回測"
            )
            return ("高優先觀察", "", watch_cond, incl, excl)

        if not near_cost20:
            watch_cond = (
                f"等待回測至20日成本線附近"
                f"（目前距 {s['dist_cost20_pct']:+.1f}%，等縮小至 {_DIST_MAX_BUY}% 以內）"
            )
        elif regime_status == REGIME_STRONG_BULL and not very_near_20:
            watch_cond = (
                f"強多市場距20日成本線 {s['dist_cost20_pct']:+.1f}%"
                f"（需 ≤ {_DIST_MAX_BUY_SB}% 才可買），等待更深回測"
            )
        elif not macd_ok:
            watch_cond = (
                f"MACD尚未收斂（目前{s['macd_status']}），"
                f"等待轉為負柱收斂或正柱放大"
            )
        elif not rr_ok and s["risk_reward"] is not None:
            watch_cond = f"風報比不足（{s['risk_reward']:.2f}），等待回檔改善"
        elif hard_excl:
            watch_cond = f"暫時觀察：{excl[0]}"
        else:
            watch_cond = "各項指標接近條件但尚未完全確認，持續觀察"

        # A 級觀察：乖離 ≤ 5% → 高優先；其餘 → 其他觀察
        if abs(s["dist_cost20_pct"]) <= 5.0 and not hard_excl:
            return ("高優先觀察", "", watch_cond, incl, excl)
        return ("其他觀察", "", watch_cond, incl, excl)

    if grade == "B1":
        if not above_c20:
            watch_cond = (
                f"守住60日成本線({s['cost_60']:.2f})，"
                f"等待重新站回20日成本線({s['cost_20']:.2f})"
            )
        else:
            watch_cond = "已站回20日成本線，等待MACD收斂或量縮確認"

        if above_c20 and macd_ok:
            return ("高優先觀察", "", watch_cond, incl, excl)
        return ("其他觀察", "", watch_cond, incl, excl)

    # 預設排除
    return ("排除", "", "", [], excl or ["不符合任何買進或觀察條件"])


# ── 排除清單完整條目建立 ──────────────────────────────────────────────────────

def _build_excluded_entry(stock_info: dict, exclude_reasons: list) -> dict:
    """從完整 stock_info 建立含完整 debug 欄位的 excluded 條目。"""
    return {
        "symbol":           stock_info.get("symbol", ""),
        "name":             stock_info.get("name", ""),
        "industry":         stock_info.get("industry", ""),
        "instrument_type":  stock_info.get("instrument_type", ""),
        "grade":            stock_info.get("grade", "—"),
        "grade_label":      stock_info.get("grade_label", "—"),
        "score":            stock_info.get("score", 0),
        "close":            stock_info.get("close"),
        "cost20":           stock_info.get("cost_20"),
        "cost60":           stock_info.get("cost_60"),
        "dist_cost20_pct":  stock_info.get("dist_cost20_pct"),
        "macd_status":      stock_info.get("macd_status", ""),
        "volume_status":    stock_info.get("volume_status", ""),
        "volume_ma20":      stock_info.get("volume_ma20"),
        "amount_ma20":      stock_info.get("amount_ma20"),
        "stop_price":       stock_info.get("stop_price"),
        "resistance_price": stock_info.get("resistance_price"),
        "resistance_status":stock_info.get("resistance_status", ""),
        "risk_reward":      stock_info.get("risk_reward"),
        "exclude_reasons":  exclude_reasons,
    }


# ── 排序輔助映射 ──────────────────────────────────────────────────────────────
_MACD_SORT  = {"負柱收斂": 0, "正柱放大": 1, "正柱": 2, "正柱收斂": 3, "負柱": 4, "負柱擴大": 5}
_VOL_SORT   = {"量縮": 0, "量平": 1, "放量": 2, "下跌放量": 3, "高檔爆量長上影": 4}
_GRADE_SORT = {"A": 0, "B1": 1, "B2": 2, "C": 3}


# ── 主函式 ────────────────────────────────────────────────────────────────────

def run_tomorrow_strategy() -> dict:
    """
    主入口。回傳：
    {
        "data_date":              "YYYY-MM-DD",
        "market_regime":          {...},
        "buy_candidates":         [...],   # 普通股明日可買，最多 20 檔
        "etf_candidates":         [...],   # ETF 候選，最多 20 檔
        "high_priority_watch":    [...],   # 高優先觀察，最多 50 檔
        "other_watch":            [...],   # 其他觀察，最多 100 檔
        "excluded":               [...],   # 含完整 debug 欄位
        "stats":                  {...},
    }
    """
    # 1. 加權指數資料
    try:
        from market_status import fetch_market_index_daily
        taiex_df = fetch_market_index_daily()
    except Exception as e:
        taiex_df = None
        print(f"[TomorrowStrategy] TAIEX 資料取得失敗: {e}")

    market_regime = calculate_market_regime(taiex_df)

    # 2. 個股資料
    conn = _get_conn()
    try:
        df_all   = pd.read_sql_query(
            "SELECT code, date, open, high, low, close, volume "
            "FROM daily_kbars ORDER BY code, date ASC",
            conn,
        )
        df_names = pd.read_sql_query(
            "SELECT code, name, category FROM stock_names", conn
        )
    except Exception as e:
        print(f"[TomorrowStrategy] 讀取個股資料失敗: {e}")
        conn.close()
        return {
            "data_date":              datetime.now().strftime("%Y-%m-%d"),
            "market_regime":          market_regime,
            "buy_candidates":         [],
            "etf_candidates":         [],
            "high_priority_watch":    [],
            "other_watch":            [],
            "excluded":               [],
            "stats":                  {"error": str(e)},
        }
    finally:
        conn.close()

    name_map = {}
    cat_map  = {}
    if not df_names.empty:
        for _, r in df_names.iterrows():
            name_map[r["code"]] = r["name"]
            cat_map[r["code"]]  = r["category"]

    # ── 資料日期：全市場最新交易日（用於新鮮度硬排除）──────────────────────
    data_date = ""
    if not df_all.empty:
        try:
            data_date = str(df_all.groupby("code")["date"].max().max())
        except Exception:
            pass

    buy_candidates  = []
    etf_candidates  = []
    high_prio_watch = []
    other_watch     = []
    excluded_list   = []

    # ── 快速排除模板（無分析資料時用）────────────────────────────────────
    def _quick_exclude(sym, nm, ind, itype, reasons):
        excluded_list.append({
            "symbol": sym, "name": nm, "industry": ind,
            "instrument_type": itype, "grade": "—", "grade_label": "—",
            "score": 0, "close": None, "cost20": None, "cost60": None,
            "dist_cost20_pct": None, "macd_status": "", "volume_status": "",
            "volume_ma20": None, "amount_ma20": None,
            "stop_price": None, "resistance_price": None, "resistance_status": "",
            "risk_reward": None, "exclude_reasons": reasons,
        })

    for code, grp in df_all.groupby("code"):
        sub_df = grp.sort_values("date").reset_index(drop=True)

        code_str = str(code)
        name     = name_map.get(code, code_str)
        industry = cat_map.get(code, "")

        # 商品類型判斷
        inst_type, is_ky = classify_instrument(code_str, name, industry)

        # ── 快速排除：不支援的商品類型 ────────────────────────────────────
        if inst_type == "reverse_etf":
            _quick_exclude(code_str, name, industry, inst_type,
                           ["反向ETF：預設排除，不納入任何候選清單"])
            continue
        if inst_type == "leveraged_etf":
            _quick_exclude(code_str, name, industry, inst_type,
                           ["槓桿ETF：預設排除，不納入任何候選清單"])
            continue
        if inst_type == "etn" and not INCLUDE_ETN:
            _quick_exclude(code_str, name, industry, inst_type,
                           ["ETN：預設排除（INCLUDE_ETN=False）"])
            continue
        if inst_type == "warrant" and not INCLUDE_WARRANT:
            _quick_exclude(code_str, name, industry, inst_type,
                           ["權證：預設排除（INCLUDE_WARRANT=False）"])
            continue
        if inst_type == "preferred_stock":
            _quick_exclude(code_str, name, industry, inst_type,
                           ["特別股：不納入選股候選"])
            continue

        # ── 資料新鮮度硬排除 ──────────────────────────────────────────────
        last_kbar_date = str(sub_df.iloc[-1]["date"]) if not sub_df.empty else ""
        if data_date and last_kbar_date != data_date:
            _quick_exclude(code_str, name, industry, inst_type, [
                f"資料未同步：最後K線日期 {last_kbar_date}，不等於 data_date {data_date}"
            ])
            continue

        # ── 個股技術分析 ──────────────────────────────────────────────────
        try:
            stock_info = _analyze_stock(
                code     = code_str,
                name     = name,
                industry = industry,
                sub_df   = sub_df,
            )
        except ValueError as e:
            _quick_exclude(code_str, name, industry, inst_type, [str(e)])
            continue

        stock_info["instrument_type"]      = inst_type
        stock_info["is_ky"]               = is_ky
        stock_info["score"]               = _calculate_score(stock_info)
        stock_info["last_kbar_date"]      = last_kbar_date
        stock_info["data_freshness_status"] = "同步"

        # ── 流動性分層 ────────────────────────────────────────────────────
        _vol = stock_info["volume_ma20"]
        _amt = stock_info["amount_ma20"]
        if _vol >= _LIQ_HIGH_VOL and _amt >= _LIQ_HIGH_AMOUNT:
            liquidity_level = "high"
        elif _vol >= _LIQ_NORM_VOL and _amt >= _LIQ_NORM_AMOUNT:
            liquidity_level = "normal"
        elif _vol < _LIQ_NORM_VOL and _amt >= _LIQ_NORM_AMOUNT:
            liquidity_level = "low_amount_pass"
        else:
            liquidity_level = "low"
        stock_info["liquidity_level"] = liquidity_level

        # ── ETF 路徑 ──────────────────────────────────────────────────────
        if inst_type == "etf" and not INCLUDE_ETF:
            grade     = stock_info["grade"]
            is_liquid = liquidity_level in ("high", "normal", "low_amount_pass")
            if grade in ("A", "B1") and is_liquid:
                etf_candidates.append(stock_info)
            else:
                reasons = ["ETF：INCLUDE_ETF=False，放入ETF候選區"]
                if grade not in ("A", "B1"):
                    reasons.append(f"ETF分級{grade}，不符A/B1條件")
                if not is_liquid:
                    reasons.append(
                        f"流動性不足（vol_ma20={_vol:.0f}張，"
                        f"amount_ma20={_amt:,.0f}元）"
                    )
                excluded_list.append(_build_excluded_entry(stock_info, reasons))
            continue

        # ── 普通股流動性過濾 ──────────────────────────────────────────────
        if liquidity_level == "low":
            excluded_list.append(_build_excluded_entry(stock_info, [
                f"流動性不足（vol_ma20={_vol:.0f}張，amount_ma20={_amt:,.0f}元；"
                f"門檻：vol≥{_LIQ_NORM_VOL}張且amount≥{_LIQ_NORM_AMOUNT:,}元）"
            ]))
            continue

        # ── 候選分類 ──────────────────────────────────────────────────────
        category, buy_method, entry_cond, incl_r, excl_r = _classify_candidate(
            stock_info, market_regime
        )

        # ── strong_bull 下 cost20_slope 過濾（普通股進 buy_candidates 需額外確認）
        if (category == "明日可買"
                and market_regime["status"] == REGIME_STRONG_BULL
                and inst_type == "common_stock"):
            slope_ok = (
                stock_info["cost20_slope"] >= 0
                or stock_info.get("cost20_bounce", False)
                or (stock_info.get("today_is_red_up", False)
                    and stock_info.get("today_above_prev_high", False))
            )
            if not slope_ok:
                category   = "高優先觀察"
                entry_cond = (
                    f"成本線20日斜率偏弱（{stock_info['cost20_slope']:+.2f}），"
                    f"等待成本線翻正或近期觸底站回後再確認進場"
                )
                excl_r = [
                    f"強多市場cost20_slope={stock_info['cost20_slope']:+.2f} < 0，"
                    f"且無近3日彈升或紅K突破前高確認，降級至高優先觀察"
                ]

        # ── 低張數流動性強制改路（low_amount_pass → 高優先觀察）─────────────
        if liquidity_level == "low_amount_pass" and category in ("明日可買", "高優先觀察"):
            category   = "高優先觀察"
            liq_note   = "⚠️ 低張數（vol_ma20<1000張），靠成交金額通過，注意掛單流動性"
            entry_cond = (entry_cond + "｜" + liq_note) if entry_cond else liq_note

        stock_info.update({
            "category":        category,
            "buy_method":      buy_method,
            "entry_condition": entry_cond,
            "include_reasons": incl_r,
            "exclude_reasons": excl_r,
        })

        if category == "明日可買":
            buy_candidates.append(stock_info)
        elif category == "高優先觀察":
            high_prio_watch.append(stock_info)
        elif category == "其他觀察":
            other_watch.append(stock_info)
        else:
            excluded_list.append(_build_excluded_entry(stock_info, excl_r))

    # ── 排序 ──────────────────────────────────────────────────────────────
    buy_candidates.sort(key=lambda x: (
        -x["score"],
        -(x["risk_reward"] if x["risk_reward"] is not None else 0.0),
        x["dist_cost20_pct"],
        _MACD_SORT.get(x["macd_status"], 9),
        _VOL_SORT.get(x["volume_status"], 9),
    ))

    high_prio_watch.sort(key=lambda x: (
        _GRADE_SORT.get(x["grade"], 9),
        abs(x["dist_cost20_pct"]),
        _MACD_SORT.get(x["macd_status"], 9),
        -x["score"],
    ))

    other_watch.sort(key=lambda x: (
        _GRADE_SORT.get(x["grade"], 9),
        -x["score"],
        abs(x["dist_cost20_pct"]),
    ))

    etf_candidates.sort(key=lambda x: (
        _GRADE_SORT.get(x["grade"], 9),
        -x["score"],
    ))

    # ── 數量限制 ──────────────────────────────────────────────────────────
    buy_candidates  = buy_candidates[:_MAX_BUY_COUNT]
    etf_candidates  = etf_candidates[:_MAX_ETF_COUNT]
    high_prio_watch = high_prio_watch[:_MAX_HIGH_WATCH_COUNT]
    other_watch     = other_watch[:_MAX_OTHER_WATCH_COUNT]

    # ── 排名編號 ──────────────────────────────────────────────────────────
    for i, s in enumerate(buy_candidates,  1): s["rank"] = i
    for i, s in enumerate(etf_candidates,  1): s["rank"] = i
    for i, s in enumerate(high_prio_watch, 1): s["rank"] = i
    for i, s in enumerate(other_watch,     1): s["rank"] = i

    return {
        "data_date":              data_date or datetime.now().strftime("%Y-%m-%d"),
        "market_regime":          market_regime,
        "buy_candidates":         buy_candidates,
        "etf_candidates":         etf_candidates,
        "high_priority_watch":    high_prio_watch,
        "other_watch":            other_watch,
        "excluded":               excluded_list,
        "stats": {
            "total_analyzed":            (
                len(buy_candidates) + len(etf_candidates)
                + len(high_prio_watch) + len(other_watch)
                + len(excluded_list)
            ),
            "buy_count":                 len(buy_candidates),
            "etf_count":                 len(etf_candidates),
            "high_priority_watch_count": len(high_prio_watch),
            "other_watch_count":         len(other_watch),
            "excluded_count":            len(excluded_list),
        },
    }
