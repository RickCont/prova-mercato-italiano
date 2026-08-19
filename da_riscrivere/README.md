# `da_riscrivere/` — il codice del progetto precedente

Questi file vengono dal progetto sul FTSE MIB e stanno qui come **riferimento**, non
come parte funzionante di questo repo. Sono il motore di backtest e le metriche che
hanno prodotto i risultati citati nel [README](../README.md) principale: vanno
riscritti per il nuovo dataset, non buttati, perché la meccanica di acquisto, take
profit, stop loss e de-duplicazione è già corretta e testata.

Girano ancora, se lanciati **da dentro questa cartella** e puntati ai vecchi dati: la
catena delle dipendenze è completa. Non importarli dai moduli nuovi.

```
build_technical_signals.py   regola tecnica -> file di segnali
backtest_strategy.py         il motore: DCA, take profit, stop loss, de-duplicazione
  ├── metriche.py            esposizione, beta, alpha, t-stat
  └── price_data.py          cache prezzi          [superato da ../prezzi.py]
        └── universi.py      costituenti indici    [superato da ../universo.py]
confronto_varianti.py        esegue piu' scenari e stampa la tabella comparativa
tests/test_backtest.py       test del motore, dipende solo da backtest e metriche
```

---

## La trappola da conoscere prima di toccare qualsiasi cosa

Nel vecchio dataset la colonna **`chiusura` era il prezzo aggiustato** per split e
dividendi. Nel pannello nuovo `chiusura` è il prezzo **grezzo** (aggiustato per i soli
split) e quello aggiustato si chiama **`chiusura_agg`**.

Questo significa che `build_technical_signals.py` **gira senza errori sul pannello
nuovo e calcola risultati sbagliati**: tutte le colonne che legge — `data`, `ticker`,
`chiusura`, `var_5g`, `dal_massimo_52s`, `rsi_14`, `giorni_rossi_consecutivi` —
esistono con lo stesso nome. Nessuna eccezione, nessun avviso, solo numeri diversi.

È l'unico punto di questa cartella in cui un errore non si manifesta come crash. Per
la cronaca: nel suo caso specifico l'effetto è benigno, perché usa `chiusura` solo per
il filtro sul prezzo minimo, e lì il prezzo grezzo è anzi *più* corretto — è quello a
cui il titolo tratta davvero. Ma la stessa sostituzione fatta altrove, per esempio
mappando `close -> chiusura` in `price_data.py`, butterebbe via i dividendi: su Milano
significa perdere la maggior parte del rendimento e reintrodurre i falsi crolli nei
giorni di stacco.

**Regola:** i rendimenti si calcolano sempre su `chiusura_agg`, il controvalore e i
filtri di prezzo sempre su `chiusura`.

---

## File per file: cosa va cambiato

### `build_technical_signals.py` — da riscrivere per primo

Traduce una regola tecnica in un file di segnali nello stesso schema del dataset delle
raccomandazioni degli analisti (`data_articolo, ticker, rank, rating_score,
recommendation`), così il backtester funziona senza modifiche. L'idea è buona e va
conservata. Cosa non funziona più:

**1. Nessun filtro di eleggibilità.** Ha un solo filtro, `--min-prezzo`, con default
`1.0`. Su un universo di 40 blue chip bastava; su 404 titoli è insufficiente e, nel
merito, sbagliato:

* **`--min-prezzo 1.0` esclude titoli validi.** Su Milano 90 titoli su 404 quotano
  sotto 1 €, fra cui TESMEC (1,6 M€ di scambi al giorno), CIR, RCS Mediagroup, IMMSI e
  GEOX. Il prezzo unitario basso non dice niente sulla qualità: il proxy giusto di
  investibilità è il controvalore. Serve `0.10`, non `1.00`, e solo per escludere i
  casi dove il tick minimo è già l'1 % del prezzo.
* **Manca del tutto il filtro di liquidità.** 154 titoli su 404 scambiano meno di
  10.000 € al giorno e 152 hanno almeno il 10 % delle sedute a volume zero. Servono
  `controvalore_medio_20g >= 100_000` e `sedute_scambiate_20g >= 18`.
* **Manca la stagionatura post-IPO.** Serve `sedute_di_storia >= 252`.

I filtri vanno applicati **riga per riga**, non sulla lista dei ticker: è l'unico modo
per farli cambiare nel tempo e per poter misurare quanto contano.

**2. La soglia percentuale fissa non seleziona più.** `--soglia 10` su 40 titoli
scattava 5.052 volte in 26 anni ed era selettiva. Su 404 titoli, in una giornata di
panico scatta su decine di titoli insieme, e il `top_n` fra questi ritaglia i più
volatili — che sono anche i più illiquidi. È il meccanismo che sull'S&P 500 aveva
prodotto beta 1,61 e alpha *negativo*: non bravura, leva. Sostituire con
`var_5g_in_atr` (la caduta in unità di volatilità del titolo) o con `pct_var_5g` (il
percentile dentro la giornata).

**3. Non usa le colonne nuove.** `rendimento_dividendo_12m` per distinguere "sceso del
10 % ma rende il 7 %" da "sceso del 10 % e non paga nulla"; `raggruppamento_24m` per
escludere i titoli in difficoltà; `var_5g_rel` per depurare dal movimento di mercato.

**4. `rating_score` è una finzione di formato.** Riscala l'intensità del segnale su
1-5 solo perché il backtester si aspetta quello schema. Va bene, ma il minimo e il
massimo sono calcolati **su tutto il campione**, quindi il punteggio di una data
dipende da date future. Non influenza le decisioni — l'ordinamento è già fissato dal
`rank`, che è calcolato dentro la giornata — ma è look-ahead nella colonna, e chi la
usasse come feature sbaglierebbe. Da normalizzare per data.

### `backtest_strategy.py` — il motore, da adattare non da rifare

La meccanica è corretta e testata: versamenti, take profit, stop loss, una sola
posizione per titolo, `--dimensione-posizione patrimonio`, `--benchmark EQUIPESATO`,
`--valuta locale`. Cosa serve:

* **Adattatore per i prezzi.** Carica tramite `price_data.load_prices`, che si aspetta
  lo schema `date, ticker, open, high, low, close`. Il nuovo `dati/prezzi_grezzi.csv.gz`
  ha `data, apertura, massimo, minimo, chiusura, chiusura_agg, volume, dividendo,
  split, ticker, fattore_dividendi`. Mappare `close -> chiusura_agg` (vedi la trappola
  sopra), e `open/high/low` alle colonne riscalate.
* **Costi per fascia di liquidità.** Ha `--commissione-pct` e `--commissione-fissa`,
  un numero **piatto uguale per tutti i titoli**. Fra ENI (spread 0,05 %) e una
  microcap dell'ex-AIM (3,21 % mediano, 16,6 % al 90° percentile) ci sono tre ordini
  di grandezza: qualunque valore unico si scelga è sbagliato per metà dell'universo.
  Con un take profit del 10 %, nella fascia 10-50k il giro completo si mangia il 19 %
  del guadagno. La tabella di calibrazione la produce
  `anagrafica.costo_per_fascia()`.
* **Nessuno spread modellato.** Oggi il costo è solo commissionale. Su questo universo
  lo spread è il costo dominante, non un dettaglio.
* **Benchmark total return.** `FTSEMIB.MI` è un indice *price*: zero dividendi in
  7.050 sedute, mentre i prezzi dei titoli sono total return. Misurato: FTSE MIB price
  +1,77 % annuo dal 2008 contro ETF `IMIB.MI` total return +4,16 % — 2,4 punti l'anno
  regalati alla strategia. Va aggiunto l'ETF come benchmark di mercato, tenendo
  `EQUIPESATO` per il survivorship bias.

### `metriche.py` — riutilizzabile quasi così com'è

Esposizione, beta, alpha, t-stat. È la parte che nel progetto precedente ha smontato
il falso "+2,26 punti sull'S&P 500" mostrando che era beta 1,61 e alpha −1,83 %.
Nessuna dipendenza dallo schema dei dati: prende serie di rendimenti. Da rivedere solo
il benchmark che le si passa, per il punto sopra.

### `confronto_varianti.py` — da riscrivere sui due esperimenti

Carica dati una volta e confronta scenari. Struttura buona, scenari da rifare: servono
i due esperimenti previsti — **universo liquido** come test primario (~135 titoli al
giorno nel periodo recente, quello investibile) e **universo completo** come misura di
quanto rendimento viene dall'illiquidità. Se il completo va molto meglio del liquido,
quella differenza non è alpha: è prezzo fermo e spread non pagato.

Va aggiunta la separazione dei sotto-periodi. Fino al 2014 l'universo investibile è
appena più grande del FTSE MIB (49 titoli al giorno nel 2000-2007, 65 nel 2008-2014,
contro 135 nel 2021-2026): un risultato 2000-2026 riportato come numero unico è
dominato dagli ultimi anni.

### `price_data.py` e `universi.py` — superati

`price_data.py` è sostituito da [`../prezzi.py`](../prezzi.py), che scarica con
`auto_adjust=False` per conservare dividendi e split, e ha una pulizia molto più
severa: quella vecchia tagliava solo prima dell'ultimo prezzo negativo e lasciava
passare la cascata di aggiustamenti che segue (su `BES.MI` un rapporto massimo/ultimo
di 748 milioni).

`universi.py` è sostituito da [`../universo.py`](../universo.py): prendeva i
costituenti degli indici da Wikipedia, mentre ora serve il listino completo dal product
directory di Euronext.

Stanno qui solo perché `backtest_strategy.py` non importa senza di loro. **Non
importarli dai moduli nuovi.**

### `tests/test_backtest.py` — da tenere e ampliare

Testa il motore e dipende solo da `backtest_strategy` e `metriche`, quindi è portabile
così com'è. Gli altri test del progetto precedente non sono stati copiati perché
dipendono da `build_recommendations_dataset.py`, che riguarda i giudizi degli analisti:
inutilizzabile qui, `upgrades_downgrades` di Yahoo è vuoto per i titoli non americani.
