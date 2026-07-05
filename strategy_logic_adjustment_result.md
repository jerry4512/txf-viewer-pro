# strategy_logic_adjustment_result.md
# 策略選股邏輯調整結果報告
> 修改日期：2026-05-18  
> 修改檔案：`screener.py`（唯一修改檔案）

---

## 一、修改項目對照表

| 項目 | 修改前 | 修改後 |
|---|---|---|
| return60 硬篩 | `return60 <= 4.0%` 直接排除 | 移除硬篩，改為加分條件 |
| 流動性缺失 | `_liq_data_missing = True` → `_liq_passed = True`（直接通過） | `_liq_data_missing = True` → `_liq_passed = False`（直接排除） |
| 明日優先條件 | `score >= 80 AND bias20 <= 6% AND abs_sl <= 6% AND 多頭排列` | 加入 `_liq_passed = True` 保護 |
| 排序邏輯 | 策略→分數→法人佔比→乖離率 | 策略→分數→產業共振→成交金額→停損距離→法人佔比→乖離率 |
| 產業共振計算 | 只在 `compute_industry_rankings()` 外部呼叫時計算 | 在 `run_screener_query()` 排序前先呼叫，確保排序可用 `hasIndustryResonance` |
| 主力特徵封頂 | 已有 `min(8, major_bonus_raw)` | 確認保留，無需修改 |
| 單股 Debug | 只回傳 Step1 法人 + K 線根數 | 全步驟完整追蹤（20 個欄位） |

---

## 二、各項修改詳細說明

### 2.1 return60 從硬篩改為加分

**修改前（`screener.py` ~line 770）：**
```python
if return20 <= index_gain_20 or return60 <= index_gain_60:
    continue  # 直接排除
```

**修改後：**
```python
# Step 5b：20日強度硬篩（保留）
if return20 <= index_gain_20:
    continue  # 只排除 return20 不足的股票

# Step 5c：60日強度改為加分條件（不再硬篩）
# TRACE log 顯示 return60 是加分還是不加分

# 分數計算段：
score += _item("60日相對強度", return60 > index_gain_60, 10,
               f"近60日漲幅 {return60:+.1f}%（> {index_gain_60}% 加分，否則不扣分）")
```

**效果：**
- 近20日已轉強但60日還不足的股票不再被排除
- 60日強勢的股票仍獲得 +10 分優勢
- 候選股數量增加

---

### 2.2 流動性資料缺失改為排除

**修改前：**
```python
_liq_passed = (
    _liq_data_missing          # ← 缺失直接視為通過
    or (_ama5 is not None and _ama5 >= 50_000_000)
    or (_ama20 is not None and _ama20 >= 50_000_000)
)
```

**修改後：**
```python
if _liq_data_missing:
    _liq_passed = False
    liquidity_reason = "成交金額資料缺失：amountMa5 / amountMa20 均無效"
else:
    _liq_passed = (
        (_ama5 is not None and _ama5 >= 50_000_000)
        or (_ama20 is not None and _ama20 >= 50_000_000)
    )
```

**效果：**
- 成交金額資料完全缺失的股票不再通過流動性篩選
- 在硬篩階段直接排除，不會進入後續分數計算

---

### 2.3 明日優先加入 `_liq_passed` 保護

**修改前：**
```python
elif score >= 80 and bias20 <= 6.0 and abs_sl <= 6.0 and \
     latest['close'] > latest['ma20'] > latest['ma60']:
    strategy_state = "明日優先"
```

**修改後：**
```python
elif score >= 80 and bias20 <= 6.0 and abs_sl <= 6.0 and _liq_passed and \
     latest['close'] > latest['ma20'] > latest['ma60']:
    strategy_state = "明日優先"
```

**注意：** 由於流動性硬篩已在前面排除 `_liq_passed = False` 的股票，此條件在目前架構下為防禦性條件，確保即使邏輯有調整也不會誤入明日優先。

---

### 2.4 排序邏輯調整

**修改前：**
```python
results.sort(key=lambda x: (
    state_priority_map.get(x["strategyState"], 99),
    -x["score"],
    -x["institutionBuyRatio5"],     # ← 法人佔比排第三
    abs(x["bias20"])
))
```

**修改後：**
```python
if results:
    compute_industry_rankings(results)  # 先計算產業共振

results.sort(key=lambda x: (
    state_priority_map.get(x["strategyState"], 99),           # 1. 策略狀態
    -x["score"],                                               # 2. 分數
    0 if x.get("hasIndustryResonance") else 1,                # 3. 產業共振優先
    -(x.get("amountMa5") or x.get("amountMa20") or 0),       # 4. 成交金額
    abs(x.get("stopLossPercent", 0)),                         # 5. 停損距離
    -x.get("institutionBuyRatio5", 0),                        # 6. 法人佔比（已降低優先序）
    abs(x.get("bias20", 0))                                   # 7. 乖離率
))
```

**效果：**
- 法人佔比高但成交量小的股票不再排在成交金額大的股票前面
- 停損距離較小的股票在同分時更優先
- 有產業共振的股票在同分時更優先

---

### 2.5 主力特徵分數封頂確認

```python
major_bonus = min(8, major_bonus_raw)   # 上限 +8，已存在
```

已確認封頂邏輯存在，無需修改。

---

### 2.6 單股 Debug 全面擴充

**修改前回傳欄位：**
- `inUniverse`、`step1`（法人）、`hasKbars`、`kbarCount`、`latestKbarDate`、`messages`

**修改後回傳欄位（完整 20 項）：**

| 欄位 | 內容 |
|---|---|
| `inUniverse` | 是否在 DEFAULT_STOCKS |
| `hasDailyQuote` | 是否有今日行情 |
| `closePrice` / `changePercent` | 今日收盤 / 漲跌幅 |
| `hasKbars` / `kbarCount` / `latestKbarDate` | K 線基本資訊 |
| `step1` | 法人條件完整明細（5日合計、每日明細、通過原因） |
| `step2Liquidity` | 流動性：ama5 / ama20 / 門檻 / 缺失狀態 / 原因 |
| `step3Technical` | 技術條件：close / ma20 / ma60 / 多頭排列是否通過 |
| `step4Strength` | return20（硬篩） / return60（加分狀態） |
| `scoreBreakdown` | 完整分數明細（每項條件 +/- 分） |
| `totalScore` | 最終分數 |
| `majorBonus` / `majorFeatures` | 主力特徵加分明細 |
| `bias20` / `ma5` / `ma10` / `ma20` / `ma60` | 均線與乖離 |
| `entryPattern` / `entryPatternLabel` | 買點型態 |
| `stopLossPrice` / `stopLossPercent` | 停損價與距離 |
| `strategyState` / `strategyStateLabel` | 策略狀態 |
| `finalIncluded` | 最終是否入選 |
| `excludedAtStep` / `excludedReason` | 若未入選，被排除的步驟與原因 |
| `highInstRatioWarning` | 法人佔比 > 30% 警示 |
| `institutionBuyRatio5` | 法人5日佔比 |

**Console Log 格式（符合計畫書要求）：**
```
[TRACE 6271] inUniverse=true
[TRACE 6271] hasDailyQuote=true close=183.5 changePct=0.00%
[TRACE 6271] hasKbars=true latestDate=2026-05-14 kbarsCount=150
[TRACE 6271] Step1 法人條件 passed=true
[TRACE 6271] foreignBuy5=2831 investmentTrustBuy5=-19 dealerBuy5=-41 totalInstitutionBuy5=2771
[TRACE 6271] Step2 流動性 passed=true
[TRACE 6271] amountMa5=955000000 amountMa20=812000000 threshold=50000000
[TRACE 6271] Step3 技術條件 passed=true
[TRACE 6271] Step4 相對強度
[TRACE 6271] return20=8.16 passed=true
[TRACE 6271] return60=3.50 scoreOnly=true reason=return60未達4.0%，不排除只是不加分
[TRACE 6271] Score total=xx
[TRACE 6271] EntryPattern=高檔續強
[TRACE 6271] StrategyStatus=明日優先
[TRACE 6271] finalIncluded=true
```

---

## 三、驗收結果

| 驗收項目 | 結果 |
|---|---|
| return60 <= 4.0% 不再直接排除 | ✅ |
| return60 > 4.0% 獲得 +10 加分 | ✅ |
| Debug 顯示 return60 是加分還是硬篩 | ✅（`scoreOnly=true` 標示） |
| amountMa5 / amountMa20 都缺失時不得列為明日優先 | ✅（`_liq_passed = False` → 硬篩排除） |
| `liquidityPassed = false` 時不得列為明日優先 | ✅（明日優先條件加入 `_liq_passed`） |
| 任一股票代號可完整追蹤篩選流程 | ✅（新增 Steps 2-4 + 分數明細） |
| 法人佔比不排在成交金額前面 | ✅（排序第 4 位改為成交金額，法人佔比降至第 6） |
| 停損距離較小的股票更優先 | ✅（排序第 5 位） |
| 產業共振股票更優先 | ✅（排序第 3 位） |
| 主力特徵分數封頂 +8 | ✅（已確認原有邏輯） |
| `bias20 > 6%` 不得列為明日優先 | ✅（既有條件保留） |
| `abs_sl > 6%` 不得列為明日優先 | ✅（既有條件保留） |
| 過熱警戒優先於明日優先 | ✅（既有判斷順序保留） |

---

## 四、尚未處理的問題

| 問題 | 說明 |
|---|---|
| `突破觀察` 未限制 `liquidityPassed` | 計畫書要求「不允許列為突破觀察」，但目前僅在硬篩排除缺失資料股票，邏輯上已無法達到突破觀察，故影響不大。若需明確限制可補上 `_liq_passed` 條件。 |
| 法人佔比 > 30% 警示目前只記錄 flag | `highInstRatioWarning` 已加入 trace 回傳結果，但前端 UI 尚未顯示警示訊息，需前端配合更新。 |
| `trace_stock_filters` 的產業分數 | trace 函式不計算產業分數（需全部股票才能計算），回傳中無 `industryScore`。如需顯示，需呼叫 `run_screener_query()` 再查詢。 |
| 王品 2727 等非電子股 | 現有策略無產業排除邏輯，所有股票均使用相同規則，已符合計畫書要求（按法人、流動性、技術條件判斷）。 |
| `TRACE_CODE` 仍硬碼在 `run_screener_query()` | 目前設為 `"6271"`，非動態傳入。若需更改追蹤對象，需手動修改 `screener.py` 第 579 行。 |
