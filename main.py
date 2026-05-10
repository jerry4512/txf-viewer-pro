import os
import json
import asyncio
import urllib.request
from datetime import datetime, timedelta
from fastapi import FastAPI, WebSocket, WebSocketDisconnect, HTTPException
from fastapi.staticfiles import StaticFiles
from fastapi.responses import FileResponse
from pydantic import BaseModel
import shioaji as sj
import pandas as pd
from dotenv import load_dotenv, set_key
import screener  # 引入我們的選股大腦

load_dotenv(os.path.join(os.path.dirname(os.path.abspath(__file__)), '.env'))

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

# 全域 API 實例與狀態
api = sj.Shioaji(simulation=(os.getenv("SHIOAJI_SIMULATION", "False") == "True"))
is_logged_in = False
contract = None
main_loop = None

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

@app.on_event("startup")
async def startup_event():
    global main_loop
    main_loop = asyncio.get_running_loop()
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
            
        is_logged_in = True
        print(f"[{now_str}] [READY] 系統完全就緒，連線就緒開始看盤！\n")
        return {"status": "success", "contract": contract.code}
    except Exception as e:
        is_logged_in = False
        print(f"[{now_str}] [ERROR] 登入失敗！異常訊息: {e}\n")
        raise HTTPException(status_code=400, detail=str(e))

@app.post("/api/select_contract")
async def select_contract(req: dict):
    global api, is_logged_in, contract
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

@app.get("/api/kbars")
async def get_kbars(start: str, end: str, period: str = "1min"):
    global api, is_logged_in, contract
    now_str = datetime.now().strftime('%H:%M:%S')
    
    if not is_logged_in or not contract:
        print(f"[{now_str}] [WARN] 收到 K 線歷史數據請求，但目前為「未登入」狀態！")
        raise HTTPException(status_code=401, detail="Not logged in")
    
    print(f"\n[{now_str}] [CHART] 歷史 K 線索取請求 -> 合約: {contract.code} | 區間: {start} 至 {end} | 週期: {period}")
    
    try:
        start_date = datetime.strptime(start, '%Y-%m-%d')
        end_date = datetime.strptime(end, '%Y-%m-%d')
        
        all_df = []
        current_start = start_date
        
        # 限制最大抓取天數，避免 API 超時
        max_days = 365 if period == "D" else 60
        if (end_date - start_date).days > max_days:
            adjusted_start = end_date - timedelta(days=max_days)
            print(f"[{now_str}] [WARN] 請求天數大於單次最大限制 ({max_days} 天)，已自動縮減查詢起點為 {adjusted_start.strftime('%Y-%m-%d')}")
            start_date = adjusted_start
            current_start = start_date

        query_chunks_count = 0
        while current_start <= end_date:
            current_end = min(current_start + timedelta(days=30), end_date)
            s_str = current_start.strftime('%Y-%m-%d')
            e_str = current_end.strftime('%Y-%m-%d')
            query_chunks_count += 1
            
            print(f"[{now_str}] [SEARCH] [批次 #{query_chunks_count}] 正在呼叫永豐金 API.kbars -> 合約: {contract.code} | 區間: {s_str} 至 {e_str}")
            kbars = api.kbars(contract, start=s_str, end=e_str)
            
            if kbars and kbars.ts and len(kbars.ts) > 0:
                print(f"[{now_str}]  ↳ [SUCCESS] 成功獲取 {len(kbars.ts)} 筆原始 K 線明細")
                df_chunk = pd.DataFrame(dict(kbars))
                all_df.append(df_chunk)
            else:
                print(f"[{now_str}]  ↳ [WARN] 獲取空數據 (0 筆 K 線) - 可能此時段為休市/維護期、或合約在此區間無交易")
            
            current_start = current_end + timedelta(days=1)
            
        if not all_df:
            print(f"[{now_str}] [STOP] 查詢結束：所有批次均無返回任何歷史數據，回傳空清單。\n")
            return []
            
        df = pd.concat(all_df).drop_duplicates(subset=['ts'])
        df['ts'] = pd.to_datetime(df['ts'], unit='ns', utc=True)
        df.set_index('ts', inplace=True)
        df.sort_index(inplace=True)
        
        original_len = len(df)
        
        if period != "1min":
            p_map = {"5min": "5min", "15min": "15min", "30min": "30min", "60min": "60min", "D": "D"}
            resample_p = p_map.get(period, period)
            print(f"[{now_str}] [PROCESS] 正在進行 K 線週期聚合：將 1min 數據 ({original_len} 筆) 聚合為 {resample_p}...")
            # 聚合時確保欄位正確
            df = df.resample(resample_p).agg({
                'Open': 'first', 'High': 'max', 'Low': 'min', 'Close': 'last', 'Volume': 'sum'
            }).dropna()
            
        if df.empty:
            print(f"[{now_str}] [STOP] 聚合後無任何有效資料欄位，回傳空清單。\n")
            return []

        df.reset_index(inplace=True)
        # 修正：永豐金原始數據帶有 8 小時偏移，在此手動減去 (28800秒) 以對齊 UTC
        df['time'] = (df['ts'].values.astype('int64') // 10**9) - 28800
        df.rename(columns={'Open': 'open', 'High': 'high', 'Low': 'low', 'Close': 'close', 'Volume': 'volume'}, inplace=True)
        
        # 最後清洗
        res_data = df[['time', 'open', 'high', 'low', 'close', 'volume']].to_dict('records')
        print(f"[{now_str}] [OK] 歷史 K 線加載成功！最終回傳繪圖 K 棒總數: {len(res_data)} 筆。\n")
        return res_data

    except Exception as e:
        import traceback
        traceback.print_exc()
        print(f"[{now_str}] [ERROR] 歷史 K 線索取發生異常: {e}\n")
        # 回傳具體錯誤訊息給前端
        raise HTTPException(status_code=500, detail=f"Backend Error: {str(e)}")

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
            
    # 如果快取存在且是今天的資料，直接回傳避免被證交所阻擋 IP
    if cache_data and cache_data.get("cache_date") == today_str:
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

@app.post("/api/screener/run")
async def api_run_screener(payload: dict = {}):
    """執行六步驟策略選股"""
    turnover_min = int(float(payload.get("turnover_min", 30_000_000)))
    max_decline  = float(payload.get("max_decline_pct", -3.5))
    try:
        results = screener.run_screener_query(
            turnover_min=turnover_min,
            max_decline_pct=max_decline
        )
        return {"status": "success", "data": results}
    except Exception as e:
        import traceback
        traceback.print_exc()
        raise HTTPException(status_code=500, detail=f"選股計算失敗: {str(e)}")

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

@app.websocket("/ws")
async def websocket_endpoint(websocket: WebSocket):
    await manager.connect(websocket)
    try:
        while True:
            data = await websocket.receive_text()
            # 可以接收前端 ping 等訊息
    except WebSocketDisconnect:
        manager.disconnect(websocket)

# 掛載靜態檔案 (前端)
# 注意：為了能在根目錄直接啟動，必須確保 static 資料夾存在
static_dir = os.path.join(os.path.dirname(os.path.abspath(__file__)), "static")
os.makedirs(static_dir, exist_ok=True)
app.mount("/", StaticFiles(directory=static_dir, html=True), name="static")

if __name__ == "__main__":
    import uvicorn
    uvicorn.run("main:app", host="127.0.0.1", port=8000, reload=True)
