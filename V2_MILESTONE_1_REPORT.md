# TXF Pro Viewer 選股系統 V2 — Milestone 1 報告

> 範圍：Phase 0～6  
> 基準 commit：55036b328d84fb61ea62196764956d5f541f41cc  
> 實作日期：2026-08-10（Asia/Taipei）  
> 本輪停止點：Milestone 1；尚未開始 Phase 7～14 / Milestone 2。

## 1. 完成摘要

本次只處理 correctness 與 regression safety：

- 建立固定 regression baseline、可重建 SQLite fixture 與選股專屬測試套件。
- 選股內部建立唯一 as_of_date 邊界；日 K、大盤、法人與 MoneyDJ 都不得讀取 date > as_of_date。
- 個股或大盤必要日期不一致時 fail closed：strategy_valid=false，不產生任何可買或 Telegram 精選。
- 建立 security_master；商品分類優先使用 master，名稱只作保守 fallback。
- 修正 2945 三商家購被單一「購」字誤判為權證的問題。
- 修正 amount_ma20 為逐日成交額的 20 日平均，並增加 amount MA5、流動性趨勢與 percentile shadow metrics。
- 法人 5 日合計及連買改為 point-in-time 查詢；新同步資料保留官方原始股數。
- RR=None 不再通過可買；RR target 改用訊號日前 60 日高點。
- 增加 signal_close、signal_rr、max_entry_rr15、target_status 與隔日進場價 skip helper。
- UI 在資料無效時明確顯示原因，並清空候選列表。

以下核心規則刻意未改：A/B1/B2/C、Donchian midpoint cost20/cost60、MACD 12/26/9、流動性 1,000 張與 5,000 萬門檻、既有 final score 權重。

---

## 2. Phase 0：修改前基準

### 2.1 保留項目

- 現況稽核：CURRENT_STOCK_SELECTION_SYSTEM.md
- Git 基準：55036b328d84fb61ea62196764956d5f541f41cc
- 修改前 normalized API snapshot：tests/stock_selection/fixtures/pre_v2_integrated_output.json
- 修改後 regression snapshot：tests/stock_selection/fixtures/post_v2_m1_regression_output.json
- 固定 DB fixture spec：tests/stock_selection/fixtures/fixed_db_fixture.json
- pytest 每次依 spec 在 temp directory 重建 SQLite DB，不依賴開發者目前的 stock_cache.db。

修改前 snapshot 保存所有非排除候選的代號、分類、分數、RR、分類總數、資料日期錯誤與基準 commit；874 筆排除資料只保存 count，避免把大型、會隨本機 DB 改變的完整 dump 放進 repo。

### 2.2 新增測試分類

tests/stock_selection/ 包含：

- market regime tests
- A/B1/B2/C grade tests
- RR / previous-high / max-entry tests
- liquidity / amount tests
- instrument classification tests
- integrated classification tests
- date consistency / fail-closed tests
- institutional point-in-time tests
- MoneyDJ point-in-time tests
- baseline artifact tests

---

## 3. Phase 1：資料日期一致性

### 3.1 唯一 point-in-time 邊界

正式入口改用：

    run_tomorrow_strategy(as_of_date=...)
    run_integrated_strategy(as_of_date=...)

舊 data_date= 只保留為相容 alias；進入函式後立即轉成 as_of_date。如果兩者同時傳入且不同，直接回無效結果。

| 資料 | 實作 |
|---|---|
| daily_kbars | SQL WHERE date <= as_of_date；所有 rolling indicator 使用切割後資料 |
| market_index_daily / TAIEX | 先切 date <= as_of_date，最後日期必須等於 as_of_date |
| institutional_trading | 5 日合計、連買與資料日都只查 date <= as_of_date |
| MoneyDJ | MAX(end_date) 增加 end_date <= as_of_date |
| 產業 V1 | 輸入股票已由 as_of_date 切割；本輪未改產業公式 |

### 3.2 Fail Closed

以下任一成立即回 strategy_valid=false 及所有候選空陣列：

- as_of_date 缺失且 DB 也無法決定日期。
- 全市場可用個股 K 棒最後日期不等於 as_of_date。
- TAIEX 最後日期不等於 as_of_date。
- 個股或大盤必要資料不足 62 根。
- 大盤 Donchian 成本線或 MACD 無法計算。
- DB 必要資料讀取失敗。

不再將以上錯誤偽裝成 healthy_pullback。大盤無效狀態為 data_invalid / 資料無效。

### 3.3 Telegram 與 UI

- build_tg_pick_list() 在 strategy_valid=false 時回空清單及 blocked=true。
- 網頁整合選股頁顯示紅色「資料無效，選股已停止」卡片及實際錯誤原因。
- 無效時清空先前 buy/high/wait/other/excluded DOM，避免畫面殘留舊結果。

---

## 4. Phase 2：商品分類與 security_master

### 4.1 Schema

新增 security_master：

| 欄位 |
|---|
| code |
| name |
| market |
| security_type |
| industry |
| listing_date |
| delisting_date |
| is_etf |
| is_leveraged |
| is_inverse |
| is_etn |
| is_warrant |
| is_preferred |
| source |
| updated_at |

Runtime migration 是 idempotent；stock_names 只會 INSERT OR IGNORE 回填，不會覆蓋人工或未來正式來源建立的 master row。

### 4.2 分類優先序

1. security_master flags。
2. security_master.security_type。
3. 沒有 master row 才使用保守名稱/代號 fallback。

Fallback 不再因名稱單獨包含「購」或「售」判權證；只接受明確「權證」metadata。Regression test 已固定：

    2945 三商家購 => common_stock

實際 DB migration 後，2945 的 security_type=common_stock。在 2026-07-31 控制執行中，它仍因流動性不足被排除，但排除原因已不再是權證誤判。

### 4.3 實際 migration 狀態

| 項目 | 數量 |
|---|---:|
| security_master rows | 936 |
| common_stock | 772 |
| ETF | 134 |
| inverse ETF | 11 |
| leveraged ETF | 9 |
| preferred stock | 10 |
| market 待正式來源補值 | 936 |
| listing_date 待正式來源補值 | 936 |
| delisting_date 待正式來源補值 | 936 |

---

## 5. Phase 3：成交額與流動性

修正前：

    amount_ma20 = latest_close * volume_ma20 * 1000

修正後：

    daily_amount = close * volume * 1000
    amount_ma5 = daily_amount.rolling(5).mean()
    amount_ma20 = daily_amount.rolling(20).mean()
    liquidity_trend = amount_ma5 / amount_ma20

新增 shadow-only：

- amount_rank：as_of_date 當下可計算 20 日資料股票的成交額 percentile。
- volume_rank：同一集合的 20 日均量 percentile。
- liquidity_trend。

既有 gate 門檻保留：

- high：volume_ma20 >= 3000 AND amount_ma20 >= 100M
- normal：volume_ma20 >= 1000 AND amount_ma20 >= 50M
- low amount pass：volume_ma20 < 1000 AND amount_ma20 >= 50M
- 其餘 low hard exclusion

---

## 6. Phase 4：法人 point-in-time

_get_chip_data(codes, as_of_date) 現在只使用 latest 5 DISTINCT institutional dates WHERE date <= as_of_date。連買明細也限制 date <= as_of_date，不再讀整張表最後五日或未來日期。

institutional_trading 新增：

- foreign_buy_shares
- investment_buy_shares
- dealer_buy_shares

新的 TWSE/TPEx 同步會保存官方原始股數，並另外寫既有張數欄位。舊 24,669 rows 無法還原被整除前的精確股數，所以三個新欄位目前是 NULL；查詢層暫以既有張數×1000 作相容顯示。此限制列入未解問題，不偽稱為精確原始值。

---

## 7. Phase 5：RR correctness

### 7.1 Strict buy gate

正式可買條件統一為：

    rr_buyable = rr_valid and risk_reward >= 1.5

RR=None、target 無效、risk<=0、reward<=0 都不能進入「明日可買」。它們仍可依其他條件進高優先、等回測、其他觀察或排除。

### 7.2 Previous 60-day high

新增並區分：

    previous_60d_high = max(high[t-60:t-1])
    current_60d_high  = max(high[t-59:t])

- previous_60d_high：RR target 與 near-high 判斷。
- current_60d_high：只供顯示。

如果 signal_entry >= previous_60d_high：

    target_status = breakout_no_defined_target
    risk_reward = None
    rr_buyable = false

尚未建立突破後 target 模型，因此不產生假 RR。

---

## 8. Phase 6：明日最高可接受進場價

新增輸出：

- signal_close
- signal_entry（與 signal_close 相同；明確表示只是訊號日價格）
- stop_price
- target_price
- signal_rr
- max_entry_rr15
- actual_entry（選股當下為 null）
- skip_trade（尚無隔日實價時為 null）

公式：

    max_entry_rr15 = (target + 1.5 * stop) / 2.5

並提供 evaluate_actual_entry(actual_entry, max_entry_rr15)：

- actual entry 高於 max entry → True（skip）。
- target 無效而已有 actual entry → True。
- 還沒有 actual entry → None。

正式 UI 欄位重排屬 Phase 19，本輪只完成 API 欄位與 fail-closed UI；沒有提前開始 Milestone 2/4。

---

## 9. Before / After 選股差異

### 9.1 實際 production as_of_date = 2026-08-10

> 此表是 Milestone 1 初次完成時、個股日 K 尚停在 2026-07-31 的固定 regression snapshot。後續同步按鈕修正已使用 TWSE／TPEx 官方日行情補齊至 2026-08-10；目前 strategy_valid=true。原 snapshot 保留作為 fail-closed regression case。

| 指標 | Before | After | 導致差異的 Rule |
|---|---:|---:|---|
| strategy valid | 日期驗證 false 但仍產生清單 | false | Phase 1 fail closed |
| 個股資料日 | 2026-07-31 | 2026-07-31 | — |
| 大盤資料日 | 2026-08-10 | 2026-08-10 | — |
| 法人資料日 | 2026-08-06，且會用於 7/31 個股 | 只允許 <= as_of；整體已因個股過期停止 | Phase 4 PIT |
| 明日可買 | 10 | 0 | 個股日 != as_of_date，整批停止 |
| 高優先 | 6 | 0 | 同上 |
| 等回測 | 8 | 0 | 同上 |
| 其他 | 24 | 0 | 同上 |
| 排除 | 874 | 0（未執行逐檔分類） | fail closed 在分類前停止 |

修改前 10 檔可買中有 7 檔 risk_reward=null；修改後即使日期恢復一致，這 7 檔也不可能再因 RR 無效而自動通過買進 gate。

### 9.2 同一資料切點控制組：as_of_date = 2026-07-31

此組用來區分日期 fail closed 與其他 correctness 規則。TAIEX 當日狀態是 bear_break60，所以 Before/After 都沒有明日可買。

| 分類 | Before | After |
|---|---:|---:|
| 明日可買 | 0 | 0 |
| 高優先 | 10 | 8 |
| 等回測 | 7 | 8 |
| 其他 | 0 | 0 |
| 排除 | 905 | 906 |

逐檔變動：

| 股票 | Before → After | 直接原因 |
|---|---|---|
| 2480 敦陽科 | 高優先 → 排除 | previous-high target 使 signal RR=0.07；RR<1 與空頭市場風險形成兩項風險 |
| 2883 凱基金 | 等回測 → 排除 | previous-high target 使 signal RR=0.18；RR<1 與空頭市場風險形成兩項風險 |
| 2540 愛山林 | 高優先 → 等回測 | breakout_no_defined_target，tomorrow 保留觀察；籌碼與停損距離使 integrated 轉等回測 |
| 2880 華南金 | 排除 → 等回測 | previous-high 判定為突破、RR 不再偽算為低 RR；空頭下先保留觀察，再因籌碼/位置轉等回測 |
| 2945 三商家購 | 權證排除 → 普通股流動性排除 | security_master/fallback 修正；實際 20 日均量與成交額仍不過 gate |

這些是 correctness 規則造成的可解釋差異，沒有調整 MACD、grade 或流動性 threshold。

---

## 10. 修改與新增檔案

### 10.1 修改檔案

| 檔案 | 修改內容 |
|---|---|
| tomorrow_strategy.py | as_of_date、fail closed、security master、正確 amount、liquidity shadows、previous high、strict RR、max entry |
| integrated_strategy.py | PIT 法人/MoneyDJ、invalid passthrough、新欄位傳遞 |
| main.py | as_of_date 呼叫、日期驗證、Telegram invalid guard |
| screener.py | schema migration hook、法人原始股數保存、TWSE／TPEx 官方全市場日 K 缺日回補與完整性檢查 |
| moneydj_fetcher.py | end_date <= as_of_date |
| static/app_pro.js | invalid-data 明確顯示與清空候選 |
| requirements.txt | 增加 pytest 測試依賴 |

### 10.2 新增檔案

- stock_selection_schema.py
- migrations/001_stock_selection_v2_milestone1.sql
- tests/stock_selection/conftest.py
- tests/stock_selection/fixtures/fixed_db_fixture.json
- tests/stock_selection/fixtures/pre_v2_integrated_output.json
- tests/stock_selection/fixtures/post_v2_m1_regression_output.json
- tests/stock_selection/test_baseline.py
- tests/stock_selection/test_market_regime.py
- tests/stock_selection/test_grades.py
- tests/stock_selection/test_rr.py
- tests/stock_selection/test_liquidity.py
- tests/stock_selection/test_instrument_classification.py
- tests/stock_selection/test_integrated_classification.py
- tests/stock_selection/test_date_consistency.py
- tests/stock_selection/test_moneydj_point_in_time.py
- tests/stock_selection/test_stock_daily_sync.py
- V2_MILESTONE_1_REPORT.md

---

## 11. Tests 與結果

執行：

    python3 -m pytest tests/stock_selection -q
    python3 -m pytest -q
    python3 -m py_compile main.py tomorrow_strategy.py integrated_strategy.py screener.py moneydj_fetcher.py stock_selection_schema.py
    node --check static/app_pro.js

已驗證：

- 選股專屬測試：35 passed（含官方全市場日 K 同步 regression）。
- 全專案測試：66 passed。
- Python compile passed。
- JavaScript syntax passed。
- git diff --check passed。
- 只有 8 個 FastAPI on_event 既有 deprecation warnings；沒有 test failure。

---

## 12. 尚未解決問題

1. 個股日 K 已透過 TWSE／TPEx 官方每日全市場行情補至 2026-08-10；同步器可逐日回補，但仍仰賴兩個官方端點可用性，任一市場缺資料時會 fail closed。
2. security_master metadata 尚不完整：現有 936 rows 全部缺正式 market/listing/delisting；目前 source 是 stock_names_migration，只足以修正分類流程與接受未來正式來源。
3. 舊法人 raw shares 無法精確還原：歷史 rows 新欄位為 NULL；只有下次重新同步後才保存官方原始股數。
4. volume 單位仍依舊資料假設為張：DB 沒有 source metadata；本輪沒有假裝已證明單位。
5. adjusted price / corporate actions 未解：仍無還原權息 metadata；正式多年回測前必須處理。
6. MoneyDJ 關鍵字評分與 coverage bias 未改：屬 Phase 12 / Milestone 2；本輪只修正 point-in-time 日期。
7. 產業 V1 仍以截斷候選計算：屬 Phase 11 / Milestone 2，本輪未提前修改。
8. 尚未建立 backtester：Phase 15～18 / Milestone 3；skip_trade helper 已備妥，但沒有偽造歷史績效。
9. 突破後 target 尚未定義：依計畫標示 breakout_no_defined_target 並禁止可買，不自行發明目標模型。
10. 正式 UI Ranking/Risk 分欄尚未做：屬 Phase 19；本輪只完成 invalid-state UI。

---

## 13. Milestone 邊界確認

本輪沒有實作或啟用：

- ATR14 / ATR gate
- market breadth
- Relative Strength
- industry V2
- MoneyDJ 結構化新分數
- final_score_v2_shadow
- backtester
- A0～A6 experiments
- 新 Gate/Score/Risk 權重

因此本文件完成後停止，等待使用者確認，再決定是否開始 Milestone 2。
