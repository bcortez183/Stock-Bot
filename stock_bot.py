"""
Stock Pre-Run Alert Bot
=======================
Scans a watchlist for technical signals that commonly appear BEFORE a stock runs up.

Signals detected:
  1. Volume Surge     – today's volume is 2x+ the 20-day average (unusual accumulation)
  2. RSI Coiling      – RSI between 45–60 and rising (not overbought, building momentum)
  3. MACD Cross       – MACD line just crossed above the signal line (bullish momentum shift)
  4. Squeeze Setup    – Bollinger Bands tightening inside Keltner Channels (volatility contraction → expansion)
  5. Price Near Breakout – within 2% of a recent 20-day high (poised to break out)

Setup:
  pip install yfinance pandas ta schedule requests

Optional (Telegram alerts):
  pip install python-telegram-bot
  Set TELEGRAM_BOT_TOKEN and TELEGRAM_CHAT_ID environment variables.

Usage:
  python stock_bot.py
"""

import os
import time
import datetime
import yfinance as yf
import pandas as pd
import schedule

# ── CONFIG ────────────────────────────────────────────────────────────────────

WATCHLIST = [
    "AAPL", "NVDA", "AMD", "TSLA", "MSFT",
    "META", "AMZN", "GOOGL", "PLTR", "SOFI",
    # Add any tickers you want to watch
]

SCAN_INTERVAL_MINUTES = 15   # How often to scan (market hours only)
MIN_SIGNALS_TO_ALERT  = 2    # How many signals must fire to send an alert
LOOKBACK_DAYS         = 60   # Days of history to pull for calculations

# Telegram (optional) – set these as env vars or paste strings here
TELEGRAM_TOKEN   = "8608655894:AAF8rAsCBSWeGMhV9ALCQ17qO_ooSw7ISdU"
TELEGRAM_CHAT_ID = "8773798653"

# ── HELPERS ───────────────────────────────────────────────────────────────────

def is_market_hours() -> bool:
    """Returns True during US market hours Mon–Fri 9:30–16:00 ET."""
    import pytz
    et = pytz.timezone("America/New_York")
    now = datetime.datetime.now(et)
    if now.weekday() >= 5:          # Saturday / Sunday
        return False
    market_open  = now.replace(hour=9,  minute=30, second=0, microsecond=0)
    market_close = now.replace(hour=16, minute=0,  second=0, microsecond=0)
    return market_open <= now <= market_close


def send_telegram(message: str):
    """Send a Telegram message if credentials are configured."""
    if not TELEGRAM_TOKEN or not TELEGRAM_CHAT_ID:
        return
    try:
        import requests
        url = f"https://api.telegram.org/bot{TELEGRAM_TOKEN}/sendMessage"
        requests.post(url, data={"chat_id": TELEGRAM_CHAT_ID, "text": message}, timeout=10)
    except Exception as e:
        print(f"  [Telegram error] {e}")


def alert(ticker: str, price: float, signals: list[str]):
    """Print and optionally send a Telegram alert."""
    header  = f"\n🚨 PRE-RUN ALERT: {ticker} @ ${price:.2f}"
    details = "\n".join(f"  ✅ {s}" for s in signals)
    msg     = f"{header}\n{details}\n"
    print(msg)
    send_telegram(msg)


# ── SIGNAL DETECTION ──────────────────────────────────────────────────────────

def compute_signals(ticker: str) -> tuple[float, list[str]]:
    """
    Download data and evaluate pre-run signals.
    Returns (current_price, list_of_triggered_signal_descriptions).
    """
    signals = []

    try:
        df = yf.download(ticker, period=f"{LOOKBACK_DAYS}d", interval="1d",
                         auto_adjust=True, progress=False)
        if df.empty or len(df) < 20:
            return 0.0, []

        close  = df["Close"].squeeze()
        volume = df["Volume"].squeeze()
        high   = df["High"].squeeze()
        low    = df["Low"].squeeze()

        price = float(close.iloc[-1])

        # ── 1. Volume Surge ───────────────────────────────────────────────────
        avg_vol = volume.iloc[-21:-1].mean()   # 20-day avg (excluding today)
        today_vol = float(volume.iloc[-1])
        if avg_vol > 0 and today_vol >= 2.0 * avg_vol:
            ratio = today_vol / avg_vol
            signals.append(f"Volume Surge: {ratio:.1f}x the 20-day average")

        # ── 2. RSI Coiling (45–60 and rising) ────────────────────────────────
        delta = close.diff()
        gain  = delta.clip(lower=0).rolling(14).mean()
        loss  = (-delta.clip(upper=0)).rolling(14).mean()
        rs    = gain / loss.replace(0, float("nan"))
        rsi   = 100 - (100 / (1 + rs))
        rsi_now  = float(rsi.iloc[-1])
        rsi_prev = float(rsi.iloc[-3])     # 3 days ago
        if 45 <= rsi_now <= 60 and rsi_now > rsi_prev:
            signals.append(f"RSI Coiling: RSI={rsi_now:.1f} (rising from {rsi_prev:.1f})")

        # ── 3. MACD Bullish Cross ─────────────────────────────────────────────
        ema12 = close.ewm(span=12, adjust=False).mean()
        ema26 = close.ewm(span=26, adjust=False).mean()
        macd  = ema12 - ema26
        signal_line = macd.ewm(span=9, adjust=False).mean()
        if (float(macd.iloc[-1]) > float(signal_line.iloc[-1]) and
                float(macd.iloc[-2]) <= float(signal_line.iloc[-2])):
            signals.append(
                f"MACD Cross: MACD just crossed above signal line "
                f"({float(macd.iloc[-1]):.3f} vs {float(signal_line.iloc[-1]):.3f})"
            )

        # ── 4. Bollinger Band Squeeze (inside Keltner Channels) ───────────────
        bb_period, bb_std = 20, 2.0
        bb_mid   = close.rolling(bb_period).mean()
        bb_upper = bb_mid + bb_std * close.rolling(bb_period).std()
        bb_lower = bb_mid - bb_std * close.rolling(bb_period).std()

        tr = pd.concat([
            high - low,
            (high - close.shift()).abs(),
            (low  - close.shift()).abs()
        ], axis=1).max(axis=1)
        atr      = tr.rolling(20).mean()
        kc_upper = bb_mid + 1.5 * atr
        kc_lower = bb_mid - 1.5 * atr

        squeeze = (bb_upper.iloc[-1] < kc_upper.iloc[-1] and
                   bb_lower.iloc[-1] > kc_lower.iloc[-1])
        if squeeze:
            bb_width = float((bb_upper.iloc[-1] - bb_lower.iloc[-1]) / bb_mid.iloc[-1] * 100)
            signals.append(f"Squeeze Setup: Bands compressed to {bb_width:.1f}% width — coiled spring")

        # ── 5. Near 20-Day Breakout Level ─────────────────────────────────────
        recent_high = float(close.iloc[-21:-1].max())
        if price >= 0.98 * recent_high:
            pct_from_high = (price / recent_high - 1) * 100
            signals.append(
                f"Near Breakout: Price is {pct_from_high:+.1f}% from 20-day high (${recent_high:.2f})"
            )

    except Exception as e:
        print(f"  [Error on {ticker}] {e}")
        return 0.0, []

    return price, signals


# ── MAIN SCAN ─────────────────────────────────────────────────────────────────

def scan():
    """Run a full scan of the watchlist."""
    if not is_market_hours():
        print(f"[{datetime.datetime.now():%H:%M}] Market closed — skipping scan.")
        return

    print(f"\n{'='*55}")
    print(f"  Scanning {len(WATCHLIST)} tickers  [{datetime.datetime.now():%Y-%m-%d %H:%M}]")
    print(f"{'='*55}")

    alerts_fired = 0
    for ticker in WATCHLIST:
        price, signals = compute_signals(ticker)
        if not price:
            continue

        if len(signals) >= MIN_SIGNALS_TO_ALERT:
            alert(ticker, price, signals)
            alerts_fired += 1
        else:
            # Print a quiet status line for tickers with 1 weak signal or none
            tag = f"({len(signals)} signal)" if signals else "(no signals)"
            print(f"  {ticker:<6} ${price:<9.2f} {tag}")

    if alerts_fired == 0:
        print("\n  No strong pre-run setups detected this scan.")
    print()


# ── ENTRY POINT ───────────────────────────────────────────────────────────────

if __name__ == "__main__":
    print("Stock Pre-Run Alert Bot started.")
    print(f"Watchlist : {', '.join(WATCHLIST)}")
    print(f"Signals needed to alert: {MIN_SIGNALS_TO_ALERT}")
    print(f"Scan interval: every {SCAN_INTERVAL_MINUTES} minutes (market hours only)")
    print(f"Telegram alerts: {'enabled' if TELEGRAM_TOKEN else 'disabled (see setup comments)'}")
    print()

    scan()   # Run immediately on launch

    schedule.every(SCAN_INTERVAL_MINUTES).minutes.do(scan)
    while True:
        schedule.run_pending()
        time.sleep(30)

