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

- **Crypto**: universo da CoinGecko (prime 250 per capitalizzazione, escluse
  stablecoin, wrapped/staked, capitalizzazione < 30 M$ e volume < 3 M$). Candele
  da Binance (mirror dati pubblico), Coinbase, Kraken o Binance.US, in cascata:
  si usa la prima sorgente che risponde con un prezzo coerente con CoinGecko.
  Se CoinGecko non risponde, universo di riserva da Binance.US.
- **Brokerage**: 30 azioni US/EU/IT + 10 commodity (futures) via Yahoo Finance.

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
