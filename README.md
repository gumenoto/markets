# Mercati Screener

Scanner automatico su crypto, azioni e commodity. Gira su GitHub Actions,
pubblica una dashboard su GitHub Pages (`docs/`) e manda alert su Telegram.

## Le tre modalità

| Modalità | Cosa cerca | Alert Telegram |
|---|---|---|
| **Accelerazioni** | Rottura del massimo a 20/55 giorni con volume, forza relativa vs BTC o vs indice, trend (ADX) | 🚀 solo crypto con score ≥ 100, volume confermato e regime positivo |
| **In carica** | Volatilità compressa (Bollinger nel 20% più stretto degli ultimi 120 giorni), vicino al tetto del range, accumulo volumi | nessuno all'ingresso · ⚡ **Squeeze scattato** (crypto) quando rompe verso l'alto |
| **Rimbalzi** | Mean-reversion: RSI basso, sotto MA20 / Bollinger inferiore (logica originale) | nessuno: solo lista informativa |

## Modello v3 (da metà ottobre 2026)

Dopo due settimane di simulazione in perdita, `backtest.py` ha ricostruito i
segnali su 5 anni (99 crypto, 30 azioni, 10 commodity, costi inclusi). Esito:

- **Rimbalzi**: nessun vantaggio (profit factor 0,75 crypto, ~0,9 azioni) → niente alert.
- **Accelerazioni / Squeeze crypto**: funzionano solo con **BTC sopra la media a
  50 giorni**; sotto 100 di score non rendono; uscita a 5 giorni meglio del trailing.
- **Azioni / commodity**: vantaggio debole e poco robusto → solo portafoglio sperimentale.

Le regole stanno in `SIGNAL_POLICY` e `REGIME_MA` (`screener.py`) e in
`PAPER_RULES` (`paper.py`): un segnale è **operativo** (alert + simulazione),
**sperimentale** (solo simulazione) o **in pausa** (regime negativo). Il regime
usa l'ultima chiusura giornaliera completa: crypto = BTC vs media 50 giorni,
azioni = indice della regione (S&P 500, Euro Stoxx 50, FTSE MIB) vs media 200
giorni. Ogni alert riporta il piano (prezzo di stop, data di uscita) e quando il
regime crypto cambia arriva un messaggio.

Crypto Accelerazioni + Squeeze, portafoglio da 10.000 per anno (1.000 a operazione,
massimo 10 aperte):

| Anno | Regole v2 | Regole v3 |
|---|---|---|
| 2022 | −43,3% | −11,4% |
| 2023 | +25,4% | +49,0% |
| 2024 | +30,2% | +90,2% |
| 2025 | −16,3% | +21,2% |
| 2026 (a ottobre) | −26,8% | +13,7% |
| Totale | −34,5% (calo max −47,5%) | +162,6% (calo max −21,1%) |

Limiti: universo di crypto liquide di oggi (survivorship bias), barre
giornaliere, regole di uscita scelte sugli stessi dati. Aspettati meno, e circa
una operazione vincente su tre. Per rifarlo: Actions → **Backtest** → Run
workflow (report in `backtest_results/report.md`).

Etichette di rischio (non escludono, informano): *Esteso*, *Surriscaldato*,
*Liquidità bassa*, *Volume anomalo*, *Crollo estremo*. Sotto -50% in 24h un
asset viene escluso (di solito delisting o collasso).

## Dati

- **Crypto**: universo dalle prime 250 per capitalizzazione (escluse
  stablecoin, wrapped/staked, capitalizzazione < 30 M$ e volume < 3 M$), da
  CoinGecko o, se non risponde, da CoinPaprika. Dai server di GitHub CoinGecko
  oggi risponde 403 sulla lista mercati, quindi di fatto si usa CoinPaprika;
  CoinGecko resta per la lista "in tendenza". Candele da Binance (mirror dati
  pubblico), Coinbase, Kraken o Binance.US, in cascata: si usa la prima sorgente
  con un prezzo coerente. Ultima riserva: universo da Binance.US.
  Lo stato di ogni sorgente (chiamate, ultimo errore) è in `docs/data.json` →
  `stats.sources`.
- **Brokerage**: 30 azioni US/EU/IT + 10 commodity (futures) via Yahoo Finance.

## Portafogli simulati (paper trading)

`paper.py` gestisce quattro portafogli da 10.000 finti, salvati in `paper.json`:

- **Accelerazioni crypto**, **Squeeze crypto**: automatici, comprano 1.000 a ogni
  segnale operativo e vendono allo stop −8% o dopo 5 giorni.
- **Breakout azioni** (sperimentale): breakout su azioni e commodity, senza alert;
  stop −12%, trailing 20% dopo +10%, massimo 20 giorni.
- **Le mie scelte**: manuale, via Telegram — `/compra QNT 500`, `/vendi QNT`,
  `/vendi QNT 50%`, `/portafoglio`, `/riepilogo`. Gli ordini sono eseguiti al
  prezzo del prossimo aggiornamento; ogni acquisto è marcato "suggerito" se
  l'asset era in una lista dello scanner in quel momento.

Regole in `PAPER_RULES`, costi per operazione in `COSTS`. Ogni portafoglio ha la
sua data di partenza e il suo benchmark (BTC o S&P 500). La dashboard mostra
valore, rendimento, quota di operazioni positive, profit factor, calo massimo e
un verdetto che resta "in raccolta" fino a 20 operazioni chiuse. I portafogli
del modello v2 sono conservati nell'archivio di `paper.json`. Le esecuzioni su branch diversi da
`main` non leggono né mandano messaggi Telegram (prova a secco).

## Registro segnali

Ogni asset che entra nella top di una modalità viene registrato in
`signals_log.json` con il prezzo del momento, poi misurato dopo 1, 3 e 7 giorni.
La dashboard mostra per ogni modalità la quota di segnali in guadagno e il
rendimento medio, e a parte i soli segnali **operativi** del modello v3: è il
numero da guardare per capire, con i numeri, se il modello regge.

## Secrets (Settings → Secrets and variables → Actions)

| Nome | |
|---|---|
| `TELEGRAM_BOT_TOKEN` | obbligatorio per gli alert |
| `TELEGRAM_CHAT_ID` | obbligatorio per gli alert |
| `COINGECKO_API_KEY` | opzionale: chiave "Demo" gratuita da coingecko.com/en/api, evita i limiti sulla API pubblica |

## Parametri principali (in cima a `screener.py`)

- `TOP_N` — quanti asset per lista (10)
- `MOMENTUM_WEIGHTS`, `WEIGHTS` — pesi di Accelerazioni e Rimbalzi
- `ALERT_MIN_SCORE`, `ALERT_MIN_VOLUME` — soglie di score e volume dei segnali
- `SIGNAL_POLICY`, `REGIME_MA` — quali segnali mandano alert e il filtro di regime
- `ALERT_COOLDOWN_H` — max un alert per asset e modalità ogni 24 ore
- `SQUEEZE_MAX_PCT` — quanto deve essere compressa la volatilità per "In carica"
- `STOCK_UNIVERSE`, `COMMODITY_UNIVERSE` — cosa scansionare nel brokerage

## Eseguire in locale

```bash
pip install requests yfinance
export TELEGRAM_BOT_TOKEN=... TELEGRAM_CHAT_ID=...
python screener.py
```

## Avvertenza

Screener tecnico, non consiglio di investimento. I segnali sono calcolati su dati
passati e non garantiscono risultati futuri. I breakout falliscono spesso: usa
sempre uno stop e investi solo capitale che puoi permetterti di perdere.
