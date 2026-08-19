# Il dataset — dizionario dei dati

Documento di riferimento su **cosa contiene** `dati/pannello_italia.csv.gz`, come è
stato costruito e cosa non va fatto con esso. Per il ragionamento sulle scelte di
progetto vedi [README.md](README.md).

---

## In due righe

Un pannello di **prezzi e indicatori tecnici giornalieri su tutte le azioni quotate a
Milano** — Euronext Milan e Euronext Growth Milan — dal 3 gennaio 2000 al 18 agosto
2026. Una riga per coppia (data, ticker). Tutti gli indicatori usano **solo dati fino
a quel giorno compreso**: nessuno guarda avanti, quindi il pannello è utilizzabile in
backtest senza ulteriori precauzioni.

```
1.151.784 righe   404 titoli   54 colonne   182 MB compresso   ~1,3 GB in RAM
```

## I file

| File | Cos'è | Dimensione |
|---|---|---|
| `dati/pannello_italia.csv.gz` | **il dataset**: prezzi + indicatori, una riga per (data, ticker) | 182 MB |
| `dati/prezzi_grezzi.csv.gz` | cache dello scarico Yahoo, prima della pulizia e degli indicatori | 36 MB |
| `dati/universo_italia.txt` | i 406 ticker Yahoo, uno per riga | 3 KB |
| `dati/listino_euronext.csv` | anagrafica Euronext: ISIN, nome, mercato, sedute disponibili | 21 KB |
| `dati/anagrafica.csv` | fotografia **di oggi**: settore, industria, capitalizzazione, spread | 54 KB |
| `dati/indice_ftsemib.csv.gz` | serie dell'indice `FTSEMIB.MI`, per il beta | 124 KB |
| `dati/log_costruzione.txt` | log dell'ultima costruzione, con i ticker accorciati e scartati | |

I `.csv.gz` sono in `.gitignore`: si rigenerano con `python costruisci_dataset.py` in
circa dieci minuti, di cui uno di scarico.

## Come si carica

Il file intero occupa circa 1,3 GB in memoria. Su una regola che ne usa tre colonne
conviene leggere solo quelle:

```python
import pandas as pd

# tutto (serve RAM)
d = pd.read_csv("dati/pannello_italia.csv.gz", parse_dates=["data"])

# solo cio' che serve, molto piu' veloce e leggero
d = pd.read_csv("dati/pannello_italia.csv.gz", parse_dates=["data"],
                usecols=["data", "ticker", "chiusura", "var_5g",
                         "controvalore_medio_20g", "sedute_di_storia"],
                dtype={"ticker": "category"})
```

L'universo investibile si ottiene filtrando **riga per riga**, non a monte:

```python
inv = d[(d.sedute_di_storia >= 252)            # stagionatura post-IPO + warm-up
        & (d.controvalore_medio_20g >= 1e5)    # 100.000 EUR al giorno
        & (d.sedute_scambiate_20g >= 18)       # scambia quasi ogni giorno
        & (d.chiusura >= 0.10)]                # esclude i titoli dove il tick e' l'1%
```

---

## Le colonne

`copertura` è la percentuale di righe non vuote; i valori vuoti a inizio serie sono il
warm-up delle finestre mobili (`var_252g` ha bisogno di 252 sedute, `dist_sma200` di
100). `p1` e `p99` sono il 1° e il 99° percentile, per dare la scala.

### Identificativi

| Colonna | Tipo | Cos'è |
|---|---|---|
| `data` | data | Giorno di borsa, normalizzato a mezzanotte, senza fuso |
| `ticker` | testo | Simbolo Yahoo, sempre con suffisso `.MI` (`ENI.MI`) |

### Prezzi e volumi

Le variazioni si calcolano **sempre** su `chiusura_agg`; il controvalore **sempre** su
`chiusura`. Confonderle è l'errore che questo dataset esiste per evitare.

| Colonna | Copertura | p1 / mediana / p99 | Cos'è |
|---|---|---|---|
| `chiusura_agg` | 100 % | | Prezzo aggiustato per split **e dividendi** (Adj Close). È il total return: la base di **tutti** i rendimenti |
| `chiusura` | 100 % | | Prezzo aggiustato per i soli split (Close). Serve per il controvalore, il prezzo reale e il tick |
| `apertura`, `massimo`, `minimo` | 100 % | | Riscalati col rapporto `chiusura_agg / chiusura`, per stare sulla stessa scala dei rendimenti |
| `volume` | 100 % | | Pezzi scambiati, aggiustati per gli split |
| `dividendo` | 100 % | 0 nella grande maggioranza | Dividendo per azione staccato quel giorno, 0 altrimenti |
| `split` | 100 % | 0 / 0 / 0 | Fattore di split quel giorno, 0 se nessuno. **Sotto 1 è un raggruppamento** |
| `fattore_dividendi` | 100 % | | `chiusura_agg / chiusura`. Per costruzione ≤ 1: diagnostica dell'aggiustamento |

### Variazioni

| Colonna | Copertura | p1 / mediana / p99 |
|---|---|---|
| `var_1g` | 100 % | −0,064 / 0 / +0,077 |
| `var_5g` | 99,8 % | −0,141 / 0 / +0,176 |
| `var_20g` | 99,3 % | −0,275 / 0 / +0,362 |
| `var_60g` | 97,9 % | −0,433 / 0 / +0,664 |
| `var_252g` | 91,4 % | −0,715 / +0,004 / +1,769 |

Frazioni, non percentuali: `−0,10` significa −10 %.

### Distanza dagli estremi a 52 settimane

| Colonna | Copertura | p1 / mediana / p99 | Cos'è |
|---|---|---|---|
| `massimo_52s`, `minimo_52s` | 99,3 % | | I due livelli, in euro |
| `dal_massimo_52s` | 99,3 % | −0,755 / **−0,165** / 0 | Distanza dal massimo. Sempre ≤ 0. La misura più usata per comprare i ribassi |
| `dal_minimo_52s` | 99,3 % | 0 / +0,232 / +2,15 | Distanza dal minimo. Sempre ≥ 0 |

### Trend

| Colonna | Copertura | p1 / mediana / p99 |
|---|---|---|
| `dist_sma20` | 99,7 % | −0,167 / −0,001 / +0,182 |
| `dist_sma50` | 99,2 % | −0,270 / −0,001 / +0,292 |
| `dist_sma200` | 96,6 % | −0,504 / −0,001 / +0,610 |

Distanza percentuale dalla media mobile, non il livello: così i titoli sono
confrontabili a prescindere dal prezzo unitario.

### Oscillatori

| Colonna | Copertura | p1 / mediana / p99 | Cos'è |
|---|---|---|---|
| `rsi_14` | 100 % | 15,0 / 49,7 / 84,2 | RSI di Wilder a 14 periodi. Sotto 30 ipervenduto, sopra 70 ipercomprato |
| `rsi_2` | 100 % | 0,02 / 48,5 / 99,9 | RSI a 2 periodi. Per il ritorno alla media di brevissimo è molto più reattivo: su un crollo di tre sedute il 14 non fa in tempo a scendere |
| `zscore_20g` | 98,3 % | −2,69 / −0,06 / +2,83 | Scarti standard dalla media a 20 giorni. È il %B di Bollinger ricentrato |

### Rischio e volatilità

| Colonna | Copertura | p1 / mediana / p99 | Cos'è |
|---|---|---|---|
| `volatilita_20g` | 99,6 % | 0 / **0,291** / 1,189 | Deviazione standard dei rendimenti a 20 giorni, annualizzata |
| `volatilita_60g` | 99,0 % | 0,022 / 0,313 / 1,087 | Idem su 60 giorni |
| `rapporto_volatilita` | 98,0 % | 0,294 / 0,958 / 1,604 | `vol_20g / vol_60g`. Sopra 1 è uno shock recente, non un titolo cronicamente agitato |
| `escursione_media_20g` | 99,7 % | 0 / 0,026 / 0,081 | Ampiezza media della seduta, in frazione del prezzo |
| `atr_14` | 99,5 % | | Average True Range di Wilder, in euro. Tiene conto dei salti fra sedute, quindi non sottostima i gap |
| `atr_pct_14` | 99,5 % | 0,0001 / **0,029** / 0,087 | ATR in frazione del prezzo |

### Variazioni normalizzate per volatilità

| Colonna | Copertura | p1 / mediana / p99 | Cos'è |
|---|---|---|---|
| `var_5g_in_atr` | 99,5 % | **−4,00** / 0 / +4,26 | Variazione a 5 giorni divisa per l'ATR: "ha perso quattro volte la sua escursione tipica" |
| `var_20g_in_atr` | 99,2 % | −8,66 / 0 / +7,56 | Idem su 20 giorni |

**Perché contano.** Una soglia percentuale uguale per tutti ("è sceso del 10 %") pesca
sistematicamente i titoli più volatili, che sono anche i più illiquidi. È il meccanismo
che nel progetto precedente aveva prodotto, sull'S&P 500, beta 1,61 e alpha *negativo*:
la strategia sembrava battere il mercato ma stava solo comprando leva. Con queste
colonne una utility e una microcap diventano confrontabili.

### Eventi di seduta

| Colonna | Copertura | p1 / mediana / p99 | Cos'è |
|---|---|---|---|
| `gap_apertura` | 100 % | −0,042 / 0 / +0,043 | Apertura contro chiusura precedente. I crolli su notizia arrivano come salto: dentro `var_1g` si confondono col movimento intraday |
| `giorni_rossi_consecutivi` | 100 % | 0 / 0 / 5 | Sedute negative di fila, 0 se l'ultima è positiva |

### Liquidità

Il gruppo che non esisteva nella versione FTSE MIB, e il più importante di tutti su
questo universo.

| Colonna | Copertura | p1 / mediana / p99 | Cos'è |
|---|---|---|---|
| `controvalore_medio_20g` | 99,7 % | 0 / **109.800** / 2,42e8 | **Mediana** (non media) del controvalore giornaliero in euro su 20 sedute. Calcolato su `chiusura`, mai su `chiusura_agg` |
| `sedute_scambiate_20g` | 99,7 % | 0 / 20 / 20 | Quante delle ultime 20 sedute hanno avuto volume > 0 |
| `volume_relativo` | 98,4 % | 0 / 0,75 / 6,33 | Volume del giorno diviso la media a 20 giorni |

**Perché contano.** Su 40 blue chip la liquidità non era un problema; qui **154 titoli
su 404 scambiano meno di 10.000 € al giorno** e 152 hanno almeno il 10 % delle sedute a
volume zero. Un prezzo fermo produce **finto ritorno alla media**: la "caduta" del
giorno X è spesso la stampa arretrata di una discesa già avvenuta, e il "recupero" del
giorno dopo è meccanico. È il rischio numero uno di qualunque analisi su questo
dataset. La mediana invece della media è deliberata: per un titolo che scambia cinque
giorni su venti la mediana è 0, che è la risposta giusta sull'investibilità.

### Dividendi

| Colonna | Copertura | p1 / mediana / p99 | Cos'è |
|---|---|---|---|
| `dividendo_12m` | 100 % | 0 / 0,03 / 1,99 | Somma dei dividendi staccati nelle ultime 252 sedute, in euro |
| `rendimento_dividendo_12m` | 100 % | 0 / 0,008 / **0,131** | `dividendo_12m / chiusura` |
| `variazione_dividendo` | **50,3 %** | −1 / +0,013 / +4 | Confronto coi 12 mesi precedenti. Vuoto dove non c'era dividendo un anno prima |

**Perché contano.** È l'unico segnale quasi-fondamentale ottenibile onestamente su
tutta la storia: il dividendo è noto nel momento in cui viene pagato, quindi zero
look-ahead. Discrimina ciò che a una regola puramente tecnica manca — *"sceso del 10 %
ma rende il 7 %"* contro *"sceso del 10 % e non paga nulla"* — che su Milano è la
differenza fra le due metà del listino. `variazione_dividendo` a −1 significa dividendo
azzerato: un segnale di difficoltà molto più tempestivo di qualunque bilancio.

### Eventi societari e anzianità

| Colonna | Copertura | p1 / mediana / p99 | Cos'è |
|---|---|---|---|
| `raggruppamento_24m` | 100 % | 0 / 0 / 1 | Numero di split inversi negli ultimi 24 mesi |
| `sedute_di_storia` | 100 % | **29** / 1.854 / 6.435 | Sedute disponibili per quel titolo fino a quel giorno |

Un raggruppamento azionario è uno dei migliori indicatori gratuiti di difficoltà: lo si
fa quando il prezzo è crollato sotto la soglia di decenza. Nel dataset **55 titoli** ne
hanno fatto uno negli ultimi 24 mesi, ed è esattamente la popolazione che una strategia
"compra il ribasso" raccoglie con entusiasmo.

`sedute_di_storia` serve alla regola di stagionatura: i primi 6-12 mesi dopo la
quotazione sono un regime di prezzo diverso — stabilizzazione del collocatore nei primi
30 giorni, scadenza del lock-up fra 6 e 12 mesi, flottante minimo, nessuna copertura di
analisti — non un prezzo di mercato. Il 1° percentile a 29 dice che nel pannello ci sono
davvero righe di titoli quotati da poche settimane: vanno filtrate.

### Confronto trasversale

Colonne che confrontano i titoli **fra loro dentro la stessa giornata**.

| Colonna | Copertura | Cos'è |
|---|---|---|
| `eleggibile` | 100 % | 1 se il titolo quel giorno ha ≥ 252 sedute di storia, ≥ 50.000 € di controvalore e ha scambiato. **55,2 % delle righe** |
| `pct_var_5g` | 55,2 % | Percentile di `var_5g` dentro la giornata: 0 = il più basso del listino, 1 = il più alto |
| `pct_var_20g` | 55,2 % | Idem su 20 giorni |
| `pct_dal_massimo_52s` | 55,2 % | Idem sulla distanza dal massimo |
| `pct_var_5g_in_atr` | 55,2 % | Idem sulla caduta normalizzata per volatilità |
| `pct_rendimento_dividendo_12m` | 55,2 % | Idem sul rendimento da dividendo |
| `var_5g_rel` | 99,0 % | `var_5g` meno la mediana dei titoli eleggibili quel giorno |
| `var_20g_rel` | 98,5 % | Idem su 20 giorni |

I `pct_*` sono definiti **solo sulle righe eleggibili**: includere i titoli fermi
falserebbe i percentili, perché un prezzo che non si muove finirebbe sempre a metà
classifica spostando tutti gli altri. La soglia di eleggibilità usata qui (50.000 €) è
volutamente più larga di quella operativa consigliata (100.000 €).

I `var_*_rel` invece sono definiti su **tutte** le righe: la mediana è una sola per
giornata, e servono anche sui titoli illiquidi, che sono l'oggetto del test di
robustezza. La copertura non arriva al 100 % perché in 9.669 righe — di cui 7.030 nel
solo 2000, più una coda fino al 2005 — **nessun titolo era eleggibile quel giorno**, e
senza titoli eleggibili la mediana trasversale non esiste. È lo stesso problema
dell'ampiezza dell'universo visto da un'altra angolatura: all'inizio del campione non
c'è abbastanza mercato per fare un confronto trasversale.

Il confronto usa la mediana dei titoli eleggibili e **non** l'indice, perché
`FTSEMIB.MI` è un indice *price* mentre i nostri prezzi sono total return: sottrarlo
introdurrebbe una deriva sistematica di circa 2,4 punti l'anno a favore dei titoli.

**Perché contano.** Con 400 titoli, in una giornata di panico "sceso del 10 %" scatta su
decine di titoli insieme e la soglia fissa non seleziona più nulla: quello che serve è
sapere quanto è estremo il movimento rispetto agli altri.

### Mercato

| Colonna | Copertura | p1 / mediana / p99 | Cos'è |
|---|---|---|---|
| `var_mercato` | 99,8 % | −0,040 / +0,001 / +0,035 | Variazione giornaliera di `FTSEMIB.MI` |
| `beta_252g` | 95,7 % | −0,117 / **0,479** / 1,531 | Beta mobile su 252 sedute contro l'indice |

Fra le sole righe eleggibili il beta ha mediana **0,67** e 99° percentile **1,61**, che
sono i valori attesi. Da confrontare con il campo `beta` di `.info` di Yahoo, che per
ENI dà **0,245**: implausibile, e da non usare.

Il beta è calcolato contro un indice *price*, mentre i titoli sono total return.
L'effetto sul beta è di secondo ordine, ma va dichiarato.

---

## Copertura nel tempo

L'aspetto più importante da capire prima di usare il dataset.

| Anno | Righe | Titoli |
|---|---|---|
| 2000 | 7.474 | 37 |
| 2005 | 22.965 | 93 |
| 2010 | 30.080 | 122 |
| 2015 | 39.905 | 166 |
| 2020 | 60.978 | 246 |
| 2023 | 84.102 | 350 |
| 2026 | 62.863 | 404 |

Applicando i filtri di investibilità consigliati:

| Periodo | Titoli investibili al giorno (mediana) |
|---|---|
| 2000-2007 | 49 |
| 2008-2014 | 65 |
| 2015-2020 | 103 |
| 2021-2026 | 135 |

**Fino al 2014 l'universo investibile è appena più grande del FTSE MIB.** Il dataset fa
davvero quello per cui è nato solo dal 2015. Un backtest 2000-2026 riportato come numero
unico sarebbe dominato dagli ultimi anni e i sotto-periodi non sarebbero confrontabili:
il risultato principale va riportato **dal 2015**, col periodo precedente come
sotto-periodo separato.

La regola "caduta ≥ 10 % in 5 sedute" produce **14.423 segnali** nell'universo
investibile, contro i 5.052 del dataset FTSE MIB.

---

## Pulizia applicata

L'aggiustamento retroattivo di Yahoo degenera sui titoli con raggruppamenti azionari e
aumenti di capitale ripetuti — le banche e le small cap italiane sono il caso da
manuale. `BES.MI` arriva a un prezzo aggiustato di **−900.400 €** nel 2000 e di 43
milioni nel 2002, con ultimo prezzo 0,058. `UCG.MI` nel 2000 ha un fattore di
aggiustamento di **1091**, quando per costruzione non può superare 1.

Quattro criteri di corruzione **sistematica**, che tagliano tutta la storia precedente
all'ultima occorrenza:

1. un prezzo ≤ 0, che non esiste;
2. `fattore_dividendi` > 1: i dividendi *abbassano* i prezzi storici;
3. un prezzo oltre 1000 volte l'ultimo — la cascata prosegue anche dove nessun prezzo è
   negativo (su `BES.MI` il rapporto massimo/ultimo vale 748 milioni);
4. un salto giornaliero oltre 2,5x senza uno split registrato a fianco: un'operazione
   sul capitale che Yahoo non ha annotato. Sette casi, ma ognuno avvelenava i 252 giorni
   di finestra mobile successivi — `OPS.MI` ha un +24.900 % il 2024-02-01 che produceva
   volatilità implicita dell'88.000 % e beta −34 per un anno intero.

Più due criteri che buttano **singole righe**: la chiusura aggiustata fuori dal range
minimo-massimo della seduta (se capita su meno dell'1 % delle righe è un errore isolato
di Yahoo — ENI ne ha 2 su 6.800 — altrimenti la serie è compromessa e si taglia), e le
**stampe fantasma**, cioè un prezzo che si muove di oltre il 20 % in una seduta a volume
zero: senza scambi non c'è prezzo, quindi la riga è inventata.

Risultato sull'ultimo giro: **57 serie accorciate** (14 % dei titoli, quasi tutte banche
e small cap), 182 righe isolate buttate, 1 ticker scartato (`PINF.MI`, 76 sedute sane).
Il dettaglio è in `dati/log_costruzione.txt`.

### Residui noti

Restano 2 salti fra 1,5x e 2,5x (`BAN.MI` 2002, `FDA.MI` 2026) e 40 righe con
volatilità sopra il 500 %, su 1,15 milioni di righe. Non sono stati eliminati
abbassando la soglia perché almeno uno è **reale**: FIDIA (`FDA.MI`) quota 0,008 €, dove
il tick minimo da 0,001 vale il 12 % del prezzo, e un +177 % è una stampa vera. Il
filtro sul prezzo minimo di 0,10 € li esclude tutti dall'universo investibile: è il
posto giusto per gestirli, non la pulizia dei dati.

---

## Cosa NON fare con questo dataset

**Non usare `chiusura_agg` per il controvalore.** Il volume Yahoo è aggiustato per gli
split ma non per i dividendi. Il controvalore di ENI nel 2000 risulterebbe 4,4 volte
più basso del vero, e un filtro "almeno 100.000 € al giorno" diventerebbe di fatto
440.000 nel 2000 e 100.000 oggi: l'universo si restringerebbe da solo andando indietro,
in modo invisibile. La colonna `controvalore_medio_20g` è già calcolata bene.

**Non usare `chiusura` per i rendimenti.** Non contiene i dividendi. Su Milano, dove i
rendimenti da dividendo arrivano al 13 % (99° percentile), significa buttare via la
maggior parte del rendimento — e reintrodurre i falsi crolli nei giorni di stacco.

**Non filtrare per prezzo minimo a 1,00 €.** Su Milano **90 titoli su 404 quotano sotto
1 €**, fra cui TESMEC (1,6 M€/giorno di scambi), CIR, RCS Mediagroup, IMMSI e GEOX. Il
prezzo unitario basso non dice niente sulla qualità: il proxy giusto di investibilità è
il controvalore. La soglia a 0,10 € serve solo a escludere i casi dove il tick è già
l'1 % del prezzo.

**Non trattare l'universo come point-in-time.** È il listino di **oggi**. Chi è stato
delistato per fallimento o OPA manca, e su un mercato dove metà dell'universo è
microcap dell'ex-AIM il delisting è la norma. Il survivorship bias è forte, e
**cambia segno lungo il campione**: le coorti vecchie sono sopravvissuti che
sovrastimano il rendimento, mentre le IPO recenti hanno survivorship bias quasi nullo
ma vengono comprate *preferenzialmente* da una strategia mean-reversion. Due bias in
direzioni opposte non si correggono nemmeno di segno.

**Non usare `FTSEMIB.MI` come benchmark di rendimento.** È un indice *price*: zero
dividendi in 7.050 sedute, mentre i prezzi dei titoli sono total return. Misurato:
FTSE MIB price **+1,77 %** annuo dal 2008 contro ETF FTSE MIB total return
(`IMIB.MI`) **+4,16 %** — 2,4 punti l'anno regalati alla strategia, e sono al netto del
TER dell'ETF. Serve comunque anche un benchmark equipesato costruito sugli stessi
titoli, perché l'ETF non ha survivorship bias mentre il nostro universo sì.

**Non usare le colonne di `dati/anagrafica.csv` come segnale.** Sono la fotografia di
oggi, senza storia: `capitalizzazione`, `spread_pct`, `flottante` nel 2005 non erano
quelli del 2026. Servono per calibrare i costi, per neutralizzare il settore e per
diagnostica. Stanno in un file separato proprio perché un dato-fotografia non possa
finire per sbaglio in una colonna del pannello. Le sole ragionevolmente stabili sono
`settore` e `industria` (402 titoli su 406 coperti).

**Non ignorare i costi di transazione.** Misurati su tutti i 406 titoli, lo spread
denaro-lettera va dallo 0,05 % dei 42 titoli sopra i 10 M€/giorno al **3,21 %** mediano
(16,6 % al 90° percentile) dei 154 sotto i 10.000 €/giorno. Con un take profit del
10 %, nella fascia 10-50k il giro completo si mangia il 19 % del guadagno, e nella
fascia sotto i 10k il titolo mediano se lo mangia per un terzo. Una commissione piatta uguale per tutti è sbagliata per metà
dell'universo: `anagrafica.costo_per_fascia()` produce la tabella di calibrazione.

## Cosa il dataset non contiene, e perché

Verificato campo per campo su Yahoo, non assunto:

| Dato | Perché non c'è |
|---|---|
| Capitalizzazione storica | `get_shares_full` parte dal 2015 per ENI, dal 2009 per A2A, dal **2024** per le microcap. Su 26 anni è inutilizzabile: niente fattore size onesto |
| Bilanci | `financials`/`balance_sheet` danno 3-5 annualità e senza data di pubblicazione affidabile: look-ahead garantito |
| Multipli (P/E, P/B) | Solo il valore odierno in `.info`. Usarli storicamente è look-ahead puro |
| Giudizi degli analisti | `upgrades_downgrades` è vuoto per i titoli non americani |
| Date degli utili | `earnings_dates` copre circa l'ultimo anno |
| Volatilità implicita | La catena delle opzioni non ha storia |
| Barre intraday | 1 ora solo per gli ultimi 730 giorni, 1 minuto per gli ultimi 7-30 |
