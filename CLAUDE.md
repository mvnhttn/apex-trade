# CLAUDE.md

This file provides guidance to Claude Code (claude.ai/code) when working with this repository.

## Project Overview

**APEX TRADE** is a professional financial trading platform with a FastAPI backend and a vanilla JavaScript single-page frontend. It provides real-time technical analysis, trading signals, news aggregation, portfolio tracking, and a trade journal — using Yahoo Finance as the primary data source.

## Running the Application

```bash
# Install dependencies (one-time)
pip install -r requirements.txt

# Start the development server (with hot reload)
uvicorn backend.main:app --reload

# Access at http://localhost:8000
```

There is no test suite, build step, or package.json. No Node.js tooling is used.

## Architecture

### Backend (`backend/main.py`)

Single-file FastAPI application (~750 lines). Key sections:

- **`ASSETS` dict** — 11 tracked instruments (symbol, name, type, Yahoo Finance ticker, fallback ticker)
- **`cache_get()` / `cache_set()`** — simple in-memory cache with 5-minute TTL (`_cache` dict + `CACHE_TTL = 300`)
- **`fetch_df(symbol, period, interval="1d")`** — fetches OHLCV data with multi-level fallback (primary ticker → fallback → `max` period). Uses `curl_cffi` Chrome impersonation; falls back to standard yfinance. Supports custom tickers not in ASSETS.
- **`_parse_news_item(n)`** — handles both old yfinance news format (`n['title']`) and new format (`n['content']['title']`) introduced in yfinance ≥ 0.2.50
- **`safe_float(val, default=None)`** — NaN-safe float conversion for all indicator values
- **`sl(series)` / `sl100(series)`** — converts pandas Series to Python list (NaN → None), sl100 multiplies by 100
- **Technical indicators** — RSI, MACD, Bollinger Bands, Stoch RSI, ATR, OBV, MA20/50/200 via `ta` library
- **Signal scoring** — RSI position + MACD direction + MA50/MA200 crossovers → BUY / WATCH / HOLD / SELL
- **Support/Resistance** — local min/max over 90-day rolling window, 10-period lookback
- **`run_backtest()`** — backtesting engine (RSI/MACD/combined strategies, equity curve, Sharpe ratio)

**REST API endpoints:**

| Endpoint | Description |
|---|---|
| `GET /` | Serves `frontend/index.html` |
| `GET /api/health` | Health check, reports curl_cffi availability |
| `GET /api/assets` | All assets with current price and daily change |
| `GET /api/asset/{symbol}?period=&interval=` | Full technical analysis + full OHLC + indicator series for charts |
| `GET /api/backtest/{symbol}?period=&strategy=&capital=` | Backtesting results |
| `GET /api/news/{symbol}` | Asset-specific news (yfinance, new+old format) |
| `GET /api/news/feed` | Aggregated news all assets + SPX + OIL (5-min cache, 12s timeout/ticker) |
| `GET /api/screen?symbols=` | Screener: parallel analysis of all assets (ATR%, RSI, Signal, MACD, MA) |
| `GET /api/correlations` | Pearson correlation matrix (3-month daily returns, all assets) |
| `GET /api/search/{ticker}` | Validates any Yahoo Finance ticker, returns name/type/price |
| `GET /api/quote/{ticker}` | Lightweight price fetch for custom assets |

CORS is fully open (`allow_origins=["*"]`).

### Frontend (`frontend/index.html`)

Single-file vanilla JS SPA (~3000 lines, no framework). All HTML, CSS, and JS in one file.

**CDN scripts (in order):**
1. Chart.js 4.4.1 (cdnjs)
2. Hammer.js 2.0.8 (cdnjs) — for touch zoom
3. chartjs-plugin-zoom 2.0.1 (jsdelivr)
4. Luxon 3 + chartjs-adapter-luxon 1 (jsdelivr)

**Layout:** CSS Grid — sticky header | 260px sidebar | flexible main | 320px right panel

**Tab system (6 tabs):** ANALYSE | SCREENER | BACKTEST | JOURNAL | PORTFOLIO | NEWS

Full-width panels use `.full-panel { grid-column: 2 / 4 }`. `switchTab(tab)` hides all panels and shows the correct one. News tab auto-loads on first open.

**Global state variables:**
```javascript
allAssets, selectedSymbol, currentDetail, currentPeriod
currentInterval  // '1h' | '1d' | '1wk'
chartType        // 'line' | 'candle'
activeOverlays   // Set(['ma50','ma200']) — MA/BB overlay toggles
activeOscillator // 'rsi' | 'macd' | 'stoch' | null
chartInstance, oscChartInstance, portfolioChartInst
techCache        // { symbol: { rsi, signal, price, ma50, ma200 } }
watchlist        // Set — localStorage 'apexWatchlist'
priceAlerts      // [] — localStorage 'apexAlerts' (price + technical alerts)
alertTab         // 'preis' | 'signal'
customAssets     // [] — localStorage 'apexCustomAssets'
journal          // [] — localStorage 'apexJournal'
journalFilter, journalView  // 'tabelle' | 'kalender'
calYear, calMonth
checklistItems, checklistState  // localStorage 'apexChecklist'
portfolio        // [] — localStorage 'apexPortfolio'
screenerData, screenerFilter
corrData
newsFeed, newsFilter
riskKapital, riskProzent  // persist across asset switches
backtestResult, btPeriod, btStrategy, btCapital
```

**Key JS functions:**
- `loadAllAssets()` — fetches `/api/assets` + custom quotes, populates sidebar + header ticker, calls `checkAlerts()`
- `loadAssetDetail(symbol, period, interval)` — fetches full detail, updates `techCache[symbol]`, renders all panels
- `renderMain()` — renders chart section with interval buttons (1H/1T/1W), timeframe buttons, candlestick toggle, overlay/oscillator toggles
- `renderChart()` — **line mode**: Chart.js line + MA/BB overlays + zoom/pan. **Candle mode**: floating bar + custom `apexWicks` plugin (no external library). BB uses `fill: { target: '+1', ... }` on upper band. Zoom via `chartjs-plugin-zoom`.
- `renderOscillator()` — sub-chart (90px) below price chart for RSI/MACD/StochRSI
- `renderRightPanel()` — entry zones, risk calculator, alert card, analyst targets, news
- `checkAlerts(assets)` — checks both price alerts (above/below) and technical alerts (rsi_below/above, signal_buy/sell, ma50_above/below) against `techCache`
- `renderJournal()` — renders checklist, 8 stat cards (Profit Factor, Expected Value, Best/Worst, Avg Hold), table or calendar view
- `renderCalendar()` — monthly P&L grid from journal data (colored cells)
- `renderPortfolio()` — unrealized P&L per position + Chart.js doughnut for allocation
- `renderScreener()` — table with ATR%, RSI bar, Signal badge; also fills `techCache` from screener data
- `loadCorrelations()` / `renderCorrelations()` — fetches + renders color-coded correlation heatmap
- `loadNewsFeed()` — fetches `/api/news/feed` + Fear & Greed from `api.alternative.me/fng/` in parallel
- `renderNewsFeed()` — news cards with symbol filter buttons
- `fmt(n, d=2)` / `fmtPrice(v, sym)` / `fmtLarge(n)` — German-locale number formatting

## Tracked Assets

| Symbol | Name | Type |
|---|---|---|
| XAU | Gold | Commodity |
| XAG | Silver | Commodity |
| HG | Copper | Commodity |
| BTC | Bitcoin | Crypto |
| ETH | Ethereum | Crypto |
| FCX | Freeport-McMoRan | Stock |
| AG | First Majestic Silver | Stock |
| NEM | Newmont Corp. | Stock |
| NVDA | Nvidia | Stock |
| QBTS | D-Wave Quantum | Stock |
| SMCI | Super Micro Computer | Stock |

## Adding a New Asset

1. Add entry to `ASSETS` dict in `backend/main.py` with `symbol`, `name`, `type`, `ticker`, optional `fallback`.
2. Frontend sidebar groups by type automatically — no frontend changes needed.
3. Alternatively: use the "EIGENE ASSETS" + Lupen-Icon in the sidebar to add any Yahoo Finance ticker at runtime (stored in localStorage).

## Important Patterns & Gotchas

- **yfinance news format** changed in v0.2.50 — always use `_parse_news_item()`, never access `n['title']` directly
- **Chart destroy pattern**: always `if(inst) { inst.destroy(); inst=null; }` before creating a new Chart.js instance
- **Candlestick**: implemented via `type:'bar'` with `[bodyMin, bodyMax]` float data + `apexWicks` inline plugin. Do NOT add `chartjs-chart-financial` (incompatible with Chart.js 4.x)
- **BB fill**: use `fill: { target: '+1', above: color, below: color }` on the upper band dataset; `fill: false` on lower
- **Tech alerts**: only checked when `techCache[symbol]` is populated (happens on asset detail load or screener load)
- **News cache**: `cache_set("news_feed", result)` — if news seems stale, restart server to clear `_cache`
- **Multi-Timeframe valid combinations** (enforced in frontend):
  - `1h`: periods = `1d, 5d, 1mo`
  - `1d`: periods = `1mo, 3mo, 6mo, 1y, 2y`
  - `1wk`: periods = `1y, 2y, 5y`

## Key Dependencies

- **yfinance** — Yahoo Finance data (≥ 0.2.50, new news format)
- **curl_cffi** — Chrome-impersonating HTTP client to bypass rate limits
- **ta** — technical analysis indicators
- **pandas / numpy** — data manipulation
- **FastAPI + Uvicorn** — async web framework

## Language Note

UI is in German. Number formatting uses `de-DE` locale. Backend comments are in German.
