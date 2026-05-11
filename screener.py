#!/usr/bin/env python3
"""
Mercati Screener — backend autonomo.

Scansiona crypto (Binance.US) + azioni (Yahoo Finance via yfinance),
calcola RSI/MA20/MACD/Bollinger/Volume, e manda alert Telegram quando un
asset entra nella TOP 5 mean-reversion.

Eseguito periodicamente da GitHub Actions. Lo stato (TOP 5 precedente) è
persistito in `state.json` committato sul repo tra un'esecuzione e l'altra.

Variabili d'ambiente (segrete in GitHub):
    TELEGRAM_BOT_TOKEN    obbligatoria
    TELEGRAM_CHAT_ID      obbligatoria
"""

from __future__ import annotations

import json
import math
import os
import sys
from datetime import datetime, timezone
from pathlib import Path

import requests

# yfinance per i dati azionari (gratis, illimitato, no API key)
try:
    import yfinance as yf
    YFINANCE_AVAILABLE = True
except ImportError:
    YFINANCE_AVAILABLE = False
    print("! yfinance non installato, azioni saltate")

# ── CONFIG ────────────────────────────────────────────────────────────────

BOT_TOKEN = os.environ.get("TELEGRAM_BOT_TOKEN", "").strip()
CHAT_ID = os.environ.get("TELEGRAM_CHAT_ID", "").strip()

STATE_FILE = Path("state.json")

# Pesi dei segnali
WEIGHTS = {
    "rsi": 1.0,
    "drop": 1.0,
    "momentum": 0.6,
    "ma": 0.8,
    "volume": 0.7,
    "macd": 1.0,
    "bollinger": 1.0,
}

# Universo azioni — simboli yfinance, tutti disponibili su Trade Republic
STOCK_UNIVERSE = [
    # US
    ("AAPL", "Apple"), ("MSFT", "Microsoft"), ("GOOGL", "Alphabet"),
    ("AMZN", "Amazon"), ("META", "Meta Platforms"), ("NVDA", "NVIDIA"),
    ("TSLA", "Tesla"), ("AMD", "AMD"), ("NFLX", "Netflix"),
    ("JPM", "JPMorgan"), ("V", "Visa"), ("MA", "Mastercard"),
    ("DIS", "Disney"), ("KO", "Coca-Cola"), ("PEP", "PepsiCo"),
    # EU
    ("ASML.AS", "ASML Holding"), ("SAP.DE", "SAP SE"),
    ("MC.PA", "LVMH"), ("OR.PA", "L'Oreal"), ("AIR.PA", "Airbus"),
    ("SIE.DE", "Siemens"), ("ALV.DE", "Allianz"),
    # IT (Borsa Italiana)
    ("ENI.MI", "ENI"), ("ENEL.MI", "Enel"),
    ("ISP.MI", "Intesa Sanpaolo"), ("UCG.MI", "UniCredit"),
    ("STLAM.MI", "Stellantis"), ("STM.MI", "STMicroelectronics"),
    ("RACE.MI", "Ferrari"), ("MONC.MI", "Moncler"), ("LDO.MI", "Leonardo"),
]

BLACKLIST_BASES = {
    "USDC", "FDUSD", "TUSD", "BUSD", "DAI",
    "EUR", "GBP", "TRY", "JPY", "RUB", "UAH", "BIDR", "AEUR",
}
BLACKLIST_SUFFIXES = ("UPUSDT", "DOWNUSDT", "BULLUSDT", "BEARUSDT")

# Calibrato per Binance.US (volumi molto inferiori a Binance.com)
MIN_QUOTE_VOL_USDT = 200_000
TOP_CRYPTO_TO_ENRICH = 60
TOP_N = 10            # quanti asset mostrare nella dashboard e tracciare per gli alert


# ── INDICATORI ────────────────────────────────────────────────────────────

def calc_rsi(closes: list[float], period: int = 14) -> float | None:
    if len(closes) < period + 1:
        return None
    gains = sum(max(0, closes[i] - closes[i - 1]) for i in range(1, period + 1))
    losses = sum(max(0, closes[i - 1] - closes[i]) for i in range(1, period + 1))
    avg_g = gains / period
    avg_l = losses / period
    for i in range(period + 1, len(closes)):
        ch = closes[i] - closes[i - 1]
        g = max(0, ch)
        l = max(0, -ch)
        avg_g = (avg_g * (period - 1) + g) / period
        avg_l = (avg_l * (period - 1) + l) / period
    if avg_l == 0:
        return 100.0
    rs = avg_g / avg_l
    return 100 - 100 / (1 + rs)


def sma(arr: list[float], period: int) -> float | None:
    if len(arr) < period:
        return None
    return sum(arr[-period:]) / period


def ema_series(arr: list[float], period: int) -> list[float | None]:
    if len(arr) < period:
        return []
    k = 2 / (period + 1)
    out: list[float | None] = [None] * len(arr)
    out[period - 1] = sum(arr[:period]) / period
    for i in range(period, len(arr)):
        out[i] = arr[i] * k + out[i - 1] * (1 - k)
    return out


def calc_macd(closes: list[float], fast=12, slow=26, signal=9) -> dict | None:
    if len(closes) < slow + signal:
        return None
    fast_e = ema_series(closes, fast)
    slow_e = ema_series(closes, slow)
    macd_line = [
        (fast_e[i] - slow_e[i]) if (fast_e[i] is not None and slow_e[i] is not None) else None
        for i in range(len(closes))
    ]
    valid = [v for v in macd_line if v is not None]
    sig_e = ema_series(valid, signal)
    offset = len(macd_line) - len(valid)
    signal_line: list[float | None] = [None] * len(closes)
    for i, v in enumerate(sig_e):
        if v is not None:
            signal_line[i + offset] = v
    macd = macd_line[-1]
    sig = signal_line[-1]
    if macd is None or sig is None:
        return None
    histogram = macd - sig
    crossover = None
    if macd_line[-2] is not None and signal_line[-2] is not None:
        if macd_line[-2] <= signal_line[-2] and macd > sig:
            crossover = "bull"
        elif macd_line[-2] >= signal_line[-2] and macd < sig:
            crossover = "bear"
    return {"macd": macd, "signal": sig, "histogram": histogram, "crossover": crossover}


def calc_bollinger(closes: list[float], period=20, std_devs=2) -> dict | None:
    if len(closes) < period:
        return None
    s = closes[-period:]
    mean = sum(s) / period
    variance = sum((v - mean) ** 2 for v in s) / period
    sd = math.sqrt(variance)
    upper = mean + std_devs * sd
    lower = mean - std_devs * sd
    last = closes[-1]
    pct_b = (last - lower) / (upper - lower) if upper != lower else 0.5
    return {"upper": upper, "lower": lower, "middle": mean, "percent_b": pct_b}


# ── SCORING ───────────────────────────────────────────────────────────────

def compute_score(asset: dict, w: dict = WEIGHTS) -> tuple[float, list]:
    score = 0.0
    signals = []

    rsi = asset.get("rsi")
    if rsi is not None:
        if rsi < 30:
            pts = ((30 - rsi) / 30) * 100 * w["rsi"]
            score += pts
            signals.append(("bull", "RSI ipervenduto", f"{rsi:.1f}"))
        elif rsi > 70:
            pts = -((rsi - 70) / 30) * 60 * w["rsi"]
            score += pts
            signals.append(("bear", "RSI ipercomprato", f"{rsi:.1f}"))

    ch = asset.get("change_24h")
    if ch is not None:
        if ch < -3:
            pts = min(abs(ch), 25) * 3 * w["drop"]
            score += pts
            signals.append(("bull", "Crollo 24h", f"{ch:.2f}%"))
        elif ch > 5:
            pts = min(ch, 25) * 1 * w["momentum"]
            score += pts
            signals.append(("bull", "Momentum", f"+{ch:.2f}%"))

    dist_ma = asset.get("dist_from_ma20")
    if dist_ma is not None and dist_ma < -4:
        pts = min(abs(dist_ma), 20) * 2.5 * w["ma"]
        score += pts
        signals.append(("bull", "Sotto MA20", f"{dist_ma:.1f}%"))

    vr = asset.get("volume_ratio")
    if vr is not None and vr > 1.5:
        pts = min(vr, 6) * 6 * w["volume"]
        score += pts
        signals.append(("bull", f"Volume {vr:.1f}x", ""))

    macd = asset.get("macd")
    if macd:
        if macd.get("crossover") == "bull":
            pts = 35 * w["macd"]
            score += pts
            signals.append(("bull", "MACD cross UP", f"{macd['histogram']:.4f}"))
        elif macd.get("crossover") == "bear":
            pts = -25 * w["macd"]
            score += pts
            signals.append(("bear", "MACD cross DOWN", f"{macd['histogram']:.4f}"))
        elif macd["histogram"] > 0 and macd["macd"] < 0:
            pts = 12 * w["macd"]
            score += pts
            signals.append(("bull", "MACD ripresa", ""))

    bb = asset.get("bollinger")
    if bb:
        pb = bb["percent_b"]
        if pb < 0.05:
            pts = 50 * w["bollinger"]
            score += pts
            signals.append(("bull", "Sotto BB inf", f"%B {pb:.2f}"))
        elif pb < 0.2:
            pts = 25 * w["bollinger"]
            score += pts
            signals.append(("bull", "BB inferiore", f"%B {pb:.2f}"))
        elif pb > 0.95:
            pts = -30 * w["bollinger"]
            score += pts
            signals.append(("bear", "Sopra BB sup", f"%B {pb:.2f}"))

    return score, signals


# ── BINANCE.US ────────────────────────────────────────────────────────────

def fetch_binance_universe() -> list[dict]:
    r = requests.get("https://api.binance.us/api/v3/ticker/24hr", timeout=20)
    r.raise_for_status()
    out = []
    for t in r.json():
        sym = t["symbol"]
        if not sym.endswith("USDT"):
            continue
        base = sym[:-4]
        if base in BLACKLIST_BASES:
            continue
        if any(sym.endswith(s) for s in BLACKLIST_SUFFIXES):
            continue
        try:
            qv = float(t["quoteVolume"])
        except (KeyError, ValueError):
            continue
        if qv < MIN_QUOTE_VOL_USDT:
            continue
        out.append({
            "type": "crypto",
            "symbol": sym,
            "name": base,
            "price": float(t["lastPrice"]),
            "change_24h": float(t["priceChangePercent"]),
            "quote_volume": qv,
        })
    out.sort(key=lambda x: x["quote_volume"], reverse=True)
    return out


def enrich_crypto(asset: dict) -> dict:
    try:
        r = requests.get(
            "https://api.binance.us/api/v3/klines",
            params={"symbol": asset["symbol"], "interval": "4h", "limit": 80},
            timeout=15,
        )
        r.raise_for_status()
        klines = r.json()
        closes = [float(k[4]) for k in klines]
        volumes = [float(k[5]) for k in klines]
        asset["rsi"] = calc_rsi(closes)
        ma20 = sma(closes, 20)
        asset["dist_from_ma20"] = (asset["price"] - ma20) / ma20 * 100 if ma20 else None
        if len(volumes) >= 30:
            recent = sum(volumes[-3:]) / 3
            avg = sum(volumes[-30:-3]) / 27
            asset["volume_ratio"] = recent / avg if avg > 0 else None
        asset["macd"] = calc_macd(closes)
        asset["bollinger"] = calc_bollinger(closes)
    except Exception as e:
        print(f"  ! errore enrich {asset['symbol']}: {e}")
    return asset


# ── AZIONI (YFINANCE) ─────────────────────────────────────────────────────

def fetch_stock(symbol: str, name: str) -> dict | None:
    if not YFINANCE_AVAILABLE:
        return None
    try:
        t = yf.Ticker(symbol)
        hist = t.history(period="3mo", auto_adjust=True, prepost=False)
        if hist is None or hist.empty or len(hist) < 30:
            return None
        closes = [float(x) for x in hist["Close"].tolist()]
        volumes = [float(x) for x in hist["Volume"].tolist()]
        current_price = closes[-1]
        prev_close = closes[-2] if len(closes) >= 2 else current_price
        change_24h = (
            (current_price - prev_close) / prev_close * 100 if prev_close else 0.0
        )

        a: dict = {
            "type": "stock",
            "symbol": symbol,
            "name": name,
            "price": current_price,
            "change_24h": change_24h,
            "rsi": calc_rsi(closes),
            "macd": calc_macd(closes),
            "bollinger": calc_bollinger(closes),
        }
        ma20 = sma(closes, 20)
        a["dist_from_ma20"] = (current_price - ma20) / ma20 * 100 if ma20 else None
        if len(volumes) >= 30:
            recent = sum(volumes[-3:]) / 3
            avg = sum(volumes[-30:-3]) / 27
            a["volume_ratio"] = recent / avg if avg > 0 else None
        return a
    except Exception as e:
        print(f"  ! errore stock {symbol}: {e}")
        return None


# ── TELEGRAM ──────────────────────────────────────────────────────────────

def send_telegram(text: str) -> bool:
    if not BOT_TOKEN or not CHAT_ID:
        print("  ! Telegram non configurato (mancano TELEGRAM_BOT_TOKEN/CHAT_ID)")
        return False
    try:
        r = requests.post(
            f"https://api.telegram.org/bot{BOT_TOKEN}/sendMessage",
            json={
                "chat_id": CHAT_ID,
                "text": text,
                "parse_mode": "Markdown",
                "disable_web_page_preview": True,
            },
            timeout=15,
        )
        data = r.json()
        if not data.get("ok"):
            print(f"  ! Telegram error: {data.get('description')}")
            return False
        return True
    except Exception as e:
        print(f"  ! Telegram exception: {e}")
        return False


# ── STATE / MAIN ──────────────────────────────────────────────────────────

def asset_id(a: dict) -> str:
    return f"{a['type']}:{a['symbol']}"


def format_alert(a: dict, score: float, signals: list) -> str:
    venue = "Binance / Trade Republic" if a["type"] == "crypto" else "Trade Republic"
    bull = [s for s in signals if s[0] == "bull"][:3]
    sig_lines = "\n".join(f"- {s[1]}{(' ' + s[2]) if s[2] else ''}" for s in bull)
    price = a["price"]
    price_str = f"{price:.6f}" if price < 1 else f"{price:.2f}"
    ch = a.get("change_24h") or 0
    ch_str = f"{'+' if ch >= 0 else ''}{ch:.2f}%"
    return (
        f"*Nuovo in TOP {TOP_N}*\n"
        f"*{a['name']}* (`{a['symbol']}`)\n"
        f"Score: `{int(round(score))}`\n"
        f"Prezzo: `{price_str}` ({ch_str})\n\n"
        f"{sig_lines}\n\n"
        f"{venue}"
    )


def load_state() -> dict:
    if STATE_FILE.exists():
        try:
            return json.loads(STATE_FILE.read_text())
        except json.JSONDecodeError:
            pass
    return {"previous_top5": [], "last_run": None}


def save_state(state: dict) -> None:
    STATE_FILE.write_text(json.dumps(state, indent=2))


def save_web_data(
    combined_top: list,
    crypto_top: list,
    stocks_top: list,
    stats: dict,
) -> None:
    """Scrive docs/data.json per la dashboard web.
    Ognuna delle tre liste è già ordinata e limitata a TOP_N elementi.
    """
    docs = Path("docs")
    docs.mkdir(exist_ok=True)

    def serialize(item):
        a, score, signals = item
        return {
            "type": a["type"],
            "symbol": a["symbol"],
            "name": a["name"],
            "price": a["price"],
            "change_24h": a.get("change_24h"),
            "rsi": a.get("rsi"),
            "dist_from_ma20": a.get("dist_from_ma20"),
            "volume_ratio": a.get("volume_ratio"),
            "macd": a.get("macd"),
            "bollinger": a.get("bollinger"),
            "score": round(score, 1),
            "signals": [
                {"kind": s[0], "label": s[1], "value": s[2]} for s in signals
            ],
        }

    payload = {
        "generated_at": datetime.now(timezone.utc).isoformat(timespec="seconds"),
        "stats": stats,
        "lists": {
            "combined": [serialize(x) for x in combined_top],
            "crypto": [serialize(x) for x in crypto_top],
            "stocks": [serialize(x) for x in stocks_top],
        },
    }
    (docs / "data.json").write_text(json.dumps(payload, indent=2, default=str))


def main() -> int:
    print(f"[{datetime.now(timezone.utc).isoformat(timespec='seconds')}] Avvio screener")

    # ── Crypto ──
    print("Crypto (Binance.US)...")
    try:
        universe = fetch_binance_universe()
    except Exception as e:
        print(f"!! errore Binance universe: {e}")
        universe = []
    print(f"  {len(universe)} pair USDT con volume > {MIN_QUOTE_VOL_USDT/1e3:.0f}K$")
    top = universe[:TOP_CRYPTO_TO_ENRICH]
    enriched = [enrich_crypto(a) for a in top]

    # ── Stocks ──
    stocks: list[dict] = []
    if YFINANCE_AVAILABLE:
        print("Stocks (Yahoo Finance)...")
        for sym, name in STOCK_UNIVERSE:
            a = fetch_stock(sym, name)
            if a:
                stocks.append(a)
        print(f"  {len(stocks)}/{len(STOCK_UNIVERSE)} azioni recuperate")
    else:
        print("Stocks: skip (yfinance non disponibile)")

    # ── Scoring ──
    # Calcoliamo lo score per ogni asset e costruiamo tre classifiche
    def score_and_sort(assets):
        out = []
        for a in assets:
            score, signals = compute_score(a)
            if score > 0:
                out.append((a, score, signals))
        out.sort(key=lambda x: x[1], reverse=True)
        return out

    scored_combined = score_and_sort(enriched + stocks)
    scored_crypto = score_and_sort(enriched)
    scored_stocks = score_and_sort(stocks)

    combined_top = scored_combined[:TOP_N]
    crypto_top = scored_crypto[:TOP_N]
    stocks_top = scored_stocks[:TOP_N]

    print(f"\nTOP {TOP_N} (combinati):")
    if not combined_top:
        print("  (nessun segnale forte)")
    for a, score, _ in combined_top:
        ch = a.get("change_24h") or 0
        rsi = a.get("rsi") or 0
        print(f"  {int(round(score)):>4}  {a['symbol']:<12} {ch:+6.2f}%  rsi={rsi:5.1f}")

    # ── Alert ──
    # Si triggera quando un asset entra nella TOP N combinata
    state = load_state()
    prev_ids = set(state.get("previous_top_n", state.get("previous_top5", [])))
    prev_count = len(prev_ids)
    current_ids = [asset_id(a) for a, _, _ in combined_top]

    if not prev_ids:
        print("\nPrimo run: registro lo stato senza inviare alert.")
    elif prev_count != TOP_N and prev_count != 0:
        # Cambio del N dimensione lista (es. da 5 a 10) → skip per evitare spam
        print(f"\nDimensione TOP cambiata ({prev_count}→{TOP_N}): skip alert per questo run.")
    else:
        newcomers = [(a, s, sig) for a, s, sig in combined_top if asset_id(a) not in prev_ids]
        if newcomers:
            print(f"\n{len(newcomers)} new entries -> invio alert Telegram:")
            for a, score, signals in newcomers:
                print(f"  -> {a['symbol']} (score {int(round(score))})")
                send_telegram(format_alert(a, score, signals))
        else:
            print(f"\nTOP {TOP_N} invariata, nessun alert.")

    state["previous_top_n"] = current_ids
    state.pop("previous_top5", None)  # cleanup chiave vecchia
    state["last_run"] = datetime.now(timezone.utc).isoformat(timespec="seconds")
    save_state(state)

    # Salva il JSON per la dashboard web
    save_web_data(
        combined_top=combined_top,
        crypto_top=crypto_top,
        stocks_top=stocks_top,
        stats={
            "crypto_count": len(enriched),
            "stocks_count": len(stocks),
            "scored_count": len(scored_combined),
            "top_n": TOP_N,
        },
    )

    print("Fatto.")
    return 0


if __name__ == "__main__":
    sys.exit(main())
