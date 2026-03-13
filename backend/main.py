"""
APEX TRADE – FastAPI Backend
yfinance >= 0.2.50 + curl_cffi (löst das "Expecting value" Problem)
"""

from fastapi import FastAPI, HTTPException
from fastapi.middleware.cors import CORSMiddleware
from fastapi.staticfiles import StaticFiles
from fastapi.responses import FileResponse
from concurrent.futures import ThreadPoolExecutor
import yfinance as yf
import pandas as pd
import numpy as np
import ta
from datetime import datetime
import time
import os

# ── Einfacher In-Memory-Cache ──────────────────────────────────────────────
_cache: dict = {}  # key → {"data": ..., "ts": float}
CACHE_TTL = 300    # 5 Minuten

def cache_get(key: str):
    entry = _cache.get(key)
    if entry and (time.time() - entry["ts"]) < CACHE_TTL:
        return entry["data"]
    return None

def cache_set(key: str, data):
    _cache[key] = {"data": data, "ts": time.time()}

# ── curl_cffi Session – löst Yahoo Finance Rate-Limit/Cookie Problem ──
try:
    from curl_cffi import requests as curl_requests
    YF_SESSION = curl_requests.Session(impersonate="chrome")
except ImportError:
    YF_SESSION = None

app = FastAPI(title="APEX TRADE API", version="2.0.0")

app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"],
    allow_methods=["*"],
    allow_headers=["*"],
)

FRONTEND_DIR = os.path.join(os.path.dirname(__file__), "..", "frontend")
app.mount("/static", StaticFiles(directory=FRONTEND_DIR), name="static")

# ─────────────────────────────────────────
# Asset-Konfiguration
# ─────────────────────────────────────────
ASSETS = {
    "XAU":  {"ticker": "GC=F",    "fallback": "GLD",     "name": "Gold",                 "type": "commodity", "unit": "$/oz"},
    "XAG":  {"ticker": "SI=F",    "fallback": "SLV",     "name": "Silber",               "type": "commodity", "unit": "$/oz"},
    "HG":   {"ticker": "HG=F",    "fallback": "COPX",    "name": "Kupfer",               "type": "commodity", "unit": "$/lb"},
    "BTC":  {"ticker": "BTC-USD", "fallback": "BTC-USD", "name": "Bitcoin",              "type": "crypto",    "unit": "$"},
    "ETH":  {"ticker": "ETH-USD", "fallback": "ETH-USD", "name": "Ethereum",             "type": "crypto",    "unit": "$"},
    "FCX":  {"ticker": "FCX",     "fallback": "FCX",     "name": "Freeport-McMoRan",     "type": "stock",     "unit": "$"},
    "AG":   {"ticker": "AG",      "fallback": "AG",      "name": "First Majestic Silver","type": "stock",     "unit": "$"},
    "NEM":  {"ticker": "NEM",     "fallback": "NEM",     "name": "Newmont Corp.",        "type": "stock",     "unit": "$"},
    "NVDA": {"ticker": "NVDA",    "fallback": "NVDA",    "name": "Nvidia",               "type": "stock",     "unit": "$"},
    "QBTS": {"ticker": "QBTS",    "fallback": "QBTS",    "name": "D-Wave Quantum",       "type": "stock",     "unit": "$"},
    "SMCI": {"ticker": "SMCI",    "fallback": "SMCI",    "name": "Super Micro Computer", "type": "stock",     "unit": "$"},
    "MSTR": {"ticker": "MSTR",    "fallback": "MSTR",    "name": "MicroStrategy",         "type": "stock",     "unit": "$"},
}

# ─────────────────────────────────────────
# Leveraged Trend Rider – Konfig & State
# ─────────────────────────────────────────
MSTR_STRATEGY = {
    "symbol":        "MSTR",
    "leverage":      5,         # Ziel-Hebel (Warnung: 5x = extreme Volatilität)
    "risk_pct":      0.01,      # 1% Kapitalrisiko pro Trade
    "min_crv":       2.0,       # Min. Chance-Risiko-Verhältnis
    "atr_sl_mult":   1.5,       # Stop-Loss = 1.5× ATR unter Einstieg
    "atr_tp_mult":   3.0,       # Take-Profit = 3.0× ATR über Einstieg
    "adx_min":       20,        # ADX-Filter: kein Trade unter 20
    "rsi_long_max":  65,        # Long-Bias: RSI darf nicht überkauft sein
    "rsi_short_min": 35,        # Short-Bias: RSI darf nicht überverkauft sein
    "daily_loss_limit": 0.05,   # 5% Tagesverlust → 24h Pause
    "weekly_loss_limit": 0.10,  # 10% Wochenverlust → Hebel → 2x
    "max_drawdown":  0.15,      # 15% Drawdown ab Peak → Handelsstopp
}

# In-Memory Session-State (reset bei Server-Neustart)
_ltr_state: dict = {
    "peak_equity":    None,   # Höchststand seit Laufzeit
    "daily_loss":     0.0,    # Tagesverlust (absolut)
    "weekly_loss":    0.0,    # Wochenverlust (absolut)
    "last_reset_day": None,   # date-string "YYYY-MM-DD" des letzten Tagesresets
    "last_reset_week":None,   # isoformat-Woche
    "halted_until":   None,   # datetime wenn Pause aktiv
}

# ─────────────────────────────────────────
# Hilfsfunktionen
# ─────────────────────────────────────────

def make_ticker(sym: str):
    """Erstellt yfinance Ticker mit curl_cffi Session falls verfügbar"""
    if YF_SESSION:
        return yf.Ticker(sym, session=YF_SESSION)
    return yf.Ticker(sym)


def safe_float(val, default=None):
    try:
        if val is None:
            return default
        f = float(val)
        return default if (f != f) else f
    except Exception:
        return default


def sl(series):
    """Pandas Series → Python list, NaN wird zu None"""
    return [safe_float(v) for v in series.tolist()]


def sl100(series):
    """Pandas Series (0–1) → Python list ×100, NaN wird zu None"""
    result = []
    for v in series.tolist():
        f = safe_float(v)
        result.append(round(f * 100, 2) if f is not None else None)
    return result


def fetch_df(symbol: str, period: str, interval: str = "1d") -> tuple:
    """
    Lädt historische Kursdaten mit Fallback-Logik.
    Versucht: primärer Ticker → Fallback-Ticker → period=max
    Unterstützt auch Custom-Ticker (nicht in ASSETS) und verschiedene Intervalle.
    """
    if symbol in ASSETS:
        config = ASSETS[symbol]
    else:
        config = {"ticker": symbol, "fallback": symbol}

    # Fallback-Period je nach Interval
    fallback = "3mo" if interval == "1d" else ("1mo" if interval == "1h" else "2y")
    yf_period = period if period else fallback
    min_bars  = 10

    tickers_to_try = list(dict.fromkeys([config["ticker"], config["fallback"]]))

    for ticker_sym in tickers_to_try:
        for p in [yf_period, fallback, "max"]:
            try:
                t  = make_ticker(ticker_sym)
                df = t.history(period=p, interval=interval)
                if not df.empty and len(df) >= min_bars:
                    return df if p == yf_period else df.tail(500), ticker_sym
            except Exception:
                continue

    raise HTTPException(
        status_code=503,
        detail=f"Keine Daten für {symbol} erreichbar. Prüfe deine Internetverbindung oder versuche es später erneut."
    )


def calculate_signal(rsi, macd_hist, price, ma50, ma200):
    score = 0
    if rsi is not None:
        if rsi < 30:   score += 3
        elif rsi < 40: score += 2
        elif rsi < 50: score += 1
        elif rsi > 75: score -= 3
        elif rsi > 65: score -= 2
        elif rsi > 55: score -= 1
    if macd_hist is not None:
        score += 2 if macd_hist > 0 else -2
    if ma50  and price: score += 1 if price > ma50  else -1
    if ma200 and price: score += 1 if price > ma200 else -1
    if score >= 4:    return "BUY"
    elif score <= -4: return "SELL"
    elif score >= 2:  return "WATCH"
    else:             return "HOLD"


def get_support_resistance(df, current_price):
    highs = df["High"].rolling(window=10).max().dropna()
    lows  = df["Low"].rolling(window=10).min().dropna()
    recent_highs = sorted(highs.tail(90).unique(), reverse=True)
    recent_lows  = sorted(lows.tail(90).unique())
    resistances  = [h for h in recent_highs if h > current_price * 1.005][:2]
    supports     = [l for l in recent_lows  if l < current_price * 0.995][:2]
    while len(resistances) < 2:
        resistances.append(current_price * (1.03 + len(resistances) * 0.03))
    while len(supports) < 2:
        supports.append(current_price * (0.97 - len(supports) * 0.03))
    return {
        "resistance1": round(resistances[0], 4),
        "resistance2": round(resistances[1], 4),
        "support1":    round(supports[0], 4),
        "support2":    round(supports[1], 4),
    }


def get_entry_zones(support1, support2, price):
    d = 2 if price > 5 else 4
    return {
        "entry1": f"{round(support1*0.998,d)}–{round(support1*1.005,d)}",
        "entry2": f"{round(support2*0.998,d)}–{round(support2*1.005,d)}",
        "stop":   str(round(support2 * 0.97, d)),
        "target": f"{round(support1*1.08,d)}–{round(support1*1.13,d)}",
    }


# ─────────────────────────────────────────
# Leveraged Trend Rider – Hilfsfunktionen
# ─────────────────────────────────────────

def _resample_4h(df_1h: pd.DataFrame) -> pd.DataFrame:
    """Resampled 1H-DataFrame auf 4H-OHLCV."""
    df = df_1h.copy()
    df.index = pd.to_datetime(df.index)
    if df.index.tz is not None:
        df.index = df.index.tz_convert("UTC")
    return df.resample("4h").agg({
        "Open":   "first",
        "High":   "max",
        "Low":    "min",
        "Close":  "last",
        "Volume": "sum",
    }).dropna(subset=["Close"])


def _detect_swing_levels(df: pd.DataFrame, n: int = 5) -> tuple:
    """
    Erkennt Swing-Highs und Swing-Lows über lokale Maxima/Minima.
    Gibt (swing_high, swing_low) als float zurück.
    """
    highs = df["High"]
    lows  = df["Low"]

    swing_high = None
    swing_low  = None

    for i in range(n, len(highs) - n):
        window_h = highs.iloc[i - n: i + n + 1]
        if highs.iloc[i] == window_h.max():
            swing_high = float(highs.iloc[i])
        window_l = lows.iloc[i - n: i + n + 1]
        if lows.iloc[i] == window_l.min():
            swing_low = float(lows.iloc[i])

    # Fallback auf Rolling-Window (Fenstergröße an verfügbare Bars anpassen)
    w = min(20, len(highs))
    if swing_high is None:
        swing_high = float(highs.rolling(w).max().dropna().iloc[-1])
    if swing_low is None:
        swing_low  = float(lows.rolling(w).min().dropna().iloc[-1])

    return swing_high, swing_low


def _check_capital_protection(cfg: dict, state: dict, capital: float) -> dict:
    """
    Prüft Kapitalschutz-Regeln:
    - Tagesverlust > 5%  → 24h Handelspause
    - Wochenverlust > 10% → Hebel reduzieren auf 2x
    - Drawdown > 15%      → Handelsstopp
    Gibt Status-Dict zurück.
    """
    from datetime import datetime, timedelta

    today = datetime.utcnow().date().isoformat()
    week  = datetime.utcnow().isocalendar()[:2]  # (year, week)

    # Tages/Wochen-Loss Reset
    if state["last_reset_day"] != today:
        state["daily_loss"]     = 0.0
        state["last_reset_day"] = today
    if state["last_reset_week"] != str(week):
        state["weekly_loss"]      = 0.0
        state["last_reset_week"]  = str(week)

    # Peak-Equity tracken
    if state["peak_equity"] is None or capital > state["peak_equity"]:
        state["peak_equity"] = capital

    drawdown = (state["peak_equity"] - capital) / state["peak_equity"] if state["peak_equity"] else 0.0

    status = {
        "halted":          False,
        "halt_reason":     None,
        "effective_leverage": cfg["leverage"],
        "drawdown_pct":    round(drawdown * 100, 2),
        "daily_loss_pct":  round(state["daily_loss"] / capital * 100, 2) if capital else 0.0,
        "weekly_loss_pct": round(state["weekly_loss"] / capital * 100, 2) if capital else 0.0,
    }

    # Handelspause aktiv?
    if state["halted_until"]:
        from datetime import datetime
        if datetime.utcnow() < state["halted_until"]:
            status["halted"]      = True
            status["halt_reason"] = f"24h-Pause aktiv bis {state['halted_until'].strftime('%H:%M UTC')}"
            return status
        else:
            state["halted_until"] = None

    if drawdown >= cfg["max_drawdown"]:
        status["halted"]      = True
        status["halt_reason"] = f"Drawdown {round(drawdown*100,1)}% ≥ {int(cfg['max_drawdown']*100)}% → Handelsstopp"
        return status

    if state["daily_loss"] / (capital or 1) >= cfg["daily_loss_limit"]:
        from datetime import datetime, timedelta
        state["halted_until"] = datetime.utcnow() + timedelta(hours=24)
        status["halted"]      = True
        status["halt_reason"] = f"Tagesverlust ≥ {int(cfg['daily_loss_limit']*100)}% → 24h Pause"
        return status

    if state["weekly_loss"] / (capital or 1) >= cfg["weekly_loss_limit"]:
        status["effective_leverage"] = 2
        status["halt_reason"]        = f"Wochenverlust ≥ {int(cfg['weekly_loss_limit']*100)}% → Hebel auf 2x reduziert"

    return status


def run_leveraged_trend_rider(capital: float) -> dict:
    """
    Leveraged Trend Rider – Analyse für MSTR.
    Signale: Long/Short/Neutral basierend auf Trend-Confluence (Daily + 4H).
    Gibt vollständige Signal-Analyse mit Position-Sizing zurück.
    WARNUNG: 5x Hebel = extreme Volatilität und hohes Verlustrisiko!
    """
    cfg = MSTR_STRATEGY
    sym = cfg["symbol"]

    # ── Tages-Daten ────────────────────────────────────────────────
    df_daily, _ = fetch_df(sym, "6mo", "1d")
    close_d = df_daily["Close"]
    high_d  = df_daily["High"]
    low_d   = df_daily["Low"]

    # Tages-Indikatoren
    rsi_d     = ta.momentum.RSIIndicator(close_d, window=14).rsi()
    macd_obj  = ta.trend.MACD(close_d)
    macd_hist_d = macd_obj.macd_diff()
    adx_obj   = ta.trend.ADXIndicator(high_d, low_d, close_d, window=14)
    adx_d     = adx_obj.adx()
    adx_pos_d = adx_obj.adx_pos()
    adx_neg_d = adx_obj.adx_neg()
    ema50_d   = ta.trend.EMAIndicator(close_d, window=50).ema_indicator()
    ema200_d  = ta.trend.EMAIndicator(close_d, window=200).ema_indicator()
    atr_d     = ta.volatility.AverageTrueRange(high_d, low_d, close_d, window=14).average_true_range()

    # Aktuelle Tages-Werte
    price      = safe_float(close_d.iloc[-1])
    rsi_val    = safe_float(rsi_d.iloc[-1])
    macd_hist  = safe_float(macd_hist_d.iloc[-1])
    adx_val    = safe_float(adx_d.iloc[-1])
    dip_val    = safe_float(adx_pos_d.iloc[-1])
    dim_val    = safe_float(adx_neg_d.iloc[-1])
    ema50      = safe_float(ema50_d.iloc[-1])
    ema200     = safe_float(ema200_d.iloc[-1])
    atr        = safe_float(atr_d.iloc[-1])

    # ── 4H-Daten (resampled aus 1H) ────────────────────────────────
    try:
        df_1h, _ = fetch_df(sym, "60d", "1h")
        df_4h    = _resample_4h(df_1h)
        close_4h = df_4h["Close"]
        high_4h  = df_4h["High"]
        low_4h   = df_4h["Low"]

        rsi_4h     = safe_float(ta.momentum.RSIIndicator(close_4h, window=14).rsi().iloc[-1])
        adx_4h_obj = ta.trend.ADXIndicator(high_4h, low_4h, close_4h, window=14)
        adx_4h     = safe_float(adx_4h_obj.adx().iloc[-1])
        macd_4h    = safe_float(ta.trend.MACD(close_4h).macd_diff().iloc[-1])
        ema21_4h   = safe_float(ta.trend.EMAIndicator(close_4h, window=21).ema_indicator().iloc[-1])

        swing_high_4h, swing_low_4h = _detect_swing_levels(df_4h, n=3)
        has_4h = True
    except Exception:
        rsi_4h = adx_4h = macd_4h = ema21_4h = None
        swing_high_4h = swing_low_4h = None
        has_4h = False

    # ── Swing-Levels (Daily) ───────────────────────────────────────
    swing_high_d, swing_low_d = _detect_swing_levels(df_daily, n=5)

    # ── Kapitalschutz prüfen ───────────────────────────────────────
    protection = _check_capital_protection(cfg, _ltr_state, capital)

    # ── Signal-Logik ──────────────────────────────────────────────
    # Trend-Bias (Daily)
    above_ema50  = price > ema50  if (price and ema50)  else False
    above_ema200 = price > ema200 if (price and ema200) else False
    bull_trend   = above_ema50 and above_ema200
    bear_trend   = not above_ema50 and not above_ema200

    # ADX-Filter (mindestens einer von beiden)
    adx_ok_d  = adx_val >= cfg["adx_min"] if adx_val else False
    adx_ok_4h = adx_4h  >= cfg["adx_min"] if adx_4h  else False
    adx_ok    = adx_ok_d and (adx_ok_4h or not has_4h)

    # Momentum-Confluence
    macd_bull = macd_hist > 0  if macd_hist  else False
    macd_bear = macd_hist < 0  if macd_hist  else False
    rsi_long  = rsi_val < cfg["rsi_long_max"]  if rsi_val else False
    rsi_short = rsi_val > cfg["rsi_short_min"] if rsi_val else False

    # +DI/-DI Richtung
    di_bull = dip_val > dim_val if (dip_val and dim_val) else False
    di_bear = dim_val > dip_val if (dip_val and dim_val) else False

    # 4H-Bestätigung
    conf_4h_bull = (macd_4h > 0 if macd_4h else False) and (price > ema21_4h if (price and ema21_4h) else False)
    conf_4h_bear = (macd_4h < 0 if macd_4h else False) and (price < ema21_4h if (price and ema21_4h) else False)

    # Signal bestimmen
    long_score  = sum([bull_trend, macd_bull, di_bull, rsi_long,  conf_4h_bull])
    short_score = sum([bear_trend, macd_bear, di_bear, rsi_short, conf_4h_bear])

    rejection_reasons = []

    if protection["halted"]:
        signal    = "NEUTRAL"
        direction = None
        rejection_reasons.append(protection["halt_reason"])
    elif not adx_ok:
        signal    = "NEUTRAL"
        direction = None
        rejection_reasons.append(f"ADX {round(adx_val or 0,1)} < {cfg['adx_min']} → kein Trend (Seitwärtsmarkt)")
    elif long_score >= 3:
        signal    = "LONG"
        direction = "long"
    elif short_score >= 3:
        signal    = "SHORT"
        direction = "short"
    else:
        signal    = "NEUTRAL"
        direction = None
        rejection_reasons.append(f"Keine klare Trend-Confluence (Long {long_score}/5, Short {short_score}/5)")

    # ── Position-Sizing & Stops ────────────────────────────────────
    position_info = {}
    if signal in ("LONG", "SHORT") and atr and price:
        eff_lev = protection["effective_leverage"]
        atr_sl  = cfg["atr_sl_mult"] * atr
        atr_tp  = cfg["atr_tp_mult"] * atr

        if direction == "long":
            sl_price = round(price - atr_sl, 2)
            tp_price = round(price + atr_tp, 2)
        else:
            sl_price = round(price + atr_sl, 2)
            tp_price = round(price - atr_tp, 2)

        risk_per_share = abs(price - sl_price)
        risk_capital   = capital * cfg["risk_pct"]
        shares         = int(risk_capital / risk_per_share) if risk_per_share > 0 else 0
        position_value = shares * price
        actual_leverage = round(position_value / capital, 2) if capital else 0
        crv             = round(atr_tp / atr_sl, 2)

        if crv < cfg["min_crv"]:
            signal    = "NEUTRAL"
            direction = None
            rejection_reasons.append(f"CRV {crv} < {cfg['min_crv']} → Trade abgelehnt")
            shares = position_value = actual_leverage = 0

        position_info = {
            "entry_price":      round(price, 2),
            "stop_loss":        sl_price,
            "take_profit":      tp_price,
            "atr":              round(atr, 2),
            "shares":           shares,
            "position_value":   round(position_value, 2),
            "risk_capital":     round(risk_capital, 2),
            "risk_pct":         round(cfg["risk_pct"] * 100, 1),
            "crv":              crv,
            "effective_leverage": eff_lev,
            "swing_high":       round(swing_high_4h or swing_high_d, 2),
            "swing_low":        round(swing_low_4h  or swing_low_d,  2),
        }

    # ── Indikatoren-Zusammenfassung ───────────────────────────────
    indicators = {
        "price":      round(price, 2) if price else None,
        "rsi_daily":  round(rsi_val, 1) if rsi_val else None,
        "rsi_4h":     round(rsi_4h, 1) if rsi_4h else None,
        "adx_daily":  round(adx_val, 1) if adx_val else None,
        "adx_4h":     round(adx_4h, 1) if adx_4h else None,
        "di_plus":    round(dip_val, 1) if dip_val else None,
        "di_minus":   round(dim_val, 1) if dim_val else None,
        "macd_hist":  round(macd_hist, 4) if macd_hist else None,
        "ema50":      round(ema50, 2) if ema50 else None,
        "ema200":     round(ema200, 2) if ema200 else None,
        "above_ema50":  above_ema50,
        "above_ema200": above_ema200,
        "long_score":   long_score,
        "short_score":  short_score,
    }

    return {
        "symbol":             sym,
        "signal":             signal,
        "direction":          direction,
        "rejection_reasons":  rejection_reasons,
        "position":           position_info,
        "indicators":         indicators,
        "protection":         protection,
        "config":             {
            "leverage":      cfg["leverage"],
            "risk_pct":      cfg["risk_pct"] * 100,
            "min_crv":       cfg["min_crv"],
            "atr_sl_mult":   cfg["atr_sl_mult"],
            "atr_tp_mult":   cfg["atr_tp_mult"],
        },
        "warnings": [
            "⚠️  5x Hebel bedeutet: 20% Kursrückgang = 100% Verlust der Margin",
            "⚠️  MSTR ist an Bitcoin gebunden — extreme Volatilität möglich",
            "⚠️  Nie mehr riskieren als du dir leisten kannst zu verlieren",
        ],
        "timestamp": datetime.utcnow().isoformat() + "Z",
    }


def run_backtest(symbol: str, period: str, strategy: str, capital: float) -> dict:
    """Simuliert eine Handelsstrategie auf historischen Kursdaten."""
    df, _ = fetch_df(symbol, period)
    close = df["Close"]
    rsi_series  = ta.momentum.RSIIndicator(close, window=14).rsi()
    macd_hist_s = ta.trend.MACD(close).macd_diff()

    dates     = [str(d.date()) for d in df.index]
    prices    = close.tolist()
    rsi_vals  = rsi_series.tolist()
    hist_vals = macd_hist_s.tolist()
    n         = len(prices)
    WARMUP    = 33  # RSI ~14 + MACD ~26 Bars Anlaufzeit

    current_capital = capital
    in_position     = False
    entry_price     = 0.0
    entry_date      = ""
    entry_capital   = 0.0
    trades          = []
    equity_curve    = []
    bh_curve        = []

    bh_entry  = safe_float(prices[WARMUP] if n > WARMUP else prices[0], 1.0)
    bh_shares = capital / bh_entry if bh_entry > 0 else 0.0

    for i in range(n):
        price     = safe_float(prices[i], 0.0)
        rsi       = safe_float(rsi_vals[i])
        hist      = safe_float(hist_vals[i])
        hist_prev = safe_float(hist_vals[i - 1]) if i > 0 else None

        # Mark-to-market Portfoliowert
        if in_position and entry_price > 0:
            portfolio_value = entry_capital / entry_price * price
        else:
            portfolio_value = current_capital
        equity_curve.append(round(portfolio_value, 2))
        bh_curve.append(round(bh_shares * price, 2))

        if i < WARMUP:
            continue

        # ── VERKAUFEN ──────────────────────────────────────────────────────
        if in_position:
            stop_hit    = price > 0 and price <= entry_price * 0.95
            exit_reason = None

            if strategy == "rsi":
                if rsi is not None and rsi > 70:     exit_reason = "RSI_OVERBOUGHT"
                elif stop_hit:                       exit_reason = "STOP_LOSS"
            elif strategy == "macd":
                if (hist_prev is not None and hist is not None
                        and hist_prev > 0 and hist < 0): exit_reason = "MACD_CROSS_NEG"
                elif stop_hit:                       exit_reason = "STOP_LOSS"
            elif strategy == "combined":
                if rsi is not None and rsi > 70:     exit_reason = "RSI_OVERBOUGHT"
                elif stop_hit:                       exit_reason = "STOP_LOSS"

            if exit_reason:
                shares     = entry_capital / entry_price
                exit_cap   = shares * price
                profit_abs = exit_cap - entry_capital
                profit_pct = (price - entry_price) / entry_price * 100
                trades.append({
                    "entry_date":  entry_date,
                    "exit_date":   dates[i],
                    "entry_price": round(entry_price, 4),
                    "exit_price":  round(price, 4),
                    "profit_pct":  round(profit_pct, 2),
                    "profit_abs":  round(profit_abs, 2),
                    "result":      "WIN" if profit_abs > 0 else "LOSS",
                    "exit_reason": exit_reason,
                })
                current_capital = exit_cap
                in_position     = False

        # ── KAUFEN ─────────────────────────────────────────────────────────
        if not in_position and price > 0:
            buy = False
            if strategy == "rsi":
                buy = rsi is not None and rsi < 35
            elif strategy == "macd":
                buy = (hist_prev is not None and hist is not None
                       and hist_prev < 0 and hist > 0)
            elif strategy == "combined":
                buy = (rsi is not None and rsi < 35
                       and hist is not None and hist > 0)

            if buy:
                in_position   = True
                entry_price   = price
                entry_date    = dates[i]
                entry_capital = current_capital

    # Offene Position – Mark-to-market zum letzten Kurs (kein Trade-Eintrag)
    last_price    = safe_float(prices[-1] if prices else entry_price, entry_price)
    final_capital = (entry_capital / entry_price * last_price
                     if in_position and entry_price > 0 else current_capital)

    # ── METRIKEN ───────────────────────────────────────────────────────────
    total_return = (final_capital - capital) / capital * 100
    bh_final     = bh_shares * safe_float(prices[-1] if prices else bh_entry, bh_entry)
    bh_return    = (bh_final - capital) / capital * 100

    wins   = [t for t in trades if t["result"] == "WIN"]
    losses = [t for t in trades if t["result"] == "LOSS"]
    win_rate = len(wins) / len(trades) * 100 if trades else 0.0

    g_profit = sum(t["profit_abs"] for t in wins)
    g_loss   = abs(sum(t["profit_abs"] for t in losses))
    if g_loss > 0:      pf = round(g_profit / g_loss, 2)
    elif g_profit > 0:  pf = 999.0
    else:               pf = 0.0

    # Max Drawdown
    max_dd = 0.0
    peak   = equity_curve[0] if equity_curve else capital
    for v in equity_curve:
        if v > peak: peak = v
        dd = (peak - v) / peak * 100 if peak > 0 else 0.0
        if dd > max_dd: max_dd = dd

    # Sharpe Ratio (annualisiert, tägliche Renditen, risikofreier Zins = 0)
    sharpe = 0.0
    if len(equity_curve) > 1:
        eq  = np.array(equity_curve, dtype=float)
        ret = np.diff(eq) / np.where(eq[:-1] > 0, eq[:-1], 1.0)
        std = ret.std()
        if std > 0: sharpe = round(ret.mean() / std * np.sqrt(252), 2)

    return {
        "symbol":          symbol,
        "strategy":        strategy,
        "period":          period,
        "initial_capital": round(capital, 2),
        "final_capital":   round(final_capital, 2),
        "total_return":    round(total_return, 2),
        "buy_hold_return": round(bh_return, 2),
        "buy_hold_final":  round(bh_final, 2),
        "total_trades":    len(trades),
        "win_trades":      len(wins),
        "loss_trades":     len(losses),
        "win_rate":        round(win_rate, 1),
        "profit_factor":   pf,
        "max_drawdown":    round(max_dd, 2),
        "sharpe_ratio":    sharpe,
        "equity_curve":    {"labels": dates, "values": equity_curve},
        "bh_curve":        {"labels": dates, "values": bh_curve},
        "trades":          trades,
    }


# ─────────────────────────────────────────
# Endpoints
# ─────────────────────────────────────────

@app.get("/")
def serve_frontend():
    return FileResponse(os.path.join(FRONTEND_DIR, "index.html"))


@app.get("/api/health")
def health():
    return {
        "status": "ok",
        "time": datetime.now().isoformat(),
        "curl_cffi": YF_SESSION is not None,
    }


@app.get("/api/assets")
def get_all_assets():
    def fetch_one(item):
        symbol, config = item
        price = change = change_amt = None
        for ticker_sym in list(dict.fromkeys([config["ticker"], config["fallback"]])):
            try:
                info  = make_ticker(ticker_sym).fast_info
                price = safe_float(info.last_price)
                prev  = safe_float(info.previous_close)
                if price and prev and prev != 0:
                    change     = round((price - prev) / prev * 100, 2)
                    change_amt = round(price - prev, 4)
                else:
                    change = change_amt = 0.0
                break
            except Exception:
                continue
        return {
            "symbol":    symbol,
            "name":      config["name"],
            "type":      config["type"],
            "unit":      config["unit"],
            "price":     round(price, 4) if price else None,
            "change":    change or 0.0,
            "changeAmt": change_amt or 0.0,
        }

    with ThreadPoolExecutor(max_workers=8) as ex:
        futures = {ex.submit(fetch_one, item): item for item in ASSETS.items()}
        result  = []
        for fut in futures:
            try:
                result.append(fut.result(timeout=10))
            except Exception:
                symbol, config = futures[fut]
                result.append({"symbol": symbol, "name": config["name"],
                                "type": config["type"], "unit": config["unit"],
                                "price": None, "change": 0.0, "changeAmt": 0.0})
    # Originalreihenfolge beibehalten
    order = {s: i for i, s in enumerate(ASSETS)}
    return sorted(result, key=lambda x: order.get(x["symbol"], 99))


@app.get("/api/asset/{symbol}")
def get_asset_detail(symbol: str, period: str = "3mo", interval: str = "1d"):
    symbol = symbol.upper()
    if symbol in ASSETS:
        config = ASSETS[symbol]
    else:
        # Custom-Ticker: direkt als Yahoo-Finance-Symbol behandeln
        config = {"ticker": symbol, "fallback": symbol, "name": symbol, "type": "stock", "unit": "$"}
        try:
            info = make_ticker(symbol).info
            config["name"] = info.get("shortName") or info.get("longName") or symbol
            qt = info.get("quoteType", "").lower()
            if qt == "cryptocurrency":
                config["type"] = "crypto"
        except Exception:
            pass

    df, used_ticker = fetch_df(symbol, period, interval)

    close  = df["Close"]
    high   = df["High"]
    low    = df["Low"]
    volume = df["Volume"]

    price      = safe_float(close.iloc[-1])
    prev       = safe_float(close.iloc[-2])
    change     = round((price - prev) / prev * 100, 2) if price and prev else 0.0
    change_amt = round(price - prev, 4)                if price and prev else 0.0

    # ── Indikatoren (Serien + Skalare) ────────────────
    n = len(close)

    rsi_s      = ta.momentum.RSIIndicator(close, window=14).rsi()
    rsi        = safe_float(rsi_s.iloc[-1], 50.0)

    macd_obj    = ta.trend.MACD(close)
    macd_line_s = macd_obj.macd()
    macd_sig_s  = macd_obj.macd_signal()
    macd_hist_s = macd_obj.macd_diff()
    macd_val    = safe_float(macd_line_s.iloc[-1])
    macd_sig    = safe_float(macd_sig_s.iloc[-1])
    macd_hist   = safe_float(macd_hist_s.iloc[-1])
    macd_label  = "Bullish" if macd_hist and macd_hist > 0 else \
                  "Bearish" if macd_hist and macd_hist < 0 else "Neutral"

    bb          = ta.volatility.BollingerBands(close, window=20, window_dev=2)
    bb_upper_s  = bb.bollinger_hband()
    bb_lower_s  = bb.bollinger_lband()
    bb_upper    = safe_float(bb_upper_s.iloc[-1])
    bb_lower    = safe_float(bb_lower_s.iloc[-1])
    bb_pct      = round(((price - bb_lower) / (bb_upper - bb_lower)) * 100, 1) \
                  if bb_upper and bb_lower and (bb_upper - bb_lower) != 0 else None

    stoch       = ta.momentum.StochRSIIndicator(close, window=14, smooth1=3, smooth2=3)
    stoch_s     = stoch.stochrsi()
    stoch_val   = safe_float(stoch_s.iloc[-1])
    stoch_pct   = round(stoch_val * 100, 1) if stoch_val is not None else None

    atr = safe_float(
        ta.volatility.AverageTrueRange(high, low, close, window=14)
        .average_true_range().iloc[-1]
    )

    ma20_s  = close.rolling(min(20,  n)).mean()
    ma50_s  = close.rolling(min(50,  n)).mean()
    ma200_s = close.rolling(min(200, n)).mean()
    ma20    = safe_float(ma20_s.iloc[-1])
    ma50    = safe_float(ma50_s.iloc[-1])
    ma200   = safe_float(ma200_s.iloc[-1])

    obv_s     = ta.volume.OnBalanceVolumeIndicator(close, volume).on_balance_volume()
    obv_trend = "Positiv" if safe_float(obv_s.iloc[-1], 0) > \
                             safe_float(obv_s.iloc[-20], 0) else "Negativ"

    # ── Erweiterte Indikatoren ─────────────────────────
    ema9_s  = ta.trend.EMAIndicator(close, window=9).ema_indicator()
    ema21_s = ta.trend.EMAIndicator(close, window=21).ema_indicator()

    try:
        psar_obj   = ta.trend.PSARIndicator(high, low, close)
        psar_bull_s = psar_obj.psarbull()   # Wert wenn bullish, sonst NaN
        psar_bear_s = psar_obj.psarbear()   # Wert wenn bearish, sonst NaN
    except Exception:
        psar_bull_s = psar_bear_s = close * float("nan")

    adx_obj = ta.trend.ADXIndicator(high, low, close, window=14)
    adx_s     = adx_obj.adx()
    adx_pos_s = adx_obj.adx_pos()   # +DI
    adx_neg_s = adx_obj.adx_neg()   # -DI

    cci_s   = ta.trend.CCIIndicator(high, low, close, window=20).cci()

    willr_s = ta.momentum.WilliamsRIndicator(high, low, close, lbp=14).williams_r()

    try:
        ich     = ta.trend.IchimokuIndicator(high, low, window1=9, window2=26, window3=52)
        ich_tenkan_s  = ich.ichimoku_conversion_line()
        ich_kijun_s   = ich.ichimoku_base_line()
        ich_spanA_s   = ich.ichimoku_a()
        ich_spanB_s   = ich.ichimoku_b()
    except Exception:
        ich_tenkan_s = ich_kijun_s = ich_spanA_s = ich_spanB_s = close * float("nan")

    signal = calculate_signal(rsi, macd_hist, price, ma50, ma200)
    levels = get_support_resistance(df, price)
    zones  = get_entry_zones(levels["support1"], levels["support2"], price)

    chart_data = {
        "labels":    [str(d.date()) if interval == "1d" else str(d) for d in df.index],
        "prices":    [round(float(p), 4) for p in close.tolist()],
        "opens":     [round(float(p), 4) for p in df["Open"].tolist()],
        "highs":     [round(float(p), 4) for p in df["High"].tolist()],
        "lows":      [round(float(p), 4) for p in df["Low"].tolist()],
        "volumes":   [int(v) for v in volume.tolist()],
        "ma20":      sl(ma20_s),
        "ma50":      sl(ma50_s),
        "ma200":     sl(ma200_s),
        "bbUpper":   sl(bb_upper_s),
        "bbLower":   sl(bb_lower_s),
        "ema9":      sl(ema9_s),
        "ema21":     sl(ema21_s),
        "psarBull":  sl(psar_bull_s),
        "psarBear":  sl(psar_bear_s),
        "ichTenkan": sl(ich_tenkan_s),
        "ichKijun":  sl(ich_kijun_s),
        "ichSpanA":  sl(ich_spanA_s),
        "ichSpanB":  sl(ich_spanB_s),
        "rsi":       sl(rsi_s),
        "macdLine":  sl(macd_line_s),
        "macdSig":   sl(macd_sig_s),
        "macdHist":  sl(macd_hist_s),
        "stochRsi":  sl100(stoch_s),
        "adx":       sl(adx_s),
        "adxPos":    sl(adx_pos_s),
        "adxNeg":    sl(adx_neg_s),
        "cci":       sl(cci_s),
        "willR":     sl(willr_s),
    }

    # ── Fundamentaldaten (Aktien) ─────────────────────
    fundamentals = {}
    if config["type"] == "stock":
        try:
            info = make_ticker(used_ticker).info
            fundamentals = {
                "marketCap":        info.get("marketCap"),
                "trailingPE":       info.get("trailingPE"),
                "forwardPE":        info.get("forwardPE"),
                "eps":              info.get("trailingEps"),
                "revenue":          info.get("totalRevenue"),
                "grossMargin":      info.get("grossMargins"),
                "operatingMargin":  info.get("operatingMargins"),
                "debtToEquity":     info.get("debtToEquity"),
                "dividendYield":    info.get("dividendYield"),
                "52wHigh":          info.get("fiftyTwoWeekHigh"),
                "52wLow":           info.get("fiftyTwoWeekLow"),
                "analystTarget":    info.get("targetMeanPrice"),
                "analystLow":       info.get("targetLowPrice"),
                "analystHigh":      info.get("targetHighPrice"),
                "recommendation":   info.get("recommendationKey"),
                "numberOfAnalysts": info.get("numberOfAnalystOpinions"),
                "sector":           info.get("sector"),
                "industry":         info.get("industry"),
                "description":      (info.get("longBusinessSummary") or "")[:300],
            }
        except Exception:
            pass

    return {
        "symbol":     symbol,
        "name":       config["name"],
        "type":       config["type"],
        "unit":       config["unit"],
        "price":      round(price, 4),
        "change":     change,
        "changeAmt":  change_amt,
        "signal":     signal,
        "dataSource": used_ticker,
        "indicators": {
            "rsi":        round(rsi, 1)       if rsi        else None,
            "rsiText":    "Überverkauft"      if rsi and rsi < 35 else
                          "Überkauft"         if rsi and rsi > 70 else "Neutral",
            "macd":       round(macd_val,  4) if macd_val   else None,
            "macdSignal": round(macd_sig,  4) if macd_sig   else None,
            "macdHist":   round(macd_hist, 4) if macd_hist  else None,
            "macdLabel":  macd_label,
            "bbPercent":  bb_pct,
            "bbUpper":    round(bb_upper,  4) if bb_upper   else None,
            "bbLower":    round(bb_lower,  4) if bb_lower   else None,
            "stochRsi":   stoch_pct,
            "atr":        round(atr, 4)       if atr        else None,
            "ma20":       round(ma20,  4)     if ma20       else None,
            "ma50":       round(ma50,  4)     if ma50       else None,
            "ma200":      round(ma200, 4)     if ma200      else None,
            "obvTrend":   obv_trend,
        },
        "levels":       {**levels, **zones},
        "chart":        chart_data,
        "fundamentals": fundamentals,
        "timestamp":    datetime.now().isoformat(),
    }


def _parse_news_item(n: dict) -> dict | None:
    """Konvertiert yfinance News-Objekt (alt + neu) in einheitliches Dict."""
    try:
        if "content" in n:
            # Neues Format (yfinance >= 0.2.50)
            c = n["content"]
            title = c.get("title", "")
            link  = (c.get("canonicalUrl") or c.get("clickThroughUrl") or {}).get("url", "")
            pub   = (c.get("provider") or {}).get("displayName", "")
            raw_date = c.get("pubDate", "")
            try:
                ts = int(datetime.fromisoformat(raw_date.replace("Z", "+00:00")).timestamp())
            except Exception:
                ts = 0
        else:
            # Altes Format
            title = n.get("title", "")
            link  = n.get("link", "")
            pub   = n.get("publisher", "")
            ts    = n.get("providerPublishTime", 0)
        if not title:
            return None
        return {"title": title, "publisher": pub, "link": link, "time": ts}
    except Exception:
        return None


@app.get("/api/news/feed")
def get_news_feed():
    """Aggregierter News-Feed aller Assets (gecacht, 5 Min TTL, Timeout pro Ticker)."""
    cached = cache_get("news_feed")
    if cached is not None:
        return cached

    # Nur die eigenen Assets – weniger Requests = schneller
    MACRO = {
        "SPX": {"ticker": "^GSPC", "name": "S&P 500",  "type": "index"},
        "OIL": {"ticker": "CL=F",  "name": "Rohöl",    "type": "commodity"},
    }
    all_cfgs = {**ASSETS, **MACRO}

    def fetch_news(sym_cfg):
        sym, cfg = sym_cfg
        try:
            raw = make_ticker(cfg["ticker"]).news or []
            parsed = [_parse_news_item(n) for n in raw[:4]]
            return [(sym, cfg.get("name", sym), cfg.get("type", "stock"), p)
                    for p in parsed if p]
        except Exception:
            return []

    seen, feed = set(), []
    with ThreadPoolExecutor(max_workers=8) as ex:
        futures = {ex.submit(fetch_news, item): item for item in all_cfgs.items()}
        for fut in futures:
            try:
                items = fut.result(timeout=12)
            except Exception:
                items = []
            for sym, name, asset_type, item in items:
                title = item["title"]
                if title in seen:
                    continue
                seen.add(title)
                feed.append({**item, "symbol": sym, "name": name, "assetType": asset_type})

    feed.sort(key=lambda x: x["time"], reverse=True)
    result = feed[:60]
    cache_set("news_feed", result)
    return result


@app.get("/api/news/{symbol}")
def get_news(symbol: str):
    symbol = symbol.upper()
    if symbol not in ASSETS:
        raise HTTPException(status_code=404, detail="Symbol nicht bekannt")
    try:
        raw = make_ticker(ASSETS[symbol]["ticker"]).news or []
        return [r for n in raw[:8] if (r := _parse_news_item(n))]
    except Exception:
        return []


@app.get("/api/screen")
def get_screen(symbols: str = ""):
    """Schnell-Analyse aller Assets für den Screener (parallel via ThreadPoolExecutor)."""
    all_syms = list(ASSETS.keys())
    if symbols:
        extra = [s.strip().upper() for s in symbols.split(",") if s.strip()]
        all_syms.extend([s for s in extra if s not in all_syms])

    def analyze(sym):
        try:
            cfg   = ASSETS.get(sym, {"ticker": sym, "fallback": sym, "name": sym, "type": "stock"})
            df, _ = fetch_df(sym, "3mo")
            close = df["Close"]
            n     = len(close)
            price = safe_float(close.iloc[-1])
            prev  = safe_float(close.iloc[-2])
            chg   = round((price - prev) / prev * 100, 2) if price and prev else 0.0
            rsi   = safe_float(ta.momentum.RSIIndicator(close, window=14).rsi().iloc[-1], 50.0)
            mhist = safe_float(ta.trend.MACD(close).macd_diff().iloc[-1])
            ma50  = safe_float(close.rolling(min(50,  n)).mean().iloc[-1])
            ma200 = safe_float(close.rolling(min(200, n)).mean().iloc[-1])
            atr   = safe_float(ta.volatility.AverageTrueRange(df["High"], df["Low"], close, window=14).average_true_range().iloc[-1])
            atr_pct = round(atr / price * 100, 2) if atr and price else None
            return {
                "symbol":    sym,
                "name":      cfg.get("name", sym),
                "type":      cfg.get("type", "stock"),
                "price":     round(price, 4) if price else None,
                "change":    chg,
                "signal":    calculate_signal(rsi, mhist, price, ma50, ma200),
                "rsi":       round(rsi, 1) if rsi is not None else None,
                "macdHist":  round(mhist, 4) if mhist is not None else None,
                "aboveMa50": bool(price and ma50  and price > ma50),
                "aboveMa200":bool(price and ma200 and price > ma200),
                "atrPct":    atr_pct,
            }
        except Exception:
            return None

    with ThreadPoolExecutor(max_workers=4) as ex:
        results = list(filter(None, ex.map(analyze, all_syms)))

    order = {"BUY": 0, "WATCH": 1, "HOLD": 2, "SELL": 3}
    return sorted(results, key=lambda x: order.get(x.get("signal", "HOLD"), 2))


@app.get("/api/correlations")
def get_correlations():
    """Pearson-Korrelation der Tagesrenditen (3 Monate) für alle Assets."""
    symbols = list(ASSETS.keys())

    def fetch_returns(sym):
        try:
            df, _ = fetch_df(sym, "3mo")
            if df is None or len(df) < 20:
                return sym, None
            ret = df["Close"].pct_change().dropna()
            # Timezonen angleichen (BTC=UTC, Aktien=NY) → auf reines Datum normalisieren
            ret.index = pd.to_datetime(ret.index.date)
            return sym, ret
        except Exception:
            return sym, None

    with ThreadPoolExecutor(max_workers=4) as ex:
        raw = dict(ex.map(fetch_returns, symbols))

    valid = {k: v for k, v in raw.items() if v is not None}
    df_ret = pd.DataFrame(valid).dropna()
    if df_ret.empty or len(df_ret.columns) < 2:
        return {"symbols": list(valid.keys()), "matrix": []}
    corr   = df_ret.corr().round(2)
    syms   = list(corr.columns)
    matrix = [[float(corr.loc[r, c]) for c in syms] for r in syms]
    return {"symbols": syms, "matrix": matrix}


@app.get("/api/search/{ticker}")
def search_ticker(ticker: str):
    """Validiert einen beliebigen Yahoo Finance Ticker und gibt Basisinfos zurück."""
    ticker = ticker.upper().strip()
    try:
        t     = make_ticker(ticker)
        fast  = t.fast_info
        price = safe_float(fast.last_price)
        if not price:
            raise HTTPException(status_code=404, detail=f"Kein Preis für '{ticker}' gefunden. Prüfe den Ticker.")
        try:
            info = t.info
            name = info.get("shortName") or info.get("longName") or ticker
            qt   = info.get("quoteType", "").lower()
        except Exception:
            name, qt = ticker, ""
        asset_type = "crypto" if qt == "cryptocurrency" else "stock"
        return {"ticker": ticker, "name": name, "type": asset_type, "price": round(price, 4)}
    except HTTPException:
        raise
    except Exception:
        raise HTTPException(status_code=404, detail=f"'{ticker}' nicht auf Yahoo Finance gefunden.")


@app.get("/api/quote/{ticker}")
def get_quote(ticker: str):
    """Leichtgewichtiger Preis-Abruf für einen beliebigen Ticker (für Custom Assets)."""
    ticker = ticker.upper().strip()
    try:
        info       = make_ticker(ticker).fast_info
        price      = safe_float(info.last_price)
        prev       = safe_float(info.previous_close)
        change     = round((price - prev) / prev * 100, 2) if price and prev and prev != 0 else 0.0
        change_amt = round(price - prev, 4)               if price and prev else 0.0
        return {"ticker": ticker, "price": round(price, 4) if price else None, "change": change, "changeAmt": change_amt}
    except Exception:
        raise HTTPException(status_code=404, detail=f"Ticker '{ticker}' nicht gefunden.")


@app.get("/api/backtest/{symbol}")
def get_backtest(
    symbol:   str,
    period:   str   = "1y",
    strategy: str   = "combined",
    capital:  float = 10000.0,
):
    symbol = symbol.upper()
    if symbol not in ASSETS:
        raise HTTPException(status_code=404, detail=f"Symbol '{symbol}' nicht bekannt")
    if period not in {"6mo", "1y", "2y"}:
        raise HTTPException(status_code=400, detail="Periode: 6mo | 1y | 2y")
    if strategy not in {"rsi", "macd", "combined"}:
        raise HTTPException(status_code=400, detail="Strategie: rsi | macd | combined")
    if capital <= 0 or capital > 1_000_000_000:
        raise HTTPException(status_code=400, detail="Ungültiges Startkapital")
    return run_backtest(symbol, period, strategy, capital)


@app.get("/api/report/{symbol}")
def get_report(symbol: str):
    """Dual-Intelligence Report: Technische Analyse (I) + Quant Research Memo (II)."""
    symbol = symbol.upper()
    if symbol in ASSETS:
        config = ASSETS[symbol]
    else:
        config = {"ticker": symbol, "fallback": symbol, "name": symbol, "type": "stock", "unit": "$"}
        try:
            info = make_ticker(symbol).info
            config["name"] = info.get("shortName") or info.get("longName") or symbol
        except Exception:
            pass

    # ── Kurze Daten (3 Monate) für technische Indikatoren ──
    df, used_ticker = fetch_df(symbol, "3mo")
    close  = df["Close"]
    high   = df["High"]
    low    = df["Low"]
    volume = df["Volume"]
    n      = len(close)
    price  = safe_float(close.iloc[-1])

    rsi_s   = ta.momentum.RSIIndicator(close, window=14).rsi()
    rsi     = safe_float(rsi_s.iloc[-1], 50.0)
    macd_obj = ta.trend.MACD(close)
    macd_hist = safe_float(macd_obj.macd_diff().iloc[-1])
    bb       = ta.volatility.BollingerBands(close, window=20, window_dev=2)
    bb_upper = safe_float(bb.bollinger_hband().iloc[-1])
    bb_lower = safe_float(bb.bollinger_lband().iloc[-1])
    stoch    = ta.momentum.StochRSIIndicator(close, window=14, smooth1=3, smooth2=3)
    stoch_val = safe_float(stoch.stochrsi().iloc[-1])
    atr      = safe_float(ta.volatility.AverageTrueRange(high, low, close, window=14).average_true_range().iloc[-1])
    ma20     = safe_float(close.rolling(min(20,  n)).mean().iloc[-1])
    ma50     = safe_float(close.rolling(min(50,  n)).mean().iloc[-1])
    ma200    = safe_float(close.rolling(min(200, n)).mean().iloc[-1])
    adx_obj  = ta.trend.ADXIndicator(high, low, close, window=14)
    adx      = safe_float(adx_obj.adx().iloc[-1])
    obv_s    = ta.volume.OnBalanceVolumeIndicator(close, volume).on_balance_volume()
    obv_trend = "positiv" if safe_float(obv_s.iloc[-1], 0) > safe_float(obv_s.iloc[-20], 0) else "negativ"

    # ── Bullish / Bearish Signal-Zählung ──
    bull_signals: list[str] = []
    bear_signals: list[str] = []

    if rsi is not None:
        if rsi < 35:   bull_signals.append(f"RSI überverkauft ({round(rsi,1)}) — Reversal-Potenzial")
        elif rsi > 70: bear_signals.append(f"RSI überkauft ({round(rsi,1)}) — Korrektur-Risiko")
        elif rsi > 55: bull_signals.append(f"RSI im bullishen Bereich ({round(rsi,1)})")
        elif rsi < 45: bear_signals.append(f"RSI im bearishen Bereich ({round(rsi,1)})")

    if macd_hist is not None:
        if macd_hist > 0: bull_signals.append("MACD-Histogramm positiv — bullishes Momentum")
        else:             bear_signals.append("MACD-Histogramm negativ — bearishes Momentum")

    if price and ma50:
        if price > ma50:  bull_signals.append(f"Kurs über MA50 ({round(ma50,2)}) — kurzfristig bullish")
        else:             bear_signals.append(f"Kurs unter MA50 ({round(ma50,2)}) — kurzfristig bearish")

    if price and ma200:
        if price > ma200: bull_signals.append(f"Kurs über MA200 ({round(ma200,2)}) — langfristig bullish")
        else:             bear_signals.append(f"Kurs unter MA200 ({round(ma200,2)}) — langfristig bearish")

    if ma50 and ma200:
        if ma50 > ma200:  bull_signals.append("Golden Cross (MA50 > MA200) — Aufwärtstrend aktiv")
        else:             bear_signals.append("Death Cross (MA50 < MA200) — Abwärtstrend aktiv")

    if bb_upper and bb_lower and price and (bb_upper - bb_lower) != 0:
        bb_pct = (price - bb_lower) / (bb_upper - bb_lower) * 100
        if bb_pct < 20:   bull_signals.append(f"Kurs nahe Bollinger-Unterband ({round(bb_pct,0):.0f}%) — überverkauft")
        elif bb_pct > 80: bear_signals.append(f"Kurs nahe Bollinger-Oberband ({round(bb_pct,0):.0f}%) — überkauft")

    if stoch_val is not None:
        if stoch_val < 0.2:  bull_signals.append(f"StochRSI überverkauft ({round(stoch_val*100,1)}) — Long-Setup")
        elif stoch_val > 0.8: bear_signals.append(f"StochRSI überkauft ({round(stoch_val*100,1)}) — Short-Setup")

    if obv_trend == "positiv": bull_signals.append("OBV-Trend positiv — institutioneller Kaufdruck")
    else:                      bear_signals.append("OBV-Trend negativ — institutioneller Verkaufsdruck")

    if adx and adx > 25:
        trend_dir = "bullish" if (price and ma50 and price > ma50) else "bearish"
        if trend_dir == "bullish": bull_signals.append(f"ADX {round(adx,1)} — starker bestätigter Aufwärtstrend")
        else:                      bear_signals.append(f"ADX {round(adx,1)} — starker bestätigter Abwärtstrend")

    signal = calculate_signal(rsi, macd_hist, price, ma50, ma200)
    levels = get_support_resistance(df, price)

    # ── Trade-Setup (ATR-basiert) ──
    trade_setup: dict = {}
    if atr and price:
        if signal in ("BUY", "WATCH"):
            sl   = round(price - 1.5 * atr, 4)
            tp1  = round(price + 2.0 * atr, 4)
            tp2  = round(price + 3.5 * atr, 4)
            direction = "long"
        else:
            sl   = round(price + 1.5 * atr, 4)
            tp1  = round(price - 2.0 * atr, 4)
            tp2  = round(price - 3.5 * atr, 4)
            direction = "short"
        risk   = abs(price - sl)
        reward = abs(tp1 - price)
        crv    = round(reward / risk, 2) if risk > 0 else 0
        trade_setup = {
            "entry": round(price, 4),
            "stop_loss": sl,
            "target1": tp1,
            "target2": tp2,
            "crv": crv,
            "atr": round(atr, 4),
            "direction": direction,
        }

    # Trend-Text
    if ma50 and ma200:
        if ma50 > ma200 and price and price > ma50:    trend_text = "Bestätigter Aufwärtstrend — Kurs > MA50 > MA200"
        elif ma50 > ma200:                             trend_text = "Aufwärtstrend — Konsolidierung unter MA50"
        elif ma50 < ma200 and price and price < ma50:  trend_text = "Bestätigter Abwärtstrend — Kurs < MA50 < MA200"
        else:                                          trend_text = "Seitwärtstrend / gemischte Signale"
    else:
        trend_text = "Datenbasis zu kurz für vollständige Trendanalyse"

    # ── REPORT II: Quant Research Memo ──
    monthly_seasonality: dict = {}
    current_month_data: dict | None = None

    try:
        df_5y, _ = fetch_df(symbol, "5y")
        if df_5y is not None and len(df_5y) >= 200:
            df_monthly = df_5y["Close"].resample("ME").last()
            monthly_rets = df_monthly.pct_change().dropna()
            by_month: dict[int, list[float]] = {}
            for date, ret in monthly_rets.items():
                m = date.month
                by_month.setdefault(m, []).append(float(ret) * 100)
            month_names = ["Jan","Feb","Mär","Apr","Mai","Jun","Jul","Aug","Sep","Okt","Nov","Dez"]
            for m, rets_list in sorted(by_month.items()):
                avg_ret  = round(sum(rets_list) / len(rets_list), 2)
                win_rate = round(sum(1 for r in rets_list if r > 0) / len(rets_list) * 100)
                monthly_seasonality[month_names[m-1]] = {
                    "avg_return": avg_ret,
                    "win_rate":   int(win_rate),
                    "samples":    len(rets_list),
                }
            cur_m = datetime.now().month
            if cur_m in by_month:
                cur_rets = by_month[cur_m]
                current_month_data = {
                    "month":      month_names[cur_m - 1],
                    "win_rate":   round(sum(1 for r in cur_rets if r > 0) / len(cur_rets) * 100),
                    "avg_return": round(sum(cur_rets) / len(cur_rets), 2),
                    "samples":    len(cur_rets),
                }
    except Exception:
        pass

    # Momentum (1M / 3M / 6M / 1Y)
    momentum: dict[str, float] = {}
    try:
        df_1y, _ = fetch_df(symbol, "1y")
        c = df_1y["Close"]
        for days, key in [(21, "1m"), (63, "3m"), (126, "6m"), (252, "1y")]:
            if len(c) >= days:
                momentum[key] = round((float(c.iloc[-1]) - float(c.iloc[-days])) / float(c.iloc[-days]) * 100, 2)
    except Exception:
        pass

    # Volumen-Analyse
    recent_vol = float(volume.iloc[-20:].mean()) if len(volume) >= 20 else None
    hist_vol   = float(volume.mean())             if len(volume) > 0  else None
    vol_ratio  = round(recent_vol / hist_vol, 2)  if recent_vol and hist_vol and hist_vol > 0 else None
    vol_trend  = "steigend" if vol_ratio and vol_ratio > 1.1 else "fallend" if vol_ratio and vol_ratio < 0.9 else "normal"

    # Historische Volatilität (annualisiert)
    returns      = close.pct_change().dropna()
    hist_vol_ann = round(float(returns.std() * np.sqrt(252) * 100), 1) if len(returns) > 5 else None
    atr_pct      = round(atr / price * 100, 2) if atr and price else None

    # Net Quant Edge
    pos_factors: list[str] = []
    neg_factors: list[str] = []

    for key, label in [("1m","1M"), ("3m","3M"), ("6m","6M")]:
        val = momentum.get(key)
        if val is not None:
            if val > 0: pos_factors.append(f"{label}-Momentum: +{val}%")
            else:       neg_factors.append(f"{label}-Momentum: {val}%")

    if current_month_data:
        wr = current_month_data["win_rate"]
        if wr >= 65:   pos_factors.append(f"Saisonalität {current_month_data['month']}: {wr}% historische Long-Quote")
        elif wr <= 40: neg_factors.append(f"Saisonalität {current_month_data['month']}: nur {wr}% historische Long-Quote")

    if vol_ratio and vol_ratio > 1.15:
        if signal in ("BUY","WATCH"): pos_factors.append(f"Volumen {round((vol_ratio-1)*100):.0f}% über Ø — bullishes Interesse")
        else:                         neg_factors.append(f"Volumen {round((vol_ratio-1)*100):.0f}% über Ø — Verkaufsdruck")

    if signal == "BUY":    pos_factors.append("Technisches Signal: KAUFEN")
    elif signal == "SELL": neg_factors.append("Technisches Signal: VERKAUFEN")
    elif signal == "WATCH": pos_factors.append("Technisches Signal: BEOBACHTEN")

    if   len(pos_factors) >= len(neg_factors) + 2: net_edge, edge_color = "BULLISH",  "green"
    elif len(neg_factors) >= len(pos_factors) + 2: net_edge, edge_color = "BEARISH",  "red"
    else:                                           net_edge, edge_color = "NEUTRAL",  "gold"

    return {
        "symbol":    symbol,
        "name":      config["name"],
        "type":      config["type"],
        "price":     round(price, 4) if price else None,
        "timestamp": datetime.now().isoformat(),
        "technical": {
            "signal":       signal,
            "trend":        trend_text,
            "bull_signals": bull_signals,
            "bear_signals": bear_signals,
            "indicators": {
                "rsi":      round(rsi,  1)    if rsi      else None,
                "macd_hist":round(macd_hist,4) if macd_hist else None,
                "ma50":     round(ma50, 2)    if ma50     else None,
                "ma200":    round(ma200,2)    if ma200    else None,
                "adx":      round(adx,  1)    if adx      else None,
                "atr":      round(atr,  4)    if atr      else None,
                "atr_pct":  atr_pct,
                "obv_trend":obv_trend,
            },
            "levels":       levels,
            "trade_setup":  trade_setup,
        },
        "quant": {
            "momentum":           momentum,
            "seasonality":        monthly_seasonality,
            "current_month":      current_month_data,
            "volume": {
                "ratio":      vol_ratio,
                "trend":      vol_trend,
                "recent_avg": round(recent_vol) if recent_vol else None,
                "hist_avg":   round(hist_vol)   if hist_vol   else None,
            },
            "volatility": {
                "hist_vol_ann": hist_vol_ann,
                "atr_pct":      atr_pct,
            },
            "net_edge":        net_edge,
            "edge_color":      edge_color,
            "positive_factors":pos_factors,
            "negative_factors":neg_factors,
        },
    }


@app.get("/api/strategy/leveraged-trend-rider")
def get_ltr_signal(capital: float = 10000.0):
    """
    Leveraged Trend Rider – aktuelles Signal für MSTR (5x Hebel).
    Gibt Signal (LONG/SHORT/NEUTRAL), Position-Sizing, Indikatoren und Kapitalschutz-Status zurück.
    """
    if capital <= 0 or capital > 1_000_000_000:
        raise HTTPException(status_code=400, detail="Ungültiges Kapital (0 < capital ≤ 1.000.000.000)")
    return run_leveraged_trend_rider(capital)
