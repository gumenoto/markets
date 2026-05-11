# Mercati Screener — Backend

Scanner mean-reversion che gira automaticamente su GitHub Actions e ti manda
alert Telegram quando un asset (crypto o azione) entra nella TOP 5.

## Cosa fa

A ogni esecuzione:

1. Scarica gli ultimi 24h di tutte le coppie USDT su **Binance** (filtro: volume ≥ 5M$)
2. Per le top 50 calcola **RSI(14)**, **MA(20)**, **MACD(12,26,9)**, **Bollinger Bands(20,2)** e **volume ratio** (su candele 4h)
3. Se hai una API key **Finnhub**, fa lo stesso su un universo di azioni Trade Republic-compatibili (Apple, Nvidia, ASML, ecc.)
4. Calcola un punteggio composito mean-reversion (oversold + crollo + sotto MA + BB inferiore + MACD bullish + volume)
5. Confronta la TOP 5 corrente con quella del run precedente
6. Manda un messaggio Telegram per ogni **nuovo entrato**

Lo stato (TOP 5 precedente) è salvato in `state.json` e committato dal bot tra un run e l'altro.

## Setup (10 minuti)

### 1. Crea il bot Telegram

- Apri Telegram → cerca **@BotFather** → invia `/newbot`
- Scegli un nome e uno username (deve finire con `bot`, es. `mercati_alfio_bot`)
- Copia il **token** che ti dà (formato `123456789:ABCdef...`)
- **Importante**: cerca il tuo nuovo bot su Telegram e premi **Avvia** (`/start`).
  Senza questo passo non riceverai messaggi.

### 2. Trova il tuo chat ID

- Su Telegram cerca **@userinfobot** → invia `/start`
- Copia il numero "Id" che ti dà (es. `123456789`)

### 3. (Opzionale) API key Finnhub per le azioni

- Vai su <https://finnhub.io/register>, crea account gratis (30 secondi)
- Copia la tua API key dalla dashboard
- Se salti questo step, lo screener farà solo crypto

### 4. Crea il repo GitHub

```bash
# Sul tuo computer:
mkdir mercati-screener && cd mercati-screener
git init
# Copia qui i file: screener.py + .github/workflows/screener.yml + README.md
git add .
git commit -m "Initial commit"

# Crea un repo su GitHub (privato o pubblico — vedi nota sotto), poi:
git remote add origin git@github.com:TUO_USERNAME/mercati-screener.git
git branch -M main
git push -u origin main
```

> **Nota repo pubblico vs privato:**  
> GitHub Actions ha **2000 minuti/mese gratis sui repo privati** (~33h),
> mentre i repo pubblici hanno **Actions illimitate**.
> Lo screener consuma ~1 min per run, ~48 run/giorno = ~1500 min/mese,
> sotto al limite ma stretto. Repo pubblico = nessun pensiero.
> (Lo state file non contiene segreti, va bene anche pubblico.)

### 5. Aggiungi i segreti

Sul repo GitHub: **Settings → Secrets and variables → Actions → New repository secret**

Crea questi tre segreti:

| Nome                   | Valore                                       |
|------------------------|----------------------------------------------|
| `TELEGRAM_BOT_TOKEN`   | Il token da BotFather (es. `123456:ABC...`)  |
| `TELEGRAM_CHAT_ID`     | Il tuo numero da userinfobot                 |
| `FINNHUB_KEY`          | (Opzionale) la tua API key Finnhub           |

### 6. Abilita i workflow & testa

- Vai su **Actions** sul repo
- Se chiede "I understand my workflows, go ahead and enable them" → conferma
- Clicca **Mercati Screener** → **Run workflow** → **Run workflow**
- Aspetta ~30-60 secondi e guarda i log

Il primo run **non manda alert** (registra solo lo stato iniziale).
Dal secondo in poi, riceverai un messaggio Telegram per ogni nuovo asset
che entra nella TOP 5.

## Personalizzare

### Frequenza dei run

Modifica i `cron` in `.github/workflows/screener.yml`. Esempi:

- `*/15 * * * *` — ogni 15 min (attenzione ai limiti su repo privato)
- `*/30 * * * *` — ogni 30 min
- `0 * * * *` — ogni ora
- `0 8,12,16,20 * * *` — 4 volte al giorno

### Pesi dei segnali

In `screener.py` modifica il dizionario `WEIGHTS`:

```python
WEIGHTS = {
    "rsi": 1.0,        # 0 = ignora RSI, 2 = doppio peso
    "drop": 1.0,       # crolli intraday
    "momentum": 0.6,   # rialzi forti
    "ma": 0.8,         # distanza dalla MA20
    "volume": 0.7,     # picchi di volume
    "macd": 1.0,       # crossover MACD
    "bollinger": 1.0,  # tocco bande Bollinger
}
```

### Universo azioni

Modifica `STOCK_UNIVERSE` in `screener.py`. I simboli sono Finnhub-style
(in genere uguali al ticker US; per azioni europee usa il suffisso, es.
`ASML.US`, `SAP.US` su Finnhub free).

### Soglia volume crypto

`MIN_QUOTE_VOL_USDT = 5_000_000` — alza per essere più selettivo.

## Eseguire localmente per debug

```bash
export TELEGRAM_BOT_TOKEN="..."
export TELEGRAM_CHAT_ID="..."
export FINNHUB_KEY="..."  # opzionale

pip install requests
python screener.py
```

Stamperà su stdout la TOP 5 attuale e (se diversa dalla precedente) manderà
gli alert Telegram.

## Costi

**Tutto gratis**, nei limiti dei free tier:

- GitHub Actions: 2000 min/mese repo privati, illimitato repo pubblici
- Binance API: pubblica, no auth
- Finnhub free: 60 chiamate/min (più che sufficienti)
- Telegram Bot API: illimitata

## Disclaimer

Questo è uno **screener tecnico**, non un consiglio di investimento.
Gli indicatori mean-reversion (RSI, MACD, Bollinger) sono storici e non
garantiscono performance future. Il trading intraday su crypto e azioni
comporta rischi significativi di perdita. Verifica sempre i segnali su
fonte primaria, considera commissioni e spread, e investi solo capitale
che puoi permetterti di perdere.
