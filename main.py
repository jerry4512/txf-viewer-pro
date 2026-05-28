import os
import json
import asyncio
import sqlite3
import threading
import urllib.request
from datetime import datetime, timedelta
from fastapi import FastAPI, WebSocket, WebSocketDisconnect, HTTPException
from fastapi.staticfiles import StaticFiles
from fastapi.responses import FileResponse
from pydantic import BaseModel
import shioaji as sj
import pandas as pd
from dotenv import load_dotenv, set_key
from apscheduler.schedulers.asyncio import AsyncIOScheduler
from apscheduler.triggers.cron import CronTrigger
import screener            # 引入我們的選股大腦
import tomorrow_strategy  # 大盤狀態 × 明日策略選股
import integrated_strategy  # 整合選股（主決策＋籌碼輔助）

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

# 全域 API 實例與狀態
api = sj.Shioaji(simulation=(os.getenv("SHIOAJI_SIMULATION", "False") == "True"))
is_logged_in = False
contract = None
main_loop = None
_kbars_lock = asyncio.Lock()  # Shioaji kbars 不支援並發，全局序列化
_HISTORY_START = datetime(2025, 1, 1)  # 歷史補取目標起點

# 即時 bar 累積（每 1 min flush 進 SQLite，讓歷史圖不需重啟就有今日資料）
_rt_bar: dict = {}
_rt_bar_lock = threading.Lock()
_rt_contract_code: str = None  # 已解析的月份合約代碼，例如 TXFE6

last_snapshot_cache = {
    "open": 0,
    "high": 0,
    "low": 0,
    "close": 0,
    "volume": 0,
    "reference": 0
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
        for connection in self.active_connections:
            try:
                await connection.send_text(message)
            except:
                pass

manager = ConnectionManager()

class LoginRequest(BaseModel):
    api_key: str
    secret_key: str
    person_id: str
    is_simulation: bool
    ca_path: str = ""
    ca_passwd: str = ""
    save_keys: bool = True

# 背景報價抓取協定 (當 WebSocket 失敗時的備案)
async def quote_fallback_loop():
    global api, contract, is_logged_in, main_loop, last_snapshot_cache
    print(f"[{datetime.now().strftime('%H:%M:%S')}] Quote fallback loop started")
    while True:
        try:
            if is_logged_in and api and contract:
                # 取得最新快照 (snapshots 額度很高，每秒一次很安全)
                snaps = api.snapshots([contract])
                if snaps and len(snaps) > 0:
                    snap = snaps[0]
                    price = snap.close
                    # 更新快照快取
                    last_snapshot_cache.update({
                        "open": safe_float(snap.open) if snap.open else last_snapshot_cache.get("open", 0.0),
                        "high": safe_float(snap.high) if snap.high else last_snapshot_cache.get("high", 0.0),
                        "low": safe_float(snap.low) if snap.low else last_snapshot_cache.get("low", 0.0),
                        "close": safe_float(snap.close) if snap.close else last_snapshot_cache.get("close", 0.0),
                        "volume": safe_int(snap.volume) if snap.volume else last_snapshot_cache.get("volume", 0),
                        "reference": safe_float(getattr(contract, 'reference', 0.0))
                    })
                    msg = json.dumps({
                        "type": "tick",
                        "data": { "time": int(datetime.now().timestamp()), "price": float(price) }
                    })
                    # 透過 WebSocket 推送到前端
                    if main_loop:
                        await manager.broadcast(msg)
            
            await asyncio.sleep(0.5) # 提升至每秒同步兩次 (在 50次/5秒 限制內非常安全)
        except Exception as e:
            await asyncio.sleep(5.0)

async def _prefetch_kbars_background():
    """
    登入後背景補取歷史資料至 _HISTORY_START。
    有流量就持續補；遇流量超限立即停止；登出後自動停止。
    """
    global api, contract, is_logged_in

    now_tw = datetime.utcnow() + timedelta(hours=8)
    today = datetime(now_tw.year, now_tw.month, now_tw.day)

    # 夜盤至隔天 05:00 才算完整，以此作為快取截止（Shioaji UTC 編碼）
    if now_tw.hour >= 5:
        cacheable_before = datetime(now_tw.year, now_tw.month, now_tw.day, 5, 0, 0)
    else:
        prev_tw = now_tw - timedelta(days=1)
        cacheable_before = datetime(prev_tw.year, prev_tw.month, prev_tw.day, 5, 0, 0)

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
                        lambda c=kbars_contract, s=s_str, e=e_str: api.kbars(c, start=s, end=e, timeout=30000)
                    )
                if kbars and kbars.ts and len(kbars.ts) > 0:
                    df_new = pd.DataFrame(dict(kbars))
                    saved = _save_to_cache(code, df_new, cacheable_before)
                    cnt = len(saved) if saved else 0
                    print(f"[PREFETCH] {code} {s_str}~{e_str} → {len(df_new)} 筆，存 {cnt} 天")
                else:
                    print(f"[PREFETCH] {code} {s_str}~{e_str} 無資料，標記已確認")
                    _mark_date_range_checked(code, batch_start, current_end)
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
                            lambda c=base_contract, s=s_str, e=e_str: api.kbars(c, start=s, end=e, timeout=30000)
                        )
                    if kbars and kbars.ts and len(kbars.ts) > 0:
                        df_new = pd.DataFrame(dict(kbars))
                        saved = _save_to_cache("TXFR1", df_new, cacheable_before)
                        cnt = len(saved) if saved else 0
                        print(f"[PREFETCH] TXFR1 {s_str}~{e_str} → {len(df_new)} 筆，存 {cnt} 天")
                    else:
                        print(f"[PREFETCH] TXFR1 {s_str}~{e_str} 無資料，標記已確認")
                        _mark_date_range_checked("TXFR1", batch_start, p2_end)
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
    global main_loop, last_snapshot_cache
    try:
        # 自動判斷參數格式 (相容舊版 topic/quote 與新版 exchange/data)
        quote = args[1] if len(args) > 1 else args[0]
        
        # 兼容 dict 與新版 Tick Object
        if isinstance(quote, dict):
            price = quote.get('Close') or quote.get('close') or quote.get('Price') or quote.get('price')
        else:
            price = getattr(quote, 'close', None) or getattr(quote, 'Close', None)
            
        if price and main_loop:
            # 即時動態更新快照快取 (防範 snapshots 在夜盤出錯時的降級備份)
            p_val = float(price)
            last_snapshot_cache["close"] = p_val
            if last_snapshot_cache["open"] == 0:
                last_snapshot_cache["open"] = p_val
            last_snapshot_cache["high"] = max(last_snapshot_cache["high"], p_val)
            last_snapshot_cache["low"] = min(last_snapshot_cache["low"], p_val) if last_snapshot_cache["low"] > 0 else p_val

            # ── 即時 1-min bar 累積 ──────────────────────────────────────
            vol_tick = safe_int(
                quote.get('volume', 0) if isinstance(quote, dict)
                else getattr(quote, 'volume', 0)
            )
            now_unix = int(datetime.now().timestamp())
            bucket_ns = (now_unix // 60) * 60 * 1_000_000_000
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
                            asyncio.run_coroutine_threadsafe(
                                manager.broadcast(json.dumps({"type": "cache_updated"})), main_loop
                            )
                    else:
                        _rt_bar['h'] = max(_rt_bar.get('h', p_val), p_val)
                        _rt_bar['l'] = min(_rt_bar.get('l', p_val), p_val)
                        _rt_bar['c'] = p_val
                        _rt_bar['vol'] = _rt_bar.get('vol', 0) + vol_tick
            # ─────────────────────────────────────────────────────────────

            msg = json.dumps({
                "type": "tick",
                "data": { "time": int(datetime.now().timestamp()), "price": p_val }
            })
            # 在黑視窗印出，作為最終診斷依據
            print(f"[{datetime.now().strftime('%H:%M:%S')}] >>> 收到報價: {p_val}")
            asyncio.run_coroutine_threadsafe(manager.broadcast(msg), main_loop)
    except Exception as e:
        print(f"!!! 報價處理異常: {e}")

@app.post("/api/resubscribe")
async def resubscribe():
    global api, contract, is_logged_in
    print(f"[{datetime.now().strftime('%H:%M:%S')}] 收到手動重新訂閱請求...")
    try:
        if not is_logged_in or api is None:
            return {"status": "error", "message": "伺服器未登入或已斷線，請重新啟動連線"}
        if not contract:
            return {"status": "error", "message": "合約資訊遺失，請嘗試重新登入"}
        
        # 重新強制設定回呼 (雙重保險)
        api.quote.on_quote = global_quote_callback
        api.quote.unsubscribe(contract)
        time.sleep(0.5) # 給予一點緩衝時間
        api.quote.subscribe(contract, quote_type=sj.constant.QuoteType.Tick)
        
        print(f"[{datetime.now().strftime('%H:%M:%S')}] [OK] 已強行重新訂閱 {contract.code}")
        return {"status": "success"}
    except Exception as e:
        print(f"!!! 重新訂閱失敗: {e}")
        return {"status": "error", "message": f"API 異常: {str(e)}"}

@app.on_event("shutdown")
async def shutdown_event():
    global api
    if api:
        try:
            api.logout()
            print("Successfully logged out from Shioaji.")
        except:
            pass

@app.post("/api/login")
async def login(req: LoginRequest):
    global api, is_logged_in, contract, main_loop
    now_str = datetime.now().strftime('%Y-%m-%d %H:%M:%S')
    print(f"\n[{now_str}] [SECURE] 收到系統登入請求 [帳號模式: {'模擬交易 (Simulation)' if req.is_simulation else '實盤交易 (Real)'}]")
    print(f"[{now_str}] [DIR] CA 憑證路徑: {req.ca_path or '未提供'}")
    try:
        main_loop = asyncio.get_running_loop()
        
        # 釋放舊連線
        if api:
            try: 
                print(f"[{now_str}] [RESET] 正在登出舊有 Shioaji 工作階段連線...")
                api.logout()
            except: pass
                
        api = sj.Shioaji(simulation=req.is_simulation)
        
        print(f"[{now_str}] [WAIT] 正在登入永豐金證券伺服器...")
        api.login(api_key=req.api_key, secret_key=req.secret_key)
        print(f"[{now_str}] [SUCCESS] 永豐金證券 API 登入成功！")
        
        # 綁定回呼 (全面兼容新舊版及期貨股票 V1 介面)
        try:
            @api.on_tick_fop_v1()
            def fop_tick_cb(exchange, tick):
                global_quote_callback(exchange, tick)
                
            @api.on_tick_stk_v1()
            def stk_tick_cb(exchange, tick):
                global_quote_callback(exchange, tick)
            print(f"[{now_str}] [WS] Tick FOP/STK V1 即時回呼綁定完成。")
        except Exception as ex:
            print(f"[{now_str}] [WARN] 即時回呼綁定警告: {ex}")
            
        api.quote.on_quote = global_quote_callback
        
        if not req.is_simulation and req.ca_path and req.ca_passwd:
            print(f"[{now_str}] [KEY] 正在啟用 CA 憑證授權...")
            api.activate_ca(ca_path=req.ca_path, ca_passwd=req.ca_passwd, person_id=req.person_id)
            print(f"[{now_str}] [SUCCESS] CA 憑證授權成功！")
            
        contract = api.Contracts.Futures.TXF.TXFR1
        print(f"[{now_str}] [CONTRACT] 預設訂閱合約: {contract.code} (平盤參考價: {getattr(contract, 'reference', '未知')})")
        
        # 嘗試訂閱 WebSocket 報價
        try:
            print(f"[{now_str}] [WS] 正在向永豐金 WebSocket 伺服器訂閱 {contract.code} 即時報價...")
            api.quote.subscribe(contract, quote_type=sj.constant.QuoteType.Tick)
            print(f"[{now_str}] [SUCCESS] {contract.code} WebSocket 報價訂閱成功！")
        except Exception as eSub:
            print(f"[{now_str}] [ERROR] WebSocket 訂閱受限 ({eSub})，將啟用背景同步模式。")

        # 等待 Shioaji SDK 完成內部初始化，避免後續 kbars/snapshot 呼叫拿到空值
        print(f"[{now_str}] [WAIT] 等待 SDK 穩定中（3秒）...")
        await asyncio.sleep(3)

        is_logged_in = True
        # 登入完成後立刻在背景補快取，不阻塞前端
        asyncio.create_task(_prefetch_kbars_background())
        print(f"[{now_str}] [READY] 系統完全就緒，連線就緒開始看盤！\n")
        return {"status": "success", "contract": contract.code}
    except Exception as e:
        is_logged_in = False
        print(f"[{now_str}] [ERROR] 登入失敗！異常訊息: {e}\n")
        raise HTTPException(status_code=400, detail=str(e))

@app.post("/api/select_contract")
async def select_contract(req: dict):
    global api, is_logged_in, contract, _rt_contract_code, _rt_bar
    now_str = datetime.now().strftime('%H:%M:%S')
    if not is_logged_in or not api:
        print(f"[{now_str}] [WARN] 收到切換合約請求，但目前為「未登入」狀態！")
        return {"status": "error", "message": "請先登入連線"}
    
    code = req.get("code", "TXFR1")
    old_contract = contract
    print(f"\n[{now_str}] [CHANGE] 收到切換合約請求: {old_contract.code if old_contract else 'None'} -> {code}")
    
    try:
        # 先解除舊訂閱 (如果有)
        if old_contract:
            try:
                print(f"[{now_str}] [WS] 正在解除舊合約訂閱: {old_contract.code}")
                api.quote.unsubscribe(old_contract)
            except Exception as eUnsub:
                print(f"[{now_str}] [WARN] 解除訂閱舊合約警告: {eUnsub}")
                
        # 根據代碼找到正確的合約對象
        if "TXF" in code:
            contract = api.Contracts.Futures.TXF[code]
        elif "MXF" in code:
            contract = api.Contracts.Futures.MXF[code]
        elif "TMF" in code:
            contract = api.Contracts.Futures.TMF[code]
        elif code.isdigit() or len(code) == 4:
            contract = api.Contracts.Stocks[code]
        else:
            print(f"[{now_str}] [ERROR] 不支援的合約代碼: {code}")
            return {"status": "error", "message": "不支援的合約代碼"}
            
        # 重新訂閱新合約
        print(f"[{now_str}] [WS] 正在向永豐金訂閱新合約 {contract.code} 即時 Tick 報價...")
        api.quote.subscribe(contract, quote_type=sj.constant.QuoteType.Tick)
        print(f"[{now_str}] [SUCCESS] 新合約 {contract.code} 訂閱完成！")
        
        # 切換合約時重置即時快取狀態
        with _rt_bar_lock:
            _rt_bar.clear()
        _rt_contract_code = None

        print(f"[{now_str}] [OK] 合約順利切換完成。\n")
        return {"status": "success", "contract": contract.code}
    except Exception as e:
        print(f"[{now_str}] [ERROR] 合約切換失敗: {e}\n")
        return {"status": "error", "message": str(e)}

@app.get("/api/status")
async def get_status():
    global is_logged_in, contract
    return {
        "logged_in": is_logged_in,
        "contract": contract.code if contract else None,
        "env": {
            "api_key": os.getenv("SHIOAJI_API_KEY", ""),
            "secret_key": os.getenv("SHIOAJI_SECRET_KEY", ""),
            "person_id": os.getenv("SHIOAJI_PERSON_ID", ""),
            "is_simulation": os.getenv("SHIOAJI_SIMULATION", "False") == "True",
            "ca_path": os.getenv("SHIOAJI_CA_PATH", ""),
            "ca_passwd": os.getenv("SHIOAJI_CA_PASSWD", "")
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

def _load_from_cache(contract_code: str, start: datetime, end: datetime) -> pd.DataFrame:
    start_ns = int(start.timestamp() * 1e9)
    end_ns   = int((end + timedelta(days=1)).timestamp() * 1e9)
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
    """將 df 中早於 cacheable_before 的資料存入快取，並記錄已快取日期。"""
    df_ts = pd.to_datetime(df['ts'], unit='ns', utc=True)
    df = df.copy()
    df['_date'] = df_ts.dt.date
    # 夜盤尾段 (Shioaji UTC 00:00~04:59 = 台灣時間 00:00~04:59) 屬前一交易日
    night_mask = df_ts.dt.hour < 5
    df.loc[night_mask, '_date'] = (
        df_ts[night_mask].dt.normalize() - pd.Timedelta(days=1)
    ).dt.date
    mask = df_ts < pd.Timestamp(cacheable_before, tz='UTC')
    df_save = df[mask]
    if df_save.empty:
        return
    new_dates = [str(d) for d in df_save['_date'].unique()]
    with sqlite3.connect(_KBARS_CACHE_DB) as conn:
        conn.executemany(
            "INSERT OR REPLACE INTO kbars1m VALUES (?,?,?,?,?,?,?)",
            [(contract_code, int(r.ts), r.Open, r.High, r.Low, r.Close, int(r.Volume))
             for r in df_save.itertuples()]
        )
        conn.executemany(
            "INSERT OR REPLACE INTO cached_dates VALUES (?,?)",
            [(contract_code, d) for d in new_dates]
        )
    return new_dates

def _mark_date_range_checked(contract_code: str, start: datetime, end: datetime):
    """將日期範圍內所有日曆天標記為已確認（無論有無資料），防止補取重複空跑。"""
    dates = []
    d = start
    while d <= end:
        dates.append((contract_code, d.strftime('%Y-%m-%d')))
        d += timedelta(days=1)
    if dates:
        with sqlite3.connect(_KBARS_CACHE_DB) as conn:
            conn.executemany(
                "INSERT OR IGNORE INTO cached_dates VALUES (?,?)",
                dates
            )


def _save_rt_bar_to_db(code: str, ts_ns: int, o: float, h: float, l: float, c: float, vol: int):
    """即時 1-min bar 寫入 kbars1m，不更新 cached_dates（今日仍視為未完整快取）。
    ts_ns 為真實 UTC epoch ns（來自 datetime.now().timestamp()），
    寫入時補 +8h 偏移以符合 Shioaji UTC+8-biased 格式，
    確保 get_kbars 統一的 -28800 校正後顯示正確。
    """
    biased_ts_ns = ts_ns + 28800 * 1_000_000_000
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

# Shioaji 期貨月份字母：A=1月, B=2月, ..., L=12月
_MONTH_LETTERS = 'ABCDEFGHIJKL'

def _resolve_kbars_contracts(api_instance, base_contract, start_date, end_date, now_str: str) -> list:
    """
    TXFR1/R2 滾動合約無法直接查 kbars，需展開為月份合約（如 TXFE6 = 2026年5月）。
    Shioaji 命名格式：{商品}{月份字母}{年份末位}，例如 TXFE6。
    非滾動合約直接回傳原合約。
    """
    code = base_contract.code
    if not (code.endswith('R1') or code.endswith('R2')):
        return [base_contract]

    base_symbol = code[:-2]  # "TXFR1" → "TXF"

    # 計算需查詢的月份（含 start 前一個月，以涵蓋換月緩衝）
    if start_date.month > 1:
        sy, sm = start_date.year, start_date.month - 1
    else:
        sy, sm = start_date.year - 1, 12
    ey, em = end_date.year, end_date.month

    months = []
    cy, cm = sy, sm
    while (cy, cm) <= (ey, em):
        months.append((cy, cm))
        cy, cm = (cy, cm + 1) if cm < 12 else (cy + 1, 1)

    try:
        futures_cat = getattr(api_instance.Contracts.Futures, base_symbol)
    except Exception:
        print(f"[{now_str}] [WARN] Contracts.Futures.{base_symbol} 無法存取，回退使用 {code}")
        return [base_contract]

    result = []
    for (y, m) in months:
        c_code = f"{base_symbol}{_MONTH_LETTERS[m - 1]}{y % 10}"  # e.g. TXFE6
        # 優先用 attribute access（與 api.Contracts.Futures.TXF.TXFE6 等效）
        c = getattr(futures_cat, c_code, None)
        if c is None:
            # fallback：bracket access
            try:
                c = futures_cat[c_code]
            except Exception:
                c = None
        if c is not None:
            print(f"[{now_str}] [CHART]   合約 {c_code} ✓ type={type(c).__name__} code={getattr(c, 'code', '?')}")
            result.append(c)
        else:
            print(f"[{now_str}] [CHART]   合約 {c_code} 不存在（已到期或未上市），跳過")

    if not result:
        print(f"[{now_str}] [WARN] 找不到任何月份合約，回退使用 {code}")
        return [base_contract]

    print(f"[{now_str}] [CHART] 滾動合約 {code} → 查詢清單: [{', '.join(c.code for c in result)}]")
    return result


@app.get("/api/kbars")
async def get_kbars(start: str, end: str, period: str = "1min"):
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
    safe_end_date = now_tw.date()
    # 夜盤至隔天 05:00 才算完整，以此作為快取截止（Shioaji UTC 編碼）
    if now_tw.hour >= 5:
        safe_end = datetime(safe_end_date.year, safe_end_date.month, safe_end_date.day, 5, 0, 0)
    else:
        # 05:00 前仍在夜盤中，前天夜盤才算完整
        prev = (now_tw - timedelta(days=1)).date()
        safe_end = datetime(prev.year, prev.month, prev.day, 5, 0, 0)

    try:
        kbars_contracts = _resolve_kbars_contracts(api, contract, start_date, end_date, now_str)
        print(f"\n[{now_str}] [CHART] 歷史 K 線索取請求 -> 合約: {[c.code for c in kbars_contracts]} | 區間: {start} 至 {end} | 週期: {period}")

        max_days = 365 if period == "D" else 60
        if (end_date - start_date).days > max_days:
            adjusted_start = end_date - timedelta(days=max_days)
            print(f"[{now_str}] [WARN] 請求天數大於單次最大限制 ({max_days} 天)，自動縮減起點為 {adjusted_start.strftime('%Y-%m-%d')}")
            start_date = adjusted_start

        all_df = []
        # safe_end 當天資料可能未完整，不快取；之前的都是固定歷史資料可以快取
        cacheable_before = safe_end  # ts < cacheable_before 才存快取

        # 更新即時快取目標合約（取最新月份）
        if kbars_contracts:
            global _rt_contract_code, _rt_bar
            new_rt_code = kbars_contracts[-1].code
            if _rt_contract_code != new_rt_code:
                with _rt_bar_lock:
                    _rt_bar.clear()
                _rt_contract_code = new_rt_code

        # 05:00 前仍在夜盤：前一日夜盤尾段可能尚未快取，強制重新補取
        force_refetch_date = None
        if now_tw.hour < 5:
            force_refetch_date = (now_tw - timedelta(days=1)).strftime('%Y-%m-%d')

        for kbars_contract in kbars_contracts:
            code = kbars_contract.code
            cached_dates = _get_cached_dates(code)

            # 找出哪些日期尚未快取，需要打 API
            uncached = []
            d = start_date
            while d <= end_date:
                date_str = d.strftime('%Y-%m-%d')
                if date_str not in cached_dates or date_str == force_refetch_date:
                    uncached.append(d)
                d += timedelta(days=1)

            # 從快取載入已有的資料
            cached_df = _load_from_cache(code, start_date, end_date)
            if not cached_df.empty:
                print(f"[{now_str}] [CACHE] {code} 快取命中 {len(cached_df)} 筆")
                all_df.append(cached_df)

            if not uncached:
                print(f"[{now_str}] [CACHE] {code} 全區間已快取，略過 API")
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
                        kbars = await loop.run_in_executor(
                            None,
                            lambda c=kbars_contract, s=s_str, e=e_str: api.kbars(c, start=s, end=e, timeout=30000)
                        )
                except Exception as api_err:
                    err_type = type(api_err).__name__
                    err_msg  = str(api_err)
                    is_quota = any(k in err_msg.lower() for k in ("quota", "limit", "usage", "exceed", "流量", "請求次數"))
                    tag = "[QUOTA]" if is_quota else "[API-ERR]"
                    print(f"[{now_str}]  ↳ {tag} {code} API 呼叫失敗 ({err_type}): {err_msg}")
                    current_start = current_end + timedelta(days=1)
                    continue

                if kbars and kbars.ts and len(kbars.ts) > 0:
                    df_new = pd.DataFrame(dict(kbars))
                    print(f"[{now_str}]  ↳ [OK] 取得 {len(df_new)} 筆")
                    all_df.append(df_new)
                    saved = _save_to_cache(code, df_new, cacheable_before)
                    if saved:
                        print(f"[{now_str}]  ↳ [CACHE] 已存快取 {len(saved)} 天：{saved[0]} ~ {saved[-1]}")
                    else:
                        print(f"[{now_str}]  ↳ [SKIP-CACHE] {code} 此批次資料全屬今日（不快取），已合併至回傳資料")
                else:
                    ts_len = len(kbars.ts) if kbars and kbars.ts is not None else "N/A"
                    kbars_repr = repr(kbars)[:200] if kbars else "None"
                    print(f"[{now_str}]  ↳ [EMPTY] {code} {s_str}~{e_str} API 回傳空白")
                    print(f"[{now_str}]           kbars={kbars_repr} | ts筆數={ts_len}")

                current_start = current_end + timedelta(days=1)

        # TXFR1 直查快取補充（涵蓋月份合約查不到的舊資料）
        orig_code = contract.code if contract else ""
        if orig_code.endswith('R1') or orig_code.endswith('R2'):
            txfr1_df = _load_from_cache("TXFR1", start_date, end_date)
            if not txfr1_df.empty:
                print(f"[{now_str}] [CACHE] TXFR1 補充快取命中 {len(txfr1_df)} 筆")
                all_df.append(txfr1_df)

        # 月份合約展開後仍無資料 → 回退用原始合約（TXFR1 等）直接查詢
        if not all_df and len(kbars_contracts) > 0 and kbars_contracts[0].code != contract.code:
            print(f"[{now_str}] [CHART] 月份合約皆無資料，改用原始合約 {contract.code} 直接重試...")
            try:
                s_str = start_date.strftime('%Y-%m-%d')
                e_str = end_date.strftime('%Y-%m-%d')
                fb_loop = asyncio.get_running_loop()
                async with _kbars_lock:
                    fb_kbars = await fb_loop.run_in_executor(
                        None,
                        lambda: api.kbars(contract, start=s_str, end=e_str, timeout=30000)
                    )
                if fb_kbars and fb_kbars.ts and len(fb_kbars.ts) > 0:
                    df_fb = pd.DataFrame(dict(fb_kbars))
                    print(f"[{now_str}] [CHART] 原始合約取得 {len(df_fb)} 筆，直接使用。")
                    all_df.append(df_fb)
                else:
                    print(f"[{now_str}] [CHART] 原始合約亦無資料。")
            except Exception as efb:
                print(f"[{now_str}] [CHART] 原始合約重試失敗：{efb}")

        if not all_df:
            print(f"[{now_str}] [STOP] 查詢結束：所有批次均無返回任何歷史數據，回傳空清單。\n")
            return []

        # keep='last'：重疊期間保留較新合約資料
        df = pd.concat(all_df).drop_duplicates(subset=['ts'], keep='last')
        df['ts'] = pd.to_datetime(df['ts'], unit='ns', utc=True)
        df.set_index('ts', inplace=True)
        df.sort_index(inplace=True)

        original_len = len(df)

        if period != "1min":
            p_map = {"5min": "5min", "15min": "15min", "30min": "30min", "60min": "60min", "D": "D"}
            resample_p = p_map.get(period, period)
            print(f"[{now_str}] [PROCESS] 正在進行 K 線週期聚合：將 1min 數據 ({original_len} 筆) 聚合為 {resample_p}...")
            df = df.resample(resample_p).agg({
                'Open': 'first', 'High': 'max', 'Low': 'min', 'Close': 'last', 'Volume': 'sum'
            }).dropna()

        if df.empty:
            print(f"[{now_str}] [STOP] 聚合後無任何有效資料欄位，回傳空清單。\n")
            return []

        df.reset_index(inplace=True)
        # 永豐金原始數據帶有 8 小時偏移，手動減去 (28800秒) 以對齊 UTC
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

    # 今日 UTC 範圍
    now_utc = datetime.utcnow()
    day_start_ns = int(datetime(now_utc.year, now_utc.month, now_utc.day).timestamp() * 1e9)
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
        snap = api.snapshots([contract])[0]
        last_snapshot_cache.update({
            "open": safe_float(snap.open) if snap.open else last_snapshot_cache.get("open", 0.0),
            "high": safe_float(snap.high) if snap.high else last_snapshot_cache.get("high", 0.0),
            "low": safe_float(snap.low) if snap.low else last_snapshot_cache.get("low", 0.0),
            "close": safe_float(snap.close) if snap.close else last_snapshot_cache.get("close", 0.0),
            "volume": safe_int(snap.volume) if snap.volume else last_snapshot_cache.get("volume", 0),
            "reference": safe_float(getattr(contract, 'reference', 0.0))
        })
        # 偶爾輸出一行，避免過度洗板
        if datetime.now().second % 15 == 0:
            print(f"[{now_str}] [SNAPSHOT] 成功抓取最新個股/期貨快照 -> 收盤價: {last_snapshot_cache['close']} | 累計量: {last_snapshot_cache['volume']}")
    except Exception as e:
        print(f"[{now_str}] [WARN] snapshots 抓取失敗，採用降級機制: {e}")
        # 如果快取中沒有任何收盤價，我們以合約的 reference（基準價/平盤價）作為所有價格的初始值！
        if last_snapshot_cache.get("close", 0.0) == 0.0:
            ref = safe_float(getattr(contract, 'reference', 0.0))
            print(f"[{now_str}]  ↳ ℹ️ 快取無先前紀錄，已採用合約參考平盤價初始化數值: {ref}")
            last_snapshot_cache.update({
                "open": ref,
                "high": ref,
                "low": ref,
                "close": ref,
                "volume": 0,
                "reference": ref
            })
    return last_snapshot_cache

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
    # 與 Shioaji 用「UTC+8 時間直接當 UTC 儲存」的格式一致
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
    # Shioaji 時間戳為 UTC+8 編碼（以 UTC 解析即為台灣時間）
    df_ts = pd.to_datetime(df['ts'], unit='ns', utc=True)

    # 交易日 session 定義（對應 TAIFEX 官方日 K 慣例）：
    # 前一日 15:00（夜盤）→ 當日 13:45（日盤）
    # 實作：h>=15 的K棒歸入「次日」，h<15 保留原日期
    df['_date'] = df_ts.dt.date
    evening_mask = df_ts.dt.hour >= 15
    df.loc[evening_mask, '_date'] = (
        df_ts[evening_mask].dt.normalize() + pd.Timedelta(days=1)
    ).dt.date

    if period == "week":
        df['_group'] = pd.to_datetime(df['_date'].astype(str)).dt.to_period('W')
    elif period == "month":
        df['_group'] = pd.to_datetime(df['_date'].astype(str)).dt.to_period('M')
    else:
        df['_group'] = df['_date']

    # 每組震幅 = 最高 - 最低
    # MIN_BARS=400：過濾週一僅日盤的 ~270 根殘留組，同時保留週六夜盤延伸組(831根)
    MIN_BARS = 400 if period == "day" else 1
    grp = df.groupby('_group').agg(
        grp_high=('High', 'max'), grp_low=('Low', 'min'), bar_count=('ts', 'count')
    )
    grp = grp[grp['bar_count'] >= MIN_BARS]
    grp['amplitude'] = grp['grp_high'] - grp['grp_low']
    grp = grp.sort_index()

    today = now_tw.date()
    if period == "day":
        hist = grp[grp.index < today].tail(20)
        current_period_row = df[df['_group'] == today]
    else:
        hist = grp.iloc[:-1].tail(20) if len(grp) > 1 else grp
        current_period_row = df[df['_group'] == grp.index[-1]] if len(grp) > 0 else pd.DataFrame()

    if hist.empty:
        return {"error": "insufficient_data", "amp_max": None, "amp_large": None,
                "amp_avg": None, "amp_small": None, "amp_min": None, "amp_today": None, "days": 0}

    amps = hist['amplitude'].values.astype(float)
    amp_min   = float(np.min(amps))
    amp_max   = float(np.max(amps))
    amp_avg   = float(np.mean(amps))
    # 大大震幅 = (平均 + 最大) ÷ 2；小小震幅 = (平均 + 最小) ÷ 2
    amp_large = (amp_avg + amp_max) / 2
    amp_small = (amp_avg + amp_min) / 2

    # 本日/本週/本月震幅：從快取取當前進行中 session 的高低
    amp_today: float | None = None
    if period == "day":
        # h>=15→次日 架構下，夜盤時段（h>=15）的K棒歸屬「明日」group
        now_hour = now_tw.hour
        if now_hour >= 15:
            tomorrow = (now_tw + timedelta(days=1)).date()
            live_session_row = df[df['_group'] == tomorrow]
        else:
            live_session_row = current_period_row
        if not live_session_row.empty:
            cache_amp = float(live_session_row['High'].max()) - float(live_session_row['Low'].min())
            amp_today = cache_amp if cache_amp > 0 else None
    elif not current_period_row.empty:
        cache_amp = float(current_period_row['High'].max()) - float(current_period_row['Low'].min())
        amp_today = cache_amp if cache_amp > 0 else None

    if period == "day" and is_logged_in:
        snap_high = last_snapshot_cache.get("high", 0)
        snap_low  = last_snapshot_cache.get("low", 0)
        if snap_high > 0 and snap_low > 0:
            # 即時快照覆蓋 DB 值（快照含最新 tick，DB 以分 K 為主）
            amp_today = snap_high - snap_low

    return {
        "amp_max":   round(amp_max),
        "amp_large": round(amp_large),
        "amp_avg":   round(amp_avg),
        "amp_small": round(amp_small),
        "amp_min":   round(amp_min),
        "amp_today": round(amp_today) if amp_today is not None else None,
        "days":      len(amps),
        "period":    period,
    }


@app.get("/api/institutional_rankings")
async def get_institutional_rankings():
    import urllib.request
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
        raise HTTPException(status_code=500, detail="無法取得證交所三大法人數據，請稍後再試。")

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
    for row in raw_rows:
        code = row[code_idx].strip()
        name = row[name_idx].strip()
        
        # 轉為張數 (股數 / 1000)
        foreign_net = parse_int(row[foreign_idx]) // 1000
        it_net = parse_int(row[it_idx]) // 1000
        dealer_net = parse_int(row[dealer_idx]) // 1000
        total_net = parse_int(row[total_idx]) // 1000
        
        # 只過濾有實質買賣超的股票，過濾掉權證(長代號)或ETF(如果代號開頭為非數字，可留作彈性)
        if len(code) > 6: # 排除權證、可轉債等
            continue
            
        processed_list.append({
            "code": code,
            "name": name,
            "foreign": foreign_net,
            "it": it_net,
            "dealer": dealer_net,
            "total": total_net
        })

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
    """組成 Telegram 訊息文字（含產業摘要、市場狀態與個股建議買法）"""
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
            f"📊 今日市場狀態：{ms_emoji} *{ms_label}*\n"
            f"大盤：{m.get('index_close',0):,.0f}  20MA：{m.get('index_ma20',0):,.0f}  60MA：{m.get('index_ma60',0):,.0f}\n"
            f"距20MA：{m.get('bias_ma20_pct',0):+.1f}%  距60MA：{m.get('bias_ma60_pct',0):+.1f}%  "
            f"過熱個股：{m.get('hot_stock_ratio',0):.0f}%\n"
            f"操作原則：{ms_suggest}\n"
            f"{'─'*28}"
        )
    lines.append(f"🕐 {now_str}")

    top3 = ind_rankings[:3]
    if top3:
        medals = ["🥇", "🥈", "🥉"]
        ind_lines = ["🏭 *今日強勢產業*"]
        for i, ind in enumerate(top3):
            ind_lines.append(
                f"{medals[i]} {ind['industryName']}　"
                f"分數 {ind['industryScore']}　"
                f"候選 {ind['candidateCount']} 檔"
            )
        ind_lines.append('─' * 28)
        lines.append("\n".join(ind_lines))

    lines.append(f"📊 *{label} 交易清單*")

    # ── 個股區塊 ──
    for s in stocks:
        code  = s.get("stockCode") or s.get("code", "?")
        name  = s.get("stockName") or s.get("name", "?")
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
            tags = "  ".join(f"#{f}" for f in major_features)
            major_line = f"⭐ 主力特徵｜{tags}\n"

        # 產業標註行
        ind_info = ind_lookup.get(code, {})
        ind_name  = ind_info.get("name", s.get("industry", ""))
        ind_score = ind_info.get("score", 0)
        resonance = ind_info.get("resonance", False)
        if ind_name:
            resonance_tag = "  🔥 產業共振" if resonance else ""
            ind_line = f"🏭 {ind_name}  ｜ 產業分數 {ind_score}{resonance_tag}\n"
        else:
            ind_line = ""

        state      = s.get("strategyState", "")
        stock_emoji = "🔵" if state == "觀察中" else "🟢"
        state_tag   = "  〔觀察中〕" if state == "觀察中" else ""
        block = (
            f"\n{stock_emoji} *#{code} {name}*  ｜ 分數 {score}{state_tag}\n"
            f"{ind_line}"
            f"💰 收盤 {price:.2f}  ｜ 乖離 {bias_sign}{bias}%  ｜ 20日強度 {r20_sign}{r20}%\n"
            f"👥 法人佔比 {inst:.2f}%  ｜ 停損價 {sl_text}\n"
            f"{major_line}"
        )
        # 建議買法（market status aware）
        bm = s.get("buy_method") or {}
        if bm:
            bm_allowed = bm.get("allowed", True)
            bm_label   = bm.get("label", "")
            bm_entry   = bm.get("entry_condition", "")
            bm_sl_rule = bm.get("stop_loss_rule", "")
            allow_tag  = "✅" if bm_allowed else "🚫"
            block += (
                f"\n{allow_tag} *建議買法*：{bm_label}\n"
                f"進場條件：{bm_entry}\n"
                f"停損規則：{bm_sl_rule}\n"
            )
        if plan.get("conservative"):
            block += f"\n📌 *保守進場*\n{plan['conservative']}\n"
        if plan.get("aggressive"):
            block += f"\n🚀 *積極進場*\n{plan['aggressive']}\n"
        if plan.get("avoid"):
            block += f"\n⚠️ *不進場條件*\n{plan['avoid']}\n"
        if plan.get("stopLoss"):
            block += f"\n🛡 *停損條件*\n{plan['stopLoss']}\n"
        block += f"{'─'*28}"
        lines.append(block)

    if total and total > len(stocks):
        lines.append(f"\n前 *{len(stocks)}* 名 ／ 共 *{total}* 檔候選")
    else:
        lines.append(f"\n共 *{len(stocks)}* 檔候選")
    return "\n".join(lines)

_TG_MAX_LEN = 4000  # Telegram 上限 4096，留緩衝

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

def _tg_post(url: str, chat_id: str, text: str) -> tuple[bool, str]:
    """送出單則訊息，回傳 (success, error_str)"""
    body = json.dumps({"chat_id": chat_id, "text": text, "parse_mode": "Markdown"}).encode("utf-8")
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
                id         INTEGER PRIMARY KEY AUTOINCREMENT,
                chat_id    TEXT NOT NULL UNIQUE,
                name       TEXT,
                enabled    INTEGER DEFAULT 1,
                created_at TEXT,
                updated_at TEXT
            )
        """)
        conn.commit()
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
                        "INSERT OR IGNORE INTO telegram_targets (chat_id, name, enabled, created_at, updated_at) VALUES (?,?,1,?,?)",
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

def _send_tg_with_targets(message: str, targets: list) -> dict:
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
            success, err = _tg_post(url, chat_id, part)
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
    """從整合選股結果中挑選 TG 精選（最多3）與備選（最多2）"""
    buy = integrated_result.get("buy_candidates", [])
    tg_picks, tg_watch, tg_skipped = [], [], []

    for s in buy:
        grade  = s.get("stock_grade", "")
        dist   = abs(s.get("dist_cost20_pct") or 999)
        sl_abs = abs(s.get("stop_loss_pct") or 0)
        rr     = s.get("risk_reward") or 0
        macd   = s.get("macd_status", "")

        if grade != "A" or dist > 3 or rr < 1.5 or sl_abs > 5.5 or macd == "負柱擴大":
            tg_skipped.append(s)
            continue

        tg_warning = "風報比偏高，需複查停損與目標" if rr > 8 else ""
        tg_score   = calculate_tg_score(s)
        s_copy = dict(s)
        s_copy["tg_score"]   = tg_score
        s_copy["tg_warning"] = tg_warning

        if sl_abs <= 4 and macd in ("負柱收斂", "正柱放大"):
            tg_picks.append(s_copy)
        else:
            tg_watch.append(s_copy)

    tg_picks.sort(key=lambda x: -x.get("tg_score", 0))
    tg_watch.sort(key=lambda x: -x.get("tg_score", 0))

    return {
        "tg_picks":   tg_picks[:3],
        "tg_watch":   tg_watch[:2],
        "tg_skipped": tg_skipped,
    }


def get_today_strong_industries_from_result(integrated_result: dict) -> list:
    """從整合選股結果中計算今日強勢產業（分數≥60，最多5名）"""
    from collections import defaultdict
    all_stocks = (
        integrated_result.get("buy_candidates", []) +
        integrated_result.get("high_priority_watch", []) +
        integrated_result.get("wait_pullback", []) +
        integrated_result.get("other_watch", [])
    )
    groups: dict = defaultdict(list)
    for s in all_stocks:
        ind = (s.get("industry") or "").strip() or "其他"
        groups[ind].append(s)

    rankings = []
    for ind_name, stocks in groups.items():
        if not stocks:
            continue
        ind_score = stocks[0].get("industry_score", 0) or 0
        status    = stocks[0].get("industry_status", "") or ""
        if ind_score < 60:
            continue
        buy_stocks  = [s for s in stocks if s.get("final_category") == "buy_candidates"]
        top_stocks  = [f"{s['stock_id']} {s['stock_name']}" for s in buy_stocks[:3]]
        rankings.append({
            "rank":            0,
            "industry":        ind_name,
            "score":           ind_score,
            "status":          status,
            "candidate_count": len(stocks),
            "top_stocks":      top_stocks,
        })

    rankings.sort(key=lambda x: -x["score"])
    for i, r in enumerate(rankings, 1):
        r["rank"] = i
    return rankings[:5]


def format_tg_integrated_message(data_date: str, market_regime: dict, tg_list: dict, strong_industries: list) -> str:
    """組成整合選股 TG 訊息（精選≤3、備選≤2、強勢產業≤5）"""
    now_str   = datetime.now().strftime("%Y-%m-%d %H:%M")
    mr        = market_regime or {}
    mr_label  = mr.get("label", "正常多頭")
    mr_status = mr.get("status", "normal_bull")
    mr_emoji  = {"normal_bull": "🟢", "hot_bull": "🟡", "overheated_bull": "🔴", "weak_market": "⚪"}.get(mr_status, "📊")

    tg_picks = tg_list.get("tg_picks", [])
    tg_watch = tg_list.get("tg_watch", [])
    SEP      = "─" * 28

    lines = [f"📌 *{data_date} 明日精選股*"]
    lines.append(f"大盤狀態：{mr_emoji} {mr_label}")
    lines.append(f"操作原則：A級、距cost20≤3%、停損≤4%、MACD收斂或正柱放大")
    lines.append(SEP)

    if strong_industries:
        lines.append("🏭 *今日強勢產業*")
        for ind in strong_industries:
            top = "、".join(ind["top_stocks"][:2]) if ind.get("top_stocks") else ""
            lines.append(f"{ind['rank']}. {ind['industry']}｜分數 {ind['score']}｜{ind['status']}｜候選 {ind['candidate_count']} 檔")
            if top:
                lines.append(f"   代表：{top}")
    else:
        lines.append("🏭 今日無明顯強勢產業")
    lines.append(SEP)

    if tg_picks:
        lines.append(f"🔥 *明日精選 {len(tg_picks)} 檔*")
        num_emojis = ["1️⃣", "2️⃣", "3️⃣"]
        for i, s in enumerate(tg_picks):
            industry = s.get("industry") or "未分類"
            close_p  = s.get("close", 0)
            dist     = s.get("dist_cost20_pct") or 0
            sl_price = s.get("stop_price", 0)
            sl_pct   = s.get("stop_loss_pct") or 0
            rr       = s.get("risk_reward") or 0
            macd     = s.get("macd_status", "")
            inst_sum = s.get("institution_5d_status", "")
            warning  = s.get("tg_warning", "")
            num      = num_emojis[i] if i < 3 else f"{i+1}."
            lines.append(f"\n{num} *{s['stock_id']} {s['stock_name']}*｜{industry}")
            lines.append(f"現價 {close_p}｜距cost20 {'+' if dist>=0 else ''}{dist:.1f}%｜停損 {sl_price}（{sl_pct:.1f}%）")
            lines.append(f"MACD：{macd}｜風報比 {rr:.1f}｜法人：{inst_sum}")
            lines.append(f"建議：A級股，距cost20 {dist:.1f}%，停損{sl_pct:.1f}%，MACD{macd}。")
            if warning:
                lines.append(f"⚠️ {warning}")
    else:
        lines.append("🔥 今日無符合 TG 精選條件的明日可買")
    lines.append(SEP)

    if tg_watch:
        lines.append(f"👀 *備選 {len(tg_watch)} 檔*")
        for s in tg_watch:
            industry = s.get("industry") or "未分類"
            sl_pct   = s.get("stop_loss_pct") or 0
            reason   = s.get("final_reason") or ""
            lines.append(f"{s['stock_id']} {s['stock_name']}｜{industry}")
            lines.append(f"分數 {s.get('final_score', 0)}｜停損 {sl_pct:.1f}% 偏大")
            if reason:
                lines.append(f"原因：{reason[:60]}")
    lines.append(SEP)
    lines.append(f"🕐 {now_str}")
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
    chat_id = str(payload.get("chat_id", "")).strip()
    name    = str(payload.get("name", "")).strip() or chat_id
    if not chat_id:
        raise HTTPException(status_code=400, detail="chat_id 不可為空")
    now  = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
    conn = _get_tg_db_conn()
    try:
        conn.execute(
            "INSERT OR IGNORE INTO telegram_targets (chat_id, name, enabled, created_at, updated_at) VALUES (?,?,1,?,?)",
            (chat_id, name, now, now),
        )
        conn.commit()
        return {"success": True}
    except Exception as e:
        raise HTTPException(status_code=500, detail=str(e))
    finally:
        conn.close()

@app.put("/api/tg/targets/{target_id}")
async def api_tg_update_target(target_id: int, payload: dict = {}):
    """更新目標啟用狀態或名稱"""
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
        conn.commit()
        return {"success": True}
    finally:
        conn.close()

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

@app.post("/api/tg/test-send")
async def api_tg_test_send_integrated():
    """使用目前最新整合選股資料測試 TG 推送（不重新同步）"""
    global _last_integrated_result
    if not _last_integrated_result:
        raise HTTPException(status_code=400, detail="目前沒有整合選股資料，請先執行整合選股。")
    targets = _get_tg_db_targets(enabled_only=True)
    if not targets:
        env_recipients = _get_tg_recipients()
        targets = [{"chat_id": r["chatId"], "name": r.get("name", "")} for r in env_recipients]
    if not targets:
        raise HTTPException(status_code=400, detail="尚未設定任何 Telegram 目標，請先新增目標 ID。")
    tg_list    = build_tg_pick_list(_last_integrated_result)
    strong_ind = get_today_strong_industries_from_result(_last_integrated_result)
    msg = format_tg_integrated_message(
        _last_integrated_result.get("data_date", datetime.now().strftime("%Y-%m-%d")),
        _last_integrated_result.get("market_regime", {}),
        tg_list, strong_ind,
    )
    result = _send_tg_with_targets(msg, targets)
    if result["ok"] == 0:
        raise HTTPException(status_code=502, detail=f"全部傳送失敗：{result['errors']}")
    return {
        "status": "ok",
        "picks":  len(tg_list["tg_picks"]),
        "watch":  len(tg_list["tg_watch"]),
        "sent":   result["ok"],
        "failed": result["fail"],
    }

@app.post("/api/tg/test-send/{target_id}")
async def api_tg_test_send_single(target_id: int):
    """對單一目標測試 TG 推送"""
    global _last_integrated_result
    if not _last_integrated_result:
        raise HTTPException(status_code=400, detail="目前沒有整合選股資料，請先執行整合選股。")
    conn = _get_tg_db_conn()
    try:
        row = conn.execute("SELECT * FROM telegram_targets WHERE id=?", (target_id,)).fetchone()
    finally:
        conn.close()
    if not row:
        raise HTTPException(status_code=404, detail="找不到此目標")
    tg_list    = build_tg_pick_list(_last_integrated_result)
    strong_ind = get_today_strong_industries_from_result(_last_integrated_result)
    msg = format_tg_integrated_message(
        _last_integrated_result.get("data_date", datetime.now().strftime("%Y-%m-%d")),
        _last_integrated_result.get("market_regime", {}),
        tg_list, strong_ind,
    )
    result = _send_tg_with_targets(msg, [dict(row)])
    if result["ok"] == 0:
        raise HTTPException(status_code=502, detail=f"傳送失敗：{result['errors']}")
    return {"status": "ok"}

# ── 排程：每週一到五 18:00 自動同步 + 篩選 + 傳送 Telegram ─────────────────

scheduler = AsyncIOScheduler(timezone="Asia/Taipei")

def _send_telegram_message(message: str):
    """廣播訊息給所有收件人（供排程 job 使用）"""
    result = _send_tg_to_all(message)
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

    # 1. 同步數據
    sync_ok = True
    if not is_logged_in:
        print(f"[{now_str}] 尚未登入，略過 K 線同步，嘗試直接用快取篩選")
    else:
        try:
            curr = datetime.now()
            synced_days = 0
            for i in range(15):
                test_date = curr - timedelta(days=i)
                if test_date.weekday() in [5, 6]:
                    continue
                try:
                    screener.sync_twse_institutional_data(test_date)
                    synced_days += 1
                    if synced_days >= 5:
                        break
                except Exception as e:
                    print(f"[{now_str}] 法人同步 {test_date.strftime('%Y-%m-%d')} 失敗：{e}")
            candidates = screener.get_inst_5d_candidates()
            sync_codes = candidates if candidates else screener.DEFAULT_STOCKS
            loop = asyncio.get_event_loop()
            await loop.run_in_executor(None, lambda: screener.sync_stock_kbars(api, sync_codes))
            print(f"[{now_str}] sync success")
        except Exception as e:
            sync_ok = False
            err_msg = f"同步失敗：{e}"
            print(f"[{now_str}] {err_msg}")
            _tg_push_status.update({"last_push_time": now_str, "last_push_status": "sync_failed", "last_error": err_msg})
            err_tg = (f"⚠️ *{date_str} 選股同步失敗*\n原因：{e}\n系統未推送明日精選股，避免使用舊資料。")
            targets = _get_tg_db_targets(enabled_only=True) or [{"chat_id": r["chatId"], "name": r.get("name", "")} for r in _get_tg_recipients()]
            if targets:
                _send_tg_with_targets(err_tg, targets)
            return

    # 2. 執行整合選股
    try:
        result = integrated_strategy.run_integrated_strategy()
        _last_integrated_result = result
        buy_count = len(result.get("buy_candidates", []))
        print(f"[{now_str}] integrated strategy success, buy_candidates={buy_count}")
    except Exception as e:
        err_msg = f"整合選股失敗：{e}"
        print(f"[{now_str}] {err_msg}")
        _tg_push_status.update({"last_push_time": now_str, "last_push_status": "strategy_failed", "last_error": err_msg})
        targets = _get_tg_db_targets(enabled_only=True) or [{"chat_id": r["chatId"], "name": r.get("name", "")} for r in _get_tg_recipients()]
        if targets:
            _send_tg_with_targets(f"⚠️ *{date_str} 排程篩選失敗*\n錯誤：{e}", targets)
        return

    # 3. 建立 TG 精選名單與強勢產業
    tg_list    = build_tg_pick_list(result)
    strong_ind = get_today_strong_industries_from_result(result)
    print(f"[{now_str}] strong_industries={len(strong_ind)}")
    print(f"[{now_str}] tg_picks={len(tg_list['tg_picks'])}, tg_watch={len(tg_list['tg_watch'])}")

    # 4. 組成 TG 訊息
    data_date = result.get("data_date", date_str)
    msg = format_tg_integrated_message(data_date, result.get("market_regime", {}), tg_list, strong_ind)

    # 5. 讀取目標並傳送
    targets = _get_tg_db_targets(enabled_only=True)
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

@app.get("/api/debug/contracts")
async def api_debug_contracts():
    """列出 Shioaji 目前可存取的 TXF 合約清單，用於診斷歷史補取問題"""
    if not is_logged_in:
        raise HTTPException(status_code=401, detail="Not logged in")
    try:
        futures_txf = api.Contracts.Futures.TXF
        found = []
        for attr in dir(futures_txf):
            if attr.startswith("TXF") and len(attr) == 5:
                c = getattr(futures_txf, attr, None)
                if c is not None:
                    found.append({
                        "code": getattr(c, "code", attr),
                        "name": getattr(c, "name", ""),
                        "delivery_date": str(getattr(c, "delivery_date", "")),
                    })
        found.sort(key=lambda x: x["code"])
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

@app.post("/api/screener/sync")
async def api_sync_screener():
    """觸發背景日 K 與三大法人、PTT 數據同步"""
    global api, is_logged_in
    if not is_logged_in:
        raise HTTPException(status_code=400, detail="請先登入系統（啟動連線）才能同步股票數據。")
        
    try:
        # 1. 抓取證交所三大法人 (自動回溯抓取最近 5 個交易日，確保 5 日法人佔比有完整歷史數據)
        curr = datetime.now()
        synced_days = 0
        for i in range(15):  # 往回找最多 15 天以湊齊 5 個交易日
            test_date = curr - timedelta(days=i)
            if test_date.weekday() in [5, 6]:  # 排除週末
                continue
            try:
                screener.sync_twse_institutional_data(test_date)
                synced_days += 1
                if synced_days >= 5:
                    break
            except Exception as eSync:
                print(f"背景同步 {test_date.strftime('%Y-%m-%d')} 三大法人失敗: {eSync}")
        
        # 2. 取得法人候選名單，只對這些股票下載 K 線（大幅縮短同步時間）
        candidates = screener.get_inst_5d_candidates()
        sync_codes = candidates if candidates else screener.DEFAULT_STOCKS
        print(f"[Sync] K-bar sync target: {len(sync_codes)} stocks")

        # 3. 下載 K 線日數據（只抓候選股，避免阻塞主線程）
        loop = asyncio.get_event_loop()
        await loop.run_in_executor(None, lambda: screener.sync_stock_kbars(api, sync_codes))
        
        return {"status": "success", "message": "股票選股數據同步完成！"}
    except Exception as e:
        import traceback
        traceback.print_exc()
        raise HTTPException(status_code=500, detail=f"同步選股數據失敗: {str(e)}")

@app.get("/api/tomorrow_strategy")
async def api_tomorrow_strategy():
    """大盤狀態 × 明日策略選股"""
    try:
        result = tomorrow_strategy.run_tomorrow_strategy()
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
        result = integrated_strategy.run_integrated_strategy()
        _last_integrated_result = result
        return result
    except Exception as e:
        import traceback
        traceback.print_exc()
        raise HTTPException(status_code=500, detail=f"整合選股計算失敗: {str(e)}")


@app.websocket("/ws")
async def websocket_endpoint(websocket: WebSocket):
    await manager.connect(websocket)
    try:
        while True:
            data = await websocket.receive_text()
            # 可以接收前端 ping 等訊息
    except WebSocketDisconnect:
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
    scheduler.start()
    print("[Scheduler] 排程已啟動 — 每週一至週五 18:00 自動執行")

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
