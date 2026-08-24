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
from typing import Mapping, Optional, Tuple
from market_status import TAIEX_SYMBOL
from stock_selection_schema import (
    classification_from_master,
    ensure_stock_selection_schema,
    load_security_master,
)

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
REGIME_DATA_INVALID = "data_invalid"

_REGIME_META = {
    REGIME_STRONG_BULL:  ("強多延伸",  "#26de81"),
    REGIME_HEALTHY_PB:   ("健康回測",  "#4facfe"),
    REGIME_OVERHEATED:   ("高檔過熱",  "#ff9f43"),
    REGIME_WEAK_BOUNCE:  ("弱勢反彈",  "#ffd233"),
    REGIME_BEAR_BREAK60: ("空頭破60",  "#ff4444"),
    REGIME_DATA_INVALID: ("資料無效",   "#ff4444"),
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
    REGIME_DATA_INVALID: {
        "strategy":  "資料日期或必要資料不一致，停止產生選股訊號",
        "can_buy":   "無",
        "forbidden": "所有新進場與 Telegram 精選",
        "position":  "不產生訊號",
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
    ensure_stock_selection_schema(conn)
    return conn


# ── 商品類型分類 ──────────────────────────────────────────────────────────────

def classify_instrument(
    symbol: str,
    name: str,
    industry: str,
    security_record: Optional[Mapping] = None,
) -> Tuple[str, bool]:
    """
    判斷商品類型與是否為 KY 股。
    Returns (instrument_type, is_ky)

    instrument_type:
        common_stock | etf | reverse_etf | leveraged_etf | etn | warrant | preferred_stock | other
    """
    return classification_from_master(
        security_record, symbol=symbol, name=name, industry=industry
    )


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


def _classify_grade(
    close: float,
    cost20: float,
    cost60: float,
    cost20_slope: float,
    down_vol: bool,
    macd_neg_expanding: bool,
) -> Tuple[str, str, str]:
    """Existing A/B1/B2/C definition, extracted for regression testing."""
    above_c20 = close > cost20
    above_c60 = close > cost60
    is_b2 = (
        above_c60
        and not above_c20
        and cost20_slope < 0
        and (down_vol or macd_neg_expanding)
    )
    if above_c20 and above_c60 and cost20 > cost60:
        return "A", "A級強勢股", "#26de81"
    if is_b2:
        return "B2", "B2弱反彈", "#ff9f43"
    if above_c60:
        return "B1", "B1守60整理", "#4facfe"
    return "C", "C級跌破60", "#ff4444"


def _classify_liquidity(volume_ma20: float, amount_ma20: float) -> str:
    """Preserved Milestone-1 liquidity thresholds."""
    if volume_ma20 >= _LIQ_HIGH_VOL and amount_ma20 >= _LIQ_HIGH_AMOUNT:
        return "high"
    if volume_ma20 >= _LIQ_NORM_VOL and amount_ma20 >= _LIQ_NORM_AMOUNT:
        return "normal"
    if volume_ma20 < _LIQ_NORM_VOL and amount_ma20 >= _LIQ_NORM_AMOUNT:
        return "low_amount_pass"
    return "low"


def _calculate_rr_metrics(
    signal_entry: float,
    stop_price: float,
    previous_60d_high: Optional[float],
    current_60d_high: Optional[float],
) -> dict:
    """Calculate point-in-time-safe RR using only the prior 60-day high."""
    target = float(previous_60d_high) if previous_60d_high is not None else None
    current_high = float(current_60d_high) if current_60d_high is not None else None
    risk = signal_entry - stop_price

    if target is None or pd.isna(target) or target <= 0:
        target_status = "target_unavailable"
        rr = None
    elif signal_entry >= target:
        target_status = "breakout_no_defined_target"
        rr = None
    elif risk <= 0 or pd.isna(risk):
        target_status = "invalid_risk"
        rr = None
    else:
        reward = target - signal_entry
        if reward <= 0 or pd.isna(reward):
            target_status = "invalid_reward"
            rr = None
        else:
            target_status = "defined"
            rr = round(reward / risk, 2)

    rr_valid = rr is not None
    rr_buyable = bool(rr_valid and rr >= _RR_THRESHOLD)
    max_entry_rr15 = (
        round((target + _RR_THRESHOLD * stop_price) / (1.0 + _RR_THRESHOLD), 2)
        if target_status == "defined" else None
    )
    return {
        "signal_entry": round(signal_entry, 2),
        "signal_close": round(signal_entry, 2),
        "stop_price": round(stop_price, 2),
        "target_price": round(target, 2) if target is not None and not pd.isna(target) else None,
        "previous_60d_high": round(target, 2) if target is not None and not pd.isna(target) else None,
        "current_60d_high": round(current_high, 2) if current_high is not None and not pd.isna(current_high) else None,
        "target_status": target_status,
        "risk_reward": rr,
        "signal_rr": rr,
        "rr_valid": rr_valid,
        "rr_buyable": rr_buyable,
        "max_entry_rr15": max_entry_rr15,
        "actual_entry": None,
        "skip_trade": None,
    }


def evaluate_actual_entry(actual_entry: Optional[float], max_entry_rr15: Optional[float]) -> Optional[bool]:
    """Return whether an observed next-day entry must be skipped."""
    if actual_entry is None:
        return None
    if max_entry_rr15 is None:
        return True
    return float(actual_entry) > float(max_entry_rr15)


def _liquidity_rank_maps(df_all: pd.DataFrame) -> Tuple[dict, dict]:
    """Cross-sectional percentile ranks for shadow-only liquidity metrics."""
    values = []
    for code, grp in df_all.groupby("code"):
        grp = grp.sort_values("date")
        if len(grp) < 20:
            continue
        close = grp["close"].astype(float)
        volume = grp["volume"].astype(float)
        amount20 = float((close * volume * 1000).rolling(20).mean().iloc[-1])
        volume20 = float(volume.rolling(20).mean().iloc[-1])
        if not pd.isna(amount20) and not pd.isna(volume20):
            values.append((str(code), amount20, volume20))
    if not values:
        return {}, {}
    frame = pd.DataFrame(values, columns=["code", "amount", "volume"])
    frame["amount_rank"] = frame["amount"].rank(pct=True, method="average") * 100
    frame["volume_rank"] = frame["volume"].rank(pct=True, method="average") * 100
    return (
        {row.code: round(float(row.amount_rank), 2) for row in frame.itertuples()},
        {row.code: round(float(row.volume_rank), 2) for row in frame.itertuples()},
    )


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

def calculate_market_regime(taiex_df: Optional[pd.DataFrame], as_of_date: Optional[str] = None) -> dict:
    """
    從加權指數日 K DataFrame 判斷大盤五狀態。
    taiex_df：columns = date, open, high, low, close, volume
    as_of_date：若傳入，則過濾至該日期並驗證最後一筆必須等於 as_of_date。
    """

    def _build(status: str, basis: str, metrics: dict) -> dict:
        label, color = _REGIME_META[status]
        strat = _REGIME_STRATEGY[status]
        strategy_valid = status != REGIME_DATA_INVALID and metrics.get("data_available", True)
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
            "strategy_valid": strategy_valid,
        }

    def _invalid(basis: str, **metrics) -> dict:
        return _build(
            REGIME_DATA_INVALID,
            basis,
            {"data_available": False, "regime_error": True, **metrics},
        )

    if taiex_df is None or len(taiex_df) < _MIN_BARS:
        n = len(taiex_df) if taiex_df is not None else 0
        return _invalid(f"大盤 K 線不足（{n} 根），策略停止", bars=n)

    df = taiex_df.copy().sort_values("date").reset_index(drop=True)

    # as_of_date 驗證：過濾到指定日期，並確認最後一筆 == as_of_date
    if as_of_date is not None:
        df = df[df["date"] <= as_of_date].reset_index(drop=True)
        actual_last = str(df.iloc[-1]["date"]) if not df.empty else "無資料"
        if actual_last != as_of_date:
            print(f"[大盤計算] market_data_date={actual_last} ≠ as_of_date={as_of_date}，拒絕使用舊大盤狀態")
            return _invalid(
                f"大盤資料日期 {actual_last} ≠ 要求日期 {as_of_date}，拒絕使用舊大盤狀態",
                actual_data_date=actual_last,
                expected_data_date=as_of_date,
            )
        if len(df) < _MIN_BARS:
            return _invalid(
                f"過濾至 as_of_date={as_of_date} 後 K 線不足（{len(df)} 根），策略停止",
                bars=len(df), actual_data_date=actual_last,
                expected_data_date=as_of_date,
            )

    closes  = df["close"].astype(float)
    opens   = df["open"].astype(float)
    highs   = df["high"].astype(float)
    lows    = df["low"].astype(float)
    volumes = df["volume"].astype(float)

    # 成本線
    cost20_s = _cost_line(highs, lows, 20)
    cost60_s = _cost_line(highs, lows, 60)
    c20 = float(cost20_s.iloc[-1])
    c60 = float(cost60_s.iloc[-1])

    if pd.isna(c20) or pd.isna(c60) or c20 <= 0 or c60 <= 0:
        return _invalid("大盤成本線計算失敗，策略停止")

    # MACD
    try:
        _, _, hist_s = _compute_macd(closes)
        macd_ok = not hist_s.iloc[-3:].isna().any()
    except Exception:
        hist_s  = pd.Series(dtype=float)
        macd_ok = False
    if not macd_ok:
        return _invalid("大盤 MACD 計算失敗，策略停止")

    close    = float(closes.iloc[-1])
    open_    = float(opens.iloc[-1])
    high_    = float(highs.iloc[-1])
    low_     = float(lows.iloc[-1])
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

    _day_range      = high_ - low_
    _close_pos      = (close - low_) / _day_range if _day_range > 0 else 0.5
    _day_declining  = close < open_
    _close_near_low = _close_pos < 0.4

    metrics = {
        "data_available":            True,
        "index_close":               round(close, 2),
        "index_open":                round(open_, 2),
        "index_high":                round(high_, 2),
        "index_low":                 round(low_, 2),
        "market_day_declining":      _day_declining,
        "market_close_near_low":     _close_near_low,
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

    # 每日成交額後再取平均（volume 單位：張；1 張 = 1000 股）。
    daily_amount = closes * volumes * 1000
    amount_ma5 = float(daily_amount.rolling(5).mean().iloc[-1])
    amount_ma20 = float(daily_amount.rolling(20).mean().iloc[-1])
    liquidity_trend = amount_ma5 / amount_ma20 if amount_ma20 > 0 else None

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

    # current high 只供顯示；RR/near-high 使用訊號日前 previous 60d high。
    current_60d_high = float(highs.rolling(60).max().iloc[-1])
    previous_60d_high_raw = highs.shift(1).rolling(60).max().iloc[-1]
    previous_60d_high = (
        float(previous_60d_high_raw) if not pd.isna(previous_60d_high_raw) else None
    )
    is_near_60d_high = bool(
        previous_60d_high is not None and close >= previous_60d_high * 0.98
    )
    is_new_60d_high = bool(
        previous_60d_high is not None and close >= previous_60d_high
    )

    # 停損 = 近 3 日最低
    recent_lows = lows.iloc[-3:]
    stop_price  = float(recent_lows.min()) if len(recent_lows) >= 3 else low_val

    rr_metrics = _calculate_rr_metrics(
        signal_entry=close,
        stop_price=stop_price,
        previous_60d_high=previous_60d_high,
        current_60d_high=current_60d_high,
    )
    if rr_metrics["target_status"] == "breakout_no_defined_target":
        resistance_status = "突破前60日高點，尚無已定義目標"
    elif is_near_60d_high:
        resistance_status = "接近前60日高點，壓力區"
    else:
        resistance_status = "正常"

    # ── 個股分級 ────────────────────────────────────────────────────────
    grade, grade_label, grade_color = _classify_grade(
        close, c20, c60, cost20_slope, down_vol, macd_neg_expand
    )

    return {
        "symbol":                 code,
        "name":                   name,
        "industry":               industry or "其他",
        "grade":                  grade,
        "grade_label":            grade_label,
        "grade_color":            grade_color,
        "close":                  round(close, 2),
        "open_price":             round(open_val, 2),
        "high_price":             round(high_val, 2),
        "low_price":              round(low_val, 2),
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
        "amount_ma5":             round(amount_ma5, 0),
        "amount_ma20":            round(amount_ma20, 0),
        "liquidity_trend":         round(liquidity_trend, 4) if liquidity_trend is not None else None,
        "volume_unit_assumption": "lot",
        "stop_price":             rr_metrics["stop_price"],
        "resistance_price":       rr_metrics["target_price"],
        "target_price":           rr_metrics["target_price"],
        "previous_60d_high":      rr_metrics["previous_60d_high"],
        "current_60d_high":       rr_metrics["current_60d_high"],
        "target_status":          rr_metrics["target_status"],
        "resistance_status":      resistance_status,
        "is_near_60d_high":       is_near_60d_high,
        "is_new_60d_high":        is_new_60d_high,
        "risk_reward":            rr_metrics["risk_reward"],
        "signal_rr":              rr_metrics["signal_rr"],
        "rr_valid":               rr_metrics["rr_valid"],
        "rr_buyable":             rr_metrics["rr_buyable"],
        "signal_entry":           rr_metrics["signal_entry"],
        "signal_close":           rr_metrics["signal_close"],
        "max_entry_rr15":         rr_metrics["max_entry_rr15"],
        "actual_entry":           rr_metrics["actual_entry"],
        "skip_trade":             rr_metrics["skip_trade"],
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
    # Milestone 1 correctness gate: RR=None/invalid can never enter buy.
    rr_ok = bool(s.get("rr_buyable", False))
    sb_rr_ok = rr_ok

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
        elif not rr_ok:
            if s["risk_reward"] is not None:
                watch_cond = f"風報比不足（{s['risk_reward']:.2f}），等待回檔改善"
            elif s.get("target_status") == "breakout_no_defined_target":
                watch_cond = "已突破前60日高點，尚無已定義目標，不能以無效RR進入可買"
            else:
                watch_cond = "風報比無效或目標無法合理定義，不能進入可買"
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
        "amount_ma5":       stock_info.get("amount_ma5"),
        "amount_ma20":      stock_info.get("amount_ma20"),
        "liquidity_trend":   stock_info.get("liquidity_trend"),
        "amount_rank":       stock_info.get("amount_rank"),
        "volume_rank":       stock_info.get("volume_rank"),
        "stop_price":       stock_info.get("stop_price"),
        "resistance_price": stock_info.get("resistance_price"),
        "target_price":      stock_info.get("target_price"),
        "previous_60d_high": stock_info.get("previous_60d_high"),
        "current_60d_high":  stock_info.get("current_60d_high"),
        "target_status":     stock_info.get("target_status"),
        "resistance_status":stock_info.get("resistance_status", ""),
        "risk_reward":      stock_info.get("risk_reward"),
        "signal_rr":        stock_info.get("signal_rr"),
        "rr_valid":         stock_info.get("rr_valid", False),
        "rr_buyable":       stock_info.get("rr_buyable", False),
        "signal_entry":     stock_info.get("signal_entry"),
        "signal_close":     stock_info.get("signal_close"),
        "max_entry_rr15":   stock_info.get("max_entry_rr15"),
        "actual_entry":     stock_info.get("actual_entry"),
        "skip_trade":       stock_info.get("skip_trade"),
        "exclude_reasons":  exclude_reasons,
    }


# ── 排序輔助映射 ──────────────────────────────────────────────────────────────
_MACD_SORT  = {"負柱收斂": 0, "正柱放大": 1, "正柱": 2, "正柱收斂": 3, "負柱": 4, "負柱擴大": 5}
_VOL_SORT   = {"量縮": 0, "量平": 1, "放量": 2, "下跌放量": 3, "高檔爆量長上影": 4}
_GRADE_SORT = {"A": 0, "B1": 1, "B2": 2, "C": 3}


# ── 主函式 ────────────────────────────────────────────────────────────────────

def _invalid_strategy_result(
    as_of_date: str,
    market_regime: dict,
    errors: list[str],
    stock_kbar_date: str = "",
) -> dict:
    """Fail-closed result: never emits a candidate or Telegram-eligible row."""
    return {
        "status": "invalid_data",
        "strategy_valid": False,
        "as_of_date": as_of_date,
        "data_date": as_of_date,
        "stock_kbar_date": stock_kbar_date,
        "market_regime": market_regime,
        "buy_candidates": [],
        "etf_candidates": [],
        "high_priority_watch": [],
        "other_watch": [],
        "excluded": [],
        "data_errors": list(errors),
        "stats": {
            "total_analyzed": 0,
            "buy_count": 0,
            "etf_count": 0,
            "high_priority_watch_count": 0,
            "other_watch_count": 0,
            "excluded_count": 0,
            "error": "；".join(errors),
        },
    }


def run_tomorrow_strategy(
    as_of_date: Optional[str] = None,
    data_date: Optional[str] = None,
) -> dict:
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
    # ``data_date`` is a backwards-compatible alias only.  All internal logic
    # uses the single point-in-time boundary ``as_of_date``.
    if data_date is not None:
        if as_of_date is not None and str(as_of_date) != str(data_date):
            invalid_regime = calculate_market_regime(None, as_of_date=str(as_of_date))
            return _invalid_strategy_result(
                str(as_of_date), invalid_regime,
                [f"as_of_date {as_of_date} 與 legacy data_date {data_date} 不一致"],
            )
        as_of_date = str(data_date)

    # 1. 個股資料。SQL 層先套 date <= as_of_date，禁止 future rows 進記憶體。
    conn = _get_conn()
    try:
        if as_of_date:
            df_all = pd.read_sql_query(
                "SELECT code, date, open, high, low, close, volume "
                "FROM daily_kbars WHERE date <= ? ORDER BY code, date ASC",
                conn, params=[str(as_of_date)],
            )
        else:
            df_all = pd.read_sql_query(
                "SELECT code, date, open, high, low, close, volume "
                "FROM daily_kbars ORDER BY code, date ASC",
                conn,
            )
        df_names = pd.read_sql_query(
            "SELECT code, name, category FROM stock_names", conn
        )
        security_map = load_security_master(conn)
    except Exception as e:
        print(f"[TomorrowStrategy] 讀取個股資料失敗: {e}")
        invalid_regime = calculate_market_regime(None, as_of_date=as_of_date)
        return _invalid_strategy_result(
            str(as_of_date or ""), invalid_regime,
            [f"個股必要資料讀取失敗：{e}"],
        )
    finally:
        conn.close()

    stock_kbar_date = ""
    if not df_all.empty:
        stock_kbar_date = str(df_all["date"].max())
    if not as_of_date:
        as_of_date = stock_kbar_date
    as_of_date = str(as_of_date or "")

    # 2. 加權指數資料，亦只允許 calculate_market_regime 使用 <= as_of_date。
    try:
        from market_status import fetch_market_index_daily
        taiex_df = fetch_market_index_daily()
    except Exception as e:
        taiex_df = None
        print(f"[TomorrowStrategy] TAIEX 資料取得失敗: {e}")

    market_regime = calculate_market_regime(taiex_df, as_of_date=as_of_date)

    # 大盤資料最後日期 — 寫入 market_regime 供日期驗證使用
    if taiex_df is not None and not taiex_df.empty:
        try:
            _tmp_df = taiex_df.copy().sort_values("date")
            if as_of_date:
                _tmp_df = _tmp_df[_tmp_df["date"] <= as_of_date]
            taiex_last_date = str(_tmp_df.iloc[-1]["date"]) if not _tmp_df.empty else ""
            if taiex_last_date:
                market_regime["data_date"] = taiex_last_date
        except Exception:
            pass
    print(f"[大盤計算] market_symbol={TAIEX_SYMBOL}, market_data_date={market_regime.get('data_date', '未知')}, "
          f"market_close={market_regime.get('metrics', {}).get('index_close', 0)}, "
          f"market_regime={market_regime.get('status', '未知')}")

    name_map = {}
    cat_map  = {}
    if not df_names.empty:
        for _, r in df_names.iterrows():
            name_map[r["code"]] = r["name"]
            cat_map[r["code"]]  = r["category"]

    # 全域必要資料日期不一致時 fail closed；不再改寫 as_of_date 或 fallback。
    date_errors = []
    if not as_of_date:
        date_errors.append("as_of_date 缺失")
    if not stock_kbar_date:
        date_errors.append("個股日K無可用資料")
    elif stock_kbar_date != as_of_date:
        date_errors.append(
            f"個股日K最新日期 {stock_kbar_date} ≠ as_of_date {as_of_date}"
        )
    if not market_regime.get("strategy_valid", False):
        date_errors.append(market_regime.get("basis") or "大盤必要資料無效")
    if date_errors:
        return _invalid_strategy_result(
            as_of_date, market_regime, date_errors, stock_kbar_date=stock_kbar_date
        )

    amount_rank_map, volume_rank_map = _liquidity_rank_maps(df_all)

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
            "volume_ma20": None, "amount_ma5": None, "amount_ma20": None,
            "liquidity_trend": None, "amount_rank": None, "volume_rank": None,
            "stop_price": None, "resistance_price": None, "resistance_status": "",
            "target_price": None, "previous_60d_high": None, "current_60d_high": None,
            "target_status": "target_unavailable", "risk_reward": None,
            "signal_rr": None, "rr_valid": False, "rr_buyable": False,
            "signal_entry": None, "signal_close": None, "max_entry_rr15": None,
            "actual_entry": None, "skip_trade": None, "exclude_reasons": reasons,
        })

    for code, grp in df_all.groupby("code"):
        sub_df = grp.sort_values("date").reset_index(drop=True)

        code_str = str(code)
        name     = name_map.get(code, code_str)
        industry = cat_map.get(code, "")

        # 商品類型判斷
        inst_type, is_ky = classify_instrument(
            code_str, name, industry, security_map.get(code_str)
        )

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
        if as_of_date and last_kbar_date != as_of_date:
            _quick_exclude(code_str, name, industry, inst_type, [
                f"資料未同步：最後K線日期 {last_kbar_date}，不等於 as_of_date {as_of_date}"
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
        stock_info["amount_rank"]          = amount_rank_map.get(code_str)
        stock_info["volume_rank"]          = volume_rank_map.get(code_str)
        stock_info["score"]               = _calculate_score(stock_info)
        stock_info["last_kbar_date"]      = last_kbar_date
        stock_info["data_freshness_status"] = "同步"

        # ── 流動性分層 ────────────────────────────────────────────────────
        _vol = stock_info["volume_ma20"]
        _amt = stock_info["amount_ma20"]
        liquidity_level = _classify_liquidity(_vol, _amt)
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
        "status":                 "success",
        "strategy_valid":         True,
        "as_of_date":             as_of_date,
        "data_date":              as_of_date,
        "stock_kbar_date":        stock_kbar_date,
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
