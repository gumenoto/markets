#!/usr/bin/env python3
"""
Ricerca azioni (gira su GitHub Actions, scrive research_results/stocks_report.*).

Domanda: esiste un modello sulle azioni che un piccolo investitore può eseguire a mano
(segnale a fine giornata, ordine il giorno dopo) e che batte il semplice tenere l'indice?

Universo: le azioni dell'S&P 500 di oggi, 10 anni di barre giornaliere (Yahoo, prezzi
rettificati per dividendi e split). Ogni azione conta solo DOPO la sua data di ingresso
nell'indice: così non si "comprano" in anticipo i titoli che sono entrati nell'indice
proprio perché erano saliti. Resta il survivorship bias (mancano le azioni uscite
dall'indice): per questo ogni strategia è confrontata anche con lo stesso universo a pesi
uguali, che ha lo stesso difetto.

Strategie, decise PRIMA di guardare i risultati:

  S1  Breakout dello screener: stesso punteggio «Accelerazioni» del sito, nuovo ingresso nei
      primi 10 dell'universo, soglie attuali (score ≥ 60 e volume ≥ 1,5×; da squeeze ≥ 40 e
      ≥ 1,3×), solo con S&P 500 sopra la media a 200 giorni.
  S2  Momentum mensile classico (12-1): a fine mese le 10 azioni con il rendimento più alto
      da 12 mesi a 1 mese fa, pesi uguali, solo con S&P 500 sopra la media a 200 giorni.
  S3  Salto con volume (proxy del «post-earnings drift»): giornata ≥ +5% con volume ≥ 3× la
      media a 20 giorni e chiusura nella metà alta dell'escursione.

Esecuzione: segnale alla chiusura del giorno t, entrata all'apertura di t+1 (variante: alla
chiusura di t+1), costi 0,2% per lato (1 € su 1.000 € + spread). Uscite provate, poche e
fissate prima: a tempo (5, 10, 20, 40 sedute) con stop a 3× ATR; 20 sedute senza stop;
regola del portafoglio sperimentale (stop −12%, trailing 20% dopo +10%, 20 sedute).

Regola per promuovere una strategia a «operativa» (alert veri):
  S1/S3  con entrata all'apertura del giorno dopo: profit factor ≥ 1,3; eccesso medio
         sull'universo a pesi uguali positivo con t ≥ 2 (raggruppato per mese); profit
         factor > 1,1 ed eccesso positivo in ENTRAMBE le metà del periodo.
  S2     rendimento annuo sopra S&P 500 e sopra l'universo a pesi uguali in ENTRAMBE le
         metà, con calo massimo non peggiore di quello dell'S&P 500.
Sono tre strategie e più uscite: con tanti tentativi qualcosa passa per caso, quindi una
promozione richiede che passino le uscite vicine, non una sola.
"""

from __future__ import annotations

import io
import json
import math
import sys
import time
from collections import defaultdict
from datetime import datetime, timezone
from pathlib import Path

import numpy as np
import pandas as pd
import requests

import backtest as B
import research as R
import screener as S

OUT = Path("research_results")
CONSTITUENTS_URL = "https://raw.githubusercontent.com/datasets/s-and-p-500-companies/main/data/constituents.csv"
PERIOD = "10y"
COST = 0.002          # per lato: 1 € su 1.000 € + spread/slittamento
WARMUP = 280          # sedute prima di iniziare a valutare (servono 252 + 21 per il momentum 12-1)
SLOTS = 10
TOP_N = 10
MIN_STOCKS = 300      # sotto questa soglia il download è fallito: meglio fermarsi

EXITS = {
    "5 sedute, stop 3× ATR": dict(stop=("atr", 3.0), trail=None, max_days=5),
    "10 sedute, stop 3× ATR": dict(stop=("atr", 3.0), trail=None, max_days=10),
    "20 sedute, stop 3× ATR": dict(stop=("atr", 3.0), trail=None, max_days=20),
    "40 sedute, stop 3× ATR": dict(stop=("atr", 3.0), trail=None, max_days=40),
    "20 sedute, senza stop": dict(stop=None, trail=None, max_days=20),
    "regola sperimentale (−12%, trailing 20% da +10%, 20 sedute)": dict(stop=("fix", 0.12), trail=(0.20, 0.10), max_days=20),
}


def log(msg: str) -> None:
    print(f"[{datetime.now(timezone.utc).strftime('%H:%M:%S')}] {msg}", flush=True)


# ══ DATI ═══════════════════════════════════════════════════════════════════

def load_constituents() -> pd.DataFrame:
    r = requests.get(CONSTITUENTS_URL, timeout=30)
    r.raise_for_status()
    d = pd.read_csv(io.StringIO(r.text))
    d["yahoo"] = d["Symbol"].str.replace(".", "-", regex=False)
    d["added"] = pd.to_datetime(d["Date added"], errors="coerce").dt.tz_localize("UTC")
    return d


def _frame(h: pd.DataFrame) -> pd.DataFrame | None:
    if h is None or h.empty or "Close" not in h:
        return None
    h = h.dropna(subset=["Close"])
    if h.empty:
        return None
    df = pd.DataFrame({
        "o": h["Open"].astype(float), "h": h["High"].astype(float), "l": h["Low"].astype(float),
        "c": h["Close"].astype(float), "v": h["Volume"].fillna(0).astype(float),
    })
    df.index = pd.to_datetime([d.date() for d in h.index]).tz_localize("UTC")
    df = df[~df.index.duplicated(keep="last")].sort_index()
    for col in ("o", "h", "l"):
        df[col] = df[col].where(df[col] > 0, df["c"])
    df["h"] = df[["h", "o", "c"]].max(axis=1)
    df["l"] = df[["l", "o", "c"]].min(axis=1)
    return df


def load_prices(symbols: list[str]) -> dict:
    yf = S.yf
    out: dict = {}
    chunk_n = 40
    for i in range(0, len(symbols), chunk_n):
        chunk = [s for s in symbols[i:i + chunk_n] if s not in out]
        for attempt in range(3):
            todo = [s for s in chunk if s not in out]
            if not todo:
                break
            try:
                raw = yf.download(todo, period=PERIOD, interval="1d", auto_adjust=True, group_by="ticker",
                                  threads=True, progress=False)
            except Exception as e:  # noqa: BLE001
                log(f"  ! download {todo[0]}…: {e}")
                time.sleep(5 * (attempt + 1))
                continue
            for s in todo:
                try:
                    sub = raw[s] if isinstance(raw.columns, pd.MultiIndex) else raw
                except Exception:  # noqa: BLE001
                    continue
                df = _frame(sub)
                if df is not None and len(df) > 60:
                    out[s] = df
            if any(s not in out for s in todo):
                time.sleep(4 * (attempt + 1))
        if (i // chunk_n) % 3 == 0:
            log(f"  prezzi: {len(out)}/{min(i + chunk_n, len(symbols))}")
        time.sleep(1.0)
    for s in symbols:  # ultimi mancanti, uno alla volta
        if s in out:
            continue
        try:
            df = _frame(yf.Ticker(s).history(period=PERIOD, interval="1d", auto_adjust=True))
        except Exception as e:  # noqa: BLE001
            log(f"  ! {s}: {e}")
            df = None
        if df is not None and len(df) > 60:
            out[s] = df
        time.sleep(0.5)
    return out


def load():
    cons = load_constituents()
    symbols = cons["yahoo"].tolist()
    log(f"Costituenti S&P 500: {len(symbols)}")
    prices = load_prices(symbols + ["SPY"])
    spy = prices.pop("SPY", None)
    log(f"Prezzi caricati: {len(prices)}/{len(symbols)} · SPY {'ok' if spy is not None else 'MANCA'}")
    added = {r.yahoo: r.added for r in cons.itertuples()}
    return prices, spy, added


# ══ PANNELLO ═══════════════════════════════════════════════════════════════

def build_panel(prices: dict, spy: pd.DataFrame, added: dict):
    cal = spy.index
    spy_ret5 = (spy["c"] / spy["c"].shift(5) - 1) * 100
    regime = (spy["c"] > spy["c"].rolling(200).mean())
    P = {}
    for s, df in prices.items():
        df = df[df.index.isin(cal)]
        if len(df) < 260:
            continue
        f = B.daily_features(df, 5, spy_ret5)
        sq = B.setup_score(f)
        f["from_squeeze"] = (sq.notna().astype(float).shift(1).rolling(5).max() > 0) & f["bo20"]
        tr = pd.concat([df["h"] - df["l"], (df["h"] - df["c"].shift()).abs(), (df["l"] - df["c"].shift()).abs()],
                       axis=1).max(axis=1)
        atr = tr.ewm(alpha=1 / 14, adjust=False).mean()
        a = added.get(s)
        member = pd.Series(df.index >= a, index=df.index) if a is not None and pd.notna(a) \
            else pd.Series(True, index=df.index)
        P[s] = {"df": df, "f": f, "member": member, "ms": B.momentum_score(f).where(member),
                "sq_prev": sq.notna().astype(float).shift(1).rolling(5).max() > 0,
                "atr_pct": (atr / df["c"]).to_numpy(), "o": df["o"].to_numpy(), "h": df["h"].to_numpy(),
                "l": df["l"].to_numpy(), "c": df["c"].to_numpy(), "idx": df.index,
                "pos": {t: i for i, t in enumerate(df.index)}}
    C = pd.DataFrame({s: p["df"]["c"] for s, p in P.items()}).reindex(cal)
    O = pd.DataFrame({s: p["df"]["o"] for s, p in P.items()}).reindex(cal)
    MEM = pd.DataFrame({s: p["member"] for s, p in P.items()}).reindex(cal).fillna(False).astype(bool)
    cc = C.pct_change(fill_method=None).where(MEM)
    oc = (C / O - 1).where(MEM)
    ew_cc = cc.mean(axis=1).fillna(0.0)
    ew_oc = oc.mean(axis=1).fillna(0.0)
    bench = {"cum": (1 + ew_cc).cumprod(), "oc": ew_oc, "n_members": MEM.sum(axis=1)}
    return P, C, MEM, regime, bench


# ══ EVENTI ═════════════════════════════════════════════════════════════════

def breakout_events(P: dict, cal: pd.DatetimeIndex, regime: pd.Series, start) -> list[dict]:
    """S1: nuovo ingresso nei primi 10 per punteggio «Accelerazioni», soglie attuali dello screener."""
    M = pd.DataFrame({s: p["ms"] for s, p in P.items()}).reindex(cal)
    VR = pd.DataFrame({s: p["f"]["vr"] for s, p in P.items()}).reindex(cal)
    SQ = pd.DataFrame({s: p["sq_prev"] for s, p in P.items()}).reindex(cal).fillna(False).astype(bool)
    reg = regime.reindex(cal).fillna(False).astype(bool)
    sc = M.where(M > 0)
    top = (sc.rank(axis=1, ascending=False, method="first") <= TOP_N).fillna(False)
    new = top & ~top.shift(1, fill_value=False)
    thr_m, thr_s = S.ALERT_MIN_SCORE[("momentum", "brokerage")], S.ALERT_MIN_SCORE[("squeeze", "brokerage")]
    vr_m, vr_s = S.ALERT_MIN_VOLUME[("momentum", "brokerage")], S.ALERT_MIN_VOLUME[("squeeze", "brokerage")]
    evs = []
    for s in M.columns:
        p = P[s]
        mask = new[s].to_numpy() & (cal >= start)
        for ts in cal[mask]:
            score = float(M.at[ts, s])
            is_sq = bool(SQ.at[ts, s])
            vr = VR.at[ts, s]
            need_s, need_v = (thr_s, vr_s) if is_sq else (thr_m, vr_m)
            if score < need_s or pd.isna(vr) or vr < need_v:
                continue
            i0 = p["pos"].get(ts)
            if i0 is None:
                continue
            evs.append({"ts": ts, "asset": s, "kind": "squeeze" if is_sq else "momentum", "score": score,
                        "i0": i0, "regime": bool(reg.at[ts]), "ch1": float(p["f"]["ch1"].iloc[i0])})
    evs.sort(key=lambda e: e["ts"])
    return evs


def gap_events(P: dict, regime: pd.Series, start) -> list[dict]:
    """S3: giornata ≥ +5% con volume ≥ 3× la media a 20 giorni e chiusura nella metà alta."""
    evs = []
    for s, p in P.items():
        df, f = p["df"], p["f"]
        cond = (f["ch1"] >= 5) & (f["vr"] >= 3) & (df["c"] >= (df["h"] + df["l"]) / 2) & p["member"] \
            & (df.index >= start)
        last = -10_000
        for i in np.flatnonzero(cond.to_numpy()):
            if i - last < 20:      # un solo evento per azione ogni 20 sedute
                continue
            last = i
            ts = df.index[i]
            evs.append({"ts": ts, "asset": s, "kind": "gap", "score": float(f["ch1"].iloc[i]), "i0": int(i),
                        "regime": bool(regime.get(ts, False)), "ch1": float(f["ch1"].iloc[i])})
    evs.sort(key=lambda e: e["ts"])
    return evs


def simulate(p: dict, i0: int, entry: str, stop: tuple | None, trail: tuple | None, max_days: int):
    """Segnale alla chiusura di i0. entry «open»: apertura del giorno dopo; «close»: chiusura del
    giorno dopo. Lo stop è un ordine a riposo: eseguito allo stop, o all'apertura se apre sotto."""
    o, h, l_, c = p["o"], p["h"], p["l"], p["c"]
    ie = i0 + 1
    if ie + max_days >= len(c):
        return None
    entry_px = o[ie] if entry == "open" else c[ie]
    if not entry_px or not math.isfinite(entry_px) or entry_px <= 0:
        return None
    stop_pct = None
    if stop is not None:
        kind, v = stop
        stop_pct = v if kind == "fix" else float(min(max(v * p["atr_pct"][i0], 0.04), 0.25))
    stop_px = entry_px * (1 - stop_pct) if stop_pct else None
    peak = entry_px
    first = ie if entry == "open" else ie + 1
    for j in range(first, ie + max_days + 1):
        lvl = stop_px
        if trail and peak >= entry_px * (1 + trail[1]):
            lvl = max(lvl or 0.0, peak * (1 - trail[0]))
        if lvl and l_[j] <= lvl:
            px = min(lvl, o[j])
            return {"ret": px / entry_px - 1 - 2 * COST, "ie": ie, "ix": j, "entry": entry_px, "exit_px": px,
                    "why": "stop"}
        peak = max(peak, h[j])
    j = ie + max_days
    return {"ret": c[j] / entry_px - 1 - 2 * COST, "ie": ie, "ix": j, "entry": entry_px, "exit_px": c[j],
            "why": "tempo"}


def run_variant(P: dict, evs: list[dict], bench: dict, entry: str, ex: dict) -> list[dict]:
    out = []
    cum, oc = bench["cum"], bench["oc"]
    for e in evs:
        p = P[e["asset"]]
        r = simulate(p, e["i0"], entry, ex["stop"], ex["trail"], ex["max_days"])
        if r is None:
            continue
        d_e, d_x = p["idx"][r["ie"]], p["idx"][r["ix"]]
        b = cum.get(d_x, np.nan) / cum.get(d_e, np.nan)
        if entry == "open":
            b *= 1 + oc.get(d_e, 0.0)
        out.append({**e, **r, "entry_ts": d_e, "exit_ts": d_x, "bench": b - 1, "excess": r["ret"] - (b - 1)})
    return out


def month_t(tr: list[dict], key: str = "excess") -> dict:
    by = defaultdict(list)
    for t in tr:
        if t.get(key) is not None and math.isfinite(t[key]):
            by[(t["entry_ts"].year, t["entry_ts"].month)].append(t[key])
    m = np.array([np.mean(v) for v in by.values()])
    if len(m) < 6:
        return {"months": int(len(m)), "t": None}
    sd = m.std(ddof=1)
    return {"months": int(len(m)), "mean_month": float(m.mean() * 100),
            "t": float(m.mean() / (sd / math.sqrt(len(m)))) if sd > 0 else None}


def summarize(tr: list[dict], mid) -> dict:
    def block(sub):
        st = R.trade_stats([t["ret"] for t in sub])
        ex = [t["excess"] for t in sub if math.isfinite(t["excess"])]
        st["excess"] = float(np.mean(ex) * 100) if ex else None
        st["stops"] = float(np.mean([t["why"] == "stop" for t in sub]) * 100) if sub else None
        return st
    s = block(tr)
    s["t_month"] = month_t(tr)
    s["A"] = block([t for t in tr if t["ts"] < mid])
    s["B"] = block([t for t in tr if t["ts"] >= mid])
    s["A"]["t_month"] = month_t([t for t in tr if t["ts"] < mid])
    s["B"]["t_month"] = month_t([t for t in tr if t["ts"] >= mid])
    s["years"] = {str(y): block([t for t in tr if t["ts"].year == y]) for y in sorted({t["ts"].year for t in tr})}
    return s


def portfolio_events(tr: list[dict], C: pd.DataFrame, start, slots: int = SLOTS) -> pd.Series:
    """Portafoglio valutato ogni giorno: `slots` posizioni da 1/slots del capitale. Entrate al prezzo
    di entrata del giorno, uscite al prezzo di uscita; uno slot liberato oggi si riusa da domani."""
    by_entry = defaultdict(list)
    for t in tr:
        by_entry[t["entry_ts"]].append(t)
    Cf = C.ffill()
    dates = Cf.index[Cf.index >= start]
    cash, pos, hist, last_eq = 1.0, [], [], 1.0
    for d in dates:
        for t in sorted(by_entry.get(d, []), key=lambda t: -t["score"]):
            if len(pos) >= slots or any(p["asset"] == t["asset"] for p in pos):
                continue
            val = min(last_eq / slots, cash / (1 + COST))
            if val <= 1e-9:
                continue
            cash -= val * (1 + COST)
            pos.append({"asset": t["asset"], "qty": val / t["entry"], "exit_ts": t["exit_ts"], "exit_px": t["exit_px"]})
        keep = []
        for p in pos:
            if p["exit_ts"] <= d:
                cash += p["qty"] * p["exit_px"] * (1 - COST)
            else:
                keep.append(p)
        pos = keep
        row = Cf.loc[d]
        last_eq = cash + sum(p["qty"] * row[p["asset"]] for p in pos if pd.notna(row[p["asset"]]))
        hist.append(last_eq)
    return pd.Series(hist, index=dates)


# ══ MAIN ═══════════════════════════════════════════════════════════════════

def main() -> int:
    t0 = time.monotonic()
    OUT.mkdir(exist_ok=True)
    prices, spy, added = load()
    if spy is None or len(prices) < MIN_STOCKS:
        log("!! dati insufficienti")
        return 1
    P, C, MEM, regime, bench = build_panel(prices, spy, added)
    cal = spy.index
    start = cal[min(WARMUP, len(cal) - 1)]
    mid = cal[(WARMUP + len(cal)) // 2]
    log(f"Pannello: {len(P)} azioni · valutazione dal {start.date()} · metà al {mid.date()}")
    rep = {"generated": datetime.now(timezone.utc).isoformat(timespec="seconds"), "n_stocks": len(P),
           "period": [str(start.date()), str(cal[-1].date())], "split": str(mid.date()), "cost_per_side": COST,
           "members_start": int(bench["n_members"].get(start, 0)), "members_end": int(bench["n_members"].iloc[-1])}

    # ── riferimenti ──
    spy_c = spy["c"]
    ew = bench["cum"][bench["cum"].index >= start]
    ew = ew / ew.iloc[0]
    one = pd.DataFrame(1.0, index=cal, columns=["SPY"])
    true = pd.DataFrame(True, index=cal, columns=["SPY"])
    mo = R.monthly_dates(cal)
    spy_f, _ = R.rotation(spy[["c"]].rename(columns={"c": "SPY"}), one, true, regime, mo, 1, 1, COST, start)
    refs = {"S&P 500 comprato e tenuto": R.buy_hold(spy_c, start, COST),
            f"Universo a pesi uguali ({rep['members_end']} azioni, senza costi)": ew,
            "S&P 500 solo sopra la media a 200 giorni (controllo mensile)": spy_f}
    rep["benchmarks"] = {k: {"all": R.curve_stats(v, 252), "halves": R.halves(v, 252)} for k, v in refs.items()}

    # ── S1 e S3 ──
    all_s1 = breakout_events(P, cal, regime, start)
    all_s3 = gap_events(P, regime, start)
    ev_sets = {
        "S1 breakout dello screener · con regime": [e for e in all_s1 if e["regime"]],
        "S1 breakout dello screener · senza filtro di regime": all_s1,
        "S3 salto con volume · con regime": [e for e in all_s3 if e["regime"]],
        "S3 salto con volume · senza filtro di regime": all_s3,
    }
    rep["events"] = {}
    for name, evs in ev_sets.items():
        months = max(1.0, (cal[-1] - start).days / 30.4)
        block = {"n_events": len(evs), "per_month": len(evs) / months, "variants": {}}
        for ex_name, ex in EXITS.items():
            for entry in ("open", "close"):
                tr = run_variant(P, evs, bench, entry, ex)
                st = summarize(tr, mid)
                if entry == "open":
                    eq = portfolio_events(tr, C, start)
                    st["portfolio"] = {"all": R.curve_stats(eq, 252), "halves": R.halves(eq, 252)}
                block["variants"][f"{ex_name} · entrata {'apertura' if entry == 'open' else 'chiusura'} del giorno dopo"] = st
        if name.startswith("S1"):
            tr = run_variant(P, evs, bench, "open", EXITS["20 sedute, stop 3× ATR"])
            block["by_kind"] = {k: summarize([t for t in tr if t["kind"] == k], mid) for k in ("momentum", "squeeze")}
        rep["events"][name] = block
        log(f"{name}: {len(evs)} eventi")

    # ── S2 ──
    Cf = C.ffill()
    elig = MEM & C.notna() & (C.notna().cumsum() >= 273)
    s2 = {}
    for lb_name, lb in (("12-1", 252), ("6-1", 126)):
        score = Cf.shift(21) / Cf.shift(lb) - 1
        for k in (10, 20):
            for gate_name, gate in (("con regime", regime), ("senza filtro", None)):
                eq, picks = R.rotation(Cf, score, elig, gate, mo, k, 1, COST, start)
                key = f"{k} azioni · momentum {lb_name} · {gate_name}"
                s2[key] = {"all": R.curve_stats(eq, 252), "halves": R.halves(eq, 252)}
                if lb == 252 and k == 10 and gate is not None:
                    s2[key]["last_picks"] = picks[-3:]
    rep["S2_momentum"] = s2
    log("S2 fatto")

    (OUT / "stocks_report.json").write_text(json.dumps(rep, indent=1, default=str))
    write_markdown(rep)
    log(f"Fatto in {time.monotonic() - t0:.0f}s")
    return 0


f_ = R.f_


def write_markdown(rep: dict) -> None:
    L = [f"# Ricerca azioni — {rep['generated']}", "",
         f"Periodo {rep['period'][0]} → {rep['period'][1]} · {rep['n_stocks']} azioni dell'S&P 500 di oggi "
         f"(membri dell'indice: {rep['members_start']} all'inizio, {rep['members_end']} alla fine) · "
         f"metà A/B divise al {rep['split']} · costi {rep['cost_per_side'] * 100:.1f}% per lato", ""]

    def curve_rows(d):
        rows = ["| | totale | all'anno | volatilità | Sharpe | calo max | metà A all'anno | metà B all'anno |",
                "|---|---|---|---|---|---|---|---|"]
        for k, s in d.items():
            a, h = s.get("all", {}), s.get("halves", {})
            rows.append(f"| {k} | {f_(a.get('total'), 0)} | {f_(a.get('cagr'), 1)} | {f_(a.get('vol'), 0)} | "
                        f"{f_(a.get('sharpe'), 2, False)} | {f_(a.get('max_dd'), 1)} | "
                        f"{f_(h.get('A', {}).get('cagr'), 1)} | {f_(h.get('B', {}).get('cagr'), 1)} |")
        return rows

    def year_rows(d):
        years = sorted({y for s in d.values() for y in s.get("all", {}).get("years", {})})
        rows = ["| | " + " | ".join(years) + " |", "|---" * (len(years) + 1) + "|"]
        for k, s in d.items():
            ys = s.get("all", {}).get("years", {})
            rows.append(f"| {k} | " + " | ".join(f_(ys.get(y), 0) for y in years) + " |")
        return rows

    L += ["## Riferimenti", ""] + curve_rows(rep["benchmarks"]) + [""] + year_rows(rep["benchmarks"]) + [""]
    for name, block in rep["events"].items():
        L += [f"## {name}", "", f"{block['n_events']} segnali ({block['per_month']:.1f} al mese)", "",
              "| Uscita · entrata | n | media | positivi | PF | eccesso vs universo | t (mesi) | stop | PF A | ecc. A | PF B | ecc. B |",
              "|---|---|---|---|---|---|---|---|---|---|---|---|"]
        for k, s in block["variants"].items():
            L.append(f"| {k} | {s.get('n')} | {f_(s.get('mean'), 2)} | {f_(s.get('win'), 0, False)}% | "
                     f"{f_(s.get('pf'), 2, False)} | {f_(s.get('excess'), 2)} | {f_(s['t_month'].get('t'), 1, False)} | "
                     f"{f_(s.get('stops'), 0, False)}% | {f_(s['A'].get('pf'), 2, False)} | {f_(s['A'].get('excess'), 2)} | "
                     f"{f_(s['B'].get('pf'), 2, False)} | {f_(s['B'].get('excess'), 2)} |")
        ports = {k: v["portfolio"] for k, v in block["variants"].items() if "portfolio" in v}
        L += ["", f"Portafoglio a {SLOTS} posizioni (entrata all'apertura del giorno dopo):", ""] + curve_rows(ports) \
            + [""] + year_rows(ports) + [""]
        if "by_kind" in block:
            L += ["Per tipo (20 sedute, stop 3× ATR, entrata all'apertura):", "",
                  "| Tipo | n | media | PF | eccesso | PF A | PF B |", "|---|---|---|---|---|---|---|"]
            for k, s in block["by_kind"].items():
                L.append(f"| {k} | {s.get('n')} | {f_(s.get('mean'), 2)} | {f_(s.get('pf'), 2, False)} | "
                         f"{f_(s.get('excess'), 2)} | {f_(s['A'].get('pf'), 2, False)} | {f_(s['B'].get('pf'), 2, False)} |")
            L += [""]
        ref = block["variants"].get("20 sedute, stop 3× ATR · entrata apertura del giorno dopo")
        if ref:
            L += ["Per anno (20 sedute, stop 3× ATR, entrata all'apertura): "
                  + " · ".join(f"{y} n={s.get('n')} media {f_(s.get('mean'), 2)} ecc. {f_(s.get('excess'), 2)}"
                               for y, s in ref["years"].items()), ""]
    L += ["## S2 · Momentum mensile", ""] + curve_rows(rep["S2_momentum"]) + [""] + year_rows(rep["S2_momentum"]) + [""]
    for k, v in rep["S2_momentum"].items():
        if "last_picks" in v:
            L += [f"Ultime scelte ({k}): {v['last_picks']}", ""]
    (OUT / "stocks_report.md").write_text("\n".join(L))


if __name__ == "__main__":
    sys.exit(main())
