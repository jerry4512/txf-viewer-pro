# Capital Flow V2.1 Predictive Validation

## Research status: `INSUFFICIENT_HISTORY_FOR_STRATEGY_CONCLUSION`

This run freezes the existing V1 and V2.1 definitions. It does not modify scores, weights, thresholds, grade logic, MACD, formal ranking, or classification.

The database contains only 13 institutional dates, of which 4 have the full 10-day flow lookback. The longest forward horizon available after a full lookback is 3D; 5D/10D/20D mature samples do not exist. Therefore all model-comparison conclusions are **INCONCLUSIVE**.

## 1. Point-in-Time runner

- Historical signal dates: 2026-07-23 through 2026-08-10.
- Frozen stock-date rows in Universe A: 675.
- `future_data_rows_used = 0`.
- Signals use data `date <= t`. Entry/outcomes are joined afterward from `t+1 open` through `t+N close`.
- Forward return, TAIEX excess return, close-to-close reference, MFE and MAE are stored in the research CSV.
- Reliable industry-index history is unavailable, so industry excess returns were not fabricated.

## 2. Research universes

- Universe A: same-day grade A/B1 stocks that passed instrument, freshness, history, liquidity, and technical hard gates and have a V2.1 score. Stocks blocked only by the day's Market Regime remain in this technical universe; soft observation risks remain because they are not hard exclusions.
- Universe B: same-day formal `buy_candidates + high_priority_watch`, as a subset flag in the same dataset.
- The primary tables below use Universe A. Universe B rows are available in `capital_flow_v21_forward_analysis.csv`.

## 3. Daily Rank IC — V1 vs V2.1

| horizon | factor | IC dates | mean | median | std | positive IC % | IC IR | bootstrap 95% CI |
|---|---|---|---|---|---|---|---|---|
| 3D | V1_chip_bonus | 10 | -0.0454 | -0.0330 | 0.2412 | 30.00 | -0.1882 | [-0.1891, 0.0929] |
| 3D | V21_capital_flow | 10 | -0.0266 | -0.0876 | 0.2688 | 30.00 | -0.0989 | [-0.1732, 0.1252] |
| 5D | V1_chip_bonus | 8 | -0.0009 | -0.0372 | 0.2014 | 37.50 | -0.0046 | [-0.1340, 0.1268] |
| 5D | V21_capital_flow | 8 | -0.0388 | -0.0966 | 0.2621 | 37.50 | -0.1481 | [-0.2023, 0.1267] |
| 10D | V1_chip_bonus | 3 | 0.0617 | 0.0693 | 0.1568 | 66.67 | 0.3933 | [-0.0988, 0.2145] |
| 10D | V21_capital_flow | 3 | 0.1081 | 0.1301 | 0.1535 | 66.67 | 0.7046 | [-0.0551, 0.2494] |
| 20D | V1_chip_bonus | 0 | — | — | — | — | — | [—, —] |
| 20D | V21_capital_flow | 0 | — | — | — | — | — | [—, —] |

IC is daily cross-sectional Spearman against future TAIEX excess return. Bootstrap intervals resample signal dates, not individual stocks. Very small date counts make these intervals descriptive only.

## 4. Quantile test

| horizon | factor | group | sample | mean excess % | median excess % | bootstrap 95% CI |
|---|---|---|---|---|---|---|
| 3D | V1_chip_bonus | Q1 | 89 | 0.3796 | 1.1524 | [-2.0757, 2.3063] |
| 3D | V1_chip_bonus | Q2 | 94 | 2.2309 | 2.0631 | [-0.2737, 3.5435] |
| 3D | V1_chip_bonus | Q3 | 74 | 0.3183 | 0.0830 | [-2.9108, 2.1664] |
| 3D | V1_chip_bonus | Q4 | 109 | 0.2563 | -0.9497 | [-2.5041, 2.9532] |
| 3D | V1_chip_bonus | Q5 | 80 | 1.7457 | 1.8810 | [-1.4383, 4.6544] |
| 3D | V21_capital_flow | Q1 | 92 | -0.3563 | -0.5687 | [-2.2132, 1.2883] |
| 3D | V21_capital_flow | Q2 | 90 | 1.3992 | -0.0893 | [-1.1381, 3.1399] |
| 3D | V21_capital_flow | Q3 | 90 | 0.6513 | -0.1743 | [-1.9688, 2.5121] |
| 3D | V21_capital_flow | Q4 | 90 | 1.3885 | 0.8372 | [-1.8061, 3.6684] |
| 3D | V21_capital_flow | Q5 | 84 | 1.8799 | 1.7533 | [-1.7615, 4.4707] |
| 5D | V1_chip_bonus | Q1 | 63 | -1.6567 | -3.0801 | [-6.4207, 2.0379] |
| 5D | V1_chip_bonus | Q2 | 77 | 1.6862 | 1.3140 | [-0.9274, 3.8308] |
| 5D | V1_chip_bonus | Q3 | 47 | -3.3718 | -1.6054 | [-6.7711, -2.2009] |
| 5D | V1_chip_bonus | Q4 | 84 | -0.1367 | -2.0412 | [-4.2164, 3.5032] |
| 5D | V1_chip_bonus | Q5 | 58 | -0.9215 | -0.4113 | [-4.5817, 0.9692] |
| 5D | V21_capital_flow | Q1 | 68 | -1.3685 | -2.2745 | [-5.0956, 1.9795] |
| 5D | V21_capital_flow | Q2 | 66 | -0.6582 | -1.6000 | [-3.4047, 0.9735] |
| 5D | V21_capital_flow | Q3 | 67 | -1.0962 | -1.3220 | [-5.1806, 2.4075] |
| 5D | V21_capital_flow | Q4 | 66 | -0.5003 | -1.6193 | [-4.5693, 2.2584] |
| 5D | V21_capital_flow | Q5 | 62 | 0.7261 | 0.0171 | [-3.0206, 2.6515] |
| 10D | V1_chip_bonus | Q1 | 38 | -3.7591 | -1.2748 | [-7.5833, -1.0031] |
| 10D | V1_chip_bonus | Q2 | 57 | -0.0199 | -0.5272 | [-1.5056, 2.3499] |
| 10D | V1_chip_bonus | Q3 | 31 | -2.1799 | -1.0568 | [-2.6709, -1.7756] |
| 10D | V1_chip_bonus | Q4 | 40 | 1.4312 | -0.5021 | [-6.0457, 3.1226] |
| 10D | V1_chip_bonus | Q5 | 40 | -1.2504 | -1.5388 | [-3.9094, 2.0249] |
| 10D | V21_capital_flow | Q1 | 42 | -3.2536 | -1.2276 | [-5.1897, -2.1564] |
| 10D | V21_capital_flow | Q2 | 41 | 0.2443 | 1.3075 | [-2.7310, 1.9741] |
| 10D | V21_capital_flow | Q3 | 42 | -3.2097 | -2.9236 | [-4.5487, -1.6374] |
| 10D | V21_capital_flow | Q4 | 41 | 0.5375 | -0.0416 | [-2.4783, 5.2669] |
| 10D | V21_capital_flow | Q5 | 40 | 0.8771 | -1.5801 | [-0.1493, 1.7527] |
| 20D | V1_chip_bonus | Q1 | 0 | — | — | [—, —] |
| 20D | V1_chip_bonus | Q2 | 0 | — | — | [—, —] |
| 20D | V1_chip_bonus | Q3 | 0 | — | — | [—, —] |
| 20D | V1_chip_bonus | Q4 | 0 | — | — | [—, —] |
| 20D | V1_chip_bonus | Q5 | 0 | — | — | [—, —] |
| 20D | V21_capital_flow | Q1 | 0 | — | — | [—, —] |
| 20D | V21_capital_flow | Q2 | 0 | — | — | [—, —] |
| 20D | V21_capital_flow | Q3 | 0 | — | — | [—, —] |
| 20D | V21_capital_flow | Q4 | 0 | — | — | [—, —] |
| 20D | V21_capital_flow | Q5 | 0 | — | — | [—, —] |

V2.1 uses deterministic equal-count daily quintiles. V1 preserves score ties with average-rank groups; groups are uneven and some daily quintiles can be absent. With fewer than ten outcome dates, monotonicity is labelled `weak_monotonicity / insufficient_history` rather than interpreted as ranking skill.

## 5. Four-way V1/V2.1 cross validation

| horizon | group | sample | mean excess % | win rate % | bootstrap 95% CI |
|---|---|---|---|---|---|
| 3D | A_both_strong | 82 | 1.9035 | 59.76 | [-1.2369, 4.8759] |
| 3D | B_v1_strong_v21_weak | 27 | 0.5279 | 51.85 | [-2.1844, 3.3375] |
| 3D | C_v1_weak_v21_strong | 47 | 1.0806 | 51.06 | [-2.9419, 4.0234] |
| 3D | D_both_weak | 290 | 0.7363 | 50.34 | [-1.4979, 2.1417] |
| 5D | A_both_strong | 54 | -0.0556 | 46.30 | [-3.7087, 1.4626] |
| 5D | B_v1_strong_v21_weak | 20 | -3.0992 | 30.00 | [-6.3974, -0.5131] |
| 5D | C_v1_weak_v21_strong | 41 | 0.7300 | 46.34 | [-3.9624, 5.0774] |
| 5D | D_both_weak | 214 | -0.7612 | 42.52 | [-4.0480, 1.7326] |
| 10D | A_both_strong | 38 | -0.8943 | 39.47 | [-2.9845, 1.2356] |
| 10D | B_v1_strong_v21_weak | 12 | -4.3990 | 8.33 | [-7.7130, -1.2237] |
| 10D | C_v1_weak_v21_strong | 22 | 4.0460 | 50.00 | [-1.7721, 6.3137] |
| 10D | D_both_weak | 134 | -1.5415 | 44.03 | [-3.0308, -0.5391] |
| 20D | A_both_strong | 0 | — | — | [—, —] |
| 20D | B_v1_strong_v21_weak | 0 | — | — | [—, —] |
| 20D | C_v1_weak_v21_strong | 0 | — | — | [—, —] |
| 20D | D_both_weak | 0 | — | — | [—, —] |

The decisive comparison is C (V1 weak/V2.1 strong) minus B (V1 strong/V2.1 weak). Full forward return, MFE and MAE group/spread rows are in the analysis CSV. The available window is too short to establish stable C>B or B>C behavior.

## 6. Flow × Price quadrant

| horizon | quadrant | sample | mean excess % | win rate % | bootstrap 95% CI |
|---|---|---|---|---|---|
| 3D | Q1 | 199 | 0.5572 | 49.75 | [-2.1225, 2.9323] |
| 3D | Q2 | 123 | 0.6579 | 44.72 | [-1.2437, 1.8994] |
| 3D | Q3 | 61 | 3.1343 | 75.41 | [-0.1747, 4.5898] |
| 3D | Q4 | 63 | 0.8199 | 52.38 | [-2.3207, 2.5458] |
| 5D | Q1 | 126 | -1.3309 | 36.51 | [-4.4761, 1.5229] |
| 5D | Q2 | 98 | -0.6102 | 38.78 | [-3.2595, 1.0064] |
| 5D | Q3 | 49 | 0.8430 | 59.18 | [-3.9485, 5.2428] |
| 5D | Q4 | 56 | -0.2100 | 50.00 | [-4.7277, 2.9681] |
| 10D | Q1 | 57 | 0.2593 | 42.11 | [-0.1771, 2.4473] |
| 10D | Q2 | 57 | -0.8902 | 42.11 | [-3.0604, 1.3783] |
| 10D | Q3 | 42 | 0.2997 | 45.24 | [-0.9631, 2.5756] |
| 10D | Q4 | 50 | -3.6190 | 38.00 | [-6.6968, -2.2659] |
| 20D | Q1 | 0 | — | — | [—, —] |
| 20D | Q2 | 0 | — | — | [—, —] |
| 20D | Q3 | 0 | — | — | [—, —] |
| 20D | Q4 | 0 | — | — | [—, —] |

Q1-versus-Q2 daily spreads for forward return, excess, MFE and MAE are included in the analysis CSV. The present data cannot validate Confirmed Accumulation over Unconfirmed Accumulation.

## 7. Trust active/inactive and Foreign vs Trust

| horizon | actor factor | IC dates | mean IC | Q5-Q1 excess % | bootstrap 95% CI |
|---|---|---|---|---|---|
| 3D | foreign_signed_strength | 10 | -0.1255 | -1.2921 | [-2.7149, 0.3794] |
| 3D | trust_signed_strength_active_only | 10 | -0.0251 | 0.1347 | [-2.0561, 2.1409] |
| 5D | foreign_signed_strength | 8 | -0.1301 | -3.7700 | [-6.2544, -1.0787] |
| 5D | trust_signed_strength_active_only | 8 | -0.0309 | 0.1896 | [-2.9575, 2.7693] |
| 10D | foreign_signed_strength | 3 | 0.0908 | 2.7682 | [-0.5642, 4.8312] |
| 10D | trust_signed_strength_active_only | 3 | -0.0555 | -1.8849 | [-4.1743, 0.5455] |
| 20D | foreign_signed_strength | 0 | — | — | [—, —] |
| 20D | trust_signed_strength_active_only | 0 | — | — | [—, —] |

Trust active/inactive performance and active-only trust quintiles are reported separately in the analysis CSV. Actor tests use the V2.1 signed active-flow strength, so inactive trust observations do not enter trust rank IC.

## 8. Component ablation

| horizon | fixed model | IC dates | mean IC | Q5-Q1 excess % |
|---|---|---|---|---|
| 3D | full_v21 | 10 | -0.0266 | 1.2214 |
| 3D | without_intensity | 10 | -0.0355 | 1.5439 |
| 3D | without_persistence | 10 | -0.0197 | 1.3141 |
| 3D | without_momentum | 10 | -0.0121 | 1.0671 |
| 3D | without_price_confirmation | 10 | -0.0074 | 0.9083 |
| 3D | without_relative_flow | 10 | -0.0234 | 1.1436 |
| 5D | full_v21 | 8 | -0.0388 | -0.0068 |
| 5D | without_intensity | 8 | -0.0410 | 0.6644 |
| 5D | without_persistence | 8 | -0.0313 | -0.0923 |
| 5D | without_momentum | 8 | -0.0237 | 0.2381 |
| 5D | without_price_confirmation | 8 | 0.0259 | 0.4148 |
| 5D | without_relative_flow | 8 | -0.0356 | 0.0923 |
| 10D | full_v21 | 3 | 0.1081 | 4.3467 |
| 10D | without_intensity | 3 | 0.0921 | 4.7457 |
| 10D | without_persistence | 3 | 0.1027 | 4.0799 |
| 10D | without_momentum | 3 | 0.1261 | 5.1659 |
| 10D | without_price_confirmation | 3 | 0.0462 | 4.6633 |
| 10D | without_relative_flow | 3 | 0.0952 | 4.4351 |
| 20D | full_v21 | 0 | — | — |
| 20D | without_intensity | 0 | — | — |
| 20D | without_persistence | 0 | — | — |
| 20D | without_momentum | 0 | — | — |
| 20D | without_price_confirmation | 0 | — | — |
| 20D | without_relative_flow | 0 | — | — |

Ablation removes one existing component without changing any remaining weight. The limited observations do not support identifying a genuinely predictive component.

## 9. Controls for RS and technical score

| horizon | control | spread dates | high-low excess % | bootstrap 95% CI |
|---|---|---|---|---|
| 3D | V21_within_RS_quintile | 9 | 0.2629 | [-0.7128, 1.2109] |
| 3D | V21_within_grade_base_dist20 | 10 | 0.4341 | [-1.6497, 2.2520] |
| 5D | V21_within_RS_quintile | 7 | 0.1202 | [-1.4445, 1.3108] |
| 5D | V21_within_grade_base_dist20 | 8 | 1.2193 | [-1.2628, 3.2239] |
| 10D | V21_within_RS_quintile | 3 | 1.9241 | [1.2660, 2.4745] |
| 10D | V21_within_grade_base_dist20 | 3 | 3.4020 | [2.1530, 5.6767] |
| 20D | V21_within_RS_quintile | 0 | — | [—, —] |
| 20D | V21_within_grade_base_dist20 | 0 | — | [—, —] |

RS control first forms daily RS20 quintiles, then compares V2.1 high/low inside each quintile. Technical control uses same grade, fixed base-score bucket, and fixed dist20 bucket. Sparse within-stratum samples prevent an incremental-information conclusion.

## 10. Stability

The analysis CSV contains V2.1 Q5-Q1 spreads by month, quarter, and market regime. Only July/August 2026 and one quarter are present, so stability cannot be evaluated.

## 11. Top-N portfolio ranking research

| horizon | factor | portfolio | dates | mean excess % | bootstrap 95% CI |
|---|---|---|---|---|---|
| 5D | V1_chip_bonus | Top5 | 8 | -3.0101 | [-6.0251, -0.2950] |
| 5D | V1_chip_bonus | Top10 | 8 | -2.4718 | [-5.4946, -0.1094] |
| 5D | V1_chip_bonus | Top20 | 8 | -2.5077 | [-5.5321, 0.0139] |
| 5D | V21_capital_flow | Top5 | 8 | -1.0251 | [-4.7339, 2.5548] |
| 5D | V21_capital_flow | Top10 | 8 | -2.0368 | [-5.6769, 1.1834] |
| 5D | V21_capital_flow | Top20 | 8 | -1.7984 | [-5.4269, 1.5468] |
| 10D | V1_chip_bonus | Top5 | 3 | -1.9202 | [-4.3164, -0.0987] |
| 10D | V1_chip_bonus | Top10 | 3 | -0.7974 | [-4.4861, 2.0249] |
| 10D | V1_chip_bonus | Top20 | 3 | -0.9791 | [-3.1516, 2.1191] |
| 10D | V21_capital_flow | Top5 | 3 | 5.0351 | [0.8357, 10.3796] |
| 10D | V21_capital_flow | Top10 | 3 | 1.7940 | [-0.3618, 3.5923] |
| 10D | V21_capital_flow | Top20 | 3 | 0.2684 | [-0.5977, 1.4746] |
| 20D | V1_chip_bonus | Top5 | 0 | — | [—, —] |
| 20D | V1_chip_bonus | Top10 | 0 | — | [—, —] |
| 20D | V1_chip_bonus | Top20 | 0 | — | [—, —] |
| 20D | V21_capital_flow | Top5 | 0 | — | [—, —] |
| 20D | V21_capital_flow | Top10 | 0 | — | [—, —] |
| 20D | V21_capital_flow | Top20 | 0 | — | [—, —] |

These are equal-weight daily ranking observations with overlapping holding periods. They are not a tradable backtest and do not include costs, liquidity execution, or portfolio-capacity constraints.

## 12. Statistical limitations

- Every analysis row includes sample count, date count, mean, median, standard deviation, positive percentage, and date-cluster bootstrap 95% CI.
- Cross-sectional stock observations from the same date are correlated; the bootstrap therefore resamples dates.
- Early signals have partial institutional lookback and are exploratory only.
- No mature 5D/10D/20D outcome window exists after the full V2.1 lookback.
- Corporate-action adjustment and survivorship bias are uncontrolled.
- Daily stock coverage changes sharply around 2026-07-31.

## 13. Required conclusions

| Question | Conclusion | Reason |
|---|---|---|
| A. V2.1 是否比 V1 有更高 Rank IC？ | **INCONCLUSIVE** | 只有極少 outcome dates，且成熟 lookback 沒有 5D/10D/20D 樣本。 |
| B. V2.1 Q5 是否優於 Q1？ | **INCONCLUSIVE** | 探索性 quintile 結果不足以判斷單調性；標記 `weak_monotonicity / insufficient_history`。 |
| C. V1弱/V2.1強 是否優於 V1強/V2.1弱？ | **INCONCLUSIVE** | B/C 交叉組跨日期樣本過少，無法確認 5D、10D、MFE/MAE 的穩定改善。 |
| D. Confirmed Accumulation 是否優於 Unconfirmed？ | **INCONCLUSIVE** | Q1/Q2 的有效日期與成熟 forward horizon 不足。 |
| E. 控制 RS 後 V2.1 是否仍有額外資訊？ | **INCONCLUSIVE** | RS bucket 內樣本稀疏，沒有足夠日期形成可靠 CI。 |
| F. 控制 Technical Score 後是否仍有額外資訊？ | **INCONCLUSIVE** | grade/base/dist20 strata 內樣本更少，無法辨識 incremental effect。 |
| G. 哪個 component 最有資訊？ | **INCONCLUSIVE** | Ablation 結果僅是短窗探索，不能辨認穩定有效 component。 |
| H. 是否有足夠證據正式進入 Ranking？ | **MORE DATA REQUIRED** | 語意驗證通過不等於 predictive validation；目前歷史遠低於策略結論需求。 |

## 14. Scope stop

No V1/V2.1 formula, weight, threshold, quadrant, momentum state, technical rule, or formal ranking was modified. Research stops here for review.
