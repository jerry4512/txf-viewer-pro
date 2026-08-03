# current_strategy_logic_audit.md
# TXF Pro Viewer — 策略選股邏輯盤點
> 產出日期：2026-05-18  
> 本文件僅記錄現有邏輯，不修改任何程式碼。

---

## 1. 相關檔案與函式

| 檔案 | 函式 / class | 用途 | 影響篩選結果 |
|---|---|---|---|
| `screener.py` | `run_screener_query()` | 六步驟選股主流程 | **是** |
| `screener.py` | `get_inst_5d_candidates()` | Step 1 法人條件，回傳候選代碼清單 | **是** |
| `screener.py` | `fetch_twse_daily_quotes()` | Step 3 從 TWSE/TPEx 取得今日行情 | **是** |
| `screener.py` | `compute_macd()` | 計算 MACD histogram (12,26,9) | **是** |
| `screener.py` | `compute_industry_rankings()` | 依選股結果計算產業分數與排行 | 否（排序輔助） |
| `screener.py` | `trace_stock_filters()` | 單股 Debug 追蹤 | 否 |
| `screener.py` | `sync_twse_institutional_data()` | 同步法人資料至 SQLite | 資料來源 |
| `screener.py` | `sync_stock_kbars()` | 同步日 K 線至 SQLite | 資料來源 |
| `main.py` | `_scheduled_sync_and_alert()` | 每日 18:00 自動篩選 + Telegram | **是**（觸發） |
| `main.py` | `_build_tg_message()` | 組成 Telegram 推播文字 | 否（輸出） |
| `static/app_pro.js` | `traceStockFilter()` | 前端 Debug 面板，呼叫 `/api/screener/trace` | 否 |
| `static/app_pro.js` | 篩選結果渲染程式 | 顯示 scoreBreakdown、strategyState 等 | 否（顯示） |

---

## 2. 股票進入候選名單的完整流程

### Step 0：資料來源

| 資料 | 來源 |
|---|---|
| 法人買賣超 | TWSE T86 + TPEx，存入 `stock_cache.db > institutional_trading` |
| 日 K 線 | 現有本地資料，存於 `stock_cache.db > daily_kbars`；富邦期貨行情串接不更新個股日 K |
| 今日行情 | TWSE `STOCK_DAY_ALL` + TPEx OpenAPI（即時爬取） |
| 股票名稱 / 產業 | 既有券商合約匯入資料，存入 `stock_names` |

---

### Step 1+2：法人條件

- **條件**：`SUM(foreign_buy + investment_buy + dealer_buy) > 0` **且** `(SUM(foreign_buy) > 0 OR SUM(investment_buy) > 0)`
- **實際判斷式**（`get_inst_5d_candidates()`）：
  ```sql
  HAVING SUM(foreign_buy + investment_buy + dealer_buy) > 0
     AND (SUM(foreign_buy) > 0 OR SUM(investment_buy) > 0)
  ```
- **資料範圍**：`institutional_trading` 表中最新 5 個不同日期
- **不通過**：直接排除，不進入後續步驟

---

### Step 3：當日行情條件

- **條件**：若有今日行情，`change_pct >= max_decline_pct`（預設 −3.5%）
- **實際判斷式**：
  ```python
  if q and q['change_pct'] < max_decline_pct:
      continue  # 排除
  ```
- **注意**：若今日無行情資料（`q = None`），直接保留，不排除
- **不通過**：直接排除

---

### Step 4：K 線資料載入

- **條件**：DB 中有對應的 `daily_kbars` 記錄，且 `len(sub_df) >= 62`
- **實際判斷式**：
  ```python
  if len(sub_df) < 62:
      continue  # 排除
  if pd.isna(latest['ma20']) or pd.isna(latest['ma60']):
      continue  # 排除
  ```
- **今日補注入**：若 TWSE 今日資料確認為今天（`twse_is_today`），且 DB 最新 < 今日，則補一筆今日行情到 `sub_df`
- **不通過**：直接排除

---

### Step 5a：流動性

- **條件**：`amountMa5 >= 50M OR amountMa20 >= 50M`（OR 關係）
- **不通過**：直接排除
- **缺失資料**：`amountMa5 = None AND amountMa20 = None` → **保留**（不排除）

---

### Step 5b：多頭排列

- **條件**：`close > ma20 > ma60`
- **不通過**：直接排除

---

### Step 5c：漲幅強度

- **條件**：`return20 > 1.5%` **且** `return60 > 4.0%`（AND 關係，兩者必須同時成立）
- **不通過**：直接排除

---

### Step 5d：分數計算

- 通過所有硬篩後，進行多項技術 + 籌碼加減分
- 分數 `clamp(0, 100)` 後，再加主力特徵加分（非過熱時）

---

### Step 6：策略狀態分類與排序

- 按過熱 → 等回測 → 明日優先 → 突破觀察 → 等回測(分數) → 暫不交易 分類
- 排序：策略狀態優先序 → 分數 → 法人佔比 → 乖離率

---

## 3. 法人條件

### 3.1 各指標計算方式

| 指標 | 計算方式 |
|---|---|
| `foreignBuy5` | `institutional_trading` 表最新 5 筆 `foreign_buy` 之總和（張） |
| `investmentTrustBuy5` | 最新 5 筆 `investment_buy` 之總和（張） |
| `dealerBuy5` | 最新 5 筆 `dealer_buy` 之總和（張） |
| `totalInstitutionBuy5` | `foreignBuy5 + investmentTrustBuy5 + dealerBuy5` |

> **單位換算**：同步時已 `// 1000`（股 → 張）。

### 3.2 入選條件（Step 1）

```
totalInstitutionBuy5 > 0  AND  (foreignBuy5 > 0 OR investmentTrustBuy5 > 0)
```

- 不需要三者都 > 0
- 僅 dealerBuy5 > 0 而外資與投信均 ≤ 0 → **排除**
- 投信小賣（investmentTrustBuy5 ≤ 0）但外資正買 → **保留**（反之亦然）
- 不通過 → **直接排除**（不僅扣分）

### 3.3 主力特徵標籤（`institution_label`，基於 5 日合計）

| 標籤 | 判斷條件 |
|---|---|
| `三人同買` | `foreignBuy5 > 0 AND investmentTrustBuy5 > 0 AND totalInstitutionBuy5 > 0` |
| `外資主導` | `foreignBuy5 > 0 AND investmentTrustBuy5 <= 0 AND totalInstitutionBuy5 > 0` |
| `投信主導` | `investmentTrustBuy5 > 0 AND foreignBuy5 <= 0 AND totalInstitutionBuy5 > 0` |
| `--` | 其他（不符合以上） |

### 3.4 籌碼階級（`tier_name`，基於連續買超天數）

`investment_strike`：從最新一日往回算，`investment_buy > 0` 連續天數（一旦中斷即停）  
`foreign_strike`：同上，`foreign_buy > 0` 連續天數

| tier_name | 條件 |
|---|---|
| `黃金滿貫` | `investment_strike > 0 AND foreign_strike > 0 AND sync_buy`（最新一日三者同步） |
| `強勢雙雄` | `investment_strike > 0 AND foreign_strike > 0`（不需最新一日同步） |
| `投信鎖碼` | `investment_strike > 0` |
| `外資鎖碼` | `foreign_strike > 0` |
| `主力佈局` | 以上皆不符 |

---

## 4. 流動性 / 成交金額條件

### 4.1 計算方式

| 指標 | 計算方式 |
|---|---|
| `volume` 單位 | **張（lot）**，舊日 K 匯入資料即為張，`_VOLUME_UNIT = "lot"` |
| `amountToday` | `close * volume * 1000`（每張 = 1000 股） |
| `amount_ma5` | `amount` 的 5 日移動平均 |
| `amount_ma20` | `amount` 的 20 日移動平均 |
| 金額單位 | **元（TWD）**，`_AMOUNT_UNIT = "calculatedFromVolume"` |

### 4.2 門檻

```python
_LIQ_THRESHOLD = 50_000_000  # 5,000 萬元（= 50000000）
```

> **確認**：門檻是 50,000,000（五千萬），**不是** 5,000,000,000。

### 4.3 通過條件

```python
_liq_passed = (
    _liq_data_missing                              # 資料缺失時保留
    or (_ama5  is not None and _ama5  >= 50_000_000)
    or (_ama20 is not None and _ama20 >= 50_000_000)
)
```

- 通過關係：**OR**（任一達標即通過）
- 兩者均缺失（None）：**保留**（不排除）
- 不通過：**直接排除**

### 4.4 缺失值處理

```python
_ama5  = _safe_float(latest.get('amount_ma5',  None))   # None if isnan
_ama20 = _safe_float(latest.get('amount_ma20', None))   # None if isnan
_liq_data_missing = (_ama5 is None) and (_ama20 is None)
```

- NaN → None，兩者皆 None → 視為「資料缺失」，**不排除**，分數給 0

---

## 5. 技術條件

### 5.1 硬篩條件（不通過 = 直接排除）

| 條件名稱 | 判斷式 | 不通過後影響 |
|---|---|---|
| K 線足夠 | `len(sub_df) >= 62` | 直接排除 |
| MA 可計算 | `not isna(ma20) AND not isna(ma60)` | 直接排除 |
| 多頭排列 | `close > ma20 > ma60` | 直接排除 |
| 20 日強度 | `return20 > 1.5%` | 直接排除（需兩者同時成立） |
| 60 日強度 | `return60 > 4.0%` | 直接排除（需兩者同時成立） |
| 流動性 | `amountMa5 >= 5000萬 OR amountMa20 >= 5000萬` | 直接排除 |

### 5.2 軟指標（影響分數 / 策略狀態）

| 條件名稱 | 判斷式 | 通過後影響 | 不通過後影響 |
|---|---|---|---|
| 乖離20MA | `bias20 = (close - ma20) / ma20 * 100` | 依區間加/扣分 | 依區間加/扣分 |
| 20 日相對強度 | `return20 - 1.5% > 1.5%` (即 rs20 > 1.5%) | +10 | 0 |
| 60 日相對強度 | `return60 - 4.0% > 4.0%` (即 rs60 > 4.0%) | +10 | 0 |
| MACD 負柱收斂 | `macd < 0 AND macd > prev1 > prev2` | +10 | 0 |
| K 線轉強 | `close > open OR close > ma5 OR close > ma10 OR close > prev.high` | +10 | 0 |
| 今日漲幅過大 | `todayChangePercent > 6%` | −10 | 0 |
| 近5日漲幅過大 | `return5 > 15%` | −10 | 0 |
| 長上影線 | `upperShadowRatio > 40%` | −10 | 0 |
| 法買股不漲 | `totalBuy5 > 0 AND todayChange <= 0` | −10 | 0 |

### 5.3 各指標計算

```python
return5  = (close - close[-6])  / close[-6]  * 100   # 近5日漲幅
return20 = (close - close[-21]) / close[-21] * 100   # 近20日漲幅
return60 = (close - close[-61]) / close[-61] * 100   # 近60日漲幅
bias20   = (close - ma20) / ma20 * 100
upperShadowRatio = (high - max(open, close)) / (high - low) * 100
```

---

## 6. 分數計算邏輯

### 6.1 加分項目

| 項目 | 條件 | 分數 | 備註 |
|---|---:|---:|---|
| 趨勢多頭 | `close > ma20 > ma60` | +20 | 所有股票通過此才進入（硬篩後必得） |
| 20 日相對強度 | `rs20 > 1.5%`（rs20 = return20 − 1.5%） | +10 | |
| 60 日相對強度 | `rs60 > 4.0%`（rs60 = return60 − 4.0%） | +10 | |
| 投信近5日買超 | `investmentTrustBuy5 > 0` | +10 | 法人組合 |
| 外資近5日買超 | `foreignBuy5 > 0` | +8 | 法人組合 |
| 三大法人買超 | `totalInstitutionBuy5 > 0` | +7 | 法人組合 |
| 外資投信同步 | `foreignBuy5 > 0 AND investmentTrustBuy5 > 0` | +5 | 法人組合 |
| **法人分數上限** | 以上法人4項合計 | **max 25** | 超過截斷 |
| 乖離最佳位置 | `0% ≤ bias20 ≤ 3%` | +10 | |
| 乖離安全位置 | `3% < bias20 ≤ 6%` | +8 | |
| 乖離偏高 | `6% < bias20 ≤ 10%` | +4 | |
| MACD 負柱收斂 | 見上方判斷式 | +10 | |
| K 線轉強 | 見上方判斷式 | +10 | |
| 流動性充足 | `amountMa5 >= 3億` | +10 | |
| 流動性良好 | `amountMa5 >= 1億` | +8 | |
| 流動性普通 | `amountMa5 >= 5千萬` | +5 | |
| 流動性普通(20日) | `amountMa5 < 5千萬 AND amountMa20 >= 5千萬` | +5 | |
| 停損距離低風險 | `abs_sl ≤ 3%` | +10 | |
| 停損距離可接受 | `3% < abs_sl ≤ 5%` | +7 | |
| 停損距離偏高 | `5% < abs_sl ≤ 6%` | +3 | |

### 6.2 扣分項目

| 項目 | 條件 | 扣分 | 備註 |
|---|---|---:|---|
| 乖離過高 | `10% < bias20 ≤ 15%` | −15 | |
| 乖離嚴重過熱 | `bias20 > 15%` | −25 | 同時觸發策略狀態「等回測」or「過熱」 |
| 近5日漲幅過大 | `return5 > 15%` | −10 | |
| 今日急漲 | `todayChangePercent > 6%` | −10 | |
| 長上影線 | `upperShadowRatio > 40%` | −10 | |
| 法人買超股價不漲 | `totalBuy5 > 0 AND todayChange <= 0` | −10 | |
| 停損距離過遠 | `6% < abs_sl ≤ 8%` | −10 | |
| 停損距離極遠 | `abs_sl > 8%` | −20 | 同時觸發「過熱警戒」 |

### 6.3 總分 Clamp

```python
score = max(0, min(100, score))
```

分數先 clamp 至 0~100，**然後**才加主力特徵加分（非過熱時）：

```python
if not is_overheat:
    score = min(100, score + major_bonus)
```

### 6.4 重複加分檢查

**法人分數內部**：`投信+10、外資+8、三大合計+7、外資投信同步+5` 有重疊加成
（例如：三大合計 > 0 且外資 > 0 且投信 > 0 → 可得 10+8+7+5=30，截斷至 25）

**主力特徵加分可能重複計算**：

```python
# 1. institution_label == "三人同買" → major_bonus_raw += 3
# 2. sync_buy → major_features.append("三人同買") → major_bonus_raw += 3
```

若 `institution_label = "三人同買"` 且 `sync_buy = True`（最新一日三者同買），
兩個條件都觸發，但程式碼中 sync_buy 判斷在 `elif sync_buy:` 分支，
**只有當 `institution_label` 不在三種標籤時才觸發**，故不會重複。

```python
if institution_label in ("三人同買", "外資主導", "投信主導"):
    major_features.append(institution_label)
    ...
elif sync_buy:                   # ← institution_label 不符合上方才進入
    major_features.append("三人同買")
    major_bonus_raw += 3
```

**但 `黃金滿貫` 與 `三人同買` 仍可能重疊**：

```python
# institution_label = "三人同買" → +3
# tier_name = "黃金滿貫" → +3（額外加）
```

`institution_label` 是基於 5 日合計，`tier_name` 是基於連續天數 + 最新一日同步，
兩者條件不同，但都可能同時成立，導致 major_bonus_raw 得 6 分（截斷至 8 上限仍生效）。

---

## 7. 主力特徵分數

| 特徵 | 觸發條件 | 原始加分 |
|---|---|---:|
| 三人同買（5日） | `institution_label == "三人同買"` | +3 |
| 外資主導 | `institution_label == "外資主導"` | +1 |
| 投信主導 | `institution_label == "投信主導"` | +1 |
| 三人同買（今日） | `sync_buy AND institution_label 不符以上` | +3 |
| 黃金滿貫 | `tier_name == "黃金滿貫"` | +3 |
| 強勢雙雄 | `tier_name == "強勢雙雄"` | +2 |
| 投信連買 ≥ 5 日 | `investment_strike >= 5` | +3 |
| 投信連買 ≥ 3 日 | `3 ≤ investment_strike < 5` | +2 |
| 外資連買 ≥ 5 日 | `foreign_strike >= 5` | +3 |
| 外資連買 ≥ 3 日 | `3 ≤ foreign_strike < 5` | +2 |

```python
major_bonus = min(8, major_bonus_raw)   # 上限 +8
```

- **封頂**：主力特徵加分最高 +8
- **非過熱**才加到總分，過熱時僅顯示，不影響分數與策略狀態
- **重疊說明**：`三人同買(5日) + 黃金滿貫` 可能同時成立，合計 raw +6，截斷至 8

---

## 8. 買點型態判斷

判斷順序由上而下，**先符合先採用**：

### 8.1 過熱不交易

```python
if bias20 > 15.0 or return5 > 15.0 or candleUpperShadowRatio > 40.0:
    entry_pattern = "過熱不交易"
    stopLossPrice = 0.0
```

- 使用價格：無
- 影響分數：否（策略狀態計算在之後，以 `is_overheat` 覆蓋）
- 影響策略狀態：在策略狀態判斷時 `is_overheat` 涵蓋更多條件再次覆蓋

### 8.2 等回測（乖離過大）

```python
elif bias20 > 10.0:
    entry_pattern = "乖離過大等回測"
    stopLossPrice = latest['ma20']
```

### 8.3 回測20MA

```python
elif is_near_ma20 and (close > open or close > ma5 or close > ma10):
    entry_pattern = "回測 20MA 轉強"
    stopLossPrice = min(ma20, sub_df.iloc[-5:]['low'].min())
```

`is_near_ma20`：`abs(close - ma20) / ma20 < 3%` 且 `low >= ma20 * 0.98`

### 8.4 回測前高

```python
elif is_near_prior_high and close >= recentHigh20 * 0.97:
    entry_pattern = "回測前高不破"
    stopLossPrice = recentHigh20 * 0.97
```

`is_near_prior_high`：`abs(close - recentHigh20) / recentHigh20 < 3%`

### 8.5 突破整理

```python
elif is_breakout and volume > volumeMa20 * 1.2:
    entry_pattern = "突破整理區"
    stopLossPrice = latest['low']
```

`is_breakout`：`close > recentHigh10 OR close > recentHigh20`

### 8.6 高檔續強（預設）

```python
elif is_high_consolidation and close >= recentHigh20 * 0.98:
    entry_pattern = "創高後高檔整理"
    stopLossPrice = sub_df.iloc[-10:]['low'].min()
```

`is_high_consolidation`：`close >= recentHigh20 * 0.97` 且 `(recentHigh10 - recentLow10) / recentLow10 < 8%`

---

## 9. 停損價與停損距離

### 9.1 各型態停損

| 型態 | 停損價公式 |
|---|---|
| 高檔續強 | `min(近10日低點)` |
| 回測20MA | `min(ma20, 近5日低點)` |
| 回測前高 | `recentHigh20 * 0.97` |
| 突破整理 | `latest['low']`（突破K當日低點） |
| 等回測 / 預設 | `ma20` |
| 過熱 | `0.0`（無停損） |

### 9.2 停損距離計算

```python
stopLossPercent = ((stopLossPrice - close) / close) * 100   # 負值，例如 -4.5%
abs_sl = abs(stopLossPercent)
```

- 計算使用真實差值（stopLossPrice < close，故結果為負）
- 畫面顯示為負數；abs_sl 用於加減分判斷

### 9.3 停損距離對分數 / 策略狀態的影響

| abs_sl 範圍 | 分數影響 | 策略狀態影響 |
|---|---|---|
| ≤ 3% | +10 | 無 |
| 3~5% | +7 | 無 |
| 5~6% | +3 | 無 |
| 6~8% | −10 | `is_pullback = True`（→ 等回測） |
| > 8% | −20 | `is_overheat = True`（→ 過熱警戒） |

---

## 10. 策略狀態分類

判斷順序（優先）：

### 10.1 過熱警戒（最高優先）

```python
is_overheat = (bias20 > 15.0) or (return5 > 20.0) or (todayChangePercent > 7.0) \
              or (candleUpperShadowRatio > 40.0) or (abs_sl > 8.0)
```

→ `strategy_state = "過熱警戒"`，強制 `stopLossPrice = 0.0`，主力特徵不加分

### 10.2 等回測

```python
is_pullback = (bias20 > 10.0) or (abs_sl > 6.0 and not is_overheat)
```

→ `strategy_state = "等回測"`

### 10.3 明日優先

```python
score >= 80 and bias20 <= 6.0 and abs_sl <= 6.0 \
and close > ma20 > ma60
```

→ `strategy_state = "明日優先"`

- **最低分數需求**：80 分（主力特徵加分後）
- **乖離條件**：bias20 ≤ 6%（強制）
- **停損條件**：abs_sl ≤ 6%（強制）
- **趨勢條件**：close > ma20 > ma60（強制）

### 10.4 突破觀察

```python
score >= 65 or is_breakout_cond
```

`is_breakout_cond = is_breakout AND volume > volumeMa20 * 1.2 AND todayChange ≤ 7% AND bias20 < 12%`

### 10.5 等回測（分數不足）

```python
score >= 50
```

→ `strategy_state = "等回測"`（與 is_pullback 同狀態）

### 10.6 暫不交易

→ 分數 < 50 且不符合以上條件

### 10.7 風險條件是否可被高分覆蓋

| 問題 | 答案 |
|---|---|
| `bias20 > 10%` 是否可進入明日優先 | **否**，`is_pullback` 先攔截 |
| `abs_sl > 8%` 是否可進入明日優先 | **否**，`is_overheat` 先攔截 |
| `bias20 > 6%` 是否可進入明日優先 | **否**，明日優先有 `bias20 <= 6.0` 條件 |
| 過熱警戒是否一定優先於明日優先 | **是** |

---

## 11. 產業分數 / 產業共振

### 11.1 產業分數計算

```python
industry_score = (
    avg_score    * 0.40
  + s20          * 0.25   # s20 = clamp(50 + avg_r20 * 3, 0, 100)
  + s60          * 0.15   # s60 = clamp(50 + avg_r60 * 3, 0, 100)
  + inst_score   * 0.15   # inst_score = min(100, avg_instRatio * 5)
  + breakout_s   * 0.05   # breakout_ratio * 100
  - overheat_pen * 0.10   # overheat_ratio * 100
)
```

clamped: `0 ≤ industry_score ≤ 100`

### 11.2 產業共振

```python
s['hasIndustryResonance'] = (s['score'] >= 75 and industry_score >= 80)
```

### 11.3 產業分數是否加到股票分數

**否**。`compute_industry_rankings()` 是獨立函式，在 `run_screener_query()` 完成後才另行計算；個股 `score` 不包含產業分。

### 11.4 股票所屬產業

優先來源：`stock_names.category`（由舊版 `sync_stock_kbars` 從券商 `contract.category` 取得），
再轉換 `_resolve_industry()` 為中文。ETF 代碼產業 → `'ETF/其他'`。

---

## 12. 排序邏輯

```python
state_priority_map = {"明日優先": 1, "突破觀察": 2, "等回測": 3, "過熱警戒": 4, "暫不交易": 5}

results.sort(key=lambda x: (
    state_priority_map.get(x["strategyState"], 99),  # 1. 策略狀態優先序
    -x["score"],                                      # 2. 分數由高到低
    -x["institutionBuyRatio5"],                       # 3. 法人佔比由高到低
    abs(x["bias20"])                                  # 4. 乖離絕對值由小到大
))
```

- **不依成交金額排序**
- **不依停損距離排序**
- **不依產業分數排序**（主清單排序時）

---

## 13. Telegram 推播邏輯

### 13.1 推播哪些股票

- **每日排程（18:00）**：只推 `strategyState == "明日優先"` 的股票
- **手動 `/api/telegram/send`**：由前端傳入股票清單，通常也只傳明日優先

### 13.2 推播內容

`_build_tg_message()` 每檔股票包含：

| 欄位 | 顯示方式 |
|---|---|
| 代碼 + 名稱 | `#CODE 名稱` |
| 分數 | `分數 N` |
| 產業名稱 | 來自 `compute_industry_rankings` |
| 產業分數 | 來自 `ind_rankings` |
| 產業共振 | `🔥 產業共振` tag |
| 收盤價 | 直接顯示 |
| 乖離20MA | `乖離 +X.X%` |
| 20日強度 | `20日強度 +X.X%` |
| 法人佔比 | `法人佔比 X.XX%` |
| 停損價 | `停損價 XXX.XX (-X.X%)` |
| 主力特徵 | `#三人同買 #黃金滿貫` 等 |
| actionPlan | 保守進場 / 積極進場 / 不進場條件 / 停損條件（含具體價格） |

### 13.3 不顯示或嵌入文案的資訊

| 項目 | 狀態 |
|---|---|
| 5MA | 嵌入 actionPlan 文字，**不單獨顯示** |
| 10MA | 嵌入 actionPlan 文字 |
| 20MA | 嵌入 actionPlan 文字 |
| 前一交易日高點 | 嵌入 actionPlan 文字 |
| 停損價 | **直接顯示**在 block 頭部，也在 actionPlan |
| 停損距離 | **直接顯示**（百分比） |
| 產業分數 | **直接顯示** |
| 產業共振 | **直接顯示** tag |
| 法人佔比 | **直接顯示** |
| 主力特徵 | **直接顯示** |

### 13.4 不限制推播檔數

程式無上限限制，全部明日優先都推播，自動分段（每段 ≤ 4000 字元）。

---

## 14. 單股 Debug 追蹤

### 14.1 `trace_stock_filters(code)` 回傳資訊

| 欄位 | 內容 |
|---|---|
| `inUniverse` | 是否在 `DEFAULT_STOCKS` 名單 |
| `step1.passed` | 是否通過 Step 1 法人篩選 |
| `step1.foreignBuy5` | 外資5日合計張數 |
| `step1.investmentBuy5` | 投信5日合計張數 |
| `step1.dealerBuy5` | 自營商5日合計張數 |
| `step1.totalBuy5` | 三大合計張數 |
| `step1.instScore` | 法人分數（上限25） |
| `step1.instLabel` | 主力標籤（三人同買/外資主導/投信主導/--） |
| `step1.instDays` | 每日明細（最新5日） |
| `step1.reason` | 通過/未通過原因文字 |
| `hasKbars` | DB 中是否有 daily_kbars |
| `kbarCount` | K 線根數 |
| `latestKbarDate` | 最新 K 線日期 |
| `messages` | 錯誤/警告訊息清單 |

### 14.2 Debug 無法追蹤的資訊

`trace_stock_filters` **不包含**：

- Step 3 行情條件（跌幅過濾）
- 流動性條件細節
- 技術條件（多頭排列、漲幅強度）
- 分數明細（scoreBreakdown）
- 買點型態
- 策略狀態

以上資訊**只有在股票通過所有步驟後**才出現在 `run_screener_query()` 的輸出結果中。

如需完整 Debug，可在 `screener.py` 中設定 `TRACE_CODE = "股票代號"`，透過 server console log 追蹤。

---

## 15. 目前邏輯可能的風險點

### 15.1 條件過嚴導致候選股太少

- **風險**：Step 1 要求三大合計 > 0 **且** 外資或投信至少一方 > 0，dealer only 的股票全部排除
- **位置**：`screener.py:264-265`
- **結果**：自營商大買但外資投信均小賣的股票不入選，即使技術面極佳
- **建議**：可考慮調整為「任一法人 > 0」或加入「特殊自營商大買」門檻

---

### 15.2 外資或投信小賣不會直接排除強勢股

- **風險**：若 `foreignBuy5 = 100` 且 `investmentTrustBuy5 = -1`，仍通過 Step 1（外資 > 0）
- **結果**：這符合設計意圖，不算問題，但分數損失 10 分（投信近5日買超不得分）

---

### 15.3 流動性缺失資料不排除

- **風險**：`amountMa5 = None AND amountMa20 = None` 時直接通過，分數給 0
- **位置**：`screener.py:724-730`
- **結果**：低流動性股票可能因資料缺失通過流動性篩選
- **建議**：應考慮是否在資料缺失時加排除條件

---

### 15.4 主力特徵可能重複部分加分（三人同買 + 黃金滿貫）

- **風險**：`institution_label = "三人同買"` +3 分，`tier_name = "黃金滿貫"` +3 分，兩者可同時觸發
- **位置**：`screener.py:1070-1084`
- **結果**：raw 可達 +6，但有 max 8 上限，截斷有效
- **建議**：說明此重疊是刻意設計（雙重訊號加強），還是應互斥

---

### 15.5 過熱警戒條件與買點型態判斷不一致

- **風險**：買點型態用 `bias20 > 15 or return5 > 15 or upperShadow > 40` 判斷「過熱」，策略狀態用 `bias20 > 15 or return5 > 20 or todayChange > 7 or upperShadow > 40 or abs_sl > 8`。兩者門檻不同（return5：15% vs 20%）
- **位置**：買點型態 `screener.py:871`，策略狀態 `screener.py:1118-1119`
- **結果**：`return5 = 16%` 時，買點型態為「過熱不交易」，但策略狀態會繼續往下判斷（非過熱警戒），entry_pattern 最後被策略狀態覆蓋為正確值
- **建議**：確認此行為是否符合預期（兩個門檻不同）

---

### 15.6 return20/return60 需雙雙達標（AND 關係）

- **風險**：`return20 <= 1.5% OR return60 <= 4.0%` 直接排除。某些短期剛起漲但60日還未強的股票會被排除
- **位置**：`screener.py:771-776`
- **結果**：近期才起漲（20日強但60日弱）的股票無法入選
- **建議**：視策略目標決定，若偏好追強則可降低60日門檻

---

### 15.7 ETF 與個股使用同一套規則

- **風險**：`DEFAULT_STOCKS` 中無 ETF（`_INDUSTRY_MAP "00" = "ETF"`），但從 TWSE T86 爬下來的資料包含 ETF，會進入 `institutional_trading`
- **位置**：`screener.py:912`（`if len(code) > 6: continue`，排除 6 位以上代碼）
- **結果**：ETF（4 位代碼）若有法人買超可進入篩選，但 `DEFAULT_STOCKS` 不含 ETF，Step 1 的候選名單不含 ETF
- **說明**：`get_inst_5d_candidates()` 的股票代碼來自 `institutional_trading`，理論上可包含 ETF，若後續同步 K 線也有 ETF，則可進入選股，需確認是否為預期行為

---

### 15.8 高價股成交金額計算正確

- **說明**：`amount = close * volume * 1000`，volume 為「張」，1 張 = 1000 股
- **結論**：高價股（如 3008 大立光 2000+ 元）即使每日成交量僅幾百張，amount 仍可達數億，**不會因張數少被誤判低流動性**，公式正確

---

### 15.9 法人佔比失真問題

- **風險**：`inst_ratio_5d = totalInstitutionBuy5 / total_vol_lots * 100`，若總成交量極小，法人買超佔比可能異常偏高
- **位置**：`screener.py:844-846`
- **結果**：法人排序可能被小量股票佔據前段，但因有流動性門檻（5千萬）多半被過濾
- **建議**：確認流動性篩選與法人佔比的互動是否合理

---

### 15.10 `trace_stock_filters` Debug 不完整

- **風險**：Debug 面板只能看到 Step 1 + K 線基本資訊，無法追蹤 Step 3（行情）、流動性、技術條件、分數明細
- **位置**：`screener.py:1454-1577`
- **結果**：難以追蹤非明日優先股票為何被排除
- **建議**：擴充 `trace_stock_filters()` 以包含完整步驟

---

## 附錄：重要常數一覽

| 常數 | 值 | 說明 |
|---|---|---|
| `max_decline_pct` | -3.5% | Step 3 今日跌幅上限 |
| `index_gain_20` | 1.5% | 大盤20日基準漲幅 |
| `index_gain_60` | 4.0% | 大盤60日基準漲幅 |
| `_LIQ_THRESHOLD` | 50,000,000 | 流動性門檻（5千萬元） |
| `_INST_CAP` | 25 | 法人分數上限 |
| `major_bonus max` | 8 | 主力特徵加分上限 |
| 最小 K 線根數 | 62 | 能計算60MA的最低需求 |
| K 線同步天數 | 115 天 | `end_date - 115 days` |
| 舊匯入器 API batch 大小 | 30 天 | 僅供既有個股日 K 匯入器相容 |
| 排程時間 | 平日 18:00 | APScheduler CronTrigger |
