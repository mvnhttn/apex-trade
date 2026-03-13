# APEX TRADE – Setup & Start

## Voraussetzungen
- Python 3.10+
- Pip

---

## 1. Installation (einmalig)

```bash
# Im apex-trade/ Ordner:
pip install -r requirements.txt
```

---

## 2. Server starten

```bash
# Im apex-trade/ Ordner:
uvicorn backend.main:app --reload
```

Der Server läuft dann auf: **http://localhost:8000**

Öffne einfach **http://localhost:8000** im Browser – fertig!

---

## Was passiert im Hintergrund?

| Endpoint | Beschreibung |
|---|---|
| `GET /` | Liefert das Frontend (index.html) |
| `GET /api/assets` | Alle Assets mit aktuellem Preis (Yahoo Finance) |
| `GET /api/asset/{symbol}` | Vollanalyse: RSI, MACD, MA, Bollinger, S/R |
| `GET /api/news/{symbol}` | Aktuelle News via yfinance |

---

## Verfügbare Symbole

| Symbol | Asset |
|---|---|
| XAU | Gold |
| XAG | Silber |
| HG  | Kupfer |
| BTC | Bitcoin |
| ETH | Ethereum |
| FCX | Freeport-McMoRan |
| AG  | First Majestic Silver |
| NEM | Newmont Corp. |
| NVDA | Nvidia |
| QBTS | D-Wave Quantum |
| SMCI | Super Micro Computer |

---

## Neues Asset hinzufügen

In `backend/main.py` unter `ASSETS` einfach eine Zeile ergänzen:

```python
"AAPL": {"ticker": "AAPL", "name": "Apple Inc.", "type": "stock", "unit": "$"},
```

---

## Technische Indikatoren – Erklärung

**RSI (14)** – Relative Strength Index
- < 30: Überverkauft → Kaufsignal
- > 70: Überkauft → Verkaufssignal
- Berechnet aus den letzten 14 Tageskerzen

**MACD** – Moving Average Convergence/Divergence
- Signal: 12-Tage-EMA minus 26-Tage-EMA
- Histogramm > 0 = Bullisch, < 0 = Bearisch

**Bollinger Bands %B**
- Zeigt wo der Kurs relativ zu den Bändern liegt
- > 100%: Über oberem Band (überkauft)
- < 0%: Unter unterem Band (überverkauft)

**ATR (14)** – Average True Range
- Durchschnittliche tägliche Schwankung
- Hilft beim Setzen des Stop-Loss (z.B. 2x ATR)

**Support & Resistance**
- Berechnet aus lokalen Hochs/Tiefs der letzten 90 Tage
- Rollierende 10-Perioden-Fenster

**Signal-Berechnung (Score-System)**
```
RSI < 30:  +3  |  RSI > 75: -3
RSI < 40:  +2  |  RSI > 65: -2
MACD > 0:  +2  |  MACD < 0: -2
Preis > MA50:  +1  |  Preis < MA50:  -1
Preis > MA200: +1  |  Preis < MA200: -1

Score >= 4  → BUY
Score >= 2  → WATCH
Score <= -4 → SELL
Sonst       → HOLD
```
