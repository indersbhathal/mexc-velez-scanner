import os
import time
import requests
import pandas as pd
from datetime import datetime, timezone

TELEGRAM_TOKEN = os.environ.get("TELEGRAM_TOKEN", "")
TELEGRAM_CHAT_ID = os.environ.get("TELEGRAM_CHAT_ID", "")

MIN_24H_TURNOVER_USDT = 50_000_000
SCAN_TOP_N_COINS = 35
OKX_BASE_URL = "https://okx.com"


def fmt(p: float) -> str:
    return f"{p:,.2f}" if p >= 1000 else (f"{p:.4f}" if p >= 1 else f"{p:.6f}")


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
    resp = requests.get(f"{OKX_BASE_URL}/market/tickers", params={"instType": "SWAP"}, timeout=15)
    if resp.status_code != 200:
        return []
    payload = resp.json()
    if payload.get("code") != "0":
        return []

    pairs = []
    for item in payload.get("data", []):
        inst_id = item.get("instId", "")
        if not inst_id.endswith("-USDT-SWAP"):
            continue
        last_price = float(item.get("last", 0))
        turnover = float(item.get("volCcy24h", 0)) * last_price
        if turnover >= MIN_24H_TURNOVER_USDT:
            base = inst_id.split("-")[0]
            pairs.append({
                "inst_id": inst_id,
                "mexc_symbol": f"{base}_USDT",
                "turnover": turnover,
                "last_price": last_price,
            })

    pairs.sort(key=lambda x: x["turnover"], reverse=True)
    return pairs[:SCAN_TOP_N_COINS]


def get_klines(inst_id: str, bar: str, limit: int = 220) -> pd.DataFrame:
    resp = requests.get(
        f"{OKX_BASE_URL}/market/candles",
        params={"instId": inst_id, "bar": bar, "limit": str(limit)},
        timeout=15,
    )
    if resp.status_code != 200:
        return pd.DataFrame()
    payload = resp.json()
    rows = payload.get("data", [])
    if payload.get("code") != "0" or not rows:
        return pd.DataFrame()

    rows.reverse()
    df = pd.DataFrame(rows, columns=["ts", "open", "high", "low", "close", "vol", "v1", "v2", "c"])
    df["time"] = pd.to_numeric(df["ts"], errors="coerce") / 1000.0
    for col in ["open", "high", "low", "close", "vol"]:
        df[col] = pd.to_numeric(df[col], errors="coerce")
    df["sma20"] = df["close"].rolling(20).mean()
    df["sma200"] = df["close"].rolling(200).mean()
    return df


def check_higher_timeframes(inst_id: str):
    df_4h = get_klines(inst_id, "4H", 220)
    df_1h = get_klines(inst_id, "1H", 220)
    if len(df_4h) < 205 or len(df_1h) < 205:
        return None

    c4, c1, c1_prev = df_4h.iloc[-1], df_1h.iloc[-1], df_1h.iloc[-4]
    if c4["close"] > c4["sma20"] and c4["close"] > c4["sma200"] and c1["close"] > c1["sma20"] and c1["close"] > c1["sma200"] and c1["sma20"] > c1_prev["sma20"]:
        return "LONG"
    if c4["close"] < c4["sma20"] and c4["close"] < c4["sma200"] and c1["close"] < c1["sma20"] and c1["close"] < c1["sma200"] and c1["sma20"] < c1_prev["sma20"]:
        return "SHORT"
    return None


def analyze_5m_setup(inst_id: str, bias: str):
    df = get_klines(inst_id, "5m", 220)
    if len(df) < 205:
        return None

    df["range"] = df["high"] - df["low"]
    df["avg_r"] = df["range"].rolling(10).mean().shift(1)
    df["avg_v"] = df["vol"].rolling(10).mean().shift(1)

    bar, b1, b2, b3 = df.iloc[-2], df.iloc[-3], df.iloc[-4], df.iloc[-5]
    if bar["range"] == 0 or pd.isna(bar["sma200"]) or (time.time() - bar["time"]) > 660:
        return None
    if abs(bar["close"] - bar["sma20"]) > (2.5 * bar["avg_r"]):
        return None

    elephant = bar["range"] >= (2.0 * bar["avg_r"])
    vol_surge = bar["vol"] >= (1.5 * bar["avg_v"])
    pinch = abs(bar["sma20"] - bar["sma200"]) / bar["close"] <= 0.0035
    lower_wick = min(bar["open"], bar["close"]) - bar["low"]
    upper_wick = bar["high"] - max(bar["open"], bar["close"])
    setup = None

    if bias == "LONG":
        c_pos = (bar["close"] - bar["low"]) / bar["range"]
        bull_bar = bar["close"] > bar["open"] and c_pos >= 0.75

        if bull_bar and elephant and vol_surge and pinch and bar["close"] > bar["sma200"]:
            setup = "20/200 MA Pinch Elephant Breakout (LONG)"
        elif bull_bar and elephant and vol_surge and b1["close"] <= b1["sma200"] and bar["close"] > bar["sma200"]:
            setup = "200 SMA Elephant Breakout (LONG)"
        elif bull_bar and elephant and vol_surge and bar["sma20"] > bar["sma200"] and b1["low"] <= b1["sma20"] * 1.002 and bar["close"] > bar["sma20"]:
            setup = "20 SMA Pullback Elephant Ignition (LONG)"
        elif bar["close"] > bar["open"] and c_pos >= 0.65 and bar["sma20"] > bar["sma200"] and b1["close"] < b1["open"] and b2["close"] < b2["open"] and b3["close"] < b3["open"] and b1["low"] <= b1["sma20"] * 1.0025 and bar["close"] > b1["high"]:
            setup = "3+ Bar Color Change at 20 SMA (LONG)"
        elif (lower_wick / bar["range"]) >= 0.60 and c_pos >= 0.70 and bar["range"] >= (1.2 * bar["avg_r"]) and ((bar["low"] <= bar["sma20"] * 1.001 and bar["close"] > bar["sma20"]) or (bar["low"] <= bar["sma200"] * 1.001 and bar["close"] > bar["sma200"])):
            setup = "Bottoming Tail Rejection at MA (LONG)"

        if setup:
            risk = max(bar["high"] - bar["low"], bar["close"] * 0.001)
            return {"setup": setup, "entry": bar["high"], "stop": bar["low"], "target": bar["high"] + (2.0 * risk), "sma20": bar["sma20"]}

    elif bias == "SHORT":
        c_pos = (bar["high"] - bar["close"]) / bar["range"]
        bear_bar = bar["close"] < bar["open"] and c_pos >= 0.75

        if bear_bar and elephant and vol_surge and pinch and bar["close"] < bar["sma200"]:
            setup = "20/200 MA Pinch Elephant Breakdown (SHORT)"
        elif bear_bar and elephant and vol_surge and b1["close"] >= b1["sma200"] and bar["close"] < bar["sma200"]:
            setup = "200 SMA Elephant Breakdown (SHORT)"
        elif bear_bar and elephant and vol_surge and bar["sma20"] < bar["sma200"] and b1["high"] >= b1["sma20"] * 0.998 and bar["close"] < bar["sma20"]:
            setup = "20 SMA Rally Elephant Ignition (SHORT)"
        elif bar["close"] < bar["open"] and c_pos >= 0.65 and bar["sma20"] < bar["sma200"] and b1["close"] > b1["open"] and b2["close"] > b2["open"] and b3["close"] > b3["open"] and b1["high"] >= b1["sma20"] * 0.9975 and bar["close"] < b1["low"]:
            setup = "3+ Bar Color Change at 20 SMA (SHORT)"
        elif (upper_wick / bar["range"]) >= 0.60 and c_pos >= 0.70 and bar["range"] >= (1.2 * bar["avg_r"]) and ((bar["high"] >= bar["sma20"] * 0.999 and bar["close"] < bar["sma20"]) or (bar["high"] >= bar["sma200"] * 0.999 and bar["close"] < bar["sma200"])):
            setup = "Topping Tail Rejection at MA (SHORT)"

        if setup:
            risk = max(bar["high"] - bar["low"], bar["close"] * 0.001)
            return {"setup": setup, "entry": bar["low"], "stop": bar["high"], "target": bar["low"] - (2.0 * risk), "sma20": bar["sma20"]}

    return None


def run_single_scan():
    utc_now = datetime.now(timezone.utc).strftime("%Y-%m-%d %H:%M:%S UTC")
    pairs = get_liquid_symbols()
    print(f"[{utc_now}] Scanning top {len(pairs)} liquid perpetual pairs...")

    matches = 0
    for pair in pairs:
        bias = check_higher_timeframes(pair["inst_id"])
        if not bias:
            continue
        res = analyze_5m_setup(pair["inst_id"], bias)
        if res:
            matches += 1
            sym = pair["mexc_symbol"]
            msg = (
                f"<b>VELEZ SETUP: {sym}</b>\n"
                f"<b>Pattern:</b> {res['setup']}\n"
                f"<b>HTF Bias (4H & 1H):</b> {bias}\n"
                f"<b>Price:</b> ${fmt(pair['last_price'])}\n"
                f"<b>Trigger Entry:</b> ${fmt(res['entry'])}\n"
                f"<b>Signal Bar Stop:</b> ${fmt(res['stop'])}\n"
                f"<b>2R Target:</b> ${fmt(res['target'])}\n"
                f"<b>5M 20 SMA:</b> ${fmt(res['sma20'])}\n"
                f"<b>24h Vol:</b> ${pair['turnover']:,.0f}\n"
                f"<a href='https://mexc.com{sym}'>Open Chart on MEXC</a>"
            )
            send_telegram_alert(msg)
        time.sleep(0.15)

    print(f"Scan finished cleanly. Total setups found: {matches}")


if __name__ == "__main__":
    run_single_scan()
