# Borsa Italiana — dataset tecnico su tutto il listino

Dataset di prezzi e indicatori tecnici su **tutte le azioni quotate a Milano**, non
solo sui 40 titoli del FTSE MIB: 408 azioni fra Euronext Milan e Euronext Growth
Milan, dal 2000 a oggi.

Nasce come continuazione di un progetto sul FTSE MIB, dove la strategia "compra chi
è sceso del 10% in 5 giorni" si era rivelata priva di alpha statisticamente
significativo, in parte perché su 40 titoli i segnali sono troppo rari. L'ipotesi
da verificare qui è se **dieci volte più titoli** producano abbastanza segnali per
distinguere il caso dall'effetto — sapendo che l'universo largo porta con sé
problemi che su 40 blue chip non esistevano.

## I tre passaggi

```bash
python universo.py                 # l'elenco dei quotati, da Euronext
python costruisci_dataset.py       # scarico prezzi + calcolo indicatori
python anagrafica.py               # settore e spread denaro-lettera (fotografia di oggi)
```

## Dove si prende l'elenco dei quotati

Non da Wikipedia — che elenca solo i costituenti degli indici — ma dal product
directory di Euronext, che espone un gateway JSON:

```
POST https://live.euronext.com/en/product_directory/data/stocks-all-places?mics=MTAA,EXGM
```

| MIC | Mercato | Righe |
|---|---|---|
| `MTAA` | Euronext Milan (segmento STAR compreso) | 204 |
| `EXGM` | Euronext Growth Milan (ex AIM Italia) | 242 |

**Da non includere**, anche se la stessa lista li offre: `MTAH` (Trading After
Hours — sono azioni *estere* scambiate a Milano fuori orario: 3M, 3D Systems),
`ETLX` (EuroTLX, in massima parte obbligazioni) e `MERK`, che è il Merkur Market
di **Oslo** e non di Milano: sta nella stessa lista con 87 righe di ISIN norvegesi.

Dei 446 titoli, 38 sono **warrant** (`W FINCANTIERI 24-26`) e vengono scartati per
il nome. Il pattern deve restare stretto: una versione più larga classificava come
warrant anche `WEBUILD RSP`, che sono le azioni di risparmio di Webuild. Restano
**408 azioni**, di cui 406 con prezzi su Yahoo.

Il simbolo Euronext più il suffisso `.MI` **è** il ticker Yahoo: verificato sui 40
del FTSE MIB, 40 corrispondenze su 40, compresi i casi non ovvi (`STMMI`, `STLAM`,
`TIT`, `PST`). Nessuna tabella di traduzione da mantenere.

## Cosa si scarica, e perché non con `auto_adjust=True`

`auto_adjust=True` restituisce un solo prezzo, aggiustato per split **e** dividendi,
e butta il resto. Sembra comodo ma nasconde due problemi.

**1. Il controvalore risulta sbagliato andando indietro nel tempo.** Il volume che
Yahoo restituisce è aggiustato per gli split ma non per i dividendi (verificato: il
volume di NVDA prima dello split 10:1 è ~400M invece dei ~40M reali, e il
controvalore resta continuo attraverso lo split). Moltiplicare il prezzo
*aggiustato per i dividendi* per quel volume sottostima il controvalore esattamente
del fattore di aggiustamento cumulato, che su Milano è enorme:

| ticker | 2000 | 2005 | 2010 | 2015 | 2020 | 2026 |
|---|---|---|---|---|---|---|
| ENI.MI | 0,225 | 0,261 | 0,349 | 0,489 | 0,654 | 0,978 |
| ISP.MI | 0,192 | 0,312 | 0,389 | 0,470 | 0,643 | 0,967 |
| A2A.MI | 0,329 | 0,362 | 0,444 | 0,578 | 0,696 | 0,954 |

Il controvalore di ENI nel 2000 risulterebbe **4,4 volte più basso** del vero. Un
filtro "almeno 100.000 € al giorno" diventerebbe di fatto 440.000 nel 2000 e
100.000 oggi: l'universo si restringerebbe da solo andando indietro, in modo
invisibile. Il controvalore va calcolato sulla chiusura **grezza**.

**2. La serie dei dividendi e degli split non arriva.** Sono le due sole
informazioni quasi-fondamentali che Yahoo dà su tutta la storia e senza look-ahead.

Quindi si scarica con `auto_adjust=False, actions=True` e si tengono otto colonne:

| Colonna | Cos'è |
|---|---|
| `chiusura_agg` | Adj Close: split + dividendi. Base di **tutti** i rendimenti |
| `chiusura` | Close: aggiustata per gli split, non per i dividendi. Per controvalore, prezzo reale, tick |
| `apertura`, `massimo`, `minimo` | riscalati col rapporto Adj/Close, per restare coerenti coi rendimenti |
| `volume`, `dividendo`, `split` | |

## Le serie corrotte

L'aggiustamento retroattivo di Yahoo degenera sui titoli con raggruppamenti
azionari e aumenti di capitale ripetuti — le banche e le small cap italiane sono il
caso da manuale. Non è un dettaglio marginale: `BES.MI` arriva a un prezzo
aggiustato di **−900.400 €** nel 2000 e di 43 milioni nel 2002, con ultimo prezzo
0,058; `UCG.MI` nel 2000 ha un fattore di aggiustamento di **1091**, quando per
costruzione non può superare 1.

`prezzi.sanifica()` applica tre criteri di corruzione **sistematica** — prezzo ≤ 0,
fattore di aggiustamento > 1, prezzo oltre 1000 volte l'ultimo — e taglia tutta la
storia precedente all'ultima occorrenza. Il terzo criterio serve perché la cascata
prosegue anche dove nessun prezzo è negativo.

C'è poi un criterio di **incoerenza interna**: la chiusura aggiustata deve stare fra
minimo e massimo della seduta. Se capita su meno dell'1% delle righe è un errore
isolato di Yahoo e si buttano solo quelle righe (ENI ne ha 2 su 6.800: tagliare
tutta la storia precedente sarebbe assurdo); se capita più spesso la serie è
compromessa (`BES.MI`: 277 righe) e si taglia.

Restano due criteri di corruzione **non** sistematica, aggiunti dopo aver visto i
danni che facevano: un salto giornaliero oltre 2,5x senza uno split registrato a
fianco (7 casi, operazioni sul capitale che Yahoo non ha annotato — `OPS.MI` ha un
+24.900% il 2024-02-01 che produceva volatilità implicita dell'88.000% e beta −34 per
un anno intero), e le **stampe fantasma**, cioè un prezzo che si muove di oltre il 20%
in una seduta a volume zero: senza scambi non c'è prezzo, quindi la riga è inventata e
si butta (182 righe, 60 delle quali su `EPH.MI`).

### Residui noti

Dopo la pulizia restano 2 salti fra 1,5x e 2,5x (`BAN.MI` 2002, `FDA.MI` 2026) e 40
righe con volatilità sopra il 500% su tre ticker, su 1,15 milioni di righe. Non sono
stati eliminati abbassando la soglia perché almeno uno è **reale**: FIDIA (`FDA.MI`)
quota 0,008 €, dove il tick minimo da 0,001 vale il 12% del prezzo, e un +177% è una
stampa vera. Il filtro sul prezzo minimo di 0,10 € li esclude tutti dall'universo
investibile: è il posto giusto per gestirli, non la pulizia dei dati.

## Gli indicatori del pannello

Una riga per (data, ticker). Tutti usano **solo dati fino a quel giorno**.

| Gruppo | Colonne |
|---|---|
| Variazioni | `var_1g`, `var_5g`, `var_20g`, `var_60g`, `var_252g` |
| Estremi 52s | `massimo_52s`, `minimo_52s`, `dal_massimo_52s`, `dal_minimo_52s` |
| Trend | `dist_sma20`, `dist_sma50`, `dist_sma200` |
| Oscillatori | `rsi_14`, `rsi_2`, `zscore_20g` |
| Rischio | `volatilita_20g`, `volatilita_60g`, `rapporto_volatilita`, `escursione_media_20g`, `atr_14`, `atr_pct_14` |
| **Normalizzati** | `var_5g_in_atr`, `var_20g_in_atr` |
| Eventi | `gap_apertura`, `giorni_rossi_consecutivi` |
| **Liquidità** | `controvalore_medio_20g`, `sedute_scambiate_20g`, `volume_relativo` |
| **Dividendi** | `dividendo_12m`, `rendimento_dividendo_12m`, `variazione_dividendo` |
| **Eventi societari** | `raggruppamento_24m` |
| **Anzianità** | `sedute_di_storia` |
| **Trasversali** | `eleggibile`, `pct_var_5g`, `pct_var_20g`, `pct_dal_massimo_52s`, `pct_var_5g_in_atr`, `pct_rendimento_dividendo_12m`, `var_5g_rel`, `var_20g_rel` |
| Mercato | `var_mercato`, `beta_252g` |

In grassetto le colonne nuove rispetto alla versione FTSE MIB. Sono tutte imposte
dal passaggio da 40 a 400 titoli:

**Liquidità.** Su 40 blue chip non era un problema; su 400 titoli 108 scambiano
meno di 10.000 € al giorno e 135 hanno almeno il 10% delle sedute a volume zero. Un
prezzo fermo produce **finto ritorno alla media**: la "caduta" del giorno X è spesso
la stampa arretrata di una discesa già avvenuta, e il "recupero" del giorno dopo è
meccanico. È il rischio numero uno di tutto l'esercizio.

**Normalizzazione per volatilità.** Una soglia percentuale uguale per tutti pesca
sempre i titoli più volatili, che sono anche i più illiquidi: è il meccanismo che
sul S&P 500 aveva prodotto beta 1,61 e alpha **negativo**. `var_5g_in_atr` misura la
caduta in unità di volatilità del titolo, così una utility e una microcap sono
confrontabili.

**Confronto trasversale.** Con 400 titoli, in una giornata di panico "sceso del 10%"
scatta su decine di titoli insieme e la soglia fissa non seleziona più nulla. I
`pct_*` dicono quanto è estremo il movimento *rispetto agli altri titoli di quel
giorno*. Il confronto usa la mediana dei titoli eleggibili e non l'indice, perché
`FTSEMIB.MI` è un price index mentre i nostri prezzi sono total return: sottrarlo
introdurrebbe una deriva sistematica di circa 2,4 punti l'anno.

**Dividendi.** L'unico segnale quasi-fondamentale ottenibile onestamente su tutta la
storia: il dividendo è noto quando viene staccato, quindi zero look-ahead. Discrimina
ciò che alla regola "caduta" manca: *"sceso del 10% ma rende il 7%"* da *"sceso del
10% e non paga nulla"* — che su Milano è la differenza fra le due metà del listino.

**Raggruppamenti.** Uno split inverso è uno dei migliori indicatori gratuiti di
difficoltà: lo si fa quando il prezzo è crollato sotto la soglia di decenza. Ed è
esattamente la popolazione che "compra il ribasso" raccoglie con entusiasmo.

## Il pannello non è filtrato

Contiene anche i titoli illiquidi e le neoquotate. I filtri di eleggibilità si
applicano **giorno per giorno** quando si generano i segnali, così si possono
cambiare senza riscaricare e si può *misurare* quanto contano. Filtrare la lista a
monte sarebbe una scelta di selezione irreversibile.

I filtri consigliati, e il perché:

Applicando i filtri consigliati qui sotto, l'universo investibile risulta:

| Periodo | Titoli/giorno (mediana) |
|---|---|
| 2000-2007 | 49 |
| 2008-2014 | 65 |
| 2015-2020 | 103 |
| 2021-2026 | 135 |

Cioè fino al 2014 l'universo investibile è appena più grande del FTSE MIB, e solo dal
2015 il progetto fa davvero quello per cui è nato. La regola "caduta ≥10% in 5 sedute"
produce **14.423 segnali** contro i 5.052 del dataset FTSE MIB.

| Filtro | Valore | Motivo |
|---|---|---|
| `sedute_di_storia ≥ 252` | un anno | Stagionatura post-IPO. I primi 6-12 mesi sono un regime di prezzo diverso: stabilizzazione del collocatore, scadenza del lock-up, flottante minimo. Copre anche il warm-up degli indicatori |
| `controvalore_medio_20g ≥ 100.000` | € al giorno | Lascia ~149 titoli oggi. Sotto, lo spread mangia l'edge |
| `sedute_scambiate_20g ≥ 18` | su 20 | Elimina l'artefatto dei prezzi fermi |
| prezzo minimo | 0,10 € — **non 1,00** | Su Milano 92 titoli su 389 quotano sotto 1 €, fra cui TESMEC (1,6 M€/giorno), CIR, RCS, IMMSI, GEOX. Il prezzo unitario basso non dice niente sulla qualità: il proxy giusto è il controvalore. La soglia a 0,10 serve solo a escludere i casi dove il tick è già l'1% del prezzo |

## I costi di transazione

Spread denaro-lettera misurato su tutti i 406 titoli (`anagrafica.py`):

| Controvalore mediano/giorno | Titoli | Spread mediano | 90° percentile |
|---|---|---|---|
| > 10 M | 42 | 0,05 % | 0,10 % |
| 1 – 10 M | 46 | 0,19 % | 0,36 % |
| 200k – 1 M | 42 | 0,43 % | 0,80 % |
| 50k – 200k | 40 | 0,57 % | 1,68 % |
| 10k – 50k | 80 | 1,86 % | 4,52 % |
| < 10k | 110 | **2,84 %** | **8,43 %** |

Casi estremi nella stessa borsa: ENI 0,05 %, CLABO **34 %** (denaro 0,95 / lettera
1,34). Con un take profit del 10%, nella fascia 10-50k il giro completo si mangia il
19% del guadagno e per un titolo su dieci il 45%; sotto i 10k non è replicabile
affatto. **190 titoli su 406 stanno sotto i 50.000 € al giorno.** Una commissione
piatta uguale per tutti è sbagliata per metà dell'universo:
`anagrafica.costo_per_fascia()` produce la tabella di calibrazione.

## Il benchmark

`FTSEMIB.MI` è un indice **price**: zero dividendi in 7.050 sedute. I prezzi dei
titoli sono total return, quindi confrontarli è un regalo alla strategia. Gli ETF sul
MIB hanno invece la storia degli stacchi, quindi il loro Adj Close *è* un total
return vero: `IMIB.MI`, `ETFMIB.MI`, `XMIB.MI`, dal 2008.

```
FTSE MIB price index         +1,77% annuo   dal 2008-01-02
ETF FTSE MIB total return    +4,16% annuo   dal 2008-01-02
```

**2,4 punti l'anno** di differenza, e sono al netto del TER dell'ETF. Serve comunque
anche un benchmark equipesato costruito sugli stessi titoli della strategia, perché
l'ETF non ha survivorship bias mentre il nostro universo sì.

## Limiti dichiarati

**Survivorship bias, e peggiora rispetto al FTSE MIB.** La lista è il listino di
*oggi*: chi è stato delistato per fallimento o OPA manca. Su 408 titoli di cui metà
microcap dell'ex-AIM — mercato dove il delisting è la norma — è una selezione di
sopravvissuti molto forte.

**Il bias cambia segno lungo il campione.** Un titolo quotato nel 2000 e ancora in
listino ha superato 26 anni di selezione: le coorti vecchie sono sopravvissuti e
sovrastimano il rendimento. Le IPO recenti hanno survivorship bias quasi nullo ma
portano il bias opposto, perché una strategia mean-reversion le compra
*preferenzialmente*. Le due metà sono distorte in direzioni **opposte**, e questo è
peggio di un bias uniforme: non si può correggere il risultato nemmeno di segno.

**L'ampiezza dell'universo cresce nel tempo.** Solo 115 titoli esistono dal 2000, e
sono i big cap; il grosso entra dopo il 2017. Un backtest dal 2000 è di fatto un
backtest sul MIB fino al 2015 e su tutto il listino solo dopo. Il risultato
principale va riportato **dal 2015**, con il periodo precedente come sotto-periodo
separato. `costruisci_dataset.py` stampa la mediana annuale dei titoli eleggibili
per giorno proprio per rendere visibile questo effetto.

**Niente fondamentali.** Verificato uno per uno: `get_shares_full` (capitalizzazione
point-in-time) parte dal 2015 per ENI e dal **2024** per le microcap;
`financials`/`balance_sheet` danno 3-5 annualità senza data di pubblicazione
affidabile, quindi look-ahead garantito; `marketCap`, `trailingPE`, `priceToBook`
di `.info` sono solo il valore odierno; `upgrades_downgrades` è vuoto sui non-USA;
`earnings_dates` copre l'ultimo anno; la catena delle opzioni non ha storia. Niente
fattore size onesto e niente multipli.

**Il `beta` di `.info` non va usato**: Yahoo dà 0,245 per ENI, implausibile. Il
`beta_252g` calcolato dai prezzi dà 0,7-1,1 lungo la storia, e 1,0-1,7 per Intesa
(banca), che sono i valori attesi.
