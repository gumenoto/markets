#!/usr/bin/env python3
"""
Mercati Screener v2 — tre modalità di lettura del mercato.

  RIMBALZI       mean-reversion (logica originale, invariata): ipervenduti,
                 sotto MA20 / Bollinger inferiore, candidati a un rimbalzo.
  ACCELERAZIONI  momentum/breakout: rottura del massimo a 20/55 giorni con
                 volume, forza relativa vs BTC o vs indice, trend (ADX).
  IN CARICA      watchlist pre-breakout: volatilità compressa (Bollinger
                 squeeze), prezzo vicino al tetto del range, accumulo volumi.
                 Nessun alert all'ingresso: alert solo quando "scatta".

Universo:
  crypto     CoinGecko /coins/markets (top 250 per capitalizzazione, filtrate
             per liquidità) + candele da Binance (mirror dati pubblico),
             Coinbase, Kraken, Binance.US come fallback in cascata.
             Se CoinGecko non risponde: universo di riserva da Binance.US.
  brokerage  azioni US/EU/IT + commodity (futures) via yfinance.

Ogni segnale nuovo viene registrato in `signals_log.json` e seguito a 1/3/7
giorni: così sappiamo con i numeri quale modalità funziona davvero.

Variabili d'ambiente (secrets GitHub):
    TELEGRAM_BOT_TOKEN   obbligatoria per gli alert
    TELEGRAM_CHAT_ID     obbligatoria per gli alert
    COINGECKO_API_KEY    opzionale (chiave "Demo" gratuita, alza i limiti)
    DASHBOARD_URL        opzionale (link nei messaggi Telegram)
"""

from __future__ import annotations

import html
import json
import math
import os
import re
import statistics
import sys
import threading
import time
from concurrent.futures import ThreadPoolExecutor
from datetime import datetime, timedelta, timezone
from pathlib import Path

import requests

from paper import PAPER_FILE, PORTFOLIOS, PaperBook, build_resolver

try:
    import yfinance as yf
    YFINANCE_AVAILABLE = True
except ImportError:  # pragma: no cover
    YFINANCE_AVAILABLE = False
    print("! yfinance non installato, brokerage saltato")


# ══ CONFIG ═══════════════════════════════════════════════════════════════

MODEL_VERSION = 2

BOT_TOKEN = os.environ.get("TELEGRAM_BOT_TOKEN", "").strip()
CHAT_ID = os.environ.get("TELEGRAM_CHAT_ID", "").strip()
COINGECKO_API_KEY = os.environ.get("COINGECKO_API_KEY", "").strip()
DASHBOARD_URL = os.environ.get("DASHBOARD_URL", "https://gumenoto.github.io/markets/").strip()
# Fuori dal branch main (prove su altri branch) niente Telegram: né alert né lettura comandi
_REF = os.environ.get("GITHUB_REF_NAME", "").strip()
DRY_RUN = bool(_REF) and _REF != "main"

STATE_FILE = Path("state.json")
LOG_FILE = Path("signals_log.json")
DOCS_DIR = Path("docs")

TOP_N = 10
LENSES = ("reversion", "momentum", "setup")

# ── Rimbalzi (pesi originali, invariati) ──
WEIGHTS = {
    "rsi": 1.0,
    "drop": 1.0,
    "momentum": 0.6,
    "ma": 0.8,
    "volume": 0.7,
    "macd": 1.0,
    "bollinger": 1.0,
}

# ── Accelerazioni ──
MOMENTUM_WEIGHTS = {
    "breakout20": 30,     # punti fissi per rottura massimo 20 giorni
    "breakout55": 20,     # punti extra se rompe anche il massimo 55 giorni
    "volume": 6,          # × volume ratio (max 8x)
    "relative": 0.8,      # × forza relativa 7g in % (max 50)
    "day_move": 0.8,      # × variazione 24h in % (max 30)
    "adx": 15,            # trend forte e rialzista
    "macd": 10,           # MACD giornaliero in accelerazione
    "trending": 15,       # in tendenza su CoinGecko (proxy notizie)
    "from_squeeze": 15,   # esce da una compressione vista nei giorni prima
}

# ── Soglie alert per modalità e classe (sotto soglia: in dashboard e registro, niente push) ──
ALERT_MIN_SCORE = {
    ("reversion", "crypto"): 60,
    ("reversion", "brokerage"): 40,
    ("momentum", "crypto"): 80,
    ("momentum", "brokerage"): 60,
    ("squeeze", "crypto"): 50,
    ("squeeze", "brokerage"): 40,
    # commodity: nessuna conferma di volume disponibile → serve uno score più alto
    ("reversion", "commodity"): 40,
    ("momentum", "commodity"): 85,
    ("squeeze", "commodity"): 60,
}
# Accelerazioni e squeeze: push solo con volume confermato (volume 24h vs media 20g)
ALERT_MIN_VOLUME = {
    ("momentum", "crypto"): 2.0,
    ("momentum", "brokerage"): 1.5,
    ("squeeze", "crypto"): 1.5,
    ("squeeze", "brokerage"): 1.3,
}
ALERT_COOLDOWN_H = 24        # stesso asset + stessa modalità: max 1 alert/24h
LOG_DEDUP_H = 72             # stesso asset + modalità: 1 voce di registro/72h
SETUP_MEMORY_DAYS = 5        # "In carica" ricordato per 5 giorni
SQUEEZE_MAX_PCT = 0.20       # "In carica" se la larghezza Bollinger è nel 20% più basso (120g)
MAX_ALERTS_PER_RUN = 8
LOG_RETENTION_DAYS = 30      # il registro segnali tiene gli ultimi 30 giorni

# ── Universo crypto ──
CG_BASE = "https://api.coingecko.com/api/v3"
CG_PER_PAGE = 250
MIN_CRYPTO_VOL_USD = 3_000_000
MIN_CRYPTO_MCAP_USD = 30_000_000
CRYPTO_ENRICH_MAX = 200
PRICE_SANITY_TOL = 0.15      # candele accettate se entro ±15% dal prezzo CoinGecko
CRASH_EXCLUDE_PCT = -50      # sotto -50% in 24h: probabile delisting/rug → escluso
MAX_KRAKEN_ASSETS = 30       # Kraken è lento (1 req/s): lo usiamo con parsimonia

STABLE_SYMBOLS = {
    "usdt", "usdc", "dai", "fdusd", "tusd", "usde", "usds", "pyusd", "usdd",
    "busd", "frax", "lusd", "gusd", "usdp", "eurc", "eurs", "usd0", "usdx",
    "rlusd", "susde", "susds", "usdtb", "usd1", "usdf", "usdg", "gho",
    "crvusd", "dola", "mim", "bfusd", "usdy", "usyc", "buidl", "fxusd",
}
EXCLUDE_NAME_RE = re.compile(
    r"\b(wrapped|staked|restaked|bridged|binance-peg|liquid staking|stablecoin)\b",
    re.IGNORECASE,
)

# Crypto acquistabili anche su Trade Republic (lista indicativa: verifica in app)
TR_CRYPTO = {
    "BTC", "ETH", "SOL", "XRP", "ADA", "DOGE", "LINK", "DOT", "AVAX", "MATIC",
    "POL", "LTC", "BCH", "UNI", "AAVE", "ATOM", "TRX", "XLM", "ETC", "FIL",
    "ALGO", "EOS", "SHIB", "ARB", "OP", "INJ",
}

# ── Universo brokerage (simboli yfinance) ──
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
    ("STLAM.MI", "Stellantis"),
    ("RACE.MI", "Ferrari"), ("MONC.MI", "Moncler"), ("LDO.MI", "Leonardo"),
]

# Commodity via futures continui. Su Trade Republic si tradano con ETF/ETC.
COMMODITY_UNIVERSE = [
    ("GC=F", "Oro"),
    ("SI=F", "Argento"),
    ("PL=F", "Platino"),
    ("HG=F", "Rame"),
    ("CL=F", "Petrolio WTI"),
    ("BZ=F", "Brent"),
    ("NG=F", "Gas Naturale"),
    ("ZC=F", "Mais"),
    ("ZW=F", "Grano"),
    ("KC=F", "Caffe"),
]

# Indici di riferimento per la forza relativa delle azioni
BENCHMARKS = {"US": "SPY", "EU": "^STOXX50E", "IT": "FTSEMIB.MI"}

HTTP_HEADERS = {"User-Agent": "mercati-screener/2.0 (+github actions)"}


def now_utc() -> datetime:
    """Ora corrente (sovrascrivibile nei test con SCREENER_FAKE_NOW)."""
    fake = os.environ.get("SCREENER_FAKE_NOW")
    if fake:
        return datetime.fromisoformat(fake)
    return datetime.now(timezone.utc)


def iso(dt: datetime) -> str:
    return dt.astimezone(timezone.utc).isoformat(timespec="seconds")


def parse_iso(s: str | None) -> datetime | None:
    if not s:
        return None
    try:
        return datetime.fromisoformat(s)
    except ValueError:
        return None


# ══ HTTP: sorgenti con limite di frequenza e interruttore ═════════════════

class Source:
    """Wrapper per una API pubblica: limita la frequenza delle chiamate e si
    disattiva da sola per il resto del run dopo errori "duri" ripetuti
    (blocco geografico, rate limit, server giù)."""

    def __init__(self, name: str, min_interval: float, max_hard_failures: int = 4):
        self.name = name
        self.min_interval = min_interval
        self.max_hard = max_hard_failures
        self._lock = threading.Lock()
        self._next_ok = 0.0
        self.hard_failures = 0
        self.disabled = False
        self.disabled_reason = ""
        self.calls = 0
        self.ok = 0
        self.last_error = ""

    def _hard(self, reason: str) -> None:
        with self._lock:
            self.last_error = reason
            self.hard_failures += 1
            if self.hard_failures >= self.max_hard and not self.disabled:
                self.disabled = True
                self.disabled_reason = reason
                print(f"  ! sorgente {self.name} disattivata per questo run ({reason})")

    def get(self, url: str, params: dict | None = None, headers: dict | None = None,
            timeout: float = 15, retries_429: int = 0):
        if self.disabled:
            return None
        attempt = 0
        while True:
            with self._lock:
                wait = self._next_ok - time.monotonic()
                if wait > 0:
                    time.sleep(wait)
                self._next_ok = time.monotonic() + self.min_interval
                self.calls += 1
            try:
                h = dict(HTTP_HEADERS)
                if headers:
                    h.update(headers)
                r = requests.get(url, params=params, headers=h, timeout=timeout)
            except requests.RequestException as e:
                self._hard(type(e).__name__)
                return None
            status = getattr(r, "status_code", 0)
            if status == 200:
                with self._lock:
                    self.hard_failures = 0
                    self.ok += 1
                try:
                    return r.json()
                except ValueError:
                    return None
            if status == 429 and attempt < retries_429:
                attempt += 1
                time.sleep(15 * attempt)
                continue
            if status in (403, 418, 429, 451) or status >= 500:
                self._hard(f"HTTP {status}")
            else:
                self.last_error = f"HTTP {status}"
            return None  # 400/404 ecc.: simbolo non disponibile su questa sorgente

    def summary(self) -> str:
        s = f"{self.name}: {self.ok}/{self.calls} ok"
        if self.disabled:
            s += f" (disattivata: {self.disabled_reason})"
        elif self.last_error and self.ok < self.calls:
            s += f" (ultimo errore: {self.last_error})"
        return s

    def diag(self) -> dict:
        return {"calls": self.calls, "ok": self.ok, "disabled": self.disabled,
                "last_error": self.last_error or None}


SRC_COINGECKO = Source("coingecko", 2.5, max_hard_failures=3)
SRC_COINPAPRIKA = Source("coinpaprika", 1.0, max_hard_failures=2)
SRC_BINANCE = Source("binance", 0.06)
SRC_COINBASE = Source("coinbase", 0.15)
SRC_KRAKEN = Source("kraken", 1.05)
SRC_BINANCE_US = Source("binance.us", 0.1)
ALL_SOURCES = (SRC_COINGECKO, SRC_COINPAPRIKA, SRC_BINANCE, SRC_COINBASE, SRC_KRAKEN, SRC_BINANCE_US)


# ══ INDICATORI ═══════════════════════════════════════════════════════════

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


def bb_width_series(closes: list[float], period: int = 20) -> list[float]:
    """Larghezza relativa delle Bollinger ((sup - inf) / media) per ogni barra."""
    out = []
    for k in range(period, len(closes) + 1):
        s = closes[k - period:k]
        mean = sum(s) / period
        if mean <= 0:
            continue
        sd = math.sqrt(sum((v - mean) ** 2 for v in s) / period)
        out.append(4 * sd / mean)
    return out


def calc_adx(highs: list[float], lows: list[float], closes: list[float], period: int = 14) -> dict | None:
    """ADX di Wilder + direzionali +DI / -DI."""
    n = len(closes)
    if n < 2 * period + 1:
        return None
    tr, pdm, mdm = [], [], []
    for i in range(1, n):
        up = highs[i] - highs[i - 1]
        down = lows[i - 1] - lows[i]
        pdm.append(up if (up > down and up > 0) else 0.0)
        mdm.append(down if (down > up and down > 0) else 0.0)
        tr.append(max(highs[i] - lows[i], abs(highs[i] - closes[i - 1]), abs(lows[i] - closes[i - 1])))

    def dx(atr_, p_, m_):
        if atr_ <= 0:
            return 0.0, 0.0, 0.0
        pdi_ = 100 * p_ / atr_
        mdi_ = 100 * m_ / atr_
        s = pdi_ + mdi_
        return (100 * abs(pdi_ - mdi_) / s if s > 0 else 0.0), pdi_, mdi_

    atr = sum(tr[:period])
    p = sum(pdm[:period])
    m = sum(mdm[:period])
    d, pdi, mdi = dx(atr, p, m)
    dxs = [d]
    for i in range(period, len(tr)):
        atr = atr - atr / period + tr[i]
        p = p - p / period + pdm[i]
        m = m - m / period + mdm[i]
        d, pdi, mdi = dx(atr, p, m)
        dxs.append(d)
    if len(dxs) < period:
        return None
    adx = sum(dxs[:period]) / period
    for d in dxs[period:]:
        adx = (adx * (period - 1) + d) / period
    return {"adx": adx, "plus_di": pdi, "minus_di": mdi}


def calc_obv(closes: list[float], volumes: list[float]) -> list[float]:
    obv = [0.0]
    for i in range(1, len(closes)):
        if closes[i] > closes[i - 1]:
            obv.append(obv[-1] + volumes[i])
        elif closes[i] < closes[i - 1]:
            obv.append(obv[-1] - volumes[i])
        else:
            obv.append(obv[-1])
    return obv


def _mean(xs: list[float]) -> float | None:
    xs = [x for x in xs if x is not None]
    return sum(xs) / len(xs) if xs else None


# ══ METRICHE ═════════════════════════════════════════════════════════════

def reversion_metrics(asset: dict, closes: list[float], volumes: list[float]) -> None:
    """Metriche della modalità Rimbalzi (identiche alla versione originale)."""
    asset["rsi"] = calc_rsi(closes)
    ma20 = sma(closes, 20)
    asset["dist_from_ma20"] = (asset["price"] - ma20) / ma20 * 100 if ma20 else None
    if len(volumes) >= 30:
        recent = sum(volumes[-3:]) / 3
        avg = sum(volumes[-30:-3]) / 27
        asset["volume_ratio"] = recent / avg if avg > 0 else None
    asset["macd"] = calc_macd(closes)
    asset["bollinger"] = calc_bollinger(closes)


def daily_metrics(d: dict | None, vr_rolling: float | None = None) -> dict | None:
    """Metriche giornaliere per Accelerazioni e In carica.
    `d` = {"closes","highs","lows","volumes"} in ordine cronologico; l'ultima
    barra può essere la giornata in corso."""
    if not d:
        return None
    c, h, l, v = d["closes"], d["highs"], d["lows"], d["volumes"]
    n = len(c)
    if n < 25:
        return None
    last = c[-1]
    hi20 = max(h[-21:-1])
    hi55 = max(h[-56:-1]) if n >= 57 else None
    ma20 = sma(c, 20)
    ma50 = sma(c, 50) if n >= 50 else None

    vol_base = _mean(v[-22:-2])
    if vr_rolling is not None:
        vr = vr_rolling
    elif vol_base and vol_base > 0:
        vr = max(v[-1], v[-2]) / vol_base
    else:
        vr = None

    macd_now = calc_macd(c)
    macd_prev = calc_macd(c[:-1])
    macd_rising = bool(
        macd_now and macd_prev and macd_now["histogram"] > 0
        and macd_now["histogram"] > macd_prev["histogram"]
    )

    widths = bb_width_series(c, 20)
    bbw_pct = None
    if len(widths) >= 60:
        window = widths[-120:]
        bbw_pct = sum(1 for w in window if w <= widths[-1]) / len(window)

    accum = None
    if n >= 60:
        base = _mean(v[-60:-10])
        if base and base > 0:
            accum = _mean(v[-10:]) / base

    obv = calc_obv(c, v)
    adx = calc_adx(h, l, c)

    return {
        "last": last,
        "hi20": hi20,
        "hi55": hi55,
        "breakout20": last > hi20,
        "breakout55": hi55 is not None and last > hi55,
        "dist_top20": (hi20 - last) / hi20 * 100 if hi20 else None,
        "ma20": ma20,
        "ma50": ma50,
        "ext": (last - ma20) / ma20 * 100 if ma20 else None,
        "vr": vr,
        "rsi_d": calc_rsi(c),
        "ret5": (c[-1] / c[-6] - 1) * 100 if n >= 6 and c[-6] else None,
        "ret20": (c[-1] / c[-21] - 1) * 100 if n >= 21 and c[-21] else None,
        "macd_rising": macd_rising,
        "bbw_pct": bbw_pct,
        "accum": accum,
        "obv_up": len(obv) >= 20 and obv[-1] > obv[-20],
        "adx": adx["adx"] if adx else None,
        "plus_di": adx["plus_di"] if adx else None,
        "minus_di": adx["minus_di"] if adx else None,
    }


# ══ SCORING ══════════════════════════════════════════════════════════════

def compute_score(asset: dict, w: dict = WEIGHTS) -> tuple[float, list]:
    """Modalità RIMBALZI — logica originale, invariata."""
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


def compute_momentum(asset: dict, w: dict = MOMENTUM_WEIGHTS) -> tuple[float, list]:
    """Modalità ACCELERAZIONI — breakout con volume e forza relativa."""
    m = asset.get("m")
    if not m or m["ma20"] is None:
        return 0.0, []
    ch = asset.get("change_24h") or 0.0
    rel = asset.get("rel_7d")
    ret7 = asset.get("change_7d")
    vr = m["vr"] or 0.0

    gate = (
        m["breakout20"]
        or ((rel or 0) >= 10 and vr >= 1.5)
        or (ch >= 8 and vr >= 2)
    )
    if not gate or m["last"] < m["ma20"] or (ret7 is not None and ret7 <= 0) or ch < -3:
        return 0.0, []  # niente accelerazioni su chi sta scendendo oggi

    score = 0.0
    signals = []
    if m["breakout20"]:
        score += w["breakout20"]
        signals.append(("bull", "Breakout 20g", f"> {fmt_num(m['hi20'])}"))
    if m["breakout55"]:
        score += w["breakout55"]
        signals.append(("bull", "Breakout 55g", ""))
    if vr >= 1.5:
        score += min(vr, 8) * w["volume"]
        signals.append(("bull", f"Volume {vr:.1f}x", ""))
    if rel is not None and rel > 0:
        score += min(rel, 50) * w["relative"]
        label = {"crypto": "Forza vs BTC", "stock": "Forza vs indice"}.get(asset["type"], "Rialzo 7g")
        signals.append(("bull", label, f"+{rel:.0f}%"))
    if ch > 3:
        score += min(ch, 30) * w["day_move"]
        signals.append(("bull", "Rialzo 24h", f"+{ch:.1f}%"))
    if m["adx"] is not None and m["adx"] >= 25 and (m["plus_di"] or 0) > (m["minus_di"] or 0):
        score += w["adx"]
        signals.append(("bull", "Trend forte", f"ADX {m['adx']:.0f}"))
    if m["macd_rising"]:
        score += w["macd"]
        signals.append(("bull", "MACD in accelerazione", ""))
    if asset.get("trending"):
        score += w["trending"]
        signals.append(("bull", "In tendenza", "CoinGecko"))
    if asset.get("from_squeeze"):
        score += w["from_squeeze"]
        signals.insert(0, ("bull", "Esce da squeeze", ""))  # il più informativo: in testa
    return score, signals


def compute_setup(asset: dict) -> tuple[float, list]:
    """Modalità IN CARICA — compressione di volatilità vicino al tetto del range."""
    m = asset.get("m")
    if not m or m["bbw_pct"] is None or m["breakout20"]:
        return 0.0, []
    pct = m["bbw_pct"]
    if pct > SQUEEZE_MAX_PCT:
        return 0.0, []
    score = 40 + (SQUEEZE_MAX_PCT - pct) * 150
    signals = [("bull", "Volatilità ai minimi", f"{pct * 100:.0f}° pct")]
    if m["dist_top20"] is not None and m["dist_top20"] <= 3:
        score += 20
        signals.append(("bull", "Vicino al tetto", f"-{m['dist_top20']:.1f}%"))
    if m["accum"] is not None and 1.1 <= m["accum"] <= 3:
        score += 15
        signals.append(("bull", "Accumulo volumi", f"{m['accum']:.1f}x"))
    if m["obv_up"] and m["ret20"] is not None and abs(m["ret20"]) < 8:
        score += 10
        signals.append(("bull", "OBV in salita", ""))
    if m["ma50"] is not None:
        if m["last"] > m["ma50"]:
            score += 10
            signals.append(("bull", "Sopra MA50", ""))
        else:
            score -= 10
            signals.append(("bear", "Sotto MA50", ""))
    return score, signals


def risk_flags(asset: dict) -> list[tuple[str, str, str]]:
    """Etichette di rischio: informano, non escludono."""
    flags = []
    m = asset.get("m")
    is_crypto = asset["type"] == "crypto"
    if m:
        lim = 35 if is_crypto else 15
        if m["ext"] is not None and m["ext"] > lim:
            flags.append(("esteso", "Esteso", f"+{m['ext']:.0f}% su MA20"))
        # RSI alto da solo è normale il giorno di un breakout: segnalo solo se anche esteso
        if m["rsi_d"] is not None and m["rsi_d"] > 85 and m["ext"] is not None and m["ext"] > lim:
            flags.append(("caldo", "Surriscaldato", f"RSI {m['rsi_d']:.0f}"))
    if is_crypto:
        vol = asset.get("volume_24h")
        mc = asset.get("market_cap")
        if vol is not None and vol < 20e6:
            flags.append(("liquidita", "Liquidità bassa", f"{vol / 1e6:.1f}M$/24h"))
        if vol and mc and vol / mc > 0.5:
            flags.append(("volume_anomalo", "Volume anomalo", f"{vol / mc:.0%} della cap"))
    ch = asset.get("change_24h")
    if ch is not None and ch < -40:
        flags.append(("crollo", "Crollo estremo", f"{ch:.0f}%"))
    return flags


# ══ CANDELE CRYPTO (multi-sorgente) ═══════════════════════════════════════

def _pack(bars: list[tuple]) -> dict | None:
    """bars: (t, o, h, l, c, v) in ordine cronologico."""
    bars = [b for b in bars if b[4] and b[4] > 0]
    if not bars:
        return None
    return {
        "closes": [b[4] for b in bars],
        "highs": [b[2] for b in bars],
        "lows": [b[3] for b in bars],
        "volumes": [b[5] for b in bars],
    }


def _aggregate(bars: list[tuple], hours: int) -> list[tuple]:
    """Aggrega barre orarie in barre da `hours` ore (allineate a UTC)."""
    step = hours * 3600
    groups: dict[int, list] = {}
    counts: dict[int, int] = {}
    for t, o, h, l, c, v in bars:
        k = t - (t % step)
        g = groups.get(k)
        if g is None:
            groups[k] = [k, o, h, l, c, v]
            counts[k] = 1
        else:
            g[2] = max(g[2], h)
            g[3] = min(g[3], l)
            g[4] = c
            g[5] += v
            counts[k] += 1
    keys = sorted(groups)
    if keys and counts[keys[0]] < hours:  # prima barra incompleta
        keys = keys[1:]
    return [tuple(groups[k]) for k in keys]


def candles_binance(src: Source, base_url: str, symbol: str, interval: str, limit: int):
    data = src.get(f"{base_url}/api/v3/klines",
                   params={"symbol": symbol, "interval": interval, "limit": limit})
    if not isinstance(data, list) or not data:
        return None
    try:
        bars = [(int(k[0]) // 1000, float(k[1]), float(k[2]), float(k[3]), float(k[4]), float(k[5]))
                for k in data]
    except (TypeError, ValueError, IndexError):
        return None
    return _pack(bars)


def candles_coinbase(base: str, interval: str):
    gran = 86400 if interval == "1d" else 3600
    data = SRC_COINBASE.get(f"https://api.exchange.coinbase.com/products/{base}-USD/candles",
                            params={"granularity": gran})
    if not isinstance(data, list) or not data:
        return None
    try:
        bars = sorted((int(r[0]), float(r[3]), float(r[2]), float(r[1]), float(r[4]), float(r[5]))
                      for r in data)
    except (TypeError, ValueError, IndexError):
        return None
    if interval == "4h":
        bars = _aggregate(bars, 4)
    return _pack(bars)


KRAKEN_ALIASES = {"BTC": "XBT", "DOGE": "XDG"}


def candles_kraken(base: str, interval: str):
    pair = KRAKEN_ALIASES.get(base, base) + "USD"
    data = SRC_KRAKEN.get("https://api.kraken.com/0/public/OHLC",
                          params={"pair": pair, "interval": 1440 if interval == "1d" else 240})
    if not isinstance(data, dict) or data.get("error"):
        return None
    result = data.get("result") or {}
    rows = next((v for k, v in result.items() if k != "last" and isinstance(v, list)), None)
    if not rows:
        return None
    try:
        bars = [(int(r[0]), float(r[1]), float(r[2]), float(r[3]), float(r[4]), float(r[6])) for r in rows]
    except (TypeError, ValueError, IndexError):
        return None
    return _pack(bars[-200:])


_kraken_budget = threading.Semaphore(MAX_KRAKEN_ASSETS)


def fetch_crypto_candles(base: str, ref_price: float | None, exact_symbol: str | None = None):
    """Prova le sorgenti in cascata. Accetta la prima che ha la coppia con un
    prezzo coerente con quello di riferimento. Ritorna (h4, d1, sorgente)."""

    def try_source(name, fn4, fn1):
        h4 = fn4()
        if not h4 or len(h4["closes"]) < 40:
            return None
        if ref_price and ref_price > 0:
            if abs(h4["closes"][-1] / ref_price - 1) > PRICE_SANITY_TOL:
                return None
        d1 = fn1()
        if d1 and len(d1["closes"]) < 25:
            d1 = None
        return h4, d1, name

    chain = []
    if exact_symbol:  # universo di riserva Binance.US: coppia esatta
        chain.append(("binance.us", SRC_BINANCE_US,
                      lambda: candles_binance(SRC_BINANCE_US, "https://api.binance.us", exact_symbol, "4h", 80),
                      lambda: candles_binance(SRC_BINANCE_US, "https://api.binance.us", exact_symbol, "1d", 200)))
    else:
        chain.append(("binance", SRC_BINANCE,
                      lambda: candles_binance(SRC_BINANCE, "https://data-api.binance.vision", base + "USDT", "4h", 80),
                      lambda: candles_binance(SRC_BINANCE, "https://data-api.binance.vision", base + "USDT", "1d", 200)))
        chain.append(("coinbase", SRC_COINBASE,
                      lambda: candles_coinbase(base, "4h"),
                      lambda: candles_coinbase(base, "1d")))
        chain.append(("kraken", SRC_KRAKEN,
                      lambda: candles_kraken(base, "4h"),
                      lambda: candles_kraken(base, "1d")))
        for quote in ("USDT", "USD"):
            chain.append(("binance.us", SRC_BINANCE_US,
                          lambda q=quote: candles_binance(SRC_BINANCE_US, "https://api.binance.us", base + q, "4h", 80),
                          lambda q=quote: candles_binance(SRC_BINANCE_US, "https://api.binance.us", base + q, "1d", 200)))

    for name, src, fn4, fn1 in chain:
        if src.disabled:
            continue
        if src is SRC_KRAKEN and not _kraken_budget.acquire(blocking=False):
            continue
        res = try_source(name, fn4, fn1)
        if res:
            return res
    return None, None, None


# ══ UNIVERSO CRYPTO ══════════════════════════════════════════════════════

def _cg_headers() -> dict:
    return {"x-cg-demo-api-key": COINGECKO_API_KEY} if COINGECKO_API_KEY else {}


def fetch_coingecko_universe() -> list[dict] | None:
    data = SRC_COINGECKO.get(
        f"{CG_BASE}/coins/markets",
        params={
            "vs_currency": "usd",
            "order": "market_cap_desc",
            "per_page": CG_PER_PAGE,
            "page": 1,
            "price_change_percentage": "24h,7d",
            "sparkline": "false",
        },
        headers=_cg_headers(),
        timeout=30,
        retries_429=2,
    )
    if not isinstance(data, list) or not data:
        return None
    rows = []
    for c in data:
        if not isinstance(c, dict):
            continue
        rows.append({
            "base": (c.get("symbol") or "").upper(),
            "name": c.get("name"),
            "price": c.get("current_price"),
            "change_24h": c.get("price_change_percentage_24h_in_currency", c.get("price_change_percentage_24h")),
            "change_7d": c.get("price_change_percentage_7d_in_currency"),
            "volume_24h": c.get("total_volume"),
            "market_cap": c.get("market_cap"),
            "mcap_rank": c.get("market_cap_rank"),
        })
    return _clean_universe(rows)


def fetch_coinpaprika_universe() -> list[dict] | None:
    """Seconda fonte per l'universo (gratuita, senza chiave)."""
    data = SRC_COINPAPRIKA.get("https://api.coinpaprika.com/v1/tickers",
                               params={"quotes": "USD"}, timeout=30)
    if not isinstance(data, list) or not data:
        return None
    rows = []
    for c in data:
        if not isinstance(c, dict) or not c.get("rank"):
            continue
        q = (c.get("quotes") or {}).get("USD") or {}
        rows.append({
            "base": (c.get("symbol") or "").upper(),
            "name": c.get("name"),
            "price": q.get("price"),
            "change_24h": q.get("percent_change_24h"),
            "change_7d": q.get("percent_change_7d"),
            "volume_24h": q.get("volume_24h"),
            "market_cap": q.get("market_cap"),
            "mcap_rank": c.get("rank"),
        })
    rows.sort(key=lambda r: r["mcap_rank"])
    return _clean_universe(rows[:CG_PER_PAGE])


def _clean_universe(rows: list[dict]) -> list[dict]:
    """Normalizza e toglie stablecoin, wrapped/staked, duplicati."""
    out, seen = [], set()
    for r in rows:
        sym = r["base"]
        name = r.get("name") or sym
        try:
            price = float(r["price"]) if r.get("price") is not None else 0.0
        except (TypeError, ValueError):
            continue
        ch24, ch7 = r.get("change_24h"), r.get("change_7d")
        if not sym or price <= 0 or sym in seen:
            continue
        if sym.lower() in STABLE_SYMBOLS or EXCLUDE_NAME_RE.search(name):
            continue
        if 0.97 <= price <= 1.03 and abs(ch24 or 0) < 1 and abs(ch7 or 0) < 2:
            continue  # stablecoin non in lista
        seen.add(sym)
        out.append({
            "base": sym,
            "name": name,
            "price": price,
            "change_24h": float(ch24) if ch24 is not None else None,
            "change_7d": float(ch7) if ch7 is not None else None,
            "volume_24h": float(r.get("volume_24h") or 0),
            "market_cap": float(r.get("market_cap") or 0),
            "mcap_rank": r.get("mcap_rank"),
        })
    return out


def fetch_coingecko_trending() -> set[str]:
    """Simboli in tendenza su CoinGecko (proxy di notizie/catalizzatori)."""
    data = SRC_COINGECKO.get(f"{CG_BASE}/search/trending", headers=_cg_headers(), retries_429=1)
    syms = set()
    if isinstance(data, dict):
        for c in data.get("coins") or []:
            item = c.get("item") if isinstance(c, dict) else None
            if item and item.get("symbol"):
                syms.add(str(item["symbol"]).upper())
    return syms


def fetch_binance_us_universe() -> list[dict]:
    """Universo di riserva se CoinGecko non risponde."""
    data = SRC_BINANCE_US.get("https://api.binance.us/api/v3/ticker/24hr", timeout=20)
    if not isinstance(data, list):
        return []
    best: dict[str, dict] = {}
    for t in data:
        sym = t.get("symbol", "")
        if sym.endswith(("BUSD", "TUSD", "FDUSD", "USDC", "DAI")):
            continue  # quotate in altre stablecoin (es. OMG/BUSD)
        quote = "USDT" if sym.endswith("USDT") else ("USD" if sym.endswith("USD") else None)
        if not quote:
            continue
        base = sym[: -len(quote)]
        if base.lower() in STABLE_SYMBOLS or base in {"EUR", "GBP", "TRY", "JPY"}:
            continue
        try:
            qv = float(t["quoteVolume"])
            price = float(t["lastPrice"])
            ch = float(t["priceChangePercent"])
        except (KeyError, ValueError, TypeError):
            continue
        if qv < 1_000_000 or price <= 0:
            continue
        if base not in best or qv > best[base]["volume_24h"]:
            best[base] = {"base": base, "name": base, "price": price,
                          "change_24h": ch, "change_7d": None, "volume_24h": qv,
                          "market_cap": None, "mcap_rank": None, "exact_symbol": sym}
    return sorted(best.values(), key=lambda x: x["volume_24h"], reverse=True)


def build_crypto_assets() -> tuple[list[dict], dict]:
    info = {"universe_source": None, "universe_size": 0, "trending": 0, "btc_7d": None}
    trending: set[str] = set()
    universe = fetch_coingecko_universe()
    if universe:
        info["universe_source"] = "coingecko"
    else:
        print(f"  ! CoinGecko non disponibile ({SRC_COINGECKO.last_error or 'nessuna risposta'}): provo CoinPaprika")
        universe = fetch_coinpaprika_universe()
        if universe:
            info["universe_source"] = "coinpaprika"
    if universe:
        if not SRC_COINGECKO.disabled:
            trending = fetch_coingecko_trending()
        info["trending"] = len(trending)
        universe = [u for u in universe
                    if u["volume_24h"] >= MIN_CRYPTO_VOL_USD and (u["market_cap"] or 0) >= MIN_CRYPTO_MCAP_USD]
    else:
        print(f"  ! CoinPaprika non disponibile ({SRC_COINPAPRIKA.last_error or 'nessuna risposta'}): "
              "uso l'universo di riserva Binance.US")
        universe = fetch_binance_us_universe()
        info["universe_source"] = "binance.us" if universe else None
    universe = universe[:CRYPTO_ENRICH_MAX]
    info["universe_size"] = len(universe)
    btc = next((u for u in universe if u["base"] == "BTC"), None)
    info["btc_7d"] = btc["change_7d"] if btc else None

    def enrich(u: dict) -> dict | None:
        h4, d1, src = fetch_crypto_candles(u["base"], u["price"], u.get("exact_symbol"))
        if not h4:
            return None
        a = {
            "type": "crypto",
            "symbol": f"{u['base']}USDT",
            "base": u["base"],
            "name": u["name"],
            "price": u["price"],
            "change_24h": u["change_24h"],
            "change_7d": u["change_7d"],
            "volume_24h": u["volume_24h"],
            "market_cap": u["market_cap"],
            "mcap_rank": u["mcap_rank"],
            "trending": u["base"] in trending,
            "source": src,
        }
        reversion_metrics(a, h4["closes"], h4["volumes"])
        if a["change_24h"] is None and len(h4["closes"]) >= 7:
            a["change_24h"] = (h4["closes"][-1] / h4["closes"][-7] - 1) * 100
        vr_rolling = None
        if d1:
            base_vol = _mean(d1["volumes"][-21:-1])
            if base_vol and base_vol > 0 and len(h4["volumes"]) >= 6:
                vr_rolling = sum(h4["volumes"][-6:]) / base_vol
            if a["change_7d"] is None and len(d1["closes"]) >= 8:
                a["change_7d"] = (d1["closes"][-1] / d1["closes"][-8] - 1) * 100
        a["m"] = daily_metrics(d1, vr_rolling)
        return a

    with ThreadPoolExecutor(max_workers=8) as ex:
        assets = [a for a in ex.map(enrich, universe) if a]

    if info["btc_7d"] is None:
        btc_a = next((a for a in assets if a["base"] == "BTC"), None)
        info["btc_7d"] = btc_a["change_7d"] if btc_a else None
    for a in assets:
        if a["change_7d"] is not None:
            a["rel_7d"] = a["change_7d"] - (info["btc_7d"] or 0)
    return assets, info


# ══ BROKERAGE (yfinance) ═════════════════════════════════════════════════

def _region(symbol: str) -> str:
    if symbol.endswith(".MI"):
        return "IT"
    if any(symbol.endswith(s) for s in (".DE", ".PA", ".AS")):
        return "EU"
    return "US"


def yf_daily(symbol: str) -> dict | None:
    if not YFINANCE_AVAILABLE:
        return None
    try:
        hist = yf.Ticker(symbol).history(period="1y", interval="1d", auto_adjust=True)
    except Exception as e:
        print(f"  ! errore yfinance {symbol}: {e}")
        return None
    if hist is None or getattr(hist, "empty", True):
        return None
    hist = hist.dropna(subset=["Close"])
    if len(hist) < 30:
        return None

    def col(name):
        if name not in hist:
            return [0.0] * len(hist)
        return [float(x) if x == x else 0.0 for x in hist[name].fillna(0).tolist()]

    closes = [float(x) for x in hist["Close"].tolist()]
    highs = col("High")
    lows = col("Low")
    highs = [h if h > 0 else c for h, c in zip(highs, closes)]
    lows = [l if l > 0 else c for l, c in zip(lows, closes)]
    return {"closes": closes, "highs": highs, "lows": lows, "volumes": col("Volume")}


def fetch_yf_asset(symbol: str, name: str, asset_type: str, bench: dict) -> dict | None:
    d = yf_daily(symbol)
    if not d:
        return None
    if asset_type == "commodity":
        # il volume dei futures continui è inaffidabile (rollover): non lo usiamo
        d["volumes"] = [0.0] * len(d["closes"])
    closes = d["closes"]
    price = closes[-1]
    prev = closes[-2] if len(closes) >= 2 else price
    a: dict = {
        "type": asset_type,
        "symbol": symbol,
        "name": name,
        "price": price,
        "change_24h": (price - prev) / prev * 100 if prev else 0.0,
        "change_7d": (closes[-1] / closes[-6] - 1) * 100 if len(closes) >= 6 and closes[-6] else None,
    }
    reversion_metrics(a, closes, d["volumes"])
    a["m"] = daily_metrics(d)
    if a["change_7d"] is not None:
        ref = bench.get(_region(symbol)) if asset_type == "stock" else None
        a["rel_7d"] = a["change_7d"] - (ref or 0)
    return a


def build_brokerage_assets() -> tuple[list[dict], list[dict], dict, dict]:
    bench, bench_px = {}, {}
    for region, sym in BENCHMARKS.items():
        d = yf_daily(sym)
        if d and len(d["closes"]) >= 6:
            bench[region] = (d["closes"][-1] / d["closes"][-6] - 1) * 100
            bench_px[sym] = d["closes"][-1]
    # sequenziale di proposito: Yahoo limita le richieste parallele
    stocks = [a for a in (fetch_yf_asset(s, n, "stock", bench) for s, n in STOCK_UNIVERSE) if a]
    commodities = [a for a in (fetch_yf_asset(s, n, "commodity", bench) for s, n in COMMODITY_UNIVERSE) if a]
    return stocks, commodities, bench, bench_px


# ══ TELEGRAM ═════════════════════════════════════════════════════════════

def fetch_telegram_commands(offset: int | None, now: datetime) -> tuple[list[dict], int | None]:
    """Legge i messaggi arrivati al bot dalla tua chat (comandi del portafoglio simulato)."""
    if DRY_RUN or not BOT_TOKEN or not CHAT_ID:
        return [], offset
    params = {"timeout": 0, "allowed_updates": json.dumps(["message"])}
    if offset is not None:
        params["offset"] = offset
    try:
        r = requests.get(f"https://api.telegram.org/bot{BOT_TOKEN}/getUpdates", params=params, timeout=20)
        data = r.json()
    except Exception as e:
        print(f"  ! Telegram getUpdates: {e}")
        return [], offset
    if not data.get("ok"):
        print(f"  ! Telegram getUpdates: {data.get('description')}")
        return [], offset
    msgs, new_offset = [], offset
    for u in data.get("result") or []:
        new_offset = max(new_offset or 0, int(u["update_id"]) + 1)
        m = u.get("message") or {}
        if str((m.get("chat") or {}).get("id")) != CHAT_ID or not m.get("text"):
            continue  # solo la tua chat
        sent = datetime.fromtimestamp(m.get("date", 0), tz=timezone.utc)
        if sent < now - timedelta(hours=26):
            continue
        msgs.append({"text": m["text"], "ts": iso(sent)})
    return msgs, new_offset


def send_telegram(text: str) -> bool:
    if DRY_RUN:
        print("  [prova, Telegram disattivato] " + text.replace("\n", " | ")[:160])
        return True
    if not BOT_TOKEN or not CHAT_ID:
        print("  ! Telegram non configurato (mancano TELEGRAM_BOT_TOKEN/CHAT_ID)")
        return False
    url = f"https://api.telegram.org/bot{BOT_TOKEN}/sendMessage"
    payload = {"chat_id": CHAT_ID, "text": text, "parse_mode": "HTML",
               "disable_web_page_preview": True}
    try:
        r = requests.post(url, json=payload, timeout=15)
        data = r.json()
        if data.get("ok"):
            return True
        print(f"  ! Telegram error: {data.get('description')} — ritento senza formattazione")
        plain = re.sub(r"<[^>]+>", "", text)
        r = requests.post(url, json={"chat_id": CHAT_ID, "text": html.unescape(plain),
                                     "disable_web_page_preview": True}, timeout=15)
        return bool(r.json().get("ok"))
    except Exception as e:
        print(f"  ! Telegram exception: {e}")
        return False


def fmt_num(p: float | None) -> str:
    if p is None:
        return "—"
    if p < 1:
        return f"{p:.6f}"
    if p < 100:
        return f"{p:.3f}"
    return f"{p:.2f}"


def venue_for(a: dict) -> str:
    if a["type"] == "crypto":
        return "Trade Republic o Binance" if a.get("base") in TR_CRYPTO else "Binance o altri exchange"
    if a["type"] == "commodity":
        return "Trade Republic · via ETF/ETC"
    return "Trade Republic"


LENS_HEADERS = {
    "reversion": "🔄 <b>Rimbalzo</b>",
    "momentum": "🚀 <b>Accelerazione</b>",
    "squeeze": "⚡ <b>Squeeze scattato</b>",
}


def format_alert(kind: str, a: dict, score: float, signals: list, flags: list,
                 paper_note: str | None = None) -> str:
    e = html.escape
    ch = a.get("change_24h") or 0
    bull = [s for s in signals if s[0] == "bull"][:4]
    lines = [
        f"{LENS_HEADERS[kind]} · nuovo in Top {TOP_N}",
        f"<b>{e(a['name'])}</b> ({e(a['symbol'])})",
        f"Score <code>{int(round(score))}</code> · Prezzo <code>{fmt_num(a['price'])}</code> "
        f"({'+' if ch >= 0 else ''}{ch:.2f}% 24h)",
        "",
    ]
    lines += [f"• {e(s[1])}{(' ' + e(s[2])) if s[2] else ''}" for s in bull]
    if flags:
        lines.append("")
        lines += [f"⚠️ {e(f[1])}: {e(f[2])}" for f in flags]
    lines += ["", f"📍 {e(venue_for(a))}"]
    if paper_note:
        lines.append(f"📒 {e(paper_note)}")
    if DASHBOARD_URL:
        lines.append(f'<a href="{e(DASHBOARD_URL)}">Apri la dashboard</a>')
    return "\n".join(lines)


# ══ STATO, REGISTRO, OUTPUT ══════════════════════════════════════════════

def asset_id(a: dict) -> str:
    return f"{a['type']}:{a['symbol']}"


def load_json(path: Path, default):
    if path.exists():
        try:
            return json.loads(path.read_text())
        except (json.JSONDecodeError, OSError):
            print(f"  ! {path} illeggibile, riparto da zero")
    return default


def clean(obj):
    """Rimuove NaN/inf (non validi in JSON) e arrotonda i float."""
    if isinstance(obj, float):
        if not math.isfinite(obj):
            return None
        return float(f"{obj:.7g}")  # 7 cifre significative: ok anche per prezzi tipo 0.0000123
    if isinstance(obj, dict):
        return {k: clean(v) for k, v in obj.items()}
    if isinstance(obj, (list, tuple)):
        return [clean(v) for v in obj]
    return obj


def write_json(path: Path, obj) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_suffix(path.suffix + ".tmp")
    tmp.write_text(json.dumps(clean(obj), indent=1, ensure_ascii=False, allow_nan=False))
    tmp.replace(path)


def serialize(item: tuple) -> dict:
    a, score, signals = item
    m = a.get("m") or {}
    return {
        "type": a["type"],
        "symbol": a["symbol"],
        "name": a["name"],
        "price": a["price"],
        "change_24h": a.get("change_24h"),
        "change_7d": a.get("change_7d"),
        "rel_7d": a.get("rel_7d"),
        "rsi": a.get("rsi"),
        "dist_from_ma20": a.get("dist_from_ma20"),
        "volume_ratio": a.get("volume_ratio"),
        "macd": a.get("macd"),
        "bollinger": a.get("bollinger"),
        "score": round(score, 1),
        "signals": [{"kind": s[0], "label": s[1], "value": s[2]} for s in signals],
        "flags": [{"code": f[0], "label": f[1], "value": f[2]} for f in a.get("flags", [])],
        "trending": a.get("trending", False),
        "mcap_rank": a.get("mcap_rank"),
        "volume_24h": a.get("volume_24h"),
        "venue": venue_for(a),
        "daily": {
            "breakout20": m.get("breakout20"),
            "breakout55": m.get("breakout55"),
            "hi20": m.get("hi20"),
            "dist_top20": m.get("dist_top20"),
            "ext": m.get("ext"),
            "vr": m.get("vr"),
            "rsi_d": m.get("rsi_d"),
            "adx": m.get("adx"),
            "bbw_pct": m.get("bbw_pct"),
            "accum": m.get("accum"),
            "above_ma50": (m["last"] > m["ma50"]) if m.get("ma50") else None,
        } if m else None,
    }


def update_signal_log(log: list[dict], prices: dict[str, float], now: datetime) -> None:
    """Aggiorna rendimenti a 1/3/7 giorni dei segnali registrati."""
    for e in log:
        if e.get("closed"):
            continue
        t0 = parse_iso(e["ts"])
        if not t0:
            e["closed"] = True
            continue
        age_d = (now - t0).total_seconds() / 86400
        p = prices.get(e["asset_id"])
        if p and e.get("price"):
            ret = (p / e["price"] - 1) * 100
            e["last_ret"] = ret
            e["max_up"] = max(e.get("max_up", 0.0), ret)
            e["max_down"] = min(e.get("max_down", 0.0), ret)
            for h, days in (("1d", 1), ("3d", 3), ("7d", 7)):
                if e["rets"].get(h) is None and age_d >= days:
                    e["rets"][h] = ret
        if age_d >= 9 or all(e["rets"].get(h) is not None for h in ("1d", "3d", "7d")):
            e["closed"] = True


def perf_summary(log: list[dict]) -> dict:
    lenses = {}
    for lens in LENSES:
        entries = [e for e in log if e["lens"] == lens]
        hz = {}
        for h in ("1d", "3d", "7d"):
            vals = [e["rets"][h] for e in entries if e["rets"].get(h) is not None]
            hz[h] = {
                "n": len(vals),
                "win": (sum(1 for v in vals if v > 0) / len(vals) * 100) if vals else None,
                "avg": statistics.mean(vals) if vals else None,
                "median": statistics.median(vals) if vals else None,
            }
        lenses[lens] = {"signals": len(entries), "horizons": hz}
    recent = sorted(log, key=lambda e: e["ts"], reverse=True)[:20]
    return {
        "since": min((e["ts"] for e in log), default=None),
        "lenses": lenses,
        "recent": [{k: e.get(k) for k in ("lens", "symbol", "name", "type", "ts", "price", "score",
                                          "rets", "last_ret", "max_up", "max_down", "alerted")}
                   for e in recent],
    }


# ══ MAIN ═════════════════════════════════════════════════════════════════

def rank(assets: list[dict], fn) -> list[tuple]:
    out = []
    for a in assets:
        score, signals = fn(a)
        if score > 0:
            out.append((a, score, signals))
    out.sort(key=lambda x: x[1], reverse=True)
    return out


def asset_class(a: dict) -> str:
    return "crypto" if a["type"] == "crypto" else "brokerage"


def alert_class(a: dict) -> str:
    return {"crypto": "crypto", "commodity": "commodity"}.get(a["type"], "brokerage")


def main() -> int:
    now = now_utc()
    print(f"[{iso(now)}] Avvio screener v{MODEL_VERSION}")

    # ── Dati ──
    t0 = time.monotonic()
    print("Crypto...")
    crypto, cinfo = build_crypto_assets()
    print(f"  universo {cinfo['universe_source']}: {cinfo['universe_size']} coin liquide, "
          f"{len(crypto)} con candele · trending {cinfo['trending']} · BTC 7g {cinfo['btc_7d']}")
    by_src: dict[str, int] = {}
    for a in crypto:
        by_src[a["source"]] = by_src.get(a["source"], 0) + 1
    print(f"  candele per sorgente: {by_src}")
    for s in ALL_SOURCES:
        print(f"  · {s.summary()}")

    print("Brokerage (Yahoo Finance)...")
    stocks, commodities, bench, bench_px = build_brokerage_assets()
    print(f"  {len(stocks)}/{len(STOCK_UNIVERSE)} azioni, {len(commodities)}/{len(COMMODITY_UNIVERSE)} commodity, "
          f"indici {', '.join(f'{k} {v:+.1f}%' for k, v in bench.items()) or 'n/d'}")
    print(f"  dati raccolti in {time.monotonic() - t0:.0f}s")

    all_assets = crypto + stocks + commodities
    if not all_assets:
        print("!! Nessun dato disponibile: non sovrascrivo la dashboard.")
        return 1
    for a in all_assets:
        a["asset_id"] = asset_id(a)

    # ── Pulizia e flag ──
    tradable = []
    for a in all_assets:
        a["flags"] = risk_flags(a)
        if (a.get("change_24h") or 0) <= CRASH_EXCLUDE_PCT:
            print(f"  · escluso {a['symbol']} (crollo {a['change_24h']:.0f}% in 24h)")
            continue
        tradable.append(a)

    # ── Stato precedente ──
    state = load_json(STATE_FILE, {})
    baseline = state.get("model_version") != MODEL_VERSION
    prev_lenses = state.get("lenses") or {}
    setup_seen = {k: v for k, v in (state.get("setup_seen") or {}).items()
                  if (parse_iso(v) or now) > now - timedelta(days=SETUP_MEMORY_DAYS)}
    for a in tradable:
        if asset_id(a) in setup_seen and a.get("m") and a["m"]["breakout20"]:
            a["from_squeeze"] = True

    # ── Classifiche ──
    scorers = {"reversion": compute_score, "momentum": compute_momentum, "setup": compute_setup}
    lists: dict[str, dict[str, list]] = {}
    for lens, fn in scorers.items():
        ranked = rank(tradable, fn)
        lists[lens] = {
            "combined": ranked[:TOP_N],
            "crypto": [x for x in ranked if x[0]["type"] == "crypto"][:TOP_N],
            "brokerage": [x for x in ranked if x[0]["type"] != "crypto"][:TOP_N],
            "_count": len(ranked),
        }

    for lens in LENSES:
        print(f"\nTOP {TOP_N} {lens} (combinati):")
        if not lists[lens]["combined"]:
            print("  (nessun segnale)")
        for a, score, _ in lists[lens]["combined"]:
            ch = a.get("change_24h") or 0
            fl = ",".join(f[0] for f in a["flags"])
            print(f"  {int(round(score)):>4}  {a['symbol']:<12} {ch:+7.2f}%  [{a['type']}] {fl}")

    # ── Nuovi ingressi, alert e registro ──
    log = load_json(LOG_FILE, [])
    if not isinstance(log, list):
        log = []
    alert_hist = state.get("alert_history") or {}
    pending = []           # (priorità, kind, lens, item)
    qualified = []         # segnali sopra soglia (anche senza alert): li seguono i portafogli simulati
    new_lens_state = {}
    for lens in LENSES:
        tracked = lists[lens]["crypto"] + lists[lens]["brokerage"]
        ids = [asset_id(x[0]) for x in tracked]
        new_lens_state[lens] = ids
        if baseline or lens not in prev_lenses:
            continue
        prev = set(prev_lenses.get(lens) or [])
        for item in tracked:
            a, score, _ = item
            aid = asset_id(a)
            if aid in prev:
                continue
            recent_log = any(e["lens"] == lens and e["asset_id"] == aid
                             and (parse_iso(e["ts"]) or now) > now - timedelta(hours=LOG_DEDUP_H)
                             for e in log)
            entry = None
            if not recent_log:
                entry = {"lens": lens, "asset_id": aid, "symbol": a["symbol"], "name": a["name"],
                         "type": a["type"], "ts": iso(now), "price": a["price"], "score": round(score, 1),
                         "flags": [f[0] for f in a["flags"]], "rets": {"1d": None, "3d": None, "7d": None},
                         "alerted": False}
                log.append(entry)
            if lens == "setup":
                continue  # "In carica" non manda alert all'ingresso
            kind = "squeeze" if (lens == "momentum" and a.get("from_squeeze")) else lens
            if score < ALERT_MIN_SCORE[(kind, alert_class(a))]:
                continue
            min_vr = ALERT_MIN_VOLUME.get((kind, asset_class(a)))
            vr = (a.get("m") or {}).get("vr")
            if min_vr and vr is not None and vr < min_vr:  # vr None = volume non disponibile (commodity)
                continue
            qualified.append((kind, a, score))
            last_alert = parse_iso(alert_hist.get(f"{lens}|{aid}"))
            if last_alert and last_alert > now - timedelta(hours=ALERT_COOLDOWN_H):
                continue
            prio = {"squeeze": 0, "momentum": 1, "reversion": 2}[kind]
            pending.append((prio, -score, kind, lens, item, entry))

    if baseline:
        print(f"\nNuovo modello (v{MODEL_VERSION}): registro lo stato senza inviare alert in questo run.")

    # ── Portafogli simulati: uscite, poi nuove entrate sui segnali qualificati ──
    prices = {asset_id(a): a["price"] for a in all_assets}
    book = PaperBook.load()
    paper_bench_px = {"BTC": next((a["price"] for a in crypto if a.get("base") == "BTC"), None),
                      "SPY": bench_px.get("SPY")}
    book.ensure_started(now, {k: v for k, v in paper_bench_px.items() if v})
    paper_events = book.update(prices, now)
    paper_notes = {}
    for kind, a, score in qualified:
        opened = book.open_auto(kind, a, score, now)
        if opened:
            paper_events.append(opened)
            label = next(m["label"] for m in PORTFOLIOS.values() if m["signal"] == kind)
            paper_notes[(kind, a["asset_id"])] = f"Simulazione: comprati 1.000 nel portafoglio «{label}»"
    for ev in paper_events:
        print(f"  📒 {ev}")

    pending.sort(key=lambda x: (x[0], x[1]))
    sent = 0
    for prio, _, kind, lens, (a, score, signals), entry in pending[:MAX_ALERTS_PER_RUN - 1] \
            if len(pending) > MAX_ALERTS_PER_RUN else pending:
        print(f"  -> alert {kind}: {a['symbol']} (score {int(round(score))})")
        if send_telegram(format_alert(kind, a, score, signals, a["flags"],
                                      paper_notes.get((kind, a["asset_id"])))):
            sent += 1
        alert_hist[f"{lens}|{asset_id(a)}"] = iso(now)
        if entry:
            entry["alerted"] = True
    if len(pending) > MAX_ALERTS_PER_RUN:
        rest = pending[MAX_ALERTS_PER_RUN - 1:]
        names = ", ".join(html.escape(x[4][0]["name"]) for x in rest[:12])
        msg = f"➕ Altri {len(rest)} segnali nuovi: {names}"
        if DASHBOARD_URL:
            msg += f'\n<a href="{html.escape(DASHBOARD_URL)}">Apri la dashboard</a>'
        send_telegram(msg)
        for x in rest:
            alert_hist[f"{x[3]}|{asset_id(x[4][0])}"] = iso(now)
    if pending:
        print(f"  alert inviati: {sent}/{len(pending)}")
    elif not baseline:
        print("\nNessun nuovo segnale sopra soglia.")

    # ── Portafoglio manuale: comandi Telegram (/compra, /vendi, /portafoglio) ──
    lens_labels = {"momentum": "Accelerazioni", "setup": "In carica", "reversion": "Rimbalzi"}
    listed: dict[str, list[str]] = {}
    for lens in LENSES:
        for x in lists[lens]["crypto"] + lists[lens]["brokerage"]:
            listed.setdefault(asset_id(x[0]), []).append(lens_labels[lens])
    resolve = build_resolver(tradable)
    cmds, new_offset = fetch_telegram_commands(book.data.get("telegram_offset"), now)
    for c in cmds:
        print(f"  ✉️  comando: {c['text']}")
        reply = book.handle_command(c["text"], resolve, lambda aid: listed.get(aid, []), now, c["ts"])
        send_telegram(reply)
    book.data["telegram_offset"] = new_offset
    book.record_history(now)

    # ── Registro: rendimenti ──
    update_signal_log(log, prices, now)
    log = [e for e in log if (parse_iso(e["ts"]) or now) > now - timedelta(days=LOG_RETENTION_DAYS)]

    # ── Stato ──
    for x in lists["setup"]["crypto"] + lists["setup"]["brokerage"]:
        setup_seen[asset_id(x[0])] = iso(now)
    alert_hist = {k: v for k, v in alert_hist.items()
                  if (parse_iso(v) or now) > now - timedelta(days=3)}
    new_state = {
        "model_version": MODEL_VERSION,
        "last_run": iso(now),
        "previous_top_n": [asset_id(x[0]) for x in lists["reversion"]["combined"]],
        "lenses": new_lens_state,
        "setup_seen": setup_seen,
        "alert_history": alert_hist,
    }

    # ── Dashboard ──
    lenses_out = {
        lens: {k: [serialize(x) for x in v] for k, v in lists[lens].items() if not k.startswith("_")}
        for lens in LENSES
    }
    rev = lenses_out["reversion"]
    payload = {
        "generated_at": iso(now),
        "model_version": MODEL_VERSION,
        "stats": {
            "crypto_count": len(crypto),
            "stocks_count": len(stocks),
            "commodities_count": len(commodities),
            "brokerage_count": len(stocks) + len(commodities),
            "scored_count": lists["reversion"]["_count"],
            "lens_counts": {lens: lists[lens]["_count"] for lens in LENSES},
            "top_n": TOP_N,
            "crypto_universe": cinfo["universe_source"],
            "crypto_sources": by_src,
            "btc_7d": cinfo["btc_7d"],
            "benchmarks_7d": bench,
            "trending": cinfo["trending"],
            "sources": {s.name: s.diag() for s in ALL_SOURCES},
        },
        # retrocompatibilità con la dashboard precedente (= modalità Rimbalzi)
        "lists": {"combined": rev["combined"], "crypto": rev["crypto"],
                  "brokerage": rev["brokerage"], "stocks": rev["brokerage"]},
        "lenses": lenses_out,
        "performance": perf_summary(log),
        "paper": book.summary({k: v for k, v in paper_bench_px.items() if v}, now),
    }
    write_json(DOCS_DIR / "data.json", payload)
    write_json(LOG_FILE, log)
    write_json(PAPER_FILE, book.to_json())
    write_json(STATE_FILE, new_state)
    print(f"\nRegistro segnali: {len(log)} voci · Fatto in {time.monotonic() - t0:.0f}s.")
    return 0


if __name__ == "__main__":
    sys.exit(main())
