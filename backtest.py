#!/usr/bin/env python3
"""
Backtest storico delle modalità dello screener (gira su GitHub Actions).

Ricostruisce i segnali come nel live:
  - stessi punteggi (Rimbalzi, Accelerazioni, In carica → squeeze), in forma vettoriale
  - top 10 per classe (crypto / brokerage), "nuovo ingresso", soglie di alert e volume
e ne misura gli esiti:
  - rendimento a 1/3/7 giorni netto costi, ed eccesso rispetto alla media
    dell'universo nello stesso momento (toglie l'effetto "mercato")
  - simulazione delle regole di uscita (stop, obiettivo, trailing, scadenza)
  - portafoglio simulato come nel paper trading (10 posizioni, 1.000 a operazione)
per la configurazione attuale e per varianti (filtro di trend, filtro di regime,
breakout più selettivi, regole di uscita alternative, RSI(2) di Connors).

Limiti dichiarati nel report: universo crypto = monete liquide di oggi
(survivorship bias, gonfia i rimbalzi), niente "in tendenza" storico,
crypto Accelerazioni/In carica su barre giornaliere (il live controlla ogni 30').
"""

from __future__ import annotations

import json
import math
import sys
import time
from concurrent.futures import ThreadPoolExecutor
from datetime import datetime, timezone
from pathlib import Path

import numpy as np
import pandas as pd

import screener as S
from paper import COSTS

OUT = Path("backtest_results")
CRYPTO_N = 150
WARMUP = 220
TOP_N = 10
HORIZONS_D = (1, 3, 7)

RULES = {
    "rev_cur": {"stop": -5, "take": 6, "trail": None, "trail_from": None, "max_days": 3, "ma_exit": False},
    "rev_wide": {"stop": -8, "take": 6, "trail": None, "trail_from": None, "max_days": 5, "ma_exit": False},
    "rev_ma5": {"stop": -10, "take": None, "trail": None, "trail_from": None, "max_days": 5, "ma_exit": True},
    "mom_cur": {"stop": -8, "take": None, "trail": 15, "trail_from": 8, "max_days": 10, "ma_exit": False},
    "mom_wide": {"stop": -12, "take": None, "trail": 20, "trail_from": 10, "max_days": 20, "ma_exit": False},
    "mom_short": {"stop": -8, "take": None, "trail": None, "trail_from": None, "max_days": 5, "ma_exit": False},
}
RULESETS = {"reversion": ["rev_cur", "rev_wide", "rev_ma5"], "connors": ["rev_ma5", "rev_cur", "rev_wide"],
            "momentum": ["mom_cur", "mom_wide", "mom_short"], "squeeze": ["mom_cur", "mom_wide", "mom_short"]}


def log(msg):
    print(f"[{datetime.now(timezone.utc).strftime('%H:%M:%S')}] {msg}", flush=True)


# ══ DATI ══════════════════════════════════════════════════════════════════

def binance_klines(sym: str, interval: str, pages: int) -> pd.DataFrame | None:
    rows, end = [], None
    for _ in range(pages):
        params = {"symbol": sym, "interval": interval, "limit": 1000}
        if end:
            params["endTime"] = end
        data = S.SRC_BINANCE.get("https://data-api.binance.vision/api/v3/klines", params=params, timeout=30)
        if not isinstance(data, list) or not data:
            break
        rows = data + rows
        end = int(data[0][0]) - 1
        if len(data) < 1000:
            break
    if len(rows) < 300:
        return None
    df = pd.DataFrame([[int(r[6]) + 1, float(r[1]), float(r[2]), float(r[3]), float(r[4]), float(r[5]), float(r[7])]
                       for r in rows], columns=["t", "o", "h", "l", "c", "v", "qv"])
    df["t"] = pd.to_datetime(df["t"], unit="ms", utc=True)
    df = df.drop_duplicates("t").set_index("t").sort_index().iloc[:-1]  # ultima barra in corso
    return df[df["c"] > 0]


def yf_daily_df(sym: str) -> pd.DataFrame | None:
    try:
        h = S.yf.Ticker(sym).history(period="5y", interval="1d", auto_adjust=True)
    except Exception as e:
        log(f"  ! yfinance {sym}: {e}")
        return None
    if h is None or h.empty:
        return None
    h = h.dropna(subset=["Close"])
    df = pd.DataFrame({
        "o": h["Open"].astype(float), "h": h["High"].astype(float), "l": h["Low"].astype(float),
        "c": h["Close"].astype(float), "v": h["Volume"].fillna(0).astype(float),
    })
    df.index = pd.to_datetime([d.date() for d in h.index]).tz_localize("UTC")
    df = df[~df.index.duplicated(keep="last")]
    for col in ("o", "h", "l"):
        df[col] = df[col].where(df[col] > 0, df["c"])
    return df if len(df) > 300 else None


def load_data():
    log("Universo crypto…")
    uni = S.fetch_coingecko_universe() or S.fetch_coinpaprika_universe() or []
    uni = [u for u in uni if u["volume_24h"] >= S.MIN_CRYPTO_VOL_USD and u["market_cap"] >= S.MIN_CRYPTO_MCAP_USD]
    bases = [u["base"] for u in uni[:CRYPTO_N]]
    if "BTC" not in bases:
        bases.insert(0, "BTC")
    names = {u["base"]: u["name"] for u in uni}
    ref_px = {u["base"]: u["price"] for u in uni}
    fresh_after = pd.Timestamp.now(tz="UTC") - pd.Timedelta(days=5)

    def alive(base, df):
        """Coppia ancora quotata e con prezzo coerente con quello di mercato (no coppie morte o ridenominate)."""
        if df is None or df.index[-1] < fresh_after:
            return False
        p = ref_px.get(base)
        return p is None or abs(df["c"].iloc[-1] / p - 1) <= 0.15
    log(f"  {len(bases)} crypto candidate")

    def get(base):
        d1 = binance_klines(base + "USDT", "1d", 2)
        h4 = binance_klines(base + "USDT", "4h", 2) if d1 is not None else None
        return base, d1, h4

    crypto_d, crypto_4h = {}, {}
    with ThreadPoolExecutor(max_workers=6) as ex:
        for base, d1, h4 in ex.map(get, bases):
            if not alive(base, d1):
                if d1 is not None:
                    log(f"  · scartato {base}: coppia ferma al {d1.index[-1].date()} o prezzo incoerente")
                continue
            crypto_d[base] = d1
            if alive(base, h4):
                crypto_4h[base] = h4
    log(f"  crypto con storico: giornaliero {len(crypto_d)}, 4h {len(crypto_4h)} · {S.SRC_BINANCE.summary()}")

    log("Brokerage (Yahoo)…")
    brok = {}
    for sym, name in S.STOCK_UNIVERSE:
        df = yf_daily_df(sym)
        if df is not None:
            brok[sym] = (df, "stock", name)
    for sym, name in S.COMMODITY_UNIVERSE:
        df = yf_daily_df(sym)
        if df is not None:
            df["v"] = 0.0  # volume dei futures inaffidabile, come nel live
            brok[sym] = (df, "commodity", name)
    bench = {k: yf_daily_df(v) for k, v in S.BENCHMARKS.items()}
    log(f"  brokerage {len(brok)} · benchmark {[k for k, v in bench.items() if v is not None]}")
    return crypto_d, crypto_4h, brok, bench, names


# ══ INDICATORI (vettoriali, equivalenti a quelli del live) ════════════════

def rsi(c: pd.Series, n: int = 14) -> pd.Series:
    d = c.diff()
    g = d.clip(lower=0).ewm(alpha=1 / n, adjust=False).mean()
    lo = (-d.clip(upper=0)).ewm(alpha=1 / n, adjust=False).mean()
    out = 100 - 100 / (1 + g / lo.replace(0, np.nan))
    out = out.where(lo != 0, 100.0)
    out.iloc[: n + 1] = np.nan
    return out


def macd(c: pd.Series):
    m = c.ewm(span=12, adjust=False).mean() - c.ewm(span=26, adjust=False).mean()
    s = m.ewm(span=9, adjust=False).mean()
    m.iloc[:35] = np.nan
    s.iloc[:35] = np.nan
    return m, s, m - s


def reversion_features(df: pd.DataFrame, ch_lag: int) -> pd.DataFrame:
    c, v = df["c"], df["v"]
    f = pd.DataFrame(index=df.index)
    f["rsi"] = rsi(c)
    ma20 = c.rolling(20).mean()
    f["dist"] = (c - ma20) / ma20 * 100
    base = v.shift(3).rolling(27).mean()
    f["vr"] = (v.rolling(3).mean() / base).where(base > 0)
    m, s, h = macd(c)
    f["macd"], f["hist"] = m, h
    f["xup"] = (m > s) & (m.shift() <= s.shift())
    f["xdn"] = (m < s) & (m.shift() >= s.shift())
    sd = c.rolling(20).std(ddof=0)
    up, lo = ma20 + 2 * sd, ma20 - 2 * sd
    f["pb"] = ((c - lo) / (up - lo)).where(up != lo, 0.5)
    f["ch"] = (c / c.shift(ch_lag) - 1) * 100
    return f


def reversion_score(f: pd.DataFrame, w=S.WEIGHTS) -> pd.Series:
    r, ch, dist, vr, pb = (f[k].to_numpy(dtype=float) for k in ("rsi", "ch", "dist", "vr", "pb"))
    sc = np.zeros(len(f))
    sc += np.where(r < 30, (30 - r) / 30 * 100 * w["rsi"], 0)
    sc += np.where(r > 70, -(r - 70) / 30 * 60 * w["rsi"], 0)
    sc += np.where(ch < -3, np.minimum(np.abs(ch), 25) * 3 * w["drop"], 0)
    sc += np.where(ch > 5, np.minimum(ch, 25) * w["momentum"], 0)
    sc += np.where(dist < -4, np.minimum(np.abs(dist), 20) * 2.5 * w["ma"], 0)
    sc += np.where(vr > 1.5, np.minimum(vr, 6) * 6 * w["volume"], 0)
    has_m = ~np.isnan(f["macd"].to_numpy(dtype=float))
    xup, xdn = f["xup"].to_numpy(), f["xdn"].to_numpy()
    hist, mac = f["hist"].to_numpy(dtype=float), f["macd"].to_numpy(dtype=float)
    sc += np.where(has_m & xup, 35 * w["macd"], np.where(has_m & xdn, -25 * w["macd"],
                   np.where(has_m & (hist > 0) & (mac < 0), 12 * w["macd"], 0)))
    sc += np.where(pb < 0.05, 50 * w["bollinger"], np.where(pb < 0.2, 25 * w["bollinger"],
                   np.where(pb > 0.95, -30 * w["bollinger"], 0)))
    sc = np.where(np.isnan(r), np.nan, sc)
    return pd.Series(sc, index=f.index)


def daily_features(df: pd.DataFrame, ret_lag: int, bench_ret: pd.Series | None) -> pd.DataFrame:
    c, h, l, v = df["c"], df["h"], df["l"], df["v"]
    f = pd.DataFrame(index=df.index)
    f["c"] = c
    f["hi20"] = h.shift(1).rolling(20).max()
    f["hi55"] = h.shift(1).rolling(55).max()
    f["bo20"] = c > f["hi20"]
    f["bo55"] = c > f["hi55"]
    for n in (5, 20, 50, 200):
        f[f"ma{n}"] = c.rolling(n).mean()
    base = v.shift(1).rolling(20).mean()
    f["vr"] = (v / base).where(base > 0)
    f["ret7"] = (c / c.shift(ret_lag) - 1) * 100
    f["rel"] = f["ret7"] - (bench_ret.reindex(f.index).ffill() if bench_ret is not None else 0)
    f["ch1"] = (c / c.shift(1) - 1) * 100
    tr = pd.concat([h - l, (h - c.shift()).abs(), (l - c.shift()).abs()], axis=1).max(axis=1)
    up, dn = h.diff(), -l.diff()
    pdm = up.where((up > dn) & (up > 0), 0.0)
    mdm = dn.where((dn > up) & (dn > 0), 0.0)
    atr = tr.ewm(alpha=1 / 14, adjust=False).mean()
    pdi = 100 * pdm.ewm(alpha=1 / 14, adjust=False).mean() / atr
    mdi = 100 * mdm.ewm(alpha=1 / 14, adjust=False).mean() / atr
    dx = (100 * (pdi - mdi).abs() / (pdi + mdi)).fillna(0)
    f["adx"] = dx.ewm(alpha=1 / 14, adjust=False).mean()
    f["adx_up"] = (f["adx"] >= 25) & (pdi > mdi)
    f.loc[f.index[:30], "adx_up"] = False
    f["rsi_d"] = rsi(c)
    f["rsi2"] = rsi(c, 2)
    _, _, hist = macd(c)
    f["macd_rising"] = (hist > 0) & (hist > hist.shift())
    f["ext"] = (c - f["ma20"]) / f["ma20"] * 100
    sd = c.rolling(20).std(ddof=0)
    bbw = 4 * sd / f["ma20"]
    f["bbw_pct"] = bbw.rolling(120, min_periods=60).apply(lambda w: (w <= w[-1]).mean(), raw=True)
    f["dist_top"] = (f["hi20"] - c) / f["hi20"] * 100
    b50 = v.shift(10).rolling(50).mean()
    f["accum"] = (v.rolling(10).mean() / b50).where(b50 > 0)
    obv = (np.sign(c.diff()).fillna(0) * v).cumsum()
    f["obv_up"] = obv > obv.shift(19)
    f["ret20"] = (c / c.shift(20) - 1) * 100
    return f


def setup_score(f: pd.DataFrame) -> pd.Series:
    pct = f["bbw_pct"]
    ok = (pct <= S.SQUEEZE_MAX_PCT) & ~f["bo20"]
    sc = 40 + (S.SQUEEZE_MAX_PCT - pct) * 150
    sc = sc + 20 * (f["dist_top"] <= 3) + 15 * ((f["accum"] >= 1.1) & (f["accum"] <= 3))
    sc = sc + 10 * (f["obv_up"] & (f["ret20"].abs() < 8))
    sc = sc + np.where(f["ma50"].notna(), np.where(f["c"] > f["ma50"], 10, -10), 0)
    return sc.where(ok)


def momentum_score(f: pd.DataFrame, w=S.MOMENTUM_WEIGHTS) -> pd.Series:
    vr = f["vr"].fillna(0)
    rel = f["rel"]
    ch = f["ch1"]
    gate = f["bo20"] | ((rel >= 10) & (vr >= 1.5)) | ((ch >= 8) & (vr >= 2))
    gate &= (f["c"] >= f["ma20"]) & (f["ret7"] > 0) & (ch >= -3)
    sc = (w["breakout20"] * f["bo20"] + w["breakout55"] * f["bo55"]
          + np.where(vr >= 1.5, np.minimum(vr, 8) * w["volume"], 0)
          + np.where(rel > 0, np.minimum(rel, 50) * w["relative"], 0)
          + np.where(ch > 3, np.minimum(ch, 30) * w["day_move"], 0)
          + w["adx"] * f["adx_up"] + w["macd"] * f["macd_rising"]
          + w["from_squeeze"] * f["from_squeeze"])
    return pd.Series(np.where(gate, sc, np.nan), index=f.index)


# ══ ESITI ═════════════════════════════════════════════════════════════════

def simulate_exit(o, h, l, c, ma5, i0: int, rule: dict, bars_per_day: float):
    """Esito di un'operazione entrata alla chiusura della barra i0. None se i dati finiscono prima."""
    entry = c[i0]
    stop = entry * (1 + rule["stop"] / 100)
    take = entry * (1 + rule["take"] / 100) if rule["take"] is not None else None
    peak = entry
    max_bars = max(1, int(round(rule["max_days"] * bars_per_day)))
    n = len(c)
    for j in range(i0 + 1, i0 + max_bars + 1):
        if j >= n:
            return None
        if l[j] <= stop:
            return min(stop, o[j]) / entry - 1, j, "stop"
        if take is not None and h[j] >= take:
            return max(take, o[j]) / entry - 1, j, "take"
        if rule["trail"]:
            if (peak / entry - 1) * 100 >= rule["trail_from"]:
                lvl = peak * (1 - rule["trail"] / 100)
                if l[j] <= lvl:
                    return min(lvl, o[j]) / entry - 1, j, "trail"
            peak = max(peak, h[j])
        if rule["ma_exit"] and ma5 is not None and c[j] > ma5[j]:
            return c[j] / entry - 1, j, "ma5"
    return c[i0 + max_bars] / entry - 1, i0 + max_bars, "time"


def build_events(panel: dict, scores: pd.DataFrame, masks: dict[str, pd.DataFrame], cls_of: dict,
                 lens: str, min_score: dict, min_vr: dict | None, vr_df: pd.DataFrame | None,
                 start_ts) -> list[dict]:
    """Nuovi ingressi nella top 10 della classe, sopra soglia (come gli alert/portafogli live)."""
    events = []
    for cls in ("crypto", "brokerage"):
        cols = [a for a in scores.columns if cls_of[a] == cls]
        if not cols:
            continue
        sc = scores[cols].where(scores[cols] > 0)
        rk = sc.rank(axis=1, ascending=False, method="first")
        top = (rk <= TOP_N).fillna(False)
        new = top & ~top.shift(1, fill_value=False)
        for a in cols:
            sub_cls = panel[a]["kind"]
            thr = min_score[sub_cls]
            s = scores[a]
            ok = new[a] & (s >= thr)
            if min_vr is not None and vr_df is not None and min_vr.get(sub_cls):
                vr = vr_df[a]
                ok &= vr.isna() | (vr >= min_vr[sub_cls])
            ok &= scores.index >= start_ts
            for ts in scores.index[ok.fillna(False).to_numpy()]:
                ev = {"ts": ts, "asset": a, "lens": lens, "cls": cls, "kind": sub_cls, "score": float(s.loc[ts])}
                for mname, m in masks.items():
                    ev[mname] = bool(m[a].get(ts, False)) if a in m else False
                events.append(ev)
    return events


def attach_outcomes(events: list[dict], series: dict, ruleset: list[str], fwd_mean: dict):
    for ev in events:
        sr = series[ev["asset"]]
        i0 = sr["pos"].get(ev["ts"])
        if i0 is None:
            ev["skip"] = True
            continue
        bpd = sr["bpd"]
        cost = sr["cost_rt"]
        ev["entry"] = float(sr["c"][i0])
        for hd in HORIZONS_D:
            j = i0 + int(round(hd * bpd))
            if j < len(sr["c"]):
                r = sr["c"][j] / sr["c"][i0] - 1 - cost
                ev[f"f{hd}"] = r
                m = fwd_mean[sr["panel"]][hd].get(ev["ts"])
                ev[f"x{hd}"] = (r + cost - m) if m is not None and not math.isnan(m) else None
        for rname in ruleset:
            res = simulate_exit(sr["o"], sr["h"], sr["l"], sr["c"], sr["ma5"], i0, RULES[rname], bpd)
            if res is None:
                continue
            ret, j, why = res
            ev[f"r_{rname}"] = ret - cost
            ev[f"e_{rname}"] = sr["idx"][j]
            ev[f"w_{rname}"] = why


def fwd_means(panel_series: dict, horizons_bars: dict) -> dict:
    """Rendimento medio dell'universo (equal weight) a ogni istante e orizzonte."""
    out = {}
    closes = pd.DataFrame({a: pd.Series(s["c"], index=s["idx"]) for a, s in panel_series.items()})
    elig = pd.DataFrame({a: pd.Series(s["elig"], index=s["idx"]) for a, s in panel_series.items()}).reindex(closes.index)
    for hd, bars in horizons_bars.items():
        fr = closes.shift(-bars) / closes - 1
        fr = fr.where(elig.fillna(False).astype(bool))
        out[hd] = fr.mean(axis=1).to_dict()
    return out


# ══ STATISTICHE ═══════════════════════════════════════════════════════════

def stats(evs: list[dict], key: str) -> dict:
    vals = [e[key] for e in evs if e.get(key) is not None]
    if not vals:
        return {"n": 0}
    a = np.array(vals)
    wins, losses = a[a > 0].sum(), -a[a <= 0].sum()
    return {"n": len(a), "mean": float(a.mean() * 100), "median": float(np.median(a) * 100),
            "win": float((a > 0).mean() * 100), "pf": float(wins / losses) if losses > 0 else None}


def cluster_t(evs: list[dict], key: str) -> dict:
    """Media per giorno, poi t-statistic sui giorni (i segnali dello stesso giorno sono correlati)."""
    by_day: dict = {}
    for e in evs:
        if e.get(key) is not None:
            by_day.setdefault(e["ts"].normalize(), []).append(e[key])
    if len(by_day) < 5:
        return {"days": len(by_day)}
    d = np.array([np.mean(v) for v in by_day.values()])
    sd = d.std(ddof=1)
    return {"days": len(d), "mean": float(d.mean() * 100), "t": float(d.mean() / (sd / math.sqrt(len(d)))) if sd > 0 else None}


def portfolio_sim(evs: list[dict], rname: str, size=1000.0, cap=10000.0, max_open=10) -> dict:
    """Come il paper trading: 1.000 a operazione, max 10 aperte, no doppioni; P&L contato all'uscita."""
    evs = sorted([e for e in evs if e.get(f"r_{rname}") is not None], key=lambda e: e["ts"])
    open_pos: list = []          # (asset, exit_ts, ret)
    closed: list = []            # (exit_ts, ret)
    for e in evs:
        still = []
        for p in open_pos:
            (closed.append((p[1], p[2])) if p[1] <= e["ts"] else still.append(p))
        open_pos = still
        if len(open_pos) >= max_open or any(p[0] == e["asset"] for p in open_pos):
            continue
        open_pos.append((e["asset"], e[f"e_{rname}"], e[f"r_{rname}"]))
    closed += [(p[1], p[2]) for p in open_pos]
    closed.sort(key=lambda x: x[0])
    eq, peak, mdd = cap, cap, 0.0
    for _, r in closed:
        eq += r * size
        peak = max(peak, eq)
        mdd = min(mdd, eq / peak - 1)
    a = np.array([r for _, r in closed]) if closed else np.array([0.0])
    gains, losses = a[a > 0].sum(), -a[a <= 0].sum()
    return {"trades": len(closed), "ret_pct": (eq - cap) / cap * 100, "win": float((a > 0).mean() * 100) if closed else None,
            "pf": float(gains / losses) if losses > 0 else None, "max_dd": mdd * 100}


# ══ MAIN ══════════════════════════════════════════════════════════════════

def main() -> int:
    t0 = time.monotonic()
    OUT.mkdir(exist_ok=True)
    crypto_d, crypto_4h, brok, bench, names = load_data()
    if "BTC" not in crypto_d:
        log("!! manca BTC: impossibile calcolare forza relativa e regime")
        return 1

    btc = crypto_d["BTC"]["c"]
    btc_ret7 = (btc / btc.shift(7) - 1) * 100
    regime_crypto = btc > btc.rolling(50).mean()
    bench_ret5, regime_b = {}, {}
    for reg, df in bench.items():
        if df is not None:
            bench_ret5[reg] = (df["c"] / df["c"].shift(5) - 1) * 100
            regime_b[reg] = df["c"] > df["c"].rolling(200).mean()

    # ── pannelli ──
    series, cls_of = {}, {}
    D_scores_m, D_scores_s, D_vr, D_masks = {}, {}, {}, {"trend": {}, "regime": {}, "strict": {}, "connors": {}}
    R4_scores, R4_masks = {}, {"trend": {}, "regime": {}}
    RD_scores = {}

    def reg_series(idx, kind, sym):
        if kind == "crypto":
            return regime_crypto.reindex(idx, method="ffill").fillna(False)
        if kind == "commodity":
            return pd.Series(True, index=idx)
        r = regime_b.get(S._region(sym))
        return r.reindex(idx, method="ffill").fillna(False) if r is not None else pd.Series(True, index=idx)

    def add_daily(key, df, kind, sym, bench_r, ret_lag, cost_rt, liq_ok):
        f = daily_features(df, ret_lag, bench_r)
        sq = setup_score(f)
        f["from_squeeze"] = (sq.notna().astype(float).shift(1).rolling(5).max() > 0) & f["bo20"]
        D_scores_s[key] = sq.where(liq_ok)
        D_scores_m[key] = momentum_score(f).where(liq_ok)
        D_vr[key] = f["vr"] if kind != "commodity" else pd.Series(np.nan, index=f.index)
        reg = reg_series(f.index, kind, sym)
        D_masks["trend"][key] = f["c"] > f["ma200"]
        D_masks["regime"][key] = reg
        D_masks["strict"][key] = f["bo55"] & (f["vr"].fillna(0) >= 2) & (f["rel"] >= 10)
        D_masks["connors"][key] = (f["rsi2"] < 10) & (f["c"] > f["ma200"]) & liq_ok
        series[key] = {"o": df["o"].to_numpy(), "h": df["h"].to_numpy(), "l": df["l"].to_numpy(),
                       "c": df["c"].to_numpy(), "ma5": f["ma5"].to_numpy(), "idx": df.index,
                       "pos": {t: i for i, t in enumerate(df.index)}, "bpd": 1.0, "cost_rt": cost_rt,
                       "panel": "D_" + ("crypto" if kind == "crypto" else "brok"), "kind": kind,
                       "elig": liq_ok.reindex(df.index).fillna(False).to_numpy()}
        cls_of[key] = "crypto" if kind == "crypto" else "brokerage"
        rf = reversion_features(df, 1)
        RD_scores[key] = reversion_score(rf).where(liq_ok & (rf["ch"] > S.CRASH_EXCLUDE_PCT))

    cost = {k: (v["pct"] * 2 / 100 + v["fixed"] * 2 / 1000) for k, v in COSTS.items()}
    for base, df in crypto_d.items():
        liq = df["qv"].rolling(20).mean() >= 1_500_000
        add_daily(base, df, "crypto", base, btc_ret7, 7, cost["crypto"], liq)
    for sym, (df, kind, _) in brok.items():
        br = bench_ret5.get(S._region(sym)) if kind == "stock" else None
        add_daily(sym, df, kind, sym, br, 5, cost[kind], pd.Series(True, index=df.index))
    log(f"Pannello giornaliero: {len(series)} asset")

    # crypto Rimbalzi su 4h (come nel live)
    series4 = {}
    for base, df in crypto_4h.items():
        rf = reversion_features(df, 6)
        dly = crypto_d.get(base)
        liq = (dly["qv"].rolling(20).mean() >= 1_500_000).reindex(df.index, method="ffill").fillna(False) \
            if dly is not None else pd.Series(False, index=df.index)
        R4_scores[base] = reversion_score(rf).where(liq & (rf["ch"] > S.CRASH_EXCLUDE_PCT))
        if dly is not None:
            ma200 = dly["c"].rolling(200).mean()
            R4_masks["trend"][base] = (dly["c"] > ma200).reindex(df.index, method="ffill").fillna(False)
        else:
            R4_masks["trend"][base] = pd.Series(False, index=df.index)
        R4_masks["regime"][base] = regime_crypto.reindex(df.index, method="ffill").fillna(False)
        series4[base] = {"o": df["o"].to_numpy(), "h": df["h"].to_numpy(), "l": df["l"].to_numpy(),
                         "c": df["c"].to_numpy(), "ma5": df["c"].rolling(30).mean().to_numpy(), "idx": df.index,
                         "pos": {t: i for i, t in enumerate(df.index)}, "bpd": 6.0, "cost_rt": cost["crypto"],
                         "panel": "R4", "kind": "crypto", "elig": liq.to_numpy()}
    log(f"Pannello 4h crypto: {len(series4)} asset")

    def frame(d):
        return pd.DataFrame(d).sort_index()

    Dm, Ds, Dvr, RD, R4 = frame(D_scores_m), frame(D_scores_s), frame(D_vr), frame(RD_scores), frame(R4_scores)
    Dmask = {k: frame(v).reindex(Dm.index).fillna(False).astype(bool) for k, v in D_masks.items()}
    R4mask = {k: frame(v).reindex(R4.index).fillna(False).astype(bool) for k, v in R4_masks.items()}

    start_D = Dm.index[min(WARMUP, len(Dm.index) - 1)]
    start_4 = R4.index[min(WARMUP * 6 // 4, len(R4.index) - 1)] if len(R4.index) else None
    log(f"Periodo di valutazione: giornaliero dal {start_D.date()}, 4h dal {start_4.date() if start_4 is not None else '—'}")

    # rendimenti medi dell'universo
    fm = {
        "D_crypto": fwd_means({k: v for k, v in series.items() if v["panel"] == "D_crypto"}, {h: h for h in HORIZONS_D}),
        "D_brok": fwd_means({k: v for k, v in series.items() if v["panel"] == "D_brok"}, {h: h for h in HORIZONS_D}),
        "R4": fwd_means(series4, {h: h * 6 for h in HORIZONS_D}) if series4 else {h: {} for h in HORIZONS_D},
    }
    all_series = {**series}
    all_series.update({f"{k}@4h": v for k, v in series4.items()})

    thr_rev = {"crypto": S.ALERT_MIN_SCORE[("reversion", "crypto")], "stock": S.ALERT_MIN_SCORE[("reversion", "brokerage")],
               "commodity": S.ALERT_MIN_SCORE[("reversion", "commodity")]}
    thr_mom = {"crypto": S.ALERT_MIN_SCORE[("momentum", "crypto")], "stock": S.ALERT_MIN_SCORE[("momentum", "brokerage")],
               "commodity": S.ALERT_MIN_SCORE[("momentum", "commodity")]}
    thr_sq = {"crypto": S.ALERT_MIN_SCORE[("squeeze", "crypto")], "stock": S.ALERT_MIN_SCORE[("squeeze", "brokerage")],
              "commodity": S.ALERT_MIN_SCORE[("squeeze", "commodity")]}
    vr_mom = {"crypto": S.ALERT_MIN_VOLUME[("momentum", "crypto")], "stock": S.ALERT_MIN_VOLUME[("momentum", "brokerage")]}
    vr_sq = {"crypto": S.ALERT_MIN_VOLUME[("squeeze", "crypto")], "stock": S.ALERT_MIN_VOLUME[("squeeze", "brokerage")]}

    # ── eventi ──
    # Rimbalzi: crypto su 4h, brokerage su giornaliero (come nel live)
    rev_c = build_events({k: {"kind": "crypto"} for k in R4.columns}, R4, R4mask, {k: "crypto" for k in R4.columns},
                         "reversion", thr_rev, None, None, start_4) if len(R4.columns) else []
    for e in rev_c:
        e["asset"] = e["asset"] + "@4h"
    brok_cols = [c for c in RD.columns if cls_of[c] == "brokerage"]
    rev_b = build_events({k: {"kind": series[k]["kind"]} for k in brok_cols}, RD[brok_cols],
                         {k: v[brok_cols] for k, v in Dmask.items() if k in ("trend", "regime")},
                         {k: "brokerage" for k in brok_cols}, "reversion", thr_rev, None, None, start_D)
    kinds = {k: {"kind": series[k]["kind"]} for k in Dm.columns}
    # Accelerazioni e squeeze: separo gli eventi "da squeeze" come nel live
    # tutti i nuovi ingressi in top 10, poi soglie diverse per "squeeze" e "accelerazione" (come nel live)
    zero = {"crypto": 0, "stock": 0, "commodity": 0}
    mom_all = build_events(kinds, Dm, Dmask, cls_of, "momentum", zero, None, None, start_D)
    sq_prev = (Ds.notna().astype(float).shift(1).rolling(5).max() > 0)
    mom, sq = [], []
    for e in mom_all:
        vr = Dvr[e["asset"]].get(e["ts"]) if e["kind"] != "commodity" else None
        vr = None if vr is None or (isinstance(vr, float) and math.isnan(vr)) else vr
        if bool(sq_prev[e["asset"]].get(e["ts"], False)):
            need_s, need_v = thr_sq[e["kind"]], vr_sq.get(e["kind"])
            target, e["lens"] = sq, "squeeze"
        else:
            need_s, need_v = thr_mom[e["kind"]], vr_mom.get(e["kind"])
            target = mom
        if e["score"] < need_s or (need_v and vr is not None and vr < need_v):
            continue
        target.append(e)
    # Connors RSI(2): ogni attraversamento della condizione (non serve la top 10)
    conn = []
    cm = Dmask["connors"]
    newc = cm & ~cm.shift(1, fill_value=False)
    for a in cm.columns:
        for ts in cm.index[(newc[a] & (cm.index >= start_D)).to_numpy()]:
            conn.append({"ts": ts, "asset": a, "lens": "connors", "cls": cls_of[a], "kind": series[a]["kind"], "score": 0.0,
                         "regime": bool(Dmask["regime"][a].get(ts, False)), "trend": True})

    groups = {"reversion": rev_c + rev_b, "momentum": mom, "squeeze": sq, "connors": conn}
    for lens, evs in groups.items():
        attach_outcomes(evs, all_series, RULESETS[lens], fm)
        log(f"  {lens}: {len(evs)} eventi")

    # ── report ──
    variants = {
        "reversion": {"attuale": lambda e: True, "trend200": lambda e: e.get("trend"), "regime": lambda e: e.get("regime"),
                      "trend+regime": lambda e: e.get("trend") and e.get("regime")},
        "momentum": {"attuale": lambda e: True, "regime": lambda e: e.get("regime"), "selettivo": lambda e: e.get("strict"),
                     "selettivo+regime": lambda e: e.get("strict") and e.get("regime")},
        "squeeze": {"attuale": lambda e: True, "regime": lambda e: e.get("regime")},
        "connors": {"rsi2<10 & >MA200": lambda e: True, "+regime": lambda e: e.get("regime")},
    }
    mids = {}
    for lens, evs in groups.items():
        ts = sorted(e["ts"] for e in evs)
        mids[lens] = ts[len(ts) // 2] if ts else None
    report = {"generated": datetime.now(timezone.utc).isoformat(timespec="seconds"),
              "period": {"daily_from": str(start_D.date()), "4h_from": str(start_4.date()) if start_4 is not None else None,
                         "to": str(Dm.index[-1].date()), "split": {k: str(v.date()) for k, v in mids.items() if v is not None}},
              "universe": {"crypto_daily": len(crypto_d), "crypto_4h": len(crypto_4h), "brokerage": len(brok)},
              "benchmarks": {}, "results": []}
    for nm, s in (("BTC", btc),):
        s2 = s[s.index >= start_D]
        report["benchmarks"][nm] = float((s2.iloc[-1] / s2.iloc[0] - 1) * 100)
    if bench.get("US") is not None:
        s2 = bench["US"]["c"][bench["US"]["c"].index >= start_D]
        report["benchmarks"]["SPY"] = float((s2.iloc[-1] / s2.iloc[0] - 1) * 100)

    for lens, evs in groups.items():
        for vname, vf in variants[lens].items():
            sel = [e for e in evs if not e.get("skip") and vf(e)]
            for cls in ("crypto", "brokerage", "tutti"):
                sub = sel if cls == "tutti" else [e for e in sel if e["cls"] == cls]
                if not sub:
                    continue
                mid = mids[lens]
                halves = {"A": [e for e in sub if e["ts"] < mid], "B": [e for e in sub if e["ts"] >= mid]}
                r0 = RULESETS[lens][0]
                years = {}
                for e in sub:
                    years.setdefault(str(e["ts"].year), []).append(e)
                row = {"lens": lens, "variant": vname, "cls": cls, "n": len(sub),
                       "fwd": {f"{h}d": stats(sub, f"f{h}") for h in HORIZONS_D},
                       "excess3d": cluster_t(sub, "x3"),
                       "excess3d_halves": {k: cluster_t(v, "x3") for k, v in halves.items()},
                       "years": {y: {"rule": r0, **stats(v, f"r_{r0}"), "x3": cluster_t(v, "x3").get("mean")}
                                 for y, v in sorted(years.items())},
                       "rules": {}}
                for rname in RULESETS[lens]:
                    row["rules"][rname] = {
                        "all": stats(sub, f"r_{rname}"),
                        "A": stats(halves["A"], f"r_{rname}"),
                        "B": stats(halves["B"], f"r_{rname}"),
                        "portfolio": portfolio_sim(sub, rname),
                        "portfolio_A": portfolio_sim(halves["A"], rname),
                        "portfolio_B": portfolio_sim(halves["B"], rname),
                    }
                report["results"].append(row)

    (OUT / "report.json").write_text(json.dumps(report, indent=1, default=str))
    # eventi grezzi (per analisi successive)
    flat = []
    for evs in groups.values():
        for e in evs:
            if e.get("skip"):
                continue
            flat.append({k: (str(v) if isinstance(v, pd.Timestamp) else v) for k, v in e.items()})
    (OUT / "events.json").write_text(json.dumps(flat, default=str))
    write_markdown(report)
    log(f"Fatto in {time.monotonic() - t0:.0f}s")
    return 0


def write_markdown(rep: dict) -> None:
    def f(x, d=1, pct=True):
        if x is None or (isinstance(x, float) and math.isnan(x)):
            return "—"
        return f"{x:+.{d}f}%" if pct else f"{x:.{d}f}"
    lines = [f"# Backtest — {rep['generated']}", "",
             f"Periodo: giornaliero dal {rep['period']['daily_from']}, 4h dal {rep['period']['4h_from']}, fino al {rep['period']['to']} · metà: {rep['period']['split']}",
             f"Universo: crypto {rep['universe']}", f"Benchmark nel periodo: {rep['benchmarks']}", ""]
    for r in rep["results"]:
        lines.append(f"## {r['lens']} · {r['variant']} · {r['cls']} (n={r['n']})")
        fw = r["fwd"]
        lines.append("fwd netto: " + " · ".join(f"{h} {f(fw[h].get('mean'),2)} win {f(fw[h].get('win'),0,False)}%" for h in fw))
        ex = r["excess3d"]
        exh = r["excess3d_halves"]
        lines.append(f"eccesso 3g vs universo: {f(ex.get('mean'),2)} t={f(ex.get('t'),2,False)} giorni={ex.get('days')} · A {f(exh['A'].get('mean'),2)} t={f(exh['A'].get('t'),2,False)} · B {f(exh['B'].get('mean'),2)} t={f(exh['B'].get('t'),2,False)}")
        first_rule = next(iter(r["years"].values()), {}).get("rule", "")
        lines.append(f"per anno ({first_rule}): " + " · ".join(
            f"{y} n={v.get('n')} {f(v.get('mean'),2)} win {f(v.get('win'),0,False)}% x3 {f(v.get('x3'),2)}" for y, v in r["years"].items()))
        for rn, rr in r["rules"].items():
            a, A, B, p = rr["all"], rr["A"], rr["B"], rr["portfolio"]
            lines.append(f"  {rn:<10} n={a.get('n')} media {f(a.get('mean'),2)} win {f(a.get('win'),0,False)}% pf {f(a.get('pf'),2,False)} | A {f(A.get('mean'),2)} pf {f(A.get('pf'),2,False)} | B {f(B.get('mean'),2)} pf {f(B.get('pf'),2,False)} | portafoglio {f(p['ret_pct'],1)} dd {f(p['max_dd'],1)} ({p['trades']} op.)")
        lines.append("")
    (OUT / "report.md").write_text("\n".join(lines))


if __name__ == "__main__":
    sys.exit(main())
