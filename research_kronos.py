#!/usr/bin/env python3
"""
Ricerca Kronos — il modello «Kronos» prevede davvero le crypto?

Kronos (github.com/shiyu-coder/Kronos, pesi su Hugging Face) è un modello open source
addestrato su miliardi di candele. Sui social gira come «predittore» di crypto. Qui lo
mettiamo alla prova senza addestrarlo né regolarlo: gli diamo le ultime 256 candele
giornaliere e confrontiamo quello che prevede con quello che è successo davvero.

Disegno, deciso PRIMA di guardare i risultati:
  · 20 crypto tra le più liquide e con più storia (lista fissa, COINS)
  · solo date dal 1° settembre 2025 in poi: dopo la pubblicazione del modello (agosto 2025),
    quindi dati che non può aver visto durante l'addestramento
  · ogni giorno: previsione del rendimento a 1 giorno; ogni 5 giorni: anche a 5 giorni
  · Kronos-small (lo stesso dei post), 10 campioni per previsione, T=1.0, top_p=0.9

Misure:
  IC      correlazione di rango, giorno per giorno, tra rendimento previsto e reale sulle
          20 crypto (0 = nessuna capacità; un buon segnale reale sta intorno a 0,03–0,05)
  segno   quante volte indovina la direzione, contro il semplice «sale sempre» o «scende sempre»
  soldi   comprare ogni giorno le 3 crypto con la previsione più alta (e ogni 5 giorni le 5
          più alte a 5 giorni), costi 0,25% per lato, contro tenerle tutte e 20 a pesi uguali;
          BTC tenuto solo quando la previsione a 1 giorno è positiva, contro BTC tenuto sempre

Regola per usarlo nel modello: IC giornaliero medio ≥ 0,03 con t ≥ 3, positivo in entrambe
le metà del periodo, e almeno una strategia che batte il riferimento dopo i costi.

Uso:
  python research_kronos.py predict --shard 0 --of 12 --out preds/shard_0.json
  python research_kronos.py report --inputs preds
"""

from __future__ import annotations

import argparse
import json
import math
import os
import sys
import time
from datetime import datetime, timezone
from pathlib import Path

import numpy as np
import pandas as pd

OUT = Path("research_results")
COINS = ["BTC", "ETH", "SOL", "XRP", "BNB", "DOGE", "ADA", "AVAX", "LINK", "LTC",
         "TRX", "DOT", "BCH", "NEAR", "UNI", "APT", "SUI", "ATOM", "FIL", "ETC"]
OOS_START = "2025-09-01"
CTX = 256
SAMPLES = 10
EVERY_5 = 5
MODEL_ID = "NeoQuasar/Kronos-small"
TOKENIZER_ID = "NeoQuasar/Kronos-Tokenizer-base"
COST = 0.0025


def log(msg: str) -> None:
    print(f"[{datetime.now(timezone.utc).strftime('%H:%M:%S')}] {msg}", flush=True)


# ══ PREVISIONI (gira il modello) ═══════════════════════════════════════════

def load_candles() -> dict:
    import backtest as B
    out = {}
    for base in COINS:
        df = B.binance_klines(base + "USDT", "1d", 2)
        if df is None or len(df) < CTX + 30:
            log(f"  ! {base}: dati insufficienti")
            continue
        df = df.copy()
        df.index = (df.index - pd.Timedelta(days=1)).tz_localize(None).normalize()  # data della candela
        out[base] = df[~df.index.duplicated(keep="last")]
    return out


def cmd_predict(args) -> int:
    import torch
    sys.path.insert(0, args.kronos_src)
    from model import Kronos, KronosPredictor, KronosTokenizer

    torch.set_num_threads(os.cpu_count() or 2)
    torch.manual_seed(1234 + args.shard)
    candles = load_candles()
    if "BTC" not in candles or len(candles) < 10:
        log("!! dati insufficienti")
        return 1
    cal = candles["BTC"].index
    dates = [d for d in cal if d >= pd.Timestamp(OOS_START) and d < cal[-1]]
    mine = [(k, d) for k, d in enumerate(dates) if k % args.of == args.shard]
    if args.max_dates:
        mine = mine[:args.max_dates]
    log(f"Crypto {len(candles)} · date fuori campione {len(dates)} ({dates[0].date()} → {dates[-1].date()}) · "
        f"questa parte: {len(mine)}")

    meta = {"model": MODEL_ID, "ctx": CTX, "samples": SAMPLES, "torch": torch.__version__}
    try:
        from huggingface_hub import HfApi
        meta["model_last_modified"] = str(HfApi().model_info(MODEL_ID).last_modified)
    except Exception as e:  # noqa: BLE001
        meta["model_last_modified"] = f"n/d ({e})"
    tokenizer = KronosTokenizer.from_pretrained(TOKENIZER_ID)
    model = Kronos.from_pretrained(MODEL_ID)
    tokenizer.eval()
    model.eval()
    predictor = KronosPredictor(model, tokenizer, device="cpu", max_context=512)
    log(f"Modello caricato · parametri {sum(p.numel() for p in model.parameters()) / 1e6:.1f}M · "
        f"ultima modifica {meta['model_last_modified']}")

    rows, t_pass = [], []
    for k, d in mine:
        steps = EVERY_5 if k % EVERY_5 == 0 else 1
        dfs, xts, yts, info = [], [], [], []
        for base, df in candles.items():
            if d not in df.index:
                continue
            i = df.index.get_loc(d)
            if i + 1 < CTX or i + 1 >= len(df):
                continue
            w = df.iloc[i + 1 - CTX:i + 1]
            x = pd.DataFrame({"open": w["o"].to_numpy(), "high": w["h"].to_numpy(), "low": w["l"].to_numpy(),
                              "close": w["c"].to_numpy(), "volume": w["v"].to_numpy(), "amount": w["qv"].to_numpy()})
            if x.isnull().values.any():
                continue
            dfs.append(x)
            xts.append(pd.Series(w.index))
            yts.append(pd.Series(pd.date_range(d + pd.Timedelta(days=1), periods=steps, freq="D")))
            c = df["c"].to_numpy()
            info.append((base, c[i], c[i + 1] / c[i] - 1,
                         (c[i + EVERY_5] / c[i] - 1) if i + EVERY_5 < len(c) else None))
        if not dfs:
            continue
        t0 = time.monotonic()
        preds = predictor.predict_batch(dfs, xts, yts, pred_len=steps, T=1.0, top_p=0.9, sample_count=SAMPLES,
                                        verbose=False)
        dt = time.monotonic() - t0
        t_pass.append(dt / steps)
        for (base, last, r1, r5), p in zip(info, preds):
            pc = p["close"].to_numpy()
            rows.append({"date": str(d.date()), "coin": base, "p1": float(pc[0] / last - 1), "r1": float(r1),
                         "p5": float(pc[-1] / last - 1) if steps == EVERY_5 else None,
                         "r5": (float(r5) if r5 is not None else None) if steps == EVERY_5 else None})
        log(f"  {d.date()} · {len(dfs)} crypto · {steps} passi · {dt:.0f}s")
    meta["sec_per_step"] = float(np.mean(t_pass)) if t_pass else None
    Path(args.out).parent.mkdir(parents=True, exist_ok=True)
    Path(args.out).write_text(json.dumps({"meta": meta, "rows": rows}))
    log(f"Scritte {len(rows)} previsioni in {args.out} · {meta['sec_per_step']} s per passo")
    return 0


# ══ REPORT (solo numeri: non esegue il modello) ════════════════════════════

def spearman(a: pd.Series, b: pd.Series) -> float | None:
    if len(a) < 5 or a.nunique() < 3 or b.nunique() < 3:
        return None
    return float(a.rank().corr(b.rank()))


def ic_stats(df: pd.DataFrame, p: str, r: str) -> dict:
    ics = []
    for d, g in df.groupby("date"):
        g = g.dropna(subset=[p, r])
        v = spearman(g[p], g[r])
        if v is not None and math.isfinite(v):
            ics.append((d, v))
    if len(ics) < 5:
        return {"n_dates": len(ics)}
    s = pd.Series(dict(ics)).sort_index()
    half = len(s) // 2

    def t(x):
        sd = x.std(ddof=1)
        return float(x.mean() / (sd / math.sqrt(len(x)))) if sd > 0 else None
    return {"n_dates": int(len(s)), "mean": float(s.mean()), "t": t(s), "positive_days": float((s > 0).mean() * 100),
            "A": float(s.iloc[:half].mean()), "B": float(s.iloc[half:].mean()), "t_A": t(s.iloc[:half]), "t_B": t(s.iloc[half:])}


def sign_stats(df: pd.DataFrame, p: str, r: str) -> dict:
    g = df.dropna(subset=[p, r])
    if g.empty:
        return {"n": 0}
    hit = (np.sign(g[p]) == np.sign(g[r])).mean() * 100
    up = (g[r] > 0).mean() * 100
    n = len(g)
    return {"n": int(n), "hit": float(hit), "always_up": float(up), "always_down": float(100 - up),
            "pred_up": float((g[p] > 0).mean() * 100), "se": float(50 / math.sqrt(n))}


def curve(rets: pd.Series, per_year: float) -> dict:
    if len(rets) < 5:
        return {}
    eq = (1 + rets).cumprod()
    sd = rets.std()
    return {"total": float((eq.iloc[-1] - 1) * 100), "sharpe": float(rets.mean() / sd * math.sqrt(per_year)) if sd > 0 else None,
            "max_dd": float((eq / eq.cummax() - 1).min() * 100), "n": int(len(rets))}


def top_strategy(df: pd.DataFrame, p: str, r: str, k: int, per_year: float) -> dict:
    """Ogni data: le k crypto con la previsione più alta, pesi uguali; costi sul ricambio."""
    gross, net, ew, prev = {}, {}, {}, set()
    for d, g in df.dropna(subset=[p, r]).groupby("date"):
        if len(g) < k + 2:
            continue
        sel = set(g.sort_values(p, ascending=False)["coin"].head(k))
        ret = g[g["coin"].isin(sel)][r].mean()
        turnover = len(sel - prev) / k           # quota del portafoglio cambiata
        gross[d], net[d], ew[d] = ret, ret - 2 * COST * turnover, g[r].mean()
        prev = sel
    gs, ns, es = pd.Series(gross), pd.Series(net), pd.Series(ew)
    ex = ns - es
    sd = ex.std(ddof=1) if len(ex) > 2 else 0
    return {"k": k, "gross": curve(gs, per_year), "net": curve(ns, per_year), "equal_weight": curve(es, per_year),
            "excess_mean": float(ex.mean() * 100) if len(ex) else None,
            "excess_t": float(ex.mean() / (sd / math.sqrt(len(ex)))) if sd and sd > 0 else None}


def btc_timing(df: pd.DataFrame) -> dict:
    g = df[df["coin"] == "BTC"].dropna(subset=["p1", "r1"]).sort_values("date")
    if len(g) < 20:
        return {}
    pos = (g["p1"] > 0).astype(float).to_numpy()
    r = g["r1"].to_numpy()
    trades = np.abs(np.diff(np.r_[0.0, pos]))
    strat = pd.Series(pos * r - trades * COST, index=g["date"])
    return {"kronos": curve(strat, 365), "buy_hold": curve(pd.Series(r, index=g["date"]), 365),
            "days_in": float(pos.mean() * 100), "switches": int(trades.sum())}


def cmd_report(args) -> int:
    files = sorted(Path(args.inputs).rglob("*.json"))
    rows, metas = [], []
    for f in files:
        j = json.loads(f.read_text())
        rows += j["rows"]
        metas.append(j["meta"])
    if not rows:
        log("!! nessuna previsione trovata")
        return 1
    df = pd.DataFrame(rows).drop_duplicates(["date", "coin"])
    df["p5"] = pd.to_numeric(df["p5"], errors="coerce")
    df["r5"] = pd.to_numeric(df["r5"], errors="coerce")
    sec = [m["sec_per_step"] for m in metas if m.get("sec_per_step")]
    rep = {"generated": datetime.now(timezone.utc).isoformat(timespec="seconds"), "model": metas[0].get("model"),
           "model_last_modified": metas[0].get("model_last_modified"), "ctx": metas[0].get("ctx"),
           "samples": metas[0].get("samples"), "period": [df["date"].min(), df["date"].max()],
           "n_dates": int(df["date"].nunique()), "n_coins": int(df["coin"].nunique()), "n_predictions": int(len(df)),
           "sec_per_step": float(np.mean(sec)) if sec else None,
           "ic_1d": ic_stats(df, "p1", "r1"), "ic_5d": ic_stats(df, "p5", "r5"),
           "sign_1d": sign_stats(df, "p1", "r1"), "sign_5d": sign_stats(df, "p5", "r5"),
           "sign_1d_btc_eth": sign_stats(df[df["coin"].isin(["BTC", "ETH"])], "p1", "r1"),
           "top3_daily": top_strategy(df, "p1", "r1", 3, 365),
           "top5_5d": top_strategy(df, "p5", "r5", 5, 73),
           "btc_timing": btc_timing(df),
           "pred_vs_real": {"p1_mean": float(df["p1"].mean() * 100), "r1_mean": float(df["r1"].mean() * 100),
                            "p1_abs": float(df["p1"].abs().mean() * 100), "r1_abs": float(df["r1"].abs().mean() * 100)}}
    ic = rep["ic_1d"]
    strategies_ok = any((rep[k].get("excess_mean") or 0) > 0 and (rep[k].get("excess_t") or 0) >= 2
                        for k in ("top3_daily", "top5_5d"))
    rep["verdict"] = bool(ic.get("mean") is not None and ic["mean"] >= 0.03 and (ic.get("t") or 0) >= 3
                          and ic["A"] > 0 and ic["B"] > 0 and strategies_ok)
    OUT.mkdir(exist_ok=True)
    (OUT / "kronos_report.json").write_text(json.dumps(rep, indent=1))
    write_markdown(rep)
    log(f"Report scritto · IC 1g {ic.get('mean')} t {ic.get('t')} · verdetto {rep['verdict']}")
    return 0


def f_(x, d=2, pct=False, sign=True):
    if x is None or (isinstance(x, float) and not math.isfinite(x)):
        return "—"
    s = f"{x:+.{d}f}" if sign else f"{x:.{d}f}"
    return s + ("%" if pct else "")


def write_markdown(rep: dict) -> None:
    L = [f"# Ricerca Kronos — {rep['generated']}", "",
         f"Modello {rep['model']} (ultima modifica su Hugging Face: {rep['model_last_modified']}) · "
         f"{rep['ctx']} candele giornaliere di contesto · {rep['samples']} campioni",
         f"Periodo {rep['period'][0]} → {rep['period'][1]} · {rep['n_dates']} date · {rep['n_coins']} crypto · "
         f"{rep['n_predictions']} previsioni", "",
         f"**Verdetto secondo la regola fissata prima: {'PASSA' if rep['verdict'] else 'NON PASSA'}**", "",
         "## Correlazione di rango tra previsto e reale (IC)", "",
         "| Orizzonte | date | IC medio | t | giorni con IC > 0 | IC metà A | IC metà B |", "|---|---|---|---|---|---|---|"]
    for name, k in (("1 giorno", "ic_1d"), ("5 giorni", "ic_5d")):
        s = rep[k]
        L.append(f"| {name} | {s.get('n_dates')} | {f_(s.get('mean'), 3)} | {f_(s.get('t'), 1)} | "
                 f"{f_(s.get('positive_days'), 0, True, False)} | {f_(s.get('A'), 3)} | {f_(s.get('B'), 3)} |")
    L += ["", "## Direzione indovinata", "",
          "| | previsioni | Kronos | «sale sempre» | «scende sempre» | previsioni al rialzo | margine d'errore |",
          "|---|---|---|---|---|---|---|"]
    for name, k in (("1 giorno, tutte", "sign_1d"), ("1 giorno, BTC ed ETH", "sign_1d_btc_eth"), ("5 giorni, tutte", "sign_5d")):
        s = rep[k]
        L.append(f"| {name} | {s.get('n')} | {f_(s.get('hit'), 1, True, False)} | {f_(s.get('always_up'), 1, True, False)} | "
                 f"{f_(s.get('always_down'), 1, True, False)} | {f_(s.get('pred_up'), 1, True, False)} | ±{f_(s.get('se'), 1, True, False)} |")
    pv = rep["pred_vs_real"]
    L += ["", f"Ampiezza media dei movimenti a 1 giorno: prevista {f_(pv['p1_abs'], 2, True, False)}, reale "
              f"{f_(pv['r1_abs'], 2, True, False)}. Media prevista {f_(pv['p1_mean'], 2, True)}, reale {f_(pv['r1_mean'], 2, True)}.", "",
          "## Soldi (costi 0,25% per lato)", "",
          "| Strategia | lordo | netto | 20 crypto a pesi uguali | eccesso medio per periodo | t |", "|---|---|---|---|---|---|"]
    for name, k in (("Ogni giorno le 3 con previsione più alta", "top3_daily"),
                    ("Ogni 5 giorni le 5 con previsione più alta", "top5_5d")):
        s = rep[k]
        L.append(f"| {name} | {f_(s['gross'].get('total'), 1, True)} | {f_(s['net'].get('total'), 1, True)} | "
                 f"{f_(s['equal_weight'].get('total'), 1, True)} | {f_(s.get('excess_mean'), 2, True)} | {f_(s.get('excess_t'), 1)} |")
    b = rep["btc_timing"]
    if b:
        L += ["", f"BTC tenuto solo con previsione a 1 giorno positiva: {f_(b['kronos'].get('total'), 1, True)} "
                  f"(calo max {f_(b['kronos'].get('max_dd'), 1, True)}, dentro il {f_(b['days_in'], 0, True, False)} dei giorni, "
                  f"{b['switches']} cambi) contro BTC tenuto sempre {f_(b['buy_hold'].get('total'), 1, True)} "
                  f"(calo max {f_(b['buy_hold'].get('max_dd'), 1, True)}).", ""]
    (OUT / "kronos_report.md").write_text("\n".join(L))


def main() -> int:
    ap = argparse.ArgumentParser()
    sub = ap.add_subparsers(dest="cmd", required=True)
    p = sub.add_parser("predict")
    p.add_argument("--shard", type=int, default=0)
    p.add_argument("--of", type=int, default=1)
    p.add_argument("--out", default="preds/shard_0.json")
    p.add_argument("--max-dates", type=int, default=0)
    p.add_argument("--kronos-src", default="kronos_src")
    r = sub.add_parser("report")
    r.add_argument("--inputs", default="preds")
    args = ap.parse_args()
    return cmd_predict(args) if args.cmd == "predict" else cmd_report(args)


if __name__ == "__main__":
    sys.exit(main())
