import os
import time
import datetime
import yfinance as yf
import pandas as pd
import requests
import schedule
import pytz

# ── CONFIG ────────────────────────────────────────────────────────────────────

CORE_WATCHLIST = [
    "AAPL", "NVDA", "AMD", "TSLA", "MSFT",
    "META", "AMZN", "GOOGL", "PLTR", "SOFI",
]

TOP_MOVERS_COUNT      = 20
TOP_PENNY_COUNT       = 15
SCAN_INTERVAL_MINUTES = 15
MIN_SIGNALS_TO_ALERT  = 2
LOOKBACK_DAYS         = 60

PENNY_MAX_PRICE       = 5.00
PENNY_MIN_PRICE       = 0.10
PENNY_VOLUME_MULT     = 3.0
PENNY_MIN_GAIN        = 5.0

MAIN_TRADE_AMOUNT     = 100.00
PENNY_TRADE_AMOUNT    = 50.00
PROFIT_TARGET         = 0.07
STOP_LOSS             = 0.03
MAX_OPEN_TRADES       = 5
PAPER_TRADING         = True

ALPACA_KEY    = "PK4GYDUJPNGG6PJPAR326JXQ4V"
ALPACA_SECRET = "EtMu7LxJrpkM2nypoZkwsF8pH71i87GGvaGTPxxfmKkS"

if PAPER_TRADING:
    ALPACA_URL = "https://paper-api.alpaca.markets"
else:
    ALPACA_URL = "https://api.alpaca.markets"

TELEGRAM_TOKEN   = "8608655894:AAF8rAsCBSWeGMhV9ALCQ17qO_ooSw7ISdU"
TELEGRAM_CHAT_ID = "8773798653"

# ── HELPERS ───────────────────────────────────────────────────────────────────

def now_pt():
    return datetime.datetime.now(pytz.timezone("America/Los_Angeles"))

def now_et():
    return datetime.datetime.now(pytz.timezone("America/New_York"))

def is_market_hours():
    et = now_et()
    if et.weekday() >= 5:
        return False
    market_open  = et.replace(hour=9,  minute=30, second=0, microsecond=0)
    market_close = et.replace(hour=16, minute=0,  second=0, microsecond=0)
    return market_open <= et <= market_close

def is_weekday():
    return now_pt().weekday() < 5

def send_telegram(message):
    if not TELEGRAM_TOKEN or not TELEGRAM_CHAT_ID:
        print(message)
        return
    try:
        url = "https://api.telegram.org/bot" + TELEGRAM_TOKEN + "/sendMessage"
        requests.post(url, data={"chat_id": TELEGRAM_CHAT_ID, "text": message}, timeout=10)
    except Exception as e:
        print("Telegram error: " + str(e))

def alert(ticker, price, signals, tag="PRE-RUN"):
    header  = "\n🚨 " + tag + " ALERT: " + ticker + " @ $" + str(round(price, 2))
    details = "\n".join(["  ✅ " + s for s in signals])
    msg     = header + "\n" + details + "\n"
    print(msg)
    send_telegram(msg)

# ── ALPACA TRADING ────────────────────────────────────────────────────────────

def alpaca_headers():
    return {
        "APCA-API-KEY-ID":     ALPACA_KEY,
        "APCA-API-SECRET-KEY": ALPACA_SECRET,
        "Content-Type":        "application/json",
    }

def get_open_positions():
    try:
        r = requests.get(ALPACA_URL + "/v2/positions", headers=alpaca_headers(), timeout=10)
        positions = {}
        for p in r.json():
            positions[p["symbol"]] = {
                "qty":   float(p["qty"]),
                "entry": float(p["avg_entry_price"]),
                "pl":    float(p["unrealized_plpc"]) * 100,
            }
        return positions
    except Exception as e:
        print("Positions error: " + str(e))
        return {}

def get_account():
    try:
        r = requests.get(ALPACA_URL + "/v2/account", headers=alpaca_headers(), timeout=10)
        return r.json()
    except Exception as e:
        print("Account error: " + str(e))
        return {}

def place_buy(ticker, amount, trade_type="MAIN"):
    try:
        order = {
            "symbol":        ticker,
            "notional":      str(round(amount, 2)),
            "side":          "buy",
            "type":          "market",
            "time_in_force": "day",
        }
        r    = requests.post(ALPACA_URL + "/v2/orders", json=order, headers=alpaca_headers(), timeout=10)
        data = r.json()
        if "id" in data:
            emoji = "🟢" if trade_type == "MAIN" else "💸"
            msg   = (emoji + " BUY: " + ticker + " $" + str(amount) + " (" + trade_type + ")\n"
                     + "  🎯 Target: +7% | Stop: -3%")
            print(msg)
            send_telegram(msg)
            return True
        else:
            print("Buy error " + ticker + ": " + str(data))
            return False
    except Exception as e:
        print("Buy exception " + ticker + ": " + str(e))
        return False

def place_sell(ticker, qty, reason):
    try:
        order = {
            "symbol":        ticker,
            "qty":           str(qty),
            "side":          "sell",
            "type":          "market",
            "time_in_force": "day",
        }
        r    = requests.post(ALPACA_URL + "/v2/orders", json=order, headers=alpaca_headers(), timeout=10)
        data = r.json()
        if "id" in data:
            emoji = "✅" if "profit" in reason.lower() else "🛑"
            msg   = emoji + " SELL: " + ticker + "\n  Reason: " + reason
            print(msg)
            send_telegram(msg)
            return True
        else:
            print("Sell error " + ticker + ": " + str(data))
            return False
    except Exception as e:
        print("Sell exception " + ticker + ": " + str(e))
        return False

def monitor_positions():
    positions = get_open_positions()
    if not positions:
        return
    print("\n  -- Monitoring " + str(len(positions)) + " open position(s) --")
    for ticker, pos in positions.items():
        pl  = pos["pl"]
        qty = pos["qty"]
        print("  " + ticker + " P/L: " + str(round(pl, 2)) + "%")
        if pl >= PROFIT_TARGET * 100:
            place_sell(ticker, qty, "Profit target hit (+" + str(round(pl, 2)) + "%)")
        elif pl <= -(STOP_LOSS * 100):
            place_sell(ticker, qty, "Stop loss hit (" + str(round(pl, 2)) + "%)")

# ── WATCHLISTS ────────────────────────────────────────────────────────────────

def get_top_movers():
    tickers = set()
    headers = {"User-Agent": "Mozilla/5.0"}
    for screener in ["most_actives", "day_gainers", "day_losers"]:
        try:
            url = "https://query1.finance.yahoo.com/v1/finance/screener/predefined/saved?scrIds=" + screener + "&count=10"
            r   = requests.get(url, headers=headers, timeout=10)
            for q in r.json()["finance"]["result"][0]["quotes"]:
                s = q.get("symbol", "")
                if s and len(s) <= 5 and "." not in s and "-" not in s:
                    tickers.add(s)
        except:
            pass
    return list(tickers)[:TOP_MOVERS_COUNT]

def get_top_penny_movers():
    tickers = set()
    headers = {"User-Agent": "Mozilla/5.0"}
    for screener in ["day_gainers", "most_actives", "small_cap_gainers"]:
        try:
            url = "https://query1.finance.yahoo.com/v1/finance/screener/predefined/saved?scrIds=" + screener + "&count=25"
            r   = requests.get(url, headers=headers, timeout=10)
            for q in r.json()["finance"]["result"][0]["quotes"]:
                s = q.get("symbol", "")
                p = q.get("regularMarketPrice", 999)
                c = q.get("regularMarketChangePercent", 0)
                if s and len(s) <= 5 and "." not in s and "-" not in s and PENNY_MIN_PRICE <= p <= PENNY_MAX_PRICE and c >= 3.0:
                    tickers.add(s)
        except:
            pass
    fallback = ["SNDL","CLOV","MULN","NKLA","GOEV","WKHS","RIDE","EXPR","SPCE","ATER","BBIG","PHUN","CIDM","NAKD","GFAI"]
    result = list(tickers)
    for t in fallback:
        if t not in result:
            result.append(t)
        if len(result) >= TOP_PENNY_COUNT:
            break
    return result[:TOP_PENNY_COUNT]

_main_watchlist  = []
_penny_watchlist = []
_watchlist_date  = ""

def refresh_watchlists():
    global _main_watchlist, _penny_watchlist, _watchlist_date
    today = datetime.date.today().isoformat()
    if _watchlist_date == today:
        return
    print("  Building today's watchlists...")
    _main_watchlist  = list(dict.fromkeys(CORE_WATCHLIST + get_top_movers()))
    _penny_watchlist = get_top_penny_movers()
    _watchlist_date  = today

def get_main_watchlist():
    refresh_watchlists()
    return _main_watchlist

def get_penny_watchlist():
    refresh_watchlists()
    return _penny_watchlist

# ── MORNING BRIEFING ──────────────────────────────────────────────────────────

def morning_briefing():
    if not is_weekday():
        return
    refresh_watchlists()
    account      = get_account()
    buying_power = float(account.get("buying_power", 0))
    date_str     = now_pt().strftime("%A, %B %d")
    mode_str     = "PAPER (test)" if PAPER_TRADING else "LIVE TRADING"

    msg = (
        "☀️ Good morning! " + date_str + "\n\n"
        + "📋 MAIN WATCHLIST (" + str(len(_main_watchlist)) + " stocks):\n"
        + ", ".join(_main_watchlist) + "\n"
        + "💵 $" + str(int(MAIN_TRADE_AMOUNT)) + " per trade\n\n"
        + "💸 PENNY WATCHLIST (" + str(len(_penny_watchlist)) + " stocks):\n"
        + ", ".join(_penny_watchlist) + "\n"
        + "💵 $" + str(int(PENNY_TRADE_AMOUNT)) + " per trade\n\n"
        + "💰 Buying Power: $" + str(round(buying_power, 2)) + "\n"
        + "🎯 Target: +7% | Stop: -3% | Max: " + str(MAX_OPEN_TRADES) + " positions\n"
        + "📄 Mode: " + mode_str + "\n"
        + "⏰ Market opens 6:30 AM PT"
    )
    print("\n" + msg + "\n")
    send_telegram(msg)

# ── SIGNAL DETECTION ──────────────────────────────────────────────────────────

def compute_signals(ticker):
    signals = []
    try:
        df = yf.download(ticker, period=str(LOOKBACK_DAYS)+"d", interval="1d", auto_adjust=True, progress=False)
        if df.empty or len(df) < 20:
            return 0.0, []

        close  = df["Close"].squeeze()
        volume = df["Volume"].squeeze()
        high   = df["High"].squeeze()
        low    = df["Low"].squeeze()
        price  = float(close.iloc[-1])

        avg_vol   = volume.iloc[-21:-1].mean()
        today_vol = float(volume.iloc[-1])
        if avg_vol > 0 and today_vol >= 2.0 * avg_vol:
            signals.append("Volume Surge: " + str(round(today_vol/avg_vol, 1)) + "x average")

        delta    = close.diff()
        gain     = delta.clip(lower=0).rolling(14).mean()
        loss     = (-delta.clip(upper=0)).rolling(14).mean()
        rsi      = 100 - (100 / (1 + gain / loss.replace(0, float("nan"))))
        rsi_now  = float(rsi.iloc[-1])
        rsi_prev = float(rsi.iloc[-3])
        if 45 <= rsi_now <= 60 and rsi_now > rsi_prev:
            signals.append("RSI Coiling: " + str(round(rsi_now, 1)) + " rising from " + str(round(rsi_prev, 1)))

        macd = close.ewm(span=12).mean() - close.ewm(span=26).mean()
        sig  = macd.ewm(span=9).mean()
        if float(macd.iloc[-1]) > float(sig.iloc[-1]) and float(macd.iloc[-2]) <= float(sig.iloc[-2]):
            signals.append("MACD Cross: Bullish crossover")

        bb_mid = close.rolling(20).mean()
        bb_u   = bb_mid + 2 * close.rolling(20).std()
        bb_l   = bb_mid - 2 * close.rolling(20).std()
        tr     = pd.concat([high-low, (high-close.shift()).abs(), (low-close.shift()).abs()], axis=1).max(axis=1)
        atr    = tr.rolling(20).mean()
        if bb_u.iloc[-1] < bb_mid.iloc[-1] + 1.5*atr.iloc[-1] and bb_l.iloc[-1] > bb_mid.iloc[-1] - 1.5*atr.iloc[-1]:
            signals.append("Squeeze: Bands compressed — coiled spring")

        rh = float(close.iloc[-21:-1].max())
        if price >= 0.98 * rh:
            signals.append("Near Breakout: " + str(round((price/rh-1)*100, 1)) + "% from 20-day high")

    except Exception as e:
        print("Error on " + ticker + ": " + str(e))
        return 0.0, []
    return price, signals

def check_penny_stock(ticker, open_positions):
    try:
        df = yf.download(ticker, period="30d", interval="1d", auto_adjust=True, progress=False)
        if df.empty or len(df) < 5:
            return
        close     = df["Close"].squeeze()
        volume    = df["Volume"].squeeze()
        price     = float(close.iloc[-1])
        prev      = float(close.iloc[-2])
        avg_vol   = float(volume.iloc[-6:-1].mean())
        today_vol = float(volume.iloc[-1])

        if not (PENNY_MIN_PRICE <= price <= PENNY_MAX_PRICE):
            return

        delta    = close.diff()
        gain     = delta.clip(lower=0).rolling(14).mean()
        loss     = (-delta.clip(upper=0)).rolling(14).mean()
        rsi      = float((100-(100/(1+gain/loss.replace(0,float("nan"))))).iloc[-1])
        gain_pct = (price - prev) / prev * 100

        signals = []
        if avg_vol > 0 and today_vol >= PENNY_VOLUME_MULT * avg_vol:
            signals.append("Volume: " + str(round(today_vol/avg_vol, 1)) + "x average")
        if gain_pct >= PENNY_MIN_GAIN:
            signals.append("Momentum: Up " + str(round(gain_pct, 1)) + "% today")
        if rsi < 70:
            signals.append("RSI: " + str(round(rsi, 1)) + " — room to run")

        if len(signals) >= 2:
            alert(ticker, price, signals, tag="💸 PENNY FLIP")
            if ticker not in open_positions and len(open_positions) < MAX_OPEN_TRADES:
                place_buy(ticker, PENNY_TRADE_AMOUNT, trade_type="PENNY")
                open_positions[ticker] = {}
    except Exception as e:
        print("Penny error " + ticker + ": " + str(e))

# ── MAIN SCAN ─────────────────────────────────────────────────────────────────

def scan():
    if not is_market_hours():
        print("[" + now_pt().strftime("%H:%M") + " PT] Market closed — skipping scan.")
        return

    open_positions = get_open_positions()
    main_list      = get_main_watchlist()
    penny_list     = get_penny_watchlist()

    print("\n" + "="*55)
    print("  Main: " + str(len(main_list)) + " | Penny: " + str(len(penny_list)) + " | Open: " + str(len(open_positions)) + "/" + str(MAX_OPEN_TRADES))
    print("  [" + now_pt().strftime("%Y-%m-%d %H:%M") + " PT]")
    print("="*55)

    monitor_positions()

    alerts_fired = 0
    for ticker in main_list:
        price, signals = compute_signals(ticker)
        if not price:
            continue
        if len(signals) >= MIN_SIGNALS_TO_ALERT:
            alert(ticker, price, signals)
            alerts_fired += 1
            if ticker not in open_positions and len(open_positions) < MAX_OPEN_TRADES:
                place_buy(ticker, MAIN_TRADE_AMOUNT, trade_type="MAIN")
                open_positions[ticker] = {}
        else:
            tag = "(" + str(len(signals)) + " signal)" if signals else "(no signals)"
            print("  " + ticker.ljust(6) + " $" + str(round(price, 2)).ljust(9) + " " + tag)

    print("\n  -- Penny Stock Scan --")
    for ticker in penny_list:
        check_penny_stock(ticker, open_positions)

    if alerts_fired == 0:
        print("\n  No strong setups detected this scan.")
    print()

# ── ENTRY POINT ───────────────────────────────────────────────────────────────

if __name__ == "__main__":
    mode = "PAPER TRADING (safe test mode)" if PAPER_TRADING else "LIVE TRADING"
    print("Stock Pre-Run Alert Bot — Full Auto Buy/Sell Edition")
    print("Mode          : " + mode)
    print("Main trades   : $" + str(int(MAIN_TRADE_AMOUNT)) + " per stock")
    print("Penny trades  : $" + str(int(PENNY_TRADE_AMOUNT)) + " per stock")
    print("Profit target : +" + str(int(PROFIT_TARGET*100)) + "%")
    print("Stop loss     : -" + str(int(STOP_LOSS*100)) + "%")
    print("Max positions : " + str(MAX_OPEN_TRADES))
    print("Telegram      : " + ("enabled" if TELEGRAM_TOKEN else "disabled"))
    print("Alpaca        : " + ("connected" if ALPACA_KEY else "NOT SET"))
    print()

    schedule.every().monday.at("06:00").do(morning_briefing)
    schedule.every().tuesday.at("06:00").do(morning_briefing)
    schedule.every().wednesday.at("06:00").do(morning_briefing)
    schedule.every().thursday.at("06:00").do(morning_briefing)
    schedule.every().friday.at("06:00").do(morning_briefing)

    schedule.every(SCAN_INTERVAL_MINUTES).minutes.do(scan)

    morning_briefing()
    scan()

    while True:
        schedule.run_pending()
        time.sleep(30)
