# TXF Pro Viewer：現行選股系統稽核

> 稽核目的：只描述目前程式實際如何運作，不提出新版策略、不修改正式邏輯、不調整 threshold。  
> 稽核時間：2026-08-10（Asia/Taipei）  
> 稽核版本：Git commit `55036b328d84fb61ea62196764956d5f541f41cc`（`fix: 校正權值股漲跌幅參考價`）  
> 主要執行環境：Python 3.9.6、pandas 2.3.3、SQLite 3.43.2。

## 稽核摘要與範圍界定

專案內同時存在兩套可執行的選股邏輯，不能混為一談：

1. **現行預設「整合選股」**：`GET /api/integrated-strategy` → `integrated_strategy.run_integrated_strategy()` → `tomorrow_strategy.run_tomorrow_strategy()`。本文件第 3～18、21 節所稱「現行規則」，均指這條預設路徑。
2. **仍可呼叫的舊六步驟策略**：`POST /api/screener/run` → `screener.run_screener_query()`。前端「策略選股」頁籤目前是 `display:none`，但 API 與程式仍存在；「產業排行」仍會呼叫這套舊流程。它使用 SMA20/SMA60、四種 `market_status` 與另一套產業公式，不是 A/B1/B2/C 五狀態系統。

另外還有兩類不是主選股決策的功能：

- 法人排行：直接顯示法人排名，不等於整合選股。
- 關鍵分點：`broker_analysis.py` 的官方逐日分點分析是獨立頁籤；現行整合分數實際使用的是 `moneydj_fetcher.py` 的 MoneyDJ 5D 區間摘要，不使用 `broker_trading_daily` 的分數。

本次唯讀稽核執行現行整合選股時，`fetch_market_index_daily()` 依既有程式行為刷新了 `market_index_daily` 快取；未修改任何 Python/JavaScript 正式邏輯或 threshold。

---

## 1. 專案架構

### 1.1 主要檔案、函式與呼叫關係

| 路徑 | 主要 class / function | 工作 | 呼叫關係 |
|---|---|---|---|
| `main.py` | `get_effective_screener_data_date()` | 決定 API 要傳給策略的資料日；優先嘗試全市場 `daily_kbars.MAX(date)`，但只有與行事曆日差距 0～3 天時才採用 | 被 `/api/tomorrow_strategy`、`/api/integrated-strategy`、MoneyDJ 候選同步呼叫 |
| `main.py` | `api_integrated_strategy()` | 現行整合選股 HTTP 入口，執行策略、保存 `_last_integrated_result`、附加日期驗證 | 呼叫 `integrated_strategy.run_integrated_strategy()`、`validate_result_data_date()` |
| `main.py` | `sync_all_stock_screener_data()` | 同步法人與大盤資料、檢查個股日 K 新鮮度；目前明確不以富邦期貨 SDK 更新個股日 K | 呼叫 `screener.sync_twse_institutional_data()`、`market_status.sync_taiex_daily_kbars()` |
| `main.py` | `_sync_screener_and_moneydj_pipeline()`、`_sync_moneydj_for_integrated_candidates()` | 共用同步流程；核心資料有效後，對 buy/high/wait 候選最多 30 檔同步 MoneyDJ 5D，再重跑整合策略 | 排程與手動同步共用 |
| `main.py` | `calculate_tg_score()`、`build_tg_pick_list()`、`apply_tg_downgrade_rules()` | 從整合策略的 `buy_candidates` 再做 Telegram 精選/備選排序與 K 線風險降級 | 不改網頁五分類；只影響 Telegram 最多 3 精選 + 2 備選 |
| `main.py` | `_scheduled_sync_and_alert()` | 平日 18:00 同步、驗證、整合選股、Telegram 推播；日期關鍵錯誤時阻擋推播 | APScheduler 入口 |
| `tomorrow_strategy.py` | `run_tomorrow_strategy()` | 現行主決策：商品排除、新鮮度、技術計算、A/B1/B2/C、流動性、風報比、初步分類與上限 | 被 `integrated_strategy.py` 呼叫；亦有隱藏頁籤 API |
| `tomorrow_strategy.py` | `calculate_market_regime()` | 依 TAIEX Donchian 成本線與 MACD 判斷五種大盤狀態 | 被 `run_tomorrow_strategy()` 呼叫 |
| `tomorrow_strategy.py` | `_analyze_stock()` | 計算個股 cost20/cost60、MACD、量價、60 日高、停損、RR、A/B1/B2/C | 每個 `daily_kbars.code` 執行一次 |
| `tomorrow_strategy.py` | `_calculate_score()` | 技術原始分 `base_score_raw`（0～100） | 在商品與資料硬排除後、初步分類前執行 |
| `tomorrow_strategy.py` | `_classify_candidate()` | 依硬排除、三項動態風險、大盤狀態與個股條件產生「明日可買／高優先觀察／其他觀察／排除」 | 結果交給 `integrated_strategy.py` |
| `integrated_strategy.py` | `run_integrated_strategy()` | 現行網頁最終入口：主決策 + 法人 + 產業 + MoneyDJ + 流動性分數，產出五分類 | 被 `main.py` API/排程呼叫 |
| `integrated_strategy.py` | `_get_chip_data()` | 查 `institutional_trading` 最新 5 個全市場日期合計與每檔尾端連買天數 | 只豐富 tomorrow 非排除候選 |
| `integrated_strategy.py` | `_compute_industry_rankings()` | 依已進入 tomorrow 非排除且受名單上限限制的股票分組計算產業分數/共振 | 原地寫回每檔股票 |
| `integrated_strategy.py` | `_calculate_final_score()` | `0.65×技術原始分 + 法人 + 產業 + 流動性 + MoneyDJ − 風險扣分` | 只排序/顯示；不把 tomorrow 不可買股票升成可買 |
| `integrated_strategy.py` | `_classify_final_category()` | 產生「明日可買／高優先／等回測／其他／排除」 | tomorrow 否決優先；等回測判斷先於高優先 |
| `market_status.py` | `fetch_market_index_daily()`、`sync_taiex_daily_kbars()` | 從 Yahoo Finance `^TWII` 取得 TAIEX 日 K，必要時用 TWSE 補 close，存 `market_index_daily` | 五狀態與舊四狀態共用資料源 |
| `market_status.py` | `calculate_market_status()`、`determine_buy_method()` | 舊六步驟策略使用的四狀態/SMA 買法 | **不參與現行 integrated 五狀態主決策** |
| `screener.py` | `init_db()` | 建立 `daily_kbars`、`institutional_trading`、`stock_names`、`social_sentiment` | 資料基礎 |
| `screener.py` | `sync_twse_institutional_data()` | 從 TWSE T86 與 TPEx 下載外資/投信/自營商淨買賣，股數 `//1000` 後存張數 | 現行法人分數資料源 |
| `screener.py` | `sync_stock_kbars()` | 舊 Shioaji 股票日 K 匯入器；聚合後寫 `daily_kbars`、寫名稱/產業 | 註解及 `main.py` 明確表示目前富邦期貨流程不呼叫它 |
| `screener.py` | `run_screener_query()` | 舊六步驟選股：法人先篩、SMA、漲幅、另一套分數/買點/產業 | `/api/screener/run` 與 `/api/industry_rankings`；非預設整合選股 |
| `moneydj_fetcher.py` | `fetch_moneydj_broker_period()`、`parse_moneydj_broker_table()` | 抓富邦 MoneyDJ 分點頁的 1D/5D/10D/20D 區間買賣超摘要 | 現行整合只讀 5D |
| `moneydj_fetcher.py` | `get_moneydj_period_summary()` | 讀最新 `broker_period_summary`，以首名買/賣分點成交量占比組成狀態文字 | `integrated_strategy._score_moneydj_summary()` 再以關鍵字轉分數 |
| `broker_analysis.py` | `ensure_broker_tables()`、`analyze_key_brokers()` | 官方逐日分點統計與獨立分點頁面 | 不進現行整合分數 |
| `broker_fetcher.py` | `fetch_twse_broker_daily()`、`fetch_tpex_broker_daily()` | TWSE/TPEx 官方分點抓取與 `broker_trading_daily` upsert | 只供關鍵分點頁面 |
| `static/app_pro.js` | `activeStockTab='integrated'`、`loadIntegratedStrategy()` | 股票模式預設顯示整合選股並呼叫 `/api/integrated-strategy` | 渲染五分類 |
| `static/index.html` | 股票頁籤與結果表格 | 顯示法人排行、產業排行、整合選股、關鍵分點 | `screener`、`tomorrow` 兩頁籤目前隱藏 |
| `stock_cache.db` | SQLite | 個股日 K、法人、名稱/產業、TAIEX、MoneyDJ、Telegram 目標 | 現行策略的本地資料來源 |

現有 `current_strategy_logic_audit.md`、`screener_logic.md`、`strategy_logic_*_result.md` 是說明/歷史文件，不會被執行；本稽核以目前 `.py`/`.js` 與實際 DB 為準。

### 1.2 現行資料流程

```text
stock_cache.db
  ├─ daily_kbars ──────────────┐
  ├─ stock_names ──────────────┤
  ├─ market_index_daily ───────┤
  ├─ institutional_trading ────┤
  └─ broker_period_summary ────┘
                 ↓
商品類型 / K棒數 / 每檔最新日期 / 流動性前置排除
                 ↓
TAIEX 五種 Market Regime
                 ↓
A / B1 / B2 / C 個股分類
                 ↓
成本線 + MACD + 量價 + 距離 + 60日高點
                 ↓
Entry / Stop / Target / RR + 動態風險
                 ↓
tomorrow 技術原始分與初步分類（主決策/否決權）
                 ↓
法人 + 產業共振 + 流動性 + MoneyDJ + risk_penalty
                 ↓
final_score（排序）+ 最終五分類
                 ↓
網頁輸出；另可再經 TG 精選規則產生推播
```

---

## 2. `stock_cache.db`

### 2.1 稽核時實際資料快照

| Table | Rows | Symbols | 最早日 | 最新日 |
|---|---:|---:|---|---|
| `daily_kbars` | 75,184 | 936 | 2026-04-07 | 2026-07-31 |
| `institutional_trading` | 24,669 | 2,350 | 2026-07-23 | 2026-08-06 |
| `stock_names` | 936 | 936 | — | — |
| `market_index_daily` | 125 | 1（TAIEX） | 2026-01-28 | 2026-08-07 |
| `broker_period_summary` | 510 | 17 | 2026-07-27 | 2026-07-31 |
| `social_sentiment` | 0 | 0 | — | — |

`daily_kbars` 在 2026-07-31 有 930 檔；其餘 6 檔最後日期更早。DB 使用 WAL、timeout 60 秒；`daily_kbars` 本身沒有 `created_at`/`updated_at`。

### 2.2 實際 schema 與欄位意義

#### `daily_kbars`

```sql
CREATE TABLE daily_kbars (
    code TEXT,
    date TEXT,
    open REAL,
    high REAL,
    low REAL,
    close REAL,
    volume INTEGER,
    PRIMARY KEY (code, date)
);
```

| 欄位 | 程式中的意義/單位 |
|---|---|
| `code` | 股票/ETF 等商品代號；沒有市場別欄位 |
| `date` | `YYYY-MM-DD` 文字日期 |
| `open/high/low/close` | 程式當作每股新台幣價格使用；DB 沒有單位 metadata |
| `volume` | 程式明確假設為「張」；1 張 = 1,000 股 |

舊匯入器從 Shioaji `kbars()` 取得資料，依日期聚合 `Open:first, High:max, Low:min, Close:last, Volume:sum`。現行主程式不再更新這張表，只檢查最新日期。

#### `institutional_trading`

```sql
CREATE TABLE institutional_trading (
    code TEXT,
    date TEXT,
    foreign_buy INTEGER,
    investment_buy INTEGER,
    dealer_buy INTEGER,
    PRIMARY KEY (code, date)
);
```

三個數值都是淨買賣超「張」。同步程式把 TWSE/TPEx 官方股數以整數除法 `// 1000` 換算；負值也使用 Python floor division，非整千股的負數會向負無限方向取整。

#### `stock_names`

```sql
CREATE TABLE stock_names (
    code TEXT PRIMARY KEY,
    name TEXT NOT NULL,
    category TEXT DEFAULT ''
);
```

名稱及 `category` 由舊券商合約的 `name/chinese_name/category` 寫入；數字產業代碼經 `_INDUSTRY_MAP` 轉中文。沒有上市/上櫃、上市日、下市日、證券狀態、ISIN 或商品類型欄位。

#### `market_index_daily`

```sql
CREATE TABLE market_index_daily (
    date TEXT PRIMARY KEY,
    open REAL, high REAL, low REAL, close REAL,
    volume REAL, amount REAL,
    ma20 REAL, ma60 REAL,
    created_at TEXT, updated_at TEXT
);
```

OHLC 是 TAIEX 點數；來源為 Yahoo Finance `^TWII`，必要時只用 TWSE `MI_5MINS_HIST` 補缺失 close。`amount` 目前寫 0；`ma20/ma60` 欄位存在但同步不寫，策略現算 Donchian 成本線或舊 SMA。Yahoo 的 `volume` 可能為 0，五狀態程式只將其寫入 metrics/文案，沒有放進五狀態 boolean。

#### `broker_period_summary`

```sql
CREATE TABLE broker_period_summary (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    code TEXT NOT NULL,
    stock_name TEXT,
    start_date TEXT,
    end_date TEXT,
    period_label TEXT NOT NULL,
    side TEXT NOT NULL,
    broker_name TEXT NOT NULL,
    buy_lots INTEGER DEFAULT 0,
    sell_lots INTEGER DEFAULT 0,
    net_lots INTEGER DEFAULT 0,
    volume_ratio REAL DEFAULT 0,
    source TEXT DEFAULT 'moneydj_fubon',
    created_at TEXT DEFAULT CURRENT_TIMESTAMP,
    updated_at TEXT DEFAULT CURRENT_TIMESTAMP,
    UNIQUE(code, end_date, period_label, side, broker_name, source)
);
```

`period_label` 支援 `1D/5D/10D/20D`；現行選股只用 5D。數量為張，`volume_ratio` 為 MoneyDJ 頁面提供的百分比。`start_date` 若網頁未提供，依 `daily_kbars` 向前取指定交易日數補值。

#### 其他實際表

- `social_sentiment(code,date,mention_count)`：目前 0 rows，現行選股完全未讀。
- `telegram_targets(...)`：只控制推播目標，不影響選股。
- `broker_trading_daily`、`broker_watchlist`：`broker_analysis.ensure_broker_tables()` 可建立，但本次稽核的實際 DB 尚不存在；它們属于獨立關鍵分點頁，不進整合分數。

### 2.3 成交額、名稱、市場與商品辨識

- 現行 `amount_ma20` **不是資料源提供的成交金額，也不是 20 天逐日成交額平均**；公式是：`latest_close × volume_ma20 × 1000`。
- 舊 `screener.py` 路徑則先算每一天 `close × volume × 1000`，再取 `rolling(20).mean()`；兩條路的數字可能不同。
- `stock_names` 沒有市場別；選股本身不區分上市/上櫃。`broker_fetcher.py` 查分點時才另行用官方行情偵測市場。
- ETF/ETN/權證/特別股不是 DB 欄位，而是每次由 `classify_instrument()` 以代號、名稱、產業文字即時計算，詳見第 3 節。

### 2.4 下市、還原權息與除權息

| 問題 | 現況 |
|---|---|
| 是否保存下市股票 | **待確認**。schema 沒有上市狀態，匯入器也沒有刪除不再出現在合約清單的舊 row；但 DB 是否真的含下市股，無法只靠現有欄位判斷 |
| 是否使用還原權息價格 | **待確認**。程式呼叫舊 API `kbars()` 時沒有 adjustment 參數，DB 也沒有 adjusted flag |
| 除權息如何處理 | 程式內完全沒有價格連續化、股利/分割資料或除權息事件調整；若來源是未還原價，成本線、MACD、距離、60 日高與 RR 都會直接受跳空影響 |

### 2.5 更新時間與資料過期判斷

1. 個股日 K：每檔 `last_kbar_date` 必須等於 `daily_kbars` 全市場最大日期；否則該檔硬排除。沒有 row-level 更新時間。
2. API 有效日：`get_effective_screener_data_date()` 只在全市場最新 K 與行事曆最近非週末差 0～3 天時使用 K 棒日期；超過 3 天時保留行事曆日。
3. 同步驗證：`daily_kbars.MAX(date)` 與要求日不等為 critical error；`market_index_daily` 不等亦為 critical error；法人日期不等只列 warning。
4. MoneyDJ：`end_date == integrated data_date` 才可計分，否則 `stale_data` 且分數 0。
5. TAIEX 快取：最新日距今天 `<=1` 天且至少 62 根時直接用快取，否則嘗試重抓。

### 2.6 Bias 與價格失真現況

| 項目 | 結論與依據 |
|---|---|
| Survivorship bias | **有設計風險，實際程度待確認**。沒有歷史成分股/上市下市狀態；目前 936 檔來自曾同步的合約快照。做歷史回測時無法重建當日可交易股票池 |
| Look-ahead bias | **現行歷史/日期錯置情境確實存在**。`run_tomorrow_strategy(data_date=...)` 不以該日裁切個股 `daily_kbars`，之後還把傳入 `data_date` 改寫為 DB 全市場最新日；`_get_chip_data()` 亦不按選股日過濾，永遠取法人表最新 5 個日期。稽核時個股日為 7/31、法人最新為 8/6，因此 7/31 結果實際用了之後的法人資料 |
| 除權息價格失真 | **可能存在，是否已發生待確認**。程式沒有任何企業行動處理，來源是否已還原亦無 metadata |

---

## 3. 前置股票排除條件

以下依現行實際執行順序列出。

### 3.1 商品類型判定與排除

`tomorrow_strategy.classify_instrument()`（119～166 行）：

| 類型 | 精確判定式 | 結果 |
|---|---|---|
| ETN | `"ETN" in name.upper() or "ETN" in industry.upper()` | `INCLUDE_ETN=False`，硬排除 |
| 權證 | `any(kw in name for kw in ["購","售","權證"])` | `INCLUDE_WARRANT=False`，硬排除 |
| 特別股 | 名稱以甲特/乙特/丙特結尾，或含特別股，或最後一字是特且不是特化/特材 | 無開關，硬排除 |
| ETF | `(symbol.startswith("00") and len(symbol)<=7) or "ETF" in name/industry` | `INCLUDE_ETF=False` 時不進普通股；A/B1 且流動性通過者進 `etf_candidates`，其餘排除 |
| 反向 ETF | 已是 ETF 且名稱含反向/反1/放空/空方，或代號以 `R` 結尾 | 無條件硬排除；`INCLUDE_REVERSE_ETF` 常數未被使用 |
| 槓桿 ETF | 已是 ETF 且名稱含正2/2倍/槓桿/2X/正向2/兩倍 | 無條件硬排除 |
| KY | `"KY" in name` | 只寫 `is_ky=True`，不排除、不扣分 |
| 普通股 | 以上皆非 | 繼續 |

### 3.2 資料與計算硬排除

| 中文說明 | 變數/function | 精確判定式 | 位置 |
|---|---|---|---|
| 每檔日期未同步 | `last_kbar_date` | `global_data_date and last_kbar_date != global_data_date` | `tomorrow_strategy.py:1026-1032` |
| K 棒不足 | `_MIN_BARS` | `len(sub_df) < 62` | `:416-417` |
| 成本線無效 | `c20,c60` | `isna(c20) or isna(c60) or c20<=0 or c60<=0` | `:427-434` |
| MACD 無法計算 | `hist_s` | 例外、`len(hist_s)<3` 或最後 3 根含 NaN | `:436-448` |
| 普通股流動性不足 | `liquidity_level` | 分層後 `liquidity_level == "low"` | `:1052-1089` |

### 3.3 技術/市場硬排除

| 條件 | 精確判定式 | 影響 |
|---|---|---|
| C 級 | `grade == "C"` | 立即排除 |
| B2 | `grade == "B2"` | 立即排除 |
| 高檔爆量長上影 | `vol_now > vol_ma20*1.5 and upper_shadow_ratio > 0.4` | 立即排除 |
| 距 cost20 過遠 | `dist_cost20_pct > 12.0`；注意不是絕對值 | 立即排除 |
| 同時兩項動態風險 | `len(excl) >= 2`，三項為 MACD 負柱擴大、下跌放量、有效 RR<1 | 立即排除 |
| 空頭破 60 | regime 為 `bear_break60` 時，只有 A 且零項動態風險留高優先；其餘排除 | 不可買 |
| 弱勢反彈 | regime 為 `weak_bounce and grade != "A"` | 排除；A 仍只會落觀察，不存在 weak_bounce 可買分支 |

單一動態風險時程式命名 `hard_excl=True`，實際作用是阻止進可買；A/B1 仍可能進觀察，並非真正立即硬排除。

---

## 4. 流動性規則

### 4.1 現行計算

```python
volume_ma20 = rolling_mean(volume, 20)               # 張
amount_ma20 = latest_close * volume_ma20 * 1000      # 元
```

### 4.2 分層與門檻

| 層級 | 精確條件 | 後續影響 |
|---|---|---|
| `high` | `volume_ma20 >= 3000 AND amount_ma20 >= 100_000_000` | 正常分類 |
| `normal` | `volume_ma20 >= 1000 AND amount_ma20 >= 50_000_000` | 正常分類 |
| `low_amount_pass` | `volume_ma20 < 1000 AND amount_ma20 >= 50_000_000` | 可分析，但若原本是明日可買或高優先，最終固定高優先；附低張數警告 |
| `low` | 以上皆不符 | 普通股硬排除 |

因此「20 日均量 < 1,000 張，但成交額 ≥ 5,000 萬，只能列高優先觀察」的實作是：先標 `low_amount_pass`，先照一般技術分類，之後若 `category in ("明日可買","高優先觀察")` 再覆寫為 `高優先觀察`。若原本是其他觀察/排除，不會升級。

### 4.3 整合分數中的額外流動性分

| 條件 | 分數 |
|---|---:|
| `amount_ma20 >= 300M` | +5 |
| `100M <= amount_ma20 < 300M` | +4 |
| `50M <= amount_ma20 < 100M` | +2 |
| `volume_ma20 >= 3000` | +3 |
| `1000 <= volume_ma20 < 3000` | +1 |

兩部分可相加，上限 +8。這是 final score，不改 tomorrow 的硬排除或可買否決。

---

## 5. 大盤五種狀態

### 5.1 共用變數

```python
cost20 = (rolling_max(high,20) + rolling_min(low,20)) / 2
cost60 = (rolling_max(high,60) + rolling_min(low,60)) / 2
dist20 = (close-cost20)/cost20*100
cost20_slope_5d = cost20[-1] - cost20[-5]
hist = EMA12(close)-EMA26(close) - EMA9(EMA12(close)-EMA26(close))
```

MACD 狀態見第 8 節。判斷有優先順序，先符合即返回。

### 5.2 完整判斷順序

1. **空頭破 60 `bear_break60`**

```python
close < market_cost60
or (market_cost20 < market_cost60 and macd_status == "負柱擴大")
```

2. **弱勢反彈 `weak_bounce`**

```python
close < market_cost20
and close >= market_cost60
and market_cost20_slope_5d < 0
and macd_hist < 0
and macd_status != "負柱收斂"
```

3. **高檔過熱 `high_overheated`**

```python
close > market_cost20
and dist_cost20_pct > 8.0
```

4. **強多延伸 `strong_bull`**

```python
close > market_cost20
and market_cost20 > market_cost60
and macd_status in ("正柱放大", "負柱收斂")
```

5. **健康回測 `healthy_pullback`**

```python
close >= market_cost60
and -5.0 <= dist_cost20_pct <= 5.0
and macd_status == "負柱收斂"
```

6. **Fallback**：

```python
if close > cost20 and close > cost60 and cost20 > cost60: strong_bull
elif close >= cost60: healthy_pullback
else: bear_break60
```

若大盤不足 62 根、成本線失敗、要求日與大盤最後日不一致，程式也回傳 `healthy_pullback`；差別只在 `metrics.data_available=False`/`regime_error=True`，策略本身仍照健康回測分類股票。

### 5.3 對個股的實際影響

| Regime | A 可買 | B1 可買 | 排除/降級 | 分數影響 |
|---|---|---|---|---|
| 強多延伸 | 距 cost20 絕對值≤3%、MACD OK、有效 RR≥1.5、非「近60高且距20>5」；另需 cost20 斜率/彈升確認 | 不可買；站回 cost20 為高優先，否則其他 | A 若斜率條件失敗降高優先 | 不直接加減分 |
| 健康回測 | 距 cost20 絕對值≤8%、MACD OK、`rr_ok` | 站上 cost20 + MACD OK + 量縮 + `rr_ok` | B2/C 仍先硬排除 | 不直接加減分 |
| 高檔過熱 | A 距 cost20 絕對值≤3%、MACD OK、`rr_ok`，輕倉文案 | 無 B1 可買分支 | 其餘按觀察規則 | 不直接加減分 |
| 弱勢反彈 | 無可買分支 | 非 A 直接排除 | A 只可能高優先/其他 | 不直接加減分 |
| 空頭破60 | 無 | 無 | A 且零動態風險→高優先；其餘排除 | 不直接加減分 |

其中 `rr_ok = (RR 無效) OR (RR>=1.5)`；所以健康回測與高檔過熱允許 RR 為 `None` 的股票可買，並不等同一律要求 RR≥1.5。

---

## 6. A / B1 / B2 / C 個股分類

變數：

```python
above_c20 = close > cost20
above_c60 = close > cost60
c20_above_c60 = cost20 > cost60
```

判斷順序與完整 boolean：

### A

```python
above_c20 and above_c60 and c20_above_c60
```

### B2

```python
above_c60
and not above_c20
and cost20_slope < 0
and (down_vol or macd_neg_expanding)
```

### B1

B1 是前兩個 `if/elif` 都沒中後的 catch-all：

```python
above_c60
and not (above_c20 and above_c60 and c20_above_c60)  # 非 A
and not (
    above_c60 and not above_c20 and cost20_slope < 0
    and (down_vol or macd_neg_expanding)
)                                                    # 非 B2
```

它可包含：站上 cost20 但 `cost20<=cost60`，或低於/等於 cost20 但不滿足 B2 的斜率/量價風險。

### C

```python
not above_c60   # 即 close <= cost60
```

優先序為 A → B2 → B1 → C。同一檔即使概念文字看似可同時屬兩類，也只會落第一個命中的類別；實際 boolean 下 A 與 B2 互斥，B1 是剩餘集合。`close == cost60` 因使用嚴格 `>`，會被分類 C。

---

## 7. 20 / 60 日成本線

現行五狀態與 A/B 系統的成本線不是 SMA、不是 VWAP、不是成交金額/成交量：

```python
cost20_series = (high.rolling(20).max() + low.rolling(20).min()) / 2
cost60_series = (high.rolling(60).max() + low.rolling(60).min()) / 2
```

- 20 日成本線 = 最近 20 根（含今日）最高價與最低價中點。
- 60 日成本線 = 最近 60 根（含今日）最高價與最低價中點。
- Code location：`tomorrow_strategy.py:171-173`；個股使用 `:427-431`，大盤使用 `:270-274`。
- 舊 `/api/screener/run` 的 `cost20/cost60` 欄位其實是 close 的 SMA20/SMA60（`screener.py:742-745,1519-1520`）；是另一套算法。

---

## 8. MACD

### 8.1 參數與公式

```python
ema12  = close.ewm(span=12, adjust=False).mean()
ema26  = close.ewm(span=26, adjust=False).mean()
DIF    = ema12 - ema26
Signal = DIF.ewm(span=9, adjust=False).mean()
Hist   = DIF - Signal
```

參數 fast=12、slow=26、signal=9，全部 EMA；沒有 SMA 選項。

### 8.2 狀態

```python
負柱收斂 = h0<0 and h1<0 and h2<0 and h0>h1 and h1>h2
正柱放大 = h0>0 and h0>h1
正柱收斂 = h0>0 and h0<h1
負柱擴大 = h0<0 and h0<h1
正柱     = 其他且 h0>=0
負柱     = 其他且 h0<0
```

### 8.3 所有用途

- 大盤五狀態：空頭、弱勢、強多、健康的 boolean，見第 5 節。
- B2：`down_vol or macd_neg_expanding`。
- 三項動態風險之一：`macd_neg_expanding`；兩項風險才硬排除。
- `macd_ok = neg_converging or pos_expanding`，用於 A/B1 可買與高優先。
- 技術原始分：負柱收斂 +10；正柱放大/正柱 +8；正柱收斂 +4；負柱擴大 −10；普通負柱 0。
- Telegram：負柱收斂 +15、正柱放大 +10、正柱收斂/正柱 +5；入 TG 前另硬擋負柱擴大。
- MACD 本身不設「單項即排除」，除非參與 B2、與另一動態風險並存，或市場條件導致排除。

---

## 9. 量價條件

```python
vol_shrinking = volume_today < volume_ma20
down_candle   = close < open
down_vol      = down_candle and volume_today > volume_ma20 * 1.2
candle_range  = high-low
upper_shadow  = high-max(open,close)
upper_shadow_ratio = upper_shadow/candle_range if candle_range>0 else 0
high_vol_upper_shadow = volume_today > volume_ma20*1.5 and upper_shadow_ratio>0.4
```

`volume_status` 有固定優先序：

1. `high_vol_upper_shadow` → 高檔爆量長上影。
2. `down_vol` → 下跌放量。
3. `vol_shrinking` → 量縮。
4. `volume_today > volume_ma20*1.2` → 放量。
5. 其他 → 量平。

影響：

- 高檔爆量長上影：tomorrow 立即硬排除；技術分 −10；integrated 另有 risk penalty +8，但通常到不了 enriched 階段。
- 下跌放量：技術分 −10，也是動態風險一項。
- 量縮：技術分 +8；健康回測 B1 可買要求量縮。
- 放量且 `close>cost20`：技術分 +7。
- 爆量的精確定義只存在「爆量長上影」的 1.5 倍；一般 `volume_status=放量` 使用 1.2 倍。
- 沒有下影線、實體比例、價量背離等其他現行主決策條件。

---

## 10. 距成本線

```python
dist20 = (close-cost20)/cost20*100
dist60 = (close-cost60)/cost60*100
```

| 門檻 | 用途 |
|---|---|
| `dist20 > 12%` | tomorrow 硬排除；不是 `abs()` |
| `dist20 > 10%` | 技術原始分 −10 |
| `dist20 > 8%` | 大盤高檔過熱；integrated risk penalty −8；integrated `is_extended`；一般 A 可買的 `abs(dist20)<=8` 上限 |
| `abs(dist20)<=5%` | A 級未買成時可列高優先（且零動態風險）；健康大盤本身亦用 −5%～+5% |
| `dist20>5% and near60high` | 阻擋強多可買，並列高優先；integrated 視為 extended |
| `abs(dist20)<=3%` | 強多/過熱 A 可買；Telegram 入選也要求≤3% |
| `-2%<=dist20<=5%` | 技術原始分 +10 |
| `dist20<-2% and close>cost60` | 技術原始分 −5 |
| `0%<=dist60<=3%` | 技術原始分 +8 |
| TG 距離≤1/≤2/≤3/>3 | TG score +20/+17/+12/+4 |

`dist60` 有計算並參與原始分，但 active integrated payload 沒有複製 `dist_cost60_pct` 欄位；第 19 節的距60數字是依相同公式由 close/cost60 稽核推算。

---

## 11. 60 日高點

```python
high60 = high.rolling(60).max().iloc[-1]
is_near_60d_high = close >= high60*0.98
is_new_60d_high  = close >= high60
```

- 使用 `high`，不是 close。
- rolling window 含今日。
- 因含今日且 `close<=today_high`，`is_new_60d_high` 通常只在 close 等於區間最高 high 時成立。
- `high60` 同時是 `resistance_price` 與 RR 的 Target。
- 接近高點時 RR 直接設 `None/invalid`，不再計 `(target-entry)/risk`。
- 強多 A 若「近高且 dist20>5」不得買；觀察與 integrated 等回測亦使用此條件。
- 60 日高點本身沒有獨立加分；透過 RR、近高阻擋及等回測分類間接影響。

---

## 12. 風報比

### 12.1 現行公式

```python
Entry  = today_close
Stop   = min(low of latest 3 bars, including today)
Target = max(high of latest 60 bars, including today)
Risk   = Entry - Stop
Reward = Target - Entry
RR     = Reward / Risk
```

例外：

- `close >= Target*0.98` → `RR=None`, `rr_valid=False`。
- `Risk<=0` 或 NaN → `RR=None`, `rr_valid=False`。
- RR 四捨五入至小數 2 位。

### 12.2 RR 門檻在哪裡使用

| 階段 | 規則 |
|---|---|
| 技術原始分 | RR≥2 +10；1.5≤RR<2 +6；RR<1 −10；1～1.5 為 0；RR 無效為 0 |
| 動態風險 | 有效 RR<1 算一項 risk；與另外一項並存即硬排除 |
| 強多 A 可買 | 必須有效且 RR≥1.5 |
| 健康/過熱 A、健康 B1 | `rr_ok = RR 無效 OR RR≥1.5`；因此無效 RR 可通過 |
| integrated risk penalty | RR<1.5 −6；RR<1 再 −14，故 RR<1 累計 −20 |
| Telegram | 只從 buy 中挑，必須 RR≥1.5；2～5 得分最高 |

最低 RR 1.5 並不是全系統一致硬門檻。稽核輸出可看到 RR=None 的股票仍在「明日可買」，原因就是 `rr_ok` 將無效 RR 視為通過。

### 12.3 隔日成交與交易限制

| 情境 | 現況 |
|---|---|
| 隔日實際 Entry | 目前未重新計算；RR 固定以訊號日 close 當 Entry |
| 向上/向下跳空 | 目前未處理 |
| 漲停無法成交 | 目前未處理 |
| 跌停 | 目前未處理 |
| Stop gap / 跳空穿越停損 | 目前未處理 |
| 滑價、手續費、交易稅 | 目前未納入 RR |

Telegram 文案會寫「不追高、等回測/轉強」，但沒有成交引擎把隔日實際價格帶回 RR。

---

## 13. 風險條件

### 13.1 `excl` 動態風險清單

程式沒有名為 `risk_count` 的變數；實際用 `excl: list` 並以 `len(excl)` 計數：

```python
if macd_neg_expanding: excl.append(...)
if down_vol: excl.append(...)
if rr_valid and risk_reward < 1.0: excl.append(...)
if len(excl) >= 2: 排除
hard_excl = (len(excl) == 1)
```

所以算一項 risk 的只有：

1. `macd_neg_expanding`
2. `down_vol`
3. `rr_valid and risk_reward < 1.0`

`high_vol_upper_shadow`、grade C/B2、`dist20>12` 是更早的單項硬排除，不進這個 count。

### 13.2 integrated `risk_penalty`

這是分數扣分，不是上述 risk count：

```python
dist20 > 8                  => +8 penalty
high_vol_upper_shadow       => +8 penalty
RR is not None and RR < 1.5 => +6 penalty
RR is not None and RR < 1.0 => 再 +14 penalty
```

### 13.3 Telegram 額外 K 線風險

- `upper_shadow_ratio>0.4` 長上影。
- `(close-low)/(high-low)<0.35` 收盤靠低。
- `high>close*1.04 and close_position<0.4` 衝高收低。
- RR>8 配長上影/衝高收低，或 RR>5 配較明顯反轉，從 TG 精選降到備選；只影響推播，不改網頁分類。

---

## 14. 評分系統

### 14.1 Tomorrow 技術原始分（`base_score_raw`）

| 條件 | 分數 | 可改變分類 | 硬排除 | 位置 |
|---|---:|---|---|---|
| `close>cost60` | +15 | 間接 | 否 | `tomorrow_strategy.py:594-597` |
| `close>cost20` | +15 | 間接 | 否 | 同上 |
| `cost20>cost60` | +10 | 間接 | 否 | 同上 |
| `-2<=dist20<=5` | +10 | 只排序 | 否 | `:599-611` |
| `dist20<-2 and close>cost60` | −5 | 只排序 | 否 | 同上 |
| `close<cost60` | −30 | 技術上會低分，但 C 已先排 | C 為硬排 | 同上 |
| `dist20>10` | −10 | 只排序 | `>12` 另硬排 | 同上 |
| `0<=dist60<=3` | +8 | 只排序 | 否 | 同上 |
| 量縮 | +8 | 可買條件亦使用 | 否 | `:613-623` |
| 放量且 `close>cost20` | +7 | 只排序 | 否 | 同上 |
| 下跌放量 | −10 | risk/分類亦使用 | 與另一 risk 才硬排 | 同上 |
| 爆量長上影 | −10 | — | 單項硬排 | 同上 |
| MACD 負柱收斂 | +10 | 是 | 否 | `:624-633` |
| MACD 正柱放大/正柱 | +8 | 正柱放大可改可買 | 否 | 同上 |
| MACD 正柱收斂 | +4 | 只排序 | 否 | 同上 |
| MACD 負柱擴大 | −10 | risk/分類亦使用 | 與另一 risk 才硬排 | 同上 |
| RR≥2 | +10 | RR 條件亦使用 | 否 | `:635-643` |
| 1.5≤RR<2 | +6 | RR 條件亦使用 | 否 | 同上 |
| RR<1 | −10 | risk/分類亦使用 | 與另一 risk 才硬排 | 同上 |

最後 clamp 0～100。此原始分沒有法人、產業、MoneyDJ、大盤 regime 分。

### 14.2 Integrated final score

```python
base_score      = round(base_score_raw*0.65)
final_score     = clamp(base_score + chip_bonus + industry_bonus
                        + liquidity_bonus + broker_bonus - risk_penalty, 0, 100)
```

| 類別/條件 | 分數 | 可改變五分類 | 硬排除 | 位置 |
|---|---:|---|---|---|
| 三大法人 5 日合計>0 | +3 | 不直接 | 否 | `integrated_strategy.py:367-376` |
| 外資 5 日>0 | +2 | 不直接 | 否 | 同上 |
| 投信 5 日>0 | +3 | 不直接 | 否 | 同上 |
| 外資與投信都>0 | +4 | 不直接 | 否 | 同上 |
| 投信連買≥3 | +3 | 不直接 | 否 | 同上 |
| 外資連買≥3 | +2 | 不直接 | 否 | 同上 |
| 黃金滿貫 | +5 | 不直接 | 否 | 同上 |
| `chip_bonus` | 上限15 | 參與等回測的「強籌碼」 | 否 | 同上 |
| 產業分≥85/≥80/≥70 | +5/+4/+2 | 不直接 | 否 | `:379-390` |
| 產業共振 | +5 | 參與等回測 | 否 | 同上 |
| 強勢主流/過熱警戒 | +3/−5 | 不直接 | 否 | 同上 |
| `industry_bonus` | −5～+12 | 參與等回測 | 否 | 同上 |
| 流動性 | +0～+8 | 只排序 | 前面另有硬 gate | `:392-401` |
| MoneyDJ | −5～+8 | 只排序 | 否 | `:413-416` |
| dist20>8 | −8 | `is_extended` 另可改等回測 | 否 | `:403-411` |
| 爆量長上影 | −8 | 前面通常已硬排 | 前面是 | 同上 |
| RR<1.5 | −6 | Tomorrow RR 另分類 | 否 | 同上 |
| RR<1 | 再 −14 | Tomorrow risk 另分類 | 否 | 同上 |

### 14.3 重複計分事實

- 法人條件有重疊：總買超、外資、投信、同步、連買、黃金滿貫可同時加，最後 cap 15。
- `chip_bonus_raw` 在產業計算前算一次，`_calculate_final_score()` 又用同一套規則算一次；前者供產業平均，後者作個股分數，數值相同但用途不同。
- 技術風險同時存在於原始分、分類 hard/risk gate 與 integrated risk penalty，例如 RR<1 同時原始 −10、動態 risk 一項、integrated −20。
- 距離、MACD、量價也同時用於分數與分類。本文只記錄重複存在，不判斷合理性。
- `final_score` 沒有任何「達幾分才可買」門檻；tomorrow 分類先決定可買，final score 主要排序。

### 14.4 Telegram 第二層分數（只影響推播）

- Stop 距離：≤2/+25、≤3/+20、≤4/+15、其餘 +5。
- `abs(dist20)`：≤1/+20、≤2/+17、≤3/+12、其餘 +4。
- RR：2～5/+20、1.5～2/+15、5～8/+10、>8/+5。
- MACD：負柱收斂/+15、正柱放大/+10、正柱收斂或正柱/+5。
- 法人：外資與投信都正 +10，任一正 +6；投信連買≥3 +3；外資連買≥3 +2。
- 產業共振 +5；否則產業分≥80/+3、≥60/+1。
- 再加 `final_score*0.05`，總分 cap 100。

---

## 15. 法人資料

### 15.1 資料與期間

- `foreign_buy`：外資及陸資淨買賣超張數。
- `investment_buy`：投信淨買賣超張數。
- `dealer_buy`：自營商淨買賣超張數。
- 現行整合沒有法人「前置硬篩」；所有技術 active 候選才進法人加分。
- 5 日合計使用 `institutional_trading` 全表最新 5 個 distinct date，不按個股 K 線資料日裁切。
- 連續天數對每檔取其全部法人 row 的 `tail(10)`，由最新 row 反向連續 `>0` 計數。

### 15.2 實際使用與分數

完整分數見第 14.2 節。沒有使用：買超金額、法人買超占成交量比例、外資/投信賣超的直接扣分、法人資料缺失硬排除。

`chip_tier`：

```python
黃金滿貫 = trust_consecutive>0 and foreign_consecutive>0
           and foreign_5d>0 and trust_5d>0 and dealer_5d>0
強勢雙雄 = trust_consecutive>0 and foreign_consecutive>0
投信鎖碼 = trust_consecutive>0
外資鎖碼 = foreign_consecutive>0
主力佈局 = 其他
```

`主力佈局` 只是 default label，不表示實際主力資料為正。

---

## 16. 產業共振

現行 integrated 的產業不是產業指數，也不直接使用產業漲幅、產業成交量或全產業上漲比例。它只把 **tomorrow 已留下且受 20/50/100 名單上限限制的 active 股票** 依 `stock_names.category` 分組。

對每個產業：

```python
avg_base       = mean(base_score_raw)
avg_chip_bonus = mean(chip_bonus_raw)
grade_score    = A_count/n*100
chip_norm      = min(100, avg_chip_bonus/15*100)
overheat_pen   = count(high_vol_upper_shadow or dist20>12)/n*100

industry_score = clamp(
    avg_base*0.50 + chip_norm*0.25 + grade_score*0.15
    - overheat_pen*0.10,
    0,100
)
```

狀態優先序：

1. `overheat_count/n >= 0.30` → 過熱警戒。
2. `industry_score>=90 and n>=3` → 強勢主流。
3. `>=80` → 健康偏強。
4. `>=70` → 回測機會。
5. `>=60` → 中性觀察。
6. 其他 → 弱勢產業。

個股產業共振：

```python
base_score_raw >= 60 and industry_score >= 80
```

產業 bonus 見第 14 節。舊 `screener.compute_industry_rankings()` 是另一套 40% 個股分、25% 20 日漲幅、15% 60 日漲幅、15% 法人占比、5% 突破比例、−10% 過熱比例公式；目前可見的「產業排行」頁使用舊公式，整合選股內部使用本節公式。

---

## 17. MoneyDJ 分點

### 17.1 資料來源與期間

- URL：`https://fubon-ebrokerdj.fbs.com.tw/z/zc/zco/zco_{code}_{d}.djhtm`。
- Parser：Python `HTMLParser`，從「券商分點-進出明細」表解析買超/賣超各分點。
- 可抓 1D/5D/10D/20D；整合選股固定讀 5D。
- DB 指標：分點買張、賣張、淨張、占成交量百分比。

### 17.2 MoneyDJ 區間狀態

以排序第一名的買超分點及賣超分點 `volume_ratio`：

```python
if top_sell_ratio >= top_buy_ratio*2 and top_sell_ratio>=5: 區間賣壓集中
elif top_buy_ratio >= top_sell_ratio*2 and top_buy_ratio>=5: 區間買盤集中
elif top_buy_ratio>=3 and top_sell_ratio>=3: 多空分歧/換手明顯
else: 區間中性
```

### 17.3 現行評分方式

`integrated_strategy._score_moneydj_summary()` 沒有直接讀 ratio，而是把 `period_chip_status + period_chip_reason` 做中文關鍵字比對：

| 關鍵字 | 分數 |
|---|---:|
| 集中 | +4 |
| 偏多 | +3 |
| 連買 | +3 |
| 隔日沖 | −5 |
| 賣超 | −4 |
| 轉賣 | −4 |
| 分散 | −3 |

合計 clamp −5～+8；任一負面字出現即 `broker_risk='broker_sell_pressure'`。只有 `end_date == 選股data_date` 才有效；缺資料/日期不一致均為 0。MoneyDJ 分數只進 final score 與排序，不能把 tomorrow 不合格股票升為可買，也不參與 `has_strong_chips` 的等回測判斷。

官方 `broker_analysis.py` 的 5D/10D 分點分數不會傳入 integrated。缺 MoneyDJ 時 `_empty_broker()` 回傳 0，策略繼續。

---

## 18. 最終分類邏輯

### 18.1 Tomorrow 初步分類

先執行第 3 節硬排除與第 13 節動態風險，再依第 5 節 regime：

- **明日可買**：只可能是 A，或健康回測中的 B1；精確條件見第 5.3 節及 `tomorrow_strategy.py:739-801`。
- **高優先觀察**：空頭中的逆勢 A；強多 B1 站回 cost20；A 未買成但 `abs(dist20)<=5` 且無單一動態風險；B1 站回 cost20 且 MACD OK；或 `low_amount_pass` 強制改路。
- **其他觀察**：A/B1 結構仍在，但距離、MACD、RR、regime 等未達高優先。
- **排除**：商品/資料/流動性/技術硬 gate，兩項動態風險，或 bear/weak 規則。

初步清單排序後截斷：buy 20、ETF 20、high 50、other 100；超出上限的 row 不會移入其他桶。

### 18.2 Integrated 五分類優先序

```python
if tomorrow_category == "排除": excluded
elif tomorrow_category == "明日可買": buy_candidates
else:
    is_extended = dist20>8 or stop_distance_pct>8 \
                  or (near60high and dist20>5)
    has_strong_chips = chip_bonus>=6 or chip_bonus+industry_bonus>=10
    if is_extended and has_strong_chips and grade in ("A","B1"):
        wait_pullback
    elif tomorrow_category == "高優先觀察":
        high_priority_watch
    else:
        other_watch
```

必要事實：

- final score 無分類門檻，只用來排序。
- tomorrow 可買一律保留可買，即使 final score 很低或 MoneyDJ 為負。
- `wait_pullback` 判斷早於 high，因此可把 tomorrow 的高優先/其他改成等回測。
- excluded 不再查法人、產業、MoneyDJ，整合輸出分數固定 0。
- tomorrow 的 `etf_candidates` 沒有被 integrated 合併，也沒有進 integrated excluded；它們在整合輸出中消失。

### 18.3 各桶排序

- 可買：final score 降冪 → 共振優先 → chip bonus 降冪 → RR 降冪 → `abs(dist20)` → amount 降冪。
- 高優先：A→B1→B2→C → `abs(dist20)` → MACD 狀態 → final score → industry score。
- 等回測：industry score → chip bonus → final score → `abs(dist20)`，皆相應降/升冪。
- 其他：grade → final score → `abs(dist20)`。
- 排除：final score 降冪；passthrough 通常全為 0，實際多保留原 append 順序。

---

## 19. 實際輸出範例

### 19.1 執行基準與有效性

- API effective date：2026-08-10（因個股 7/31 與行事曆差 10 天，超過 3 天 fallback）。
- 策略回傳 `data_date`：2026-07-31（函式內又被 DB 最大日期覆寫）。
- TAIEX：2026-08-07，與要求 8/10 不一致，五狀態回傳帶 `regime_error` 的預設「健康回測」。
- 法人最新日：2026-08-06，仍被 `_get_chip_data()` 用於 7/31 個股結果。
- 日期驗證：`critical_ok=false`；下列只用於說明程式行為，不是有效交易建議。
- 統計：明日可買 10、高優先 6、等回測 8、其他 24、排除 874；tomorrow 另有 ETF 候選 14 檔未進 integrated，合計原始 936 檔。

欄位說明：

- 所有列的 Market Regime 都是上述資料錯誤後 fallback 的「健康回測」。
- `D/S/H` = MACD DIF / Signal / Histogram；為依同一份個股 K 棒重算的 EMA(12,26,9)。
- `距20/距60` = `(Close-cost)/cost*100%`。
- `均額` 單位為百萬元，但仍是現行近似式 `Close × 20日均量 × 1000`，不是 20 日逐日成交額平均。
- `量倍` = 當日 volume / 20 日均量；volume 依現行程式解讀為張。
- `法人/產業/MDJ` 依序為 `chip_bonus / industry_score(industry_bonus) / broker_bonus`。
- 所有列的 Entry 都是 Close；`—` 表示程式回傳 `None` 或在硬排除前未計算。

### 19.2 明日可買：共 10 檔

| 代號 | 名稱 | 分數 | Grade | Close | cost20 | cost60 | 距20% | 距60% | MACD D/S/H |
|---|---|---:|---|---:|---:|---:|---:|---:|---|
| 2542 | 興富發 | 66 | A | 44.00 | 43.75 | 43.15 | +0.57 | +1.97 | 0.1552 / 0.1783 / -0.0231 |
| 2882 | 國泰金 | 62 | A | 101.50 | 97.50 | 96.80 | +4.10 | +4.86 | -0.0509 / -0.1873 / +0.1363 |
| 2610 | 華航 | 62 | A | 22.40 | 21.52 | 21.40 | +4.07 | +4.67 | 0.1197 / 0.0618 / +0.0579 |
| 5880 | 合庫金 | 56 | A | 27.60 | 26.02 | 25.12 | +6.05 | +9.87 | 0.6097 / 0.4643 / +0.1453 |
| 2006 | 東和鋼鐵 | 52 | A | 74.60 | 71.30 | 69.15 | +4.63 | +7.88 | 1.2176 / 0.9610 / +0.2567 |
| 2884 | 玉山金 | 51 | A | 38.65 | 36.35 | 34.88 | +6.33 | +10.81 | 0.8843 / 0.6425 / +0.2417 |
| 2867 | 三商壽 | 46 | A | 9.90 | 9.28 | 8.74 | +6.74 | +13.27 | 0.3067 / 0.2639 / +0.0428 |
| 2892 | 第一金 | 46 | A | 36.75 | 34.45 | 32.05 | +6.68 | +14.66 | 0.9865 / 0.8806 / +0.1058 |
| 2886 | 兆豐金 | 46 | A | 53.10 | 49.53 | 46.40 | +7.22 | +14.44 | 1.7040 / 1.3561 / +0.3479 |
| 2540 | 愛山林 | 42 | A | 61.00 | 57.25 | 55.83 | +6.55 | +9.26 | 1.2165 / 0.4773 / +0.7392 |

| 代號 | 20日均量 | 均額(百萬) | 量倍 | 60日高 | Entry | Stop | Target | RR | 法人/產業/MDJ | Risk flags / 最終原因 |
|---|---:|---:|---:|---:|---:|---:|---:|---:|---|---|
| 2542 | 8,827.9 | 388.4 | 0.90 | 47.10 | 44.00 | 42.50 | 47.10 | 2.07 | 6 / 45.8(0) / -4 | 無動態 risk；大盤健康回測、A、負柱收斂、RR 2.1 |
| 2882 | 26,384.7 | 2,678.0 | 1.55 | 117.50 | 101.50 | 92.50 | 117.50 | 1.78 | 12 / 55.4(0) / -4 | 無；A、正柱放大、投信鎖碼、RR 1.8 |
| 2610 | 46,284.2 | 1,036.8 | 1.65 | 24.75 | 22.40 | 21.25 | 24.75 | 2.04 | 5 / 54.0(0) / 0 | 無；A、正柱放大、RR 2.0 |
| 5880 | 23,956.8 | 661.2 | 2.05 | 27.65 | 27.60 | 25.70 | 27.65 | — | 12 / 55.4(0) / 0 | 近60日高點使 RR 無效；A、正柱放大、投信鎖碼 |
| 2006 | 2,922.6 | 218.0 | 2.74 | 74.80 | 74.60 | 69.00 | 74.80 | — | 5 / 59.2(0) / 0 | 近60日高點使 RR 無效；A、正柱放大 |
| 2884 | 48,153.3 | 1,861.1 | 1.11 | 39.10 | 38.65 | 35.85 | 39.10 | — | 12 / 55.4(0) / 0 | 近60日高點使 RR 無效；A、正柱放大、投信鎖碼 |
| 2867 | 20,421.5 | 202.2 | 2.30 | 10.05 | 9.90 | 9.20 | 10.05 | — | 7 / 55.4(0) / -4 | 近60日高點使 RR 無效；A、正柱放大 |
| 2892 | 37,493.0 | 1,377.9 | 1.61 | 36.90 | 36.75 | 34.15 | 36.90 | — | 6 / 55.4(0) / -4 | 近60日高點使 RR 無效；A、正柱放大 |
| 2886 | 31,622.6 | 1,679.2 | 1.36 | 53.70 | 53.10 | 49.05 | 53.70 | — | 6 / 55.4(0) / -4 | 近60日高點使 RR 無效；A、正柱放大 |
| 2540 | 1,134.0 | 69.2 | 3.81 | 61.80 | 61.00 | 55.90 | 61.80 | — | 3 / 45.8(0) / 0 | 近60日高點使 RR 無效；A、正柱放大 |

### 19.3 高優先觀察：實際只有 6 檔

| 代號 | 名稱 | 分數 | Grade | Close | cost20 | cost60 | 距20% | 距60% | MACD D/S/H |
|---|---|---:|---|---:|---:|---:|---:|---:|---|
| 3362 | 先進光 | 58 | A | 172.00 | 171.25 | 159.00 | +0.44 | +8.18 | -1.2200 / -0.8918 / -0.3282 |
| 2615 | 萬海 | 63 | A | 84.70 | 82.45 | 80.70 | +2.73 | +4.96 | 1.2280 / 0.9357 / +0.2923 |
| 2027 | 大成鋼 | 58 | A | 43.15 | 41.85 | 41.55 | +3.11 | +3.85 | 0.6657 / 0.4524 / +0.2133 |
| 2881 | 富邦金 | 47 | A | 130.00 | 124.75 | 116.75 | +4.21 | +11.35 | 1.1074 / 1.3808 / -0.2734 |
| 2480 | 敦陽科 | 44 | A | 166.00 | 159.25 | 155.75 | +4.24 | +6.58 | 1.1706 / 1.0934 / +0.0772 |
| 4976 | 佳凌 | 59 | B1 | 37.30 | 36.10 | 36.77 | +3.32 | +1.44 | -0.2981 / -0.5384 / +0.2403 |

| 代號 | 20日均量 | 均額(百萬) | 量倍 | 60日高 | Entry | Stop | Target | RR | 法人/產業/MDJ | Risk flags / 最終原因 |
|---|---:|---:|---:|---:|---:|---:|---:|---:|---|---|
| 3362 | 7,345.9 | 1,263.5 | 1.29 | 218.00 | 172.00 | 156.50 | 218.00 | 2.97 | 3 / 45.3(0) / +3 | MACD 尚為負柱；A、等待轉強 K 或量縮回測 |
| 2615 | 11,648.2 | 986.6 | 0.80 | 87.90 | 84.70 | 83.70 | 87.90 | 3.20 | 5 / 54.0(0) / +3 | 正柱收斂；A、等待轉強 K 或量縮回測 |
| 2027 | 17,243.7 | 744.1 | 1.00 | 45.50 | 43.15 | 42.05 | 45.50 | 2.14 | 7 / 59.2(0) / -4 | 正柱收斂；A、等待轉強 K 或量縮回測 |
| 2881 | 23,990.5 | 3,118.8 | 1.35 | 141.00 | 130.00 | 120.50 | 141.00 | 1.16 | 12 / 55.4(0) / -4 | `RR<1.5` penalty 6；A、MACD 負柱 |
| 2480 | 351.2 | 58.3 | 4.96 | 167.00 | 166.00 | 151.50 | 167.00 | — | 0 / 44.7(0) / 0 | `low_amount_pass`、近60日高；強制最多高優先 |
| 4976 | 4,166.5 | 155.4 | 2.46 | 44.90 | 37.30 | 33.95 | 44.90 | 2.27 | 5 / 45.3(0) / 0 | B1、正柱放大；等待轉強 K 或量縮回測 |

### 19.4 等回測：實際只有 8 檔

| 代號 | 名稱 | 分數 | Grade | Close | cost20 | cost60 | 距20% | 距60% | MACD D/S/H |
|---|---|---:|---|---:|---:|---:|---:|---:|---|
| 2883 | 凱基金 | 57 | A | 30.85 | 29.45 | 26.27 | +4.75 | +17.43 | 0.4795 / 0.6566 / -0.1772 |
| 2885 | 元大金 | 37 | A | 68.10 | 66.10 | 63.10 | +3.03 | +7.92 | -0.1182 / 0.0114 / -0.1296 |
| 3029 | 零壹 | 22 | B1 | 109.50 | 103.85 | 105.05 | +5.44 | +4.24 | 0.6676 / 0.5312 / +0.1364 |
| 2357 | 華碩 | 44 | B1 | 810.00 | 733.50 | 793.00 | +10.43 | +2.14 | 11.7517 / 3.2648 / +8.4869 |
| 3231 | 緯創 | 35 | B1 | 176.00 | 161.50 | 166.50 | +8.98 | +5.71 | 4.5576 / 2.4608 / +2.0968 |
| 2347 | 聯強 | 31 | B1 | 93.70 | 89.30 | 89.30 | +4.93 | +4.93 | -0.2580 / -0.6430 / +0.3849 |
| 2059 | 川湖 | 36 | B1 | 7,850.00 | 7,870.00 | 6,675.00 | -0.25 | +17.60 | 141.4481 / 298.3059 / -156.8578 |
| 2330 | 台積電 | 23 | B1 | 2,425.00 | 2,340.00 | 2,357.50 | +3.63 | +2.86 | -21.2773 / -8.3268 / -12.9506 |

| 代號 | 20日均量 | 均額(百萬) | 量倍 | 60日高 | Entry | Stop | Target | RR | 法人/產業/MDJ | Risk flags / 最終原因 |
|---|---:|---:|---:|---:|---:|---:|---:|---:|---|---|
| 2883 | 54,201.2 | 1,672.1 | 1.62 | 31.30 | 30.85 | 28.35 | 31.30 | — | 12 / 55.4(0) / 0 | 停損距離 8.1%；籌碼佳，等回 cost20 |
| 2885 | 33,212.8 | 2,261.8 | 1.35 | 72.30 | 68.10 | 60.70 | 72.30 | 0.57 | 12 / 55.4(0) / 0 | `RR<1`、停損距離 10.9%，risk penalty 20 |
| 3029 | 1,280.6 | 140.2 | 3.21 | 116.00 | 109.50 | 97.70 | 116.00 | 0.55 | 14 / 44.7(0) / 0 | `RR<1`、停損距離 10.8%，risk penalty 20 |
| 2357 | 4,507.9 | 3,651.4 | 2.05 | 964.00 | 810.00 | 722.00 | 964.00 | 1.75 | 12 / 44.3(0) / 0 | 距20 10.4%、停損距離 10.9%，penalty 8 |
| 3231 | 72,687.5 | 12,793.0 | 1.89 | 201.00 | 176.00 | 157.50 | 201.00 | 1.35 | 12 / 44.3(0) / 0 | 距20 9.0%、停損 10.5%、`RR<1.5`，penalty 14 |
| 2347 | 8,528.0 | 799.1 | 1.37 | 98.30 | 93.70 | 83.40 | 98.30 | 0.45 | 14 / 40.2(0) / 0 | `RR<1`、停損距離 11.0%，penalty 20 |
| 2059 | 603.1 | 4,734.7 | 0.35 | 8,900.00 | 7,850.00 | 6,840.00 | 8,900.00 | 1.04 | 9 / 36.5(0) / 0 | `low_amount_pass`、停損 12.9%、`RR<1.5`，penalty 6 |
| 2330 | 34,362.1 | 83,328.1 | 1.66 | 2,535.00 | 2,425.00 | 2,180.00 | 2,535.00 | 0.45 | 6 / 32.5(0) / 0 | `RR<1`、停損距離 10.1%，penalty 20 |

### 19.5 其他觀察前 10（實際共 24 檔）

| 代號 | 名稱 | 分數 | Grade | Close | cost20 | cost60 | 距20% | 距60% | MACD D/S/H |
|---|---|---:|---|---:|---:|---:|---:|---:|---|
| 2646 | 星宇航空 | 52 | A | 21.65 | 21.55 | 21.07 | +0.46 | +2.75 | 0.1505 / 0.1464 / +0.0040 |
| 2633 | 台灣高鐵 | 40 | A | 26.05 | 26.05 | 25.85 | 0.00 | +0.77 | 0.0004 / 0.0089 / -0.0085 |
| 2618 | 長榮航 | 32 | A | 43.30 | 41.52 | 39.88 | +4.27 | +8.58 | 0.3968 / 0.4197 / -0.0229 |
| 6277 | 宏正 | 30 | A | 80.20 | 77.10 | 76.75 | +4.02 | +4.50 | 1.4221 / 1.2318 / +0.1903 |
| 2889 | 國票金 | 29 | A | 15.70 | 15.43 | 15.28 | +1.78 | +2.75 | 0.1349 / 0.1687 / -0.0338 |
| 2880 | 華南金 | 26 | A | 43.70 | 40.62 | 37.38 | +7.57 | +16.91 | 1.4990 / 1.3138 / +0.1851 |
| 3441 | 聯一光 | 26 | A | 84.00 | 76.35 | 66.90 | +10.02 | +25.56 | 2.1103 / 0.7392 / +1.3711 |
| 2890 | 永豐金 | 25 | A | 40.60 | 39.95 | 35.47 | +1.63 | +14.46 | 0.5006 / 0.7684 / -0.2678 |
| 3005 | 神基 | 24 | A | 114.00 | 110.50 | 107.90 | +3.17 | +5.65 | 1.6634 / 2.1059 / -0.4424 |
| 2887 | 台新新光金 | 24 | A | 35.95 | 34.80 | 30.30 | +3.30 | +18.65 | 0.6762 / 1.0370 / -0.3608 |

| 代號 | 20日均量 | 均額(百萬) | 量倍 | 60日高 | Entry | Stop | Target | RR | 法人/產業/MDJ | Risk flags / 最終原因 |
|---|---:|---:|---:|---:|---:|---:|---:|---:|---|---|
| 2646 | 5,702.4 | 123.5 | 1.21 | 22.45 | 21.65 | 21.15 | 22.45 | 1.60 | 5 / 54.0(0) / 0 | 下跌放量；A、持續觀察 |
| 2633 | 6,678.6 | 174.0 | 1.00 | 26.90 | 26.05 | 25.70 | 26.90 | 2.43 | 0 / 54.0(0) / 0 | MACD 負柱擴大；A、持續觀察 |
| 2618 | 54,330.9 | 2,352.5 | 1.26 | 45.65 | 43.30 | 40.75 | 45.65 | 0.92 | 7 / 54.0(0) / 0 | `RR<1`，penalty 20；A、持續觀察 |
| 6277 | 802.1 | 64.3 | 0.95 | 85.00 | 80.20 | 74.20 | 85.00 | 0.80 | 12 / 44.3(0) / 0 | `low_amount_pass`、`RR<1`，penalty 20 |
| 2889 | 8,160.9 | 128.1 | 1.43 | 16.10 | 15.70 | 15.15 | 16.10 | 0.73 | 0 / 55.4(0) / 0 | `RR<1`，penalty 20；A、持續觀察 |
| 2880 | 24,966.2 | 1,091.0 | 1.46 | 45.15 | 43.70 | 40.70 | 45.15 | 0.48 | 9 / 55.4(0) / 0 | `RR<1`，penalty 20；A、持續觀察 |
| 3441 | 10,170.6 | 854.3 | 1.59 | 95.80 | 84.00 | 75.00 | 95.80 | 1.31 | 0 / 45.3(0) / +3 | 距20>8、`RR<1.5`，penalty 14 |
| 2890 | 41,047.9 | 1,666.5 | 1.19 | 42.30 | 40.60 | 38.00 | 42.30 | 0.65 | 5 / 55.4(0) / 0 | `RR<1`，penalty 20；A、持續觀察 |
| 3005 | 5,614.2 | 640.0 | 1.40 | 120.00 | 114.00 | 104.00 | 120.00 | 0.60 | 5 / 44.3(0) / 0 | `RR<1`，penalty 20；MACD 負柱 |
| 2887 | 77,843.1 | 2,798.5 | 1.21 | 37.75 | 35.95 | 31.85 | 37.75 | 0.44 | 5 / 55.4(0) / 0 | `RR<1`，penalty 20；MACD 負柱 |

### 19.6 排除範例 10 檔

排除 passthrough 不再補法人、產業、MoneyDJ 或 final score；因此這些欄位固定為 0，若在商品/資料 gate 就被排除，技術欄位也尚未產生。

| 代號 | 名稱 | Close | Grade | cost20 / cost60 | MACD | 均量 / 均額 | Stop / RR | 精確排除原因 |
|---|---|---:|---|---|---|---|---|---|
| 00401A | 主動摩根台灣鑫收 | — | — | — | — | — | — | 資料不足 62 根（僅 60 根） |
| 00632R | 元大台灣50反1 | — | — | — | — | — | — | 反向ETF：預設排除，不納入任何候選清單 |
| 00647L | 元大S&P500正2 | — | — | — | — | — | — | 槓桿ETF：預設排除，不納入任何候選清單 |
| 00717 | 富邦美國特別股 | — | — | — | — | — | — | 特別股：不納入選股候選 |
| 2945 | 三商家購 | — | — | — | — | — | — | 權證：預設排除（`INCLUDE_WARRANT=False`）；實際是名稱含「購」誤判 |
| 1259 | 安心 | — | — | — | — | — | — | 資料未同步：最後K線日期 2026-07-30，不等於 data_date 2026-07-31 |
| 1102 | 亞泥 | 33.00 | C | 34.55 / 34.55 | 負柱收斂 | 16,767.6 / 553.3百萬 | 32.25 / 5.47 | C級：收盤(33.0)跌破60日成本線(34.55) |
| 1476 | 儒鴻 | 346.00 | B1 | 336.25 / 337.00 | 正柱收斂 | 1,806.3 / 625.0百萬 | 328.00 / 1.14 | 高檔爆量長上影，收盤遠低於當日高點，籌碼疑問 |
| 2395 | 研華 | 563.00 | A | 555.50 / 520.75 | 負柱擴大 | 4,127.8 / 2,323.9百萬 | 529.00 / 1.62 | MACD負柱擴大 + 下跌放量，兩項明顯風險 |
| 2206 | 三陽工業 | 64.80 | A | 63.05 / 61.70 | 正柱放大 | 1,597.9 / 103.5百萬 | 62.50 / 0.83 | 下跌放量 + 風報比不足(<1)，兩項明顯風險 |

這些輸出也直接證實幾個流程事實：final score 不是分類 gate；RR 無效的近高股票仍可在健康回測成為可買；`wait_pullback` 可包含 tomorrow 已帶「風報比不足」的股票；排除股票不做後續整合評分。

---

## 20. 回測現況

目前尚未建立完整選股歷史回測。

專案中沒有：

- 依歷史日期重建股票池與逐日產生訊號的 runner。
- 下一交易日 Entry fill、Exit、Stop/Target 成交模擬。
- 手續費、交易稅、滑價、Gap、漲跌停、除權息處理。
- Trades、Win Rate、Profit Factor、Expectancy、Max Drawdown、年度結果或 A/B1 分類績效輸出。

目前 `run_integrated_strategy(data_date=...)` 不能當作歷史回測入口，因個股 K 與法人沒有依 `data_date` 截斷，存在 look-ahead。

---

## 21. 現行規則總表

| Rule ID | 階段 | 規則 | Threshold | 類型 | 最終影響 | Code Location |
|---|---|---|---|---|---|---|
| DATA-01 | 有效日 | DB 最新日只有距行事曆 0～3 天才取代行事曆日 | 3 calendar days | Data gate | 影響大盤要求日 | `main.py:5729-5763` |
| DATA-02 | 個股新鮮度 | 每檔最後 K 日須等於全市場最大 K 日 | equality | Hard gate | 不同即排除 | `tomorrow_strategy.py:968-1032` |
| DATA-03 | 最少 K | `len>=62` | 62 | Hard gate | 不足排除 | `:416-417` |
| DATA-04 | 大盤日期 | 大盤最後日須等於要求日 | equality | Error/fallback | 不符卻回健康回測 + regime_error | `:245-262` |
| TYPE-01 | ETN | 名稱/產業含 ETN | keyword | Hard gate | 排除 | `:135-137,1013-1016` |
| TYPE-02 | 權證 | 名稱含購/售/權證 | keyword | Hard gate | 排除 | `:139-141,1017-1020` |
| TYPE-03 | 特別股 | 名稱 suffix/keyword | keyword | Hard gate | 排除 | `:143-150,1021-1024` |
| TYPE-04 | 反向 ETF | 關鍵字或代號 R | keyword | Hard gate | 排除 | `:157-160,1005-1008` |
| TYPE-05 | 槓桿 ETF | 正2/2倍/槓桿等 | keyword | Hard gate | 排除 | `:161-164,1009-1012` |
| TYPE-06 | 普通 ETF | `00*`/ETF 文字 | — | Separate path | 不混普通股；integrated 遺失 | `:152-164,1065-1081` |
| LIQ-01 | 高流動 | vol≥3000 AND amount≥100M | 3000/100M | Layer | 通過 | `:1052-1063` |
| LIQ-02 | 一般 | vol≥1000 AND amount≥50M | 1000/50M | Layer | 通過 | 同上 |
| LIQ-03 | 低張數金額過 | vol<1000 AND amount≥50M | 1000/50M | Downgrade | 最多高優先 | `:1059-1060,1117-1121` |
| LIQ-04 | 低流動 | 其餘 | — | Hard gate | 排除 | `:1083-1089` |
| REG-01 | 空頭破60 | close<c60 OR(c20<c60 AND MACD負柱擴大) | — | Market gate | 禁買，多數排除 | `:347-356` |
| REG-02 | 弱勢反彈 | close<c20、>=c60、slope<0、hist<0、非負柱收斂 | — | Market gate | 非A排除，A觀察 | `:358-368` |
| REG-03 | 高檔過熱 | close>c20 AND dist20>8 | 8% | Market gate | 只低乖離A可買 | `:370-376` |
| REG-04 | 強多延伸 | close>c20>c60 AND MACD OK | — | Market gate | A嚴格，B1禁買 | `:378-385` |
| REG-05 | 健康回測 | close>=c60 AND dist20∈[-5,5] AND負柱收斂 | ±5% | Market gate | A/B1條件式可買 | `:387-395` |
| GRD-01 | A | close>c20、close>c60、c20>c60 | strict `>` | Class | 主要可買集合 | `:526-545` |
| GRD-02 | B2 | >c60、不>c20、slope<0、downvol或MACD擴大 | — | Hard gate | 排除 | 同上、`:666-668` |
| GRD-03 | B1 | >c60 且非A/B2 | — | Class | 可買/觀察依 regime | 同上 |
| GRD-04 | C | close<=c60 | — | Hard gate | 排除 | 同上、`:663-665` |
| COST-01 | cost20/60 | Donchian high/low midpoint | 20/60 | Indicator | regime/grade/score | `:171-173` |
| MACD-01 | MACD | EMA 12/26/9 | 12/26/9 | Indicator | score/class/risk | `:176-206` |
| VOL-01 | 量縮 | vol<volMA20 | 1.0x | Score/condition | +8；B1 buy 條件 | `:481-500,613-623` |
| VOL-02 | 下跌放量 | close<open AND vol>1.2×MA20 | 1.2x | Risk/score | −10、一項 risk | 同上 |
| VOL-03 | 放量 | vol>1.2×MA20，且非前項 | 1.2x | Score | close>c20 時 +7 | 同上 |
| VOL-04 | 爆量長上影 | vol>1.5×MA20 AND shadow/range>0.4 | 1.5x/40% | Hard gate | 排除 | `:486-500,669-671` |
| DIST-01 | 遠離20 | dist20>12 | 12% | Hard gate | 排除 | `:672-674` |
| DIST-02 | 一般可買 | abs(dist20)<=8 | 8% | Buy gate | 健康 A | `:722,760-768` |
| DIST-03 | 強多/過熱可買 | abs(dist20)<=3 | 3% | Buy gate | A | `:723,741-777` |
| DIST-04 | A高優先 | abs(dist20)<=5且零動態風險 | 5% | Category | 高優先 | `:837-840` |
| HIGH-01 | 60日高 | rolling max(high,60)，含今日 | 60 | Target | RR Target | `:502-505` |
| HIGH-02 | 近60高 | close>=high60×0.98 | 98% | Risk/position | RR無效、阻追 | `:503-506,512-524` |
| RR-01 | Stop | 近3日 low 最低 | 3 | Risk | Stop | `:508-510` |
| RR-02 | RR | (high60-close)/(close-stop) | — | Indicator | score/buy/risk | `:512-524` |
| RR-03 | 強多最低RR | 有效 RR>=1.5 | 1.5 | Buy gate | 不符禁買 | `:729-733,741-751` |
| RISK-01 | 動態風險 | MACD擴大、下跌放量、有效RR<1 | 3 flags | Risk | 兩項即排除 | `:676-687` |
| SLOPE-01 | 強多斜率確認 | slope>=0 OR近3日cost20彈升 OR紅K突破前高 | — | Downgrade | 否則 buy→high | `:1096-1115` |
| SCORE-01 | 技術原始分 | 第14.1節 | 0～100 | Score | tomorrow 排序 | `:590-645` |
| CHIP-01 | 法人5日 | 最新5個法人日期合計 | 5 dates | Score | +0～15 | `integrated_strategy.py:44-122,367-376` |
| CHIP-02 | 法人連買 | 每檔尾端最多10 row連續>0 | >=3得分 | Score | +2/+3 | 同上 |
| IND-01 | 產業分 | active member 加權公式 | 0～100 | Score | bonus/共振 | `:298-346` |
| IND-02 | 共振 | base_raw>=60 AND industry>=80 | 60/80 | Score/tag | +5且參與強籌碼 | `:344-346,387` |
| MDJ-01 | 日期 | end_date==data_date | equality | Score gate | 否則0 | `:140-170` |
| MDJ-02 | 關鍵字 | 集中/偏多/連買/隔日沖/賣超/轉賣/分散 | −5～+8 | Score | 排序 | `:172-210` |
| FS-01 | base權重 | round(raw×0.65) | 65% | Score | final score | `:351-356` |
| FS-02 | 流動 bonus | amount/volume 分層 | 50/100/300M;1000/3000 | Score | +0～8 | `:392-401` |
| FS-03 | risk penalty | dist、shadow、RR | 8%;1.5;1.0 | Score | −0～20+ | `:403-411` |
| CAT-01 | tomorrow 否決 | ts_cat排除不可升級 | — | Hard gate | final excluded | `:450-456` |
| CAT-02 | 可買保留 | ts_cat明日可買 | — | Category | final buy | 同上 |
| CAT-03 | 等回測 | extended AND strong chips AND grade A/B1 | 8%/10 bonus | Category | wait_pullback | `:458-465` |
| CAT-04 | 高優先 | 非等回測且 ts_cat高優先 | — | Category | high | `:467-472` |
| TG-01 | TG基本 gate | grade A、absdist<=3、RR>=1.5、stop<=5.5、非負柱擴大 | 多門檻 | Output gate | 才入TG候選 | `main.py:4218-4227` |
| TG-02 | 精選/備選 | stop<=4且MACD收斂/放大為精選 | 4% | Output category | 最多3/2 | `:4229-4269` |

### 21.1 仍可呼叫但非預設的舊六步驟規則摘要

| Rule ID | 舊路徑規則 | 實際條件 | 位置 |
|---|---|---|---|
| LEG-01 | 法人前置硬篩 | 最新5個法人日期 total>0 AND(foreign>0 OR trust>0) | `screener.py:287-303,621-628` |
| LEG-02 | 當日跌幅 | 有 quote 且 change_pct<−3.5 排除；無 quote 不因此排除 | `:630-655` |
| LEG-03 | 流動性 | amountMA5/20 任一≥30M OR volumeMA20≥1000；缺資料排除 | `:765-821` |
| LEG-04 | SMA 多頭 | close>SMA20>SMA60 | `:823-828` |
| LEG-05 | 20日強度 | return20>1.5 | `:830-846` |
| LEG-06 | 60日強度 | return60>4 只加分，不再硬篩 | `:847-852` |
| LEG-07 | 舊大盤 | weak/hot/overheated/normal 四狀態，SMA20/60 | `market_status.py:345-473` |
| LEG-08 | 舊產業 | 40%分數+25%R20+15%R60+15%法人占比+5%突破−10%過熱 | `screener.py:1701-1791` |

---

## 22. Uncertainties / Potential Inconsistencies

以下只列現況，不提出修正方案。

1. **資料日期覆寫**：`run_tomorrow_strategy(data_date=...)` 先用傳入日期算大盤，之後在 968～973 行把 `data_date` 重設為個股 DB 全市場最新日；回傳結果可能同时含「要求日、個股日、大盤日」三種日期。
2. **目前實際日期無效但 API status 仍 success**：稽核時個股 7/31、大盤 8/7、法人 8/6；`/api/integrated-strategy` 仍回策略 `status=success`，另以 `market_regime_success=false/data_validation.errors` 表示異常。排程 Telegram 會阻擋，網頁仍渲染清單。
3. **法人 look-ahead**：`_get_chip_data()` 無 `date<=data_date`，7/31 個股結果用了 8/3～8/6 法人資料；歷史日期執行同樣存在。
4. **個股歷史 look-ahead**：傳入歷史 `data_date` 不會裁切 `daily_kbars`。
5. **大盤錯誤 fallback**：要求日不一致不是中止策略，而是回 `healthy_pullback`；股票分類會依健康回測規則繼續。
6. **個股日 K 不再更新**：`main.py` 明示富邦目前只接期貨，`sync_all_stock_screener_data()` 只檢查本地個股 DB；舊 `sync_stock_kbars()` 需要 legacy stock API，但現行流程不呼叫。
7. **註解與流動性實作不一致**：`tomorrow_strategy.py` 頂部註解寫「volume>=1000 OR amount>=3000萬」，實際是 normal 要 `volume>=1000 AND amount>=5000萬`，另有 low_amount_pass。
8. **RR 門檻文案與實作不完全一致**：策略文案常寫 RR≥1.5，但健康/過熱的 `rr_ok` 將 `rr_valid=False` 視為通過，稽核輸出確有 RR=None 的可買股。
9. **單項風險命名**：變數名 `hard_excl=True` 但一項動態風險不是立即排除，只是不能買；中文容易誤讀。
10. **C 級 equality**：條件是 `close<=cost60`，理由文字卻寫「跌破60日成本線」；剛好等於也會被稱為跌破。
11. **ETF 在 integrated 遺失**：本次 tomorrow 有 14 檔 `etf_candidates`，integrated 不接此桶；它們既非五分類亦非 excluded，所以 936 原始檔只在 integrated summary 計到 922。
12. **ETF/特別股/權證文字誤判**：分類先判「名稱含購/售」，本次 `2945 三商家購` 被判權證；先判特別股再判 ETF，`00717 富邦美國特別股` 被判特別股。此為實際輸出。
13. **未使用的開關**：`INCLUDE_REVERSE_ETF=False` 有宣告，但 reverse/leveraged 分支無論開關都硬排；`instrument_type` 文件列 `other`，函式實際 default 回 common_stock。
14. **MoneyDJ 註解衝突**：`moneydj_fetcher.py` 模組註解寫「does not feed strategy scores」，但 `integrated_strategy.py` 確實把其摘要轉成 `broker_bonus`。
15. **MoneyDJ 以文案關鍵字計分**：區間中性/賣壓文字常含「賣超第一名」，會命中 −4；「區間賣壓集中」同時命中「集中」+4 與「賣超」−4，可能淨 0 但仍標 broker_sell_pressure。它不是直接按 ratio 符號計分。
16. **MoneyDJ 只同步候選前 30**：手動/排程只對 buy/high/wait 候選依目前順序最多 30 檔同步，其他 active 候選可能長期維持 0/舊資料。
17. **兩套成本線**：integrated/tomorrow 的 cost 是 Donchian midpoint；舊 screener 的 cost 欄是 SMA。前端不同頁面同叫「成本線」但公式不同。
18. **兩套大盤與產業演算法**：五狀態+integrated 產業與四狀態+舊產業 API 同時存在；可見「產業排行」不是整合選股內部產業分。
19. **產業樣本是已篩且已截斷集合**：integrated 產業分不是全市場產業寬度；excluded、ETF 及 tomorrow 上限外股票不參與。
20. **成交額算法差異**：tomorrow 用「今日 close × 20日均量」，舊 screener 用「逐日 close×volume 的20日平均」。
21. **法人負值換算**：官方股數使用 `//1000`，負的非整千數會 floor 而非向零截斷。
22. **調整價格未知**：無 adjusted flag/企業行動表，是否還原權息待確認。
23. **市場別未知**：`stock_names` 無 TSE/OTC 欄位；同一 category 無法還原市場。
24. **上市/下市與 survivorship**：無歷史股票池/狀態欄位，無法確認 DB 是否保留所有下市股，也無法做無 survivorship bias 的回測。
25. **final score 無分類門檻**：低 final score 的 tomorrow 可買仍是可買；MoneyDJ/法人/產業大多只改排序。這與 UI 上強調總分可能造成語意差距，但實作明確。
26. **風險重複作用**：同一 RR/距離/量價可同時影響原始分、hard/risk gate、integrated penalty、TG gate；不是互斥規則。
27. **60 日高包含今日**：Target 可被當日 high 推高，且 near-high 會直接令 RR 無效；程式沒有「只看昨日以前」版本。
28. **沒有正式選股自動測試**：目前 repo 的測試檔集中在期貨 K 棒、價差、振幅與加權報價，沒有 tomorrow/integrated 的回歸測試檔。
29. **舊說明文件可能過期**：例如 `current_strategy_logic_audit.md` 的舊 screener 流動性/60日強度描述與目前 `screener.py` 已不同；本文件採程式實作。
