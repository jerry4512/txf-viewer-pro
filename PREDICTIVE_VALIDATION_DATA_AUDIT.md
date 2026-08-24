# Predictive Validation Data Audit

## Status: `INSUFFICIENT_HISTORY_FOR_STRATEGY_CONCLUSION`

## Source ranges

| source | start | end | trading dates | rows |
|---|---|---|---|---|
| daily_kbars | 2026-04-07 | 2026-08-10 | 87 | 88123 |
| institutional_trading | 2026-07-23 | 2026-08-10 | 13 | 29163 |
| market_index_daily | 2026-01-28 | 2026-08-10 | 126 | 126 |

- Dates where V1 and V2.1 can technically be calculated: **13**.
- Dates with the full 10-institutional-day lookback used by V2.1 persistence: **4**.
- Early dates are retained only as explicitly labelled exploratory partial-lookback observations; they are not treated as mature-model evidence.

## Forward-date availability

| horizon | all exploratory dates | full-lookback dates |
|---|---|---|
| 1D | 12 | 3 |
| 3D | 10 | 1 |
| 5D | 8 | 0 |
| 10D | 3 | 0 |
| 20D | 0 | 0 |

## Daily eligible universe and PIT check

| date | institution days | full lookback | Universe A raw | Universe A with V2.1 | Universe B | regime | future rows used |
|---|---|---|---|---|---|---|---|
| 2026-07-23 | 1 | False | 75 | 75 | 22 | strong_bull | 0 |
| 2026-07-24 | 2 | False | 59 | 59 | 17 | weak_bounce | 0 |
| 2026-07-27 | 3 | False | 72 | 72 | 17 | weak_bounce | 0 |
| 2026-07-28 | 4 | False | 27 | 27 | 13 | bear_break60 | 0 |
| 2026-07-29 | 5 | False | 14 | 14 | 6 | bear_break60 | 0 |
| 2026-07-30 | 6 | False | 25 | 25 | 9 | bear_break60 | 0 |
| 2026-07-31 | 7 | False | 29 | 29 | 8 | bear_break60 | 0 |
| 2026-08-03 | 8 | False | 28 | 28 | 10 | bear_break60 | 0 |
| 2026-08-04 | 9 | False | 44 | 44 | 19 | bear_break60 | 0 |
| 2026-08-05 | 10 | True | 73 | 73 | 28 | healthy_pullback | 0 |
| 2026-08-06 | 11 | True | 73 | 73 | 25 | healthy_pullback | 0 |
| 2026-08-07 | 12 | True | 74 | 74 | 32 | healthy_pullback | 0 |
| 2026-08-10 | 13 | True | 82 | 82 | 36 | healthy_pullback | 0 |

- `future_data_rows_used = 0` across all dates.
- Each strategy run receives its historical `as_of_date`; daily bars, institutional data, TAIEX, and shadow queries use `date <= t`. Forward prices are joined only after signal rows have been frozen.

## Corporate actions / adjusted prices

- Corporate-action/adjustment tables found: **0**.
- `daily_kbars` has only raw OHLCV columns and no adjusted flag. The ingestion source's adjustment behavior cannot be proven from stored metadata.
- Status: **UNKNOWN / UNCONTROLLED**. Splits, dividends, or ex-right adjustments can distort technical features and forward returns.

## Survivorship bias

- `security_master` rows: 2142; listing dates populated: 0; delisting dates populated: 0.
- There is no dated historical constituent master or delisted-security roster. Current names/classification metadata are used for historical dates.
- Status: **POSSIBLE / UNCONTROLLED SURVIVORSHIP BIAS**.

## Additional coverage limitation

Daily stock coverage has a structural jump near 2026-07-31 (roughly 900 codes before the jump and roughly 1,950–2,100 afterward). Cross-date universe composition is therefore not stable.

## Conclusion

`INSUFFICIENT_HISTORY_FOR_STRATEGY_CONCLUSION`

The available institutional window is 13 trading days and the mature 10-day-lookback window is only four dates. There are no full-lookback 5D/10D/20D forward samples. All predictive output must be treated as exploratory and cannot support a strategy-level conclusion.
