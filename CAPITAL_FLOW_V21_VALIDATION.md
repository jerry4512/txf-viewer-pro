# Capital Flow V2.1 Validation

## 1. Scope and invariants

- Fixed `as_of_date = 2026-08-10`; eligible common-stock universe = **767**.
- This audit changes only Shadow Mode semantics: activity, inactive momentum, active-only absolute-intensity percentile, signed strength, and direction-aware momentum.
- Formal A/B1/B2/C, category, V1 final score, MACD, factor caps, weights, and existing momentum thresholds were not changed.
- Original `capital_flow_score_v2_shadow` is retained; corrected output is `capital_flow_score_v21_shadow`.
- The prior `CAPITAL_FLOW_V2_VALIDATION.md` and `capital_flow_v2_snapshot.csv` remain untouched.

## 2. Formal Regression

| bucket | old | new | no-shadow hash | V2.1 hash | fixture hash | result |
|---|---|---|---|---|---|---|
| buy_candidates | 14 | 14 | 5d45b513286a | 5d45b513286a | 5d45b513286a | PASS |
| high_priority_watch | 22 | 22 | 1a72e80c273e | 1a72e80c273e | 1a72e80c273e | PASS |
| wait_pullback | 9 | 9 | 51f80fcdf82a | 51f80fcdf82a | 51f80fcdf82a | PASS |
| other_watch | 37 | 37 | 086e7f6b1735 | 086e7f6b1735 | 086e7f6b1735 | PASS |
| excluded | 2060 | 2060 | 5463fec40738 | 5463fec40738 | 5463fec40738 | PASS |

- `正式分類改變 = 0`
- `正式 V1 分數改變 = 0`
- `原 V2 shadow 分數相對舊快照改變 = 0`
- Formal regression: **PASS**

## 3. V2 vs V2.1

| comparison | Pearson | Spearman | Top10 | Top20 | Top30 |
|---|---|---|---|---|---|
| capital_flow_score_v2_shadow vs capital_flow_score_v21_shadow | 0.990857 | 0.942229 | 9/10 | 20/20 | 25/30 |

These values measure impact; lower correlation is not a success criterion.

## 4. Trust Flow Activity

| metric | count | percentage |
|---|---|---|
| Eligible universe | 767 | 100.00% |
| Trust active | 202 | 26.34% |
| Trust inactive | 565 | 73.66% |

### Trust active days in the latest 5 institutional trading days

| active days | count | percentage |
|---|---|---|
| 0 | 565 | 73.66% |
| 1 | 32 | 4.17% |
| 2 | 45 | 5.87% |
| 3 | 27 | 3.52% |
| 4 | 20 | 2.61% |
| 5 | 78 | 10.17% |

An actor is inactive only when all five daily raw flows are zero. A net-zero five-day sum with offsetting nonzero days remains active.

## 5. Momentum Distribution

### foreign — All Universe

| state | count | share |
|---|---|---|
| inactive | 1 | 0.13% |
| accelerating | 216 | 28.16% |
| stable | 162 | 21.12% |
| decelerating | 110 | 14.34% |
| reversing_positive | 121 | 15.78% |
| reversing_negative | 65 | 8.47% |
| neutral | 92 | 11.99% |

### foreign — Active Flow Only

| state | count | share |
|---|---|---|
| inactive | 0 | 0.00% |
| accelerating | 216 | 28.20% |
| stable | 162 | 21.15% |
| decelerating | 110 | 14.36% |
| reversing_positive | 121 | 15.80% |
| reversing_negative | 65 | 8.49% |
| neutral | 92 | 12.01% |

### trust — All Universe

| state | count | share |
|---|---|---|
| inactive | 565 | 73.66% |
| accelerating | 20 | 2.61% |
| stable | 25 | 3.26% |
| decelerating | 32 | 4.17% |
| reversing_positive | 20 | 2.61% |
| reversing_negative | 19 | 2.48% |
| neutral | 86 | 11.21% |

### trust — Active Flow Only

| state | count | share |
|---|---|---|
| inactive | 0 | 0.00% |
| accelerating | 20 | 9.90% |
| stable | 25 | 12.38% |
| decelerating | 32 | 15.84% |
| reversing_positive | 20 | 9.90% |
| reversing_negative | 19 | 9.41% |
| neutral | 86 | 42.57% |

## 6. Negative Flow Invariant

The direction-aware component uses the existing state magnitudes with actor cap 5 points. Positive direction is nonnegative, negative direction is nonpositive, inactive is zero, `reversing_positive` is nonnegative, and `reversing_negative` is nonpositive.

`reversing_positive` is the specification's explicitly named reversal exception: its five-day net direction may still be negative while the latest two days have turned positive.

```text
negative_flow_positive_momentum_count=0
direction_state_invariant_violation_count=0
```

Result: **PASS**

## 7. Active-only Percentile Distribution

| actor | active count | P5 | P10 | P25 | P50 | P75 | P90 | P95 |
|---|---|---|---|---|---|---|---|---|
| foreign | 766 | 5.12 | 10.12 | 25.10 | 50.06 | 75.03 | 90.02 | 95.01 |
| trust | 202 | 5.47 | 10.45 | 25.37 | 50.25 | 75.12 | 90.05 | 95.02 |

Ranking input is `abs(flow_ratio_5d)` and only active stocks participate. Direction is stored separately and signed strength applies the sign after ranking.

- `inactive active_percentile non-null = 0`
- `inactive signed_flow_strength non-zero = 0`
- Inactive invariant: **PASS**

## 8. Typical Cases

### A — trust inactive

Representative count shown: 5.

| code | name | last 5 trust flow (shares) | 5D ratio | activity | active days | direction | momentum | active pct | signed strength | momentum component | V2 | V2.1 |
|---|---|---|---|---|---|---|---|---|---|---|---|---|
| 1103 | 嘉泥 | [0, 0, 0, 0, 0] | +0.0000% | inactive | 0 | zero | inactive | null | 0.00 | 0.00 | 5.50 | 4.50 |
| 1104 | 環泥 | [0, 0, 0, 0, 0] | +0.0000% | inactive | 0 | zero | inactive | null | 0.00 | 0.00 | 7.50 | 6.50 |
| 1108 | 幸福 | [0, 0, 0, 0, 0] | +0.0000% | inactive | 0 | zero | inactive | null | 0.00 | 0.00 | 36.82 | 32.79 |
| 1109 | 信大 | [0, 0, 0, 0, 0] | +0.0000% | inactive | 0 | zero | inactive | null | 0.00 | 0.00 | 7.00 | 0.00 |
| 1110 | 東泥 | [0, 0, 0, 0, 0] | +0.0000% | inactive | 0 | zero | inactive | null | 0.00 | 0.00 | 7.50 | 6.50 |

### B — trust small persistent buys

Representative count shown: 5.

| code | name | last 5 trust flow (shares) | 5D ratio | activity | active days | direction | momentum | active pct | signed strength | momentum component | V2 | V2.1 |
|---|---|---|---|---|---|---|---|---|---|---|---|---|
| 2353 | 宏碁 | [-336000, 111429, 102000, 122371, 100497] | +0.0948% | active | 5 | positive | decelerating | 18.81 | 18.81 | 1.75 | 57.63 | 48.95 |
| 2493 | 揚博 | [9000, 0, -7000, 5000, 44000] | +0.2221% | active | 4 | positive | accelerating | 27.72 | 27.72 | 5.00 | 25.23 | 11.83 |
| 2609 | 陽明 | [1000, 34373, 45000, 55000, 42962] | +0.2594% | active | 5 | positive | decelerating | 30.20 | 30.20 | 1.75 | 77.38 | 68.61 |
| 5289 | 宜鼎 | [44000, 18471, -3000, 2500, 2542] | +0.3059% | active | 5 | positive | accelerating | 33.17 | 33.17 | 5.00 | 26.48 | 13.58 |
| 3533 | 嘉澤 | [44560, -70110, -5000, 17000, 30358] | +0.3453% | active | 5 | positive | accelerating | 35.64 | 35.64 | 5.00 | 27.62 | 21.92 |

### C — trust high-intensity buys

Representative count shown: 5.

| code | name | last 5 trust flow (shares) | 5D ratio | activity | active days | direction | momentum | active pct | signed strength | momentum component | V2 | V2.1 |
|---|---|---|---|---|---|---|---|---|---|---|---|---|
| 2880 | 華南金 | [7874085, 6331000, 10426000, 6911000, 6747000] | +31.2753% | active | 5 | positive | decelerating | 100.00 | 100.00 | 1.75 | 37.25 | 30.25 |
| 2886 | 兆豐金 | [4182245, 3718872, 7520000, 2849000, 2968372] | +14.8369% | active | 5 | positive | decelerating | 98.02 | 98.02 | 1.75 | 38.24 | 31.05 |
| 4551 | 智伸科 | [2000, 0, 145000, 189000, 255000] | +13.5550% | active | 4 | positive | accelerating | 97.52 | 97.52 | 5.00 | 38.97 | 31.75 |
| 3023 | 信邦 | [125000, 84000, 244000, 300000, 0] | +9.7958% | active | 4 | positive | decelerating | 96.53 | 96.53 | 1.75 | 40.21 | 37.90 |
| 3017 | 奇鋐 | [527110, 524300, 908000, 527200, 120179] | +9.2259% | active | 5 | positive | decelerating | 96.04 | 96.04 | 1.75 | 85.79 | 83.49 |

### D — trust stable negative

Representative count shown: 5.

| code | name | last 5 trust flow (shares) | 5D ratio | activity | active days | direction | momentum | active pct | signed strength | momentum component | V2 | V2.1 |
|---|---|---|---|---|---|---|---|---|---|---|---|---|
| 2204 | 中華 | [-268951, -663672, -991000, -695000, -297000] | -17.6000% | active | 5 | negative | stable | 99.01 | -99.01 | -3.50 | 43.69 | 36.12 |
| 4764 | 雙鍵 | [77000, 0, -500000, -528000, -587000] | -14.9815% | active | 4 | negative | stable | 98.51 | -98.51 | -3.50 | 29.03 | 18.28 |
| 5371 | 中光電 | [-1000000, -1101000, -990000, -3000000, -1842000] | -10.9120% | active | 5 | negative | stable | 97.03 | -97.03 | -3.50 | 44.74 | 37.18 |
| 3227 | 原相 | [-60000, -1000, -2800, -211000, -300000] | -7.6072% | active | 5 | negative | stable | 92.57 | -92.57 | -3.50 | 62.88 | 55.80 |
| 4991 | 環宇-KY | [0, 64550, -430000, 0, -280000] | -6.9620% | active | 3 | negative | stable | 91.58 | -91.58 | -3.50 | 61.16 | 53.11 |

### E — trust reversing positive

Representative count shown: 5.

| code | name | last 5 trust flow (shares) | 5D ratio | activity | active days | direction | momentum | active pct | signed strength | momentum component | V2 | V2.1 |
|---|---|---|---|---|---|---|---|---|---|---|---|---|
| 2472 | 立隆電 | [-160540, 31400, -917000, -2000, 78000] | -6.4027% | active | 5 | negative | reversing_positive | 90.10 | -90.10 | 3.00 | 37.16 | 34.69 |
| 5876 | 上海商銀 | [-1323000, 279000, -1395000, 320000, 221000] | -5.6470% | active | 5 | negative | reversing_positive | 87.62 | -87.62 | 3.00 | 44.66 | 43.29 |
| 3583 | 辛耘 | [-23000, -76000, -110000, 70000, -39000] | -4.0695% | active | 5 | negative | reversing_positive | 83.17 | -83.17 | 3.00 | 23.55 | 19.60 |
| 3045 | 台灣大 | [-945638, -31845, 47000, 105063, -73239] | -2.3335% | active | 5 | negative | reversing_positive | 71.29 | -71.29 | 3.00 | 14.00 | 7.00 |
| 6147 | 頎邦 | [-982050, -376500, -1327641, 9000, 50404] | -2.1976% | active | 5 | negative | reversing_positive | 69.80 | -69.80 | 3.00 | 10.00 | 3.00 |

### F — trust reversing negative

Representative count shown: 5.

| code | name | last 5 trust flow (shares) | 5D ratio | activity | active days | direction | momentum | active pct | signed strength | momentum component | V2 | V2.1 |
|---|---|---|---|---|---|---|---|---|---|---|---|---|
| 2059 | 川湖 | [233500, 74495, 3970, -68000, 15529] | +7.0745% | active | 5 | positive | reversing_negative | 92.08 | 92.08 | 0.00 | 59.40 | 58.71 |
| 2882 | 國泰金 | [4519000, -631512, 35000, -2000, -5284] | +4.6293% | active | 5 | positive | reversing_negative | 84.16 | 84.16 | 0.00 | 70.41 | 64.65 |
| 2812 | 台中銀 | [2619000, -24454, 0, 0, -8151] | +2.2483% | active | 3 | positive | reversing_negative | 70.30 | 70.30 | 0.00 | 26.27 | 16.65 |
| 3443 | 創意 | [150000, 7408, 52183, 100700, -130521] | +1.6349% | active | 5 | positive | reversing_negative | 65.84 | 65.84 | 0.00 | 66.64 | 59.70 |
| 3665 | 貿聯-KY | [404200, 131996, 508000, -672000, -76977] | +1.3015% | active | 5 | positive | reversing_negative | 56.93 | 56.93 | 0.00 | 50.26 | 39.44 |

## 9. V1 vs V2.1

| comparison | Pearson | Spearman | Top10 | Top20 | Top30 |
|---|---|---|---|---|---|
| chip_bonus_v1 vs capital_flow_score_v21_shadow | 0.864815 | 0.879428 | 4/10 | 15/20 | 20/30 |

Correlation is descriptive only. The success criteria are semantic correctness, activity/neutral separation, zero-tie removal from active percentile, direction-consistent momentum, and zero formal regression.

## 10. Validation Summary

| check | result | evidence |
|---|---|---|
| Formal category and V1 score unchanged | PASS | category=0, score=0, hashes=match |
| Original V2 score preserved | PASS | changed=0 |
| Inactive separated from neutral | PASS | trust inactive=565, active neutral=86 |
| Negative stable no positive momentum | PASS | count=0 |
| Inactive percentile/strength invariant | PASS | percentile=0, strength=0 |
| At least five examples per requested case | PASS | all six cases have 5 |

**Overall V2.1 validation: PASS**

This audit stops here. No factor-weight change, optimization, new strategy factor, or formal-strategy migration was performed.
