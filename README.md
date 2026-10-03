# Mercati Screener

Scanner automatico su crypto, azioni e commodity. Gira su GitHub Actions,
pubblica una dashboard su GitHub Pages (`docs/`) e manda alert su Telegram.

## Le tre modalità

| Modalità | Cosa cerca | Alert Telegram |
|---|---|---|
| **Accelerazioni** | Rottura del massimo a 20/55 giorni con volume, forza relativa vs BTC o vs indice, trend (ADX) | 🚀 quando entra un nuovo asset sopra soglia e con volume confermato |
| **In carica** | Volatilità compressa (Bollinger nel 20% più stretto degli ultimi 120 giorni), vicino al tetto del range, accumulo volumi | nessuno all'ingresso · ⚡ **Squeeze scattato** quando rompe verso l'alto |
| **Rimbalzi** | Mean-reversion: RSI basso, sotto MA20 / Bollinger inferiore (logica originale) | 🔄 quando entra un nuovo asset sopra soglia |

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

- **Accelerazioni**, **In carica → squeeze**, **Rimbalzi**: automatici, comprano
  1.000 a ogni alert della modalità e vendono da soli (stop, obiettivo o trailing,
  durata massima: vedi `PAPER_RULES`). Costi per operazione in `COSTS`.
- **Le mie scelte**: manuale, via Telegram — `/compra QNT 500`, `/vendi QNT`,
  `/vendi QNT 50%`, `/portafoglio`. Gli ordini sono eseguiti al prezzo del
  prossimo aggiornamento; ogni acquisto è marcato "suggerito" se l'asset era in
  una lista dello scanner in quel momento.

La dashboard mostra per ognuno valore, rendimento contro BTC e S&P 500, quota di
operazioni positive, profit factor, calo massimo e un verdetto che resta
"in raccolta" fino a 20 operazioni chiuse. Le esecuzioni su branch diversi da
`main` non leggono né mandano messaggi Telegram (prova a secco).

## Registro segnali

Ogni asset che entra nella top di una modalità viene registrato in
`signals_log.json` con il prezzo del momento, poi misurato dopo 1, 3 e 7 giorni.
La dashboard mostra per ogni modalità la quota di segnali in guadagno e il
rendimento medio: è il modo per capire, con i numeri, cosa funziona.

## Secrets (Settings → Secrets and variables → Actions)

| Nome | |
|---|---|
| `TELEGRAM_BOT_TOKEN` | obbligatorio per gli alert |
| `TELEGRAM_CHAT_ID` | obbligatorio per gli alert |
| `COINGECKO_API_KEY` | opzionale: chiave "Demo" gratuita da coingecko.com/en/api, evita i limiti sulla API pubblica |

## Parametri principali (in cima a `screener.py`)

- `TOP_N` — quanti asset per lista (10)
- `MOMENTUM_WEIGHTS`, `WEIGHTS` — pesi di Accelerazioni e Rimbalzi
- `ALERT_MIN_SCORE`, `ALERT_MIN_VOLUME` — quando mandare un push
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
