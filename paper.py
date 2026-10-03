"""
Paper trading — soldi finti per verificare se i segnali valgono qualcosa.

Quattro portafogli da 10.000 (unità di conto, "€" indicativi):
  momentum   segue ogni alert 🚀 Accelerazione
  setup      segue ogni alert ⚡ Squeeze scattato (asset usciti da "In carica")
  reversion  segue ogni alert 🔄 Rimbalzo
  manual     le tue scelte, via Telegram: /compra QNT 500 · /vendi QNT · /portafoglio

Regole dei portafogli automatici (PAPER_RULES): taglio fisso per operazione,
stop loss, take profit o trailing stop, durata massima. Costi realistici per
operazione (spread/commissioni). Prezzi senza cambio valuta: i rendimenti sono
nella valuta dell'asset (USD per crypto e azioni USA, EUR per quelle europee).

Le operazioni vengono eseguite al prezzo dell'aggiornamento in cui sono
processate: niente "senno di poi". Stop e target sono controllati solo agli
aggiornamenti: se il prezzo scavalca lo stop tra un run e l'altro, l'uscita
avviene al prezzo osservato (conservativo).
"""

from __future__ import annotations

import json
import math
import re
from datetime import datetime
from pathlib import Path

PAPER_FILE = Path("paper.json")
PAPER_VERSION = 1

INITIAL_CAPITAL = 10_000.0
TRADE_SIZE = 1_000.0          # importo per operazione nei portafogli automatici
MAX_OPEN = 10                 # posizioni aperte al massimo per portafoglio
DEFAULT_MANUAL_SIZE = 1_000.0
MIN_TRADES_FOR_VERDICT = 20   # operazioni chiuse prima di dare un giudizio

PORTFOLIOS = {
    "momentum": {"label": "Accelerazioni", "kind": "auto", "signal": "momentum"},
    "setup": {"label": "In carica → squeeze", "kind": "auto", "signal": "squeeze"},
    "reversion": {"label": "Rimbalzi", "kind": "auto", "signal": "reversion"},
    "manual": {"label": "Le mie scelte", "kind": "manual", "signal": None},
}

# Regole di uscita per tipo di segnale (percentuali)
PAPER_RULES = {
    # rimbalzo veloce: obiettivo +6%, stop -5%, massimo 3 giorni
    "reversion": {"stop": -5.0, "take": 6.0, "trail": None, "trail_from": None, "max_days": 3},
    # breakout: stop -8%, poi trailing 15% dal massimo quando è sopra +8%; massimo 10 giorni
    "momentum": {"stop": -8.0, "take": None, "trail": 15.0, "trail_from": 8.0, "max_days": 10},
    "squeeze": {"stop": -8.0, "take": None, "trail": 15.0, "trail_from": 8.0, "max_days": 10},
}

# Costi per lato (acquisto o vendita): percentuale + fisso
COSTS = {
    "crypto": {"pct": 0.25, "fixed": 0.0},     # spread/commissione exchange + slippage
    "stock": {"pct": 0.10, "fixed": 1.0},      # Trade Republic: 1 € + spread
    "commodity": {"pct": 0.15, "fixed": 1.0},  # ETF/ETC: 1 € + spread più largo
}

HELP_TEXT = (
    "📒 <b>Portafoglio simulato</b> (soldi finti, 10.000 di partenza)\n\n"
    "<code>/compra QNT 500</code> — compra 500 di QNT\n"
    "<code>/compra NVDA</code> — compra 1.000 (importo predefinito)\n"
    "<code>/vendi QNT</code> — vende tutto QNT\n"
    "<code>/vendi QNT 50%</code> — vende metà\n"
    "<code>/portafoglio</code> — situazione attuale\n\n"
    "Puoi usare il ticker (QNT, NVDA, ENI.MI, GC=F) o il nome (Quant, Oro).\n"
    "Gli ordini vengono eseguiti al prezzo del prossimo aggiornamento dello scanner."
)


def _iso(dt: datetime) -> str:
    return dt.isoformat(timespec="seconds")


def _parse(s: str | None) -> datetime | None:
    try:
        return datetime.fromisoformat(s) if s else None
    except ValueError:
        return None


def _num(s: str) -> float | None:
    """Importi tipo 500, 500€, 1.000, 1.000,50, 1,5 → float."""
    s = s.replace("€", "").replace("$", "").strip()
    if "," in s and "." in s:
        s = s.replace(".", "").replace(",", ".")
    elif "," in s:
        s = s.replace(",", ".")
    elif s.count(".") == 1 and len(s.split(".")[1]) == 3:
        s = s.replace(".", "")  # 1.000 = mille
    try:
        v = float(s)
        return v if math.isfinite(v) and v > 0 else None
    except ValueError:
        return None


def fmt_money(v: float) -> str:
    s = f"{abs(v):,.2f}".replace(",", "X").replace(".", ",").replace("X", ".")
    return ("−" if v < 0 else "") + s


def fmt_pct(v: float | None) -> str:
    if v is None:
        return "—"
    return f"{'+' if v >= 0 else '−'}{abs(v):.1f}%"


def trade_cost(asset_type: str, amount: float) -> float:
    c = COSTS.get(asset_type, COSTS["stock"])
    return amount * c["pct"] / 100 + c["fixed"]


class PaperBook:
    def __init__(self, data: dict):
        self.data = data

    # ── persistenza ──
    @classmethod
    def load(cls, path: Path = PAPER_FILE) -> "PaperBook":
        data = None
        if path.exists():
            try:
                data = json.loads(path.read_text())
            except (json.JSONDecodeError, OSError):
                print(f"  ! {path} illeggibile: riparto con portafogli nuovi")
        if not isinstance(data, dict) or data.get("version") != PAPER_VERSION:
            data = {"version": PAPER_VERSION, "started": None, "bench_start": {},
                    "telegram_offset": None, "portfolios": {}}
        for key, meta in PORTFOLIOS.items():
            p = data["portfolios"].setdefault(key, {})
            p.setdefault("cash", INITIAL_CAPITAL)
            p.setdefault("initial", INITIAL_CAPITAL)
            p.setdefault("positions", [])
            p.setdefault("closed", [])
            p.setdefault("history", [])
            p.setdefault("seq", 0)
        return cls(data)

    def to_json(self) -> dict:
        return self.data

    @property
    def started(self) -> bool:
        return bool(self.data.get("started"))

    def ensure_started(self, now: datetime, bench_prices: dict[str, float]) -> None:
        if not self.data.get("started"):
            self.data["started"] = _iso(now)
        for k, v in bench_prices.items():
            if v and k not in self.data["bench_start"]:
                self.data["bench_start"][k] = v

    # ── operazioni ──
    def _p(self, key: str) -> dict:
        return self.data["portfolios"][key]

    def find_position(self, key: str, asset_id: str) -> dict | None:
        return next((x for x in self._p(key)["positions"] if x["asset_id"] == asset_id), None)

    def buy(self, key: str, asset: dict, amount: float, now: datetime, reason: str,
            suggested_by: list[str] | None = None, rule: str | None = None) -> tuple[bool, str]:
        p = self._p(key)
        price = asset.get("price")
        if not price or price <= 0:
            return False, "prezzo non disponibile"
        cost = trade_cost(asset["type"], amount)
        if amount + cost > p["cash"] + 1e-9:
            return False, f"liquidità insufficiente ({fmt_money(p['cash'])} disponibili)"
        existing = self.find_position(key, asset["asset_id"])
        if key != "manual" and not existing and len(p["positions"]) >= MAX_OPEN:
            return False, f"già {MAX_OPEN} posizioni aperte"
        p["seq"] += 1
        qty = amount / price
        if existing:  # media del prezzo di carico
            tot_qty = existing["qty"] + qty
            existing["entry_price"] = (existing["entry_price"] * existing["qty"] + price * qty) / tot_qty
            existing["qty"] = tot_qty
            existing["invested"] += amount
            existing["costs"] += cost
        else:
            p["positions"].append({
                "id": f"{key}-{p['seq']}",
                "asset_id": asset["asset_id"],
                "symbol": asset["symbol"],
                "name": asset["name"],
                "type": asset["type"],
                "qty": qty,
                "entry_price": price,
                "entry_ts": _iso(now),
                "invested": amount,
                "costs": cost,
                "peak": price,
                "last_price": price,
                "last_seen": _iso(now),
                "rule": rule,
                "reason": reason,
                "suggested_by": suggested_by or [],
            })
        p["cash"] -= amount + cost
        return True, f"comprato {fmt_money(amount)} a {price:.6g}"

    def sell(self, key: str, pos: dict, fraction: float, price: float, now: datetime, reason: str) -> dict:
        p = self._p(key)
        fraction = max(0.0, min(1.0, fraction))
        qty = pos["qty"] * fraction
        gross = qty * price
        cost_out = trade_cost(pos["type"], gross)
        invested = pos["invested"] * fraction
        cost_in = pos["costs"] * fraction
        pnl = gross - cost_out - invested - cost_in
        p["cash"] += gross - cost_out
        closed = {
            "symbol": pos["symbol"], "name": pos["name"], "type": pos["type"], "asset_id": pos["asset_id"],
            "entry_ts": pos["entry_ts"], "exit_ts": _iso(now),
            "entry_price": pos["entry_price"], "exit_price": price,
            "invested": invested, "pnl": pnl,
            "pnl_pct": pnl / invested * 100 if invested else 0.0,
            "reason": reason, "rule": pos.get("rule"), "suggested_by": pos.get("suggested_by", []),
        }
        p["closed"].append(closed)
        if fraction >= 0.999:
            p["positions"] = [x for x in p["positions"] if x is not pos]
        else:
            pos["qty"] -= qty
            pos["invested"] -= invested
            pos["costs"] -= cost_in
        return closed

    # ── portafogli automatici ──
    def open_auto(self, signal_kind: str, asset: dict, score: float, now: datetime) -> str | None:
        key = next((k for k, m in PORTFOLIOS.items() if m["signal"] == signal_kind), None)
        if not key or self.find_position(key, asset["asset_id"]):
            return None
        ok, msg = self.buy(key, asset, TRADE_SIZE, now, reason=f"alert {signal_kind} (score {score:.0f})",
                           rule=signal_kind)
        return f"{PORTFOLIOS[key]['label']}: {asset['symbol']} {msg}" if ok else None

    def update(self, prices: dict[str, float], now: datetime) -> list[str]:
        """Aggiorna i prezzi e applica le regole di uscita dei portafogli automatici."""
        events = []
        for key, meta in PORTFOLIOS.items():
            p = self._p(key)
            for pos in list(p["positions"]):
                price = prices.get(pos["asset_id"])
                if price:
                    pos["last_price"] = price
                    pos["last_seen"] = _iso(now)
                    pos["peak"] = max(pos.get("peak", price), price)
                if meta["kind"] != "auto":
                    continue
                rule = PAPER_RULES.get(pos.get("rule") or meta["signal"])
                px = pos["last_price"]
                ret = (px / pos["entry_price"] - 1) * 100
                peak_ret = (pos["peak"] / pos["entry_price"] - 1) * 100
                age_d = (now - (_parse(pos["entry_ts"]) or now)).total_seconds() / 86400
                reason = None
                if ret <= rule["stop"]:
                    reason = f"stop {rule['stop']:.0f}%"
                elif rule["take"] is not None and ret >= rule["take"]:
                    reason = f"obiettivo +{rule['take']:.0f}%"
                elif rule["trail"] and peak_ret >= rule["trail_from"] and px <= pos["peak"] * (1 - rule["trail"] / 100):
                    reason = f"trailing {rule['trail']:.0f}% dal massimo"
                elif age_d >= rule["max_days"]:
                    reason = f"scadenza {rule['max_days']} giorni"
                if reason:
                    c = self.sell(key, pos, 1.0, px, now, reason)
                    events.append(f"{meta['label']}: chiuso {c['symbol']} {fmt_pct(c['pnl_pct'])} ({reason})")
        return events

    def equity(self, key: str) -> float:
        p = self._p(key)
        return p["cash"] + sum(x["qty"] * x["last_price"] for x in p["positions"])

    def record_history(self, now: datetime) -> None:
        for key in PORTFOLIOS:
            h = self._p(key)["history"]
            eq = round(self.equity(key), 2)
            h.append([_iso(now), eq])
            if len(h) > 1500:
                del h[: len(h) - 1500]

    # ── comandi Telegram (portafoglio manuale) ──
    def handle_command(self, text: str, resolve, suggested_by, now: datetime, order_ts: str | None = None) -> str:
        t = (text or "").strip()
        parts = t.split()
        if not parts:
            return HELP_TEXT
        cmd = parts[0].lower().lstrip("/").split("@")[0]
        args = parts[1:]
        if cmd in ("start", "aiuto", "help", "comandi"):
            return HELP_TEXT
        if cmd in ("portafoglio", "p", "saldo", "portfolio"):
            return self.portfolio_text("manual")
        if cmd in ("compra", "buy", "c"):
            if not args:
                return "Scrivi cosa comprare, es. <code>/compra QNT 500</code>"
            amount = DEFAULT_MANUAL_SIZE
            query_parts = args
            if len(args) >= 2 and _num(args[-1]) is not None:
                amount = _num(args[-1])
                query_parts = args[:-1]
            asset, candidates = resolve(" ".join(query_parts))
            if not asset:
                hint = f" Forse: {', '.join(candidates[:5])}?" if candidates else ""
                return f"❓ Non trovo «{_esc(' '.join(query_parts))}» tra gli asset analizzati.{hint}"
            sug = suggested_by(asset["asset_id"])
            ok, msg = self.buy("manual", asset, amount, now, reason="manuale", suggested_by=sug)
            if not ok:
                return f"⛔ {_esc(asset['name'])}: {msg}."
            tag = f"\n💡 Era tra i suggerimenti: {', '.join(sug)}" if sug else "\n(non era tra i suggerimenti dello scanner)"
            when = ""
            if order_ts:
                when = f"\nOrdine delle {_hm(order_ts)} · eseguito alle {_hm(_iso(now))}"
            return (f"✅ <b>Comprato</b> {_esc(asset['name'])} ({_esc(asset['symbol'])})\n"
                    f"{fmt_money(amount)} a <code>{asset['price']:.6g}</code> · costi {fmt_money(trade_cost(asset['type'], amount))}"
                    f"{when}{tag}\nLiquidità: {fmt_money(self._p('manual')['cash'])}")
        if cmd in ("vendi", "sell", "v"):
            if not args:
                return "Scrivi cosa vendere, es. <code>/vendi QNT</code> o <code>/vendi QNT 50%</code>"
            fraction = 1.0
            query_parts = args
            last = args[-1].lower()
            if len(args) >= 2 and (last.endswith("%") or last in ("tutto", "all")):
                if last.endswith("%"):
                    v = _num(last[:-1])
                    if v is None or v > 100:
                        return "Percentuale non valida."
                    fraction = v / 100
                query_parts = args[:-1]
            q = " ".join(query_parts)
            pos = self._match_position("manual", q, resolve)
            if not pos:
                return f"❓ Non hai «{_esc(q)}» nel portafoglio. Scrivi <code>/portafoglio</code> per vedere le posizioni."
            c = self.sell("manual", pos, fraction, pos["last_price"], now, "manuale")
            return (f"✅ <b>Venduto</b> {_esc(c['name'])} {int(round(fraction * 100))}% a <code>{c['exit_price']:.6g}</code>\n"
                    f"Risultato: {fmt_money(c['pnl'])} ({fmt_pct(c['pnl_pct'])}, costi inclusi)\n"
                    f"Liquidità: {fmt_money(self._p('manual')['cash'])}")
        return "Comando non riconosciuto.\n\n" + HELP_TEXT

    def _match_position(self, key: str, q: str, resolve) -> dict | None:
        ql = q.strip().lower()
        for pos in self._p(key)["positions"]:
            base = pos["symbol"].lower().removesuffix("usdt")
            if ql in (pos["symbol"].lower(), base, pos["name"].lower(), pos["symbol"].lower().split(".")[0]):
                return pos
        asset, _ = resolve(q)
        if asset:
            return self.find_position(key, asset["asset_id"])
        return None

    def portfolio_text(self, key: str) -> str:
        p = self._p(key)
        eq = self.equity(key)
        ret = (eq / p["initial"] - 1) * 100
        lines = [f"📒 <b>{PORTFOLIOS[key]['label']}</b>",
                 f"Valore {fmt_money(eq)} ({fmt_pct(ret)}) · liquidità {fmt_money(p['cash'])}", ""]
        if not p["positions"]:
            lines.append("Nessuna posizione aperta.")
        for pos in p["positions"]:
            r = (pos["last_price"] / pos["entry_price"] - 1) * 100
            lines.append(f"• {_esc(pos['name'])}: {fmt_money(pos['qty'] * pos['last_price'])} ({fmt_pct(r)})")
        s = self.stats(key)
        if s["n_closed"]:
            lines += ["", f"Chiuse: {s['n_closed']} · positive {s['win_rate']:.0f}% · risultato {fmt_money(s['realized'])}"]
        return "\n".join(lines)

    # ── statistiche ──
    def stats(self, key: str) -> dict:
        p = self._p(key)
        closed = p["closed"]
        wins = [c["pnl"] for c in closed if c["pnl"] > 0]
        losses = [c["pnl"] for c in closed if c["pnl"] <= 0]
        eq = self.equity(key)
        peak, mdd = p["initial"], 0.0
        for _, v in p["history"]:
            peak = max(peak, v)
            mdd = min(mdd, (v / peak - 1) * 100)
        return {
            "equity": eq,
            "ret_pct": (eq / p["initial"] - 1) * 100,
            "n_open": len(p["positions"]),
            "n_closed": len(closed),
            "win_rate": len(wins) / len(closed) * 100 if closed else None,
            "profit_factor": (sum(wins) / abs(sum(losses))) if losses and sum(losses) != 0 else (None if not wins else float("inf")),
            "avg_trade_pct": sum(c["pnl_pct"] for c in closed) / len(closed) if closed else None,
            "realized": sum(c["pnl"] for c in closed),
            "max_dd": mdd,
        }

    def bench_returns(self, bench_prices: dict[str, float]) -> dict[str, float | None]:
        out = {}
        for k, p0 in self.data["bench_start"].items():
            p1 = bench_prices.get(k)
            out[k] = (p1 / p0 - 1) * 100 if p1 and p0 else None
        return out

    def verdict(self, st: dict, bench: dict) -> dict:
        n = st["n_closed"]
        vals = [v for v in bench.values() if v is not None]
        market = sum(vals) / len(vals) if vals else 0.0
        if n < MIN_TRADES_FOR_VERDICT:
            return {"code": "raccolta", "label": f"In raccolta · {n}/{MIN_TRADES_FOR_VERDICT} operazioni",
                    "market": market}
        pf = st["profit_factor"] or 0
        if st["ret_pct"] > market and pf >= 1.3:
            return {"code": "promettente", "label": "Promettente: batte il mercato", "market": market}
        if st["ret_pct"] > 0:
            return {"code": "debole", "label": "Positivo ma non batte il mercato", "market": market}
        return {"code": "negativo", "label": "Non funziona finora", "market": market}

    def summary(self, bench_prices: dict[str, float], now: datetime) -> dict:
        bench = self.bench_returns(bench_prices)
        out = {}
        for key, meta in PORTFOLIOS.items():
            p = self._p(key)
            st = self.stats(key)
            hist = p["history"]
            step = max(1, len(hist) // 120)
            sampled = hist[::step]
            if hist and (not sampled or sampled[-1] is not hist[-1]):
                sampled = sampled + [hist[-1]]
            out[key] = {
                "label": meta["label"],
                "kind": meta["kind"],
                "initial": p["initial"],
                "cash": p["cash"],
                **{k: (None if isinstance(v, float) and not math.isfinite(v) else v) for k, v in st.items()},
                "pf_infinite": st["profit_factor"] == float("inf"),
                "verdict": self.verdict(st, bench),
                "history": sampled,
                "open": [{
                    "symbol": x["symbol"], "name": x["name"], "type": x["type"],
                    "entry_ts": x["entry_ts"], "entry_price": x["entry_price"], "last_price": x["last_price"],
                    "value": x["qty"] * x["last_price"], "ret_pct": (x["last_price"] / x["entry_price"] - 1) * 100,
                    "suggested_by": x.get("suggested_by", []), "reason": x.get("reason"),
                } for x in p["positions"]],
                "closed": list(reversed(p["closed"][-25:])),
            }
            if key == "manual":
                sug = [c for c in p["closed"] if c.get("suggested_by")]
                other = [c for c in p["closed"] if not c.get("suggested_by")]
                out[key]["split"] = {
                    "suggested": {"n": len(sug), "avg_pct": (sum(c["pnl_pct"] for c in sug) / len(sug)) if sug else None},
                    "other": {"n": len(other), "avg_pct": (sum(c["pnl_pct"] for c in other) / len(other)) if other else None},
                }
        return {
            "started": self.data.get("started"),
            "bench": bench,
            "rules": PAPER_RULES,
            "costs": COSTS,
            "trade_size": TRADE_SIZE,
            "min_trades": MIN_TRADES_FOR_VERDICT,
            "portfolios": out,
        }


def _esc(s: str) -> str:
    return str(s).replace("&", "&amp;").replace("<", "&lt;").replace(">", "&gt;")


def _hm(iso: str) -> str:
    d = _parse(iso)
    return d.strftime("%d/%m %H:%M UTC") if d else "?"


def build_resolver(assets: list[dict]):
    """Trova un asset da ticker o nome: QNT, QNTUSDT, Quant, NVDA, ENI.MI, ENI, Oro, GC=F."""
    exact: dict[str, dict] = {}
    names: dict[str, dict] = {}
    for a in assets:
        sym = a["symbol"].upper()
        keys = {sym}
        if a["type"] == "crypto":
            keys.add(sym.removesuffix("USDT"))
        else:
            keys.add(sym.split(".")[0].split("=")[0])
        for k in keys:
            exact.setdefault(k, a)  # il primo vince: crypto prima di azioni a parità di ticker
        names.setdefault(a["name"].lower(), a)

    def resolve(q: str):
        qn = re.sub(r"\s+", " ", (q or "").strip())
        if not qn:
            return None, []
        a = exact.get(qn.upper()) or names.get(qn.lower())
        if a:
            return a, []
        ql = qn.lower()
        cands = [x for n, x in names.items() if ql in n]
        if len(cands) == 1:
            return cands[0], []
        return None, [f"{x['name']} ({x['symbol']})" for x in cands]
    return resolve
