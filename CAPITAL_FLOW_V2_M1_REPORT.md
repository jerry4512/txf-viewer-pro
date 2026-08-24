# Capital Flow V2 Milestone 1 實作報告

## 1. 結論與範圍

本次只完成 V2.1 改造計畫的 Milestone 1：

1. `as_of_date`
2. 法人資料 point-in-time
3. 法人資料 schema
4. 外資／投信 1D、3D、5D、10D flow
5. flow intensity
6. flow persistence 與 momentum
7. Relative Strength
8. Flow × Price quadrant
9. Capital Flow V2 shadow fields
10. 自動測試與正式候選 regression

所有新因子均為 `shadow_mode = true`。它們可以在整合選股 API 的每檔股票資料中被讀取，但沒有任何正式評分、分類、排除或排序程式讀取這些欄位。

驗證基準日固定為 `2026-08-10`。正式候選清單的五個分類在修改前後，數量、順序及正式欄位的 SHA-256 全部相同。

Milestone 2（Market Breadth、Industry V2、stock vs industry RS、MoneyDJ structured scoring、Event Risk、ATR）未開始。

## 2. 修改檔案

| 檔案 | 工作 |
|---|---|
| `capital_flow_v2.py` | 新增完整 common-stock eligible universe 的 PIT 資金流、RS、percentile、四象限與 0–100 shadow score 計算。 |
| `stock_selection_schema.py` | 新增法人 identity 欄位的 idempotent schema migration；歷史無法拆分的自營商資料明確歸入 `dealer_unknown_net`。 |
| `migrations/001_stock_selection_v2_milestone1.sql` | 記錄 security master 與 Capital Flow V2.1 法人欄位 migration。SQLite 條件式 ALTER 由 Python migration 執行。 |
| `screener.py` | 官方 TWSE／TPEx 法人同步改為保存精確股數及自營自行買賣／避險分拆；既有正式欄位保留原定義。 |
| `main.py` | 法人同步只在官方同步實際成功時才累計成功交易日，空資料或休市日不再被誤算為完成。 |
| `integrated_strategy.py` | 載入獨立 shadow bundle，把欄位附加到 eligible 股票輸出；正式 score/classify/sort 不讀取它。 |
| `tests/stock_selection/test_capital_flow_v2.py` | 新增 PIT、schema、單位、公式、momentum、quadrant、score cap、官方欄位映射與 formal isolation 測試。 |
| `tests/stock_selection/fixtures/capital_flow_v2_m1_before.json` | 固定修改前正式候選 projection、順序與雜湊。 |
| `tests/stock_selection/fixtures/capital_flow_v2_m1_after.json` | 固定修改後正式候選 projection、順序與雜湊。 |
| `CAPITAL_FLOW_V2_M1_REPORT.md` | 本報告。 |

## 3. DB migration

### 3.1 `institutional_trading` 新欄位

| 欄位 | 型別 | 單位／意義 |
|---|---|---|
| `foreign_net` | INTEGER | 外資淨買賣超，股。TWSE 為「外陸資不含外資自營商」加「外資自營商」；TPEx 為外資及陸資合計。 |
| `trust_net` | INTEGER | 投信淨買賣超，股。 |
| `dealer_prop_net` | INTEGER | 自營商自行買賣淨額，股。可作 secondary confirmation，但目前只進 shadow score。 |
| `dealer_hedge_net` | INTEGER | 自營商避險淨額，股。只顯示，不給方向性正分。 |
| `dealer_unknown_net` | INTEGER | 歷史或舊格式只能取得自營商合計時保存於此，股。不得假裝可拆分。 |
| `flow_detail_level` | TEXT | `split`、`mixed` 或 `legacy_combined`。 |
| `flow_data_source` | TEXT | `twse_t86`、`tpex_3insti` 或 `legacy_migration`。 |

既有欄位 `foreign_buy`、`investment_buy`、`dealer_buy` 仍以「張」保存，供正式舊版籌碼評分使用；既有 `*_buy_shares` 仍保留。這是刻意的相容層，避免 shadow migration 改變正式候選。

### 3.2 歷史資料處理

Migration 對舊資料採保守回填：

```text
foreign_net = foreign_buy_shares，缺值才使用 foreign_buy × 1000
trust_net   = investment_buy_shares，缺值才使用 investment_buy × 1000

若 dealer_prop_net 與 dealer_hedge_net 都不存在：
dealer_unknown_net = dealer_buy_shares，缺值才使用 dealer_buy × 1000
flow_detail_level = legacy_combined
```

它不會把歷史自營商合計錯標為自行買賣或避險。

### 3.3 官方來源欄位

TWSE T86：

```text
formal foreign_buy_shares = 外陸資買賣超股數(不含外資自營商)
foreign_net = formal foreign_buy_shares + 外資自營商買賣超股數
trust_net = 投信買賣超股數
dealer_prop_net = 自營商買賣超股數(自行買賣)
dealer_hedge_net = 自營商買賣超股數(避險)
```

TPEx 三大法人：

```text
foreign_net = row[10] 外資及陸資合計買賣超
trust_net = row[13] 投信買賣超
dealer_prop_net = row[16] 自營商自行買賣買賣超
dealer_hedge_net = row[19] 自營商避險買賣超
legacy dealer_buy_shares = row[22] 自營商合計買賣超
```

截至驗證時，資料庫最近 10 個法人交易日（`2026-07-28` 至 `2026-08-10`）已重新由官方來源同步為 `split`：

| detail/source | rows |
|---|---:|
| `split / twse_t86` | 13,356 |
| `split / tpex_3insti` | 9,117 |
| `legacy_combined / legacy_migration` | 6,690 |

## 4. Point-in-time 與 universe

所有查詢均先截斷 `as_of_date`：

```sql
daily_kbars.date <= :as_of_date
market_index_daily.date <= :as_of_date
institutional_trading.date <= :as_of_date
```

法人視窗由 `as_of_date` 當下可見的最後 10 個法人交易日建立，不使用今日之後的資料。測試資料刻意放入未來日 K 與未來法人 999,000 股資料，計算結果證明它們不會滲入過去日期。

橫向 percentile 的 universe 不是正式候選清單，而是當日完整 eligible common-stock universe：

```text
security_type == common_stock
AND 最新日 K 日期 == as_of_date
AND 日 K 數量 >= 61（足以計算 60 日報酬）
```

`2026-08-10` 的 shadow universe 為 767 檔；正式整合候選僅 82 檔，因此 percentile 沒有使用候選股票反推。

## 5. 新公式

### 5.1 Flow 1D／3D／5D／10D

對外資與投信各自計算：

```text
actor_flow_Nd_shares = 最近 N 個可見法人交易日淨買賣超股數總和
actor_flow_Nd = actor_flow_Nd_shares / 1000
```

無 `_shares` 後綴的 `actor_flow_Nd` 單位是張；所有 normalization 都使用精確股數。

### 5.2 Intensity

```text
actor_flow_ratio_Nd
= actor_flow_Nd_shares
  / 同一批日期日 K 成交量總和 × 1000
```

日 K `volume` 單位為張，所以分母乘 1000 轉成股。

```text
actor_amount_ratio_5d
= Σ(每日法人淨買賣超股數 × 當日 close)
  / Σ(每日成交量張數 × 1000 × 當日 close)
```

法人淨買超金額是規格允許的 approximation，並已在 code comment 明確標示。

### 5.3 Persistence

```text
actor_positive_days_5  = 最近 5 法人交易日中 flow > 0 的日數
actor_positive_days_10 = 最近 10 法人交易日中 flow > 0 的日數
actor_consecutive_buy  = 從 as_of_date 往回連續 flow > 0 的日數
```

連買天數只保存為 component，不直接改變正式結果。

### 5.4 Flow Momentum

判斷順序如下；較前面的規則優先：

```text
reversing_negative:
    flow_5d > 0 and last_2d_flow < 0

reversing_positive:
    flow_5d < 0 and last_2d_flow > 0

accelerating:
    flow_1d > 0 and flow_3d > 0
    and flow_ratio_1d > flow_ratio_3d

decelerating:
    flow_5d > 0 and flow_3d > 0
    and flow_ratio_1d < flow_ratio_3d

stable:
    flow_1d、flow_3d、flow_5d 全正或全負

neutral:
    其餘情況
```

此版本未增加「明顯下降」的額外 magic number；所有可調常數集中在 `CAPITAL_FLOW_V2_CONFIG`。

### 5.5 Return 與 Relative Strength

```text
return_Nd = (close_t / close_t-N - 1) × 100

rs5  = stock_return5  - taiex_return5
rs20 = stock_return20 - taiex_return20
rs60 = stock_return60 - taiex_return60
```

輸出同時保留規格中的別名 `rs_5d = rs5`、`rs_20d = rs20`。

```text
rs20_percentile = rs20 在完整 eligible universe 的 pandas average-rank percentile
rs60_percentile = rs60 在完整 eligible universe 的 pandas average-rank percentile
```

`foreign_flow_intensity_percentile` 與 `trust_flow_intensity_percentile` 同樣以全 universe 的 5 日 flow ratio 計算。為相容規格範例，也輸出 `foreign_flow_percentile` 與 `trust_flow_percentile` 別名。

### 5.6 Flow × Price quadrant

本版固定使用：

```text
combined_flow_5d = foreign_flow_5d_shares + trust_flow_5d_shares
price_rs_positive = return_5d > 0 and rs20 > 0
```

| Quadrant | 精確條件 | `flow_price_state` |
|---|---|---|
| Q1 | combined flow > 0 且 price_rs_positive | `confirmed_accumulation` |
| Q2 | combined flow > 0 且非 price_rs_positive | `unconfirmed_accumulation` |
| Q3 | combined flow < 0 且 price_rs_positive | `absorption_divergence` |
| Q4 | combined flow < 0 且非 price_rs_positive | `confirmed_distribution` |

combined flow 等於 0 或缺少報酬／RS 時，quadrant 為 `null`、state 為 `neutral`。

## 6. Capital Flow V2 shadow score

總分 0–100，六個 factor bucket 的 cap 固定為 10 + 25 + 10 + 10 + 25 + 20。這些權重沒有依歷史結果調校。

| Component | Cap | 現行 shadow 公式 |
|---|---:|---|
| Identity | 10 | 外資 5D 正 +3.5；投信 5D 正 +3.5；兩者同正 +2；`split` 且 dealer prop 5D 正 +1。 |
| Intensity | 25 | 外資與投信各計 `clamp(max(flow_ratio_5d,0) / 5%, 0, 1)`，兩者平均後乘 25。 |
| Persistence | 10 | 外資、投信 `positive_days_10 / 10` 的平均乘 10。 |
| Momentum | 10 | actor quality：accelerating 1.0、stable 0.7、decelerating 0.35、reversing_positive 0.6、reversing_negative 0、neutral 0.2；兩者平均乘 10。 |
| Price Confirmation | 25 | Q1=25、Q2=5、Q3=0、Q4=0。Q3 只觀察，不給正分。 |
| Cross-sectional | 20 | actor 5D flow 正時採其 intensity percentile/100，否則為 0；外資投信平均乘 20。 |

`dealer_hedge_net` 從未獲得方向性正分。`multi_flow_confirmation` 只存在於 Identity 的 +2，沒有第三次無限制重複加分。

## 7. 新增 shadow 輸出欄位

每檔 eligible 股票會附加：

```text
shadow_mode
capital_flow_v2_available
capital_flow_v2_unavailable_reason

return_1d, return_3d, return_5d
stock_return5, stock_return20, stock_return60
taiex_return5, taiex_return20, taiex_return60
rs5, rs20, rs60, rs_5d, rs_20d
rs20_percentile, rs60_percentile

foreign_flow_{1,3,5,10}d_shares
foreign_flow_{1,3,5,10}d
foreign_flow_ratio_{1,3,5,10}d
foreign_amount_ratio_5d
foreign_positive_days_5, foreign_positive_days_10
foreign_consecutive_buy
foreign_flow_momentum

trust_flow_{1,3,5,10}d_shares
trust_flow_{1,3,5,10}d
trust_flow_ratio_{1,3,5,10}d
trust_amount_ratio_5d
trust_positive_days_5, trust_positive_days_10
trust_consecutive_buy
trust_flow_momentum

dealer_prop_flow_5d_shares, dealer_prop_flow_5d
dealer_hedge_flow_5d_shares, dealer_hedge_flow_5d
dealer_unknown_flow_5d_shares, dealer_unknown_flow_5d
dealer_flow_detail_level

foreign_flow_intensity_percentile
trust_flow_intensity_percentile
foreign_flow_percentile
trust_flow_percentile
multi_flow_confirmation
flow_price_quadrant
flow_price_state

flow_identity_score
flow_intensity_score
flow_persistence_score
flow_momentum_score
flow_price_confirmation_score
flow_relative_score
capital_flow_score_v2_shadow
```

整體輸出另含 `capital_flow_v2_shadow` metadata：基準日、universe size、法人日期、台股大盤報酬、config 與 errors，但不重複包含整份股票 map。

## 8. 任選 10 檔股票的新舊籌碼比較

舊欄位是正式系統使用的整數張；新欄位由官方精確股數換算，可保留小於一張的差異。Ratio 為淨買超股數／同期成交股數。

| 代號 | 名稱 | 舊外資5D(張) | 新外資5D(張) | 外資 ratio | 舊投信5D(張) | 新投信5D(張) | 投信 ratio | 外資 momentum | 投信 momentum | RS20 | Quadrant | Shadow score |
|---|---|---:|---:|---:|---:|---:|---:|---|---|---:|---|---:|
| 2330 | 台積電 | -3429 | -3428.975 | -2.297% | -640 | -639.520 | -0.428% | reversing_positive | reversing_positive | -1.464 | Q4 | 12.00 |
| 2891 | 中信金 | 34859 | 34862.268 | 22.897% | 2444 | 2443.670 | 1.605% | decelerating | accelerating | 0.995 | Q1 | 82.87 |
| 1402 | 遠東新 | 43614 | 43615.999 | 46.222% | -1584 | -1582.306 | -1.677% | decelerating | reversing_positive | 2.485 | Q1 | 64.66 |
| 3362 | 先進光 | 504 | 504.665 | 2.066% | 0 | 0.000 | 0.000% | decelerating | neutral | -4.575 | Q2 | 25.74 |
| 2882 | 國泰金 | 1010 | 1010.690 | 1.195% | 3916 | 3915.204 | 4.629% | neutral | reversing_negative | 5.442 | Q1 | 70.41 |
| 5371 | 中光電 | 18524 | 18526.095 | 25.483% | -7933 | -7933.000 | -10.912% | accelerating | stable | -9.183 | Q2 | 44.74 |
| 4938 | 和碩 | 12801 | 12802.160 | 26.118% | 1150 | 1150.912 | 2.348% | accelerating | decelerating | 9.349 | Q1 | 86.09 |
| 2542 | 興富發 | -3240 | -3241.204 | -13.332% | 2010 | 2010.000 | 8.268% | stable | accelerating | 3.539 | Q3 | 41.92 |
| 2606 | 裕民 | 8694 | 8695.651 | 41.128% | -629 | -629.000 | -2.975% | decelerating | neutral | 5.534 | Q1 | 59.58 |
| 6197 | 佳必琪 | -809 | -807.090 | -6.802% | 43 | 43.000 | 0.362% | neutral | neutral | -4.941 | Q4 | 20.68 |

## 9. Quadrant 範例

| Quadrant | 範例 | Combined flow / Price evidence | 解讀 |
|---|---|---|---|
| Q1 | 2891 中信金 | 外資與投信 5D 合計正、5D 報酬正、RS20 正 | Confirmed Accumulation |
| Q2 | 3362 先進光 | 5D flow 正，但 RS20 = -4.575 | Unconfirmed Accumulation |
| Q3 | 2542 興富發 | 外資＋投信合計為負，但價格與 RS 保持正向 | Absorption / Divergence |
| Q4 | 2330 台積電 | 5D flow 負，5D 價格／RS 條件未確認 | Confirmed Distribution |

## 10. Shadow score breakdown 範例

| 股票 | Identity | Intensity | Persistence | Momentum | Price | Relative | Total |
|---|---:|---:|---:|---:|---:|---:|---:|
| 2891 中信金 | 9.00 | 16.51 | 7.00 | 6.75 | 25.00 | 18.61 | 82.87 |
| 4938 和碩 | 9.00 | 18.37 | 8.00 | 6.75 | 25.00 | 18.97 | 86.09 |
| 3362 先進光 | 4.50 | 5.16 | 3.00 | 2.75 | 5.00 | 5.33 | 25.74 |
| 2542 興富發 | 4.50 | 12.50 | 6.50 | 8.50 | 0.00 | 9.92 | 41.92 |
| 2330 台積電 | 0.00 | 0.00 | 6.00 | 6.00 | 0.00 | 0.00 | 12.00 |

此分數沒有進入 `final_score`，也沒有參與任何排序。

## 11. Before / After 正式候選 regression

比較方法：對每個分類維持原順序，僅投影下列正式欄位，再以 `ensure_ascii=false`、`sort_keys=true`、無空白 JSON 計算 SHA-256：

```text
stock_id, stock_name, final_category, tomorrow_category,
final_score, base_score, chip_bonus, industry_bonus,
liquidity_bonus, broker_bonus, risk_penalty,
grade, risk_reward, rr_valid, rr_buyable
```

| 正式分類 | Before count | After count | Before SHA-256 | After SHA-256 | 結果 |
|---|---:|---:|---|---|---|
| 明日可買 | 14 | 14 | `5d45b513...f8b0fa` | `5d45b513...f8b0fa` | 完全一致 |
| 高優先觀察 | 22 | 22 | `1a72e80c...0b785b` | `1a72e80c...0b785b` | 完全一致 |
| 等回測 | 9 | 9 | `51f80fcd...475781b` | `51f80fcd...475781b` | 完全一致 |
| 其他觀察 | 37 | 37 | `086e7f6b...0bbff8` | `086e7f6b...0bbff8` | 完全一致 |
| 排除 | 2060 | 2060 | `5463fec4...8c443` | `5463fec4...8c443` | 完全一致 |

正式清單順序亦逐檔一致。完整值保存在 before/after fixture。

## 12. Tests

新增測試涵蓋：

- 六種 flow momentum 狀態與判定優先序
- Q1／Q2／Q3／Q4
- 六個 factor bucket cap 及總分 100 上限
- 歷史 dealer total 必須進 `dealer_unknown_net`
- 未來日 K／法人資料不得進入過去 `as_of_date`
- reverse ETF 不得進入 percentile universe
- 精確股數與日 K 張數的 flow ratio 單位換算
- amount ratio approximation
- RS 5／20／60 與別名
- 官方 TWSE／TPEx identity split 欄位映射
- shadow 欄位不能改變正式 score
- versioned formal Before/After 完全一致

執行命令：

```bash
python3 -m pytest -q tests/stock_selection
```

選股測試結果：`52 passed`。

交付前另執行：

```bash
python3 -m pytest -q --disable-warnings --maxfail=1
```

全專案結果：`83 passed, 8 warnings`。Warnings 為既有警告，沒有測試失敗。

## 13. 尚未解決／刻意保留項目

1. `market_foreign_flow` 與 `stock_foreign_specificity` 尚未建立。V2.1 規格將它寫成「如果可取得」的 shadow metric；目前沒有已驗證、同口徑的全市場外資每日淨額表。
2. 早期 6,690 筆歷史自營商資料只有 combined total，不能事後可靠拆成 prop／hedge，因此保留 `dealer_unknown_net`。最近 10 個交易日已可拆分。
3. 法人金額沒有官方逐股淨買賣超金額欄位，目前依規格用每日淨股數乘 close 近似。
4. `price_rs_positive` 本版明確定義為 `return_5d > 0 and rs20 > 0`。這是第一版固定規則，尚未回測或最佳化。
5. Shadow score 的 5% intensity full-score threshold 與 momentum quality 權重集中在 config，但尚未做 Milestone 3 的 train／validation／out-of-sample 驗證。
6. Shadow 結果目前隨整合選股 API 輸出，尚未建立每日 research dataset 持久化表或 CSV 排程。
7. 本次沒有啟用任何新 hard gate、正式加減分或分類規則。

## 14. Milestone 停止點

Milestone 1 已完成並停止。後續若要開始 Milestone 2，應另行確認後再處理；本次沒有提前實作任何 Milestone 2 因子。
