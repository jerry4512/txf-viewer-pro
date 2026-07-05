# 策略選股後續補強實作結果

## 修改摘要

本次共修改 4 個檔案，針對 3 個補強項目：

| 項目 | 說明 | 狀態 |
|------|------|------|
| 1. TRACE_CODE 改為參數 | `run_screener_query(trace_code=...)` + API 支援 `traceCode` | ✅ 完成 |
| 2. 突破觀察加入 liquidityPassed | `elif (score >= 65 or is_breakout_cond) and _liq_passed` | ✅ 完成 |
| 3. 法人佔比警示顯示到前端 | 股票列表 ⚠️ icon + Drawer 警示區塊 + Debug 面板 | ✅ 完成 |

---

## 修改前邏輯

### 項目一：TRACE_CODE
```python
# screener.py — run_screener_query() 內
TRACE_CODE = "6271"  # 硬碼，需要手動改程式碼追蹤不同股票
```

```python
# main.py — /api/screener/run
results = screener.run_screener_query(max_decline_pct=max_decline)
# 不支援 traceCode
```

### 項目二：突破觀察
```python
elif score >= 65 or is_breakout_cond:
    strategy_state = "突破觀察"
    # 無明確 liquidityPassed 檢查
```

### 項目三：highInstRatioWarning
- 後端 `trace_stock_filters()` 已計算 `highInstRatioWarning`
- `run_screener_query()` 結果 dict 未包含 `highInstRatioWarning`
- 前端 Drawer 法人佔比欄位無警示顯示
- 前端 Debug 追蹤面板只顯示 Step 1

---

## 修改後邏輯

### 項目一：TRACE_CODE（screener.py + main.py）

**screener.py**
```python
def run_screener_query(
    max_decline_pct=-3.5,
    trace_code: str | None = None   # ← 新增
):
    TRACE_CODE = trace_code   # ← 不再硬碼
```

新增 TRACE log（inst_ratio 計算後）：
```
[TRACE 2727] institutionBuyRatio5=38.95
[TRACE 2727] highInstRatioWarning=true warning=法人佔比偏高，請確認成交金額與流動性
```

**main.py**
```python
trace_code = str(payload.get("traceCode", "")).strip() or None
results = screener.run_screener_query(
    max_decline_pct=max_decline,
    trace_code=trace_code      # ← 傳入
)
```

前端可呼叫：`POST /api/screener/run` with body `{ "traceCode": "6271" }`

### 項目二：突破觀察（screener.py）

```python
elif (score >= 65 or is_breakout_cond) and _liq_passed:   # ← 明確加入
    strategy_state = "突破觀察"
    strategy_state_label = "🔵 突破觀察"
```

流動性保護狀態確認：
- 明日優先：`_liq_passed` 已在 score >= 80 條件中明確要求 ✅
- 突破觀察：`and _liq_passed` 已明確加入 ✅
- 等回測 / 暫不交易：只出現在 _liq_passed=True 的股票（pre-filter already continues）
- 流動性未通過時：已在 Step 5a 前 `continue`，不會到達策略狀態判斷

### 項目三：highInstRatioWarning（screener.py + index.html + app_pro.js）

**screener.py** — `run_screener_query()` result dict 新增欄位：
```python
"highInstRatioWarning": bool(inst_ratio_5d > 30.0)
```

**static/index.html** — Drawer 法人籌碼區塊新增警示容器：
```html
<div id="drawer-inst-ratio-warning" style="display:none; margin-top:8px;"></div>
```

**static/app_pro.js** — 三處前端顯示：

1. **股票列表** — 法人佔比欄位加 ⚠️ icon + hover tooltip
2. **股票 Drawer** — `openStockDrawer()` 設定警示區塊：
   ```
   ⚠️ 法人佔比偏高，請確認成交金額與流動性
   法人佔比過高可能代表籌碼集中，也可能是成交金額較小導致比例被放大。
   ```
3. **個股追蹤 Debug** — `traceStockFilter()` 擴充為顯示全步驟：
   - Step 1 法人 / Step 2 流動性 / Step 3 技術 / Step 4 相對強度
   - 法人佔比 + `highInstRatioWarning` 警示
   - 決策結果（分數、策略狀態、最終納入）

---

## 測試案例

### 6271 同欣電
- `traceCode=6271` → 後端輸出 `[TRACE 6271]` 完整 TRACE ✅
- 法人佔比欄位顯示（預期正常值）
- return60 不足時不排除，只不加分（已是現有邏輯）

### 2727 王品
- `traceCode=2727` → 後端輸出 `[TRACE 2727]` 完整 TRACE ✅
- 若法人佔比 > 30%：前端列表顯示 ⚠️，Drawer 顯示警示區塊
- 警示不影響分數，不直接排除

### 2382 廣達
- `traceCode=2382` → 後端輸出 `[TRACE 2382]` 完整 TRACE ✅
- 成交金額應明顯通過流動性門檻
- 策略狀態依分數、買點型態、停損距離決定

---

## 驗收結果

| 驗收項目 | 結果 |
|----------|------|
| 不需改程式碼即可追蹤不同股票 | ✅ 使用 `traceCode` 參數或 Debug 面板 |
| Debug UI 可輸入股票代號 | ✅ 個股追蹤面板（現有 UI），新增後端 trace_code 串接 |
| traceCode=6271 時輸出 6271 TRACE | ✅ 後端 console 輸出 [TRACE 6271] |
| traceCode=2727 時輸出 2727 TRACE | ✅ 後端 console 輸出 [TRACE 2727] |
| 沒有 traceCode 時不輸出 TRACE | ✅ trace_code=None 時 TRACE_CODE=None，_trace 不執行 |
| TRACE_CODE 不再寫死在 screener.py | ✅ 已移除硬碼 |
| liquidityPassed=false 不得列為突破觀察 | ✅ `and _liq_passed` 已加入條件 |
| highInstRatioWarning > 30% 前端顯示警示 | ✅ 列表 ⚠️ icon + Drawer 警示 + Debug 面板 |
| 警示不直接排除股票 | ✅ 僅顯示，不影響分數與策略狀態邏輯 |
| Debug 中可看到 highInstRatioWarning=true | ✅ TRACE log + Debug 面板均顯示 |

---

## 修改的檔案

1. `screener.py` — `run_screener_query()` 參數化 + 突破觀察條件 + highInstRatioWarning 欄位 + TRACE log
2. `main.py` — `/api/screener/run` 傳遞 `trace_code`
3. `static/index.html` — Drawer 新增 `drawer-inst-ratio-warning` 容器
4. `static/app_pro.js` — 列表 ⚠️ icon + Drawer 警示 + Debug 面板擴充

---

## 尚未處理的問題

- `trace_stock_filters()` 中的 `突破觀察` 未加 `_liq_passed` 明確檢查（但該函式在流動性未通過時已提前 return，行為正確）
- Telegram 推播尚未加入 `highInstRatioWarning` 標記（現有推播格式已含法人佔比數值）
- 前端 Debug 面板 `/api/screener/run?traceCode=xxx` 支援已完成，但 Debug 面板本身（個股追蹤）仍使用 `/api/screener/trace`（行為正確，兩者互補）
