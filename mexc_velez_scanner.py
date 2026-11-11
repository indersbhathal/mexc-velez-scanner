import os
import time
import requests
import pandas as pd
from datetime import datetime, timezone

TELEGRAM_TOKEN = os.environ.get("TELEGRAM_TOKEN", "")
TELEGRAM_CHAT_ID = os.environ.get("TELEGRAM_CHAT_ID", "")

# Scanner Settings
MIN_24H_TURNOVER_USDT = 50_000_000  # Minimum $50M 24h volume
SCAN_TOP_N_COINS = 35               # Scan top 35 highest-volume perpetuals
OKX_BASE_URL = "https://okx.com"


def send_telegram_alert(message: str):
    if not TELEGRAM_TOKEN or not TELEGRAM_CHAT_ID:
        print("Missing TELEGRAM_TOKEN or TELEGRAM_CHAT_ID secret!")
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
        r = requests.post(url, json=payload, timeout=10)
        if r.status_code != 200:
            print(f"Telegram API error ({r.status_code}): {r.text}")
    except Exception as e:
        print(f"Telegram connection error: {e}")


def get_liquid_symbols():
    """Fetches high-volume USDT perpetual swaps from OKX public market API."""
    url = f"{OKX_BASE_URL}/market/tickers"
    resp = requests.get(url, params={"instType": "SWAP"}, timeout=15)
    if resp.status_code != 200:
        print(f"API HTTP error {resp.status_code}: {resp.text[:200]}")
        return []

    payload = resp.json()
    if payload.get("code") != "0":
        print(f"API response error: {payload}")
        return []

    liquid_pairs = []
    for item in payload.get("data", []):
        inst_id = item.get("instId", "")  # Format: BTC-USDT-SWAP
        if not inst_id.endswith("-USDT-SWAP"):
            continue

        vol_ccy_24h = float(item.get("volCcy24h", 0))  # Volume in base coin
        last_price = float(item.get("last", 0))
        turnover_usdt = vol_ccy_24h * last_price

        if turnover_usdt >= MIN_24H_TURNOVER_USDT:
            base_coin = inst_id.split("-")[0]
            mexc_symbol = f"{base_coin}_USDT"
            liquid_pairs.append({
                "inst_id": inst_id,
                "mexc_symbol": mexc_symbol,
                "turnover": turnover_usdt,
                "last_price": last_price,
            })

    liquid_pairs.sort(key=lambda x: x["turnover"], reverse=True)
    return liquid_pairs[:SCAN_TOP_N_COINS]


def get_klines(inst_id: str, bar: str, limit: int = 220) -> pd.DataFrame:
    """Fetches historical candlesticks (bar: '5m', '1H', '4H')."""
    url = f"{OKX_BASE_URL}/market/candles"
    params = {"instId": inst_id, "bar": bar, "limit": str(limit)}
    resp = requests.get(url, params=params, timeout=15)
    if resp.status_code != 200:
        return pd.DataFrame()

    payload = resp.json()
    rows = payload.get("data", [])
    if payload.get("code") != "0" or not rows:
        return pd.DataFrame()

    # OKX returns newest candle first; reverse to chronological order
    rows.reverse()

    df = pd.DataFrame(rows, columns=[
        "ts", "open", "high", "low", "close", "vol", "volCcy", "volCcyQuote", "confirm"
    ])

    df["time"] = pd.to_numeric(df["ts"], errors="coerce") / 1000.0
    for col in ["open", "high", "low", "close", "vol"]:
        df[col] = pd.to_numeric(df[col], errors="coerce")

    df["sma20"] = df["close"].rolling(20).mean()
    df["sma200"] = df["close"].rolling(200).mean()
    return df


def check_higher_timeframes(inst_id: str):
    """Checks 4H and 1H alignment for BULLISH or BEARISH bias."""
    df_4h = get_klines(inst_id, "4H", limit=220)
    df_1h = get_klines(inst_id, "1H", limit=220)

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


def analyze_5m_setup(inst_id: str, bias: str):
    """Scans 5M chart for Velez Elephant Bar 200 SMA Breakouts or 20 SMA Pullbacks."""
    df = get_klines(inst_id, "5m", limit=220)
    if len(df) < 205:
        return None

    df["range"] = df["high"] - df["low"]
    df["avg_range_10"] = df["range"].rolling(10).mean().shift(1)
    df["avg_vol_10"] = df["vol"].rolling(10).mean().shift(1)

    bar = df.iloc[-2]       # Most recently closed 5-minute candle
    prev_bar = df.iloc[-3]  # Candle before it

    if bar["range"] == 0 or pd.isna(bar["sma200"]):
        return None

    # Ensure the closed candle is fresh (within the last 11 minutes)
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
    print(f"[{utc_now}] Scanning top {len(pairs)} liquid perpetual pairs...")

    matches = 0
    for pair in pairs:
        inst_id = pair["inst_id"]
        mexc_symbol = pair["mexc_symbol"]

        bias = check_higher_timeframes(inst_id)
        if not bias:
            continue

        setup_name = analyze_5m_setup(inst_id, bias)
        if setup_name:
            matches += 1
            print(f"MATCH: {mexc_symbol} -> {setup_name}")
            mexc_link = f"https://mexc.com{mexc_symbol}"
            msg = (
                f"<b>VELEZ SETUP DETECTED: {mexc_symbol}</b>\n"
                f"<b>Setup:</b> {setup_name}\n"
                f"<b>HTF Bias (4H & 1H):</b> {bias}\n"
                f"<b>Price:</b> ${pair['last_price']}\n"
                f"<b>24h Vol:</b> ${pair['turnover']:,.0f}\n"
                f"<a href='{mexc_link}'>Open Chart on MEXC</a>"
            )
            send_telegram_alert(msg)

        time.sleep(0.15)

    print(f"Scan finished cleanly. Total setups found: {matches}")


if __name__ == "__main__":
    run_single_scan()
