# Mercati Screener

Scanner automatico su crypto, azioni e commodity. Gira su GitHub Actions,
pubblica una dashboard su GitHub Pages (`docs/`) e manda alert su Telegram.

## Le tre modalità

| Modalità | Cosa cerca | Alert Telegram |
|---|---|---|
| **Accelerazioni** | Rottura del massimo a 20/55 giorni con volume, forza relativa vs BTC o vs indice, trend (ADX) | 🚀 solo crypto con score ≥ 100, volume confermato e regime positivo |
| **In carica** | Volatilità compressa (Bollinger nel 20% più stretto degli ultimi 120 giorni), vicino al tetto del range, accumulo volumi | nessuno all'ingresso · ⚡ **Squeeze scattato** (crypto) quando rompe verso l'alto |
| **Rimbalzi** | Mean-reversion: RSI basso, sotto MA20 / Bollinger inferiore (logica originale) | nessuno: solo lista informativa |

## Modello v4 (da novembre 2026)

Storia breve: la v2 (Rimbalzi + Accelerazioni) ha perso nelle prime settimane; la v3
ha tenuto solo i breakout crypto con BTC sopra la media a 50 giorni e stop −8%; nelle
due settimane successive 4 operazioni su 4 sono finite allo stop, con una perdita media
di −13,7% invece di −8% (stop controllato ogni 30 minuti, token poco liquidi).

`research.py` (workflow **Ricerca**) ha rifatto i conti su 5 anni con esecuzione
realistica (entrata il giorno dopo il segnale, 1% di slittamento). Esito:

- **Velocità**: un giorno di ritardo toglie circa un terzo del guadagno medio; con lo
  stop a −8% il vantaggio della v3 spariva quasi del tutto (profit factor 1,03).
- **Stop**: uno stop a 3× l'ATR a 14 giorni (tipicamente −21%) rende più del −8% fisso,
  in entrambe le metà del periodo. Per compensare la posizione è dimensionata a
  rischio fisso: lo stop costa l'1% del capitale (massimo 20% in una posizione).
- **Regime**: BTC sopra la media a 50 giorni **e** media 50 sopra la 200. Resa un po'
  più bassa del filtro solo MA50 (14% contro 17,5% l'anno, stesso Sharpe) ma calo
  massimo quasi dimezzato (−13% contro −24%) e fuori da tutto il 2022.
  Nella prima metà del periodo è leggermente peggiore (profit factor 1,57 contro
  1,61), nella seconda molto migliore (1,82 contro 1,15).
- **Scartate**: rotazione settimanale tra le crypto più forti (−12% l'anno, calo
  massimo −71%) e rotazione mensile tra 13 ETF (+7% l'anno contro +20% dell'S&P 500
  tenuto). Il filtro di liquidità e uscite più lunghe non migliorano in modo robusto.

Breakout crypto, portafoglio da 10.000 valutato ogni giorno, esecuzione realistica:

| Anno | Regole v3 | Regole v4 | BTC tenuto |
|---|---|---|---|
| 2022 | −17,9% | 0,0% | −64,8% |
| 2023 | +23,0% | +12,2% | +153,7% |
| 2024 | +75,1% | +74,1% | +120,2% |
| 2025 | +1,3% | −3,0% | −4,6% |
| 2026 (a ottobre) | −24,3% | 0,0% | −12,6% |
| Totale | +35,7% (6,5%/anno, calo max −35%) | +89,7% (14,0%/anno, calo max −14%) | +65,7% (10,9%/anno, calo max −69%) |

Le regole stanno in `SIGNAL_POLICY` e `REGIME_MA` (`screener.py`) e in
`PAPER_RULES` (`paper.py`): un segnale crypto è **operativo** (alert + simulazione)
o **in pausa** (regime negativo). Il regime usa l'ultima chiusura giornaliera
completa. Ogni alert riporta il piano: importo sul tuo capitale (`/capitale`),
prezzo di stop, data di uscita. Azioni e commodity sono solo liste informative
(vedi sotto).

Limiti: universo di crypto liquide di oggi (survivorship bias), barre
giornaliere, regole scelte sugli stessi dati. Aspettati meno. Trade Republic non
accetta ordini stop sulle crypto: con TR lo stop va controllato a mano; Kraken e
Binance lo eseguono da soli. Per rifare i conti: Actions → **Ricerca** (report in
`research_results/report.md`) o **Backtest** (`backtest_results/report.md`).

## Verifica di metà novembre 2026 (v4.1)

Due settimane dopo il lancio della v4 non era arrivato nessun alert. Il sistema
funzionava: BTC era sceso da circa 77.400 $ a 64.150 $ (−17%), il regime crypto è
rimasto negativo e il portafoglio «Breakout crypto» è rimasto fermo a 10.000. I
segnali che il modello ha scartato sono andati male: Rimbalzi crypto −7,8% in media
a 7 giorni (12% in guadagno), Accelerazioni sotto soglia −4,1%. Le regole crypto non
sono cambiate: non c'era nessuna prova nuova contro di loro.

Il limite vero è che con le crypto in discesa il modello resta muto per settimane o
mesi. Abbiamo quindi cercato sul serio un modello per le azioni e messo alla prova il
modello «Kronos» che gira sui social. Nessuno dei due ha passato le regole fissate
prima di guardare i risultati.

**Azioni** (`research_stocks.py`, workflow **Ricerca azioni**): 500 azioni dell'S&P 500
di oggi, valutazione dal 26/12/2017 al 13/11/2026, ogni azione contata solo dopo
l'ingresso nell'indice, segnale a fine giornata ed entrata all'apertura del giorno
dopo, costi 0,2% per lato. Riferimenti: S&P 500 tenuto +14,1% l'anno (calo massimo
−34%), le stesse 500 azioni a pesi uguali +14,2% l'anno.

| Strategia | Risultato | Regola per promuoverla |
|---|---|---|
| Breakout dello screener (6.205 segnali) | rende **meno** dell'universo: −0,58% a operazione a 5 sedute (t −5,6), −0,65% a 20 sedute (t −4,2); negativo in entrambe le metà; profit factor 0,80–1,06 | non passa |
| Salto di prezzo ≥ 5% con volume ≥ 3× (884 segnali) | eccesso sull'universo tra −0,5% e −0,9% a operazione con tutte le uscite | non passa |
| Momentum mensile, 10 azioni più forti a 12 mesi | con filtro di regime +3,1% l'anno; senza filtro +16,3% l'anno ma volatilità 36%, calo massimo −46% e prima metà sotto l'indice (+9,6% contro +12,0%) | non passa |
| S&P 500 tenuto solo sopra la media a 200 giorni | +6,5% l'anno (calo massimo −28%) | peggio di tenerlo sempre |

Conclusione: sulle azioni, in questi anni, tenere un ETF ha battuto tutto quello che
abbiamo provato. Azioni e commodity restano quindi liste informative, come i Rimbalzi, e
l'esperimento «Breakout azioni» è stato chiuso (le posizioni aperte seguono le loro
regole, poi il risultato va in archivio). Limite del test: mancano le azioni uscite
dall'indice (survivorship bias), per questo il confronto è con lo stesso universo.

**Kronos** (`research_kronos.py`, workflow **Ricerca Kronos**): modello open source
addestrato su miliardi di candele, usato così com'è (Kronos-small, 256 candele
giornaliere, 10 campioni) su 20 crypto liquide e solo su date successive alla sua
pubblicazione: 15/9/2025 → 13/11/2026, 425 giorni, 8.500 previsioni.

| Misura | Risultato |
|---|---|
| Correlazione di rango previsto/reale a 1 giorno | −0,007 (t −0,5): nulla |
| Correlazione a 5 giorni | −0,016 (t −0,5): nulla |
| Direzione indovinata a 1 giorno | 48,8% (dire sempre «scende» avrebbe fatto 54,4%) |
| Ogni giorno le 3 crypto con la previsione più alta | −89,9% contro −63,0% delle 20 crypto a pesi uguali |
| BTC tenuto solo con previsione positiva | −34,8% contro −44,4% di BTC tenuto sempre |

L'ultimo numero non è capacità di previsione: sta fuori dal mercato il 44% dei giorni in
un periodo di ribasso, e su BTC ed ETH indovina la direzione il 50,7% delle volte (±1,7).
Kronos non entra nel modello. Limite del test: orizzonte giornaliero (quello che ci serve),
non le candele orarie dei post.

Cosa è cambiato davvero nel codice:

- **Doppio avvio**: quando lo schedule di GitHub e cron-job.org partivano insieme, il
  secondo lavorava su uno stato vecchio, falliva il salvataggio e poteva mandare
  messaggi doppi. Ora il lavoro parte dalla punta del branch e salta se l'ultimo
  aggiornamento è di meno di 10 minuti fa (`MIN_RUN_GAP_MIN`).
- **Riepilogo della domenica** su Telegram (ore 18): sistema attivo, regime e cosa
  manca perché torni positivo, segnali della settimana e come sono andati quelli
  scartati, portafogli. Si spegne con `/settimanale off`.
- **`/stato`**: in qualsiasi momento il bot risponde con lo stato del sistema e del regime.
- **Cosa manca al regime**: in dashboard e nei messaggi («BTC deve chiudere sopra …»).
- **Importi negli alert**: corretti i separatori per importi da 1.000 € in su.

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

`paper.py` gestisce i portafogli da 10.000 finti, salvati in `paper.json`:

- **Breakout crypto**: automatico, compra a ogni segnale operativo (Accelerazioni e
  Squeeze crypto) l'importo per cui lo stop a 3× ATR costa l'1% del capitale, e vende
  allo stop o dopo 5 giorni.
- **Breakout azioni** (esperimento chiuso a novembre 2026): non apre più posizioni;
  quelle aperte seguono le loro regole (stop −12%, trailing 20% dopo +10%, massimo
  20 giorni), poi il portafoglio passa da solo in archivio.
- **Le mie scelte**: manuale, via Telegram — `/compra QNT 500`, `/vendi QNT`,
  `/vendi QNT 50%`, `/portafoglio`, `/riepilogo`, `/capitale 5000` (il capitale
  su cui gli alert calcolano l'importo). Gli ordini sono eseguiti al prezzo del
  prossimo aggiornamento; ogni acquisto è marcato "suggerito" se l'asset era in
  una lista dello scanner in quel momento.

Altri comandi del bot: `/stato` (sistema e regime), `/settimanale on|off`
(riepilogo della domenica), `/aiuto`.

Costi per operazione in `COSTS`. Ogni portafoglio ha la sua data di partenza e il
suo benchmark (BTC o S&P 500). La dashboard mostra valore, rendimento, quota di
operazioni positive, profit factor, calo massimo e un verdetto che resta
"in raccolta" fino a 20 operazioni chiuse. I portafogli dei modelli v2 e v3 e gli
esperimenti chiusi sono conservati nell'archivio di `paper.json`.

## Registro segnali

Ogni asset che entra nella top di una modalità viene registrato in
`signals_log.json` con il prezzo del momento, poi misurato dopo 1, 3 e 7 giorni.
La dashboard mostra per ogni modalità la quota di segnali in guadagno e il
rendimento medio, e a parte i soli segnali **operativi** del modello in uso: è il
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
- `MIN_RUN_GAP_MIN` — due avvii a meno di 10 minuti: il secondo salta (`SCREENER_FORCE=1` per forzare)
- `WEEKLY_WEEKDAY`, `WEEKLY_HOUR` — quando parte il riepilogo settimanale (domenica, 18 ora italiana)
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
