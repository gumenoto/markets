#!/usr/bin/env python3
"""
Ricerca per il modello v4 (gira su GitHub Actions, scrive research_results/).

Domande, decise PRIMA di guardare i risultati (niente pesca a strascico):

  E1  Segnali crypto v3 (Accelerazioni ≥ 100 e Squeeze, regime BTC > MA50):
      quanto contano esecuzione in ritardo, slittamento dello stop, tipo di stop
      (fisso −8% / −12% / multiplo dell'ATR), liquidità ed estensione del prezzo?
  E2  Stessi segnali in un portafoglio valutato ogni giorno: importo fisso,
      quota fissa del capitale, rischio fisso per operazione (dimensione = rischio / stop).
  E3  Rotazione crypto SETTIMANALE da eseguire a mano: ogni lunedì le K crypto
      liquide con il rendimento a 28 giorni più alto, solo con BTC sopra la MA50,
      altrimenti liquidità. Confronto con BTC comprato e tenuto e con BTC/ETH con filtro di trend.
  E4  Rotazione ETF MENSILE (stile Faber/Antonacci): i 3 ETF con il rendimento a 6 mesi
      più alto tra 13 classi di attivo, solo se sopra la media a 200 giorni, altrimenti liquidità.
      Confronto con S&P 500 e 60/40.

  E5  Definizione del regime: BTC > MA50 (attuale), BTC > MA200, BTC > MA50 con MA50 > MA200,
      ampiezza (metà delle crypto liquide sopra la propria MA50), nessun filtro.
  E6  Con stop 3× ATR: uscita a 3, 5, 7, 10 giorni; filtro di liquidità ≥ 20 M$/giorno.
      Regola per adottare una modifica: deve migliorare ENTRAMBE le metà del periodo.

Per E3 ed E4 i parametri di riferimento vengono dalla letteratura (non ottimizzati qui);
la griglia di sensibilità serve solo a vedere se il risultato regge cambiandoli.

Limiti: universo crypto = monete liquide di oggi (survivorship bias, gonfia soprattutto
le rotazioni sulle altcoin), barre giornaliere, ETF in dollari (senza cambio EUR/USD),
liquidità remunerata a zero.
"""

from __future__ import annotations

import json
import math
import sys
import time
from datetime import datetime, timezone
from pathlib import Path

import numpy as np
import pandas as pd

import backtest as B
import screener as S

OUT = Path("research_results")
WARMUP = 220
COST_CRYPTO = 0.0025     # per lato (spread + commissione + slittamento)
COST_ETF = 0.0015        # per lato (Trade Republic 1 € + spread su ~1.000 €)
SIZE_CAP = 10_000.0

ETF_UNIVERSE = [
    ("SPY", "S&P 500"), ("QQQ", "Nasdaq 100"), ("IWM", "Small cap USA"), ("EFA", "Sviluppati ex USA"),
    ("VGK", "Europa"), ("EEM", "Emergenti"), ("EWJ", "Giappone"), ("GLD", "Oro"), ("SLV", "Argento"),
    ("TLT", "Treasury 20+ anni"), ("IEF", "Treasury 7-10 anni"), ("DBC", "Materie prime"), ("VNQ", "Immobiliare USA"),
]


def log(msg: str) -> None:
    print(f"[{datetime.now(timezone.utc).strftime('%H:%M:%S')}] {msg}", flush=True)


# ══ DATI ═══════════════════════════════════════════════════════════════════

def load():
    crypto_d, _, _, _, _ = B.load_data()
    etf = {}
    for sym, _ in ETF_UNIVERSE:
        df = B.yf_daily_df(sym)
        if df is not None:
            etf[sym] = df
    log(f"ETF caricati: {len(etf)}/{len(ETF_UNIVERSE)}")
    return crypto_d, etf


# ══ METRICHE ═══════════════════════════════════════════════════════════════

def curve_stats(eq: pd.Series, periods: int) -> dict:
    eq = eq.dropna()
    if len(eq) < 10 or eq.iloc[0] <= 0:
        return {}
    r = eq.pct_change().dropna()
    years = (eq.index[-1] - eq.index[0]).days / 365.25
    cagr = (eq.iloc[-1] / eq.iloc[0]) ** (1 / years) - 1 if years > 0 else None
    dd = (eq / eq.cummax() - 1).min()
    sd = r.std()
    by_year = {}
    for y, g in eq.groupby(eq.index.year):
        prev = eq[eq.index < g.index[0]]
        base = prev.iloc[-1] if len(prev) else g.iloc[0]
        by_year[str(y)] = (g.iloc[-1] / base - 1) * 100
    return {"total": (eq.iloc[-1] / eq.iloc[0] - 1) * 100, "cagr": cagr * 100 if cagr is not None else None,
            "vol": sd * math.sqrt(periods) * 100, "sharpe": r.mean() / sd * math.sqrt(periods) if sd > 0 else None,
            "max_dd": dd * 100, "years": by_year}


def halves(eq: pd.Series, periods: int) -> dict:
    eq = eq.dropna()
    mid = eq.index[len(eq) // 2]
    return {"A": curve_stats(eq[eq.index <= mid], periods), "B": curve_stats(eq[eq.index >= mid], periods),
            "split": str(mid.date())}


def trade_stats(rets: list[float]) -> dict:
    a = np.array([r for r in rets if r is not None and not math.isnan(r)])
    if not len(a):
        return {"n": 0}
    g, l_ = a[a > 0].sum(), -a[a <= 0].sum()
    return {"n": int(len(a)), "mean": float(a.mean() * 100), "median": float(np.median(a) * 100),
            "win": float((a > 0).mean() * 100), "pf": float(g / l_) if l_ > 0 else None,
            "worst": float(a.min() * 100)}


# ══ E1/E2 · SEGNALI CRYPTO v3 ══════════════════════════════════════════════

def crypto_panel(crypto_d: dict):
    btc = crypto_d["BTC"]["c"]
    btc_ret7 = (btc / btc.shift(7) - 1) * 100
    regime = btc > btc.rolling(50).mean()
    ma50, ma200 = btc.rolling(50).mean(), btc.rolling(200).mean()
    regimes = {"BTC > MA50 (attuale)": regime, "BTC > MA200": btc > ma200,
               "BTC > MA50 e MA50 > MA200": (btc > ma50) & (ma50 > ma200)}
    P = {}
    for base, df in crypto_d.items():
        f = B.daily_features(df, 7, btc_ret7)
        sq = B.setup_score(f)
        f["from_squeeze"] = (sq.notna().astype(float).shift(1).rolling(5).max() > 0) & f["bo20"]
        qv20 = df["qv"].rolling(20).mean()
        liq = qv20 >= 1_500_000
        tr = pd.concat([df["h"] - df["l"], (df["h"] - df["c"].shift()).abs(), (df["l"] - df["c"].shift()).abs()],
                       axis=1).max(axis=1)
        atr = tr.ewm(alpha=1 / 14, adjust=False).mean()
        P[base] = {"df": df, "f": f, "ms": B.momentum_score(f).where(liq), "sq": sq.where(liq), "qv20": qv20,
                   "atr_pct": atr / df["c"], "o": df["o"].to_numpy(), "h": df["h"].to_numpy(),
                   "l": df["l"].to_numpy(), "c": df["c"].to_numpy(), "idx": df.index,
                   "pos": {t: i for i, t in enumerate(df.index)}}
    C = pd.DataFrame({b: p["df"]["c"] for b, p in P.items()}).sort_index()
    above = C > C.rolling(50).mean()
    liq_all = pd.DataFrame({b: p["qv20"] >= 1_500_000 for b, p in P.items()}).reindex(C.index).fillna(False)
    share = (above & liq_all).sum(axis=1) / liq_all.sum(axis=1).replace(0, np.nan)
    regimes["ampiezza: metà delle crypto sopra MA50"] = share >= 0.5
    regimes["nessun filtro"] = pd.Series(True, index=C.index)
    return P, regime, regimes


def v3_events(P: dict, regimes: dict) -> tuple[list[dict], pd.Timestamp]:
    """Tutti i segnali v3 SENZA filtro di regime; ogni evento porta i flag dei vari regimi."""
    M = pd.DataFrame({b: p["ms"] for b, p in P.items()}).sort_index()
    Sq = pd.DataFrame({b: p["sq"] for b, p in P.items()}).reindex(M.index)
    VR = pd.DataFrame({b: p["f"]["vr"] for b, p in P.items()}).reindex(M.index)
    regs = {k: r.reindex(M.index).ffill().fillna(False).astype(bool) for k, r in regimes.items()}
    sc = M.where(M > 0)
    top = (sc.rank(axis=1, ascending=False, method="first") <= B.TOP_N).fillna(False)
    new = top & ~top.shift(1, fill_value=False)
    sq_prev = Sq.notna().astype(float).shift(1).rolling(5).max() > 0
    start = M.index[min(WARMUP, len(M.index) - 1)]
    thr_mom = S.ALERT_MIN_SCORE[("momentum", "crypto")]
    thr_sq = S.ALERT_MIN_SCORE[("squeeze", "crypto")]
    evs = []
    for b in M.columns:
        p = P[b]
        mask = new[b].to_numpy() & (M.index >= start)
        for ts in M.index[mask]:
            s = float(M.at[ts, b])
            is_sq = bool(sq_prev.at[ts, b])
            vr = VR.at[ts, b]
            need_s, need_v = (thr_sq, 1.5) if is_sq else (thr_mom, 2.0)
            if s < need_s or (pd.notna(vr) and vr < need_v):
                continue
            i0 = p["pos"].get(ts)
            if i0 is None:
                continue
            f = p["f"]
            evs.append({"ts": ts, "asset": b, "kind": "squeeze" if is_sq else "momentum", "score": s,
                        "i0": i0, "qv20": float(p["qv20"].iloc[i0]) if pd.notna(p["qv20"].iloc[i0]) else None,
                        "ext": float(f["ext"].iloc[i0]) if pd.notna(f["ext"].iloc[i0]) else None,
                        "atr_pct": float(p["atr_pct"].iloc[i0]), "ch1": float(f["ch1"].iloc[i0]),
                        "regimes": {k: bool(r.at[ts]) for k, r in regs.items()}})
    evs.sort(key=lambda e: e["ts"])
    return evs, start


def simulate(p: dict, i0: int, lag: int, stop: tuple | None, max_days: int, slip: float):
    """Entrata alla chiusura di i0+lag, stop come ordine a riposo (riempito allo stop o
    all'apertura se il prezzo apre sotto, meno lo slittamento), uscita a tempo."""
    c, o, l_ = p["c"], p["o"], p["l"]
    ie = i0 + lag
    if ie + max_days >= len(c):
        return None
    entry = c[ie]
    stop_pct = None
    if stop is not None:
        kind, v = stop
        stop_pct = v if kind == "fix" else min(max(v * p["atr_pct"].iloc[i0], 0.04), 0.35)
    stop_px = entry * (1 - stop_pct) if stop_pct else None
    for j in range(ie + 1, ie + max_days + 1):
        if stop_px is not None and l_[j] <= stop_px:
            px = min(stop_px, o[j]) * (1 - slip)
            return {"ret": px / entry - 1 - 2 * COST_CRYPTO, "ie": ie, "ix": j, "exit_px": px, "entry": entry,
                    "why": "stop", "stop_pct": stop_pct}
    j = ie + max_days
    return {"ret": c[j] / entry - 1 - 2 * COST_CRYPTO, "ie": ie, "ix": j, "exit_px": c[j], "entry": entry,
            "why": "time", "stop_pct": stop_pct or 0.08}


EXEC_VARIANTS = {
    "base: entrata alla chiusura, stop −8%, 5 giorni": dict(lag=0, stop=("fix", 0.08), max_days=5, slip=0.0),
    "entrata il giorno dopo": dict(lag=1, stop=("fix", 0.08), max_days=5, slip=0.0),
    "stop eseguito a mano (−2% di slittamento)": dict(lag=0, stop=("fix", 0.08), max_days=5, slip=0.02),
    "stop −12%": dict(lag=0, stop=("fix", 0.12), max_days=5, slip=0.0),
    "stop 2× ATR": dict(lag=0, stop=("atr", 2.0), max_days=5, slip=0.0),
    "stop 3× ATR": dict(lag=0, stop=("atr", 3.0), max_days=5, slip=0.0),
    "senza stop, 5 giorni": dict(lag=0, stop=None, max_days=5, slip=0.0),
    "stop 3× ATR, entrata il giorno dopo, slittamento 1%": dict(lag=1, stop=("atr", 3.0), max_days=5, slip=0.01),
}


def segment_table(evs: list[dict], res: list[dict | None]) -> dict:
    pairs = [(e, r) for e, r in zip(evs, res) if r is not None]

    def grp(fn):
        out = {}
        for e, r in pairs:
            k = fn(e)
            if k is not None:
                out.setdefault(k, []).append(r["ret"])
        return {k: trade_stats(v) for k, v in out.items()}

    def liq_tier(e):
        q = e["qv20"]
        if q is None:
            return None
        return "a <5M$" if q < 5e6 else "b 5–20M$" if q < 20e6 else "c 20–100M$" if q < 100e6 else "d ≥100M$"

    def ext_tier(e):
        x = e["ext"]
        return None if x is None else ("a ≤15%" if x <= 15 else "b 15–35%" if x <= 35 else "c >35% (esteso)")

    atrs = sorted(e["atr_pct"] for e, _ in pairs)
    t1, t2 = (atrs[len(atrs) // 3], atrs[2 * len(atrs) // 3]) if len(atrs) >= 3 else (0, 0)

    def atr_tier(e):
        a = e["atr_pct"]
        return "a bassa" if a <= t1 else "b media" if a <= t2 else "c alta"

    return {
        "liquidità (volume medio 20g su Binance)": grp(liq_tier),
        "estensione sopra MA20": grp(ext_tier),
        "volatilità (ATR %)": grp(atr_tier),
        "tipo": grp(lambda e: e["kind"]),
        "anno": grp(lambda e: str(e["ts"].year)),
        "score": grp(lambda e: None if e["kind"] == "squeeze" else ("a 100–130" if e["score"] < 130 else "b ≥130")),
    }


def portfolio_daily(trades: list[dict], closes: pd.DataFrame, sizing: str, start, cap=SIZE_CAP,
                    max_open=10, risk=0.01, frac=0.10, max_frac=0.20) -> pd.Series:
    """Portafoglio valutato ogni giorno. trades: asset, entry_ts, exit_ts, entry, exit_px, stop_pct."""
    by_entry: dict = {}
    for t in trades:
        by_entry.setdefault(t["entry_ts"], []).append(t)
    dates = closes.index[closes.index >= start]
    cash, pos, eq_hist = cap, [], []
    last_eq = cap
    for d in dates:
        keep = []
        for p in pos:
            if p["exit_ts"] <= d:
                cash += p["qty"] * p["exit_px"] * (1 - COST_CRYPTO)
            else:
                keep.append(p)
        pos = keep
        for t in by_entry.get(d, []):
            if len(pos) >= max_open or any(p["asset"] == t["asset"] for p in pos):
                continue
            if sizing == "fisso 1.000":
                val = 1000.0
            elif sizing == "10% del capitale":
                val = frac * last_eq
            else:  # rischio fisso
                val = min(risk * last_eq / max(t["stop_pct"], 0.01), max_frac * last_eq)
            val = min(val, cash / (1 + COST_CRYPTO))
            if val <= 1:
                continue
            cash -= val * (1 + COST_CRYPTO)
            pos.append({"asset": t["asset"], "qty": val / t["entry"], "exit_ts": t["exit_ts"], "exit_px": t["exit_px"]})
        row = closes.loc[d]
        last_eq = cash + sum(p["qty"] * row.get(p["asset"], np.nan) for p in pos if pd.notna(row.get(p["asset"], np.nan)))
        eq_hist.append(last_eq)
    return pd.Series(eq_hist, index=dates)


# ══ E3/E4 · ROTAZIONI ══════════════════════════════════════════════════════

def rotation(closes: pd.DataFrame, score: pd.DataFrame, eligible: pd.DataFrame, gate: pd.Series | None,
             rebal: pd.DatetimeIndex, k: int, lag: int, cost: float, start) -> tuple[pd.Series, list]:
    """Ogni data di ribilanciamento: le k migliori per score tra le eleggibili (se gate vero),
    pesi uguali; esecuzione alla chiusura di `lag` giorni dopo. Costi sul turnover."""
    rets = closes.pct_change().fillna(0.0)
    idx = closes.index[closes.index >= start]
    targets = {}
    pos_of = {d: i for i, d in enumerate(closes.index)}
    for r in rebal:
        if r < start or r not in pos_of:
            continue
        ie = pos_of[r] + lag
        if ie >= len(closes.index):
            continue
        sel = []
        if gate is None or bool(gate.get(r, False)):
            s = score.loc[r].where(eligible.loc[r].fillna(False).astype(bool)).dropna()
            sel = list(s.sort_values(ascending=False).index[:k])
        targets[closes.index[ie]] = {a: 1.0 / k for a in sel}
    w: dict = {}
    eq, hist, picks = 1.0, [], []
    for d in idx:
        if w:
            gross = sum(wi * (1 + rets.at[d, a]) for a, wi in w.items())
            cash_w = 1 - sum(w.values())
            new_total = gross + cash_w
            w = {a: wi * (1 + rets.at[d, a]) / new_total for a, wi in w.items()}
            eq *= new_total
        if d in targets:
            tgt = targets[d]
            turnover = sum(abs(tgt.get(a, 0) - w.get(a, 0)) for a in set(tgt) | set(w))
            eq *= 1 - turnover * cost
            w = dict(tgt)
            picks.append((str(d.date()), list(tgt)))
        hist.append(eq)
    return pd.Series(hist, index=idx), picks


def buy_hold(c: pd.Series, start, cost: float) -> pd.Series:
    c = c[c.index >= start].ffill().dropna()
    return c / c.iloc[0] * (1 - cost)


def weekly_dates(index: pd.DatetimeIndex) -> pd.DatetimeIndex:
    return index[index.dayofweek == 0]


def monthly_dates(index: pd.DatetimeIndex) -> pd.DatetimeIndex:
    s = pd.Series(index, index=index)
    return pd.DatetimeIndex(s.groupby([index.year, index.month]).max().tolist())


def crypto_rotations(crypto_d: dict, regime: pd.Series, start) -> dict:
    C = pd.DataFrame({b: df["c"] for b, df in crypto_d.items()}).sort_index()
    QV = pd.DataFrame({b: df["qv"] for b, df in crypto_d.items()}).reindex(C.index)
    hist_ok = C.notna().cumsum() >= 90
    elig = (QV.rolling(20).mean() >= 20e6) & hist_ok
    C = C.ffill()
    reg = regime.reindex(C.index).ffill().fillna(False).astype(bool)
    wk = weekly_dates(C.index)
    out = {"grid": {}, "curves": {}}
    for lb in (14, 28, 56):
        score = C / C.shift(lb) - 1
        for k in (1, 3, 5):
            for lag in (0, 1):
                eq, picks = rotation(C, score, elig, reg, wk, k, lag, COST_CRYPTO, start)
                key = f"K={k} · {lb}g · ritardo {lag}g"
                out["grid"][key] = curve_stats(eq, 365)
                if lb == 28 and k == 3:
                    out["curves"][key] = eq
                    if lag == 1:
                        out["reference_picks"] = picks[-8:]
                        out["reference_halves"] = halves(eq, 365)
    # riferimenti
    btc = C[["BTC"]]
    one = pd.DataFrame(True, index=C.index, columns=["BTC"])
    out["curves"]["BTC comprato e tenuto"] = buy_hold(C["BTC"], start, COST_CRYPTO)
    eq_bt, _ = rotation(btc, pd.DataFrame(1.0, index=C.index, columns=["BTC"]), one, reg, wk, 1, 1, COST_CRYPTO, start)
    out["curves"]["BTC con filtro MA50 (settimanale)"] = eq_bt
    if "ETH" in C.columns:
        be = C[["BTC", "ETH"]]
        ma = be > be.rolling(50).mean()
        eq_be, _ = rotation(be, pd.DataFrame(1.0, index=C.index, columns=["BTC", "ETH"]), ma, None, wk, 2, 1, COST_CRYPTO, start)
        out["curves"]["BTC+ETH, ognuno con filtro MA50"] = eq_be
    out["summary"] = {k: {"all": curve_stats(v, 365), "halves": halves(v, 365)} for k, v in out["curves"].items()}
    out.pop("curves")
    return out


def etf_rotations(etf: dict, start_hint=None) -> dict:
    if len(etf) < 6 or "SPY" not in etf:
        return {"error": "ETF insufficienti"}
    C = pd.DataFrame({s: df["c"] for s, df in etf.items()}).sort_index().ffill()
    ma200 = C.rolling(200).mean()
    trend = C > ma200
    start = C.index[min(252, len(C.index) - 1)]
    mo = monthly_dates(C.index)
    out = {"grid": {}, "summary": {}}
    for lb in (63, 126, 252):
        score = C / C.shift(lb) - 1
        for k in (2, 3, 4):
            eq, picks = rotation(C, score, trend, None, mo, k, 1, COST_ETF, start)
            key = f"K={k} · {lb}g"
            out["grid"][key] = curve_stats(eq, 252)
            if lb == 126 and k == 3:
                out["summary"]["Rotazione: 3 ETF più forti a 6 mesi, sopra MA200 (mensile)"] = {
                    "all": curve_stats(eq, 252), "halves": halves(eq, 252)}
                out["reference_picks"] = picks[-6:]
    one = pd.DataFrame(1.0, index=C.index, columns=C.columns)
    true = pd.DataFrame(True, index=C.index, columns=C.columns)
    eq_spy = buy_hold(C["SPY"], start, COST_ETF)
    out["summary"]["S&P 500 comprato e tenuto"] = {"all": curve_stats(eq_spy, 252), "halves": halves(eq_spy, 252)}
    if "IEF" in C.columns:
        # 60/40 ribilanciato ogni mese
        rets = C[["SPY", "IEF"]].pct_change().fillna(0)
        idx = C.index[C.index >= start]
        w = {"SPY": 0.6, "IEF": 0.4}
        eq, hist = 1.0, []
        mo_set = set(mo)
        for d in idx:
            tot = sum(wi * (1 + rets.at[d, a]) for a, wi in w.items())
            w = {a: wi * (1 + rets.at[d, a]) / tot for a, wi in w.items()}
            eq *= tot
            if d in mo_set:
                eq *= 1 - (abs(w["SPY"] - 0.6) + abs(w["IEF"] - 0.4)) * COST_ETF
                w = {"SPY": 0.6, "IEF": 0.4}
            hist.append(eq)
        s6040 = pd.Series(hist, index=idx)
        out["summary"]["60% S&P 500 / 40% Treasury 7-10 anni"] = {"all": curve_stats(s6040, 252), "halves": halves(s6040, 252)}
    eq_ew, _ = rotation(C, one, true, None, mo, len(C.columns), 1, COST_ETF, start)
    out["summary"][f"Tutti i {len(C.columns)} ETF a pesi uguali (mensile)"] = {"all": curve_stats(eq_ew, 252), "halves": halves(eq_ew, 252)}
    out["period"] = [str(start.date()), str(C.index[-1].date())]
    out["loaded"] = list(C.columns)
    return out


# ══ MAIN ═══════════════════════════════════════════════════════════════════

def main() -> int:
    t0 = time.monotonic()
    OUT.mkdir(exist_ok=True)
    crypto_d, etf = load()
    if "BTC" not in crypto_d:
        log("!! manca BTC")
        return 1
    P, regime, regimes = crypto_panel(crypto_d)
    all_evs, start = v3_events(P, regimes)
    CUR = "BTC > MA50 (attuale)"
    evs = [e for e in all_evs if e["regimes"][CUR]]
    log(f"Eventi v3: {len(evs)} (senza filtro di regime: {len(all_evs)}) dal {start.date()}")
    rep = {"generated": datetime.now(timezone.utc).isoformat(timespec="seconds"),
           "period_start": str(start.date()), "n_crypto": len(crypto_d), "n_events": len(evs)}

    # E1
    e1 = {}
    results = {}
    for name, v in EXEC_VARIANTS.items():
        res = [simulate(P[e["asset"]], e["i0"], **v) for e in evs]
        results[name] = res
        e1[name] = trade_stats([r["ret"] for r in res if r])
    rep["E1_execution"] = e1
    base = "base: entrata alla chiusura, stop −8%, 5 giorni"
    rep["E1_segments_base"] = segment_table(evs, results[base])
    rep["E1_segments_atr3"] = segment_table(evs, results["stop 3× ATR"])
    log("E1 fatto")

    # E2
    closes = pd.DataFrame({b: p["df"]["c"] for b, p in P.items()}).sort_index().ffill()
    e2 = {}
    for vname in (base, "stop 3× ATR", "stop 3× ATR, entrata il giorno dopo, slittamento 1%"):
        trades = []
        for e, r in zip(evs, results[vname]):
            if r is None:
                continue
            p = P[e["asset"]]
            trades.append({"asset": e["asset"], "entry_ts": p["idx"][r["ie"]], "exit_ts": p["idx"][r["ix"]],
                           "entry": r["entry"], "exit_px": r["exit_px"], "stop_pct": r["stop_pct"]})
        for sizing in ("fisso 1.000", "10% del capitale", "rischio 1% per operazione"):
            eq = portfolio_daily(trades, closes, sizing, start)
            e2[f"{vname} · {sizing}"] = {"all": curve_stats(eq, 365), "halves": halves(eq, 365)}
    rep["E2_portfolio"] = e2
    log("E2 fatto")

    # E3
    rep["E3_crypto_rotation"] = crypto_rotations(crypto_d, regime, start)
    log("E3 fatto")

    # E4
    rep["E4_etf_rotation"] = etf_rotations(etf)
    log("E4 fatto")

    # E5 · regime
    closes_all = closes
    mid = all_evs[len(all_evs) // 2]["ts"] if all_evs else start
    ATR3 = dict(lag=0, stop=("atr", 3.0), max_days=5, slip=0.0)
    REAL = dict(lag=1, stop=("atr", 3.0), max_days=5, slip=0.01)
    e5 = {}
    for rk in regimes:
        sub = [e for e in all_evs if e["regimes"][rk]]
        row = {"n": len(sub)}
        for tag, v in (("atr3", ATR3), ("realistico", REAL)):
            res = [simulate(P[e["asset"]], e["i0"], **v) for e in sub]
            pairs = [(e, r) for e, r in zip(sub, res) if r]
            row[tag] = trade_stats([r["ret"] for _, r in pairs])
            row[tag + "_A"] = trade_stats([r["ret"] for e, r in pairs if e["ts"] < mid])
            row[tag + "_B"] = trade_stats([r["ret"] for e, r in pairs if e["ts"] >= mid])
            row[tag + "_years"] = {y: trade_stats([r["ret"] for e, r in pairs if e["ts"].year == int(y)])
                                   for y in sorted({str(e["ts"].year) for e, _ in pairs})}
            trades = [{"asset": e["asset"], "entry_ts": P[e["asset"]]["idx"][r["ie"]],
                       "exit_ts": P[e["asset"]]["idx"][r["ix"]], "entry": r["entry"], "exit_px": r["exit_px"],
                       "stop_pct": r["stop_pct"]} for e, r in pairs]
            eq = portfolio_daily(trades, closes_all, "rischio 1% per operazione", start)
            row[tag + "_portfolio"] = {"all": curve_stats(eq, 365), "halves": halves(eq, 365)}
        e5[rk] = row
    rep["E5_regime"] = e5
    rep["E5_split"] = str(mid.date()) if hasattr(mid, "date") else str(mid)
    log("E5 fatto")

    # E6 · durata e liquidità (regime attuale, stop 3× ATR)
    e6 = {}
    for liq_name, liq_min in (("tutte", 0), ("liquidità ≥ 20 M$", 20e6)):
        sub = [e for e in evs if (e["qv20"] or 0) >= liq_min]
        for days in (3, 5, 7, 10):
            for tag, lag, slip in (("chiusura", 0, 0.0), ("giorno dopo +1%", 1, 0.01)):
                res = [simulate(P[e["asset"]], e["i0"], lag=lag, stop=("atr", 3.0), max_days=days, slip=slip) for e in sub]
                pairs = [(e, r) for e, r in zip(sub, res) if r]
                key = f"{liq_name} · {days} giorni · {tag}"
                st = trade_stats([r["ret"] for _, r in pairs])
                st["A"] = trade_stats([r["ret"] for e, r in pairs if e["ts"] < mid]).get("pf")
                st["B"] = trade_stats([r["ret"] for e, r in pairs if e["ts"] >= mid]).get("pf")
                st["y2026"] = trade_stats([r["ret"] for e, r in pairs if e["ts"].year == 2026]).get("pf")
                if days == 5:
                    trades = [{"asset": e["asset"], "entry_ts": P[e["asset"]]["idx"][r["ie"]],
                               "exit_ts": P[e["asset"]]["idx"][r["ix"]], "entry": r["entry"],
                               "exit_px": r["exit_px"], "stop_pct": r["stop_pct"]} for e, r in pairs]
                    eq = portfolio_daily(trades, closes_all, "rischio 1% per operazione", start)
                    st["portfolio"] = curve_stats(eq, 365)
                    st["portfolio_halves"] = halves(eq, 365)
                e6[key] = st
    rep["E6_hold_liquidity"] = e6
    log("E6 fatto")

    (OUT / "report.json").write_text(json.dumps(rep, indent=1, default=str))
    write_markdown(rep)
    log(f"Fatto in {time.monotonic() - t0:.0f}s")
    return 0


def f_(x, d=1, pct=True):
    if x is None or (isinstance(x, float) and math.isnan(x)):
        return "—"
    return f"{x:+.{d}f}%" if pct else f"{x:.{d}f}"


def write_markdown(rep: dict) -> None:
    L = [f"# Ricerca modello v4 — {rep['generated']}", "",
         f"Valutazione dal {rep['period_start']} · {rep['n_crypto']} crypto · {rep['n_events']} segnali v3", ""]

    def tstat_rows(d):
        rows = ["| Variante | n | media | mediana | positivi | PF | peggiore |", "|---|---|---|---|---|---|---|"]
        for k, s in d.items():
            rows.append(f"| {k} | {s.get('n')} | {f_(s.get('mean'),2)} | {f_(s.get('median'),2)} | "
                        f"{f_(s.get('win'),0,False)}% | {f_(s.get('pf'),2,False)} | {f_(s.get('worst'),1)} |")
        return rows

    L += ["## E1 · Esecuzione dei segnali crypto v3", ""] + tstat_rows(rep["E1_execution"]) + [""]
    for nm in ("E1_segments_base", "E1_segments_atr3"):
        L += [f"### Segmenti ({'stop −8%' if nm.endswith('base') else 'stop 3× ATR'})", ""]
        for seg, d in rep[nm].items():
            L += [f"**{seg}**", ""] + tstat_rows(dict(sorted(d.items()))) + [""]

    def curve_rows(d):
        rows = ["| Variante | totale | CAGR | vol | Sharpe | calo max | metà A | metà B |", "|---|---|---|---|---|---|---|---|"]
        for k, s in d.items():
            a, h = s.get("all", {}), s.get("halves", {})
            rows.append(f"| {k} | {f_(a.get('total'),0)} | {f_(a.get('cagr'),1)} | {f_(a.get('vol'),0)} | "
                        f"{f_(a.get('sharpe'),2,False)} | {f_(a.get('max_dd'),1)} | "
                        f"{f_(h.get('A',{}).get('cagr'),1)} | {f_(h.get('B',{}).get('cagr'),1)} |")
        return rows

    def year_rows(d):
        years = sorted({y for s in d.values() for y in s.get("all", {}).get("years", {})})
        rows = ["| Variante | " + " | ".join(years) + " |", "|---" * (len(years) + 1) + "|"]
        for k, s in d.items():
            ys = s.get("all", {}).get("years", {})
            rows.append(f"| {k} | " + " | ".join(f_(ys.get(y), 0) for y in years) + " |")
        return rows

    L += ["## E2 · Portafoglio valutato ogni giorno (segnali v3)", ""] + curve_rows(rep["E2_portfolio"]) + [""] \
        + year_rows(rep["E2_portfolio"]) + [""]
    e3 = rep["E3_crypto_rotation"]
    L += ["## E3 · Rotazione crypto settimanale", "", "Riferimento: K=3, 28 giorni, ritardo 1 giorno.", ""] \
        + curve_rows(e3["summary"]) + [""] + year_rows(e3["summary"]) + ["", "Griglia di sensibilità (CAGR · Sharpe · calo max):", "",
                                                                         "| Variante | CAGR | Sharpe | calo max |", "|---|---|---|---|"]
    for k, s in e3["grid"].items():
        L.append(f"| {k} | {f_(s.get('cagr'),1)} | {f_(s.get('sharpe'),2,False)} | {f_(s.get('max_dd'),1)} |")
    L += ["", f"Ultime scelte (riferimento): {e3.get('reference_picks')}", ""]
    e4 = rep["E4_etf_rotation"]
    if "error" not in e4:
        L += ["## E4 · Rotazione ETF mensile", "", f"Periodo {e4['period']} · ETF {e4['loaded']}", ""] \
            + curve_rows(e4["summary"]) + [""] + year_rows(e4["summary"]) + ["", "Griglia di sensibilità:", "",
                                                                             "| Variante | CAGR | Sharpe | calo max |", "|---|---|---|---|"]
        for k, s in e4["grid"].items():
            L.append(f"| {k} | {f_(s.get('cagr'),1)} | {f_(s.get('sharpe'),2,False)} | {f_(s.get('max_dd'),1)} |")
        L += ["", f"Ultime scelte: {e4.get('reference_picks')}", ""]
    if "E5_regime" in rep:
        L += ["## E5 · Definizione del regime (stop 3× ATR)", "", f"Metà A/B divise al {rep['E5_split']}", "",
              "| Regime | n | media | PF | PF metà A | PF metà B | realistico: media | PF | PF A | PF B | portafoglio CAGR | Sharpe | calo max |",
              "|---|---|---|---|---|---|---|---|---|---|---|---|---|"]
        for k, r in rep["E5_regime"].items():
            a, ra, pt = r["atr3"], r["realistico"], r["realistico_portfolio"]["all"]
            L.append(f"| {k} | {r['n']} | {f_(a.get('mean'),2)} | {f_(a.get('pf'),2,False)} | {f_(r['atr3_A'].get('pf'),2,False)} | "
                     f"{f_(r['atr3_B'].get('pf'),2,False)} | {f_(ra.get('mean'),2)} | {f_(ra.get('pf'),2,False)} | "
                     f"{f_(r['realistico_A'].get('pf'),2,False)} | {f_(r['realistico_B'].get('pf'),2,False)} | "
                     f"{f_(pt.get('cagr'),1)} | {f_(pt.get('sharpe'),2,False)} | {f_(pt.get('max_dd'),1)} |")
        years = sorted({y for r in rep["E5_regime"].values() for y in r["realistico_years"]})
        L += ["", "PF per anno (realistico):", "", "| Regime | " + " | ".join(years) + " |", "|---" * (len(years) + 1) + "|"]
        for k, r in rep["E5_regime"].items():
            L.append(f"| {k} | " + " | ".join(f"{f_(r['realistico_years'].get(y, {}).get('pf'),2,False)} (n={r['realistico_years'].get(y, {}).get('n', 0)})" for y in years) + " |")
        L += [""]
    if "E6_hold_liquidity" in rep:
        L += ["## E6 · Durata e liquidità (regime attuale, stop 3× ATR)", "",
              "| Variante | n | media | positivi | PF | PF A | PF B | PF 2026 | portafoglio CAGR | Sharpe | calo max |",
              "|---|---|---|---|---|---|---|---|---|---|---|"]
        for k, st in rep["E6_hold_liquidity"].items():
            pt = st.get("portfolio", {})
            L.append(f"| {k} | {st.get('n')} | {f_(st.get('mean'),2)} | {f_(st.get('win'),0,False)}% | {f_(st.get('pf'),2,False)} | "
                     f"{f_(st.get('A'),2,False)} | {f_(st.get('B'),2,False)} | {f_(st.get('y2026'),2,False)} | "
                     f"{f_(pt.get('cagr'),1)} | {f_(pt.get('sharpe'),2,False)} | {f_(pt.get('max_dd'),1)} |")
        L += [""]
    (OUT / "report.md").write_text("\n".join(L))


if __name__ == "__main__":
    sys.exit(main())
