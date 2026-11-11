import os
import time
import requests
import pandas as pd
from datetime import datetime, timezone

# Reads securely from GitHub Secrets
TELEGRAM_TOKEN = os.environ.get("TELEGRAM_TOKEN", "")
TELEGRAM_CHAT_ID = os.environ.get("TELEGRAM_CHAT_ID", "")

MIN_24H_TURNOVER_USDT = 50_000_000  # Minimum $50M 24h volume
MAX_ABS_FUNDING_RATE = 0.0005       # 0.05% max funding rate
BASE_URL = "https://mexc.com"


def send_telegram_alert(message: str):
    if not TELEGRAM_TOKEN or not TELEGRAM_CHAT_ID:
        print(message)
        return

    url = f"https://telegram.org{TELEGRAM_TOKEN}/sendMessage"
    payload = {
        "chat_id": TELEGRAM_CHAT_ID,
        "text": message,
        "parse_mode": "HTML",
        "disable_web_page_preview": True,
    }
    try:
        requests.post(url, json=payload, timeout=10)
    except Exception as e:
        print(f"Telegram error: {e}")


def get_liquid_symbols():
    resp = requests.get(f"{BASE_URL}/ticker", timeout=10).json()
    if not resp.get("success"):
        return []

    liquid_pairs = []
    for item in resp.get("data", []):
        symbol = item.get("symbol", "")
        turnover = float(item.get("amount24", 0))
        funding = float(item.get("fundingRate", 0))
        last_price = float(item.get("lastPrice", 0))

        if symbol.endswith("_USDT") and turnover >= MIN_24H_TURNOVER_USDT:
            liquid_pairs.append({
                "symbol": symbol,
                "turnover": turnover,
                "funding": funding,
                "last_price": last_price,
            })

    liquid_pairs.sort(key=lambda x: x["turnover"], reverse=True)
    return liquid_pairs


def get_klines(symbol: str, interval: str, limit: int = 220) -> pd.DataFrame:
    interval_seconds = {
        "Min5": 300,
        "Hour1": 3600,
        "Hour4": 14400,
    }[interval]

    end_ts = int(time.time())
    start_ts = end_ts - (limit * interval_seconds)

    resp = requests.get(
        f"{BASE_URL}/kline/{symbol}",
        params={"interval": interval, "start": start_ts, "end": end_ts},
        timeout=10,
    ).json()

    if not resp.get("success") or not resp.get("data"):
        return pd.DataFrame()

    data = resp["data"]
    df = pd.DataFrame({
        "time": data["time"],
        "open": pd.to_numeric(data["open"], errors="coerce"),
        "high": pd.to_numeric(data["high"], errors="coerce"),
        "low": pd.to_numeric(data["low"], errors="coerce"),
        "close": pd.to_numeric(data["close"], errors="coerce"),
        "vol": pd.to_numeric(data["vol"], errors="coerce"),
    })

    df["sma20"] = df["close"].rolling(20).mean()
    df["sma200"] = df["close"].rolling(200).mean()
    return df


def check_higher_timeframes(symbol: str):
    df_4h = get_klines(symbol, "Hour4", limit=220)
    df_1h = get_klines(symbol, "Hour1", limit=220)

    if len(df_4h) < 205 or len(df_1h) < 205:
        return None

    c4 = df_4h.iloc[-1]
    c1 = df_1h.iloc[-1]
    c1_prev3 = df_1h.iloc[-4]

    bull_4h = c4["close"] > c4["sma20"] and c4["close"] > c4["sma200"]
    bull_1h = (
        c1["close"] > c1["sma20"]
        and c1["close"] > c1["sma200"]
        and c1["sma20"] > c1_prev3["sma20"]
    )

    bear_4h = c4["close"] < c4["sma20"] and c4["close"] < c4["sma200"]
    bear_1h = (
        c1["close"] < c1["sma20"]
        and c1["close"] < c1["sma200"]
        and c1["sma20"] < c1_prev3["sma20"]
    )

    if bull_4h and bull_1h:
        return "LONG"
    if bear_4h and bear_1h:
        return "SHORT"
    return None


def analyze_5m_setup(symbol: str, bias: str):
    df = get_klines(symbol, "Min5", limit=220)
    if len(df) < 205:
        return None

    df["range"] = df["high"] - df["low"]
    df["avg_range_10"] = df["range"].rolling(10).mean().shift(1)
    df["avg_vol_10"] = df["vol"].rolling(10).mean().shift(1)

    bar = df.iloc[-2]       # Most recently closed 5-minute candle
    prev_bar = df.iloc[-3]  # Candle before it

    if bar["range"] == 0 or pd.isna(bar["sma200"]):
        return None

    # Ensure the closed candle is fresh (closed within the last 6 minutes)
    if (time.time() - bar["time"]) > 660:
        return None

    is_elephant_size = bar["range"] >= (2.0 * bar["avg_range_10"])
    has_volume_surge = bar["vol"] >= (1.5 * bar["avg_vol_10"])
    not_overextended = abs(bar["close"] - bar["sma20"]) <= (2.5 * bar["avg_range_10"])

    if not not_overextended:
        return None

    if bias == "LONG":
        close_pos = (bar["close"] - bar["low"]) / bar["range"]
        if bar["close"] > bar["open"] and close_pos >= 0.75 and is_elephant_size and has_volume_surge:
            if prev_bar["close"] <= prev_bar["sma200"] and bar["close"] > bar["sma200"]:
                return "200 SMA Elephant Breakout (LONG)"
            if bar["sma20"] > bar["sma200"] and prev_bar["low"] <= prev_bar["sma20"] * 1.002 and bar["close"] > bar["sma20"]:
                return "20 SMA Pullback Elephant Ignition (LONG)"

    elif bias == "SHORT":
        close_pos = (bar["high"] - bar["close"]) / bar["range"]
        if bar["close"] < bar["open"] and close_pos >= 0.75 and is_elephant_size and has_volume_surge:
            if prev_bar["close"] >= prev_bar["sma200"] and bar["close"] < bar["sma200"]:
                return "200 SMA Elephant Breakdown (SHORT)"
            if bar["sma20"] < bar["sma200"] and prev_bar["high"] >= prev_bar["sma20"] * 0.998 and bar["close"] < bar["sma20"]:
                return "20 SMA Rally Elephant Ignition (SHORT)"

    return None


def run_single_scan():
    utc_now = datetime.now(timezone.utc).strftime("%Y-%m-%d %H:%M:%S UTC")
    pairs = get_liquid_symbols()
    print(f"[{utc_now}] Scanning {len(pairs)} liquid MEXC pairs...")

    for pair in pairs:
        symbol = pair["symbol"]
        funding = pair["funding"]

        bias = check_higher_timeframes(symbol)
        if not bias:
            continue

        if bias == "LONG" and funding > MAX_ABS_FUNDING_RATE:
            continue
        if bias == "SHORT" and funding < -MAX_ABS_FUNDING_RATE:
            continue

        setup_name = analyze_5m_setup(symbol, bias)
        if setup_name:
            mexc_link = f"https://mexc.com{symbol}"
            msg = (
                f"<b>VELEZ SETUP DETECTED: {symbol}</b>\n"
                f"<b>Setup:</b> {setup_name}\n"
                f"<b>HTF Bias (4H & 1H):</b> {bias}\n"
                f"<b>Price:</b> ${pair['last_price']}\n"
                f"<b>24h Vol:</b> ${pair['turnover']:,.0f}\n"
                f"<b>Funding:</b> {funding * 100:.4f}%\n"
                f"<a href='{mexc_link}'>Open Chart on MEXC</a>"
            )
            send_telegram_alert(msg)

        time.sleep(0.2)


if __name__ == "__main__":
    run_single_scan()
