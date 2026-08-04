import os
import json
import html as _html_mod
import asyncio
import calendar
import sqlite3
import threading
import time
import urllib.request
from datetime import datetime, timedelta
from fastapi import FastAPI, WebSocket, WebSocketDisconnect, HTTPException
from fastapi.staticfiles import StaticFiles
from fastapi.responses import FileResponse
from pydantic import BaseModel
import pandas as pd
from dotenv import load_dotenv, set_key
from apscheduler.schedulers.asyncio import AsyncIOScheduler
from apscheduler.triggers.cron import CronTrigger
import screener            # 引入我們的選股大腦
import tomorrow_strategy  # 大盤狀態 × 明日策略選股
import integrated_strategy  # 整合選股（主決策＋籌碼輔助）
import broker_analysis  # key broker analysis tab
import broker_fetcher  # official on-demand broker data fetcher
import moneydj_fetcher  # MoneyDJ Fubon broker period summary fetcher
from market_status import sync_taiex_daily_kbars, normalize_date, TAIEX_SYMBOL
from fubon_market_data import FubonMarketDataClient, FubonMarketDataError

load_dotenv(os.path.join(os.path.dirname(os.path.abspath(__file__)), '.env'))

_BASE_DIR_MAIN = os.path.dirname(os.path.abspath(__file__))
_STOCK_DB_PATH = os.path.join(_BASE_DIR_MAIN, "stock_cache.db")

_tg_push_status = {
    "last_push_time":   None,
    "last_push_status": None,
    "last_picks":       0,
    "last_watch":       0,
    "last_error":       None,
    "target_count":     0,
    "sent_count":       0,
}
_last_integrated_result = None

app = FastAPI(title="TXF Pro Viewer Backend")

def safe_float(val, default=0.0):
    try:
        return float(val) if val is not None else default
    except:
        return default

def safe_int(val, default=0):
    try:
        return int(val) if val is not None else default
    except:
        return default

def sanitize_for_json(obj):
    """Recursively convert numpy scalars to Python natives so json.dumps never chokes."""
    if isinstance(obj, dict):
        return {k: sanitize_for_json(v) for k, v in obj.items()}
    if isinstance(obj, list):
        return [sanitize_for_json(v) for v in obj]
    # numpy scalar detection without importing numpy (avoids hard dep in main.py)
    t = type(obj)
    module = getattr(t, '__module__', '') or ''
    if module.startswith('numpy'):
        if hasattr(obj, 'item'):
            return obj.item()  # converts any numpy scalar to Python native
    return obj

# 全域行情 API 實例與狀態（只使用富邦行情，不含任何交易功能）
api = FubonMarketDataClient()
is_logged_in = False
contract = None
main_loop = None
_kbars_lock = asyncio.Lock()  # 富邦 REST 查詢全局序列化，避免碰觸速率限制
_kbars_retry_after: dict[tuple[str, str], float] = {}
_kbars_retry_lock = threading.Lock()
_kbars_forced_refresh_after: dict[str, float] = {}
_KBARS_FORCED_REFRESH_COOLDOWN_SECONDS = 45
_HISTORY_START = datetime(2025, 1, 1)  # 歷史補取目標起點

# 即時 bar 累積（每 1 min flush 進 SQLite，讓歷史圖不需重啟就有今日資料）
_rt_bar: dict = {}
_rt_bar_lock = threading.Lock()
_rt_contract_code: str = None  # 已解析的月份合約代碼，例如 TXFE6

_quote_state_lock = threading.Lock()
_last_tick_monotonic = 0.0
_last_tick_received_at = 0
_last_tick_exchange_time = 0
_last_tick_code = None
_quote_tick_count = 0
_quote_tick_count_since_log = 0
_quote_log_monotonic = 0.0
_quote_subscription_status = "idle"
_quote_event_detail = ""

_WEIGHTED_STOCK_DEFS = (
    ("2330", "台積電"),
    ("2454", "聯發科"),
    ("2317", "鴻海"),
    ("2308", "台達電"),
)
_WEIGHTED_STOCK_CODES = {code for code, _ in _WEIGHTED_STOCK_DEFS}
_weighted_stock_state_lock = threading.Lock()
_weighted_stock_contracts = {}
_weighted_stock_stream_state = {}
_contract_info_lock = threading.Lock()
_contract_info_cache = {}

last_snapshot_cache = {
    "open": 0,
    "high": 0,
    "low": 0,
    "close": 0,
    "volume": 0,
    "total_volume": 0,
    "reference": 0,
    "time": None,
    "code": None,
    "source": "none",
}


def _quote_scalar(value):
    """Normalize scalar and legacy one-element list payloads."""
    while isinstance(value, (list, tuple)):
        if not value:
            return None
        value = value[-1]
    return value


def _quote_field(quote, *names, default=None):
    if isinstance(quote, dict):
        lower_map = {str(k).lower(): v for k, v in quote.items()}
        for name in names:
            if name in quote:
                return _quote_scalar(quote[name])
            lowered = name.lower()
            if lowered in lower_map:
                return _quote_scalar(lower_map[lowered])
        return default
    for name in names:
        if hasattr(quote, name):
            return _quote_scalar(getattr(quote, name))
    return default


def _quote_timestamp(quote) -> int:
    """Return the exchange event time as real UTC epoch seconds."""
    value = _quote_field(quote, "datetime", "ts")
    if isinstance(value, datetime):
        return int(value.timestamp())
    if isinstance(value, (int, float)):
        numeric = float(value)
        if numeric > 1e17:       # nanoseconds
            numeric /= 1e9
        elif numeric > 1e14:     # microseconds
            numeric /= 1e6
        elif numeric > 1e11:     # milliseconds
            numeric /= 1e3
        if numeric > 0:
            return int(numeric)
    if isinstance(value, str) and ("-" in value or "T" in value):
        try:
            return int(datetime.fromisoformat(value.replace("Z", "+00:00")).timestamp())
        except ValueError:
            pass
    return int(time.time())


def _new_weighted_stock_state(code: str, name: str) -> dict:
    return {
        "code": code,
        "name": name,
        "contract": None,
        "tick_subscription": "idle",
        "kbar_subscription": "idle",
        "last_tick_received_at": 0,
        "last_tick_exchange_time": 0,
        "last_kbar_received_at": 0,
        "last_kbar_exchange_time": 0,
        "tick_count": 0,
        "kbar_count": 0,
        "last": 0.0,
        "avg": 0.0,
        "open": 0.0,
        "reference": 0.0,
        "error": None,
    }


def _reset_weighted_stock_stream_state():
    global _weighted_stock_contracts, _weighted_stock_stream_state
    with _weighted_stock_state_lock:
        _weighted_stock_contracts = {}
        _weighted_stock_stream_state = {
            code: _new_weighted_stock_state(code, name)
            for code, name in _WEIGHTED_STOCK_DEFS
        }


_reset_weighted_stock_stream_state()


def _weighted_stock_health_data() -> dict:
    now = int(time.time())
    with _weighted_stock_state_lock:
        stocks = []
        for code, name in _WEIGHTED_STOCK_DEFS:
            state = dict(
                _weighted_stock_stream_state.get(code)
                or _new_weighted_stock_state(code, name)
            )
            tick_at = safe_int(state.get("last_tick_received_at"), 0)
            kbar_at = safe_int(state.get("last_kbar_received_at"), 0)
            state["last_tick_age"] = now - tick_at if tick_at else None
            state["last_kbar_age"] = now - kbar_at if kbar_at else None
            stocks.append(state)
    return {
        "logged_in": is_logged_in,
        "provider": "fubon",
        "source": "fubon_stock_marketdata",
        "websocket_connected": bool(getattr(api, "stock_connected", False)),
        "fubon_version": getattr(api, "version", "unknown"),
        "kbar_callback_available": True,
        "stocks": stocks,
    }


def _stream_callback_broadcast(message_type: str, payload: dict):
    if not main_loop:
        return
    asyncio.run_coroutine_threadsafe(
        manager.broadcast(json.dumps({"type": message_type, "data": payload})),
        main_loop,
    )


def _weighted_kbar_timestamp(kbar) -> int:
    """Convert Taiwan wall-clock KBar date/time to real UTC epoch."""
    date_value = _quote_field(kbar, "date", "Date")
    time_value = _quote_field(kbar, "time", "Time")
    if date_value is not None and time_value is not None:
        date_text = (
            date_value.strftime("%Y-%m-%d")
            if hasattr(date_value, "strftime")
            else str(date_value)
        )
        time_text = (
            time_value.strftime("%H:%M:%S.%f")
            if hasattr(time_value, "strftime")
            else str(time_value)
        )
        try:
            local_dt = datetime.fromisoformat(
                f"{date_text.replace('/', '-')}T{time_text}"
            )
            return int(calendar.timegm(local_dt.timetuple()) - 28800)
        except (TypeError, ValueError):
            pass
    return _quote_timestamp(kbar)


def _handle_weighted_stock_tick(quote):
    code = str(_quote_field(quote, "code", "Code") or "")
    if code not in _WEIGHTED_STOCK_CODES:
        return
    price = safe_float(_quote_field(quote, "close", "Close", "price", "Price"), 0)
    if price <= 0:
        return

    event_time = _quote_timestamp(quote)
    received_at = int(time.time())
    tick_volume = max(0, safe_int(_quote_field(quote, "volume", "Volume"), 0))
    total_volume = max(
        0, safe_int(_quote_field(quote, "total_volume", "TotalVolume"), 0)
    )
    avg_price = safe_float(_quote_field(quote, "avg_price", "AvgPrice"), 0)
    open_price = safe_float(_quote_field(quote, "open", "Open"), 0)
    reference = safe_float(
        _quote_field(quote, "reference", "Reference", "yesterday_price"), 0
    )

    with _weighted_stock_state_lock:
        state = _weighted_stock_stream_state.setdefault(
            code,
            _new_weighted_stock_state(
                code, dict(_WEIGHTED_STOCK_DEFS).get(code, code)
            ),
        )
        if reference <= 0:
            reference = safe_float(state.get("reference"), 0)
        if open_price <= 0:
            open_price = safe_float(state.get("open"), 0) or price
        if avg_price <= 0:
            avg_price = safe_float(state.get("avg"), 0) or price
        state.update({
            "tick_subscription": "live",
            "last_tick_received_at": received_at,
            "last_tick_exchange_time": event_time,
            "tick_count": safe_int(state.get("tick_count"), 0) + 1,
            "last": price,
            "avg": avg_price,
            "open": open_price,
            "reference": reference,
            "error": None,
        })

    change = price - reference if reference else 0
    _stream_callback_broadcast("weighted_stock_tick", {
        "code": code,
        "time": event_time,
        "price": price,
        "avg": avg_price,
        "open": open_price,
        "reference": reference,
        "change": change,
        "change_pct": (change / reference * 100) if reference else 0,
        "tick_volume": tick_volume,
        "total_volume": total_volume,
        "source": "market_stream",
    })


def weighted_stock_kbar_callback(*args):
    """Normalize a server-side realtime one-minute stock KBar."""
    try:
        if not args:
            return
        kbar = args[-1]
        code = str(_quote_field(kbar, "code", "Code") or "")
        if code not in _WEIGHTED_STOCK_CODES:
            return

        close_price = safe_float(_quote_field(kbar, "close", "Close"), 0)
        if close_price <= 0:
            return
        event_time = _weighted_kbar_timestamp(kbar)
        received_at = int(time.time())
        open_price = safe_float(_quote_field(kbar, "open", "Open"), close_price)
        volume = max(0, safe_int(_quote_field(kbar, "volume", "Volume"), 0))
        amount = max(0.0, safe_float(_quote_field(kbar, "amount", "Amount"), 0))
        candle_average = safe_float(
            _quote_field(kbar, "average", "Average", "avg_price", "AvgPrice"),
            0,
        )

        with _weighted_stock_state_lock:
            state = _weighted_stock_stream_state.setdefault(
                code,
                _new_weighted_stock_state(
                    code, dict(_WEIGHTED_STOCK_DEFS).get(code, code)
                ),
            )
            reference = safe_float(state.get("reference"), 0)
            avg_price = candle_average or safe_float(state.get("avg"), 0) or close_price
            state.update({
                "kbar_subscription": "live",
                "last_kbar_received_at": received_at,
                "last_kbar_exchange_time": event_time,
                "kbar_count": safe_int(state.get("kbar_count"), 0) + 1,
                "last": close_price,
                "avg": avg_price,
                "open": safe_float(state.get("open"), 0) or open_price,
                "error": None,
            })

        change = close_price - reference if reference else 0
        _stream_callback_broadcast("weighted_stock_kbar", {
            "code": code,
            "time": event_time,
            "open": open_price,
            "high": safe_float(_quote_field(kbar, "high", "High"), close_price),
            "low": safe_float(_quote_field(kbar, "low", "Low"), close_price),
            "close": close_price,
            "avg": avg_price,
            "volume": volume,
            "amount": amount,
            "tick_count": max(
                0, safe_int(_quote_field(kbar, "tick_count", "TickCount"), 0)
            ),
            "reference": reference,
            "change": change,
            "change_pct": (change / reference * 100) if reference else 0,
            "source": "market_kbar",
        })
    except Exception as exc:
        print(f"[WEIGHTED_KBAR] 即時 K 棒處理異常: {exc}")


def _contract_reference(contract_obj=None) -> float:
    contract_obj = contract_obj or contract
    if not contract_obj:
        return 0.0
    direct_reference = safe_float(getattr(contract_obj, "reference", 0.0), 0.0)
    if direct_reference > 0:
        return direct_reference
    code = str(getattr(contract_obj, "code", "") or "")
    with _contract_info_lock:
        info = _contract_info_cache.get(code)
    return safe_float(getattr(info, "reference", 0.0), 0.0) if info else 0.0


def _resolve_market_contract(api_instance, code: str):
    """Resolve a futures alias through Fubon's current contract list."""
    if not api_instance or not code:
        return None
    code = str(code).strip().upper()
    if not code.startswith(("TXF", "MXF", "TMF")):
        return None
    resolved = api_instance.resolve_contract(code)
    if resolved:
        with _contract_info_lock:
            _contract_info_cache[code] = resolved
    return resolved


def _reset_quote_state(contract_obj=None):
    global _last_tick_monotonic, _last_tick_received_at, _last_tick_exchange_time
    global _last_tick_code, _quote_tick_count, _quote_tick_count_since_log
    global _quote_log_monotonic, _quote_subscription_status, _quote_event_detail

    code = getattr(contract_obj, "code", None) if contract_obj else None
    reference = _contract_reference(contract_obj)
    with _quote_state_lock:
        _last_tick_monotonic = 0.0
        _last_tick_received_at = 0
        _last_tick_exchange_time = 0
        _last_tick_code = None
        _quote_tick_count = 0
        _quote_tick_count_since_log = 0
        _quote_log_monotonic = 0.0
        _quote_subscription_status = "subscribing" if contract_obj else "idle"
        _quote_event_detail = ""
        last_snapshot_cache.clear()
        last_snapshot_cache.update({
            "open": 0,
            "high": 0,
            "low": 0,
            "close": 0,
            "volume": 0,
            "total_volume": 0,
            "reference": reference,
            "time": None,
            "code": code,
            "source": "none",
        })


def _selected_quote_codes() -> set:
    if not contract:
        return set()
    values = {
        getattr(contract, "code", None),
        getattr(contract, "target_code", None),
        getattr(contract, "symbol", None),
    }
    return {str(v) for v in values if v}


def _quote_matches_selected_contract(quote) -> bool:
    tick_code = _quote_field(quote, "code", "Code")
    if not tick_code or not contract:
        return True
    tick_code = str(tick_code)
    selected_codes = _selected_quote_codes()
    if tick_code in selected_codes:
        return True
    selected_code = str(getattr(contract, "code", ""))
    # TXFR1/MXFR1/TMFR1 callbacks use the resolved monthly target code.
    if selected_code.endswith(("R1", "R2")):
        return tick_code.startswith(selected_code[:-2])
    return False


def _update_snapshot_cache(quote, price: float, event_time: int, source: str) -> dict:
    """Update the UI snapshot from authoritative streaming/snapshot fields."""
    tick_volume = max(0, safe_int(_quote_field(quote, "volume", "Volume"), 0))
    total_volume_value = _quote_field(quote, "total_volume", "VolSum")
    total_volume = max(0, safe_int(total_volume_value, 0))

    open_value = safe_float(_quote_field(quote, "open", "Open"), 0.0)
    high_value = safe_float(_quote_field(quote, "high", "High"), 0.0)
    low_value = safe_float(_quote_field(quote, "low", "Low"), 0.0)
    change_value = _quote_field(quote, "price_chg", "change_price")
    reference = safe_float(
        _quote_field(quote, "reference", "previousClose"),
        _contract_reference(),
    )
    if reference <= 0 and change_value is not None:
        reference = price - safe_float(change_value, 0.0)

    code = _quote_field(quote, "code", "Code") or getattr(contract, "code", None)
    with _quote_state_lock:
        previous = last_snapshot_cache
        previous_open = safe_float(previous.get("open"), 0.0)
        previous_high = safe_float(previous.get("high"), 0.0)
        previous_low = safe_float(previous.get("low"), 0.0)
        previous_total = safe_int(previous.get("total_volume"), 0)
        previous_reference = safe_float(previous.get("reference"), 0.0)

        last_snapshot_cache.update({
            "open": open_value if open_value > 0 else (previous_open or price),
            "high": high_value if high_value > 0 else max(previous_high, price),
            "low": low_value if low_value > 0 else (min(previous_low, price) if previous_low > 0 else price),
            "close": price,
            "volume": tick_volume,
            "total_volume": total_volume if total_volume_value is not None else previous_total,
            "reference": reference if reference > 0 else previous_reference,
            "time": event_time,
            "code": str(code) if code else None,
            "source": source,
        })
        return dict(last_snapshot_cache)


def _is_expected_quote_session(now=None) -> bool:
    """Best-effort Taiwan session check used only for the health label."""
    now = now or datetime.now()
    minute = now.hour * 60 + now.minute
    weekday = now.weekday()  # Monday=0
    selected_code = str(getattr(contract, "code", ""))
    is_future = selected_code.startswith(("TXF", "MXF", "TMF"))
    if is_future:
        day_session = weekday <= 4 and 8 * 60 + 45 <= minute <= 13 * 60 + 45
        night_start = weekday <= 4 and minute >= 15 * 60
        night_end = 1 <= weekday <= 5 and minute < 5 * 60
        return day_session or night_start or night_end
    return weekday <= 4 and 9 * 60 <= minute <= 13 * 60 + 30


def _quote_health_data() -> dict:
    with _quote_state_lock:
        last_monotonic = _last_tick_monotonic
        subscription = _quote_subscription_status
        detail = _quote_event_detail
        tick_count = _quote_tick_count
        received_at = _last_tick_received_at
        exchange_time = _last_tick_exchange_time
        tick_code = _last_tick_code

    age = max(0.0, time.monotonic() - last_monotonic) if last_monotonic else None
    expected = bool(is_logged_in and contract and _is_expected_quote_session())
    if not is_logged_in:
        status = "disconnected"
    elif subscription in {"error", "disconnected"}:
        status = subscription
    elif age is not None and age <= 15:
        status = "live"
    elif expected and age is not None:
        status = "stale"
    elif expected:
        status = "waiting"
    else:
        status = "idle"
    return {
        "status": status,
        "subscription": subscription,
        "detail": detail,
        "market_expected": expected,
        "last_tick_age": round(age, 1) if age is not None else None,
        "last_tick_received_at": received_at or None,
        "last_tick_exchange_time": exchange_time or None,
        "tick_count": tick_count,
        "tick_code": tick_code,
        "selected_code": getattr(contract, "code", None) if contract else None,
        "target_code": getattr(contract, "target_code", None) if contract else None,
        "provider": "fubon",
        "kbar_source": "fubon_intraday_rest_plus_websocket_candles",
    }


def global_quote_event_callback(*args):
    global _quote_subscription_status, _quote_event_detail
    status = "connected"
    detail = "Fubon market data connected"
    with _quote_state_lock:
        _quote_subscription_status = status
        _quote_event_detail = detail
    print("[QUOTE_EVENT] Fubon WebSocket connected")
    if main_loop:
        asyncio.run_coroutine_threadsafe(
            manager.broadcast(json.dumps({"type": "quote_status", "data": _quote_health_data()})),
            main_loop,
        )


def global_quote_session_down_callback(*args):
    global _quote_subscription_status, _quote_event_detail
    with _quote_state_lock:
        _quote_subscription_status = "disconnected"
        _quote_event_detail = "Fubon quote session down"
    print("[QUOTE_EVENT] Fubon 報價連線中斷，背景重連中")
    if main_loop:
        asyncio.run_coroutine_threadsafe(
            manager.broadcast(json.dumps({"type": "quote_status", "data": _quote_health_data()})),
            main_loop,
        )


def global_fubon_candle_callback(candle):
    """Persist Fubon's authoritative server-side one-minute futures candle."""
    code = str(_quote_field(candle, "code") or "")
    event_time = _quote_timestamp(candle)
    close_price = safe_float(_quote_field(candle, "close"), 0)
    if not code.startswith(("TXF", "MXF", "TMF")) or event_time <= 0 or close_price <= 0:
        return
    bucket_ns = (event_time // 60) * 60 * 1_000_000_000
    _save_rt_bar_to_db(
        code,
        bucket_ns,
        safe_float(_quote_field(candle, "open"), close_price),
        safe_float(_quote_field(candle, "high"), close_price),
        safe_float(_quote_field(candle, "low"), close_price),
        close_price,
        max(0, safe_int(_quote_field(candle, "volume"), 0)),
    )
    if main_loop:
        asyncio.run_coroutine_threadsafe(
            manager.broadcast(json.dumps({"type": "cache_updated"})), main_loop
        )


def global_fubon_aggregate_callback(aggregate):
    """Refresh session OHLC/reference without double-counting trades."""
    if not _quote_matches_selected_contract(aggregate):
        return
    price = safe_float(
        _quote_field(aggregate, "closePrice", "lastPrice", "close"), 0
    )
    if price <= 0:
        return
    normalized = {
        "code": _quote_field(aggregate, "code", "symbol"),
        "close": price,
        "open": _quote_field(aggregate, "openPrice", "open"),
        "high": _quote_field(aggregate, "highPrice", "high"),
        "low": _quote_field(aggregate, "lowPrice", "low"),
        "reference": _quote_field(aggregate, "previousClose", "reference"),
        "total_volume": _quote_field(aggregate, "tradeVolume"),
        "ts": _quote_field(aggregate, "lastUpdated", "closeTime"),
    }
    total = _quote_field(aggregate, "total", default={}) or {}
    if isinstance(total, dict):
        normalized["total_volume"] = total.get(
            "tradeVolume", normalized["total_volume"]
        )
    reference = safe_float(normalized.get("reference"), 0)
    if reference > 0 and contract:
        contract.reference = reference
    _update_snapshot_cache(
        normalized, price, _quote_timestamp(normalized), "fubon_aggregates"
    )


def global_fubon_error_callback(error):
    global _quote_subscription_status, _quote_event_detail
    detail = str(error)[:240]
    with _quote_state_lock:
        _quote_subscription_status = "error"
        _quote_event_detail = detail
    print(f"[QUOTE_EVENT] Fubon market data error: {detail}")


def global_fubon_stock_connect_callback(*args):
    del args
    print("[WEIGHTED_STREAM] Fubon stock WebSocket connected")


def global_fubon_stock_disconnect_callback(*args):
    del args
    with _weighted_stock_state_lock:
        for state in _weighted_stock_stream_state.values():
            state.update({
                "tick_subscription": "disconnected",
                "kbar_subscription": "disconnected",
                "error": "富邦股票行情連線中斷，背景重連中",
            })
    print("[WEIGHTED_STREAM] Fubon stock WebSocket disconnected")


def global_fubon_stock_error_callback(error):
    detail = str(error)[:160]
    with _weighted_stock_state_lock:
        for state in _weighted_stock_stream_state.values():
            if state.get("tick_subscription") != "live":
                state["error"] = detail
    print(f"[WEIGHTED_STREAM] Fubon stock market data error: {detail}")


def _register_quote_callbacks(api_instance):
    """Register Fubon read-only futures and stock market-data callbacks."""
    api_instance.set_callbacks(
        tick=global_quote_callback,
        candle=global_fubon_candle_callback,
        aggregate=global_fubon_aggregate_callback,
        connect=global_quote_event_callback,
        disconnect=global_quote_session_down_callback,
        error=global_fubon_error_callback,
        stock_tick=_handle_weighted_stock_tick,
        stock_candle=weighted_stock_kbar_callback,
        stock_connect=global_fubon_stock_connect_callback,
        stock_disconnect=global_fubon_stock_disconnect_callback,
        stock_error=global_fubon_stock_error_callback,
    )


def _subscribe_contract(api_instance, contract_obj, quote_type, version=None):
    del quote_type, version
    return api_instance.subscribe_contract(contract_obj)


def _unsubscribe_contract(api_instance, contract_obj, quote_type, version=None):
    # 三種指數期貨都持續寫入本地 DB；切換畫面不解除背景行情。
    del api_instance, contract_obj, quote_type, version
    return None


def _subscribe_weighted_stock_streams(unsubscribe_first=False) -> dict:
    """Subscribe four weighted stocks to Fubon trades and 1-minute candles."""
    del unsubscribe_first
    subscribed = []
    errors = {}
    for code, name in _WEIGHTED_STOCK_DEFS:
        try:
            stock_contract = _resolve_stock_contract(api, code)
            if not stock_contract:
                raise RuntimeError("找不到富邦股票資料")
            _weighted_stock_contracts[code] = stock_contract

            snapshot = None
            try:
                snapshots = api.snapshots([stock_contract])
                snapshot = snapshots[0] if snapshots else None
            except Exception:
                snapshot = None

            reference = (
                safe_float(getattr(snapshot, "reference", 0), 0)
                if snapshot else _contract_reference(stock_contract)
            )
            open_price = safe_float(getattr(snapshot, "open", 0), 0) if snapshot else 0
            avg_price = safe_float(getattr(snapshot, "avg_price", 0), 0) if snapshot else 0
            last_price = safe_float(getattr(snapshot, "close", 0), 0) if snapshot else 0
            api.subscribe_stock(stock_contract)
            with _weighted_stock_state_lock:
                _weighted_stock_stream_state[code].update({
                    "contract": stock_contract.target_code,
                    "tick_subscription": "subscribed",
                    "kbar_subscription": "subscribed",
                    "last": last_price,
                    "avg": avg_price,
                    "open": open_price,
                    "reference": reference,
                    "error": None,
                })
            subscribed.append(code)
            print(f"[WEIGHTED_STREAM] {code} {name} Fubon trades + candles subscribed")
        except Exception as exc:
            errors[code] = type(exc).__name__
            with _weighted_stock_state_lock:
                _weighted_stock_stream_state[code].update({
                    "contract": code,
                    "tick_subscription": "error",
                    "kbar_subscription": "error",
                    "error": f"{type(exc).__name__}: {str(exc)[:120]}",
                })
            print(f"[WEIGHTED_STREAM] {code} Fubon subscription failed: {type(exc).__name__}")
    return {
        "subscribed": subscribed,
        "errors": errors,
        "source": "fubon_stock_marketdata",
    }

# 儲存活躍的 WebSocket 連線
class ConnectionManager:
    def __init__(self):
        self.active_connections: list[WebSocket] = []

    async def connect(self, websocket: WebSocket):
        await websocket.accept()
        self.active_connections.append(websocket)

    def disconnect(self, websocket: WebSocket):
        if websocket in self.active_connections:
            self.active_connections.remove(websocket)

    async def broadcast(self, message: str):
        disconnected = []
        for connection in self.active_connections:
            try:
                await connection.send_text(message)
            except:
                disconnected.append(connection)
        for connection in disconnected:
            self.disconnect(connection)

manager = ConnectionManager()

class LoginRequest(BaseModel):
    api_key: str = ""
    person_id: str = ""
    cert_path: str = ""
    cert_pass: str = ""
    save_keys: bool = True

# 報價健康監控：不輪詢 snapshots，避免把查詢型 API 當成即時源
async def quote_fallback_loop():
    print(f"[{datetime.now().strftime('%H:%M:%S')}] Quote health monitor started")
    while True:
        try:
            if is_logged_in and main_loop:
                await manager.broadcast(json.dumps({
                    "type": "quote_status",
                    "data": _quote_health_data(),
                }))
            await asyncio.sleep(5.0)
        except Exception as e:
            print(f"[QUOTE_MONITOR] 健康狀態廣播失敗: {e}")
            await asyncio.sleep(5.0)

async def _prefetch_kbars_background():
    """
    登入後背景補取歷史資料至 _HISTORY_START。
    有流量就持續補；遇流量超限立即停止；登出後自動停止。
    """
    global api, contract, is_logged_in

    now_tw = datetime.utcnow() + timedelta(hours=8)
    today = datetime(now_tw.year, now_tw.month, now_tw.day)

    # SQLite 延續既有「台灣牆鐘時間」格式；今天 00:00 前的日曆日已完整。
    cacheable_before = datetime(now_tw.year, now_tw.month, now_tw.day)

    print(f"[PREFETCH] ▶ 背景補快取啟動 {now_tw.strftime('%H:%M')} UTC+8 — 目標補至 {_HISTORY_START.strftime('%Y-%m-%d')}")

    try:
        kbars_contracts = _resolve_kbars_contracts(api, contract, _HISTORY_START, today, "PREFETCH")
    except Exception as e:
        print(f"[PREFETCH] 合約解析失敗: {e}")
        return

    if kbars_contracts:
        global _rt_contract_code, _rt_bar
        _rt_contract_code = kbars_contracts[-1].code
    total_days = (today - _HISTORY_START).days

    for kbars_contract in kbars_contracts:
        if not is_logged_in:
            break
        code = kbars_contract.code
        cached_dates = _get_cached_dates(code)

        uncached = sorted([
            d for d in (_HISTORY_START + timedelta(days=i) for i in range(total_days))
            if d.strftime('%Y-%m-%d') not in cached_dates
        ], reverse=True)  # 最新的先補，往回滾

        if not uncached:
            print(f"[PREFETCH] {code} 快取完整，略過")
            continue

        print(f"[PREFETCH] {code} 缺少 {len(uncached)} 天，從最新往前補取")

        current_end = uncached[0]   # 最新的未快取日
        overall_start = uncached[-1]  # 最舊的未快取日
        quota_exceeded = False
        while current_end >= overall_start and is_logged_in:
            batch_start = max(current_end - timedelta(days=29), overall_start)
            s_str = batch_start.strftime('%Y-%m-%d')
            e_str = current_end.strftime('%Y-%m-%d')
            try:
                loop = asyncio.get_running_loop()
                async with _kbars_lock:
                    kbars = await loop.run_in_executor(
                        None,
                        lambda c=kbars_contract, s=s_str, e=e_str: api.kbars(
                            contract=c, start=s, end=e, timeout=30000
                        )
                    )
                if kbars and kbars.ts and len(kbars.ts) > 0:
                    df_new = pd.DataFrame(dict(kbars))
                    saved = _save_to_cache(code, df_new, cacheable_before)
                    cnt = len(saved) if saved else 0
                    print(f"[PREFETCH] {code} {s_str}~{e_str} → {len(df_new)} 筆，存 {cnt} 天")
                else:
                    print(f"[PREFETCH] {code} {s_str}~{e_str} 無資料，不標記完成（可能是流量限制）")
            except Exception as e:
                err_msg = str(e)
                is_quota = any(k in err_msg.lower() for k in ("quota", "limit", "usage", "exceed", "流量", "請求次數"))
                if is_quota:
                    print(f"[PREFETCH] ⚠ 流量超限，停止補取，下次登入繼續")
                    quota_exceeded = True
                    break
                print(f"[PREFETCH] {code} {s_str}~{e_str} 失敗: {e}")
                await asyncio.sleep(5)
            current_end = batch_start - timedelta(days=1)
            await asyncio.sleep(0.5)

        if quota_exceeded:
            break

    # ── Phase 2: TXFR1 直查補取（只看 TXFR1 自身快取，不受月份合約空白標記影響）──
    if is_logged_in and contract and (contract.code.endswith('R1') or contract.code.endswith('R2')):
        txfr1_cached_dates = _get_cached_dates("TXFR1")
        uncached_r1 = sorted([
            d for d in (_HISTORY_START + timedelta(days=i) for i in range(total_days))
            if d.strftime('%Y-%m-%d') not in txfr1_cached_dates
        ], reverse=True)

        if not uncached_r1:
            print("[PREFETCH] Phase 2: TXFR1 無需補取（月份合約已全覆蓋）")
        else:
            print(f"[PREFETCH] Phase 2: TXFR1 直查 {len(uncached_r1)} 天（月份合約未涵蓋）")
            base_contract = contract
            p2_end = uncached_r1[0]
            p2_start = uncached_r1[-1]
            while p2_end >= p2_start and is_logged_in:
                batch_start = max(p2_end - timedelta(days=29), p2_start)
                s_str = batch_start.strftime('%Y-%m-%d')
                e_str = p2_end.strftime('%Y-%m-%d')
                try:
                    loop = asyncio.get_running_loop()
                    async with _kbars_lock:
                        kbars = await loop.run_in_executor(
                            None,
                            lambda c=base_contract, s=s_str, e=e_str: api.kbars(
                                contract=c, start=s, end=e, timeout=30000
                            )
                        )
                    if kbars and kbars.ts and len(kbars.ts) > 0:
                        df_new = pd.DataFrame(dict(kbars))
                        saved = _save_to_cache("TXFR1", df_new, cacheable_before)
                        cnt = len(saved) if saved else 0
                        print(f"[PREFETCH] TXFR1 {s_str}~{e_str} → {len(df_new)} 筆，存 {cnt} 天")
                    else:
                        print(f"[PREFETCH] TXFR1 {s_str}~{e_str} 無資料，不標記完成（可能是流量限制）")
                except Exception as ep2:
                    err_msg = str(ep2)
                    if any(k in err_msg.lower() for k in ("quota", "limit", "usage", "exceed", "流量", "請求次數")):
                        print("[PREFETCH] Phase 2 ⚠ 流量超限，停止補取，下次登入繼續")
                        break
                    print(f"[PREFETCH] TXFR1 {s_str}~{e_str} 失敗: {ep2}")
                    await asyncio.sleep(5)
                p2_end = batch_start - timedelta(days=1)
                await asyncio.sleep(0.5)

    print("[PREFETCH] ◀ 背景補快取完成")


@app.on_event("startup")
async def startup_event():
    global main_loop
    main_loop = asyncio.get_running_loop()
    _init_kbars_cache()
    # 啟動報價守護進程
    asyncio.create_task(quote_fallback_loop())

# 全域報價回呼函式
def global_quote_callback(*args):
    global _last_tick_monotonic, _last_tick_received_at, _last_tick_exchange_time
    global _last_tick_code, _quote_tick_count, _quote_tick_count_since_log
    global _quote_log_monotonic, _quote_subscription_status
    try:
        if not args:
            return
        # 自動判斷參數格式 (相容舊版 topic/quote 與新版 exchange/data)
        quote = args[1] if len(args) > 1 else args[0]

        simtrade = _quote_field(quote, "simtrade", default=False)
        if simtrade is True or str(simtrade).lower() in {"1", "true"}:
            return

        tick_code = str(_quote_field(quote, "code", "Code") or "")
        if tick_code in _WEIGHTED_STOCK_CODES:
            _handle_weighted_stock_tick(quote)
            # 四大權值股的額外訂閱不可污染目前選取的期貨報價。
            if not _quote_matches_selected_contract(quote):
                return
        elif not _quote_matches_selected_contract(quote):
            return

        price = _quote_field(quote, "close", "Close", "price", "Price")
        p_val = safe_float(price, 0.0)
        if p_val <= 0:
            return

        event_time = _quote_timestamp(quote)
        vol_tick = max(0, safe_int(_quote_field(quote, "volume", "Volume"), 0))
        tick_code = tick_code or getattr(contract, "code", None)
        monotonic_now = time.monotonic()

        with _quote_state_lock:
            _last_tick_monotonic = monotonic_now
            _last_tick_received_at = int(time.time())
            _last_tick_exchange_time = event_time
            _last_tick_code = str(tick_code) if tick_code else None
            _quote_tick_count += 1
            _quote_tick_count_since_log += 1
            _quote_subscription_status = "subscribed"

        snapshot_payload = _update_snapshot_cache(quote, p_val, event_time, "stream")

        # ── 即時 1-min bar 累積 ──────────────────────────────────────────
        bucket_ns = (event_time // 60) * 60 * 1_000_000_000
        with _rt_bar_lock:
            rt_code = _rt_contract_code
            if rt_code:
                if _rt_bar.get('bucket_ns') != bucket_ns:
                    prev = dict(_rt_bar)
                    _rt_bar.clear()
                    _rt_bar.update({'bucket_ns': bucket_ns, 'code': rt_code,
                                    'o': p_val, 'h': p_val, 'l': p_val, 'c': p_val, 'vol': vol_tick})
                    if prev.get('bucket_ns') and prev.get('code') == rt_code:
                        _save_rt_bar_to_db(rt_code, prev['bucket_ns'],
                                           prev['o'], prev['h'], prev['l'], prev['c'], prev['vol'])
                        bar_t = datetime.fromtimestamp(prev['bucket_ns'] / 1e9).strftime('%H:%M')
                        print(f"[RT_CACHE] 存入 {rt_code} {bar_t}")
                        if main_loop:
                            asyncio.run_coroutine_threadsafe(
                                manager.broadcast(json.dumps({"type": "cache_updated"})), main_loop
                            )
                else:
                    _rt_bar['h'] = max(_rt_bar.get('h', p_val), p_val)
                    _rt_bar['l'] = min(_rt_bar.get('l', p_val), p_val)
                    _rt_bar['c'] = p_val
                    _rt_bar['vol'] = _rt_bar.get('vol', 0) + vol_tick
        # ─────────────────────────────────────────────────────────────────

        if main_loop:
            tick_payload = {
                **snapshot_payload,
                "price": p_val,
                "time": event_time,
                "tick_volume": vol_tick,
                "ticks": 1,
            }
            asyncio.run_coroutine_threadsafe(
                manager.broadcast(json.dumps({"type": "tick", "data": tick_payload})),
                main_loop,
            )

        # 高頻 Tick 不逐筆 print，避免 stdout I/O 阻塞行情 callback。
        if monotonic_now - _quote_log_monotonic >= 5:
            with _quote_state_lock:
                ticks_in_window = _quote_tick_count_since_log
                _quote_tick_count_since_log = 0
                _quote_log_monotonic = monotonic_now
            print(
                f"[QUOTE] live code={tick_code} price={p_val:g} "
                f"volume={vol_tick} ticks/5s={ticks_in_window}"
            )
    except Exception as e:
        print(f"!!! 報價處理異常: {e}")

@app.post("/api/resubscribe")
async def resubscribe():
    global api, contract, is_logged_in, _quote_subscription_status, _quote_event_detail
    print(f"[{datetime.now().strftime('%H:%M:%S')}] 收到手動重新訂閱請求...")
    try:
        if not is_logged_in or api is None:
            return {"status": "error", "message": "伺服器未登入或已斷線，請重新啟動連線"}
        if not contract:
            return {"status": "error", "message": "合約資訊遺失，請嘗試重新登入"}

        loop = asyncio.get_running_loop()
        with _quote_state_lock:
            _quote_subscription_status = "subscribing"
            _quote_event_detail = "manual resubscribe"
        await loop.run_in_executor(None, api.resubscribe_all)
        weighted_result = _subscribe_weighted_stock_streams(unsubscribe_first=True)

        print(f"[{datetime.now().strftime('%H:%M:%S')}] [OK] 已強行重新訂閱 {contract.code}")
        return {
            "status": "success",
            "quote": _quote_health_data(),
            "weighted_stocks": weighted_result,
        }
    except Exception as e:
        print(f"!!! 重新訂閱失敗: {e}")
        return {"status": "error", "message": f"API 異常: {str(e)}"}

@app.on_event("shutdown")
async def shutdown_event():
    global api
    if api:
        try:
            api.logout()
            print("Successfully logged out from Fubon market data.")
        except:
            pass

@app.post("/api/login")
async def login(req: LoginRequest):
    global api, is_logged_in, contract, main_loop
    global _quote_subscription_status, _quote_event_detail
    global _rt_contract_code, _rt_bar
    now_str = datetime.now().strftime('%Y-%m-%d %H:%M:%S')
    personal_id = req.person_id.strip() or os.getenv("FUBON_PERSON_ID", "").strip()
    api_key = req.api_key.strip() or os.getenv("FUBON_API_KEY", "").strip()
    cert_path = req.cert_path.strip() or os.getenv("FUBON_CERT_PATH", "").strip()
    cert_pass = req.cert_pass or os.getenv("FUBON_CERT_PASS", "")
    print(f"\n[{now_str}] [SECURE] 收到富邦唯讀期貨行情登入請求")
    print(f"[{now_str}] [DIR] 登入模式: {'API Key + 憑證' if cert_path else 'API Key DMA'}")
    try:
        main_loop = asyncio.get_running_loop()
        is_logged_in = False

        # 釋放舊連線
        if api:
            try:
                print(f"[{now_str}] [RESET] 正在登出舊有富邦行情工作階段...")
                api.logout()
            except: pass

        api = FubonMarketDataClient()
        _register_quote_callbacks(api)
        with _contract_info_lock:
            _contract_info_cache.clear()

        loop = asyncio.get_running_loop()
        print(f"[{now_str}] [WAIT] 正在登入富邦新一代 API 行情服務...")
        await loop.run_in_executor(
            None,
            lambda: api.login(
                personal_id=personal_id,
                api_key=api_key,
                cert_path=cert_path,
                cert_pass=cert_pass or None,
            ),
        )
        print(f"[{now_str}] [SUCCESS] 富邦 API Key 登入成功，Normal Mode 已建立！")

        contract = await loop.run_in_executor(
            None, lambda: _resolve_market_contract(api, "TXFR1")
        )
        if not contract:
            raise RuntimeError("富邦契約清單無法解析 TXFR1 近月合約")
        _reset_quote_state(contract)
        with _rt_bar_lock:
            _rt_bar.clear()
        _rt_contract_code = contract.code
        print(
            f"[{now_str}] [CONTRACT] 預設商品: {contract.code} → {contract.target_code} "
            f"(平盤參考價: {_contract_reference(contract) or '未知'})"
        )

        # 三種商品的日／夜盤 trades、aggregates、candles 全部持續訂閱並存 DB。
        futures_subscribed = []
        futures_errors = {}
        for alias in ("TXFR1", "MXFR1", "TMFR1"):
            try:
                item = contract if alias == "TXFR1" else await loop.run_in_executor(
                    None, lambda a=alias: _resolve_market_contract(api, a)
                )
                if not item:
                    raise RuntimeError("找不到近月契約")
                await loop.run_in_executor(None, lambda c=item: api.subscribe_contract(c))
                futures_subscribed.append({"code": alias, "target_code": item.target_code})
            except Exception as exc:
                futures_errors[alias] = type(exc).__name__
                print(f"[{now_str}] [WS] {alias} 訂閱失敗: {type(exc).__name__}")

        is_logged_in = True
        _reset_weighted_stock_stream_state()
        weighted_result = _subscribe_weighted_stock_streams()
        print(f"[{now_str}] [WS] 富邦期貨訂閱完成: {futures_subscribed}")
        print(
            f"[{now_str}] [WS] 富邦四大權值股訂閱完成: "
            f"{weighted_result.get('subscribed', [])}"
        )
        if futures_errors:
            print(f"[{now_str}] [WS] 部分期貨商品待重試: {sorted(futures_errors)}")
        if weighted_result.get("errors"):
            print(
                f"[{now_str}] [WS] 部分權值股待重試: "
                f"{sorted(weighted_result['errors'])}"
            )

        # 儲存登入憑證至 .env
        if req.save_keys:
            env_path = os.path.join(os.path.dirname(os.path.abspath(__file__)), ".env")
            set_key(env_path, "FUBON_API_KEY", api_key)
            set_key(env_path, "FUBON_PERSON_ID", personal_id)
            set_key(env_path, "FUBON_CERT_PATH", cert_path)
            set_key(env_path, "FUBON_CERT_PASS", cert_pass)
            os.environ.update({
                "FUBON_API_KEY": api_key,
                "FUBON_PERSON_ID": personal_id,
                "FUBON_CERT_PATH": cert_path,
                "FUBON_CERT_PASS": cert_pass,
            })
            print(f"[{now_str}] [SAVE] 富邦登入資訊已儲存至本機 .env")

        print(
            f"[{now_str}] [HISTORY] 期貨由富邦日內 REST 補當日日／夜盤；"
            "四大權值股由富邦股票 REST 補當日分時。"
        )
        print(f"[{now_str}] [READY] 系統完全就緒，連線就緒開始看盤！\n")
        return {
            "status": "success",
            "contract": contract.code,
            "target_contract": contract.target_code,
            "futures": futures_subscribed,
            "futures_errors": futures_errors,
            "weighted_stocks": weighted_result,
        }
    except Exception as e:
        is_logged_in = False
        try:
            api.logout()
        except Exception:
            pass
        print(f"[{now_str}] [ERROR] 登入失敗！異常訊息: {e}\n")
        raise HTTPException(status_code=400, detail=str(e))

@app.post("/api/select_contract")
async def select_contract(req: dict):
    global api, is_logged_in, contract, _rt_contract_code, _rt_bar
    now_str = datetime.now().strftime('%H:%M:%S')
    if not is_logged_in or not api:
        print(f"[{now_str}] [WARN] 收到切換合約請求，但目前為「未登入」狀態！")
        return {"status": "error", "message": "請先登入連線"}
    
    code = str(req.get("code", "TXFR1")).strip().upper()
    old_contract = contract
    print(f"\n[{now_str}] [CHANGE] 收到切換合約請求: {old_contract.code if old_contract else 'None'} -> {code}")

    try:
        if not code.startswith(("TXF", "MXF", "TMF")):
            print(f"[{now_str}] [ERROR] 不支援的合約代碼: {code}")
            return {"status": "error", "message": "不支援的合約代碼"}
        new_contract = _resolve_market_contract(api, code)

        if not new_contract:
            return {"status": "error", "message": f"找不到合約: {code}"}

        contract = new_contract
        _reset_quote_state(contract)
        api.subscribe_contract(contract)
        print(
            f"[{now_str}] [WS] 畫面切換為 {contract.code} → "
            f"{contract.target_code}；其他商品繼續背景存檔。"
        )

        # 切換合約時重置即時快取狀態
        with _rt_bar_lock:
            _rt_bar.clear()
        _rt_contract_code = contract.code

        print(f"[{now_str}] [OK] 合約順利切換完成。\n")
        return {"status": "success", "contract": contract.code, "quote": _quote_health_data()}
    except Exception as e:
        # 新合約訂閱失敗時，盡力恢復舊合約，避免畫面永久斷流。
        if old_contract and contract is not old_contract:
            try:
                contract = old_contract
                _reset_quote_state(contract)
                api.subscribe_contract(contract)
            except Exception as restore_error:
                print(f"[{now_str}] [ERROR] 舊合約恢復失敗: {restore_error}")
        print(f"[{now_str}] [ERROR] 合約切換失敗: {e}\n")
        return {"status": "error", "message": str(e)}

@app.get("/api/status")
async def get_status():
    global is_logged_in, contract
    return {
        "logged_in": is_logged_in,
        "contract": contract.code if contract else None,
        "target_contract": contract.target_code if contract else None,
        "provider": "fubon",
        "env": {
            "has_api_key": bool(os.getenv("FUBON_API_KEY", "").strip()),
            "has_person_id": bool(os.getenv("FUBON_PERSON_ID", "").strip()),
            "has_cert_path": bool(os.getenv("FUBON_CERT_PATH", "").strip()),
            "has_cert_pass": bool(os.getenv("FUBON_CERT_PASS", "")),
        }
    }

# ── K 線本地快取（SQLite）────────────────────────────────────────
_KBARS_CACHE_DB = os.path.join(os.path.dirname(os.path.abspath(__file__)), 'kbars_cache.db')

def _init_kbars_cache():
    with sqlite3.connect(_KBARS_CACHE_DB) as conn:
        conn.execute("""
            CREATE TABLE IF NOT EXISTS kbars1m (
                contract_code TEXT,
                ts            INTEGER,
                Open  REAL, High REAL, Low REAL, Close REAL, Volume INTEGER,
                PRIMARY KEY (contract_code, ts)
            )
        """)
        conn.execute("""
            CREATE TABLE IF NOT EXISTS cached_dates (
                contract_code TEXT,
                date          TEXT,
                PRIMARY KEY (contract_code, date)
            )
        """)
        conn.execute("CREATE INDEX IF NOT EXISTS idx_kbars1m ON kbars1m(contract_code, ts)")
    print("[CACHE] K 線快取資料庫已初始化")

def _get_cached_dates(contract_code: str) -> set:
    with sqlite3.connect(_KBARS_CACHE_DB) as conn:
        rows = conn.execute(
            "SELECT date FROM cached_dates WHERE contract_code=?", (contract_code,)
        ).fetchall()
    return {r[0] for r in rows}

def _taipei_wallclock_ns(value: datetime) -> int:
    """Encode a naive Taiwan wall-clock datetime for the legacy cache format."""
    return int(calendar.timegm(value.timetuple()) * 1e9 + value.microsecond * 1000)

def _get_cache_bar_counts(contract_code: str, start: datetime, end: datetime) -> dict[str, int]:
    """Count cached rows by Taiwan wall-clock calendar date."""
    start_ns = _taipei_wallclock_ns(start)
    end_ns = _taipei_wallclock_ns(end + timedelta(days=1))
    with sqlite3.connect(_KBARS_CACHE_DB) as conn:
        rows = conn.execute(
            "SELECT date(ts / 1000000000, 'unixepoch') AS d, COUNT(*) "
            "FROM kbars1m WHERE contract_code=? AND ts>=? AND ts<? GROUP BY d",
            (contract_code, start_ns, end_ns),
        ).fetchall()
    return {str(d): int(count) for d, count in rows}

def _recent_cache_date_is_incomplete(day: datetime, count: int, today) -> bool:
    """Detect obvious recent futures gaps even if legacy metadata says cached."""
    calendar_day = day.date()
    if calendar_day >= today:
        return True
    if calendar_day < today - timedelta(days=10):
        return False
    weekday = calendar_day.weekday()
    if weekday == 6:  # Sunday has no TXF session.
        return False
    minimum = 650 if weekday == 0 else (180 if weekday == 5 else 850)
    return count < minimum

def _kbars_date_in_backoff(contract_code: str, day: datetime) -> bool:
    key = (contract_code, day.strftime("%Y-%m-%d"))
    with _kbars_retry_lock:
        retry_at = _kbars_retry_after.get(key, 0.0)
        if retry_at <= time.monotonic():
            _kbars_retry_after.pop(key, None)
            return False
        return True

def _kbars_range_in_backoff(contract_code: str, start: datetime, end: datetime) -> bool:
    day = start
    while day <= end:
        if not _kbars_date_in_backoff(contract_code, day):
            return False
        day += timedelta(days=1)
    return True

def _set_kbars_backoff(contract_code: str, start: datetime, end: datetime, seconds: int):
    retry_at = time.monotonic() + max(0, seconds)
    with _kbars_retry_lock:
        day = start
        while day <= end:
            _kbars_retry_after[(contract_code, day.strftime("%Y-%m-%d"))] = retry_at
            day += timedelta(days=1)


def _claim_forced_kbars_refresh(contract_code: str) -> bool:
    """Throttle recent-session repair across all open browser tabs."""
    now = time.monotonic()
    with _kbars_retry_lock:
        allowed_at = _kbars_forced_refresh_after.get(contract_code, 0.0)
        if allowed_at > now:
            return False
        _kbars_forced_refresh_after[contract_code] = (
            now + _KBARS_FORCED_REFRESH_COOLDOWN_SECONDS
        )
        return True

def _load_from_cache(contract_code: str, start: datetime, end: datetime) -> pd.DataFrame:
    start_ns = _taipei_wallclock_ns(start)
    end_ns = _taipei_wallclock_ns(end + timedelta(days=1))
    with sqlite3.connect(_KBARS_CACHE_DB) as conn:
        rows = conn.execute(
            "SELECT ts, Open, High, Low, Close, Volume FROM kbars1m "
            "WHERE contract_code=? AND ts >= ? AND ts < ? ORDER BY ts",
            (contract_code, start_ns, end_ns)
        ).fetchall()
    if not rows:
        return pd.DataFrame()
    return pd.DataFrame(rows, columns=['ts', 'Open', 'High', 'Low', 'Close', 'Volume'])

def _save_to_cache(contract_code: str, df: pd.DataFrame, cacheable_before: datetime):
    """Store every returned bar; only mark completed raw calendar dates as final."""
    df_ts = pd.to_datetime(df['ts'], unit='ns', utc=True)
    df = df.copy()
    df['_date'] = df_ts.dt.date
    mask = df_ts < pd.Timestamp(cacheable_before, tz='UTC')
    completed_dates = [str(d) for d in df.loc[mask, '_date'].unique()]
    if df.empty:
        return
    with sqlite3.connect(_KBARS_CACHE_DB) as conn:
        conn.executemany(
            "INSERT OR REPLACE INTO kbars1m VALUES (?,?,?,?,?,?,?)",
            [(contract_code, int(r.ts), r.Open, r.High, r.Low, r.Close, int(r.Volume))
             for r in df.itertuples()]
        )
        if completed_dates:
            conn.executemany(
                "INSERT OR REPLACE INTO cached_dates VALUES (?,?)",
                [(contract_code, d) for d in completed_dates]
            )
    return completed_dates

def _save_rt_bar_to_db(code: str, ts_ns: int, o: float, h: float, l: float, c: float, vol: int):
    """即時 1-min bar 寫入 kbars1m，不更新 cached_dates（今日仍視為未完整快取）。
    ts_ns 是該分鐘的起點；寫入時轉為既有快取的「分鐘收盤時間」
    並補 +8h wall-clock 偏移，讓 API 歷史棒與即時暫存棒共用一套分桶規則。
    """
    close_ts_ns = ts_ns + 60 * 1_000_000_000
    biased_ts_ns = close_ts_ns + 28800 * 1_000_000_000
    try:
        with sqlite3.connect(_KBARS_CACHE_DB, timeout=5) as conn:
            # 同時清除舊的未偏移版本（升級前寫入的錯誤資料）
            conn.execute("DELETE FROM kbars1m WHERE contract_code=? AND ts=?", (code, ts_ns))
            conn.execute(
                "INSERT OR REPLACE INTO kbars1m VALUES (?,?,?,?,?,?,?)",
                (code, biased_ts_ns, o, h, l, c, vol)
            )
    except Exception as e:
        print(f"[RT_CACHE] DB 寫入失敗: {e}")

def _resolve_stock_contract(market_api, code: str):
    """Resolve a Taiwan stock through Fubon's official intraday ticker API."""
    if not market_api or not code:
        return None
    return market_api.resolve_stock_contract(str(code).strip())

def _fetch_twse_stock_snapshots(stock_defs: list[tuple[str, str]]) -> dict:
    """Fetch TWSE MIS snapshots for the four weighted-stock mini charts."""
    ex_ch = "|".join(f"tse_{code}.tw" for code, _ in stock_defs)
    url = f"https://mis.twse.com.tw/stock/api/getStockInfo.jsp?ex_ch={ex_ch}&json=1&delay=0"
    req = urllib.request.Request(
        url,
        headers={
            "User-Agent": "Mozilla/5.0",
            "Referer": "https://mis.twse.com.tw/stock/index.jsp",
        },
    )
    try:
        with urllib.request.urlopen(req, timeout=8) as resp:
            raw = resp.read().decode("utf-8", errors="ignore")
        payload = json.loads(raw)
    except Exception as e:
        print(f"[TWSE] 四大權值股快照 fallback 失敗: {e}")
        return {}

    rows = payload.get("msgArray") or []
    return {str(row.get("c") or "").strip(): row for row in rows if row.get("c")}

def _twse_snapshot_to_intraday_payload(code: str, name: str, row: dict) -> dict:
    """Convert a TWSE MIS quote snapshot into a small chartable intraday path."""
    date_raw = str(row.get("d") or row.get("^") or "").strip()
    time_raw = str(row.get("t") or row.get("%") or "13:30:00").strip()
    if len(date_raw) == 8:
        date_str = f"{date_raw[:4]}-{date_raw[4:6]}-{date_raw[6:8]}"
    else:
        date_str = (datetime.utcnow() + timedelta(hours=8)).strftime("%Y-%m-%d")

    open_p = safe_float(row.get("o"), 0)
    high_p = safe_float(row.get("h"), 0)
    low_p = safe_float(row.get("l"), 0)
    last_p = safe_float(row.get("z"), 0) or safe_float(row.get("pz"), 0)
    prev_p = safe_float(row.get("y"), 0)
    total_vol = safe_int(row.get("v"), 0)

    if not last_p:
        return {"code": code, "name": name, "date": date_str, "source": "twse_snapshot", "bars": []}

    if not open_p:
        open_p = last_p
    if not high_p:
        high_p = max(open_p, last_p)
    if not low_p:
        low_p = min(open_p, last_p)

    def _epoch_at(hhmmss: str) -> int:
        try:
            dt = datetime.strptime(f"{date_str} {hhmmss}", "%Y-%m-%d %H:%M:%S")
        except ValueError:
            dt = datetime.strptime(f"{date_str} 13:30:00", "%Y-%m-%d %H:%M:%S")
        return int(dt.timestamp())

    if last_p >= open_p:
        path = [("09:00:00", open_p), ("10:30:00", low_p), ("12:00:00", high_p), (time_raw, last_p)]
    else:
        path = [("09:00:00", open_p), ("10:30:00", high_p), ("12:00:00", low_p), (time_raw, last_p)]

    bars = []
    seen_times = set()
    running_amount = 0.0
    running_volume = 0
    chunk_vol = max(total_vol // max(len(path), 1), 1)
    for idx, (hhmmss, price) in enumerate(path):
        t = _epoch_at(hhmmss)
        if t in seen_times:
            t += idx * 60
        seen_times.add(t)
        running_volume += chunk_vol
        running_amount += price * chunk_vol
        bars.append({
            "time": t,
            "price": price,
            "avg": running_amount / running_volume if running_volume else price,
            "volume": chunk_vol,
        })

    return {
        "code": code,
        "name": name,
        "date": date_str,
        "is_today": date_str == (datetime.utcnow() + timedelta(hours=8)).strftime("%Y-%m-%d"),
        "source": "twse_snapshot",
        "open": open_p,
        "last": last_p,
        "change": last_p - prev_p if prev_p else last_p - open_p,
        "change_pct": ((last_p - prev_p) / prev_p * 100) if prev_p else ((last_p - open_p) / open_p * 100 if open_p else 0),
        "bars": bars,
    }

def _resolve_kbars_contracts(api_instance, base_contract, start_date, end_date, now_str: str) -> list:
    """
    富邦用即時契約清單把 R1/R2 alias 解析為目前月份；快取仍以 alias
    保存，換月後可延續成同一條本地近月序列。
    """
    del api_instance, start_date, end_date
    code = base_contract.code
    if code.endswith(("R1", "R2")):
        print(
            f"[{now_str}] [CHART] 富邦近月 {code} → "
            f"{base_contract.target_code}；日內 REST＋本地歷史"
        )
    return [base_contract]

def _aggregate_kbars_dataframe(df: pd.DataFrame, period: str) -> pd.DataFrame:
    """Aggregate close-stamped 1-minute bars on TXF session boundaries."""
    aggregations = {
        'Open': 'first',
        'High': 'max',
        'Low': 'min',
        'Close': 'last',
        'Volume': 'sum',
    }
    if period == "D":
        trading_day = pd.Series(df.index.normalize(), index=df.index)
        after_hours = df.index.hour >= 15
        trading_day.loc[after_hours] = (
            trading_day.loc[after_hours] + pd.Timedelta(days=1)
        )
        weekday = trading_day.dt.weekday
        trading_day = trading_day + pd.to_timedelta(
            weekday.map({5: 2, 6: 1}).fillna(0), unit="D"
        )
        grouped = df.groupby(trading_day, sort=True).agg(aggregations).dropna()
        grouped.index.name = "ts"
        return grouped

    minutes = {"5min": 5, "15min": 15, "30min": 30, "60min": 60}.get(period)
    if not minutes:
        raise ValueError(f"不支援的 K 棒週期: {period}")

    effective = pd.Series(df.index - pd.Timedelta(seconds=1), index=df.index)
    calendar_day = effective.dt.normalize()
    minute_of_day = effective.dt.hour * 60 + effective.dt.minute
    anchor = calendar_day.copy()

    early_night = minute_of_day < 5 * 60
    late_night = minute_of_day >= 15 * 60
    day_session = (
        (minute_of_day >= 8 * 60 + 45)
        & (minute_of_day <= 13 * 60 + 45)
    )
    anchor.loc[early_night] = (
        calendar_day.loc[early_night]
        - pd.Timedelta(days=1)
        + pd.Timedelta(hours=15)
    )
    anchor.loc[late_night] = (
        calendar_day.loc[late_night] + pd.Timedelta(hours=15)
    )
    anchor.loc[day_session] = (
        calendar_day.loc[day_session]
        + pd.Timedelta(hours=8, minutes=45)
    )

    period_delta = pd.Timedelta(minutes=minutes)
    bucket = anchor + ((effective - anchor) // period_delta) * period_delta
    grouped = df.groupby(bucket, sort=True).agg(aggregations).dropna()
    grouped.index.name = "ts"
    return grouped


def _drop_incomplete_recent_futures_dates(
    df: pd.DataFrame, today
) -> tuple[pd.DataFrame, list[str], int]:
    """Remove obviously fragmented completed raw dates before charting."""
    raw_dates = pd.Series(df.index.strftime("%Y-%m-%d"), index=df.index)
    counts = raw_dates.value_counts()
    incomplete_dates = []
    for raw_date, count in counts.items():
        raw_day = datetime.strptime(str(raw_date), "%Y-%m-%d")
        # 凌晨／日盤重新開啟時，當前交易日的夜盤起點位於前一個日曆日
        # 15:00。富邦日內 candles 可能只回傳這段夜盤，而沒有同日早上的
        # 日盤；只要已有足夠連續分鐘，就必須保留來填滿初始畫面。
        is_current_session_lead = (
            raw_day.date() == today - timedelta(days=1)
            and int(count) >= 120
        )
        if (
            raw_day.date() < today
            and not is_current_session_lead
            and _recent_cache_date_is_incomplete(raw_day, int(count), today)
        ):
            incomplete_dates.append(str(raw_date))
    if not incomplete_dates:
        return df, [], 0
    filtered = df.loc[~raw_dates.isin(incomplete_dates)]
    return filtered, sorted(incomplete_dates), len(df) - len(filtered)


@app.get("/api/kbars")
async def get_kbars(
    start: str,
    end: str,
    period: str = "1min",
    refresh_recent: bool = False,
):
    global api, is_logged_in, contract
    now_str = datetime.now().strftime('%H:%M:%S')

    if not is_logged_in or not contract:
        print(f"[{now_str}] [WARN] 收到 K 線歷史數據請求，但目前為「未登入」狀態！")
        raise HTTPException(status_code=401, detail="Not logged in")

    try:
        start_date = datetime.strptime(start, '%Y-%m-%d')
        end_date = datetime.strptime(end, '%Y-%m-%d')
    except ValueError as e:
        raise HTTPException(status_code=400, detail=f"日期格式錯誤: {e}")

    now_tw = datetime.utcnow() + timedelta(hours=8)  # 轉台灣時間 UTC+8
    today_tw = now_tw.date()
    # DB 延續台灣牆鐘日期；今天 00:00 前的原始日曆日已完整。
    safe_end = datetime(today_tw.year, today_tw.month, today_tw.day)

    try:
        kbars_contracts = _resolve_kbars_contracts(api, contract, start_date, end_date, now_str)
        print(f"\n[{now_str}] [CHART] 歷史 K 線索取請求 -> 合約: {[c.code for c in kbars_contracts]} | 區間: {start} 至 {end} | 週期: {period}")

        max_days = 365 if period == "D" else 60
        if (end_date - start_date).days > max_days:
            adjusted_start = end_date - timedelta(days=max_days)
            print(f"[{now_str}] [WARN] 請求天數大於單次最大限制 ({max_days} 天)，自動縮減起點為 {adjusted_start.strftime('%Y-%m-%d')}")
            start_date = adjusted_start

        output_start_date = start_date
        output_end_date = end_date
        # 一個期貨交易日包含前一日 15:00 起的夜盤；聚合日 K 時要多載一天，
        # 最後再裁回使用者要求的交易日區間。
        source_start_date = (
            start_date - timedelta(days=1) if period == "D" else start_date
        )
        source_end_date = end_date

        all_df = []
        # 今日資料也寫入 SQLite 當暫存，但只有 ts < safe_end 的日曆日標記為完整。
        cacheable_before = safe_end

        # 更新即時快取目標合約（取最新月份）
        if kbars_contracts:
            global _rt_contract_code, _rt_bar
            new_rt_code = kbars_contracts[-1].code
            if _rt_contract_code != new_rt_code:
                with _rt_bar_lock:
                    _rt_bar.clear()
                _rt_contract_code = new_rt_code

        for kbars_contract in kbars_contracts:
            code = kbars_contract.code
            cached_dates = _get_cached_dates(code)
            cache_counts = _get_cache_bar_counts(
                code, source_start_date, source_end_date
            )
            is_future = code.startswith(("TXF", "MXF", "TMF"))

            # 舊版可能把 API 空回傳誤標為完成；最近日期再用筆數檢查明顯缺口。
            uncached = []
            d = source_start_date
            while d <= source_end_date:
                date_str = d.strftime('%Y-%m-%d')
                closed_weekend = d.weekday() == 6 if is_future else d.weekday() >= 5
                forced_recent_day = (
                    refresh_recent
                    and is_future
                    and today_tw - timedelta(days=1) <= d.date() <= today_tw
                )
                incomplete_recent = (
                    is_future
                    and _recent_cache_date_is_incomplete(
                        d, cache_counts.get(date_str, 0), today_tw
                    )
                )
                needs_fetch = (
                    date_str not in cached_dates
                    or incomplete_recent
                    or forced_recent_day
                )
                retry_ready = (
                    forced_recent_day
                    or not _kbars_date_in_backoff(code, d)
                )
                if needs_fetch and not closed_weekend and retry_ready:
                    uncached.append(d)
                d += timedelta(days=1)

            # 從快取載入已有的資料
            cached_df = _load_from_cache(
                code, source_start_date, source_end_date
            )
            if not cached_df.empty:
                print(f"[{now_str}] [CACHE] {code} 快取命中 {len(cached_df)} 筆")
                all_df.append(cached_df)

            if not uncached:
                print(
                    f"[{now_str}] [CACHE] {code} 全區間已快取，"
                    "或缺口暫在 API 冷卻中；略過 API"
                )
                continue

            print(f"[{now_str}] [CACHE] {code} 未快取日期 ({len(uncached)} 天): {[d.strftime('%m/%d') for d in uncached]}")

            # 以 30 天為上限分批打 API（取 uncached 的整個 span）
            api_start = uncached[0]
            api_end   = uncached[-1]
            batch_num = 0
            current_start = api_start
            while current_start <= api_end:
                current_end = min(current_start + timedelta(days=29), api_end)
                s_str = current_start.strftime('%Y-%m-%d')
                e_str = current_end.strftime('%Y-%m-%d')
                batch_num += 1

                print(f"[{now_str}] [API] [批次 #{batch_num}] {code} | {s_str} 至 {e_str}")
                loop = asyncio.get_running_loop()
                try:
                    async with _kbars_lock:
                        force_recent_batch = (
                            refresh_recent
                            and is_future
                            and current_end.date() >= today_tw - timedelta(days=1)
                            and current_start.date() <= today_tw
                            and _claim_forced_kbars_refresh(code)
                        )
                        # 另一個圖表可能剛查過相同空區間；進鎖後再次檢查冷卻。
                        if (
                            not force_recent_batch
                            and _kbars_range_in_backoff(code, current_start, current_end)
                        ):
                            print(f"[{now_str}]  ↳ [BACKOFF] 相同區間剛查過，略過重複 API")
                            current_start = current_end + timedelta(days=1)
                            continue
                        if force_recent_batch:
                            print(f"[{now_str}]  ↳ [REPAIR] 強制校準最近交易時段")
                        kbars = await loop.run_in_executor(
                            None,
                            lambda c=kbars_contract, s=s_str, e=e_str: api.kbars(
                                contract=c, start=s, end=e, timeout=30000
                            )
                        )
                except Exception as api_err:
                    err_type = type(api_err).__name__
                    err_msg  = str(api_err)
                    error_lower = err_msg.lower()
                    is_quota = any(
                        k in error_lower
                        for k in (
                            "quota",
                            "usage limit",
                            "rate limit",
                            "exceed",
                            "流量",
                            "請求次數",
                        )
                    )
                    tag = "[QUOTA]" if is_quota else "[API-ERR]"
                    # SDK 例外可能包含連線資訊，不可原樣寫入 log。
                    print(
                        f"[{now_str}]  ↳ {tag} {code} API 呼叫失敗 "
                        f"({err_type})；已啟用冷卻重試"
                    )
                    _set_kbars_backoff(code, current_start, current_end, 3600 if is_quota else 300)
                    current_start = current_end + timedelta(days=1)
                    continue

                if kbars and kbars.ts and len(kbars.ts) > 0:
                    df_new = pd.DataFrame(dict(kbars))
                    print(f"[{now_str}]  ↳ [OK] 取得 {len(df_new)} 筆")
                    all_df.append(df_new)
                    saved = _save_to_cache(code, df_new, cacheable_before)
                    # 最近交易時段校準由發出 HTTP 請求的圖表直接合併回傳資料。
                    # 若再廣播 history_cache_updated，其他舊分頁可能把通知當成
                    # 新的修復要求，形成 REST 自觸發迴圈。
                    if not refresh_recent:
                        await manager.broadcast(json.dumps({
                            "type": "history_cache_updated",
                            "data": {
                                "contract": code,
                                "start": s_str,
                                "end": e_str,
                                "rows": len(df_new),
                            },
                        }))
                    success_backoff = (
                        300 if current_end.date() >= today_tw else 60
                    )
                    _set_kbars_backoff(
                        code, current_start, current_end, success_backoff
                    )
                    if saved:
                        print(f"[{now_str}]  ↳ [CACHE] K 棒已寫入，完成日 {saved[0]} ~ {saved[-1]}")
                    else:
                        print(f"[{now_str}]  ↳ [CACHE] 今日 K 棒已暫存（不標記完整）")
                else:
                    ts_len = len(kbars.ts) if kbars and kbars.ts is not None else "N/A"
                    kbars_repr = repr(kbars)[:200] if kbars else "None"
                    print(f"[{now_str}]  ↳ [EMPTY] {code} {s_str}~{e_str} API 回傳空白")
                    print(f"[{now_str}]           kbars={kbars_repr} | ts筆數={ts_len}")
                    _set_kbars_backoff(code, current_start, current_end, 900)

                current_start = current_end + timedelta(days=1)

            # 啟動／切換商品時可能同時出現多個歷史請求。第二個請求會在
            # _kbars_lock 外先讀到舊快取，等第一個請求補完後又因 backoff
            # 略過 API；若直接回傳，舊快取就會覆蓋前一個完整結果。
            # API 批次結束後必須重新讀取 SQLite，讓每個併發請求都回傳
            # 目前最新、已包含關機期間回補資料的版本。
            refreshed_df = _load_from_cache(
                code, source_start_date, source_end_date
            )
            if not refreshed_df.empty:
                all_df.append(refreshed_df)
                print(
                    f"[{now_str}] [CACHE] {code} 補取後重新載入 "
                    f"{len(refreshed_df)} 筆"
                )

        if not all_df:
            print(f"[{now_str}] [STOP] 查詢結束：所有批次均無返回任何歷史數據，回傳空清單。\n")
            return []

        # keep='last'：重疊期間保留較新合約資料
        df = pd.concat(all_df).drop_duplicates(subset=['ts'], keep='last')
        df['ts'] = pd.to_datetime(df['ts'], unit='ns', utc=True)
        df.set_index('ts', inplace=True)
        df.sort_index(inplace=True)

        # 最近已結束日若只剩零散即時暫存列，寧可整日先不畫，
        # 也不要讓殘缺 OHLC 在 5/15/30 分圖上形成漂浮短棒。
        selected_code = str(getattr(contract, "code", ""))
        if selected_code.startswith(("TXF", "MXF", "TMF")):
            df, incomplete_dates, removed_count = (
                _drop_incomplete_recent_futures_dates(df, today_tw)
            )
            if incomplete_dates:
                print(
                    f"[{now_str}] [QUALITY] 略過不完整歷史日 "
                    f"{incomplete_dates}，移除 {removed_count} 筆零散棒"
                )

        original_len = len(df)

        if period != "1min":
            print(f"[{now_str}] [PROCESS] 依台指期交易時段聚合 {original_len} 筆 1min → {period}")
            df = _aggregate_kbars_dataframe(df, period)
            if period == "D":
                start_bound = pd.Timestamp(output_start_date, tz="UTC")
                end_bound = pd.Timestamp(
                    output_end_date + timedelta(days=1), tz="UTC"
                )
                df = df.loc[(df.index >= start_bound) & (df.index < end_bound)]

        if df.empty:
            print(f"[{now_str}] [STOP] 聚合後無任何有效資料欄位，回傳空清單。\n")
            return []

        df.reset_index(inplace=True)
        # 快取使用台灣牆鐘偏移，回傳前減 8 小時還原為真實 UTC epoch。
        df['time'] = (df['ts'].values.astype('int64') // 10**9) - 28800
        df.rename(columns={'Open': 'open', 'High': 'high', 'Low': 'low', 'Close': 'close', 'Volume': 'volume'}, inplace=True)

        res_data = df[['time', 'open', 'high', 'low', 'close', 'volume']].to_dict('records')
        print(f"[{now_str}] [OK] 歷史 K 線加載成功！最終回傳繪圖 K 棒總數: {len(res_data)} 筆。\n")
        return res_data

    except Exception as e:
        import traceback
        traceback.print_exc()
        print(f"[{now_str}] [ERROR] 歷史 K 線索取發生異常: {e}\n")
        raise HTTPException(status_code=500, detail=f"Backend Error: {str(e)}")

@app.get("/api/cache_info")
async def get_cache_info():
    """回傳 SQLite 快取的日期清單與今日即時 bar 數量。"""
    if not is_logged_in:
        raise HTTPException(status_code=401, detail="Not logged in")
    with sqlite3.connect(_KBARS_CACHE_DB) as conn:
        rows = conn.execute("SELECT date FROM cached_dates ORDER BY date").fetchall()
    all_dates = sorted({r[0] for r in rows})

    # DB 使用既有台灣牆鐘時間格式，今日範圍也必須用相同編碼。
    now_tw = datetime.utcnow() + timedelta(hours=8)
    day_start_ns = _taipei_wallclock_ns(
        datetime(now_tw.year, now_tw.month, now_tw.day)
    )
    day_end_ns   = day_start_ns + 86400 * 1_000_000_000
    rt_count = 0
    if _rt_contract_code:
        with sqlite3.connect(_KBARS_CACHE_DB) as conn:
            r = conn.execute(
                "SELECT COUNT(*) FROM kbars1m WHERE contract_code=? AND ts >= ? AND ts < ?",
                (_rt_contract_code, day_start_ns, day_end_ns)
            ).fetchone()
            rt_count = r[0] if r else 0

    return {
        "dates":        all_dates,
        "first":        all_dates[0]  if all_dates else None,
        "last":         all_dates[-1] if all_dates else None,
        "count":        len(all_dates),
        "rt_bars_today": rt_count
    }

@app.get("/api/snapshot")
async def get_snapshot():
    global api, is_logged_in, contract, last_snapshot_cache
    now_str = datetime.now().strftime('%H:%M:%S')
    if not is_logged_in or not contract:
        raise HTTPException(status_code=401, detail="Not logged in")
    try:
        loop = asyncio.get_running_loop()
        snaps = await loop.run_in_executor(None, lambda: api.snapshots([contract]))
        if not snaps:
            raise RuntimeError("富邦即時報價回傳空清單")
        snap = snaps[0]
        snap_price = safe_float(getattr(snap, "close", None), 0.0)
        if snap_price <= 0:
            raise RuntimeError("富邦即時報價無有效成交價")
        snap_time = _quote_timestamp(snap)
        snapshot_payload = _update_snapshot_cache(snap, snap_price, snap_time, "snapshot")
        # 偶爾輸出一行，避免過度洗板
        if datetime.now().second % 15 == 0:
            print(
                f"[{now_str}] [SNAPSHOT] 收盤價: {snapshot_payload['close']} "
                f"| 累計量: {snapshot_payload['total_volume']}"
            )
    except Exception as e:
        print(f"[{now_str}] [WARN] snapshots 抓取失敗，採用降級機制: {e}")
        # 如果快取中沒有任何收盤價，我們以合約的 reference（基準價/平盤價）作為所有價格的初始值！
        if last_snapshot_cache.get("close", 0.0) == 0.0:
            ref = _contract_reference(contract)
            print(f"[{now_str}]  ↳ ℹ️ 快取無先前紀錄，已採用合約參考平盤價初始化數值: {ref}")
            with _quote_state_lock:
                last_snapshot_cache.update({
                    "open": ref,
                    "high": ref,
                    "low": ref,
                    "close": ref,
                    "volume": 0,
                    "total_volume": 0,
                    "reference": ref,
                    "time": None,
                    "code": getattr(contract, "code", None),
                    "source": "contract_reference",
                })
    with _quote_state_lock:
        return dict(last_snapshot_cache)


@app.get("/api/quote_health")
async def get_quote_health():
    """Report the actual Fubon subscription/Tick health, not only browser WS state."""
    return _quote_health_data()


@app.get("/api/weighted_stocks_stream_health")
async def get_weighted_stocks_stream_health():
    """Report subscription and last-message state for the four weighted stocks."""
    return _weighted_stock_health_data()


@app.get("/api/major_weighted_stocks_intraday")
async def get_major_weighted_stocks_intraday():
    """Bootstrap four mini charts from Fubon REST; continue over Fubon WebSocket."""
    global api, is_logged_in
    stock_defs = list(_WEIGHTED_STOCK_DEFS)
    today_dt = (datetime.utcnow() + timedelta(hours=8)).date()
    if not is_logged_in:
        twse_snapshots = _fetch_twse_stock_snapshots(stock_defs)
        stocks = [
            _twse_snapshot_to_intraday_payload(code, name, twse_snapshots.get(code, {}))
            for code, name in stock_defs
        ]
        dates = sorted({s.get("date") for s in stocks if s.get("date")})
        return {
            "date": today_dt.strftime("%Y-%m-%d"),
            "data_date": dates[-1] if dates else None,
            "source": "twse_snapshot",
            "stocks": stocks,
        }

    candidate_dates = [
        (today_dt - timedelta(days=offset)).strftime("%Y-%m-%d")
        for offset in range(0, 8)
    ]
    loop = asyncio.get_running_loop()
    result = []
    twse_snapshots = None

    for code, name in stock_defs:
        stock_contract = _resolve_stock_contract(api, code)
        if not stock_contract:
            if twse_snapshots is None:
                twse_snapshots = _fetch_twse_stock_snapshots(stock_defs)
            result.append(_twse_snapshot_to_intraday_payload(code, name, twse_snapshots.get(code, {})))
            continue

        kbars = None
        used_date = None
        last_error = None
        for date_str in candidate_dates:
            try:
                async with _kbars_lock:
                    kbars = await loop.run_in_executor(
                        None,
                        lambda c=stock_contract, d=date_str: api.kbars(
                            contract=c, start=d, end=d, timeout=15000
                        )
                    )
                if kbars and getattr(kbars, "ts", None) and len(kbars.ts) > 0:
                    used_date = date_str
                    break
            except Exception as e:
                last_error = str(e)
                continue

        if not used_date:
            if twse_snapshots is None:
                twse_snapshots = _fetch_twse_stock_snapshots(stock_defs)
            payload = _twse_snapshot_to_intraday_payload(code, name, twse_snapshots.get(code, {}))
            if last_error and not payload.get("bars"):
                payload["error"] = last_error
            result.append(payload)
            continue

        df = pd.DataFrame(dict(kbars))
        if df.empty:
            if twse_snapshots is None:
                twse_snapshots = _fetch_twse_stock_snapshots(stock_defs)
            result.append(_twse_snapshot_to_intraday_payload(code, name, twse_snapshots.get(code, {})))
            continue

        df["ts"] = pd.to_datetime(df["ts"], unit="ns", utc=True)
        df.sort_values("ts", inplace=True)
        df["time"] = (df["ts"].values.astype("int64") // 10**9) - 28800
        df.rename(columns={"Close": "price", "Volume": "volume"}, inplace=True)

        if "Average" in df.columns:
            avg_price = pd.to_numeric(df["Average"], errors="coerce")
        elif "Amount" in df.columns:
            amount = pd.to_numeric(df["Amount"], errors="coerce").fillna(0)
            volume = pd.to_numeric(df["volume"], errors="coerce").fillna(0)
            cum_volume = volume.cumsum()
            cum_amount = amount.cumsum()
            avg_price = cum_amount / cum_volume.where(cum_volume > 0)
        else:
            amount = pd.to_numeric(df["price"], errors="coerce").fillna(0) * pd.to_numeric(df["volume"], errors="coerce").fillna(0)
            volume = pd.to_numeric(df["volume"], errors="coerce").fillna(0)
            cum_volume = volume.cumsum()
            cum_amount = amount.cumsum()
            avg_price = cum_amount / cum_volume.where(cum_volume > 0)
        df["avg"] = avg_price.ffill().fillna(df["price"])

        first_price = safe_float(df["price"].iloc[0], 0)
        open_price = (
            safe_float(df["Open"].iloc[0], first_price)
            if "Open" in df.columns
            else first_price
        )
        last_price = safe_float(df["price"].iloc[-1], 0)
        reference = _contract_reference(stock_contract)
        change_base = reference or open_price or first_price
        bars = []
        for row in df[["time", "price", "avg", "volume"]].itertuples(index=False):
            bars.append({
                "time": int(row.time),
                "price": safe_float(row.price),
                "avg": safe_float(row.avg),
                "volume": safe_int(row.volume),
            })

        stock_payload = {
            "code": code,
            "name": name,
            "date": used_date,
            "is_today": used_date == today_dt.strftime("%Y-%m-%d"),
            "source": "fubon_stock_candles",
            "open": open_price,
            "reference": reference,
            "last": last_price,
            "change": last_price - change_base if change_base else 0,
            "change_pct": (
                (last_price - change_base) / change_base * 100
                if change_base
                else 0
            ),
            "bars": bars,
        }
        result.append(stock_payload)
        with _weighted_stock_state_lock:
            state = _weighted_stock_stream_state.setdefault(
                code, _new_weighted_stock_state(code, name)
            )
            state.update({
                "last": last_price,
                "avg": safe_float(df["avg"].iloc[-1], last_price),
                "open": open_price,
                "reference": reference,
            })

    dates = sorted({s.get("date") for s in result if s.get("date")})
    fubon_count = sum(
        1 for stock in result
        if str(stock.get("source") or "").startswith("fubon_")
    )
    return {
        "date": today_dt.strftime("%Y-%m-%d"),
        "data_date": dates[-1] if dates else None,
        "source": (
            "fubon_stock_marketdata" if fubon_count else "twse_snapshot"
        ),
        "fubon_stock_count": fubon_count,
        "fallback_stock_count": len(result) - fubon_count,
        "stream": _weighted_stock_health_data(),
        "stocks": result,
    }

@app.get("/api/txf_amplitude")
async def get_txf_amplitude(period: str = "day"):
    """
    計算台指期震幅統計（近20個交易日/週/月）。
    period: day | week | month
    回傳: amp_max, amp_large, amp_avg, amp_small, amp_min, amp_today, days
    """
    import numpy as np

    import calendar as _cal
    now_tw = datetime.utcnow() + timedelta(hours=8)

    # 依週期決定回查天數
    look_back_days = {"day": 45, "week": 200, "month": 700}.get(period, 45)
    start_dt = now_tw - timedelta(days=look_back_days)
    # calendar.timegm 把 naive datetime 當作 UTC 轉 epoch，
    # 與既有快取的「UTC+8 時間直接當 UTC 儲存」格式一致
    start_ns = int(_cal.timegm(start_dt.timetuple()) * 1e9)
    end_ns   = int(_cal.timegm(now_tw.timetuple()) * 1e9)

    try:
        with sqlite3.connect(_KBARS_CACHE_DB, timeout=10) as conn:
            rows = conn.execute(
                "SELECT ts, High, Low FROM kbars1m "
                "WHERE contract_code='TXFR1' AND ts >= ? AND ts < ? ORDER BY ts",
                (start_ns, end_ns)
            ).fetchall()
            if not rows:
                rows = conn.execute(
                    "SELECT ts, High, Low FROM kbars1m "
                    "WHERE contract_code LIKE 'TXF%' AND ts >= ? AND ts < ? ORDER BY ts",
                    (start_ns, end_ns)
                ).fetchall()
    except Exception as e:
        raise HTTPException(status_code=500, detail=f"DB error: {e}")

    if not rows:
        return {"error": "no_data", "amp_max": None, "amp_large": None, "amp_avg": None,
                "amp_small": None, "amp_min": None, "amp_today": None, "days": 0}

    df = pd.DataFrame(rows, columns=['ts', 'High', 'Low'])
    # 快取時間戳為 UTC+8 牆鐘編碼（以 UTC 解析即為台灣時間）
    df_ts = pd.to_datetime(df['ts'], unit='ns', utc=True)

    # 交易日 session 定義：前一日 15:00（夜盤）→ 當日 13:45（日盤）。
    # 週五夜盤與週六凌晨必須併入下週一，不能獨立算成週六。
    trading_day = pd.Series(df_ts.dt.normalize(), index=df.index)
    evening_mask = df_ts.dt.hour >= 15
    trading_day.loc[evening_mask] = (
        trading_day.loc[evening_mask] + pd.Timedelta(days=1)
    )
    weekday = trading_day.dt.weekday
    trading_day = trading_day + pd.to_timedelta(
        weekday.map({5: 2, 6: 1}).fillna(0), unit="D"
    )
    df['_date'] = trading_day.dt.date

    if period == "week":
        df['_group'] = pd.to_datetime(df['_date'].astype(str)).dt.to_period('W')
    elif period == "month":
        df['_group'] = pd.to_datetime(df['_date'].astype(str)).dt.to_period('M')
    else:
        df['_group'] = df['_date']

    # 每組振幅 = 最高 - 最低。
    # day 模式依使用者定義：取過去 20 個交易日，每一天的「最高價 - 最低價」。
    # 「近20個完整交易日」不可把只有日盤、夜盤或零散即時列的日期納入。
    # 正常 TXF 交易日約 1,140 根 1 分 K；850 可容許少量缺筆，但排除半日殘缺。
    MIN_BARS = 850 if period == "day" else 1
    grp = df.groupby('_group').agg(
        grp_high=('High', 'max'), grp_low=('Low', 'min'), bar_count=('ts', 'count')
    )
    grp = grp[grp['bar_count'] >= MIN_BARS]
    grp['amplitude'] = grp['grp_high'] - grp['grp_low']
    grp = grp.sort_index()

    today = now_tw.date()
    if period == "day":
        # 日統計採「前 19 個完整交易日 + 本日即時振幅」共 20 日。
        # 先保留 20 日作為本日尚無資料時的 fallback；取得 amp_today 後再換入。
        hist = grp[grp.index < today].tail(20)
        current_period_row = df[df['_group'] == today]
    else:
        hist = grp.iloc[:-1].tail(20) if len(grp) > 1 else grp
        current_period_row = df[df['_group'] == grp.index[-1]] if len(grp) > 0 else pd.DataFrame()

    if hist.empty:
        return {"error": "insufficient_data", "amp_max": None, "amp_large": None,
                "amp_avg": None, "amp_small": None, "amp_min": None, "amp_today": None, "days": 0}

    def _round_amp(value):
        return int(float(value) + 0.5)

    # 本日/本週/本月震幅：從快取取當前進行中 session 的高低
    amp_today = None  # float | None
    amp_today_high = None
    amp_today_low = None
    if period == "day":
        # h>=15→次日 架構下，夜盤時段（h>=15）的K棒歸屬「明日」group
        now_hour = now_tw.hour
        if now_hour >= 15:
            tomorrow = (now_tw + timedelta(days=1)).date()
            live_session_row = df[df['_group'] == tomorrow]
        else:
            live_session_row = current_period_row
        if not live_session_row.empty:
            amp_today_high = float(live_session_row['High'].max())
            amp_today_low = float(live_session_row['Low'].min())
            cache_amp = amp_today_high - amp_today_low
            amp_today = cache_amp if cache_amp > 0 else None
    elif not current_period_row.empty:
        amp_today_high = float(current_period_row['High'].max())
        amp_today_low = float(current_period_row['Low'].min())
        cache_amp = amp_today_high - amp_today_low
        amp_today = cache_amp if cache_amp > 0 else None

    if period == "day" and is_logged_in:
        with _quote_state_lock:
            snap_high = safe_float(last_snapshot_cache.get("high"), 0)
            snap_low = safe_float(last_snapshot_cache.get("low"), 0)
        if snap_high > 0 and snap_low > 0:
            # Snapshot 的 high/low 可能只涵蓋目前盤段；不可覆蓋已包含夜盤的
            # 完整交易日 K 棒。兩者合併後再由最新 Tick 持續延伸。
            amp_today_high = (
                max(amp_today_high, snap_high)
                if amp_today_high is not None else snap_high
            )
            amp_today_low = (
                min(amp_today_low, snap_low)
                if amp_today_low is not None else snap_low
            )
            amp_today = amp_today_high - amp_today_low

    historical_amps = hist['amplitude'].values.astype(float)
    if period == "day" and amp_today is not None:
        # 本日進行中也算在「近 20 日」內，因此換掉最舊的一個完整交易日。
        amps = np.append(historical_amps[-19:], float(amp_today))
    else:
        amps = historical_amps

    amp_sum = float(np.sum(amps))
    amp_count = len(amps)
    amp_min = _round_amp(np.min(amps))
    amp_max = _round_amp(np.max(amps))
    amp_avg_raw = amp_sum / amp_count if amp_count else 0
    amp_avg = _round_amp(amp_avg_raw)
    # 大大震幅 = (平均震幅 + 最大震幅) / 2；小小震幅 = (平均震幅 + 最小震幅) / 2
    amp_large = _round_amp((amp_avg + amp_max) / 2)
    amp_small = _round_amp((amp_avg + amp_min) / 2)

    return {
        "amp_max":   amp_max,
        "amp_large": amp_large,
        "amp_avg":   amp_avg,
        "amp_small": amp_small,
        "amp_min":   amp_min,
        "amp_today": _round_amp(amp_today) if amp_today is not None else None,
        "amp_today_high": _round_amp(amp_today_high) if amp_today_high is not None else None,
        "amp_today_low": _round_amp(amp_today_low) if amp_today_low is not None else None,
        "amp_sum":   _round_amp(amp_sum),
        "days":      amp_count,
        "period":    period,
        "definitions": {
            "amp_max": "日週期為前19個完整交易日加本日即時振幅的最大值；其他週期取近20個完整週期",
            "amp_large": "(平均振幅 + 最大振幅) / 2",
            "amp_avg": "日週期為前19個完整交易日加本日即時振幅後除以20",
            "amp_sum": "納入統計的20日高低振幅點數加總",
            "amp_small": "(平均振幅 + 最小振幅) / 2",
            "amp_min": "日週期為前19個完整交易日加本日即時振幅的最小值；其他週期取近20個完整週期",
            "amp_today": "目前進行中週期的高低點振幅",
        },
    }


@app.get("/api/amplitude_statistics")
async def get_amplitude_statistics(days: int = 20, contract: str = "TXFR1", date_mode: str = "trading_date"):
    """
    三時段震幅統計：早盤(08:45-13:45) / 午盤(15:00-21:30) / 晚盤(21:30-05:00)
    回傳最近 N 個完整交易日 + 今日的震幅表格。
    """
    import numpy as np
    import calendar as _cal

    now_tw = datetime.utcnow() + timedelta(hours=8)
    today = now_tw.date()

    look_back_days = (days + 25) * 3 + 14  # 顯示N天 + 20天rolling歷史 + buffer
    start_dt = now_tw - timedelta(days=look_back_days)
    start_ns = int(_cal.timegm(start_dt.timetuple()) * 1e9)
    end_ns   = int(_cal.timegm(now_tw.timetuple()) * 1e9)

    try:
        with sqlite3.connect(_KBARS_CACHE_DB, timeout=10) as conn:
            rows = conn.execute(
                "SELECT ts, High, Low FROM kbars1m "
                "WHERE contract_code=? AND ts >= ? AND ts < ? ORDER BY ts",
                (contract, start_ns, end_ns)
            ).fetchall()
            if not rows:
                rows = conn.execute(
                    "SELECT ts, High, Low FROM kbars1m "
                    "WHERE contract_code LIKE 'TXF%' AND ts >= ? AND ts < ? ORDER BY ts",
                    (start_ns, end_ns)
                ).fetchall()
    except Exception as e:
        raise HTTPException(status_code=500, detail=f"DB error: {e}")

    if not rows:
        return {"success": False, "error": "no_data", "columns": [], "rows": [], "stats": {}}

    df = pd.DataFrame(rows, columns=['ts', 'High', 'Low'])
    # 快取時間戳：UTC+8 編碼為 UTC，以 UTC 解析後直接得到台灣時間
    df_ts = pd.to_datetime(df['ts'], unit='ns', utc=True)

    tw_hour   = df_ts.dt.hour.values
    tw_minute = df_ts.dt.minute.values
    tw_date   = df_ts.dt.date.values

    # 從早盤資料中取得已知的有效交易日（排除國定假日等無早盤日）
    _ktd_set = set()
    for i in range(len(tw_hour)):
        t = int(tw_hour[i]) * 60 + int(tw_minute[i])
        if 8*60+45 <= t < 13*60+45:
            _ktd_set.add(tw_date[i])
    _known_trading_days = sorted(_ktd_set)

    def next_trading_day(d):
        """下一個有效交易日：優先查 DB 中有早盤資料的日期，fallback 跳週末。"""
        for td in _known_trading_days:
            if td > d:
                return td
        next_d = d + timedelta(days=1)
        while next_d.weekday() >= 5:
            next_d += timedelta(days=1)
        return next_d

    sessions       = []
    trading_dates  = []
    for i in range(len(df)):
        h, m, d = int(tw_hour[i]), int(tw_minute[i]), tw_date[i]
        t = h * 60 + m
        if 8*60+45 <= t < 13*60+45:
            sessions.append('morning');   trading_dates.append(d)
        elif 15*60 <= t < 21*60+30:
            if date_mode == 'trading_date':
                sessions.append('afternoon'); trading_dates.append(next_trading_day(d))
            else:
                sessions.append('afternoon'); trading_dates.append(d)
        elif t >= 21*60+30:
            if date_mode == 'trading_date':
                sessions.append('night');     trading_dates.append(next_trading_day(d))
            else:
                sessions.append('night');     trading_dates.append(d)
        elif t < 5*60:
            if date_mode == 'trading_date':
                sessions.append('night');     trading_dates.append(next_trading_day(d - timedelta(days=1)))
            else:
                sessions.append('night');     trading_dates.append(d - timedelta(days=1))
        else:
            sessions.append(None);        trading_dates.append(d)

    df['session']      = sessions
    df['trading_date'] = trading_dates
    df = df[df['session'].notna()].copy()

    grp = df.groupby(['trading_date', 'session']).agg(
        high=('High', 'max'), low=('Low', 'min')
    ).reset_index()
    grp['amplitude'] = (grp['high'] - grp['low']).astype(int)

    session_lookup = {
        (row['trading_date'], row['session']): {
            'value': int(row['amplitude']),
            'high':  int(row['high']),
            'low':   int(row['low']),
        }
        for _, row in grp.iterrows()
    }

    # ── 今日資料補取：DB 若無今日任何時段，從富邦日內 REST 取 ───────────────────
    if is_logged_in and api and _rt_contract_code:
        has_today = any((today, s) in session_lookup for s in ('morning', 'afternoon', 'night'))
        if not has_today:
            try:
                rt_obj = _resolve_market_contract(api, _rt_contract_code)
                if rt_obj:
                    today_str = today.isoformat()
                    loop = asyncio.get_running_loop()
                    async with _kbars_lock:
                        today_kbars = await loop.run_in_executor(
                            None,
                            lambda: api.kbars(
                                contract=rt_obj,
                                start=today_str,
                                end=today_str,
                                timeout=15000,
                            )
                        )
                    if today_kbars and today_kbars.ts and len(today_kbars.ts) > 0:
                        df_t = pd.DataFrame(dict(today_kbars))
                        dts_t = pd.to_datetime(df_t['ts'], unit='ns', utc=True)
                        # 累積各時段高低點
                        sess_acc = {}
                        for i in range(len(df_t)):
                            h = int(dts_t.iloc[i].hour)
                            m = int(dts_t.iloc[i].minute)
                            t_min = h * 60 + m
                            bar_d = dts_t.iloc[i].date()
                            hi = float(df_t['High'].iloc[i])
                            lo = float(df_t['Low'].iloc[i])
                            if 8*60+45 <= t_min < 13*60+45:
                                key = (bar_d, 'morning')
                            elif 15*60 <= t_min < 21*60+30:
                                td = next_trading_day(bar_d) if date_mode == 'trading_date' else bar_d
                                key = (td, 'afternoon')
                            elif t_min >= 21*60+30:
                                td = next_trading_day(bar_d) if date_mode == 'trading_date' else bar_d
                                key = (td, 'night')
                            elif t_min < 5*60:
                                prev = bar_d - timedelta(days=1)
                                td = next_trading_day(prev) if date_mode == 'trading_date' else prev
                                key = (td, 'night')
                            else:
                                continue
                            if key not in sess_acc:
                                sess_acc[key] = {'high': hi, 'low': lo}
                            else:
                                sess_acc[key]['high'] = max(sess_acc[key]['high'], hi)
                                sess_acc[key]['low']  = min(sess_acc[key]['low'], lo)
                        for key, sd in sess_acc.items():
                            if key not in session_lookup:
                                session_lookup[key] = {
                                    'value': int(sd['high'] - sd['low']),
                                    'high': int(sd['high']),
                                    'low':  int(sd['low']),
                                }
            except Exception as e:
                print(f"[AmpStats] today fetch fallback failed: {e}")

    # 完整交易日 = 有早盤資料且早於今日（保留所有查到的，供 rolling 計算用）
    all_complete_days = sorted(
        d for (d, s) in session_lookup if s == 'morning' and d < today
    )

    # 顯示視窗：僅最近 days 天
    display_days = all_complete_days[-days:]

    # ── Per-date rolling 統計（每個日期欄使用「該日期之前」的最近 20 個完整交易日）──
    # prior 必須從 all_complete_days 取，確保第一個顯示日期也有足夠的歷史基礎
    all_dates = display_days + [today]
    date_stats = {}
    for d in all_dates:
        prior = [cd for cd in all_complete_days if cd < d][-20:]
        date_stats[d] = {}
        for sess in ('morning', 'afternoon', 'night'):
            amps = [session_lookup[(cd, sess)]['value']
                    for cd in prior if (cd, sess) in session_lookup]
            if amps:
                date_stats[d][sess] = {
                    'avg20': float(np.mean(amps)),
                    'max20': float(np.max(amps)),
                    'min20': float(np.min(amps)),
                }
            else:
                date_stats[d][sess] = {'avg20': None, 'max20': None, 'min20': None}

    # 最新 20 日統計（供 response.stats 摘要使用）
    stats = date_stats.get(today, {
        s: {'avg20': None, 'max20': None, 'min20': None}
        for s in ('morning', 'afternoon', 'night')
    })

    def _status(value, sess, d):
        if value is None:
            return 'empty'
        st = date_stats.get(d, {}).get(sess, {})
        avg20, max20, min20 = st.get('avg20'), st.get('max20'), st.get('min20')
        if avg20 is None:
            return 'empty'
        if max20 and value >= max20 * 0.95:
            return 'super_large'
        if min20 and value <= min20 * 1.05:
            return 'compressed'
        if value > avg20:
            return 'large'
        if value < avg20:
            return 'small'
        return 'normal'

    _wday = {0: '一', 1: '二', 2: '三', 3: '四', 4: '五', 5: '六', 6: '日'}
    columns_out = [
        {
            'date':     d.isoformat(),
            'weekday':  _wday[d.weekday()],
            'label':    f"{d.month}/{d.day}",
            'is_today': d == today,
        }
        for d in all_dates
    ]

    session_defs = [
        ('morning',   '08:45~13:45'),
        ('afternoon', '15:00~21:30'),
        ('night',     '21:30~05:00'),
    ]
    rows_out = []
    for sess_key, sess_label in session_defs:
        cells = []
        for d in all_dates:
            info = session_lookup.get((d, sess_key))
            if info is None:
                cells.append({'date': d.isoformat(), 'value': None,
                              'status': 'empty', 'high': None, 'low': None})
            else:
                cells.append({
                    'date':   d.isoformat(),
                    'value':  info['value'],
                    'status': _status(info['value'], sess_key, d),
                    'high':   info['high'],
                    'low':    info['low'],
                })
        rows_out.append({'key': sess_key, 'label': sess_label, 'cells': cells})

    # 振幅總和 row
    total_cells = []
    for d in all_dates:
        parts = [session_lookup.get((d, s)) for s in ('morning', 'afternoon', 'night')]
        avail = [p['value'] for p in parts if p is not None]
        if avail:
            total_cells.append({'date': d.isoformat(), 'value': sum(avail),
                                 'status': 'normal', 'high': None, 'low': None})
        else:
            total_cells.append({'date': d.isoformat(), 'value': None,
                                 'status': 'empty', 'high': None, 'low': None})
    rows_out.append({'key': 'total', 'label': '振幅總和', 'cells': total_cells})

    # 各時段 rolling 20 日平均 rows（每欄顯示該日期之前的 rolling avg，非固定值）
    for sess_key, avg_label in [
        ('morning',   '早盤20日Avg.'),
        ('afternoon', '午盤20日Avg.'),
        ('night',     '晚盤20日Avg.'),
    ]:
        cells = []
        for d in all_dates:
            avg_val = date_stats[d][sess_key].get('avg20')
            avg_rounded = round(avg_val) if avg_val is not None else None
            cells.append({'date': d.isoformat(), 'value': avg_rounded,
                          'status': 'avg', 'high': None, 'low': None})
        rows_out.append({'key': f'{sess_key}_avg20', 'label': avg_label, 'cells': cells})

    return {
        'success':    True,
        'contract':   contract,
        'days':       len(display_days),
        'updated_at': now_tw.strftime('%Y-%m-%d %H:%M:%S'),
        'columns':    columns_out,
        'rows':       rows_out,
        'stats': {
            k: {sk: round(sv) if sv is not None else None for sk, sv in v.items()}
            for k, v in stats.items()
        },
    }


@app.get("/api/institutional_rankings")
async def get_institutional_rankings():
    from fastapi.responses import JSONResponse
    import urllib.request

    def safe_get(row, idx, default="0"):
        try:
            return row[idx]
        except (IndexError, TypeError):
            return default

    # 本地快取檔案
    CACHE_FILE = "institutional_cache.json"
    today_str = datetime.now().strftime("%Y%m%d")

    # 1. 嘗試讀取本地快取
    cache_data = None
    if os.path.exists(CACHE_FILE):
        try:
            with open(CACHE_FILE, "r", encoding="utf-8") as f:
                cache_data = json.load(f)
        except Exception as e:
            print("讀取三大法人快取失敗:", e)
            
    # 如果快取存在、是今天存的、且實際資料日期也是今天，才直接回傳
    today_date_formatted = f"{today_str[:4]}/{today_str[4:6]}/{today_str[6:]}"
    if cache_data and cache_data.get("cache_date") == today_str and cache_data.get("date") == today_date_formatted:
        return cache_data

    # 2. 爬取證交所三大法人買賣超數據 (向下尋找最新有交易的交易日)
    curr_date = datetime.now()
    res_data = None
    fetched_date_str = ""
    
    for i in range(10):
        test_date_obj = curr_date - timedelta(days=i)
        # 跳過週末 (週六、週日證交所絕對沒有資料)
        if test_date_obj.weekday() in [5, 6]:
            continue
            
        test_date_str = test_date_obj.strftime("%Y%m%d")
        try:
            print(f"嘗試抓取三大法人數據: {test_date_str}")
            url = f"https://www.twse.com.tw/fund/T86?response=json&date={test_date_str}&selectType=ALLBUT0999"
            req = urllib.request.Request(
                url, 
                headers={'User-Agent': 'Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/120.0.0.0 Safari/537.36'}
            )
            with urllib.request.urlopen(req, timeout=5) as response:
                raw_json = json.loads(response.read().decode('utf-8'))
                if raw_json.get("stat") == "OK" and raw_json.get("data"):
                    res_data = raw_json
                    fetched_date_str = test_date_str
                    break
            # 每次請求間隔 500ms，對證交所伺服器表示禮貌，避免被封鎖
            await asyncio.sleep(0.5)
        except Exception as e:
            print(f"抓取 {test_date_str} 失敗:", e)
            
    # 如果完全抓不到 (例如無網路或證交所 API 修改)，且有舊快取，則退一步使用舊快取
    if not res_data:
        if cache_data:
            print("無法抓取最新數據，退而使用舊快取")
            return cache_data
        return JSONResponse(
            status_code=503,
            content={"status": "error", "detail": "無法取得證交所三大法人數據，請稍後再試。"}
        )

    # 3. 解析與清洗資料
    fields = res_data.get('fields', [])
    raw_rows = res_data.get('data', [])
    
    # 動態定位欄位索引，百分之百相容證交所未來修改欄位順序！
    code_idx = 0
    name_idx = 1
    foreign_idx = 4
    it_idx = 10
    dealer_idx = 11
    total_idx = 18
    
    for idx, f in enumerate(fields):
        f_clean = f.replace(" ", "")
        if "證券代號" in f_clean: code_idx = idx
        elif "證券名稱" in f_clean: name_idx = idx
        elif "外陸資買賣超股數" in f_clean and "不含外資自營商" in f_clean: foreign_idx = idx
        elif "投信買賣超股數" in f_clean: it_idx = idx
        elif "自營商買賣超股數" in f_clean: dealer_idx = idx
        elif "三大法人買賣超股數" in f_clean: total_idx = idx

    def parse_int(val_str):
        try:
            return int(str(val_str).replace(",", "").strip())
        except:
            return 0

    processed_list = []
    skipped_rows = 0
    for row in raw_rows:
        try:
            code_raw = safe_get(row, code_idx, "")
            name_raw = safe_get(row, name_idx, "")
            if not code_raw:
                skipped_rows += 1
                print(f"[三大法人] 警告：row 長度 {len(row)} 不足，跳過此筆")
                continue
            code = str(code_raw).strip()
            name = str(name_raw).strip()

            # 轉為張數 (股數 / 1000)
            foreign_net = parse_int(safe_get(row, foreign_idx)) // 1000
            it_net      = parse_int(safe_get(row, it_idx))      // 1000
            dealer_net  = parse_int(safe_get(row, dealer_idx))  // 1000
            total_net   = parse_int(safe_get(row, total_idx))   // 1000

            # 排除權證、可轉債等（代號超過6碼）
            if len(code) > 6:
                continue

            processed_list.append({
                "code": code,
                "name": name,
                "foreign": foreign_net,
                "it": it_net,
                "dealer": dealer_net,
                "total": total_net
            })
        except Exception as _row_err:
            skipped_rows += 1
            print(f"[三大法人] 警告：解析 row 失敗（{_row_err}），跳過此筆")
    if skipped_rows:
        print(f"[三大法人] 共跳過 {skipped_rows} 筆格式異常的 row")

    # 分別排列買超前 15 名與賣超前 15 名
    buy_rank = sorted(processed_list, key=lambda x: x["total"], reverse=True)[:15]
    sell_rank = sorted(processed_list, key=lambda x: x["total"])[:15]

    result = {
        "status": "success",
        "cache_date": today_str,
        "date": f"{fetched_date_str[:4]}/{fetched_date_str[4:6]}/{fetched_date_str[6:]}",
        "buy_rank": buy_rank,
        "sell_rank": sell_rank
    }

    # 4. 寫入本地快取
    try:
        with open(CACHE_FILE, "w", encoding="utf-8") as f:
            json.dump(result, f, ensure_ascii=False, indent=4)
    except Exception as e:
        print("儲存三大法人快取失敗:", e)

    return result

@app.get("/api/industry_rankings")
async def get_industry_rankings():
    """依策略選股結果計算產業分數與排行"""
    try:
        result_dict = screener.run_screener_query()
        stocks = result_dict.get("stocks", []) if isinstance(result_dict, dict) else result_dict
        rankings = screener.compute_industry_rankings(stocks)
        return sanitize_for_json({"status": "success", "data": rankings})
    except Exception as e:
        import traceback
        traceback.print_exc()
        raise HTTPException(status_code=500, detail=f"產業排行計算失敗: {str(e)}")

@app.post("/api/screener/run")
async def api_run_screener(payload: dict = {}):
    """執行六步驟策略選股"""
    from fastapi.responses import JSONResponse
    import json
    max_decline = float(payload.get("max_decline_pct", -3.5))
    trace_code = str(payload.get("traceCode", "")).strip() or None
    try:
        result_dict = screener.run_screener_query(
            max_decline_pct=max_decline,
            trace_code=trace_code
        )
        if isinstance(result_dict, dict):
            stocks             = result_dict.get("stocks", [])
            market_status_data = result_dict.get("market_status")
            buy_candidates     = result_dict.get("buy_candidates", [])
            high_priority      = result_dict.get("high_priority_watch", [])
            other_watch        = result_dict.get("other_watch", [])
            excluded           = result_dict.get("excluded", [])
            etf_candidates     = result_dict.get("etf_candidates", [])
            summary            = result_dict.get("summary", {})
        else:
            stocks = result_dict
            market_status_data = buy_candidates = high_priority = other_watch = excluded = etf_candidates = None
            summary = {}
        response_data = {
            "status":              "success",
            "data":                stocks,           # 向後相容（全部）
            "buy_candidates":      buy_candidates,   # 明日可買（最多20）
            "high_priority_watch": high_priority,    # 高優先觀察（最多50）
            "other_watch":         other_watch,      # 其他觀察
            "excluded":            excluded,         # 排除清單
            "etf_candidates":      etf_candidates,   # ETF候選
            "summary":             summary,
            "market_status":       market_status_data,
        }
        response_data = sanitize_for_json(response_data)
        return response_data
    except Exception as e:
        import traceback
        traceback.print_exc()
        raise HTTPException(status_code=500, detail=f"選股計算失敗: {str(e)}")

def _get_tg_recipients() -> list:
    """從環境變數讀取收件人清單 [{name, chatId}, ...]"""
    raw = os.environ.get("TELEGRAM_RECIPIENTS", "")
    if raw:
        try:
            return json.loads(raw)
        except Exception:
            pass
    # 相容舊版單一 chat_id 格式
    old_id = os.environ.get("TELEGRAM_CHAT_ID", "")
    if old_id:
        return [{"name": "預設", "chatId": old_id}]
    return []

async def _generate_ai_insights(stocks: list) -> dict:
    """呼叫 Claude API 為每支股票生成一句話分析，回傳 {code: insight}"""
    api_key = os.environ.get("ANTHROPIC_API_KEY", "")
    if not api_key or not stocks:
        return {}
    try:
        import anthropic
        client = anthropic.AsyncAnthropic(api_key=api_key)
        summaries = []
        for s in stocks:
            code     = s.get("stockCode") or s.get("code", "?")
            name     = s.get("stockName") or s.get("name", "?")
            price    = s.get("closePrice") or s.get("close", 0)
            score    = s.get("score") or s.get("priority", 0)
            bias     = s.get("bias20") or s.get("bias", 0)
            r20      = s.get("return20") or s.get("gain_20", 0)
            inst     = s.get("institutionBuyRatio5") or s.get("inst_ratio_5d", 0)
            features = s.get("majorFeatures") or []
            industry = s.get("industry", "")
            plan     = s.get("actionPlan") or {}
            feat_str = "、".join(features) if features else "無"
            entry    = (plan.get("conservative") or "")[:60]
            summaries.append(
                f"[{code}] {name}（{industry}）\n"
                f"收盤{price:.2f} 分數{score} 乖離{bias:+.1f}% 20日強度{r20:+.1f}%\n"
                f"法人5日佔比{inst:.1f}% 籌碼特徵：{feat_str}\n"
                f"進場方向：{entry}"
            )
        prompt = (
            "以下是今日技術面與籌碼面強勢的台股候選標的資訊。\n"
            "請為每支股票用繁體中文寫一句話（25字以內）說明其值得關注的核心理由，"
            "重點放在籌碼動向與技術訊號，風格簡潔直白。\n\n"
            "輸出格式（嚴格遵守，每行一支，不要其他任何文字）：\n"
            "代號:理由\n\n"
            "候選標的：\n\n" + "\n\n".join(summaries)
        )
        msg = await client.messages.create(
            model="claude-haiku-4-5",
            max_tokens=512,
            messages=[{"role": "user", "content": prompt}]
        )
        text = msg.content[0].text if msg.content else ""
        insights = {}
        for line in text.strip().splitlines():
            if ":" in line:
                code_part, _, insight = line.partition(":")
                code_part = code_part.strip()
                insight   = insight.strip()
                if code_part and insight:
                    insights[code_part] = insight
        print(f"[AI] 生成 {len(insights)} 筆個股分析")
        return insights
    except Exception as e:
        print(f"[AI] 分析生成失敗：{e}")
        return {}

def _build_tg_message(stocks: list, label: str, total: int = 0, all_stocks: list = None, market_status: dict = None) -> str:
    """組成 Telegram 訊息文字（HTML 模式，含產業摘要、市場狀態與個股建議買法）"""
    now_str = datetime.now().strftime("%Y/%m/%d %H:%M")

    # ── 計算產業排行：用完整名單（含 ETF）確保產業摘要不受推薦過濾影響 ──
    try:
        ind_rankings = screener.compute_industry_rankings(all_stocks if all_stocks is not None else stocks)
    except Exception:
        ind_rankings = []

    # 建立 code → 產業資訊快速查表
    ind_lookup: dict = {}
    for ind in ind_rankings:
        for s in ind.get("stocks", []):
            code_key = s.get("stockCode") or s.get("code", "")
            ind_lookup[code_key] = {
                "name":      ind["industryName"],
                "score":     ind["industryScore"],
                "resonance": s.get("hasIndustryResonance", False),
            }

    # ── 頭部：市場狀態 → 時間 → 產業摘要 → 清單標題 ──
    lines = []
    if market_status:
        ms_status  = market_status.get('status', 'normal_bull')
        ms_label   = market_status.get('label', '')
        ms_suggest = market_status.get('suggestion', '')
        ms_emoji   = {'normal_bull': '🟢', 'hot_bull': '🟡', 'overheated_bull': '🔴', 'weak_market': '⚪'}.get(ms_status, '📊')
        m = market_status.get('metrics', {})
        lines.append(
            f"📊 今日市場狀態：{ms_emoji} <b>{_he(ms_label)}</b>\n"
            f"大盤：{m.get('index_close',0):,.0f}  20MA：{m.get('index_ma20',0):,.0f}  60MA：{m.get('index_ma60',0):,.0f}\n"
            f"距20MA：{m.get('bias_ma20_pct',0):+.1f}%  距60MA：{m.get('bias_ma60_pct',0):+.1f}%  "
            f"過熱個股：{m.get('hot_stock_ratio',0):.0f}%\n"
            f"操作原則：{_he(ms_suggest)}\n"
            f"{'─'*28}"
        )
    lines.append(f"🕐 {now_str}")

    top3 = ind_rankings[:3]
    if top3:
        medals = ["🥇", "🥈", "🥉"]
        ind_lines = ["🏭 <b>今日強勢產業</b>"]
        for i, ind in enumerate(top3):
            ind_lines.append(
                f"{medals[i]} {_he(ind['industryName'])}　"
                f"分數 {ind['industryScore']}　"
                f"候選 {ind['candidateCount']} 檔"
            )
        ind_lines.append('─' * 28)
        lines.append("\n".join(ind_lines))

    lines.append(f"📊 <b>{_he(label)} 交易清單</b>")

    # ── 個股區塊 ──
    for s in stocks:
        code  = _he(s.get("stockCode") or s.get("code", "?"))
        name  = _he(s.get("stockName") or s.get("name", "?"))
        price = s.get("closePrice") or s.get("close", 0)
        score = s.get("score") or s.get("priority", 0)
        bias  = s.get("bias20") or s.get("bias", 0)
        r20   = s.get("return20") or s.get("gain_20", 0)
        sl_p  = s.get("stopLossPrice", 0)
        sl_pc = s.get("stopLossPercent", 0)
        inst  = s.get("institutionBuyRatio5") or s.get("inst_ratio_5d", 0)
        plan  = s.get("actionPlan") or {}

        sl_text   = f"{sl_p:.2f} ({sl_pc:+.1f}%)" if sl_p else "--"
        bias_sign = "+" if bias >= 0 else ""
        r20_sign  = "+" if r20  >= 0 else ""

        major_features = s.get("majorFeatures") or []
        major_line = ""
        if major_features:
            tags = "  ".join(f"#{_he(f)}" for f in major_features)
            major_line = f"⭐ 主力特徵｜{tags}\n"

        # 產業標註行
        ind_info  = ind_lookup.get(s.get("stockCode") or s.get("code", ""), {})
        ind_name  = _he(ind_info.get("name", s.get("industry", "")))
        ind_score = ind_info.get("score", 0)
        resonance = ind_info.get("resonance", False)
        if ind_name:
            resonance_tag = "  🔥 產業共振" if resonance else ""
            ind_line = f"🏭 {ind_name}  ｜ 產業分數 {ind_score}{resonance_tag}\n"
        else:
            ind_line = ""

        state       = s.get("strategyState", "")
        stock_emoji = "🔵" if state == "觀察中" else "🟢"
        state_tag   = "  〔觀察中〕" if state == "觀察中" else ""
        block = (
            f"\n{stock_emoji} <b>#{code} {name}</b>  ｜ 分數 {score}{state_tag}\n"
            f"{ind_line}"
            f"💰 收盤 {price:.2f}  ｜ 乖離 {bias_sign}{bias}%  ｜ 20日強度 {r20_sign}{r20}%\n"
            f"👥 法人佔比 {inst:.2f}%  ｜ 停損價 {sl_text}\n"
            f"{major_line}"
        )
        # 建議買法（market status aware）
        bm = s.get("buy_method") or {}
        if bm:
            bm_allowed = bm.get("allowed", True)
            bm_label   = _he(bm.get("label", ""))
            bm_entry   = _he(bm.get("entry_condition", ""))
            bm_sl_rule = _he(bm.get("stop_loss_rule", ""))
            allow_tag  = "✅" if bm_allowed else "🚫"
            block += (
                f"\n{allow_tag} <b>建議買法</b>：{bm_label}\n"
                f"進場條件：{bm_entry}\n"
                f"停損規則：{bm_sl_rule}\n"
            )
        if plan.get("conservative"):
            block += f"\n📌 <b>保守進場</b>\n{_he(plan['conservative'])}\n"
        if plan.get("aggressive"):
            block += f"\n🚀 <b>積極進場</b>\n{_he(plan['aggressive'])}\n"
        if plan.get("avoid"):
            block += f"\n⚠️ <b>不進場條件</b>\n{_he(plan['avoid'])}\n"
        if plan.get("stopLoss"):
            block += f"\n🛡 <b>停損條件</b>\n{_he(plan['stopLoss'])}\n"
        block += f"{'─'*28}"
        lines.append(block)

    if total and total > len(stocks):
        lines.append(f"\n前 <b>{len(stocks)}</b> 名 ／ 共 <b>{total}</b> 檔候選")
    else:
        lines.append(f"\n共 <b>{len(stocks)}</b> 檔候選")
    return "\n".join(lines)

_TG_MAX_LEN = 4000  # Telegram 上限 4096，留緩衝


def _he(text) -> str:
    """Escape HTML entities for Telegram HTML mode (<, >, &)."""
    return _html_mod.escape(str(text))


def _split_tg_message(message: str) -> list[str]:
    """以個股分隔線為邊界切割訊息，確保每段 <= _TG_MAX_LEN"""
    if len(message) <= _TG_MAX_LEN:
        return [message]
    # 以分隔線切塊（每個個股區塊末尾有 ─*28）
    SEPARATOR = "─" * 28
    parts, current = [], ""
    for chunk in message.split(SEPARATOR):
        segment = chunk + SEPARATOR
        if len(current) + len(segment) > _TG_MAX_LEN:
            if current:
                parts.append(current.rstrip(SEPARATOR))
            current = segment
        else:
            current += segment
    if current:
        parts.append(current.rstrip(SEPARATOR))
    return parts or [message[:_TG_MAX_LEN]]

def _tg_post(url: str, chat_id: str, text: str, parse_mode: str = "HTML") -> tuple[bool, str]:
    """送出單則訊息，回傳 (success, error_str)"""
    body = json.dumps({"chat_id": chat_id, "text": text, "parse_mode": parse_mode}).encode("utf-8")
    req  = urllib.request.Request(url, data=body, headers={"Content-Type": "application/json"})
    try:
        with urllib.request.urlopen(req, timeout=10) as resp:
            result = json.loads(resp.read().decode("utf-8"))
            if result.get("ok"):
                return True, ""
            return False, str(result)
    except urllib.error.HTTPError as e:
        body_str = e.read().decode("utf-8", errors="replace")
        print(f"[TG] HTTPError {e.code} → {body_str}")
        return False, f"HTTP {e.code} {body_str}"
    except Exception as e:
        print(f"[TG] Exception → {e}")
        return False, str(e)

def _send_tg_to_all(message: str) -> dict:
    """廣播訊息給所有收件人（自動分段），回傳 {ok: N, fail: N, errors: [...]}"""
    token = os.environ.get("TELEGRAM_BOT_TOKEN", "")
    if not token:
        return {"ok": 0, "fail": 0, "errors": ["尚未設定 Bot Token"]}
    recipients = _get_tg_recipients()
    if not recipients:
        return {"ok": 0, "fail": 0, "errors": ["尚未設定任何收件人"]}
    parts = _split_tg_message(message)
    url = f"https://api.telegram.org/bot{token}/sendMessage"
    ok_count, fail_count, errors = 0, 0, []
    for r in recipients:
        chat_id = r.get("chatId", "")
        if not chat_id:
            continue
        recipient_ok = True
        for part in parts:
            success, err = _tg_post(url, chat_id, part)
            if not success:
                recipient_ok = False
                errors.append(f"{r.get('name','?')}：{err}")
        if recipient_ok:
            ok_count += 1
        else:
            fail_count += 1
    return {"ok": ok_count, "fail": fail_count, "errors": errors}

# ── 整合選股 TG 推送：DB 目標管理 ─────────────────────────────────────────────

def _get_tg_db_conn():
    conn = sqlite3.connect(_STOCK_DB_PATH, timeout=60.0)
    conn.row_factory = sqlite3.Row
    try:
        conn.execute("PRAGMA journal_mode=WAL;")
    except Exception:
        pass
    return conn

def _init_tg_targets_table():
    conn = _get_tg_db_conn()
    try:
        conn.execute("""
            CREATE TABLE IF NOT EXISTS telegram_targets (
                id          INTEGER PRIMARY KEY AUTOINCREMENT,
                chat_id     TEXT NOT NULL UNIQUE,
                name        TEXT,
                enabled     INTEGER DEFAULT 1,
                target_type TEXT DEFAULT 'stock',
                created_at  TEXT,
                updated_at  TEXT
            )
        """)
        conn.commit()
        # Migration v1: 既有 DB 若缺少 target_type 欄位則補上
        try:
            conn.execute("ALTER TABLE telegram_targets ADD COLUMN target_type TEXT DEFAULT 'stock'")
            conn.commit()
        except Exception:
            pass  # 欄位已存在，忽略
        # Migration v2: 將 UNIQUE(chat_id) 升級為 UNIQUE(chat_id, target_type)
        # 讓同一個人可以分別作為 stock 和 amplitude 目標
        try:
            indexes = conn.execute("PRAGMA index_list(telegram_targets)").fetchall()
            has_composite = any(
                len(conn.execute(f"PRAGMA index_info({dict(idx)['name']})").fetchall()) == 2
                for idx in indexes
                if dict(idx).get('unique') == 1
            )
            if not has_composite:
                conn.execute("""
                    CREATE TABLE _tg_targets_v2 (
                        id          INTEGER PRIMARY KEY AUTOINCREMENT,
                        chat_id     TEXT NOT NULL,
                        name        TEXT,
                        enabled     INTEGER DEFAULT 1,
                        target_type TEXT DEFAULT 'stock',
                        created_at  TEXT,
                        updated_at  TEXT,
                        UNIQUE(chat_id, target_type)
                    )
                """)
                conn.execute(
                    "INSERT OR IGNORE INTO _tg_targets_v2 "
                    "(id, chat_id, name, enabled, target_type, created_at, updated_at) "
                    "SELECT id, chat_id, name, enabled, target_type, created_at, updated_at FROM telegram_targets"
                )
                conn.execute("DROP TABLE telegram_targets")
                conn.execute("ALTER TABLE _tg_targets_v2 RENAME TO telegram_targets")
                conn.commit()
                print("[TG-DB] Migration v2 完成：UNIQUE(chat_id) → UNIQUE(chat_id, target_type)")
        except Exception as e:
            print(f"[TG-DB] Migration v2 失敗（忽略）：{e}")
        # 若 DB 為空，從 .env TELEGRAM_RECIPIENTS 種子
        count = conn.execute("SELECT COUNT(*) FROM telegram_targets").fetchone()[0]
        if count == 0:
            recipients = _get_tg_recipients()
            now = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
            for r in recipients:
                cid  = str(r.get("chatId") or r.get("chat_id") or "").strip()
                name = str(r.get("name", "") or cid).strip()
                if cid:
                    conn.execute(
                        "INSERT OR IGNORE INTO telegram_targets (chat_id, name, enabled, target_type, created_at, updated_at) VALUES (?,?,1,'stock',?,?)",
                        (cid, name, now, now),
                    )
            conn.commit()
    finally:
        conn.close()

def _get_tg_db_targets(enabled_only: bool = False) -> list:
    conn = _get_tg_db_conn()
    try:
        q = "SELECT * FROM telegram_targets"
        if enabled_only:
            q += " WHERE enabled=1"
        q += " ORDER BY id ASC"
        rows = conn.execute(q).fetchall()
        return [dict(r) for r in rows]
    except Exception as e:
        print(f"[TG-DB] 讀取目標失敗：{e}")
        return []
    finally:
        conn.close()

def get_telegram_targets(target_type: str) -> list:
    """取得指定推送類型的啟用目標。
    stock     → target_type IN ('stock', 'all')
    amplitude → target_type IN ('amplitude', 'all')
    """
    conn = _get_tg_db_conn()
    try:
        rows = conn.execute(
            "SELECT * FROM telegram_targets WHERE enabled=1 AND target_type IN (?, 'all') ORDER BY id ASC",
            (target_type,)
        ).fetchall()
        return [dict(r) for r in rows]
    except Exception as e:
        print(f"[TG-DB] get_telegram_targets({target_type}) 失敗：{e}")
        return []
    finally:
        conn.close()

def _send_tg_with_targets(message: str, targets: list, parse_mode: str = "HTML") -> dict:
    """廣播訊息給指定的目標列表（自動分段）"""
    token = os.environ.get("TELEGRAM_BOT_TOKEN", "")
    if not token:
        return {"ok": 0, "fail": 0, "errors": ["尚未設定 Bot Token"]}
    if not targets:
        return {"ok": 0, "fail": 0, "errors": ["無目標收件人"]}
    url   = f"https://api.telegram.org/bot{token}/sendMessage"
    parts = _split_tg_message(message)
    ok_count, fail_count, errors = 0, 0, []
    for t in targets:
        chat_id = str(t.get("chat_id") or t.get("chatId") or "").strip()
        if not chat_id:
            continue
        recipient_ok = True
        for part in parts:
            success, err = _tg_post(url, chat_id, part, parse_mode)
            if not success:
                recipient_ok = False
                errors.append(f"{t.get('name', '?')}：{err}")
                break
        if recipient_ok:
            ok_count += 1
        else:
            fail_count += 1
    return {"ok": ok_count, "fail": fail_count, "errors": errors}

def is_tw_market_trading_day(dt=None) -> bool:
    """判斷是否為台股交易日（初版：排除週六週日）"""
    if dt is None:
        dt = datetime.now()
    return dt.weekday() < 5

def calculate_candle_risk(kbar: dict) -> dict:
    """計算當日 K 線風險：長上影、收盤位置、衝高收低。"""
    high  = kbar.get("high", 0) or 0
    low   = kbar.get("low", 0) or 0
    open_ = kbar.get("open", 0) or 0
    close = kbar.get("close", 0) or 0

    range_ = high - low
    if range_ <= 0:
        return {
            "upper_shadow_ratio": 0.0,
            "close_position": 0.5,
            "is_long_upper_shadow": False,
            "is_close_near_low": False,
            "is_intraday_reversal": False,
            "candle_warning": "",
        }

    upper_shadow      = high - max(open_, close)
    upper_shadow_ratio = upper_shadow / range_
    close_position    = (close - low) / range_

    is_long_upper_shadow  = upper_shadow_ratio > 0.4
    is_close_near_low     = close_position < 0.35
    is_intraday_reversal  = (high > close * 1.04) and (close_position < 0.4)

    warnings = []
    if is_long_upper_shadow and is_close_near_low:
        warnings.append("當日長上影且收盤靠近低點，需複查是否追價失敗")
    elif is_long_upper_shadow:
        warnings.append("當日上影線偏長，隔日需確認不再轉弱")
    if is_intraday_reversal and not warnings:
        warnings.append("當日衝高收低，需複查買盤承接力道")

    return {
        "upper_shadow_ratio":    round(upper_shadow_ratio, 3),
        "close_position":        round(close_position, 3),
        "is_long_upper_shadow":  is_long_upper_shadow,
        "is_close_near_low":     is_close_near_low,
        "is_intraday_reversal":  is_intraday_reversal,
        "candle_warning":        warnings[0] if warnings else "",
    }


def apply_tg_downgrade_rules(stock: dict) -> dict:
    """
    根據 K 線風險、風報比、停損距離決定是否從精選降到備選。

    降級（到備選）：
      - rr > 8 且（長上影 or 衝高收低）
      - 長上影 且 收盤靠低 且 rr > 5  ← 中高風報比才降
      - 單純衝高收低 且 rr > 5

    警告（留精選加 ⚠️）：
      - rr > 8（無顯著 K 線問題）
      - 長上影（其他條件尚可）
      - 長上影 且 收盤靠低 但 rr ≤ 5（停損小、報酬合理時保留精選）
    """
    s = dict(stock)
    rr     = s.get("risk_reward") or 0
    kbar   = {"open": s.get("open_price", 0), "high": s.get("high_price", 0),
              "low":  s.get("low_price", 0),  "close": s.get("close", 0)}
    risk   = calculate_candle_risk(kbar)
    s["candle_risk"] = risk

    reasons   = []
    downgrade = False
    warn_only = False

    if rr > 8 and (risk["is_long_upper_shadow"] or risk["is_intraday_reversal"]):
        downgrade = True
        reasons.append("風報比偏高且當日長上影，從精選降到備選")
    elif risk["is_long_upper_shadow"] and risk["is_close_near_low"] and rr > 5:
        downgrade = True
        reasons.append("長上影且收盤靠近低點，降到備選")
    elif risk["is_intraday_reversal"] and rr > 5:
        downgrade = True
        reasons.append("衝高收低，降到備選")
    elif rr > 8:
        warn_only = True
        reasons.append("風報比偏高，需複查停損與目標")
    elif risk["is_long_upper_shadow"] and risk["is_close_near_low"]:
        warn_only = True
        reasons.append("當日長上影且收盤靠近低點，需複查是否追價失敗")
    elif risk["is_long_upper_shadow"]:
        warn_only = True
        reasons.append("當日上影線偏長，隔日需確認不再轉弱")
    elif risk["candle_warning"]:
        warn_only = True
        reasons.append(risk["candle_warning"])

    s["tg_downgraded"]    = downgrade
    s["tg_warn_only"]     = warn_only
    s["downgrade_reason"] = reasons[0] if reasons else ""
    s["tg_warning"]       = reasons[0] if reasons else (s.get("tg_warning") or "")
    return s


def get_tg_pick_concentrated_industries(tg_picks: list, tg_watch: list) -> list:
    """從 TG 精選與備選股票中統計產業集中度（同產業 >= 2 檔才列出）。"""
    from collections import defaultdict
    counter: dict = defaultdict(lambda: {"pick_count": 0, "watch_count": 0, "stocks": []})
    for s in tg_picks:
        ind = _resolve_industry_name(s.get("industry") or "未分類")
        counter[ind]["pick_count"] += 1
        counter[ind]["stocks"].append(f"{s['stock_id']} {s['stock_name']}")
    for s in tg_watch:
        ind = _resolve_industry_name(s.get("industry") or "未分類")
        counter[ind]["watch_count"] += 1
        counter[ind]["stocks"].append(f"{s['stock_id']} {s['stock_name']}")

    result = []
    for ind, data in counter.items():
        total = data["pick_count"] + data["watch_count"]
        if total < 2:
            continue
        result.append({
            "industry":            ind,
            "pick_count":          data["pick_count"],
            "watch_count":         data["watch_count"],
            "representative_stocks": data["stocks"][:5],
        })
    result.sort(key=lambda x: -(x["pick_count"] * 2 + x["watch_count"]))
    return result


def validate_screener_data_date(data_date: str) -> dict:
    """
    驗證整合選股所需資料是否同步到同一個 data_date。
    直接查 DB，不依賴策略執行結果。
    """
    data_date = normalize_date(data_date) if data_date else ""
    warnings, errors = [], []

    stock_kbar_date = ""
    try:
        conn = sqlite3.connect(_STOCK_DB_PATH, timeout=30.0)
        row = conn.execute("SELECT MAX(date) FROM daily_kbars").fetchone()
        conn.close()
        if row and row[0]:
            stock_kbar_date = normalize_date(str(row[0]))
    except Exception as e:
        errors.append(f"無法查詢個股日K最新日期: {e}")

    market_data_date = ""
    market_close = 0.0
    try:
        conn = sqlite3.connect(_STOCK_DB_PATH, timeout=30.0)
        row = conn.execute(
            "SELECT date, close FROM market_index_daily ORDER BY date DESC LIMIT 1"
        ).fetchone()
        conn.close()
        if row:
            market_data_date = normalize_date(str(row[0]))
            market_close = float(row[1] or 0)
    except Exception as e:
        errors.append(f"無法查詢大盤日K最新日期: {e}")

    institution_data_date = ""
    try:
        conn = sqlite3.connect(_STOCK_DB_PATH, timeout=30.0)
        row = conn.execute("SELECT MAX(date) FROM institutional_trading").fetchone()
        conn.close()
        if row and row[0]:
            institution_data_date = normalize_date(str(row[0]))
    except Exception as e:
        warnings.append(f"無法查詢法人資料最新日期: {e}")

    industry_data_ok = True
    try:
        conn = sqlite3.connect(_STOCK_DB_PATH, timeout=30.0)
        count = conn.execute(
            "SELECT COUNT(*) FROM stock_names WHERE name IS NOT NULL AND name != ''"
        ).fetchone()[0]
        conn.close()
        if count < 100:
            warnings.append(f"股票基本資料不足（{count} 筆）")
            industry_data_ok = False
    except Exception as e:
        warnings.append(f"無法查詢股票基本資料: {e}")

    if data_date:
        if stock_kbar_date and stock_kbar_date != data_date:
            errors.append(f"個股日K最新日期 {stock_kbar_date} ≠ 選股基準日 {data_date}")
        if market_data_date and market_data_date != data_date:
            errors.append(f"大盤資料日 {market_data_date} ≠ 選股基準日 {data_date}")
        if institution_data_date and institution_data_date != data_date:
            warnings.append(f"法人資料日 {institution_data_date} ≠ 選股基準日 {data_date}")

    critical_ok = len(errors) == 0
    return {
        "data_date":             data_date,
        "stock_kbar_date":       stock_kbar_date,
        "market_data_date":      market_data_date,
        "market_close":          market_close,
        "institution_data_date": institution_data_date,
        "industry_data_ok":      industry_data_ok,
        "critical_ok":           critical_ok,
        "warnings":              warnings,
        "errors":                errors,
    }


def validate_result_data_date(integrated_result: dict) -> dict:
    """
    檢查選股結果、大盤資料是否使用同一個 data_date。
    回傳 {valid, critical_ok, data_date, stock_kbar_date, market_data_date, ...}。
    """
    data_date   = integrated_result.get("data_date", "")
    mr          = integrated_result.get("market_regime", {}) or {}
    market_date = mr.get("data_date", "")
    mr_metrics  = mr.get("metrics") or {}
    mr_error    = mr_metrics.get("regime_error", False) or (not mr_metrics.get("data_available", True))
    # Only trust index_close when there is no data error
    taiex_close = (mr_metrics.get("index_close") or 0) if not mr_error else None
    mr_status   = "資料異常" if mr_error else mr.get("status", "")

    warnings, errors = [], []

    if not data_date:
        errors.append("data_date 缺失，無法驗證資料一致性")

    if mr_error:
        errors.append(
            f"大盤資料日期 {(mr.get('metrics') or {}).get('actual_data_date', '未知')} "
            f"≠ 要求日期 {(mr.get('metrics') or {}).get('expected_data_date', data_date)}，大盤狀態計算已拒絕"
        )
    elif market_date and data_date and market_date != data_date:
        errors.append(f"大盤資料日期 {market_date} ≠ 選股資料日期 {data_date}")

    # 法人資料日期（從 DB 查詢）
    institution_data_date = ""
    try:
        conn = sqlite3.connect(_STOCK_DB_PATH, timeout=30.0)
        row = conn.execute("SELECT MAX(date) FROM institutional_trading").fetchone()
        conn.close()
        if row and row[0]:
            institution_data_date = normalize_date(str(row[0]))
            if data_date and institution_data_date != data_date:
                warnings.append(f"法人資料日 {institution_data_date} ≠ 選股基準日 {data_date}")
    except Exception:
        pass

    valid = critical_ok = (len(errors) == 0)

    print(
        f"[日期檢查] data_date={data_date}, stock_kbar_date={data_date}, "
        f"market_data_date={market_date}, institution_data_date={institution_data_date}, "
        f"critical_ok={critical_ok}"
    )
    for w in warnings:
        print(f"[日期檢查] WARNING: {w}")
    for e in errors:
        print(f"[日期檢查] ERROR: {e}")

    return {
        "valid":                 valid,
        "critical_ok":           critical_ok,
        "data_date":             data_date,
        "stock_kbar_date":       data_date,
        "market_data_date":      market_date,
        "taiex_close":           taiex_close,
        "market_close":          taiex_close,
        "market_regime":         mr_status,
        "market_regime_success": not mr_error,
        "regime_error":          mr_error,
        "institution_data_date": institution_data_date,
        "warnings":              warnings,
        "errors":                errors,
        "data_validation": {
            "critical_ok": critical_ok,
            "warnings":    warnings,
            "errors":      errors,
        },
    }


def calculate_tg_score(stock: dict) -> float:
    """計算 TG 精選排序分數（0~100）"""
    score = 0.0

    sl_abs = abs(stock.get("stop_loss_pct") or 0)
    if sl_abs <= 2:
        score += 25
    elif sl_abs <= 3:
        score += 20
    elif sl_abs <= 4:
        score += 15
    else:
        score += 5

    dist = abs(stock.get("dist_cost20_pct") or 0)
    if dist <= 1:
        score += 20
    elif dist <= 2:
        score += 17
    elif dist <= 3:
        score += 12
    else:
        score += 4

    rr = stock.get("risk_reward") or 0
    if 2 <= rr <= 5:
        score += 20
    elif 1.5 <= rr < 2:
        score += 15
    elif 5 < rr <= 8:
        score += 10
    elif rr > 8:
        score += 5

    macd = stock.get("macd_status", "")
    if macd == "負柱收斂":
        score += 15
    elif macd == "正柱放大":
        score += 10
    elif macd in ("正柱收斂", "正柱"):
        score += 5

    trust   = stock.get("trust_5d", 0) or 0
    foreign = stock.get("foreign_5d", 0) or 0
    tc      = stock.get("trust_consecutive", 0) or 0
    fc      = stock.get("foreign_consecutive", 0) or 0
    if trust > 0 and foreign > 0:
        score += 10
    elif trust > 0 or foreign > 0:
        score += 6
    if tc >= 3:
        score += 3
    if fc >= 3:
        score += 2

    if stock.get("has_industry_resonance"):
        score += 5
    elif (stock.get("industry_score") or 0) >= 80:
        score += 3
    elif (stock.get("industry_score") or 0) >= 60:
        score += 1

    score += (stock.get("final_score") or 0) * 0.05
    return round(min(100.0, score), 1)


def build_tg_pick_list(integrated_result: dict) -> dict:
    """從整合選股結果中挑選 TG 精選（最多3）與備選（最多2），含 K 線風險降級。"""
    buy = integrated_result.get("buy_candidates", [])
    tg_picks_pre, tg_watch_pre, tg_skipped = [], [], []
    downgraded: list = []

    for s in buy:
        grade  = s.get("stock_grade", "")
        dist   = abs(s.get("dist_cost20_pct") or 999)
        sl_abs = abs(s.get("stop_loss_pct") or 0)
        rr     = s.get("risk_reward") or 0
        macd   = s.get("macd_status", "")

        if grade != "A" or dist > 3 or rr < 1.5 or sl_abs > 5.5 or macd == "負柱擴大":
            tg_skipped.append(s)
            continue

        tg_score = calculate_tg_score(s)
        s_copy   = apply_tg_downgrade_rules(dict(s))
        s_copy["tg_score"] = tg_score

        if sl_abs <= 4 and macd in ("負柱收斂", "正柱放大"):
            tg_picks_pre.append(s_copy)
        else:
            tg_watch_pre.append(s_copy)

    tg_picks_pre.sort(key=lambda x: -x.get("tg_score", 0))
    tg_watch_pre.sort(key=lambda x: -x.get("tg_score", 0))

    # 降級：將 tg_downgraded=True 的精選股移到備選
    tg_picks_final, tg_watch_final = [], []
    for s in tg_picks_pre:
        if s.get("tg_downgraded"):
            downgraded.append({
                "stock_id": s.get("stock_id", ""),
                "reason":   s.get("downgrade_reason", ""),
            })
            tg_watch_final.append(s)
            print(f"[TG 降級] {s.get('stock_id')} {s.get('stock_name')} downgraded: {s.get('downgrade_reason')}")
        else:
            tg_picks_final.append(s)

    # 備選中 downgraded 的放後面，原本備選中 warn_only 也加入
    for s in tg_watch_pre:
        if s.get("tg_downgraded"):
            downgraded.append({"stock_id": s.get("stock_id", ""), "reason": s.get("downgrade_reason", "")})
        tg_watch_final.append(s)
        if s.get("tg_downgraded"):
            print(f"[TG 降級] {s.get('stock_id')} {s.get('stock_name')} (備選) downgraded: {s.get('downgrade_reason')}")
        elif s.get("tg_warn_only"):
            print(f"[TG 警告] {s.get('stock_id')} {s.get('stock_name')} warning: {s.get('tg_warning')}")

    for s in tg_picks_final:
        if s.get("tg_warn_only"):
            print(f"[TG 警告] {s.get('stock_id')} {s.get('stock_name')} warning: {s.get('tg_warning')}")

    result_picks = tg_picks_final[:3]
    result_watch = tg_watch_final[:2]

    print(f"[TG 結果] tg_picks={len(result_picks)}, tg_watch={len(result_watch)}")

    return {
        "tg_picks":        result_picks,
        "tg_watch":        result_watch,
        "tg_skipped":      tg_skipped,
        "downgraded":      downgraded,
        "downgrade_count": len(downgraded),
    }


def get_resonance_industries(integrated_result: dict) -> list:
    """
    法人技術共振產業：分數≥60，最多5名。
    每項加 is_strong=True（分數≥70）或 False（60-69，中性觀察）。
    """
    from collections import defaultdict
    all_stocks = (
        integrated_result.get("buy_candidates", []) +
        integrated_result.get("high_priority_watch", []) +
        integrated_result.get("wait_pullback", []) +
        integrated_result.get("other_watch", [])
    )
    groups: dict = defaultdict(list)
    for s in all_stocks:
        ind_raw = (s.get("industry") or "").strip() or "其他"
        ind = _resolve_industry_name(ind_raw)
        groups[ind].append(s)

    rankings = []
    for ind_name, stocks in groups.items():
        if not stocks:
            continue
        ind_score  = stocks[0].get("industry_score", 0) or 0
        raw_status = stocks[0].get("industry_status", "") or ""
        if ind_score < 60:
            continue
        if raw_status == "過熱警戒":
            display_status = "過熱警戒"
        elif ind_score >= 80:
            display_status = "資金共振強"
        elif ind_score >= 70:
            display_status = "偏強觀察"
        else:
            display_status = "中性觀察"
        buy_stocks = [s for s in stocks if s.get("final_category") == "buy_candidates"]
        top_stocks = [f"{s['stock_id']} {s['stock_name']}" for s in buy_stocks[:3]]
        rankings.append({
            "rank":            0,
            "industry":        ind_name,
            "score":           ind_score,
            "status":          display_status,
            "is_strong":       ind_score >= 70,
            "candidate_count": len(stocks),
            "top_stocks":      top_stocks,
        })

    rankings.sort(key=lambda x: -x["score"])
    for i, r in enumerate(rankings, 1):
        r["rank"] = i
    return rankings[:5]


_TWSE_INDUSTRY_CODE_MAP: dict = {
    "1": "水泥", "2": "食品", "3": "塑膠", "4": "紡織纖維",
    "5": "電機機械", "6": "電器電纜", "8": "化學生技醫療",
    "9": "玻璃陶瓷", "10": "造紙", "11": "鋼鐵", "12": "橡膠",
    "13": "汽車", "14": "電子工業", "15": "建材營造", "16": "航運業",
    "17": "觀光旅遊", "18": "金融保險", "19": "貿易百貨", "20": "其他",
    "21": "化工", "22": "生技醫療業", "23": "油電燃氣業",
    "24": "半導體業", "25": "電腦及週邊設備業", "26": "光電業",
    "27": "通信網路業", "28": "電子零組件業", "29": "電子通路業",
    "30": "資訊服務業", "31": "其他電子業", "32": "文化創意業",
    "33": "農業科技業", "34": "電子商務業", "35": "綠能環保",
    "36": "數位雲端", "37": "運動休閒", "38": "居家生活",
    "39": "電動車", "80": "國內ETF", "81": "境外ETF",
}


def _resolve_industry_name(raw: str) -> str:
    """將數字型產業代碼轉為中文名，找不到則回傳「其他未分類族群」。"""
    if not raw or not raw.isdigit():
        return raw or "其他"
    return _TWSE_INDUSTRY_CODE_MAP.get(raw, "其他未分類族群")


def get_industry_daily_stats_from_db() -> tuple:
    """
    從 stock_cache.db daily_kbars + stock_names 計算全市場各類股今日漲跌統計。
    Returns (stats_dict, data_date_str, source_str)
      stats_dict: { industry: {avg_change_pct, stock_count, vol_surge_ratio, rep_stocks} }
    """
    from collections import defaultdict
    _db = os.path.join(os.path.dirname(os.path.abspath(__file__)), "stock_cache.db")
    try:
        conn = sqlite3.connect(_db, timeout=30.0)
        conn.row_factory = sqlite3.Row
        # 取最後3個交易日，確保算出「前一日收盤」
        df = pd.read_sql_query(
            """
            SELECT k.code, k.date, k.close, k.volume,
                   n.name, n.category
            FROM daily_kbars k
            LEFT JOIN stock_names n ON k.code = n.code
            WHERE k.date IN (
                SELECT DISTINCT date FROM daily_kbars ORDER BY date DESC LIMIT 22
            )
            ORDER BY k.code, k.date ASC
            """,
            conn,
        )
        conn.close()
    except Exception as e:
        print(f"[盤面族群DB] 讀取失敗: {e}")
        return {}, "", f"DB 讀取失敗：{e}"

    if df.empty:
        return {}, "", "DB 無資料"

    dates = sorted(df["date"].unique())
    if len(dates) < 2:
        return {}, "", f"日期不足（僅 {len(dates)} 個交易日）"

    today_date = dates[-1]
    prev_date  = dates[-2]

    today_df = df[df["date"] == today_date]
    prev_df  = df[df["date"] == prev_date]

    prev_close_map = dict(zip(prev_df["code"].astype(str), prev_df["close"].astype(float)))

    # 近20日均量（使用整個查詢期間）
    vol_ma_map: dict = {}
    for code, grp in df.groupby("code"):
        vols = grp["volume"].astype(float).tail(20)
        if len(vols) >= 3:
            vol_ma_map[str(code)] = float(vols.mean())

    groups: dict = defaultdict(list)
    for _, row in today_df.iterrows():
        code = str(row["code"])
        prev_c = prev_close_map.get(code)
        if not prev_c or prev_c <= 0:
            continue
        chg = (float(row["close"]) - prev_c) / prev_c * 100
        ind_raw = (str(row["category"] or "") or "其他").strip() or "其他"
        ind = _resolve_industry_name(ind_raw)
        name = str(row["name"] or code)
        vol_now  = float(row["volume"] or 0)
        vol_ma20 = vol_ma_map.get(code, 0)
        groups[ind].append({
            "code": code, "name": name, "change_pct": chg,
            "vol_surge": (vol_now > vol_ma20 * 1.3) if vol_ma20 > 0 else False,
        })

    stats: dict = {}
    for ind, stocks in groups.items():
        if len(stocks) < 2:
            continue
        avg_chg = sum(s["change_pct"] for s in stocks) / len(stocks)
        if avg_chg <= 0:
            continue
        surge_count = sum(1 for s in stocks if s["vol_surge"])
        top3 = sorted(stocks, key=lambda x: -x["change_pct"])[:3]
        stats[ind] = {
            "avg_change_pct":  round(avg_chg, 2),
            "stock_count":     len(stocks),
            "vol_surge_ratio": surge_count / len(stocks),
            "rep_stocks":      [f"{s['code']} {s['name']}" for s in top3],
        }

    source = f"DB 全市場日K {today_date}（{len(today_df)} 檔）"
    print(f"[盤面族群DB] {source}，計算出 {len(stats)} 個產業")
    return stats, today_date, source


def get_market_hot_industries(integrated_result: dict) -> list:
    """
    今日盤面強勢族群，最多5名。
    優先：DB 全市場日 K（涵蓋所有股票，不受選股名單限制）。
    Fallback：TWSE 今日報價 × 選股名單（資料來源受限，僅反映選股範圍內族群）。
    """
    from collections import defaultdict

    def _status(avg_chg: float, vol_surge_ratio: float) -> str:
        if avg_chg >= 3.0:              return "盤面偏強"
        if vol_surge_ratio >= 0.5:      return "成交放大"
        if avg_chg > 1.0 and vol_surge_ratio >= 0.3: return "資金活躍"
        return "題材延續"

    # ── 優先：DB 全市場計算 ──────────────────────────────────────────────────
    stats, db_date, source = get_industry_daily_stats_from_db()
    if stats:
        results = []
        for ind_name, data in stats.items():
            results.append({
                "rank":           0,
                "industry":       ind_name,
                "avg_change_pct": data["avg_change_pct"],
                "status":         _status(data["avg_change_pct"], data["vol_surge_ratio"]),
                "rep_stocks":     data["rep_stocks"],
                "source":         source,
            })
        results.sort(key=lambda x: -x["avg_change_pct"])
        for i, r in enumerate(results, 1):
            r["rank"] = i
        print(f"[TG 族群] DB全市場 industries={len(results)}, source={source}")
        return results[:5]

    # ── Fallback：TWSE 報價 × 選股名單 ───────────────────────────────────────
    print(f"[TG 族群] DB 計算失敗（{source}），fallback 到 TWSE 報價×選股名單")
    all_stocks = (
        integrated_result.get("buy_candidates", []) +
        integrated_result.get("high_priority_watch", []) +
        integrated_result.get("wait_pullback", []) +
        integrated_result.get("other_watch", [])
    )
    if not all_stocks:
        return []
    try:
        daily_quotes, _ = screener.fetch_twse_daily_quotes()
    except Exception as e:
        print(f"[TG 族群] TWSE 報價抓取失敗：{e}")
        return []

    groups: dict = defaultdict(list)
    for s in all_stocks:
        ind = (s.get("industry") or "").strip() or "其他"
        groups[ind].append(s)

    results = []
    for ind_name, stocks in groups.items():
        chg_entries = []
        vol_surge   = 0
        for s in stocks:
            q = daily_quotes.get(s.get("stock_id", ""))
            if not q:
                continue
            chg       = q.get("change_pct", 0) or 0
            amt_today = q.get("turnover", 0) or 0
            amt_ma20  = s.get("amount_ma20", 0) or 0
            chg_entries.append((chg, s.get("stock_id", ""), s.get("stock_name", "")))
            if amt_ma20 > 0 and amt_today > amt_ma20 * 1.3:
                vol_surge += 1
        if not chg_entries:
            continue
        avg_chg         = sum(c[0] for c in chg_entries) / len(chg_entries)
        vol_surge_ratio = vol_surge / len(chg_entries)
        if avg_chg <= 0:
            continue
        chg_entries.sort(key=lambda x: -x[0])
        results.append({
            "rank":           0,
            "industry":       ind_name,
            "avg_change_pct": round(avg_chg, 2),
            "status":         _status(avg_chg, vol_surge_ratio),
            "rep_stocks":     [f"{c[1]} {c[2]}" for c in chg_entries[:3]],
            "source":         "TWSE 今日報價（選股範圍）",
        })

    results.sort(key=lambda x: -x["avg_change_pct"])
    for i, r in enumerate(results, 1):
        r["rank"] = i
    return results[:5]


def _md_escape(text: str) -> str:
    """Escape HTML special characters for Telegram HTML parse mode."""
    return str(text).replace("&", "&amp;").replace("<", "&lt;").replace(">", "&gt;")




def _moneydj_tg_period_text(period: str) -> str:
    p = str(period or "5D").upper().strip()
    return {
        "1D": "近1日",
        "5D": "近5日",
        "10D": "近10日",
        "20D": "近20日",
    }.get(p, p or "近5日")


def _moneydj_tg_risk_text(risk: str) -> str:
    key = str(risk or "").strip()
    return {
        "stale_data": "資料日期不一致",
        "broker_sell_pressure": "分點賣壓 / 多空分歧",
        "broker_accumulation": "分點偏多",
        "broker_daytrade": "疑似隔日沖",
        "broker_distributed": "買盤分散",
    }.get(key, "無明顯風險")


def _moneydj_tg_score_label(broker_bonus) -> str:
    try:
        bonus_val = float(broker_bonus or 0)
    except Exception:
        bonus_val = 0.0

    if bonus_val > 0:
        score_text = f"+{int(bonus_val) if bonus_val.is_integer() else bonus_val:g}"
    elif bonus_val < 0:
        score_text = f"{int(bonus_val) if bonus_val.is_integer() else bonus_val:g}"
    else:
        score_text = "0"

    if bonus_val >= 6:
        label = "🟢 強多"
    elif bonus_val >= 2:
        label = "🟢 偏多"
    elif -1 <= bonus_val <= 1:
        label = "⚪ 中性"
    elif -3 <= bonus_val <= -2:
        label = "🟠 偏空"
    else:
        label = "🔴 賣壓"
    return f"{label}（{score_text}）"

def _moneydj_tg_reason_text(stock: dict) -> str:
    if not stock or stock.get("moneydj_date_valid") is not True:
        return ""
    key = str(stock.get("broker_risk") or "").strip()
    if key == "stale_data":
        return ""
    reason = {
        "broker_sell_pressure": "買賣雙方力量接近，偏多空分歧 / 換手",
        "broker_accumulation": "買盤集中，籌碼偏多",
        "broker_daytrade": "疑似短線隔日沖，避免追價",
        "broker_distributed": "買盤分散，主力結構不明",
    }.get(key, "無明顯分點風險")
    return reason if len(reason) <= 36 else ""

def _format_moneydj_tg_line(stock: dict) -> list[str]:
    if not stock or stock.get("moneydj_date_valid") is not True:
        return []
    if str(stock.get("broker_risk") or "").strip() == "stale_data":
        return []

    score_label = _moneydj_tg_score_label(stock.get("broker_bonus"))
    period_text = _moneydj_tg_period_text(stock.get("moneydj_period_label"))
    risk_text = _moneydj_tg_risk_text(stock.get("broker_risk"))
    lines = [f"\u5206\u9ede\u5224\u5b9a\uff1a{score_label}\uff5c{period_text}\uff5c{risk_text}"]

    reason = _moneydj_tg_reason_text(stock)
    if reason:
        lines.append(f"\u539f\u56e0\uff1a{_md_escape(reason)}")
    return lines

def format_tg_integrated_message(
    data_date: str,
    market_regime: dict,
    tg_list: dict,
    hot_industries: list,
    resonance_industries: list,
    is_test_mode: bool = False,
) -> str:
    """組成整合選股 TG 訊息（精選≤3、備選≤2）。"""
    now_str   = datetime.now().strftime("%Y-%m-%d %H:%M")
    mr        = market_regime or {}
    mr_label  = mr.get("label", "正常多頭")
    mr_status = mr.get("status", "normal_bull")
    mr_close  = mr.get("index_close") or mr.get("metrics", {}).get("index_close") or 0
    mr_date   = mr.get("data_date", "")
    mr_emoji  = {
        "strong_bull": "🟢", "healthy_pullback": "🟢",
        "high_overheated": "🟡", "weak_bounce": "🟠", "bear_break60": "🔴",
    }.get(mr_status, "📊")

    tg_picks = tg_list.get("tg_picks", [])
    tg_watch = tg_list.get("tg_watch", [])
    SEP      = "─" * 28

    # 若大盤是「強多延伸」但當日收盤下跌或靠近低點，改顯示「強多回測」
    if mr_status == "strong_bull":
        _m = mr.get("metrics") or {}
        if _m.get("market_day_declining") or _m.get("market_close_near_low"):
            mr_label = "強多回測"

    date_mismatch = bool(mr_date and data_date and mr_date != data_date)

    # 1. 標題（使用 data_date，不是推送時間）
    title_date = data_date if data_date else "未知"
    if is_test_mode:
        lines = [f"🔴 *【測試訊息，不可作為正式推送】*"]
        lines.append(f"📌 *明日精選股｜資料日 {title_date}*")
    else:
        lines = [f"📌 *明日精選股｜資料日 {title_date}*"]
    if data_date:
        lines.append(f"📅 選股基準日：{data_date} 收盤")
    else:
        lines.append("📅 選股基準日：未知，請檢查資料同步狀態")
        print("[日期檢查] WARNING: data_date 缺失，無法確認資料基準日")
    # 若大盤資料日期與選股基準日不一致，顯示警告
    if date_mismatch:
        lines.append(f"⚠️ 大盤資料日 {mr_date} ≠ 選股基準日 {data_date}，請確認資料同步")
        print(f"[日期檢查] WARNING: mr_date={mr_date} ≠ data_date={data_date}")
    lines.append(f"🕐 推送時間：{now_str}")
    lines.append(SEP)

    # 2. 大盤狀態 + 操作原則
    lines.append(f"大盤狀態：{mr_emoji} {mr_label}")
    if mr_close:
        date_label = f"（{mr_date}）" if mr_date else ""
        lines.append(f"大盤收盤：{mr_close:,.2f}{date_label}")
    if mr_status == "strong_bull":
        lines.append("操作原則：A級、距cost20≤3%、停損≤4%、MACD收斂或正柱放大；強多延伸不追高，優先等回測不破或開盤轉強確認。")
    else:
        lines.append("操作原則：A級、距cost20≤3%、停損≤4%、MACD收斂或正柱放大")
    lines.append(SEP)

    # 3. 今日盤面強勢族群
    hot_source = hot_industries[0].get("source", "") if hot_industries else ""
    lines.append("🏭 *今日盤面強勢族群*")
    if hot_industries:
        for ind in hot_industries:
            rep = "、".join(_md_escape(r) for r in ind["rep_stocks"][:3]) if ind.get("rep_stocks") else ""
            sign = "+" if ind["avg_change_pct"] >= 0 else ""
            lines.append(f"{_md_escape(ind['industry'])}｜漲幅 {sign}{ind['avg_change_pct']:.2f}%｜{_md_escape(ind['status'])}")
            if rep:
                lines.append(f"   代表：{rep}")
        if hot_source:
            lines.append(f"（資料來源：{_md_escape(hot_source)}）")
    else:
        lines.append("盤面強勢族群資料不足")
    print(f"[TG 族群] market_strength_groups_available={bool(hot_industries)}, source={hot_source or '無'}")

    # 4. 明日精選集中族群
    concentrated = get_tg_pick_concentrated_industries(tg_picks, tg_watch)
    lines.append(f"\n📌 *明日精選集中族群*")
    if concentrated:
        for c in concentrated:
            reps = "、".join(_md_escape(r) for r in c["representative_stocks"][:5])
            lines.append(f"{_md_escape(c['industry'])}｜精選 {c['pick_count']} 檔｜備選 {c['watch_count']} 檔｜代表：{reps}")
        print(f"[TG 族群] concentrated_industries={'、'.join(c['industry'] for c in concentrated)}")
    else:
        lines.append("今日精選股無明顯產業集中")
    lines.append(SEP)

    # 5. 法人技術共振產業（分層顯示）
    lines.append("🧭 *法人技術共振產業*")
    strong_inds  = [x for x in resonance_industries if x.get("is_strong")]
    neutral_inds = [x for x in resonance_industries if not x.get("is_strong")]
    if strong_inds:
        for ind in strong_inds:
            top = "、".join(_md_escape(t) for t in ind["top_stocks"][:2]) if ind.get("top_stocks") else ""
            lines.append(f"{ind['rank']}. {_md_escape(ind['industry'])}｜分數 {ind['score']}｜{_md_escape(ind['status'])}｜候選 {ind['candidate_count']} 檔")
            if top:
                lines.append(f"   代表：{top}")
    else:
        lines.append("今日無明顯強共振產業")
    if neutral_inds:
        for ind in neutral_inds:
            top = "、".join(_md_escape(t) for t in ind["top_stocks"][:2]) if ind.get("top_stocks") else ""
            lines.append(f"中性觀察：{_md_escape(ind['industry'])}｜分數 {ind['score']}｜候選 {ind['candidate_count']} 檔")
            if top:
                lines.append(f"   代表：{top}")
    lines.append("註：法人技術共振產業為產業層級觀察，明日精選仍以個股等級、成本線、停損與MACD條件排序。")
    lines.append(SEP)

    # 6. 明日精選
    if tg_picks:
        lines.append(f"🔥 *明日精選 {len(tg_picks)} 檔*")
        num_emojis = ["1️⃣", "2️⃣", "3️⃣"]
        for i, s in enumerate(tg_picks):
            industry = _md_escape(s.get("industry") or "未分類")
            close_p  = s.get("close", 0)
            dist     = s.get("dist_cost20_pct") or 0
            sl_price = s.get("stop_price", 0)
            sl_pct   = s.get("stop_loss_pct") or 0
            rr       = s.get("risk_reward") or 0
            macd     = _md_escape(s.get("macd_status", ""))
            inst_sum = _md_escape(s.get("institution_5d_status", ""))
            warning  = _md_escape(s.get("tg_warning", ""))
            sname    = _md_escape(s.get("stock_name", ""))
            num      = num_emojis[i] if i < 3 else f"{i+1}."
            lines.append(f"\n{num} *{s['stock_id']} {sname}*｜{industry}")
            lines.append(f"現價 {close_p}｜距cost20 {'+' if dist>=0 else ''}{dist:.1f}%｜停損 {sl_price}（{sl_pct:.1f}%）")
            lines.append(f"MACD：{macd}｜風報比 {rr:.1f}｜法人：{inst_sum}")
            for moneydj_line in _format_moneydj_tg_line(s):
                lines.append(moneydj_line)
            dist_sign = "+" if dist >= 0 else ""
            _candle_risk = s.get("candle_risk") or {}
            _is_upper_shadow = _candle_risk.get("is_long_upper_shadow", False)
            if _is_upper_shadow:
                lines.append(
                    f"建議：位置接近cost20，停損距離合理，MACD{macd}；"
                    f"但當日長上影，隔日需確認不再跌破低點。"
                )
            elif not warning:
                if macd == "負柱收斂":
                    lines.append(f"建議：位置接近cost20，停損距離小，MACD負柱收斂；隔日不追高，等回測不破或轉強確認。")
                elif macd == "正柱放大":
                    lines.append(f"建議：站在cost20附近，停損距離仍可控，MACD正柱放大；若開高過大不追，等回測守穩。")
                else:
                    lines.append(f"建議：資料面乾淨，距cost20 {dist_sign}{dist:.1f}%，停損{sl_pct:.1f}%，MACD{macd}。")
            else:
                lines.append(f"建議：位置接近cost20，停損距離合理，MACD{macd}。")
            if warning:
                lines.append(f"⚠️ {warning}")
    else:
        lines.append("🔥 今日無符合 TG 精選條件的明日可買")
    lines.append(SEP)

    # 7. 備選（含降級原因）
    if tg_watch:
        lines.append(f"👀 *備選觀察 {len(tg_watch)} 檔*")
        for s in tg_watch:
            industry     = _md_escape(s.get("industry") or "未分類")
            close_p      = s.get("close", 0)
            dist         = s.get("dist_cost20_pct") or 0
            sl_price     = s.get("stop_price", 0)
            sl_pct       = s.get("stop_loss_pct") or 0
            rr           = s.get("risk_reward") or 0
            macd         = _md_escape(s.get("macd_status", ""))
            downgrade_r  = _md_escape((s.get("downgrade_reason") or s.get("final_reason") or "")[:80])
            sname        = _md_escape(s.get("stock_name", ""))
            dist_sign    = "+" if dist >= 0 else ""
            lines.append(f"\n{s['stock_id']} {sname}｜{industry}")
            lines.append(f"現價 {close_p}｜距cost20 {dist_sign}{dist:.1f}%｜停損 {sl_price}（{sl_pct:.1f}%）")
            lines.append(f"MACD：{macd}｜風報比 {rr:.1f}")
            for moneydj_line in _format_moneydj_tg_line(s):
                lines.append(moneydj_line)
            if downgrade_r:
                lines.append(f"原因：{downgrade_r}")
        lines.append(SEP)
    return "\n".join(lines)


@app.get("/api/telegram/config")
async def api_telegram_get_config():
    """讀取目前 Telegram 設定"""
    token = os.environ.get("TELEGRAM_BOT_TOKEN", "")
    masked = ("****" + token[-4:]) if len(token) > 4 else ("****" if token else "")
    recipients = _get_tg_recipients()
    return {"hasToken": bool(token), "maskedToken": masked, "recipients": recipients}

@app.post("/api/telegram/config")
async def api_telegram_save_config(payload: dict = {}):
    """儲存 Bot Token 與收件人清單到 .env"""
    env_path = os.path.join(os.path.dirname(os.path.abspath(__file__)), ".env")
    token = payload.get("botToken", "").strip()
    if token:
        set_key(env_path, "TELEGRAM_BOT_TOKEN", token)
        os.environ["TELEGRAM_BOT_TOKEN"] = token
    recipients = payload.get("recipients", None)
    if recipients is not None:
        raw = json.dumps(recipients, ensure_ascii=False)
        set_key(env_path, "TELEGRAM_RECIPIENTS", raw)
        os.environ["TELEGRAM_RECIPIENTS"] = raw
    return {"status": "ok"}

@app.post("/api/telegram/send")
async def api_telegram_send(payload: dict = {}):
    """將傳入的股票清單廣播給所有收件人"""
    if not os.environ.get("TELEGRAM_BOT_TOKEN"):
        raise HTTPException(status_code=400, detail="尚未設定 Telegram Bot Token")
    stocks_input  = payload.get("stocks", [])
    all_stocks    = payload.get("all_stocks") or stocks_input
    label         = payload.get("label", "明日優先")
    market_status = payload.get("market_status") or None
    if not stocks_input:
        raise HTTPException(status_code=400, detail="沒有可傳送的股票")
    stocks = [s for s in stocks_input if s.get("industry") != "ETF"]
    if not stocks:
        raise HTTPException(status_code=400, detail="過濾 ETF 後沒有可傳送的股票")
    total  = len(stocks)
    stocks = stocks[:5]
    message = _build_tg_message(stocks, label, total=total, all_stocks=all_stocks, market_status=market_status)
    result  = _send_tg_to_all(message)
    if result["ok"] == 0:
        raise HTTPException(status_code=502, detail=f"全部傳送失敗：{result['errors']}")
    return {"status": "ok", "sent": len(stocks), "recipients": result["ok"], "failed": result["fail"]}

# ── 整合選股 TG 目標管理 API ──────────────────────────────────────────────────

@app.get("/api/tg/targets")
async def api_tg_list_targets():
    """列出所有 Telegram 目標（整合選股專用）"""
    return {"success": True, "targets": _get_tg_db_targets()}

@app.post("/api/tg/targets")
async def api_tg_add_target(payload: dict = {}):
    """新增 Telegram 目標"""
    chat_id     = str(payload.get("chat_id", "")).strip()
    name        = str(payload.get("name", "")).strip() or chat_id
    target_type = str(payload.get("target_type", "stock")).strip()
    if target_type not in ("stock", "amplitude", "all"):
        target_type = "stock"
    if not chat_id:
        raise HTTPException(status_code=400, detail="chat_id 不可為空")
    now  = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
    conn = _get_tg_db_conn()
    try:
        existing = conn.execute(
            "SELECT id FROM telegram_targets WHERE chat_id=? AND target_type=?",
            (chat_id, target_type)
        ).fetchone()
        if existing:
            type_label = {"stock": "股票", "amplitude": "震幅統計", "all": "全部"}.get(target_type, target_type)
            raise HTTPException(status_code=409, detail=f"此 Chat ID 已以「{type_label}」類型存在，請勿重複新增")
        conn.execute(
            "INSERT INTO telegram_targets (chat_id, name, enabled, target_type, created_at, updated_at) VALUES (?,?,1,?,?,?)",
            (chat_id, name, target_type, now, now),
        )
        conn.commit()
        return {"success": True}
    except HTTPException:
        raise
    except Exception as e:
        raise HTTPException(status_code=500, detail=str(e))
    finally:
        conn.close()

@app.put("/api/tg/targets/{target_id}")
async def api_tg_update_target(target_id: int, payload: dict = {}):
    """更新目標啟用狀態、名稱、Chat ID 或推送類型"""
    now  = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
    conn = _get_tg_db_conn()
    try:
        if "enabled" in payload:
            conn.execute(
                "UPDATE telegram_targets SET enabled=?, updated_at=? WHERE id=?",
                (1 if payload["enabled"] else 0, now, target_id),
            )
        if "name" in payload:
            conn.execute(
                "UPDATE telegram_targets SET name=?, updated_at=? WHERE id=?",
                (str(payload["name"]), now, target_id),
            )
        if "chat_id" in payload:
            cid = str(payload["chat_id"]).strip()
            if not cid:
                raise HTTPException(status_code=400, detail="chat_id 不可為空")
            row = conn.execute("SELECT target_type FROM telegram_targets WHERE id=?", (target_id,)).fetchone()
            cur_type = dict(row)["target_type"] if row else "stock"
            dup = conn.execute(
                "SELECT id FROM telegram_targets WHERE chat_id=? AND target_type=? AND id!=?",
                (cid, cur_type, target_id)
            ).fetchone()
            if dup:
                raise HTTPException(status_code=409, detail="此 Chat ID 已以相同類型存在")
            try:
                conn.execute(
                    "UPDATE telegram_targets SET chat_id=?, updated_at=? WHERE id=?",
                    (cid, now, target_id),
                )
            except sqlite3.IntegrityError:
                raise HTTPException(status_code=409, detail="此 Chat ID 已被其他目標使用")
        if "target_type" in payload:
            tt = str(payload["target_type"])
            if tt not in ("stock", "amplitude", "all"):
                tt = "stock"
            conn.execute(
                "UPDATE telegram_targets SET target_type=?, updated_at=? WHERE id=?",
                (tt, now, target_id),
            )
        conn.commit()
        return {"success": True}
    finally:
        conn.close()

@app.post("/api/tg/targets/{target_id}/test")
async def api_tg_simple_test(target_id: int):
    """對單一目標發送簡易測試訊息（不需整合選股資料）"""
    conn = _get_tg_db_conn()
    try:
        row = conn.execute("SELECT * FROM telegram_targets WHERE id=?", (target_id,)).fetchone()
    finally:
        conn.close()
    if not row:
        raise HTTPException(status_code=404, detail="找不到此目標")
    target = dict(row)
    token = os.environ.get("TELEGRAM_BOT_TOKEN", "")
    if not token:
        raise HTTPException(status_code=400, detail="尚未設定 Bot Token")
    type_label = {"stock": "股票", "amplitude": "震幅統計", "all": "全部"}.get(
        target.get("target_type", "stock"), "未知"
    )
    msg = (
        f"✅ <b>Telegram 測試訊息</b>\n"
        f"名稱：{target.get('name', '未命名')}\n"
        f"類型：{type_label}\n"
        f"來源：txf_viewer_pro"
    )
    url = f"https://api.telegram.org/bot{token}/sendMessage"
    success, err = _tg_post(url, target["chat_id"], msg)
    if not success:
        raise HTTPException(status_code=502, detail=f"傳送失敗：{err}")
    return {"success": True}

@app.delete("/api/tg/targets/{target_id}")
async def api_tg_delete_target(target_id: int):
    """刪除目標"""
    conn = _get_tg_db_conn()
    try:
        conn.execute("DELETE FROM telegram_targets WHERE id=?", (target_id,))
        conn.commit()
        return {"success": True}
    finally:
        conn.close()

@app.get("/api/tg/push-status")
async def api_tg_push_status():
    """查詢上次 TG 整合選股推送狀態"""
    return _tg_push_status

async def _build_amplitude_report_msg():
    """產生震幅日報訊息（calendar_date 模式），回傳 (msg, yesterday_date)"""
    try:
        amp_data = await get_amplitude_statistics(days=20, contract="TXFR1", date_mode="calendar_date")
    except Exception as e:
        raise HTTPException(status_code=500, detail=f"震幅資料讀取失敗：{e}")

    if not amp_data.get("success") or not amp_data.get("columns"):
        raise HTTPException(status_code=500, detail="震幅資料不足，無法產生報告")

    columns = amp_data["columns"]
    rows    = amp_data["rows"]

    yesterday_col = None
    for col in reversed(columns):
        if not col.get("is_today"):
            yesterday_col = col
            break

    if not yesterday_col:
        raise HTTPException(status_code=500, detail="找不到昨日完整資料")

    yesterday_date = yesterday_col["date"]
    weekday_str    = yesterday_col.get("weekday", "")

    status_label = {
        "super_large": "🔴 超大波動",
        "large":       "🟠 大波動",
        "normal":      "⚪ 正常",
        "small":       "🔵 小波動",
        "compressed":  "🟢 壓縮",
        "empty":       "－",
    }

    def get_cell(row_key, date_str):
        for row in rows:
            if row["key"] == row_key:
                for cell in row["cells"]:
                    if cell["date"] == date_str:
                        return cell
        return None

    def fmt_cell(cell):
        if not cell or cell.get("value") is None:
            return "－"
        v   = cell["value"]
        lbl = status_label.get(cell.get("status", ""), "")
        return f"{v}點 {lbl}"

    morning   = get_cell("morning",   yesterday_date)
    afternoon = get_cell("afternoon", yesterday_date)
    night     = get_cell("night",     yesterday_date)
    total     = get_cell("total",     yesterday_date)

    msg = (
        f"📊 <b>台指期震幅日報</b>\n"
        f"日期：{yesterday_date}（週{weekday_str}）\n\n"
        f"🌅 早盤 08:45~13:45\n{fmt_cell(morning)}\n\n"
        f"🌇 午盤 15:00~21:30\n{fmt_cell(afternoon)}\n\n"
        f"🌙 晚盤 21:30~05:00\n{fmt_cell(night)}\n\n"
        f"📐 振幅總和：{fmt_cell(total)}"
    )
    return msg, yesterday_date


@app.post("/api/amplitude/send_daily_report")
async def api_amplitude_send_daily_report():
    """推送震幅統計日報給所有 amplitude 類型目標"""
    targets = get_telegram_targets('amplitude')
    if not targets:
        return {"success": False, "message": "尚未設定震幅統計 Telegram 接收對象，請先到 Telegram 目標管理新增。"}

    msg, yesterday_date = await _build_amplitude_report_msg()
    result = _send_tg_with_targets(msg, targets)
    if result["ok"] == 0:
        raise HTTPException(status_code=502, detail=f"全部傳送失敗：{result['errors']}")
    return {
        "success":      True,
        "data_date":    yesterday_date,
        "sent":         result["ok"],
        "failed":       result["fail"],
        "target_count": len(targets),
    }


@app.post("/api/amplitude/send_daily_report/{target_id}")
async def api_amplitude_send_report_to_target(target_id: int):
    """推送震幅統計日報給單一指定目標"""
    conn = _get_tg_db_conn()
    try:
        row = conn.execute("SELECT * FROM telegram_targets WHERE id=?", (target_id,)).fetchone()
    finally:
        conn.close()
    if not row:
        raise HTTPException(status_code=404, detail="找不到此目標")
    target = dict(row)
    token  = os.environ.get("TELEGRAM_BOT_TOKEN", "")
    if not token:
        raise HTTPException(status_code=400, detail="尚未設定 Bot Token")

    msg, yesterday_date = await _build_amplitude_report_msg()
    result = _send_tg_with_targets(msg, [target])
    if result["ok"] == 0:
        raise HTTPException(status_code=502, detail=f"傳送失敗：{result['errors']}")
    return {"success": True, "data_date": yesterday_date}

def _generate_integrated_tg_message(integrated_result: dict = None, data_date: str = None, is_test_mode: bool = False) -> dict:
    """Build the integrated-strategy Telegram message through the single shared pipeline."""
    if integrated_result is None:
        if data_date:
            integrated_result = integrated_strategy.run_integrated_strategy(data_date=data_date)
        else:
            integrated_result = integrated_strategy.run_integrated_strategy()

    data_date_str = integrated_result.get("data_date", data_date or datetime.now().strftime("%Y-%m-%d"))
    tg_list       = build_tg_pick_list(integrated_result)
    hot_ind       = get_market_hot_industries(integrated_result)
    resonance_ind = get_resonance_industries(integrated_result)
    date_check    = validate_result_data_date(integrated_result)
    msg = format_tg_integrated_message(
        data_date_str,
        integrated_result.get("market_regime", {}),
        tg_list, hot_ind, resonance_ind,
        is_test_mode=is_test_mode,
    )
    concentrated = get_tg_pick_concentrated_industries(tg_list["tg_picks"], tg_list["tg_watch"])
    hot_source = hot_ind[0].get("source", "") if hot_ind else ""
    return {
        "message": msg,
        "integrated_result": integrated_result,
        "data_date": data_date_str,
        "tg_list": tg_list,
        "hot_ind": hot_ind,
        "resonance_ind": resonance_ind,
        "date_check": date_check,
        "concentrated": concentrated,
        "hot_source": hot_source,
        "is_test_mode": is_test_mode,
    }

@app.post("/api/tg/test-send")
async def api_tg_test_send_integrated():
    """TG test send: rerun integrated strategy and use the shared message builder."""
    global _last_integrated_result
    targets = get_telegram_targets('stock')
    if not targets:
        env_recipients = _get_tg_recipients()
        targets = [{"chat_id": r["chatId"], "name": r.get("name", "")} for r in env_recipients]
    if not targets:
        raise HTTPException(status_code=400, detail="???????? Telegram ????????? ID?")

    built = _generate_integrated_tg_message(is_test_mode=True)
    _last_integrated_result = built["integrated_result"]
    result = _send_tg_with_targets(built["message"], targets)
    if result["ok"] == 0:
        raise HTTPException(status_code=502, detail=f"???????{result['errors']}")

    data_date_str = built["data_date"]
    tg_list = built["tg_list"]
    date_check = built["date_check"]
    return {
        "status":                "ok",
        "data_date":             data_date_str,
        "send_time":             datetime.now().strftime("%Y-%m-%d %H:%M"),
        "stock_kbar_date":       date_check.get("stock_kbar_date", data_date_str),
        "market_data_date":      date_check.get("market_data_date", ""),
        "market_close":          date_check.get("market_close", 0),
        "institution_data_date": date_check.get("institution_data_date", ""),
        "market_regime":         date_check.get("market_regime", ""),
        "market_regime_success": date_check.get("market_regime_success", True),
        "date_valid":            date_check.get("valid", True),
        "is_test_mode":          built["is_test_mode"],
        "data_validation":       date_check.get("data_validation", {}),
        "market_strength_groups_available": bool(built["hot_ind"]),
        "market_strength_groups_source":    built["hot_source"],
        "tg_pick_count":         len(tg_list["tg_picks"]),
        "tg_watch_count":        len(tg_list["tg_watch"]),
        "downgraded_count":      tg_list.get("downgrade_count", 0),
        "downgrade_reasons":     tg_list.get("downgraded", []),
        "concentrated_industries": [c["industry"] for c in built["concentrated"]],
        "sent":                  result["ok"],
        "failed":                result["fail"],
    }

@app.post("/api/tg/test-send/{target_id}")
async def api_tg_test_send_single(target_id: int):
    """Single-target TG test send: only target differs; message flow is shared."""
    global _last_integrated_result
    conn = _get_tg_db_conn()
    try:
        row = conn.execute("SELECT * FROM telegram_targets WHERE id=?", (target_id,)).fetchone()
    finally:
        conn.close()
    if not row:
        raise HTTPException(status_code=404, detail="??????")

    built = _generate_integrated_tg_message(is_test_mode=True)
    _last_integrated_result = built["integrated_result"]
    result = _send_tg_with_targets(built["message"], [dict(row)])
    if result["ok"] == 0:
        raise HTTPException(status_code=502, detail=f"?????{result['errors']}")

    data_date_str = built["data_date"]
    tg_list = built["tg_list"]
    date_check = built["date_check"]
    return {
        "status":                "ok",
        "data_date":             data_date_str,
        "send_time":             datetime.now().strftime("%Y-%m-%d %H:%M"),
        "stock_kbar_date":       date_check.get("stock_kbar_date", data_date_str),
        "market_data_date":      date_check.get("market_data_date", ""),
        "market_close":          date_check.get("market_close", 0),
        "institution_data_date": date_check.get("institution_data_date", ""),
        "market_regime":         date_check.get("market_regime", ""),
        "market_regime_success": date_check.get("market_regime_success", True),
        "date_valid":            date_check.get("valid", True),
        "is_test_mode":          built["is_test_mode"],
        "data_validation":       date_check.get("data_validation", {}),
        "market_strength_groups_available": bool(built["hot_ind"]),
        "market_strength_groups_source":    built["hot_source"],
        "tg_pick_count":         len(tg_list["tg_picks"]),
        "tg_watch_count":        len(tg_list["tg_watch"]),
        "downgraded_count":      tg_list.get("downgrade_count", 0),
        "downgrade_reasons":     tg_list.get("downgraded", []),
        "concentrated_industries": [c["industry"] for c in built["concentrated"]],
    }

scheduler = AsyncIOScheduler(timezone="Asia/Taipei")


async def sync_all_stock_screener_data(target_date: str = "") -> dict:
    """
    統一同步整合選股所需資料：法人 + 個股日K + 大盤TAIEX日K。
    給前端同步選股數據按鈕與 TG 每日排程共用。
    """
    global api, is_logged_in
    now = datetime.now()

    # 決定 data_date（最近交易日）
    if not target_date:
        curr = now
        for _ in range(10):
            if curr.weekday() not in [5, 6]:
                target_date = curr.strftime("%Y-%m-%d")
                break
            curr -= timedelta(days=1)

    print(f"[同步選股數據] data_date={target_date}")

    # 1. 法人資料同步（最近 5 個交易日）
    inst_result = {"success": False, "synced_days": 0, "latest_date": "", "error": None}
    synced_days = 0
    last_inst_date = ""
    for i in range(15):
        test_date = now - timedelta(days=i)
        if test_date.weekday() in [5, 6]:
            continue
        try:
            screener.sync_twse_institutional_data(test_date)
            synced_days += 1
            if not last_inst_date:
                last_inst_date = test_date.strftime("%Y-%m-%d")
            if synced_days >= 5:
                break
        except Exception as e:
            print(f"[同步選股數據] 法人同步 {test_date.strftime('%Y-%m-%d')} 失敗：{e}")
    inst_result = {
        "success": synced_days > 0, "synced_days": synced_days,
        "latest_date": last_inst_date, "error": None,
    }
    print(f"[同步選股數據] sync_institutional={'success' if synced_days > 0 else 'warning'}, latest={last_inst_date}")

    # 2. 個股日K：本次富邦授權範圍僅限期貨行情，不能拿期貨 SDK
    #    冒充股票日K來源。保留既有 DB 供篩選使用，並明確回報新鮮度。
    kbar_result = {"success": False, "latest_date": "", "error": None}
    try:
        _conn = sqlite3.connect(_STOCK_DB_PATH, timeout=30.0)
        _row = _conn.execute("SELECT MAX(date) FROM daily_kbars").fetchone()
        _conn.close()
        _latest_stock_kbar = normalize_date(str(_row[0])) if _row and _row[0] else ""
        _is_current = bool(_latest_stock_kbar and _latest_stock_kbar >= target_date)
        kbar_result = {
            "success": _is_current,
            "latest_date": _latest_stock_kbar,
            "error": None if _is_current else (
                "富邦目前只串接期貨行情；個股日K沿用本地DB，尚未更新到目標日期"
            ),
        }
        print(
            "[同步選股數據] stock_daily_kbars="
            f"{'cache-current' if _is_current else 'cache-stale'}, latest={_latest_stock_kbar or 'none'}"
        )
    except Exception as e:
        kbar_result = {"success": False, "latest_date": "", "error": str(e)}
        print(f"[同步選股數據] stock_daily_kbars=failed: {e}")

    # 3. 大盤 TAIEX 日K同步（不需要登入）
    market_result = sync_taiex_daily_kbars(target_date)
    if market_result["success"]:
        print(f"[同步選股數據] sync_taiex_daily_kbars=success, latest={market_result['latest_date']}")
    else:
        print(f"[同步選股數據] sync_taiex_daily_kbars=failed: {market_result.get('error', '')}")

    # 3.5 Re-derive target_date from actual data availability.
    # Calendar weekday may point to today before today's close is available.
    # Use MAX(daily_kbars.date) — the last date for which we have validated stock data —
    # as the effective data_date when it falls within 3 calendar days of the original target.
    effective_data_date_reason = "最近非假日（行事曆推算）"
    try:
        _conn = sqlite3.connect(_STOCK_DB_PATH, timeout=30.0)
        _kb_row = _conn.execute("SELECT MAX(date) FROM daily_kbars").fetchone()
        _conn.close()
        _kb_latest = normalize_date(str(_kb_row[0])) if _kb_row and _kb_row[0] else ""
        if _kb_latest:
            _days_diff = (
                datetime.strptime(target_date, "%Y-%m-%d")
                - datetime.strptime(_kb_latest, "%Y-%m-%d")
            ).days
            if 0 <= _days_diff <= 3:
                if _kb_latest != target_date:
                    effective_data_date_reason = (
                        f"調整為daily_kbars最新日期（{_kb_latest}，原行事曆推算{target_date}）"
                    )
                    print(f"[同步選股數據] data_date 調整 {target_date} → {_kb_latest} (daily_kbars latest)")
                else:
                    effective_data_date_reason = "daily_kbars最新日期（與行事曆推算一致）"
                target_date = _kb_latest
    except Exception as _e:
        print(f"[同步選股數據] data_date re-derive 失敗: {_e}")

    # 4. 資料一致性驗證
    validation = validate_screener_data_date(target_date)
    print(f"[同步選股數據] validation.critical_ok={validation['critical_ok']}")

    return {
        "success":                    validation["critical_ok"],
        "data_date":                  target_date,
        "stock_kbar_date":            validation["stock_kbar_date"],
        "market_data_date":           validation["market_data_date"],
        "market_close":               validation.get("market_close", 0),
        "institution_data_date":      validation["institution_data_date"],
        "stock_result":               kbar_result,
        "institution_result":         inst_result,
        "market_result":              market_result,
        "validation":                 validation,
        "effective_data_date_reason": effective_data_date_reason,
        "market_close_source":        market_result.get("market_close_source", "Yahoo Finance"),
        "used_twse_fallback":         market_result.get("used_twse_fallback", False),
    }


def _send_telegram_message(message: str):
    """廣播訊息給股票推送對象（供排程 job 使用）"""
    targets = get_telegram_targets('stock') or _get_tg_recipients()
    result = _send_tg_with_targets(message, targets) if targets else {"ok": 0, "fail": 0, "errors": ["無目標"]}
    if result["ok"] > 0:
        print(f"[Scheduler] Telegram 傳送成功：{result['ok']} 人")
    if result["fail"] > 0:
        print(f"[Scheduler] Telegram 傳送失敗：{result['errors']}")

async def _scheduled_sync_and_alert():
    """排程任務主體：同步數據 → 整合選股 → TG 精選 → 傳送 Telegram"""
    global api, is_logged_in, _last_integrated_result, _tg_push_status
    now      = datetime.now()
    now_str  = now.strftime("%Y-%m-%d %H:%M:%S")
    date_str = now.strftime("%Y-%m-%d")
    print(f"[{now_str}] TG stock push started")

    if not is_tw_market_trading_day(now):
        print(f"[{now_str}] 今日非交易日，略過推送")
        return

    # 1. Run the same screener + MoneyDJ pipeline used by the manual sync button.
    try:
        pipeline = await _sync_screener_and_moneydj_pipeline(date_str)
        sync_res = pipeline["sync_result"]
        moneydj_sync = pipeline.get("moneydj_sync", {})
        result = pipeline.get("integrated_result")
        print(f"[{now_str}] sync_all: critical_ok={sync_res['success']}, "
              f"market_latest={sync_res.get('market_data_date', '')}, "
              f"stock_latest={sync_res.get('stock_kbar_date', '')}")
        print(f"[{now_str}] moneydj_sync: status={moneydj_sync.get('moneydj_sync_status')}, "
              f"fetched={moneydj_sync.get('moneydj_fetched_count', 0)}, "
              f"skipped={moneydj_sync.get('moneydj_skipped_count', 0)}, "
              f"failed={moneydj_sync.get('moneydj_failed_count', 0)}")
    except Exception as e:
        err_msg = f"同步失敗：{e}"
        print(f"[{now_str}] {err_msg}")
        _tg_push_status.update({"last_push_time": now_str, "last_push_status": "sync_failed", "last_error": err_msg})
        err_tg = (f"⚠️ *{date_str} 選股同步失敗*\n原因：{e}\n系統未推送明日精選股，避免使用舊資料。")
        targets = get_telegram_targets('stock') or [{"chat_id": r["chatId"], "name": r.get("name", "")} for r in _get_tg_recipients()]
        if targets:
            _send_tg_with_targets(err_tg, targets)
        return

    if result is None:
        err_msg = "整合選股未產生結果"
        print(f"[{now_str}] {err_msg}")
        _tg_push_status.update({"last_push_time": now_str, "last_push_status": "strategy_failed", "last_error": err_msg})
        targets = get_telegram_targets('stock') or [{"chat_id": r["chatId"], "name": r.get("name", "")} for r in _get_tg_recipients()]
        if targets:
            _send_tg_with_targets(f"⚠️ *{date_str} 排程篩選失敗*\n錯誤：{err_msg}", targets)
        return

    buy_count = len(result.get("buy_candidates", []))
    print(f"[{now_str}] integrated strategy success, buy_candidates={buy_count}")
    # 2b. 資料日期驗證
    data_date  = result.get("data_date", date_str)
    date_check = validate_result_data_date(result)
    mkt_date         = date_check.get("market_data_date", "？")
    taiex_close_val  = date_check.get("taiex_close", 0)
    mkt_regime_val   = date_check.get("market_regime", "")
    inst_date        = date_check.get("institution_data_date", "？")
    critical_ok      = date_check.get("critical_ok", True)
    print(
        f"[{now_str}] data_date={data_date}, market_data_date={mkt_date}, "
        f"institution_data_date={inst_date}, taiex_close={taiex_close_val}, "
        f"market_regime={mkt_regime_val}, tg_blocked={not critical_ok}"
    )

    if not critical_ok:
        err_lines = date_check.get("errors", [])
        stock_date = date_check.get("stock_kbar_date", date_str)
        err_tg = (
            f"⚠️ *選股資料異常｜資料日 {data_date}*\n\n"
            f"大盤資料日：{mkt_date}\n"
            f"個股資料日：{stock_date}\n\n"
            f"系統未推送明日精選股，避免使用舊資料。\n"
            f"請先執行「同步選股數據」，並確認大盤資料已同步。\n"
            f"原因：{chr(10).join(err_lines)}"
        )
        print(f"[TG 推送阻擋] data_date={data_date}, market_data_date={mkt_date}, reason=market data stale")
        targets = get_telegram_targets('stock') or [{"chat_id": r["chatId"], "name": r.get("name", "")} for r in _get_tg_recipients()]
        if targets:
            _send_tg_with_targets(err_tg, targets)
        _tg_push_status.update({"last_push_time": now_str, "last_push_status": "date_mismatch",
                                 "last_error": "; ".join(err_lines)})
        return

    # 3. Build TG picks/industry blocks and compose the message through the shared builder.
    built = _generate_integrated_tg_message(integrated_result=result, is_test_mode=False)
    tg_list = built["tg_list"]
    hot_ind = built["hot_ind"]
    hot_source = built["hot_source"]
    print(
        f"[{now_str}] hot_industries={len(hot_ind)}, source={hot_source}, "
        f"resonance_industries={len(built['resonance_ind'])}"
    )
    print(f"[TG ??] mode=scheduled, data_date={data_date}, market_data_date={mkt_date}, "
          f"tg_blocked=false, tg_picks={len(tg_list['tg_picks'])}, tg_watch={len(tg_list['tg_watch'])}")

    # 4. ?? TG ??
    msg = built["message"]

    # 5. ?????????????????
    targets = get_telegram_targets('stock')
    if not targets:
        targets = [{"chat_id": r["chatId"], "name": r.get("name", "")} for r in _get_tg_recipients()]
    print(f"[{now_str}] telegram targets={len(targets)}")

    if not targets:
        print(f"[{now_str}] 無啟用中的 Telegram 目標，略過傳送")
        _tg_push_status.update({
            "last_push_time": now_str, "last_push_status": "no_targets",
            "last_picks": len(tg_list["tg_picks"]), "last_watch": len(tg_list["tg_watch"]),
            "last_error": "無啟用中的目標", "target_count": 0, "sent_count": 0,
        })
        return

    send_result = _send_tg_with_targets(msg, targets)
    print(f"[{now_str}] telegram targets={len(targets)}, sent={send_result['ok']}, failed={send_result['fail']}")
    _tg_push_status.update({
        "last_push_time":   now_str,
        "last_push_status": "success" if send_result["ok"] > 0 else "all_failed",
        "last_picks":       len(tg_list["tg_picks"]),
        "last_watch":       len(tg_list["tg_watch"]),
        "last_error":       "; ".join(send_result["errors"]) if send_result["errors"] else None,
        "target_count":     len(targets),
        "sent_count":       send_result["ok"],
    })

async def _scheduled_amplitude_morning_report():
    """每週一至五 08:00 自動發送昨日震幅統計 Telegram 日報"""
    now_str = datetime.now().strftime("%Y/%m/%d %H:%M:%S")
    print(f"[{now_str}] [震幅排程] 開始執行早盤震幅日報")

    targets = get_telegram_targets('amplitude')
    if not targets:
        print(f"[{now_str}] [震幅排程] 無震幅 TG 接收者，略過傳送")
        return

    try:
        msg, yesterday_date = await _build_amplitude_report_msg()
    except Exception as e:
        print(f"[{now_str}] [震幅排程] 震幅資料建立失敗：{e}")
        return

    result = _send_tg_with_targets(msg, targets)
    print(
        f"[{now_str}] [震幅排程] 昨日={yesterday_date}, "
        f"targets={len(targets)}, sent={result['ok']}, failed={result['fail']}"
    )


@app.get("/api/debug/contracts")
async def api_debug_contracts():
    """列出富邦目前可存取的 TXF 合約清單。"""
    if not is_logged_in:
        raise HTTPException(status_code=401, detail="Not logged in")
    try:
        loop = asyncio.get_running_loop()
        futures = await loop.run_in_executor(None, lambda: api.list_contracts("TXF"))
        found = [{
            "code": item.code,
            "target_code": item.target_code,
            "name": item.name,
            "delivery_date": item.delivery_date,
        } for item in futures]
        return {"count": len(found), "contracts": found}
    except Exception as e:
        raise HTTPException(status_code=500, detail=str(e))

@app.get("/api/scheduler/status")
async def api_scheduler_status():
    """查詢排程狀態"""
    jobs = []
    for job in scheduler.get_jobs():
        jobs.append({
            "id": job.id,
            "name": job.name,
            "next_run": job.next_run_time.strftime("%Y/%m/%d %H:%M:%S") if job.next_run_time else None
        })
    return {"running": scheduler.running, "jobs": jobs}

@app.post("/api/scheduler/trigger")
async def api_scheduler_trigger():
    """手動立即觸發一次排程任務（測試用）"""
    asyncio.create_task(_scheduled_sync_and_alert())
    return {"status": "ok", "message": "排程任務已手動觸發，請稍候並查看 Telegram"}

@app.post("/api/scheduler/trigger_amplitude")
async def api_scheduler_trigger_amplitude():
    """手動立即觸發震幅日報（測試用）"""
    asyncio.create_task(_scheduled_amplitude_morning_report())
    return {"status": "ok", "message": "震幅日報已手動觸發，請稍候並查看 Telegram"}

@app.post("/api/screener/trace")
async def api_screener_trace(payload: dict = {}):
    """查詢指定股票的篩選追蹤結果（無論是否通過），供 Debug 面板使用"""
    code = str(payload.get("code", "")).strip()
    if not code:
        raise HTTPException(status_code=400, detail="請提供股票代號")
    try:
        result = screener.trace_stock_filters(code)
        return sanitize_for_json({"status": "success", "data": result})
    except Exception as e:
        import traceback
        traceback.print_exc()
        raise HTTPException(status_code=500, detail=f"追蹤查詢失敗: {str(e)}")

def _sync_moneydj_for_integrated_candidates(data_date: str, max_codes: int = 30, sleep_sec: float = 1.0, initial_result: dict = None) -> dict:
    """Sync MoneyDJ 5D broker data for the high-value integrated candidate pool."""
    conn = None
    summary = {
        "moneydj_sync_enabled": True,
        "moneydj_sync_status": "skipped",
        "moneydj_fetched_count": 0,
        "moneydj_skipped_count": 0,
        "moneydj_failed_count": 0,
        "moneydj_data_date": data_date,
        "moneydj_candidate_counts": {},
        "moneydj_candidate_codes_count": 0,
        "moneydj_valid_before": 0,
        "moneydj_valid_after": 0,
        "moneydj_errors": [],
    }
    if not data_date:
        summary["moneydj_sync_status"] = "skipped_no_data_date"
        summary["moneydj_errors"].append("missing data_date")
        return summary

    try:
        if initial_result is None:
            initial_result = integrated_strategy.run_integrated_strategy(data_date=data_date)
        candidate_buckets = ["buy_candidates", "high_priority_watch", "wait_pullback"]
        summary["moneydj_candidate_counts"] = {
            bucket: len(initial_result.get(bucket, []) or []) for bucket in candidate_buckets
        }

        codes = []
        for bucket in candidate_buckets:
            for stock in initial_result.get(bucket, []) or []:
                code = stock.get("stock_id") or stock.get("symbol") or stock.get("code")
                if code:
                    codes.append(str(code))
        summary["moneydj_candidate_codes_count"] = len(codes)

        for bucket in ("buy_candidates", "high_priority_watch", "wait_pullback", "other_watch", "excluded"):
            summary["moneydj_valid_before"] += sum(
                1 for stock in (initial_result.get(bucket, []) or [])
                if stock.get("moneydj_date_valid") is True
            )

        conn = sqlite3.connect(_STOCK_DB_PATH, timeout=30.0)
        conn.row_factory = sqlite3.Row
        sync_summary = moneydj_fetcher.sync_moneydj_periods_for_codes(
            conn,
            codes,
            period_label="5D",
            max_codes=max_codes,
            sleep_sec=sleep_sec,
            skip_existing=True,
            data_date=data_date,
        )

        refreshed_result = integrated_strategy.run_integrated_strategy(data_date=data_date)
        for bucket in ("buy_candidates", "high_priority_watch", "wait_pullback", "other_watch", "excluded"):
            summary["moneydj_valid_after"] += sum(
                1 for stock in (refreshed_result.get(bucket, []) or [])
                if stock.get("moneydj_date_valid") is True
            )

        failed_count = int(sync_summary.get("failed_count") or 0)
        fetched_count = int(sync_summary.get("fetched_count") or 0)
        skipped_count = int(sync_summary.get("skipped_count") or 0)
        summary.update({
            "moneydj_sync_status": "success" if failed_count == 0 else "partial_failed",
            "moneydj_fetched_count": fetched_count,
            "moneydj_skipped_count": skipped_count,
            "moneydj_failed_count": failed_count,
            "moneydj_data_date": sync_summary.get("data_date") or data_date,
            "moneydj_sync_summary": sync_summary,
            "refreshed_integrated_result": refreshed_result,
        })
        if failed_count:
            summary["moneydj_errors"] = sync_summary.get("failed_items", [])
        return summary
    except Exception as exc:
        print(f"[sync_screener] MoneyDJ candidate sync failed: {exc}")
        summary["moneydj_sync_status"] = "partial_failed"
        summary["moneydj_failed_count"] = 1
        summary["moneydj_errors"].append(str(exc))
        return summary
    finally:
        if conn is not None:
            conn.close()


async def _sync_screener_and_moneydj_pipeline(target_date: str = "") -> dict:
    """Run core screener sync, candidate MoneyDJ sync, and return the refreshed integrated result."""
    global _last_integrated_result
    sync_result = await sync_all_stock_screener_data(target_date)
    validation = sync_result.get("validation", {})
    data_date = sync_result.get("data_date", target_date or datetime.now().strftime("%Y-%m-%d"))
    moneydj_sync = {
        "moneydj_sync_enabled": True,
        "moneydj_sync_status": "skipped_data_not_ready",
        "moneydj_fetched_count": 0,
        "moneydj_skipped_count": 0,
        "moneydj_failed_count": 0,
        "moneydj_data_date": data_date,
        "moneydj_errors": [],
    }
    integrated_result = None

    if not sync_result.get("success"):
        _last_integrated_result = None
        return {
            "sync_result": sync_result,
            "validation": validation,
            "integrated_result": integrated_result,
            "moneydj_sync": moneydj_sync,
            "data_date": data_date,
        }

    initial_result = integrated_strategy.run_integrated_strategy(data_date=data_date)
    integrated_result = initial_result
    _last_integrated_result = initial_result

    try:
        moneydj_sync = _sync_moneydj_for_integrated_candidates(
            data_date,
            max_codes=30,
            sleep_sec=1.0,
            initial_result=initial_result,
        )
        refreshed = moneydj_sync.pop("refreshed_integrated_result", None)
        if refreshed is not None:
            integrated_result = refreshed
    except Exception as exc:
        print(f"[sync_pipeline] MoneyDJ candidate sync failed: {exc}")
        moneydj_sync = {
            "moneydj_sync_enabled": True,
            "moneydj_sync_status": "partial_failed",
            "moneydj_fetched_count": 0,
            "moneydj_skipped_count": 0,
            "moneydj_failed_count": 1,
            "moneydj_data_date": data_date,
            "moneydj_errors": [str(exc)],
        }

    _last_integrated_result = integrated_result
    return {
        "sync_result": sync_result,
        "validation": validation,
        "integrated_result": integrated_result,
        "moneydj_sync": moneydj_sync,
        "data_date": data_date,
    }

@app.post("/api/screener/sync")
async def api_sync_screener():
    """Trigger the shared screener + MoneyDJ candidate sync pipeline."""
    try:
        pipeline = await _sync_screener_and_moneydj_pipeline()
        result = pipeline["sync_result"]
        validation = pipeline.get("validation", {})
        moneydj_sync = pipeline.get("moneydj_sync", {})

        if result.get("success"):
            print("[sync_screener] shared pipeline refreshed integrated strategy cache")
        else:
            print("[sync_screener] data validation not ready; skipped MoneyDJ sync and cleared integrated cache")

        warnings = list(validation.get("warnings", []))
        if moneydj_sync.get("moneydj_sync_status") in ("partial_failed", "failed"):
            warnings.append("MoneyDJ candidate sync partially failed; core screener data sync completed")

        return {
            "status":                      "success" if result["success"] else "warning",
            "message":                     "選股數據同步完成！" if result["success"] else "同步完成但資料未完全對齊，請確認大盤資料",
            "data_date":                   result["data_date"],
            "stock_kbar_date":             validation.get("stock_kbar_date", ""),
            "market_data_date":            validation.get("market_data_date", ""),
            "market_close":                validation.get("market_close", 0),
            "institution_data_date":       validation.get("institution_data_date", ""),
            "critical_ok":                 validation.get("critical_ok", False),
            "market_regime_success":       result["market_result"].get("success", False),
            "warnings":                    warnings,
            "errors":                      validation.get("errors", []),
            "data_validation":             validation,
            "market_close_source":         result.get("market_close_source", "Yahoo Finance"),
            "effective_data_date_reason":  result.get("effective_data_date_reason", ""),
            "used_twse_fallback":          result.get("used_twse_fallback", False),
            **moneydj_sync,
        }
    except Exception as e:
        import traceback
        traceback.print_exc()
        raise HTTPException(status_code=500, detail=f"同步選股數據失敗: {str(e)}")

def get_effective_screener_data_date() -> dict:
    """
    回傳目前選股可用的有效資料日。
    優先使用 MAX(daily_kbars.date)。
    若與 calendar target 差距 <= 3 天，使用 daily_kbars 最新日期。
    回傳 data_date 與 reason。
    """
    _now = datetime.now()
    _curr = _now
    for _ in range(10):
        if _curr.weekday() not in [5, 6]:
            break
        _curr -= timedelta(days=1)
    target_date = _curr.strftime("%Y-%m-%d")
    reason = "最近非假日（行事曆推算）"
    try:
        _conn = sqlite3.connect(_STOCK_DB_PATH, timeout=30.0)
        _kb_row = _conn.execute("SELECT MAX(date) FROM daily_kbars").fetchone()
        _conn.close()
        _kb_latest = normalize_date(str(_kb_row[0])) if _kb_row and _kb_row[0] else ""
        if _kb_latest:
            _days_diff = (
                datetime.strptime(target_date, "%Y-%m-%d")
                - datetime.strptime(_kb_latest, "%Y-%m-%d")
            ).days
            if 0 <= _days_diff <= 3:
                if _kb_latest != target_date:
                    reason = f"調整為daily_kbars最新日期（{_kb_latest}，原行事曆推算{target_date}）"
                    print(f"[effective_data_date] {target_date} → {_kb_latest}")
                else:
                    reason = "daily_kbars最新日期（與行事曆推算一致）"
                target_date = _kb_latest
    except Exception as _e:
        print(f"[effective_data_date] 計算失敗: {_e}")
    return {"data_date": target_date, "reason": reason}



def _broker_existing_recent_dates(conn, code: str, recent_dates: list[str]) -> set:
    if not recent_dates:
        return set()
    broker_analysis.ensure_broker_tables(conn)
    ph = ",".join("?" for _ in recent_dates)
    rows = conn.execute(
        f"SELECT DISTINCT date FROM broker_trading_daily WHERE code = ? AND date IN ({ph})",
        [code, *recent_dates],
    ).fetchall()
    return {str(r[0]) for r in rows}



@app.get("/api/broker/fetch-trace")
async def api_broker_fetch_trace(code: str, days: int = 10):
    """Development-only trace for official broker fetch attempts."""
    conn = None
    try:
        conn = sqlite3.connect(_STOCK_DB_PATH, timeout=30.0)
        conn.row_factory = sqlite3.Row
        broker_analysis.ensure_broker_tables(conn)
        stock = broker_analysis.resolve_stock_query(conn, code)
        if not stock:
            return sanitize_for_json({
                "status": "error",
                "code": code,
                "stock_name": "",
                "market_type": "unknown",
                "recent_trading_dates": [],
                "attempts": [],
                "final_status": "not_found",
                "final_message": "找不到股票代號或名稱",
            })
        result = broker_fetcher.trace_broker_fetch_for_stock(conn, stock["code"], days=days, write=True)
        result["status"] = "success"
        return sanitize_for_json(result)
    except Exception as e:
        import traceback
        traceback.print_exc()
        return sanitize_for_json({
            "status": "success",
            "code": code,
            "stock_name": "",
            "market_type": "unknown",
            "recent_trading_dates": [],
            "attempts": [],
            "final_status": "failed",
            "final_message": "fetch trace failed",
            "error_message": str(e),
        })
    finally:
        if conn is not None:
            conn.close()

@app.get("/api/broker/moneydj-trace")
async def api_broker_moneydj_trace(code: str, period: str = "5D"):
    """Debug-only MoneyDJ Fubon period broker fetch for one stock."""
    try:
        trace = moneydj_fetcher.trace_moneydj_fetch(code, period)
        return sanitize_for_json({"status": "success", "trace": trace, **trace})
    except Exception as e:
        return sanitize_for_json({
            "status": "failed",
            "code": code,
            "period_label": period,
            "parse_status": "fetch_failed",
            "error_message": str(e),
        })

@app.get("/api/broker/moneydj-fetch")
async def api_broker_moneydj_fetch(code: str, period: str = "5D"):
    """Fetch and store MoneyDJ Fubon period broker summary for one stock."""
    conn = None
    try:
        conn = sqlite3.connect(_STOCK_DB_PATH, timeout=30.0)
        conn.row_factory = sqlite3.Row
        result = moneydj_fetcher.fetch_and_store_moneydj_period(conn, code, period)
        return sanitize_for_json({
            "status": result.get("status"),
            "message": result.get("message"),
            "parsed_rows": result.get("parsed_rows", 0),
            "inserted_rows": result.get("inserted_rows", 0),
            "updated_rows": result.get("updated_rows", 0),
            "trace": result.get("trace", {}),
        })
    except Exception as e:
        return sanitize_for_json({
            "status": "failed",
            "message": str(e),
            "parsed_rows": 0,
            "inserted_rows": 0,
            "updated_rows": 0,
        })
    finally:
        if conn is not None:
            conn.close()

@app.get("/api/broker/period-summary")
async def api_broker_period_summary(code: str, period: str = "5D"):
    """Read stored MoneyDJ Fubon period broker summary for one stock."""
    conn = None
    try:
        conn = sqlite3.connect(_STOCK_DB_PATH, timeout=30.0)
        conn.row_factory = sqlite3.Row
        result = moneydj_fetcher.get_moneydj_period_summary(conn, code, period)
        return sanitize_for_json(result)
    except Exception as e:
        return sanitize_for_json({
            "status": "failed",
            "code": code,
            "period_label": period,
            "message": str(e),
            "rows": [],
            "buy_rows": [],
            "sell_rows": [],
        })
    finally:
        if conn is not None:
            conn.close()

@app.get("/api/broker/key-points")
async def api_broker_key_points(query: str, as_of_date: str = None):
    """Key broker analysis for one stock code or stock name, with on-demand official fetch."""
    conn = None
    fetch_status = "not_needed"
    fetch_message = "分點資料已存在，未自動抓取"
    try:
        conn = sqlite3.connect(_STOCK_DB_PATH, timeout=30.0)
        conn.row_factory = sqlite3.Row
        broker_analysis.ensure_broker_tables(conn)

        stock = broker_analysis.resolve_stock_query(conn, query)
        if not stock:
            result = {"status": "error", "message": "找不到股票代號或名稱", "query": query}
            return sanitize_for_json(result)

        code = stock["code"]
        recent_dates = broker_fetcher.get_recent_trading_dates(conn, code, 10)
        existing_dates = _broker_existing_recent_dates(conn, code, recent_dates)
        enough_recent_data = bool(recent_dates) and len(existing_dates) >= len(recent_dates)

        if not enough_recent_data:
            try:
                fetch_result = broker_fetcher.fetch_broker_data_for_stock(conn, code, days=10)
                fetch_status = fetch_result.get("status", "failed")
                fetch_message = fetch_result.get("message", "抓取失敗，請稍後再試或使用 CSV 匯入")
            except Exception as fetch_err:
                print(f"[broker_fetcher] fetch failed for {code}: {fetch_err}")
                fetch_status = "failed"
                fetch_message = "抓取失敗，請稍後再試或使用 CSV 匯入"

        result = broker_analysis.analyze_key_brokers(conn, code, as_of_date=as_of_date)
        if result.get("status") == "success":
            result["fetch_status"] = fetch_status
            result["fetch_message"] = fetch_message
        return sanitize_for_json(result)
    except Exception as e:
        import traceback
        traceback.print_exc()
        return sanitize_for_json({
            "status": "success",
            "query": query,
            "stock": None,
            "data_date": None,
            "summary": {
                "available": False,
                "broker_status": "無資料",
                "broker_score_5d": 0,
                "broker_score_10d": 0,
                "main_key_brokers": [],
                "main_warning": "資料抓取失敗或暫無資料",
                "conclusion": "資料抓取失敗或暫無資料，請稍後再試或使用 CSV 匯入。",
            },
            "key_brokers": [],
            "top_buy_brokers_5d": [],
            "top_sell_brokers_5d": [],
            "warnings": [{"type": "fetch_failed", "level": "warning", "message": "資料抓取失敗或暫無資料"}],
            "available": False,
            "fetch_status": "failed",
            "fetch_message": "抓取失敗，請稍後再試或使用 CSV 匯入",
        })
    finally:
        if conn is not None:
            conn.close()


@app.post("/api/broker/moneydj-sync-candidates")
async def api_broker_moneydj_sync_candidates(payload: dict = {}):
    """Manually sync MoneyDJ 5D broker data for high-value integrated candidates."""
    global _last_integrated_result
    conn = None
    try:
        payload = payload or {}
        try:
            max_codes = int(payload.get("max_codes", 30))
        except Exception:
            max_codes = 30
        max_codes = max(1, min(max_codes, 50))
        try:
            sleep_sec = float(payload.get("sleep_sec", 1.0))
        except Exception:
            sleep_sec = 1.0
        sleep_sec = max(0.0, min(sleep_sec, 5.0))

        _eff = get_effective_screener_data_date()
        data_date = _eff["data_date"]
        print(f"[api/broker/moneydj-sync-candidates] data_date={data_date}, reason={_eff['reason']}")

        initial_result = integrated_strategy.run_integrated_strategy(data_date=data_date)
        candidate_buckets = ["buy_candidates", "high_priority_watch", "wait_pullback"]
        candidate_counts = {bucket: len(initial_result.get(bucket, []) or []) for bucket in candidate_buckets}

        codes = []
        for bucket in candidate_buckets:
            for stock in initial_result.get(bucket, []) or []:
                code = stock.get("stock_id") or stock.get("symbol") or stock.get("code")
                if code:
                    codes.append(str(code))

        before_valid = 0
        for bucket in ("buy_candidates", "high_priority_watch", "wait_pullback", "other_watch", "excluded"):
            before_valid += sum(1 for stock in (initial_result.get(bucket, []) or []) if stock.get("moneydj_date_valid") is True)

        conn = sqlite3.connect(_STOCK_DB_PATH, timeout=30.0)
        conn.row_factory = sqlite3.Row
        sync_summary = moneydj_fetcher.sync_moneydj_periods_for_codes(
            conn,
            codes,
            period_label="5D",
            max_codes=max_codes,
            sleep_sec=sleep_sec,
            skip_existing=True,
            data_date=data_date,
        )

        refreshed_result = integrated_strategy.run_integrated_strategy(data_date=data_date)
        _last_integrated_result = refreshed_result
        refreshed_summary = refreshed_result.get("summary", {})
        after_valid = 0
        for bucket in ("buy_candidates", "high_priority_watch", "wait_pullback", "other_watch", "excluded"):
            after_valid += sum(1 for stock in (refreshed_result.get(bucket, []) or []) if stock.get("moneydj_date_valid") is True)

        return sanitize_for_json({
            "ok": True,
            "data_date": data_date,
            "sync_summary": sync_summary,
            "candidate_counts": candidate_counts,
            "candidate_codes_count": len(codes),
            "moneydj_valid_before": before_valid,
            "moneydj_valid_after": after_valid,
            "refreshed_summary": refreshed_summary,
        })
    except Exception as e:
        import traceback
        traceback.print_exc()
        raise HTTPException(status_code=500, detail=f"MoneyDJ candidate sync failed: {str(e)}")
    finally:
        if conn is not None:
            conn.close()

@app.get("/api/tomorrow_strategy")
async def api_tomorrow_strategy():
    """大盤狀態 × 明日策略選股"""
    try:
        _eff = get_effective_screener_data_date()
        _data_date = _eff["data_date"]
        print(f"[api/tomorrow_strategy] data_date={_data_date}, reason={_eff['reason']}")
        result = tomorrow_strategy.run_tomorrow_strategy(data_date=_data_date)
        return {"status": "success", **result}
    except Exception as e:
        import traceback
        traceback.print_exc()
        raise HTTPException(status_code=500, detail=f"明日策略選股計算失敗: {str(e)}")

@app.get("/api/integrated-strategy")
async def api_integrated_strategy():
    """整合選股（tomorrow_strategy 主決策 + screener 籌碼輔助）"""
    global _last_integrated_result
    try:
        _eff = get_effective_screener_data_date()
        _data_date = _eff["data_date"]
        print(f"[api/integrated-strategy] data_date={_data_date}, reason={_eff['reason']}")
        result = integrated_strategy.run_integrated_strategy(data_date=_data_date)
        _last_integrated_result = result
        date_check = validate_result_data_date(result)
        return {
            **result,
            "stock_kbar_date":       date_check.get("stock_kbar_date", result.get("data_date", "")),
            "market_data_date":      date_check.get("market_data_date", ""),
            "market_close":          date_check.get("market_close", 0),
            "institution_data_date": date_check.get("institution_data_date", ""),
            "market_regime_success": date_check.get("market_regime_success", True),
            "data_validation":       date_check.get("data_validation", {}),
        }
    except Exception as e:
        import traceback
        traceback.print_exc()
        raise HTTPException(status_code=500, detail=f"整合選股計算失敗: {str(e)}")


@app.websocket("/ws")
async def websocket_endpoint(websocket: WebSocket):
    await manager.connect(websocket)
    try:
        await websocket.send_text(json.dumps({
            "type": "quote_status",
            "data": _quote_health_data(),
        }))
        while True:
            await websocket.receive_text()
            # 可以接收前端 ping 等訊息
    except WebSocketDisconnect:
        pass
    except Exception as e:
        print(f"[WS] 前端連線異常: {e}")
    finally:
        manager.disconnect(websocket)

@app.on_event("startup")
async def startup_event():
    _init_tg_targets_table()
    scheduler.add_job(
        _scheduled_sync_and_alert,
        CronTrigger(day_of_week="mon-fri", hour=18, minute=0, timezone="Asia/Taipei"),
        id="daily_alert",
        name="每日18:00整合選股+Telegram精選推送",
        replace_existing=True,
        misfire_grace_time=3600,
        coalesce=True,
    )
    scheduler.add_job(
        _scheduled_amplitude_morning_report,
        CronTrigger(day_of_week="mon-fri", hour=8, minute=0, timezone="Asia/Taipei"),
        id="amplitude_morning_report",
        name="每日08:00震幅統計Telegram日報",
        replace_existing=True,
        misfire_grace_time=3600,
        coalesce=True,
    )
    scheduler.start()
    print("[Scheduler] 排程已啟動 — 每週一至週五 18:00 選股推送 / 08:00 震幅日報")

@app.on_event("shutdown")
async def shutdown_event():
    scheduler.shutdown(wait=False)
    print("[Scheduler] 排程已停止")

# 掛載靜態檔案 (前端)
# 注意：為了能在根目錄直接啟動，必須確保 static 資料夾存在
static_dir = os.path.join(os.path.dirname(os.path.abspath(__file__)), "static")
os.makedirs(static_dir, exist_ok=True)
app.mount("/", StaticFiles(directory=static_dir, html=True), name="static")

if __name__ == "__main__":
    import uvicorn
    uvicorn.run("main:app", host="127.0.0.1", port=8000, reload=True)
