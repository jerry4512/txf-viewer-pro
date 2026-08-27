"""Read-only Fubon Neo futures and stock market-data integration.

This module deliberately exposes market-data operations only.  It contains no
order, cancel, modify, position-closing, or conditional-order functionality.
"""

from __future__ import annotations

import calendar
import json
import threading
import time
from dataclasses import dataclass
from datetime import date, datetime, timedelta, timezone
from typing import Any, Callable, Iterable, Optional

from fubon_neo.sdk import FubonSDK, Mode
from fubon_neo.fugle_marketdata.rest.base_rest import FugleAPIError


TAIPEI = timezone(timedelta(hours=8))


class FubonMarketDataError(RuntimeError):
    """Safe, credential-free error raised by the market-data adapter."""


@dataclass
class FubonContract:
    """Contract identity used by the existing TXF viewer API."""

    code: str
    target_code: str
    symbol: str
    name: str = ""
    reference: float = 0.0
    delivery_date: str = ""
    security_type: str = "FUT"


class FubonKbars(dict):
    """Mapping with attribute access, compatible with the existing cache path."""

    _FIELDS = ("ts", "Open", "High", "Low", "Close", "Volume")
    _OPTIONAL_FIELDS = ("Average",)

    def __init__(self, rows: Iterable[dict[str, Any]] = ()):
        rows = list(rows)
        fields = list(self._FIELDS)
        fields.extend(
            field for field in self._OPTIONAL_FIELDS
            if any(field in row for row in rows)
        )
        super().__init__({
            field: [row.get(field) for row in rows]
            for field in fields
        })

    def __getattr__(self, name: str):
        try:
            return self[name]
        except KeyError as exc:
            raise AttributeError(name) from exc


class FubonSnapshot:
    """Attribute-style normalized intraday quote."""

    def __init__(self, payload: dict[str, Any]):
        self.__dict__.update(payload)


def _safe_float(value: Any, default: float = 0.0) -> float:
    try:
        return float(value) if value is not None else default
    except (TypeError, ValueError):
        return default


def _safe_int(value: Any, default: int = 0) -> int:
    try:
        return int(value) if value is not None else default
    except (TypeError, ValueError):
        return default


def _response_payload(response: Any) -> dict[str, Any]:
    if isinstance(response, dict):
        return response
    if isinstance(response, str):
        decoded = json.loads(response)
        return decoded if isinstance(decoded, dict) else {}
    data = getattr(response, "data", None)
    if isinstance(data, dict):
        return data
    if hasattr(response, "json"):
        decoded = response.json()
        return decoded if isinstance(decoded, dict) else {}
    return {}


def _timestamp_seconds(value: Any) -> int:
    if isinstance(value, datetime):
        if value.tzinfo is None:
            value = value.replace(tzinfo=TAIPEI)
        return int(value.timestamp())
    if isinstance(value, (int, float)):
        numeric = float(value)
        if numeric > 1e17:
            numeric /= 1e9
        elif numeric > 1e14:
            numeric /= 1e6
        elif numeric > 1e11:
            numeric /= 1e3
        return int(numeric) if numeric > 0 else 0
    if isinstance(value, str) and value:
        text = value.strip().replace("Z", "+00:00")
        try:
            parsed = datetime.fromisoformat(text)
            if parsed.tzinfo is None:
                parsed = parsed.replace(tzinfo=TAIPEI)
            return int(parsed.timestamp())
        except ValueError:
            return 0
    return 0


def _taipei_wallclock_close_ns(value: Any) -> tuple[int, Optional[date]]:
    """Convert Fubon candle start time to legacy close-stamped wall-clock ns."""
    seconds = _timestamp_seconds(value)
    if seconds <= 0:
        return 0, None
    local_dt = datetime.fromtimestamp(seconds, tz=timezone.utc).astimezone(TAIPEI)
    naive = local_dt.replace(tzinfo=None)
    start_ns = int(calendar.timegm(naive.timetuple()) * 1e9 + naive.microsecond * 1000)
    return start_ns + 60 * 1_000_000_000, local_dt.date()


class FubonMarketDataClient:
    """Fubon read-only futures/stock REST and WebSocket facade."""

    FUTURES_ROOTS = ("TXF", "MXF", "TMF")

    def __init__(self):
        self.sdk: Optional[FubonSDK] = None
        self.rest = None
        self.ws = None
        self.stock_rest = None
        self.stock_ws = None
        self.logged_in = False
        self.connected = False
        self.stock_connected = False
        self._closing = False
        self._callbacks: dict[str, Optional[Callable]] = {
            "tick": None,
            "candle": None,
            "aggregate": None,
            "connect": None,
            "disconnect": None,
            "error": None,
            "stock_tick": None,
            "stock_candle": None,
            "stock_connect": None,
            "stock_disconnect": None,
            "stock_error": None,
        }
        self._contracts: dict[str, tuple[float, FubonContract]] = {}
        self._stock_contracts: dict[str, tuple[float, FubonContract]] = {}
        self._alias_by_symbol: dict[str, str] = {}
        self._aggregate_by_symbol: dict[str, dict[str, Any]] = {}
        self._desired_subscriptions: set[tuple[str, str, bool]] = set()
        self._pending_subscriptions: set[tuple[str, str, bool]] = set()
        self._subscription_ids: set[str] = set()
        self._subscription_lock = threading.RLock()
        self._reconnect_lock = threading.Lock()
        self._reconnect_thread: Optional[threading.Thread] = None
        self._desired_stock_subscriptions: set[tuple[str, str]] = set()
        self._pending_stock_subscriptions: set[tuple[str, str]] = set()
        self._active_stock_subscriptions: set[tuple[str, str]] = set()
        self._stock_subscription_ids: set[str] = set()
        self._stock_subscription_lock = threading.RLock()
        self._stock_reconnect_lock = threading.Lock()
        self._stock_reconnect_thread: Optional[threading.Thread] = None

    @property
    def version(self) -> str:
        try:
            import importlib.metadata

            return importlib.metadata.version("fubon-neo")
        except Exception:
            return "unknown"

    def set_callbacks(
        self,
        *,
        tick: Optional[Callable] = None,
        candle: Optional[Callable] = None,
        aggregate: Optional[Callable] = None,
        connect: Optional[Callable] = None,
        disconnect: Optional[Callable] = None,
        error: Optional[Callable] = None,
        stock_tick: Optional[Callable] = None,
        stock_candle: Optional[Callable] = None,
        stock_connect: Optional[Callable] = None,
        stock_disconnect: Optional[Callable] = None,
        stock_error: Optional[Callable] = None,
    ) -> None:
        self._callbacks.update({
            "tick": tick,
            "candle": candle,
            "aggregate": aggregate,
            "connect": connect,
            "disconnect": disconnect,
            "error": error,
            "stock_tick": stock_tick,
            "stock_candle": stock_candle,
            "stock_connect": stock_connect,
            "stock_disconnect": stock_disconnect,
            "stock_error": stock_error,
        })

    def login(
        self,
        *,
        personal_id: str,
        api_key: str,
        cert_path: str = "",
        cert_pass: Optional[str] = None,
    ) -> Any:
        if not personal_id or not api_key:
            raise FubonMarketDataError("富邦登入需要身分證字號與 API Key")
        self.logout()
        self._closing = False
        self.sdk = FubonSDK()
        try:
            if cert_path:
                result = self.sdk.apikey_login(
                    personal_id,
                    api_key,
                    cert_path,
                    cert_pass or None,
                )
            else:
                result = self.sdk.apikey_dma_login(personal_id, api_key)
        except Exception as exc:
            raise FubonMarketDataError(
                f"富邦 API Key 登入失敗（{type(exc).__name__}）"
            ) from exc

        success = getattr(result, "is_success", None)
        if success is None and isinstance(result, dict):
            success = result.get("is_success", result.get("isSuccess"))
        if success is False:
            message = getattr(result, "message", None)
            if message is None and isinstance(result, dict):
                message = result.get("message")
            raise FubonMarketDataError(
                f"富邦登入未成功：{str(message or '請確認金鑰、權限與憑證')[:160]}"
            )

        try:
            self.sdk.init_realtime(Mode.Normal)
            self.rest = self.sdk.marketdata.rest_client.futopt
            self.stock_rest = self.sdk.marketdata.rest_client.stock
            self.ws = self.sdk.marketdata.websocket_client.futopt
            self.stock_ws = self.sdk.marketdata.websocket_client.stock
            self.ws.on("message", self._handle_message)
            self.ws.on("connect", self._handle_connect)
            self.ws.on("disconnect", self._handle_disconnect)
            self.ws.on("error", self._handle_error)
            self.stock_ws.on("message", self._handle_stock_message)
            self.stock_ws.on("connect", self._handle_stock_connect)
            self.stock_ws.on("disconnect", self._handle_stock_disconnect)
            self.stock_ws.on("error", self._handle_stock_error)
            self.ws.connect()
            try:
                self.stock_ws.connect()
            except Exception as stock_exc:
                self._call("stock_error", f"connect {type(stock_exc).__name__}")
        except Exception as exc:
            try:
                self.sdk.logout()
            except Exception:
                pass
            self.sdk = None
            self.rest = None
            self.ws = None
            self.stock_rest = None
            self.stock_ws = None
            raise FubonMarketDataError(
                f"富邦行情連線初始化失敗（{type(exc).__name__}）"
            ) from exc

        self.logged_in = True
        return result

    def logout(self) -> None:
        self._closing = True
        ws, stock_ws, sdk = self.ws, self.stock_ws, self.sdk
        self.connected = False
        self.stock_connected = False
        self.logged_in = False
        self.rest = None
        self.ws = None
        self.stock_rest = None
        self.stock_ws = None
        self.sdk = None
        with self._subscription_lock:
            self._desired_subscriptions.clear()
            self._pending_subscriptions.clear()
            self._subscription_ids.clear()
        with self._stock_subscription_lock:
            self._desired_stock_subscriptions.clear()
            self._pending_stock_subscriptions.clear()
            self._active_stock_subscriptions.clear()
            self._stock_subscription_ids.clear()
        if ws is not None:
            try:
                ws.disconnect()
            except Exception:
                pass
        if stock_ws is not None:
            try:
                stock_ws.disconnect()
            except Exception:
                pass
        if sdk is not None:
            try:
                sdk.logout()
            except Exception:
                pass

    def _call(self, name: str, *args) -> None:
        callback = self._callbacks.get(name)
        if callback:
            callback(*args)

    def _handle_connect(self, *args) -> None:
        self.connected = True
        self._call("connect", *args)
        self._resubscribe_desired()

    def _handle_disconnect(self, *args) -> None:
        self.connected = False
        with self._subscription_lock:
            self._pending_subscriptions.clear()
            self._subscription_ids.clear()
        self._call("disconnect", *args)
        if not self._closing and self.logged_in:
            self._start_reconnect_worker()

    def _handle_error(self, *args) -> None:
        self._call("error", *args)

    def _handle_stock_connect(self, *args) -> None:
        self.stock_connected = True
        self._call("stock_connect", *args)
        self._resubscribe_desired_stocks()

    def _handle_stock_disconnect(self, *args) -> None:
        self.stock_connected = False
        with self._stock_subscription_lock:
            self._pending_stock_subscriptions.clear()
            self._active_stock_subscriptions.clear()
            self._stock_subscription_ids.clear()
        self._call("stock_disconnect", *args)
        if not self._closing and self.logged_in:
            self._start_stock_reconnect_worker()

    def _handle_stock_error(self, *args) -> None:
        self._call("stock_error", *args)

    def _start_reconnect_worker(self) -> None:
        with self._reconnect_lock:
            if self._reconnect_thread and self._reconnect_thread.is_alive():
                return
            self._reconnect_thread = threading.Thread(
                target=self._reconnect_worker,
                name="fubon-marketdata-reconnect",
                daemon=True,
            )
            self._reconnect_thread.start()

    def _reconnect_worker(self) -> None:
        for delay in (1, 2, 5, 10, 20, 30):
            if self._closing or not self.logged_in or self.ws is None:
                return
            time.sleep(delay)
            try:
                self.ws.connect()
                return
            except Exception as exc:
                self._call("error", f"reconnect {type(exc).__name__}")

    def _start_stock_reconnect_worker(self) -> None:
        with self._stock_reconnect_lock:
            if (
                self._stock_reconnect_thread
                and self._stock_reconnect_thread.is_alive()
            ):
                return
            self._stock_reconnect_thread = threading.Thread(
                target=self._stock_reconnect_worker,
                name="fubon-stock-marketdata-reconnect",
                daemon=True,
            )
            self._stock_reconnect_thread.start()

    def _stock_reconnect_worker(self) -> None:
        for delay in (1, 2, 5, 10, 20, 30):
            if self._closing or not self.logged_in or self.stock_ws is None:
                return
            time.sleep(delay)
            try:
                self.stock_ws.connect()
                return
            except Exception as exc:
                self._call("stock_error", f"reconnect {type(exc).__name__}")

    def _handle_message(self, message: Any) -> None:
        try:
            payload = json.loads(message) if isinstance(message, str) else message
            if not isinstance(payload, dict):
                return
            event = str(payload.get("event") or "")
            if event == "authenticated":
                self.connected = True
                return
            if event == "subscribed":
                data = payload.get("data")
                items = data if isinstance(data, list) else [data]
                with self._subscription_lock:
                    for item in items:
                        if not isinstance(item, dict):
                            continue
                        channel = str(item.get("channel") or "")
                        symbol = str(item.get("symbol") or "")
                        subscription_id = str(item.get("id") or "")
                        if subscription_id:
                            self._subscription_ids.add(subscription_id)
                        for key in list(self._pending_subscriptions):
                            if key[0] == channel and key[1] == symbol:
                                self._pending_subscriptions.discard(key)
                return
            if event == "error":
                with self._subscription_lock:
                    self._pending_subscriptions.clear()
                self._call("error", payload.get("data") or payload)
                return
            if event != "data":
                return

            channel = str(payload.get("channel") or "")
            data = payload.get("data")
            if not isinstance(data, dict):
                return
            symbol = str(data.get("symbol") or "")
            alias = self._alias_by_symbol.get(symbol, symbol)

            if channel == "aggregates":
                aggregate = dict(data)
                aggregate["code"] = alias
                self._aggregate_by_symbol[symbol] = aggregate
                self._call("aggregate", aggregate)
            elif channel == "candles":
                candle = dict(data)
                candle["code"] = alias
                candle["target_code"] = symbol
                candle["ts"] = _timestamp_seconds(
                    candle.get("date", candle.get("time"))
                )
                self._call("candle", candle)
            elif channel == "trades":
                trades = data.get("trades") or []
                if isinstance(trades, dict):
                    trades = [trades]
                if not trades:
                    return
                last_trade = trades[-1]
                if not isinstance(last_trade, dict):
                    return
                aggregate = self._aggregate_by_symbol.get(symbol, {})
                total = data.get("total") or aggregate.get("total") or {}
                tick = {
                    "code": alias,
                    "target_code": symbol,
                    "close": _safe_float(last_trade.get("price")),
                    "volume": sum(max(0, _safe_int(t.get("size"))) for t in trades if isinstance(t, dict)),
                    "total_volume": _safe_int(total.get("tradeVolume")),
                    "ts": data.get("time"),
                    "open": _safe_float(aggregate.get("openPrice")),
                    "high": _safe_float(aggregate.get("highPrice")),
                    "low": _safe_float(aggregate.get("lowPrice")),
                    "reference": _safe_float(aggregate.get("previousClose")),
                    "price_chg": _safe_float(aggregate.get("change")),
                    "bid": _safe_float(last_trade.get("bid")),
                    "ask": _safe_float(last_trade.get("ask")),
                    "serial": data.get("serial"),
                }
                if tick["close"] > 0:
                    self._call("tick", tick)
        except Exception as exc:
            self._call("error", f"message {type(exc).__name__}")

    def _handle_stock_message(self, message: Any) -> None:
        try:
            payload = json.loads(message) if isinstance(message, str) else message
            if not isinstance(payload, dict):
                return
            event = str(payload.get("event") or "")
            if event == "authenticated":
                self.stock_connected = True
                return
            if event == "subscribed":
                data = payload.get("data")
                items = data if isinstance(data, list) else [data]
                with self._stock_subscription_lock:
                    for item in items:
                        if not isinstance(item, dict):
                            continue
                        key = (
                            str(item.get("channel") or ""),
                            str(item.get("symbol") or ""),
                        )
                        subscription_id = str(item.get("id") or "")
                        if subscription_id:
                            self._stock_subscription_ids.add(subscription_id)
                        self._pending_stock_subscriptions.discard(key)
                        self._active_stock_subscriptions.add(key)
                return
            if event == "error":
                with self._stock_subscription_lock:
                    self._pending_stock_subscriptions.clear()
                self._call("stock_error", payload.get("data") or payload)
                return
            if event != "data":
                return

            channel = str(payload.get("channel") or "")
            data = payload.get("data")
            if not isinstance(data, dict):
                return
            symbol = str(data.get("symbol") or "")
            if not symbol:
                return

            if channel == "candles":
                candle = dict(data)
                candle["code"] = symbol
                candle["target_code"] = symbol
                candle["ts"] = _timestamp_seconds(
                    candle.get("date", candle.get("time"))
                )
                self._call("stock_candle", candle)
            elif channel == "trades":
                if data.get("isTrial") is True:
                    return
                price = _safe_float(data.get("price"))
                if price <= 0:
                    return
                self._call("stock_tick", {
                    "code": symbol,
                    "target_code": symbol,
                    "close": price,
                    "volume": max(0, _safe_int(data.get("size"))),
                    "total_volume": max(0, _safe_int(data.get("volume"))),
                    "ts": data.get("time"),
                    "bid": _safe_float(data.get("bid")),
                    "ask": _safe_float(data.get("ask")),
                    "serial": data.get("serial"),
                })
        except Exception as exc:
            self._call("stock_error", f"message {type(exc).__name__}")

    def _subscribe_key(self, key: tuple[str, str, bool]) -> None:
        if self.ws is None:
            return
        with self._subscription_lock:
            if key in self._pending_subscriptions:
                return
            self._pending_subscriptions.add(key)
        channel, symbol, after_hours = key
        try:
            self.ws.subscribe({
                "channel": channel,
                "symbol": symbol,
                "afterHours": after_hours,
            })
        except Exception:
            with self._subscription_lock:
                self._pending_subscriptions.discard(key)
            raise

    def _resubscribe_desired(self) -> None:
        with self._subscription_lock:
            desired = list(self._desired_subscriptions)
        for key in desired:
            try:
                self._subscribe_key(key)
            except Exception as exc:
                self._call("error", f"subscribe {key[0]} {type(exc).__name__}")

    def subscribe_contract(
        self,
        contract: FubonContract,
        channels: Iterable[str] = ("trades", "aggregates", "candles"),
    ) -> None:
        symbol = contract.target_code
        self._alias_by_symbol[symbol] = contract.code
        keys = {
            (channel, symbol, after_hours)
            for channel in channels
            for after_hours in (False, True)
        }
        with self._subscription_lock:
            self._desired_subscriptions.update(keys)
        for key in sorted(keys):
            self._subscribe_key(key)

    def _subscribe_stock_key(self, key: tuple[str, str]) -> None:
        if self.stock_ws is None:
            raise FubonMarketDataError("富邦股票 WebSocket 尚未連線")
        with self._stock_subscription_lock:
            if (
                key in self._pending_stock_subscriptions
                or key in self._active_stock_subscriptions
            ):
                return
            self._pending_stock_subscriptions.add(key)
        channel, symbol = key
        try:
            self.stock_ws.subscribe({
                "channel": channel,
                "symbol": symbol,
            })
        except Exception:
            with self._stock_subscription_lock:
                self._pending_stock_subscriptions.discard(key)
            raise

    def _resubscribe_desired_stocks(self) -> None:
        with self._stock_subscription_lock:
            desired = list(self._desired_stock_subscriptions)
        for key in desired:
            try:
                self._subscribe_stock_key(key)
            except Exception as exc:
                self._call(
                    "stock_error", f"subscribe {key[0]} {type(exc).__name__}"
                )

    def subscribe_stock(
        self,
        contract: FubonContract,
        channels: Iterable[str] = ("trades", "candles"),
    ) -> None:
        if contract.security_type != "STK":
            raise FubonMarketDataError("股票訂閱需要 STK 合約")
        keys = {(str(channel), contract.target_code) for channel in channels}
        with self._stock_subscription_lock:
            self._desired_stock_subscriptions.update(keys)
        for key in sorted(keys):
            self._subscribe_stock_key(key)

    def resubscribe_all(self) -> None:
        with self._subscription_lock:
            subscription_ids = sorted(self._subscription_ids)
            self._subscription_ids.clear()
            self._pending_subscriptions.clear()
        if self.ws is not None and subscription_ids:
            try:
                self.ws.unsubscribe({"ids": subscription_ids})
            except Exception as exc:
                self._call("error", f"unsubscribe {type(exc).__name__}")
        self._resubscribe_desired()
        with self._stock_subscription_lock:
            stock_subscription_ids = sorted(self._stock_subscription_ids)
            self._stock_subscription_ids.clear()
            self._pending_stock_subscriptions.clear()
            self._active_stock_subscriptions.clear()
        if self.stock_ws is not None and stock_subscription_ids:
            try:
                self.stock_ws.unsubscribe({"ids": stock_subscription_ids})
            except Exception as exc:
                self._call("stock_error", f"unsubscribe {type(exc).__name__}")
        self._resubscribe_desired_stocks()

    def resolve_contract(self, code: str, force: bool = False) -> Optional[FubonContract]:
        alias = str(code or "").strip().upper()
        if not alias:
            return None
        cached = self._contracts.get(alias)
        if cached and not force and time.monotonic() - cached[0] < 1800:
            return cached[1]
        if self.rest is None:
            return None

        if alias.endswith(("R1", "R2")):
            root = alias[:-2]
            rank = 0 if alias.endswith("R1") else 1
            if root not in self.FUTURES_ROOTS:
                return None
            rows = self._query_tickers(root)
            today = datetime.now(TAIPEI).date()
            active = []
            for row in rows:
                symbol = str(row.get("symbol") or "")
                if not symbol.startswith(root):
                    continue
                end_text = str(row.get("endDate") or row.get("settlementDate") or "")
                try:
                    end_date = datetime.strptime(end_text[:10], "%Y-%m-%d").date()
                except ValueError:
                    continue
                if end_date >= today:
                    active.append((end_date, symbol, row))
            active.sort(key=lambda item: (item[0], item[1]))
            if len(active) <= rank:
                return None
            end_date, symbol, row = active[rank]
        else:
            symbol = alias
            row = self._query_ticker(symbol)
            if not row:
                return None
            end_text = str(row.get("endDate") or row.get("settlementDate") or "")
            try:
                end_date = datetime.strptime(end_text[:10], "%Y-%m-%d").date()
            except ValueError:
                end_date = datetime.now(TAIPEI).date()

        contract = FubonContract(
            code=alias,
            target_code=symbol,
            symbol=symbol,
            name=str(row.get("name") or alias),
            reference=_safe_float(row.get("referencePrice")),
            delivery_date=end_date.isoformat(),
        )
        self._contracts[alias] = (time.monotonic(), contract)
        self._alias_by_symbol[symbol] = alias
        return contract

    def _query_tickers(self, root: str) -> list[dict[str, Any]]:
        merged: dict[str, dict[str, Any]] = {}
        last_error: Optional[Exception] = None
        for session in ("REGULAR", "AFTERHOURS"):
            try:
                response = self.rest.intraday.tickers(
                    type="FUTURE",
                    exchange="TAIFEX",
                    session=session,
                    product=root,
                    contractType="I",
                )
                for row in _response_payload(response).get("data") or []:
                    if isinstance(row, dict) and row.get("symbol"):
                        merged[str(row["symbol"])] = row
            except Exception as exc:
                last_error = exc
        if not merged and last_error:
            raise self._safe_rest_error(last_error, "契約清單")
        return list(merged.values())

    def _query_ticker(self, symbol: str) -> dict[str, Any]:
        last_error: Optional[Exception] = None
        for session in ("REGULAR", "AFTERHOURS"):
            try:
                response = self.rest.intraday.ticker(symbol=symbol, session=session)
                payload = _response_payload(response)
                if payload.get("symbol"):
                    return payload
            except Exception as exc:
                last_error = exc
        if last_error:
            raise self._safe_rest_error(last_error, "契約資料")
        return {}

    def resolve_stock_contract(
        self, code: str, force: bool = False
    ) -> Optional[FubonContract]:
        symbol = str(code or "").strip()
        if not symbol or not symbol.isdigit() or self.stock_rest is None:
            return None
        cached = self._stock_contracts.get(symbol)
        if cached and not force and time.monotonic() - cached[0] < 1800:
            return cached[1]
        try:
            row = _response_payload(
                self.stock_rest.intraday.ticker(symbol=symbol)
            )
        except Exception as exc:
            raise self._safe_rest_error(exc, f"股票 {symbol} 基本資料")
        if str(row.get("symbol") or "") != symbol:
            return None
        contract = FubonContract(
            code=symbol,
            target_code=symbol,
            symbol=symbol,
            name=str(row.get("name") or symbol),
            reference=_safe_float(row.get("referencePrice")),
            security_type="STK",
        )
        self._stock_contracts[symbol] = (time.monotonic(), contract)
        return contract

    def list_contracts(self, root: str = "TXF") -> list[FubonContract]:
        contracts = []
        for row in self._query_tickers(root):
            symbol = str(row.get("symbol") or "")
            if not symbol:
                continue
            contracts.append(FubonContract(
                code=symbol,
                target_code=symbol,
                symbol=symbol,
                name=str(row.get("name") or symbol),
                reference=_safe_float(row.get("referencePrice")),
                delivery_date=str(row.get("endDate") or row.get("settlementDate") or ""),
            ))
        contracts.sort(key=lambda item: (item.delivery_date, item.code))
        return contracts

    def kbars(
        self,
        *,
        contract: FubonContract,
        start: str,
        end: str,
        timeout: int = 30000,
    ) -> FubonKbars:
        del timeout
        if contract.security_type == "STK":
            return self._stock_kbars(contract=contract, start=start, end=end)
        if self.rest is None:
            raise FubonMarketDataError("富邦行情尚未登入")
        start_date = datetime.strptime(start, "%Y-%m-%d").date()
        end_date = datetime.strptime(end, "%Y-%m-%d").date()
        rows_by_ts: dict[int, dict[str, Any]] = {}
        errors = []
        for session in (None, "afterhours"):
            try:
                params = {"symbol": contract.target_code, "timeframe": "1"}
                if session:
                    params["session"] = session
                response = self.rest.intraday.candles(**params)
                for candle in _response_payload(response).get("data") or []:
                    if not isinstance(candle, dict):
                        continue
                    stamp = candle.get("date", candle.get("time"))
                    ts_ns, local_date = _taipei_wallclock_close_ns(stamp)
                    if not ts_ns or local_date is None:
                        continue
                    if local_date < start_date or local_date > end_date:
                        continue
                    close = _safe_float(candle.get("close"))
                    if close <= 0:
                        continue
                    rows_by_ts[ts_ns] = {
                        "ts": ts_ns,
                        "Open": _safe_float(candle.get("open"), close),
                        "High": _safe_float(candle.get("high"), close),
                        "Low": _safe_float(candle.get("low"), close),
                        "Close": close,
                        "Volume": max(0, _safe_int(candle.get("volume"))),
                    }
            except Exception as exc:
                errors.append(exc)
        if not rows_by_ts and errors:
            raise self._safe_rest_error(errors[-1], "日內 K 棒")
        return FubonKbars(rows_by_ts[ts] for ts in sorted(rows_by_ts))

    def _stock_kbars(
        self, *, contract: FubonContract, start: str, end: str
    ) -> FubonKbars:
        if self.stock_rest is None:
            raise FubonMarketDataError("富邦股票行情尚未登入")
        start_date = datetime.strptime(start, "%Y-%m-%d").date()
        end_date = datetime.strptime(end, "%Y-%m-%d").date()
        rows_by_ts: dict[int, dict[str, Any]] = {}
        try:
            response = self.stock_rest.intraday.candles(
                symbol=contract.target_code,
                timeframe="1",
                sort="asc",
            )
            for candle in _response_payload(response).get("data") or []:
                if not isinstance(candle, dict):
                    continue
                stamp = candle.get("date", candle.get("time"))
                ts_ns, local_date = _taipei_wallclock_close_ns(stamp)
                if not ts_ns or local_date is None:
                    continue
                if local_date < start_date or local_date > end_date:
                    continue
                close = _safe_float(candle.get("close"))
                if close <= 0:
                    continue
                rows_by_ts[ts_ns] = {
                    "ts": ts_ns,
                    "Open": _safe_float(candle.get("open"), close),
                    "High": _safe_float(candle.get("high"), close),
                    "Low": _safe_float(candle.get("low"), close),
                    "Close": close,
                    "Volume": max(0, _safe_int(candle.get("volume"))),
                    "Average": _safe_float(candle.get("average"), close),
                }
        except Exception as exc:
            raise self._safe_rest_error(exc, f"股票 {contract.code} 日內 K 棒")
        return FubonKbars(rows_by_ts[ts] for ts in sorted(rows_by_ts))

    def snapshots(self, contracts: Iterable[FubonContract]) -> list[FubonSnapshot]:
        results = []
        for contract in contracts:
            payload = self._quote(contract)
            if payload:
                results.append(FubonSnapshot(payload))
        return results

    def _quote(self, contract: FubonContract) -> dict[str, Any]:
        if contract.security_type == "STK":
            return self._stock_quote(contract)
        now = datetime.now(TAIPEI)
        minute = now.hour * 60 + now.minute
        night_first = minute >= 15 * 60 or minute < 5 * 60
        sessions = ("afterhours", None) if night_first else (None, "afterhours")
        last_error: Optional[Exception] = None
        for session in sessions:
            try:
                params = {"symbol": contract.target_code}
                if session:
                    params["session"] = session
                raw = _response_payload(self.rest.intraday.quote(**params))
                price = _safe_float(raw.get("closePrice") or raw.get("lastPrice"))
                if price <= 0:
                    continue
                reference = _safe_float(raw.get("previousClose"), contract.reference)
                if reference > 0:
                    contract.reference = reference
                total = raw.get("total") or {}
                last_trade = raw.get("lastTrade") or {}
                return {
                    "code": contract.code,
                    "target_code": contract.target_code,
                    "close": price,
                    "open": _safe_float(raw.get("openPrice"), price),
                    "high": _safe_float(raw.get("highPrice"), price),
                    "low": _safe_float(raw.get("lowPrice"), price),
                    "reference": reference,
                    "volume": _safe_int(raw.get("lastSize") or last_trade.get("size")),
                    "total_volume": _safe_int(total.get("tradeVolume")),
                    "ts": raw.get("lastUpdated") or raw.get("closeTime") or last_trade.get("time"),
                }
            except Exception as exc:
                last_error = exc
        if last_error:
            raise self._safe_rest_error(last_error, "即時報價")
        return {}

    def _stock_quote(self, contract: FubonContract) -> dict[str, Any]:
        if self.stock_rest is None:
            raise FubonMarketDataError("富邦股票行情尚未登入")
        try:
            raw = _response_payload(
                self.stock_rest.intraday.quote(symbol=contract.target_code)
            )
        except Exception as exc:
            raise self._safe_rest_error(exc, f"股票 {contract.code} 即時報價")
        price = _safe_float(raw.get("closePrice") or raw.get("lastPrice"))
        if price <= 0:
            return {}
        reference = _safe_float(
            raw.get("previousClose") or raw.get("referencePrice"),
            contract.reference,
        )
        if reference > 0:
            contract.reference = reference
        total = raw.get("total") or {}
        last_trade = raw.get("lastTrade") or {}
        return {
            "code": contract.code,
            "target_code": contract.target_code,
            "close": price,
            "open": _safe_float(raw.get("openPrice"), price),
            "high": _safe_float(raw.get("highPrice"), price),
            "low": _safe_float(raw.get("lowPrice"), price),
            "reference": reference,
            "avg_price": _safe_float(raw.get("avgPrice"), price),
            "volume": _safe_int(raw.get("lastSize") or last_trade.get("size")),
            "total_volume": _safe_int(total.get("tradeVolume")),
            "price_chg": _safe_float(raw.get("change")),
            "change_pct": _safe_float(raw.get("changePercent")),
            "ts": raw.get("lastUpdated") or raw.get("closeTime") or last_trade.get("time"),
        }

    def etf_holdings(
        self,
        *,
        symbol: str,
        start: str,
        end: str,
        sort: str = "asc",
    ) -> dict[str, Any]:
        """Return an ETF's disclosed holdings through the shared stock client.

        The endpoint was added in Fubon Neo 2.2.9.  Keeping it on this facade
        ensures ETF analysis reuses the existing login and market-data session.
        """
        if self.stock_rest is None:
            raise FubonMarketDataError("富邦股票行情尚未登入")
        ownership = getattr(self.stock_rest, "ownership", None)
        request = getattr(ownership, "etf_holdings", None)
        if not callable(request):
            raise FubonMarketDataError(
                f"富邦 SDK {self.version} 不支援 ETF Holdings，最低需要 2.2.9"
            )
        normalized_symbol = str(symbol or "").strip().upper()
        if not normalized_symbol:
            raise FubonMarketDataError("ETF 代號不可為空")
        try:
            datetime.strptime(start, "%Y-%m-%d")
            datetime.strptime(end, "%Y-%m-%d")
        except (TypeError, ValueError) as exc:
            raise FubonMarketDataError("ETF Holdings 日期格式必須為 yyyy-MM-dd") from exc
        if sort not in {"asc", "desc"}:
            raise FubonMarketDataError("ETF Holdings sort 僅支援 asc 或 desc")
        try:
            payload = _response_payload(request(**{
                "symbol": normalized_symbol,
                "from": start,
                "to": end,
                "sort": sort,
            }))
        except Exception as exc:
            raise self._safe_rest_error(exc, f"ETF {normalized_symbol} 持股")
        if not isinstance(payload.get("data"), list):
            raise FubonMarketDataError("富邦 ETF Holdings 回應格式異常（缺少 data 陣列）")
        return payload

    @staticmethod
    def _safe_rest_error(exc: Exception, operation: str) -> FubonMarketDataError:
        status = getattr(exc, "status_code", None)
        if isinstance(exc, FugleAPIError) and status == 429:
            return FubonMarketDataError(f"富邦{operation}超過每分鐘速率限制，請稍後重試")
        suffix = f" HTTP {status}" if status else f" {type(exc).__name__}"
        return FubonMarketDataError(f"富邦{operation}查詢失敗（{suffix.strip()}）")
