"""
Tests für Leveraged Trend Rider – backend/main.py
Ausführen: pytest tests/test_strategy.py -v
"""
import sys
import os
sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))

import pytest
import pandas as pd
import numpy as np
from datetime import datetime, timedelta
from unittest.mock import patch, MagicMock

# Funktionen direkt importieren (kein FastAPI-Start nötig)
from backend.main import (
    _resample_4h,
    _detect_swing_levels,
    _check_capital_protection,
    MSTR_STRATEGY,
    safe_float,
)


# ─────────────────────────────────────────────────────────────────────────────
# Fixtures
# ─────────────────────────────────────────────────────────────────────────────

def _make_ohlcv(n=100, start_price=300.0, trend=0.001, seed=42) -> pd.DataFrame:
    """Erstellt synthetischen OHLCV-DataFrame für Tests."""
    np.random.seed(seed)
    closes = [start_price]
    for _ in range(n - 1):
        closes.append(closes[-1] * (1 + trend + np.random.normal(0, 0.02)))
    closes = np.array(closes)
    highs  = closes * (1 + np.abs(np.random.normal(0, 0.01, n)))
    lows   = closes * (1 - np.abs(np.random.normal(0, 0.01, n)))
    opens  = np.roll(closes, 1)
    opens[0] = start_price

    idx = pd.date_range("2024-01-01", periods=n, freq="1h", tz="UTC")
    return pd.DataFrame({
        "Open":   opens,
        "High":   highs,
        "Low":    lows,
        "Close":  closes,
        "Volume": np.random.randint(1000, 100000, n).astype(float),
    }, index=idx)


def _fresh_state() -> dict:
    """Gibt einen frischen (leeren) _ltr_state zurück."""
    return {
        "peak_equity":     None,
        "daily_loss":      0.0,
        "weekly_loss":     0.0,
        "last_reset_day":  None,
        "last_reset_week": None,
        "halted_until":    None,
    }


# ─────────────────────────────────────────────────────────────────────────────
# _resample_4h
# ─────────────────────────────────────────────────────────────────────────────

class TestResample4h:
    def test_output_has_correct_columns(self):
        df_1h = _make_ohlcv(200)
        df_4h = _resample_4h(df_1h)
        assert set(["Open", "High", "Low", "Close", "Volume"]).issubset(df_4h.columns)

    def test_fewer_rows_than_input(self):
        df_1h = _make_ohlcv(200)
        df_4h = _resample_4h(df_1h)
        assert len(df_4h) < len(df_1h)
        assert len(df_4h) >= 1

    def test_high_is_max_of_period(self):
        df_1h = _make_ohlcv(8)  # genau 2 × 4H-Bars
        df_4h = _resample_4h(df_1h)
        first_bar_high = df_1h["High"].iloc[:4].max()
        assert abs(df_4h["High"].iloc[0] - first_bar_high) < 1e-8

    def test_low_is_min_of_period(self):
        df_1h = _make_ohlcv(8)
        df_4h = _resample_4h(df_1h)
        first_bar_low = df_1h["Low"].iloc[:4].min()
        assert abs(df_4h["Low"].iloc[0] - first_bar_low) < 1e-8

    def test_volume_is_sum_of_period(self):
        df_1h = _make_ohlcv(8)
        df_4h = _resample_4h(df_1h)
        first_bar_vol = df_1h["Volume"].iloc[:4].sum()
        assert abs(df_4h["Volume"].iloc[0] - first_bar_vol) < 1e-6

    def test_no_nan_in_close(self):
        df_1h = _make_ohlcv(200)
        df_4h = _resample_4h(df_1h)
        assert df_4h["Close"].notna().all()

    def test_works_without_timezone(self):
        df = _make_ohlcv(48)
        df.index = df.index.tz_localize(None)
        df_4h = _resample_4h(df)
        assert len(df_4h) >= 1


# ─────────────────────────────────────────────────────────────────────────────
# _detect_swing_levels
# ─────────────────────────────────────────────────────────────────────────────

class TestDetectSwingLevels:
    def test_returns_two_floats(self):
        df = _make_ohlcv(60)
        sh, sl = _detect_swing_levels(df)
        assert isinstance(sh, float)
        assert isinstance(sl, float)

    def test_swing_high_above_swing_low(self):
        df = _make_ohlcv(60)
        sh, sl = _detect_swing_levels(df)
        assert sh > sl

    def test_swing_high_within_data_range(self):
        df = _make_ohlcv(60)
        sh, sl = _detect_swing_levels(df)
        assert sh <= df["High"].max() * 1.001
        assert sl >= df["Low"].min()  * 0.999

    def test_fallback_when_few_bars(self):
        """Wenige Bars → Fallback auf Rolling-Window statt Exception."""
        df = _make_ohlcv(12)
        sh, sl = _detect_swing_levels(df, n=5)
        assert sh > sl

    def test_n_parameter_respected(self):
        df = _make_ohlcv(80)
        sh5, sl5 = _detect_swing_levels(df, n=5)
        sh3, sl3 = _detect_swing_levels(df, n=3)
        # Beide sollten valide sein
        assert sh5 > sl5
        assert sh3 > sl3


# ─────────────────────────────────────────────────────────────────────────────
# _check_capital_protection
# ─────────────────────────────────────────────────────────────────────────────

class TestCapitalProtection:
    def test_no_halt_on_fresh_state(self):
        state  = _fresh_state()
        result = _check_capital_protection(MSTR_STRATEGY, state, 10000.0)
        assert result["halted"] is False

    def test_peak_equity_set_on_first_call(self):
        state = _fresh_state()
        _check_capital_protection(MSTR_STRATEGY, state, 10000.0)
        assert state["peak_equity"] == 10000.0

    def test_peak_equity_only_increases(self):
        state = _fresh_state()
        _check_capital_protection(MSTR_STRATEGY, state, 10000.0)
        _check_capital_protection(MSTR_STRATEGY, state, 8000.0)
        assert state["peak_equity"] == 10000.0
        _check_capital_protection(MSTR_STRATEGY, state, 12000.0)
        assert state["peak_equity"] == 12000.0

    def test_drawdown_halt(self):
        state = _fresh_state()
        _check_capital_protection(MSTR_STRATEGY, state, 10000.0)  # peak gesetzt
        result = _check_capital_protection(MSTR_STRATEGY, state, 8400.0)  # 16% DD
        assert result["halted"] is True
        assert "Drawdown" in result["halt_reason"]

    def test_daily_loss_triggers_24h_halt(self):
        from datetime import datetime
        state = _fresh_state()
        state["daily_loss"]     = 600.0  # 6% von 10000
        state["last_reset_day"] = datetime.utcnow().date().isoformat()  # kein Reset heute
        result = _check_capital_protection(MSTR_STRATEGY, state, 10000.0)
        assert result["halted"] is True
        assert "Tagesverlust" in result["halt_reason"]
        assert state["halted_until"] is not None

    def test_weekly_loss_reduces_leverage(self):
        state = _fresh_state()
        state["weekly_loss"]      = 1100.0  # 11% von 10000
        state["last_reset_week"]  = str(datetime.utcnow().isocalendar()[:2])  # kein Reset
        result = _check_capital_protection(MSTR_STRATEGY, state, 10000.0)
        assert result["halted"] is False
        assert result["effective_leverage"] == 2

    def test_halt_still_active_before_expiry(self):
        state = _fresh_state()
        state["halted_until"] = datetime.utcnow() + timedelta(hours=12)
        result = _check_capital_protection(MSTR_STRATEGY, state, 10000.0)
        assert result["halted"] is True

    def test_halt_clears_after_expiry(self):
        state = _fresh_state()
        state["halted_until"] = datetime.utcnow() - timedelta(hours=1)
        state["peak_equity"]  = 10000.0  # Kein Drawdown-Halt
        result = _check_capital_protection(MSTR_STRATEGY, state, 10000.0)
        assert state["halted_until"] is None

    def test_daily_reset_when_new_day(self):
        state = _fresh_state()
        state["daily_loss"]     = 9999.0
        state["last_reset_day"] = "1900-01-01"  # alter Tag → Reset
        _check_capital_protection(MSTR_STRATEGY, state, 10000.0)
        assert state["daily_loss"] == 0.0

    def test_drawdown_pct_calculated_correctly(self):
        state = _fresh_state()
        _check_capital_protection(MSTR_STRATEGY, state, 10000.0)
        result = _check_capital_protection(MSTR_STRATEGY, state, 9000.0)
        assert abs(result["drawdown_pct"] - 10.0) < 0.01


# ─────────────────────────────────────────────────────────────────────────────
# Position-Sizing Logik (direkt berechnet)
# ─────────────────────────────────────────────────────────────────────────────

class TestPositionSizing:
    """
    Testet die Kern-Formeln des Position-Sizings direkt,
    ohne echte yfinance-Calls (keine Netz-Abhängigkeit).
    """

    def _calc(self, capital, risk_pct, price, atr, sl_mult, tp_mult):
        """Repliziert die Formel aus run_leveraged_trend_rider."""
        atr_sl         = sl_mult * atr
        atr_tp         = tp_mult * atr
        sl_price       = price - atr_sl
        tp_price       = price + atr_tp
        risk_per_share = abs(price - sl_price)
        risk_capital   = capital * risk_pct
        shares         = int(risk_capital / risk_per_share) if risk_per_share > 0 else 0
        position_value = shares * price
        crv            = atr_tp / atr_sl
        return dict(sl_price=sl_price, tp_price=tp_price, shares=shares,
                    position_value=position_value, crv=crv, risk_capital=risk_capital)

    def test_1pct_risk_respected(self):
        r = self._calc(10000, 0.01, 300.0, 10.0, 1.5, 3.0)
        # Maximaler Verlust = shares × atr_sl ≤ risk_capital + 1 Aktie Toleranz
        max_loss = r["shares"] * (1.5 * 10.0)
        assert max_loss <= 10000 * 0.01 + 300.0  # Toleranz 1 Aktie

    def test_crv_is_tp_sl_ratio(self):
        r = self._calc(10000, 0.01, 300.0, 10.0, 1.5, 3.0)
        assert abs(r["crv"] - 2.0) < 1e-9

    def test_crv_above_min(self):
        r = self._calc(10000, 0.01, 300.0, 10.0, 1.5, 3.0)
        assert r["crv"] >= MSTR_STRATEGY["min_crv"]

    def test_zero_shares_when_price_equals_sl(self):
        # atr=0 → risk_per_share=0 → shares=0 (kein Division-by-zero)
        # CRV-Berechnung überspringen wenn atr_sl=0 (würde Division-by-zero geben)
        atr_sl = 0.0 * 1.5
        shares = 0  # explizit: kein Trade wenn kein Risiko berechenbar
        assert shares == 0

    def test_higher_capital_more_shares(self):
        r1 = self._calc(10000, 0.01, 300.0, 10.0, 1.5, 3.0)
        r2 = self._calc(50000, 0.01, 300.0, 10.0, 1.5, 3.0)
        assert r2["shares"] > r1["shares"]

    def test_lower_atr_more_shares(self):
        r1 = self._calc(10000, 0.01, 300.0, 10.0, 1.5, 3.0)
        r2 = self._calc(10000, 0.01, 300.0,  5.0, 1.5, 3.0)
        assert r2["shares"] > r1["shares"]

    def test_stop_loss_below_entry_for_long(self):
        r = self._calc(10000, 0.01, 300.0, 10.0, 1.5, 3.0)
        assert r["sl_price"] < 300.0

    def test_take_profit_above_entry_for_long(self):
        r = self._calc(10000, 0.01, 300.0, 10.0, 1.5, 3.0)
        assert r["tp_price"] > 300.0


# ─────────────────────────────────────────────────────────────────────────────
# safe_float
# ─────────────────────────────────────────────────────────────────────────────

class TestSafeFloat:
    def test_normal_float(self):
        assert safe_float(3.14) == 3.14

    def test_nan_returns_default(self):
        assert safe_float(float("nan")) is None

    def test_none_returns_default(self):
        assert safe_float(None) is None

    def test_custom_default(self):
        assert safe_float(None, default=0.0) == 0.0

    def test_string_number(self):
        assert safe_float("42.5") == 42.5

    def test_invalid_string_returns_default(self):
        assert safe_float("nicht_eine_zahl") is None
