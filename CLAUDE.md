# CLAUDE.md

This file provides guidance to Claude Code (claude.ai/code) when working with code in this repository.

## Project Overview

**TXF Pro Viewer** is a Taiwan stock futures trading analytics platform built with FastAPI backend and JavaScript frontend. It monitors Taiwan Futures Index (TXF/MXF/TMF), provides real-time charting with technical indicators, and runs an automated screening algorithm to identify trading candidates based on institutional activity and technical patterns.

- Backend: FastAPI + Fubon Neo read-only futures market data, pandas, SQLite
- Frontend: HTML5 with TradingView Lightweight Charts v4.1.1, WebSocket real-time updates
- Database: SQLite — `kbars_cache.db` (1-min K-bars) and `stock_cache.db` (daily stock data, institutional trading)

## Running the Application

**Windows:**
```bat
啟動工具.bat
```
Or manually:
```powershell
python -m uvicorn main:app --host 127.0.0.1 --port 8000
```

**macOS/Linux:**
```bash
bash 啟動工具.command
# or
python3 -m uvicorn main:app --host 127.0.0.1 --port 8000
```

Then open `http://127.0.0.1:8000`.

**Setup:**
1. `pip install -r requirements.txt`
2. Run `python3 install_fubon_sdk.py` to install the official platform wheel
3. Copy `.env.example` → `.env` and fill in the Fubon API Key settings

**Test K-bar caching:**
```bash
python test_kbars.py
```

**Inspect SQLite cache:**
```bash
sqlite3 kbars_cache.db "SELECT DISTINCT contract_code FROM kbars1m;"
```

## Architecture

### Backend — `main.py`

**Global state** (module-level singletons):
- `api` — `FubonMarketDataClient` session (one instance per process)
- `is_logged_in` — Boolean gate for all API calls
- `contract` — Currently selected contract object
- `last_snapshot_cache` — Dict preserving last known OHLCV for fallback
- `manager` — WebSocket connection pool for broadcasting ticks

**Login flow** (`POST /api/login`): Authenticates using Fubon API Key login, initializes `Mode.Normal`, resolves the current TXF/MXF/TMF monthly contracts, and subscribes read-only day/night `trades`, `aggregates`, and `candles` channels. The application contains no order operations.

**Real-time quotes**: Fubon futures WebSocket messages are normalized in `fubon_market_data.py`, then passed to `global_quote_callback()` and broadcast to browser WebSockets. The health loop reports stale/disconnected states without polling REST continuously.

**Four weighted stocks**: 2330/2454/2317/2308 are bootstrapped from Fubon stock REST intraday candles/quotes and continued through Fubon stock `trades` + `candles` WebSocket subscriptions. TWSE MIS remains an error-only fallback.

**K-bars API** (`GET /api/kbars`):
- Resolves rolling aliases (for example `TXFR1`) from Fubon's current TAIFEX contract list
- Uses Fubon intraday day/night candles for the current trading date
- Caches 1-min bars in `kbars_cache.db` (WAL mode, 60 s timeout); skips already-cached dates on repeat requests
- Resamples 1-min → 5/15/30/60-min or daily on the fly
- Preserves the existing Taiwan wall-clock cache representation and converts to UTC epoch before returning to the frontend

**Institutional rankings** (`GET /api/institutional_rankings`): Scrapes TWSE T86 endpoint, parses by field name (not index), caches daily as local JSON to avoid rate limiting.

**Screener** (`POST /api/screener/run`): Delegates to `screener.run_screener_query()`. Also scheduled daily at 18:00 weekdays via APScheduler, which then sends Telegram alerts.

### Screener Engine — `screener.py`

Six-step pipeline (`run_screener_query()`):

1. **Institutional 5-day filter** — selects stocks where the 5-day sum of (foreign + investment trust + dealer) > 0 AND (foreign > 0 OR investment trust > 0) from `institutional_trading` table.
2. **Daily quote filter** — fetches TWSE + TPEx close/volume/change; filters out stocks with `change_pct < max_decline_pct` (default −3.5 %).
3. **K-bar load** — reads daily bars from `stock_cache.db`; requires ≥ 62 days of history to compute 60 MA.
4. **Technical analysis** — computes 5/10/20/60-day EMAs, MACD (12/26/9), Bollinger Bias (20-day deviation %), 20/60-day gain vs index baseline (1.5 % / 4.0 %), and liquidity check (5-day or 20-day avg amount ≥ 50 M TWD).
5. **Strategy assignment** — scores and classifies: `明日優先` (Tomorrow Priority), `觀察中` (Watching), `待機中` (On Standby). Each stock gets a stop-loss price and entry rules.
6. **Industry rankings** — aggregates institutional momentum by industry sector.

Key SQLite tables: `daily_kbars` (code, date, open, high, low, close, volume), `institutional_trading` (code, date, foreign_buy, investment_buy, dealer_buy), `stock_names` (code, name, category).

Debug a single stock: `GET /api/screener/trace?code=2330`

### Frontend — `static/app_pro.js` + `static/index.html`

**`TradingPane` class** is the core abstraction for each chart panel:
- `init()` — creates a LightweightCharts instance; the `tickMarkFormatter` **adds 8 h** to display UTC+8 Taiwan time.
- `loadKbars(contractCode, startDate, endDate, period)` — fetches `/api/kbars`, stores in `kbarsCache`, redraws.
- `updateRealTime(tick)` — merges incoming WebSocket tick into the current candlestick bar.
- MA series: 5-day (yellow), 10-day (cyan), 20-day (purple).

**View modes**: `viewMode=1` (single), `=2` (50/50 dual), `=3` (33 % triple). All panes synchronise crosshair on hover.

**WebSocket flow**: `{type: "tick", data: {time, price}}` → `TradingPane.updateRealTime()` → candlestick + MA/MACD update → all panes synced.

## Key Implementation Details

**Timezone compatibility**: Existing SQLite rows encode Taiwan wall-clock time. The backend converts Fubon ISO timestamps into that cache representation, subtracts 8 h before returning UTC epoch seconds, and the frontend adds 8 h for display. Any format migration must update all three layers together.

**SQLite concurrency**: `get_db_connection()` enables WAL mode with a 60 s timeout. This prevents "database is locked" errors when screener sync and K-bar fetch run concurrently.

**Rolling contract resolution**: `TXFR1`/`MXFR1`/`TMFR1` are resolved to the earliest non-expired monthly contract from Fubon's contract list while cache rows continue using stable aliases.

**Log markers**: `[SECURE]` login, `[WS]` WebSocket, `[CHART]` K-bar fetch, `[CACHE]` SQLite hit/miss, `>>>` real-time tick (high frequency).

## Constraints

- Fubon's documented futures REST candles endpoint is intraday-only. The current day is backfilled from REST; older bars come from `kbars_cache.db`.
- A multi-day application outage cannot be reconstructed from an undocumented SDK route. Do not guess historical method names.
- TWSE T86 scraping can fail during market closure or exchange maintenance; local JSON cache is the fallback.
- Institutional data backfill: call `sync_twse_institutional_data(date)` manually for missed trading days.
- `stock_cache.db` needs ≥ 62 days of bars per stock before a stock qualifies for screener output. The four weighted-stock intraday charts use Fubon, but the screener's bulk historical stock sync remains a separate local-cache workflow.
