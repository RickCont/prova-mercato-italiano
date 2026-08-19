# Prossimi passi

Il dataset è finito e validato. Da qui in avanti si tratta di usarlo, e la prima cosa
da fare è riscrivere il generatore di segnali: il motore di backtest invece va
adattato, non rifatto.

Stato di partenza: il pannello ha 1.151.784 righe su 404 titoli dal 2000, il codice
del progetto precedente è in [`da_riscrivere/`](da_riscrivere/) con l'analisi
file per file di cosa cambiare.

---

## Passo 1 — `segnali.py`, riscrittura di `build_technical_signals.py`

È il pezzo che decide *quali titoli comprare*, e l'unico che va riscritto da zero.
Deve produrre lo stesso schema che il backtester si aspetta
(`data_articolo, ticker, rank, rating_score, recommendation`), così il motore continua
a funzionare senza modifiche.

**1.1 Filtri di eleggibilità applicati riga per riga.** Non sulla lista dei ticker: è
l'unico modo per farli variare nel tempo e per poter misurare quanto contano.

```python
eleggibile = (d.sedute_di_storia >= 252)          # stagionatura post-IPO + warm-up
           & (d.controvalore_medio_20g >= 1e5)    # 100.000 EUR al giorno
           & (d.sedute_scambiate_20g >= 18)       # scambia quasi ogni giorno
           & (d.chiusura >= 0.10)                 # non 1,00: escluderebbe TESMEC, CIR, GEOX
           & (d.raggruppamento_24m == 0)          # nessun raggruppamento recente
```

Tutti e cinque devono essere parametri da riga di comando, perché il Passo 3 consiste
esattamente nel misurare cosa cambia togliendoli.

**1.2 Sostituire la soglia percentuale fissa.** `--soglia 10` su 40 titoli scattava
5.052 volte in 26 anni ed era selettiva. Su 404 titoli scatta 14.423 volte, e in una
giornata di panico su decine di titoli insieme: fra quelli il `top_n` ritaglia i più
volatili, che sono anche i più illiquidi. È il meccanismo che sull'S&P 500 aveva
prodotto beta 1,61 e alpha −1,83 %. Due alternative da confrontare:

* `var_5g_in_atr` — la caduta in unità di volatilità del titolo. Rende confrontabili
  una utility e una microcap;
* `pct_var_5g` — il percentile dentro la giornata. Selettivo per costruzione,
  qualunque sia il numero di titoli.

**1.3 Usare le colonne nuove.** `rendimento_dividendo_12m` come discriminante fra
*"sceso del 10 % ma rende il 7 %"* e *"sceso del 10 % e non paga nulla"*;
`var_5g_rel` per depurare dal movimento di mercato; `variazione_dividendo` a −1
(dividendo azzerato) come esclusione.

**1.4 Correggere il look-ahead in `rating_score`.** La versione vecchia riscala
l'intensità su 1-5 usando minimo e massimo di **tutto il campione**, quindi il
punteggio di una data dipende da date future. Non influenza le decisioni — l'ordine è
fissato dal `rank`, calcolato dentro la giornata — ma è look-ahead nella colonna. Va
normalizzato per data.

**1.5 Neutralizzazione settoriale, da valutare.** `dati/anagrafica.csv` ha il settore
per 402 titoli su 406. Senza neutralizzazione, in una giornata di stress bancario la
strategia compra otto banche e quello che ha in portafoglio è una scommessa
settoriale, non otto idee. Da provare come opzione, non come default: il settore è una
fotografia di oggi, e per la maggioranza dei titoli è ragionevolmente stabile ma non
garantito.

## Passo 2 — adattare `backtest_strategy.py`

La meccanica è corretta e testata (`da_riscrivere/tests/test_backtest.py`). Servono
tre cose.

**2.1 Adattatore di schema.** Carica tramite `price_data.load_prices`, che si aspetta
`date, ticker, open, high, low, close`. Il nuovo `dati/prezzi_grezzi.csv.gz` ha
`data, apertura, massimo, minimo, chiusura, chiusura_agg, volume, dividendo, split,
ticker, fattore_dividendi`.

> **Mappare `close` su `chiusura_agg`, non su `chiusura`.** Nel vecchio dataset
> `chiusura` era il prezzo aggiustato; qui è quello grezzo. La mappatura ovvia per
> nome butta via i dividendi: su Milano significa perdere la maggior parte del
> rendimento e reintrodurre i falsi crolli nei giorni di stacco. È l'errore che non
> dà nessun messaggio d'errore.

**2.2 Costi per fascia di liquidità.** Oggi c'è una commissione piatta uguale per
tutti. Misurato sui 404 titoli:

| Controvalore/giorno | Titoli | Spread mediano | 90° pct |
|---|---|---|---|
| > 10 M | 42 | 0,05 % | 0,10 % |
| 1 – 10 M | 46 | 0,19 % | 0,36 % |
| 200k – 1 M | 42 | 0,43 % | 0,80 % |
| 50k – 200k | 40 | 0,57 % | 1,68 % |
| 10k – 50k | 80 | 1,86 % | 4,52 % |
| < 10k | 154 | 3,21 % | 16,6 % |

Tre ordini di grandezza: qualunque numero unico si scelga è sbagliato per metà
dell'universo. La tabella la produce `anagrafica.costo_per_fascia()`. Va aggiunto lo
spread come costo, non solo la commissione: su questo universo è il costo dominante.

**2.3 Benchmark total return.** `FTSEMIB.MI` è un indice *price*: zero dividendi in
7.050 sedute, contro prezzi dei titoli che sono total return. Misurato dal 2008: FTSE
MIB price **+1,77 %** annuo contro ETF `IMIB.MI` total return **+4,16 %**, cioè 2,4
punti l'anno regalati alla strategia. Va aggiunto l'ETF come benchmark di mercato,
tenendo `EQUIPESATO` per il survivorship bias — misurano due cose diverse e servono
entrambi.

## Passo 3 — i due esperimenti

Non uno. Il secondo è quello che dà senso al primo.

**Test primario, universo liquido.** Coi filtri del Passo 1: circa 135 titoli al
giorno nel periodo recente, 49 nel 2000-2007. È l'unico investibile, ed è comunque
tre volte il FTSE MIB.

**Test di robustezza, universo completo.** Gli stessi parametri senza i filtri di
liquidità. Non per investirci: per **misurare** quanto rendimento viene
dall'illiquidità. Se il completo va molto meglio del liquido, quella differenza non è
alpha — è prezzo fermo e spread non pagato.

Il motivo è meccanico e va tenuto presente leggendo i risultati: 154 titoli su 404
scambiano meno di 10.000 € al giorno e 152 hanno almeno il 10 % delle sedute a volume
zero. Un prezzo che non si muove produce **finto ritorno alla media** — la "caduta"
del giorno X è spesso la stampa arretrata di una discesa già avvenuta, e il "recupero"
del giorno dopo è meccanico. Su questo universo la regola `caduta` sembrerà brillante
per costruzione.

**Sotto-periodi separati, obbligatorio.** L'ampiezza dell'universo investibile passa
da 49 titoli al giorno (2000-2007) a 135 (2021-2026): un risultato 2000-2026 riportato
come numero unico è dominato dagli ultimi anni. Riportare il periodo principale
**dal 2015** e il precedente a parte.

## Passo 4 — le regole mai testate

Nel progetto precedente solo `caduta` è stata provata. Restano implementate e non
verificate `dal-massimo`, `rsi`, `giorni-rossi` e `momentum`, più le varianti nuove.

La più interessante resta **`momentum`**: se comprare i perdenti carica beta senza
alpha, l'esperimento simmetrico merita la verifica prima di ogni altra cosa. Poi
`rsi_2`, che sul ritorno alla media di brevissimo è molto più reattivo del 14, e le
combinazioni con `rendimento_dividendo_12m`.

---

## Questioni aperte

**Il test `repair=True` non è stato fatto.** `prezzi.py` ha il parametro
`--riparazione`, che attiva la correzione di yfinance per gli errori noti di Yahoo
(prezzi sbagliati di 100x, aggiustamenti mancanti). Non è mai stato eseguito. Su un
listino dove 57 serie su 404 sono state accorciate per corruzione dell'aggiustamento,
un confronto A/B vale la pena: se recupera storia che oggi buttiamo, cambia i risultati
dei primi anni. Costa uno scarico completo (~2 min) più la costruzione.

**Il survivorship bias resta senza rimedio.** L'universo è il listino di *oggi*: i
delistati per fallimento o OPA mancano, e Yahoo non offre un modo di recuperarli.
L'effetto è forte e, peggio, **cambia segno lungo il campione**: le coorti vecchie sono
sopravvissuti che sovrastimano il rendimento, mentre le IPO recenti hanno survivorship
bias quasi nullo ma vengono comprate *preferenzialmente* da una strategia
mean-reversion. Due bias in direzioni opposte non si correggono nemmeno di segno.
`EQUIPESATO` mitiga il confronto ma non i livelli assoluti. Va dichiarato in ogni
risultato, non risolto.

**Due salti residui non ripuliti.** `BAN.MI` (2002) e `FDA.MI` (2026) hanno salti fra
1,5x e 2,5x, sotto la soglia di `sanifica()`. Non sono stati eliminati abbassando la
soglia perché almeno uno è **reale**: FIDIA quota 0,008 €, dove il tick minimo da
0,001 vale il 12 % del prezzo. Il filtro sul prezzo minimo di 0,10 € li esclude
dall'universo investibile, che è il posto giusto per gestirli.

**Niente fondamentali, e non è un ritardo ma un limite.** Verificato campo per campo:
la capitalizzazione point-in-time parte dal 2015 per ENI e dal 2024 per le microcap;
i bilanci danno 3-5 annualità senza data di pubblicazione affidabile; i multipli di
`.info` sono solo il valore odierno. Niente fattore size onesto, niente value. Il
dividendo è l'unico segnale quasi-fondamentale ottenibile senza look-ahead, ed è già
nel pannello.
