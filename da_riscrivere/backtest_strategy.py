#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
backtest_strategy.py — backtest della strategia DCA + Take Profit / Stop Loss.

REGOLE SIMULATE
---------------
1. Il primo mese si versa `--capitale-iniziale` euro.
2. Ogni mese successivo, il primo giorno di borsa, si versano
   `--versamento-mensile` euro che si sommano alla liquidita' rientrata dalle
   vendite.
3. Tutta la liquidita' disponibile viene divisa in `--n-titoli` parti uguali e
   investita nei titoli piu' raccomandati di quel mese (dataset
   `raccomandazioni_storiche.csv`). Dopo gli acquisti la liquidita' e' zero.
4. Un titolo gia' in portafoglio viene saltato: si scorre la classifica
   (11-esimo, 12-esimo, ...) finche' non si riempiono gli slot. Mai due
   posizioni aperte sullo stesso titolo.
5. Ogni giorno si controlla ciascuna posizione:
   - guadagno >= `--take-profit` per cento  -> si vende;
   - perdita  >= `--stop-loss` per cento    -> si vende (se lo stop e' attivo).
   Il ricavato torna in liquidita' e sara' reinvestito il mese successivo.

Le serie dei prezzi sono aggiustate per split e dividendi (total return).
I versamenti sono in euro e i titoli quotano in dollari: il cambio EUR/USD e'
applicato di default (`--valuta EUR`).

USO
---
    python backtest_strategy.py --take-profit 10 --stop-loss 15
    python backtest_strategy.py --sweep --take-profit-grid 5,10,15,20 \
                                --stop-loss-grid none,10,20,30
"""

from __future__ import annotations

import argparse
import logging
import math
import os
import sys
from dataclasses import dataclass, field
from typing import Dict, List, Optional, Sequence, Tuple

import pandas as pd

import metriche as mt
import price_data

LOGGER = logging.getLogger("backtest")

GIORNI_ANNO = 365.25

# Tolleranza sulle soglie: 100 * 1.1 in virgola mobile fa 110.00000000000001,
# quindi un titolo salito esattamente del 10% non farebbe scattare il confronto.
EPS = 1e-9


# ---------------------------------------------------------------------------
# Configurazione
# ---------------------------------------------------------------------------


@dataclass
class Parametri:
    """Parametri della simulazione. X = take_profit, Y = stop_loss."""

    take_profit: Optional[float] = 10.0       # X, in percentuale; None = non si vende in guadagno
    stop_loss: Optional[float] = None         # Y, in percentuale; None = si tiene
    capitale_iniziale: float = 1000.0
    versamento_mensile: float = 1000.0
    n_titoli: int = 10
    # "liquidita" = ogni mese la cassa si divide in n_titoli parti (regola
    # originale); "patrimonio" = ogni posizione vale 1/n_titoli del portafoglio,
    # regola sensata quando i segnali arrivano pochi per volta.
    dimensione_posizione: str = "liquidita"
    # Finestra e tetto per il dimensionamento sulla volatilita'.
    vol_finestra: int = 60
    vol_limite: float = 3.0
    # Se True lo stesso titolo puo' essere comprato di nuovo mentre una
    # posizione e' gia' aperta: si accumulano piu' "pacchetti" indipendenti,
    # ciascuno col proprio prezzo di carico e la propria soglia.
    consenti_riacquisto: bool = False
    max_lotti_per_titolo: int = 3
    # Frazione del pacchetto venduta al raggiungimento del take profit.
    # 100 = si vende tutto (regola originale); 50 = si vende meta' e il resto
    # continua a correre, con il riferimento che sale al prezzo di vendita.
    vendita_parziale_pct: float = 100.0
    # Quante volte un pacchetto puo' essere alleggerito. Con 1, dopo la prima
    # vendita parziale il resto **corre libero**: niente piu' take profit, esce
    # solo con lo stop o a fine periodo. E' il classico "metti al sicuro il
    # capitale e cavalca il resto gratis". 0 = nessun limite (scala continua).
    max_scaglioni: int = 0
    # Come si verifica il raggiungimento delle soglie:
    #   "close"    -> sul prezzo di chiusura giornaliero (conservativo)
    #   "intraday" -> su massimo/minimo di giornata, con gestione dei gap
    esecuzione: str = "close"
    # Z: trailing stop, in percentuale dal massimo raggiunto dopo l'acquisto.
    # Alternativa al take profit fisso: lascia correre i vincenti proteggendo il guadagno.
    trailing_stop: Optional[float] = None
    # "mensile" = il ricavato aspetta il primo del mese (regola originale);
    # "subito"  = viene reinvestito il giorno stesso, senza liquidita' ferma.
    reinvestimento: str = "mensile"
    commissione_pct: float = 0.0              # per operazione, in percentuale
    commissione_fissa: float = 0.0            # per operazione, in euro
    valuta: str = "EUR"                       # EUR = converte da USD; USD = ignora il cambio
    inizio: Optional[str] = None
    fine: Optional[str] = None
    escludi_ticker: Tuple[str, ...] = ()
    # Prelievo annuo per simulare un portafoglio vero, da cui si spende.
    # Applicato una volta l'anno, alla prima data di selezione dell'anno.
    prelievo_annuo_pct: float = 0.0
    prelievo_base: str = "patrimonio"   # patrimonio | versamenti
    # Con segnali giornalieri (es. "compra chi e' sceso del 10%") gli acquisti
    # possono avvenire ogni giorno, ma il versamento resta mensile.
    versamento_solo_mensile: bool = False

    @property
    def soglia_tp(self) -> Optional[float]:
        return None if self.take_profit is None else 1.0 + self.take_profit / 100.0

    @property
    def soglia_sl(self) -> Optional[float]:
        return None if self.stop_loss is None else 1.0 - self.stop_loss / 100.0

    def etichetta(self) -> str:
        tp = "noTP" if self.take_profit is None else f"TP{self.take_profit:g}%"
        sl = "hold" if self.stop_loss is None else f"SL{self.stop_loss:g}%"
        ts = "" if self.trailing_stop is None else f"/TS{self.trailing_stop:g}%"
        rv = "" if self.reinvestimento == "mensile" else "/subito"
        pr = "" if self.prelievo_annuo_pct <= 0 else f"/-{self.prelievo_annuo_pct:g}%anno"
        vp = "" if self.vendita_parziale_pct >= 100 else f"/vendi{self.vendita_parziale_pct:g}%"
        ri = "/riacquisti" if self.consenti_riacquisto else ""
        return f"{tp}/{sl}{ts}{rv}{pr}{vp}{ri}"


@dataclass
class Posizione:
    """Un singolo pacchetto acquistato: ha un suo carico e una sua soglia.

    Con `--consenti-riacquisto` lo stesso ticker puo' avere piu' pacchetti
    aperti insieme, comprati in momenti diversi a prezzi diversi.
    """

    ticker: str
    data_acquisto: pd.Timestamp
    prezzo_acquisto: float      # in valuta di conto, gia' al netto del cambio
    quote: float                # frazionabili
    costo: float                # capitale impiegato, commissioni incluse
    prezzo_massimo: float = 0.0  # serve al trailing stop
    # Riferimento su cui si misurano take profit e stop loss. Coincide col
    # prezzo di acquisto finche' non si vende una frazione: dopo una vendita
    # parziale sale al prezzo incassato, cosi' la parte rimasta punta al
    # gradino successivo invece di ri-scattare ogni giorno.
    riferimento: float = 0.0
    quote_iniziali: float = 0.0
    incassato: float = 0.0      # ricavi delle vendite parziali gia' fatte
    frazioni_vendute: int = 0

    def __post_init__(self) -> None:
        if not self.riferimento:
            self.riferimento = self.prezzo_acquisto
        if not self.quote_iniziali:
            self.quote_iniziali = self.quote

    def valore(self, prezzo: float) -> float:
        return self.quote * prezzo


@dataclass
class Operazione:
    ticker: str
    data_acquisto: pd.Timestamp
    data_vendita: Optional[pd.Timestamp]
    prezzo_acquisto: float
    prezzo_vendita: Optional[float]
    quote: float
    motivo: str                 # take_profit | stop_loss | delisting | fine_periodo
    rendimento: float           # netto commissioni, in frazione (0.10 = +10%)
    giorni: int


@dataclass
class Risultato:
    parametri: Parametri
    equity: pd.DataFrame        # data, valore_totale, investito, liquidita'
    operazioni: pd.DataFrame
    metriche: Dict[str, float] = field(default_factory=dict)


# ---------------------------------------------------------------------------
# Preparazione dei dati
# ---------------------------------------------------------------------------


class DatiMercato:
    """Prezzi giornalieri in valuta di conto, indicizzati per data e ticker."""

    def __init__(self, prezzi: pd.DataFrame, fx: Optional[pd.DataFrame], valuta: str) -> None:
        prezzi = prezzi.copy()
        prezzi["date"] = pd.to_datetime(prezzi["date"]).dt.normalize()

        if valuta.upper() == "EUR":
            if fx is None or fx.empty:
                raise ValueError("Cambio EUR/USD mancante: usa --valuta USD per ignorarlo.")
            cambio = fx.copy()
            cambio["date"] = pd.to_datetime(cambio["date"]).dt.normalize()
            cambio = cambio.set_index("date")["eurusd"].sort_index()
            # I giorni di festivita' locali mancano: si porta avanti l'ultimo cambio noto.
            giorni = pd.date_range(prezzi["date"].min(), prezzi["date"].max(), freq="D")
            cambio = cambio.reindex(giorni).ffill().bfill()
            fattore = prezzi["date"].map(cambio)          # dollari per euro
            for col in ("open", "high", "low", "close"):
                prezzi[col] = prezzi[col] / fattore        # da USD a EUR
            LOGGER.info("Prezzi convertiti in euro (cambio EUR/USD da %.4f a %.4f).",
                        cambio.iloc[0], cambio.iloc[-1])
        elif valuta.upper() == "LOCALE":
            LOGGER.info("Valuta locale: i prezzi sono gia' nella valuta di conto, nessuna conversione.")
        else:
            LOGGER.warning("Valuta USD: il cambio e' ignorato, i versamenti sono trattati come dollari.")

        self.open = prezzi.pivot_table(index="date", columns="ticker", values="open").sort_index()
        self.close = prezzi.pivot_table(index="date", columns="ticker", values="close").sort_index()
        self.high = prezzi.pivot_table(index="date", columns="ticker", values="high").sort_index()
        self.low = prezzi.pivot_table(index="date", columns="ticker", values="low").sort_index()
        if self.close.empty:
            raise ValueError(
                "Nessun prezzo disponibile per i ticker richiesti: controlla i simboli "
                "(le borse non americane vogliono il suffisso, es. ENI.MI, SAP.DE, 7203.T)."
            )
        self.giorni: List[pd.Timestamp] = list(self.close.index)
        self.tickers = set(self.close.columns)
        # Matrici numpy + mappe di indici: con centinaia di posizioni aperte per
        # migliaia di giorni, il costo dei lookup pandas (.at) domina tutto il
        # resto della simulazione. Qui un prezzo costa due lookup di dizionario.
        self._mat = {nome: getattr(self, nome).to_numpy(dtype=float)
                     for nome in ("open", "high", "low", "close")}
        # Volatilita' annualizzata su finestra mobile, calcolata solo con i
        # dati fino a ciascun giorno: serve a dimensionare le posizioni senza
        # guardare avanti. Si costruisce su richiesta, e' costosa in memoria.
        self._vol_cache: Dict[int, pd.DataFrame] = {}
        self._riga = {giorno: i for i, giorno in enumerate(self.close.index)}
        self._col = {ticker: j for j, ticker in enumerate(self.close.columns)}
        ultimi = self.close.notna()[::-1].idxmax()
        self._ultimo_giorno = {t: (ultimi[t] if self.close[t].notna().any() else None)
                               for t in self.close.columns}
        LOGGER.info("Mercato: %d giorni di borsa, %d titoli, %s -> %s",
                    len(self.giorni), len(self.tickers),
                    self.giorni[0].date(), self.giorni[-1].date())

    def prezzo(self, giorno: pd.Timestamp, ticker: str, colonna: str = "close") -> Optional[float]:
        """Un prezzo, per data e ticker. None se il titolo non quotava quel giorno."""
        riga = self._riga.get(giorno)
        colonna_idx = self._col.get(ticker)
        if riga is None or colonna_idx is None:
            return None
        valore = self._mat[colonna][riga, colonna_idx]
        return None if valore != valore else float(valore)   # NaN != NaN

    def volatilita(self, giorno: pd.Timestamp, ticker: str, finestra: int) -> Optional[float]:
        """Volatilita' annualizzata del titolo alla data indicata.

        Usa i rendimenti dei `finestra` giorni **precedenti**: la serie viene
        spostata di un giorno cosi' la seduta in cui si compra non entra nel
        calcolo che decide quanto comprare.
        """
        if finestra not in self._vol_cache:
            rend = self.close.pct_change(fill_method=None)
            self._vol_cache[finestra] = (rend.rolling(finestra, min_periods=finestra // 2)
                                         .std().shift(1) * (252 ** 0.5))
        tabella = self._vol_cache[finestra]
        if ticker not in tabella.columns:
            return None
        try:
            valore = tabella.at[giorno, ticker]
        except KeyError:
            return None
        return None if valore != valore or valore <= 0 else float(valore)

    def ultimo_giorno_valido(self, ticker: str) -> Optional[pd.Timestamp]:
        """Ultimo giorno quotato di un titolo (serve a riconoscere i delisting).

        Precalcolato una volta per ticker: veniva interrogato per ogni posizione
        aperta di ogni giorno, e ricalcolarlo ogni volta costava piu' di tutta
        la simulazione.
        """
        return self._ultimo_giorno.get(ticker)


def carica_raccomandazioni(path: str, inizio: Optional[str], fine: Optional[str],
                           escludi: Sequence[str] = ()) -> pd.DataFrame:
    reco = pd.read_csv(path, parse_dates=["data_articolo"])
    if escludi:
        esclusi = {t.strip().upper() for t in escludi if t.strip()}
        prima = len(reco)
        reco = reco[~reco.ticker.isin(esclusi)]
        LOGGER.info("Esclusi %d ticker su richiesta: %d righe rimosse.", len(esclusi), prima - len(reco))
    if inizio:
        reco = reco[reco.data_articolo >= pd.Timestamp(inizio)]
    if fine:
        reco = reco[reco.data_articolo <= pd.Timestamp(fine)]
    reco = reco.sort_values(["data_articolo", "rank"]).reset_index(drop=True)
    if reco.empty:
        raise ValueError("Nessuna raccomandazione nel periodo richiesto.")
    LOGGER.info("Raccomandazioni: %d righe, %d mesi, %s -> %s",
                len(reco), reco.data_articolo.nunique(),
                reco.data_articolo.min().date(), reco.data_articolo.max().date())
    return reco


def report_qualita(reco: pd.DataFrame, mercato: DatiMercato) -> Dict[str, float]:
    """Segnala cosa il backtest non potra' simulare, prima di simulare.

    Due problemi tipici dei dati gratuiti:
    * raccomandazioni senza prezzo a quella data (titoli poi delistati e
      cancellati da Yahoo): vengono saltate, quindi il backtest non compra i
      casi peggiori -> ottimismo residuo;
    * ticker riciclati: un simbolo che oggi appartiene a un'altra societa'.
      Si riconoscono perche' la serie dei prezzi inizia molto dopo la prima
      raccomandazione.
    """
    coppie = set(zip(reco.ticker, reco.data_articolo))
    tradabili = set()
    for ticker, giorno in coppie:
        if ticker in mercato.close.columns:
            try:
                if not pd.isna(mercato.close.at[giorno, ticker]):
                    tradabili.add((ticker, giorno))
            except KeyError:
                pass
    non_tradabili = coppie - tradabili
    top10 = reco[reco["rank"] <= 10]
    non_top10 = sum(1 for t, g in zip(top10.ticker, top10.data_articolo) if (t, g) in non_tradabili)

    inizio_prezzi = {t: mercato.close[t].dropna().index.min() for t in mercato.close.columns}
    prima_reco = reco.groupby("ticker").data_articolo.min()
    sospetti = [t for t, primo in prima_reco.items()
                if t in inizio_prezzi and pd.notna(inizio_prezzi[t])
                and inizio_prezzi[t] > primo + pd.Timedelta(days=30)]

    LOGGER.info("Qualita' dati: %d raccomandazioni su %d non tradabili (%.1f%%), "
                "di cui %d nei primi 10 rank (%.1f%%).",
                len(non_tradabili), len(coppie), 100 * len(non_tradabili) / max(1, len(coppie)),
                non_top10, 100 * non_top10 / max(1, len(top10)))
    if sospetti:
        LOGGER.warning("Ticker con serie prezzi che inizia dopo la prima raccomandazione "
                       "(possibile simbolo riciclato): %s. Usa --escludi-ticker per toglierli.",
                       ", ".join(sorted(sospetti)))
    return {"non_tradabili": float(len(non_tradabili)), "non_tradabili_top10": float(non_top10)}


def serie_equipesata(mercato: DatiMercato, tickers: Sequence[str]) -> pd.Series:
    """Indice equipesato costruito sugli **stessi** titoli della strategia.

    E' il termine di paragone corretto, perche' elimina due distorsioni che
    rendono ingannevole il confronto con l'indice ufficiale:

    * **i dividendi**: gli indici "price" (^GSPC, FTSEMIB.MI) non li contengono,
      mentre i prezzi dei titoli sono aggiustati e quindi li includono. Il
      confronto regalerebbe alla strategia 2-4 punti l'anno;
    * **l'universo**: la lista dei costituenti e' quella di oggi. Se la strategia
      pesca fra i sopravvissuti, anche il benchmark deve pescare fra gli stessi.

    Si compone la media dei rendimenti giornalieri dei titoli quotati in ciascun
    giorno: cosi' entrate e uscite dall'universo non creano salti artificiali.
    """
    presenti = [t for t in tickers if t in mercato.close.columns]
    if not presenti:
        return pd.Series(dtype=float)
    # fill_method=None: un buco nei prezzi non deve essere colmato all'indietro,
    # altrimenti si inventano rendimenti nei giorni in cui il titolo non quotava.
    rendimenti = mercato.close[presenti].pct_change(fill_method=None)
    # Un titolo che quel giorno non quota semplicemente non entra nella media.
    medi = rendimenti.mean(axis=1, skipna=True).fillna(0.0)
    return 100.0 * (1.0 + medi).cumprod()


def benchmark_dca(mercato: DatiMercato, ticker: str, date_versamento: Sequence[pd.Timestamp],
                  par: Parametri) -> Tuple[Dict[str, float], pd.DataFrame]:
    """Termine di paragone: gli stessi versamenti, tutti sull'indice, mai vendendo.

    Senza questo numero il rendimento della strategia non e' interpretabile:
    battere lo zero e' facile, battere un piano di accumulo sull'indice no.
    """
    if ticker not in mercato.close.columns:
        LOGGER.warning("Benchmark %s non disponibile fra i prezzi.", ticker)
        return {}, pd.DataFrame()
    serie = mercato.close[ticker].dropna()
    quote = 0.0
    versato = 0.0
    valori: List[Dict[str, object]] = []
    date_set = set(date_versamento)
    primo = True
    mesi_versati: Set[Tuple[int, int]] = set()
    for giorno in serie.index:
        if giorno in date_set:
            # Stessa cadenza della strategia: con segnali giornalieri il
            # versamento resta mensile, altrimenti il benchmark riceverebbe
            # 1.000 euro al giorno e il confronto sarebbe privo di senso.
            mese = (giorno.year, giorno.month)
            if par.versamento_solo_mensile and not primo and mese in mesi_versati:
                valori.append({"data": giorno, "totale": quote * float(serie.loc[giorno]),
                               "versato": versato})
                continue
            mesi_versati.add(mese)
            importo = par.capitale_iniziale if primo else par.versamento_mensile
            primo = False
            prezzo = float(serie.loc[giorno])
            commissione = importo * par.commissione_pct / 100.0 + par.commissione_fissa
            quote += max(0.0, importo - commissione) / prezzo
            versato += importo
        valori.append({"data": giorno, "totale": quote * float(serie.loc[giorno]), "versato": versato})
    equity = pd.DataFrame(valori).set_index("data")
    equity["liquidita"] = 0.0
    equity["investito"] = equity["totale"]
    equity["prelevato"] = 0.0
    metriche = calcola_metriche(equity, pd.DataFrame(), 0.0)
    metriche["ticker"] = ticker
    return metriche, equity


# ---------------------------------------------------------------------------
# Motore
# ---------------------------------------------------------------------------


class Backtester:
    def __init__(self, reco: pd.DataFrame, mercato: DatiMercato, par: Parametri) -> None:
        self.reco = reco
        self.mercato = mercato
        self.par = par

    # -- utilita' --------------------------------------------------------

    def _prezzo(self, ticker: str, giorno: pd.Timestamp, colonna: str = "close") -> Optional[float]:
        return self.mercato.prezzo(giorno, ticker, colonna)

    def _commissione(self, controvalore: float) -> float:
        return controvalore * self.par.commissione_pct / 100.0 + self.par.commissione_fissa

    # -- vendite ---------------------------------------------------------

    def _prezzo_di_uscita(self, pos: Posizione, giorno: pd.Timestamp) -> Optional[Tuple[float, str]]:
        """Verifica se la posizione va chiusa oggi e a quale prezzo."""
        chiusura = self._prezzo(pos.ticker, giorno, "close")
        if chiusura is None:
            return None

        soglia = self.par.soglia_tp
        # Esauriti gli scaglioni previsti, il pacchetto non ha piu' un take
        # profit: la parte rimasta corre finche' non interviene lo stop.
        if (self.par.max_scaglioni and pos.frazioni_vendute >= self.par.max_scaglioni):
            soglia = None
        # Le soglie si misurano sul riferimento: dopo una vendita parziale
        # e' il prezzo dell'ultimo incasso, non piu' quello di acquisto.
        tp = None if soglia is None else pos.riferimento * soglia
        sl = None if self.par.soglia_sl is None else pos.riferimento * self.par.soglia_sl
        # Trailing stop: soglia mobile, calcolata sul massimo toccato finora.
        ts = None
        if self.par.trailing_stop is not None and pos.prezzo_massimo > 0:
            ts = pos.prezzo_massimo * (1.0 - self.par.trailing_stop / 100.0)
            sl = ts if sl is None else max(sl, ts)

        if self.par.esecuzione == "intraday":
            massimo = self._prezzo(pos.ticker, giorno, "high")
            minimo = self._prezzo(pos.ticker, giorno, "low")
            apertura = self._prezzo(pos.ticker, giorno, "open") or chiusura
            if massimo is None or minimo is None:
                massimo = minimo = chiusura
            # Lo stop ha la precedenza: se in giornata sono state toccate
            # entrambe le soglie non sappiamo in che ordine, e assumere il caso
            # peggiore evita di gonfiare i risultati.
            if sl is not None and minimo <= sl * (1 + EPS):
                # Se il titolo ha aperto gia' sotto la soglia (gap), l'ordine
                # esegue all'apertura, non al livello dello stop.
                motivo = "trailing_stop" if ts is not None and sl == ts else "stop_loss"
                return (min(sl, apertura), motivo)
            if tp is not None and massimo >= tp * (1 - EPS):
                return (max(tp, apertura), "take_profit")
            return None

        if sl is not None and chiusura <= sl * (1 + EPS):
            return (chiusura, "trailing_stop" if ts is not None and sl == ts else "stop_loss")
        if tp is not None and chiusura >= tp * (1 - EPS):
            return (chiusura, "take_profit")
        return None

    def _chiudi(self, pos: Posizione, giorno: pd.Timestamp, prezzo: float, motivo: str,
                frazione: float = 1.0) -> Tuple[float, Optional[Operazione]]:
        """Vende `frazione` del pacchetto. Con frazione < 1 il resto continua a correre.

        L'operazione viene registrata solo quando il pacchetto si chiude del
        tutto: cosi' il rendimento riportato e' quello dell'intero pacchetto,
        incassi parziali compresi, e non una serie di finti +10%.
        """
        frazione = max(0.0, min(1.0, frazione))
        quote_vendute = pos.quote * frazione
        lordo = quote_vendute * prezzo
        netto = lordo - self._commissione(lordo)
        pos.quote -= quote_vendute
        pos.incassato += netto

        if frazione < 1.0 and pos.quote > 1e-12:
            # Il riferimento sale al prezzo incassato: la parte rimasta punta
            # al gradino successivo invece di ri-scattare il giorno dopo.
            pos.riferimento = prezzo
            pos.prezzo_massimo = max(pos.prezzo_massimo, prezzo)
            pos.frazioni_vendute += 1
            return netto, None

        op = Operazione(
            ticker=pos.ticker, data_acquisto=pos.data_acquisto, data_vendita=giorno,
            prezzo_acquisto=pos.prezzo_acquisto, prezzo_vendita=prezzo,
            quote=pos.quote_iniziali, motivo=motivo,
            rendimento=(pos.incassato / pos.costo) - 1.0 if pos.costo else 0.0,
            giorni=(giorno - pos.data_acquisto).days,
        )
        return netto, op

    # -- acquisti --------------------------------------------------------

    def _acquista(self, giorno: pd.Timestamp, liquidita: float,
                  aperte: Dict[str, Posizione]) -> Tuple[float, List[Posizione], int]:
        """Divide la liquidita' in `n_titoli` parti e compra i migliori disponibili."""
        candidati = self.reco[self.reco.data_articolo == giorno]
        return self._acquista_da_lista(giorno, liquidita, aperte, candidati, self.par.n_titoli)

    def _acquista_da_lista(self, giorno: pd.Timestamp, liquidita: float,
                           aperte: Dict[str, Posizione], candidati: pd.DataFrame,
                           n_slot: int) -> Tuple[float, List[Posizione], int]:
        """Compra fino a `n_slot` titoli dalla lista, saltando quelli gia' aperti."""
        if candidati.empty or liquidita <= 0 or n_slot <= 0:
            return liquidita, [], 0

        pesi_vol: Dict[str, float] = {}
        if self.par.dimensione_posizione == "volatilita":
            # Peso inversamente proporzionale alla volatilita', normalizzato
            # sulla mediana dei candidati del giorno: cosi' la size media resta
            # quella di prima e si misura solo l'effetto della ridistribuzione.
            vols = {}
            for t in candidati.ticker:
                v = self.mercato.volatilita(giorno, t, self.par.vol_finestra)
                if v:
                    vols[t] = v
            if vols:
                mediana = float(pd.Series(list(vols.values())).median())
                tetto = max(1.0, self.par.vol_limite)
                for t, v in vols.items():
                    pesi_vol[t] = min(tetto, max(1.0 / tetto, mediana / v))

        if self.par.dimensione_posizione in ("patrimonio", "volatilita"):
            # Con segnali sparsi (uno o due al giorno) dividere la cassa per
            # n_titoli lascerebbe il portafoglio quasi sempre liquido: qui ogni
            # posizione vale 1/n del patrimonio, fin dove la cassa lo consente.
            valore_titoli = sum(pos.quote * (self._prezzo(pos.ticker, giorno, "close") or 0.0)
                                for pos in aperte.values())
            quota = min(liquidita, (liquidita + valore_titoli) / self.par.n_titoli)
        else:
            quota = liquidita / n_slot
        nuove: List[Posizione] = []
        scartati_duplicati = 0

        # Quanti pacchetti sono gia' aperti su ciascun titolo.
        conteggio: Dict[str, int] = {}
        for pos in aperte.values():
            conteggio[pos.ticker] = conteggio.get(pos.ticker, 0) + 1

        for _, riga in candidati.iterrows():
            if len(nuove) >= n_slot:
                break
            ticker = riga.ticker
            aperti_su_titolo = conteggio.get(ticker, 0) + sum(1 for p in nuove if p.ticker == ticker)
            if aperti_su_titolo:
                if not self.par.consenti_riacquisto:
                    scartati_duplicati += 1          # regola di de-duplicazione
                    continue
                if aperti_su_titolo >= self.par.max_lotti_per_titolo:
                    # Tetto ai pacchetti sullo stesso nome: senza, in un crollo
                    # si finirebbe con tutto il portafoglio su un titolo solo.
                    scartati_duplicati += 1
                    continue
            prezzo = self._prezzo(ticker, giorno, "close")
            if prezzo is None or prezzo <= 0:
                continue                              # nessun prezzo quel giorno: si salta
            quota_titolo = quota * pesi_vol.get(ticker, 1.0)
            quota_titolo = min(quota_titolo, liquidita)
            if quota_titolo <= 0:
                continue
            commissione = self._commissione(quota_titolo)
            investito = quota_titolo - commissione
            if investito <= 0:
                continue
            nuove.append(Posizione(ticker=ticker, data_acquisto=giorno, prezzo_acquisto=prezzo,
                                   quote=investito / prezzo, costo=quota_titolo, prezzo_massimo=prezzo))
            liquidita -= quota_titolo

        return liquidita, nuove, scartati_duplicati

    def _preleva(self, importo: float, liquidita: float,
                 aperte: Dict[str, Posizione], giorno: pd.Timestamp) -> Tuple[float, float]:
        """Preleva `importo` dal portafoglio: prima la liquidita', poi i titoli.

        Le posizioni vengono ridotte in proporzione al loro valore (non se ne
        chiude una intera): cosi' il prelievo non altera la composizione del
        portafoglio ne' anticipa vendite che le regole non prevedono.
        """
        preso = min(liquidita, importo)
        liquidita -= preso
        mancante = importo - preso
        if mancante <= 0 or not aperte:
            return liquidita, preso

        valori = {}
        for chiave, pos in aperte.items():
            prezzo = self.mercato.prezzo(giorno, pos.ticker, "close")
            if prezzo is not None:
                valori[chiave] = pos.quote * prezzo
        totale = sum(valori.values())
        if totale <= 0:
            return liquidita, preso

        frazione = min(1.0, mancante / totale)
        for chiave in valori:
            pos = aperte[chiave]
            quote_vendute = pos.quote * frazione
            pos.quote -= quote_vendute
            # Il costo residuo scala con le quote: il rendimento della parte
            # che resta aperta non viene falsato dal prelievo.
            pos.costo *= (1.0 - frazione)
        preso += totale * frazione
        return liquidita, preso

    # -- ciclo principale ------------------------------------------------

    def esegui(self) -> Risultato:
        par = self.par
        date_acquisto = set(self.reco.data_articolo.unique())
        # Chiave = identificativo del pacchetto (ticker piu' un progressivo),
        # cosi' lo stesso titolo puo' comparire piu' volte.
        aperte: Dict[str, Posizione] = {}
        contatore_lotti = 0
        operazioni: List[Operazione] = []
        equity: List[Dict[str, object]] = []

        primo = self.reco.data_articolo.min()
        ultimo_giorno_utile = self.mercato.giorni[-1]
        giorni = [g for g in self.mercato.giorni if primo <= g <= ultimo_giorno_utile]
        if not giorni:
            raise ValueError("Nessuna sovrapposizione fra raccomandazioni e prezzi.")

        liquidita = 0.0
        versamenti = 0.0
        prelievi = 0.0
        duplicati_totali = 0
        primo_giro = True
        anni_con_prelievo: Set[int] = set()
        mesi_versati: Set[Tuple[int, int]] = set()

        # Lista di candidati piu' recente: serve al reinvestimento immediato.
        lista_corrente = pd.DataFrame()

        for giorno in giorni:
            # 0) aggiornamento del massimo raggiunto (base del trailing stop)
            if par.trailing_stop is not None:
                for pos in aperte.values():
                    riferimento = (self._prezzo(pos.ticker, giorno, "high")
                                   or self._prezzo(pos.ticker, giorno, "close"))
                    if riferimento is not None and riferimento > pos.prezzo_massimo:
                        pos.prezzo_massimo = riferimento

            # 1) vendite: si controllano tutte le posizioni aperte
            incassi_del_giorno = 0.0
            chiuse_del_giorno = 0
            for chiave in list(aperte):
                pos = aperte[chiave]
                if giorno <= pos.data_acquisto:
                    continue
                esito = self._prezzo_di_uscita(pos, giorno)
                if esito is not None:
                    prezzo, motivo = esito
                    # Il take profit puo' essere parziale; lo stop chiude tutto.
                    frazione = (par.vendita_parziale_pct / 100.0
                                if motivo == "take_profit" else 1.0)
                    incasso, op = self._chiudi(pos, giorno, prezzo, motivo, frazione)
                    liquidita += incasso
                    incassi_del_giorno += incasso
                    if op is not None:
                        chiuse_del_giorno += 1
                        operazioni.append(op)
                        del aperte[chiave]
                    continue
                # delisting: se la serie finisce, si liquida all'ultimo prezzo noto
                fine_serie = self.mercato.ultimo_giorno_valido(pos.ticker)
                if fine_serie is not None and giorno > fine_serie:
                    prezzo = self._prezzo(pos.ticker, fine_serie, "close")
                    if prezzo is not None:
                        incasso, op = self._chiudi(pos, fine_serie, prezzo, "delisting")
                        liquidita += incasso
                        if op is not None:
                            operazioni.append(op)
                        del aperte[chiave]

            # 1b) reinvestimento immediato: il ricavato non aspetta il mese nuovo
            if (par.reinvestimento == "subito" and incassi_del_giorno > 0
                    and giorno not in date_acquisto and not lista_corrente.empty):
                # Tante nuove posizioni quante se ne sono chiuse: la size media
                # resta quella di prima, non si spezzetta un incasso in dieci.
                da_investire = min(liquidita, incassi_del_giorno)
                liquidita_prima = liquidita
                liquidita = da_investire
                liquidita, nuove, dupl = self._acquista_da_lista(
                    giorno, da_investire, aperte, lista_corrente, chiuse_del_giorno)
                liquidita += liquidita_prima - da_investire
                duplicati_totali += dupl
                for pos in nuove:
                    contatore_lotti += 1
                    aperte["%s#%d" % (pos.ticker, contatore_lotti)] = pos

            # 2) versamento e acquisti nei giorni di selezione
            if giorno in date_acquisto:
                lista_corrente = self.reco[self.reco.data_articolo == giorno]

                # 2a) prelievo annuo: una volta per anno civile, prima di investire
                if (par.prelievo_annuo_pct > 0 and not primo_giro
                        and giorno.year not in anni_con_prelievo):
                    anni_con_prelievo.add(giorno.year)
                    if par.prelievo_base == "versamenti":
                        base = par.versamento_mensile * 12
                    else:
                        valore_titoli = sum(
                            pos.quote * (self._prezzo(pos.ticker, giorno, "close") or 0.0)
                            for pos in aperte.values())
                        base = liquidita + valore_titoli
                    richiesto = base * par.prelievo_annuo_pct / 100.0
                    liquidita, preso = self._preleva(richiesto, liquidita, aperte, giorno)
                    prelievi += preso
                    # Le posizioni azzerate dal prelievo non vanno tenute in giro.
                    for chiave in [k for k, pos in aperte.items() if pos.quote <= 1e-12]:
                        del aperte[chiave]
                # Primo mese: solo il capitale iniziale. Dai successivi, la rata.
                # Il versamento e' mensile anche quando i segnali sono giornalieri:
                # altrimenti una strategia che compra ogni giorno riceverebbe
                # 1.000 euro al giorno e il confronto non avrebbe senso.
                mese_corrente = (giorno.year, giorno.month)
                versa = primo_giro or not par.versamento_solo_mensile or mese_corrente not in mesi_versati
                if versa:
                    mesi_versati.add(mese_corrente)
                    versamento = par.capitale_iniziale if primo_giro else par.versamento_mensile
                    primo_giro = False
                    liquidita += versamento
                    versamenti += versamento
                liquidita, nuove, duplicati = self._acquista(giorno, liquidita, aperte)
                duplicati_totali += duplicati
                for pos in nuove:
                    contatore_lotti += 1
                    aperte["%s#%d" % (pos.ticker, contatore_lotti)] = pos

            # 3) valorizzazione di fine giornata
            investito = 0.0
            for pos in aperte.values():
                prezzo = self._prezzo(pos.ticker, giorno, "close")
                investito += pos.valore(prezzo) if prezzo is not None else pos.costo
            equity.append({"data": giorno, "liquidita": liquidita, "investito": investito,
                           "totale": liquidita + investito, "versato": versamenti,
                           "prelevato": prelievi, "posizioni": len(aperte)})

        # chiusura finale a mercato: serve per calcolare il rendimento complessivo
        ultimo = giorni[-1]
        for pos in list(aperte.values()):
            prezzo = self._prezzo(pos.ticker, ultimo, "close")
            if prezzo is None:
                continue
            _, op = self._chiudi(pos, ultimo, prezzo, "fine_periodo")
            if op is not None:
                operazioni.append(op)

        df_equity = pd.DataFrame(equity).set_index("data")
        df_ops = pd.DataFrame([op.__dict__ for op in operazioni])
        risultato = Risultato(parametri=par, equity=df_equity, operazioni=df_ops)
        risultato.metriche = calcola_metriche(df_equity, df_ops, duplicati_totali)
        return risultato


# ---------------------------------------------------------------------------
# Metriche
# ---------------------------------------------------------------------------


def calcola_metriche(equity: pd.DataFrame, ops: pd.DataFrame, duplicati: int,
                     tasso_privo_rischio: float = 0.0) -> Dict[str, float]:
    """Metriche di un piano di accumulo: il rendimento semplice non basta.

    Con versamenti periodici il capitale medio investito cresce nel tempo, quindi
    si usa anche il **TWR** (time-weighted return), che misura la bravura della
    strategia indipendentemente dai flussi, e il **MWR/IRR** annuo sui flussi.
    """
    if equity.empty:
        return {}

    finale = float(equity["totale"].iloc[-1])
    versato = float(equity["versato"].iloc[-1])
    prelevato = float(equity["prelevato"].iloc[-1]) if "prelevato" in equity.columns else 0.0
    # Un euro prelevato e speso e' comunque un euro guadagnato: va sommato.
    ricevuto = finale + prelevato
    anni = max((equity.index[-1] - equity.index[0]).days / GIORNI_ANNO, 1e-9)

    rendimenti = mt.rendimenti_giornalieri(equity)
    twr = float((1.0 + rendimenti).prod() - 1.0)
    twr_annuo = (1.0 + twr) ** (1.0 / anni) - 1.0 if twr > -1 else -1.0

    # MWR (IRR): flussi mensili netti (versamenti meno prelievi) + valore finale.
    flussi = mt.flussi_netti(equity)
    mensili = flussi[flussi.abs() > 1e-9]
    irr_annuo = _irr_annuo(list(mensili.values), finale, len(mensili))

    # Drawdown sul patrimonio al netto dei versamenti cumulati.
    tot = equity["totale"]
    picco = tot.cummax()
    drawdown = (tot - picco) / picco.replace(0, float("nan"))
    max_dd = float(drawdown.min()) if not drawdown.isna().all() else 0.0

    metriche = {
        "valore_finale": finale,
        "prelevato": prelevato,
        "valore_piu_prelievi": ricevuto,
        "versato": versato,
        "guadagno": ricevuto - versato,
        "guadagno_pct": (ricevuto / versato - 1.0) * 100.0 if versato else 0.0,
        "twr_totale_pct": twr * 100.0,
        "twr_annuo_pct": twr_annuo * 100.0,
        "irr_annuo_pct": irr_annuo * 100.0 if irr_annuo is not None else float("nan"),
        "max_drawdown_pct": max_dd * 100.0,
        "anni": anni,
        "duplicati_scartati": float(duplicati),
    }
    metriche.update(mt.metriche_rischio(rendimenti, tasso_privo_rischio))
    if "investito" in equity.columns:
        metriche.update(mt.metriche_esposizione(equity))

    if not ops.empty:
        chiuse = ops[ops.motivo != "fine_periodo"]
        metriche.update({
            "operazioni": float(len(ops)),
            "chiuse": float(len(chiuse)),
            "take_profit": float((ops.motivo == "take_profit").sum()),
            "stop_loss": float((ops.motivo == "stop_loss").sum()),
            "trailing_stop": float((ops.motivo == "trailing_stop").sum()),
            "delisting": float((ops.motivo == "delisting").sum()),
            "aperte_a_fine": float((ops.motivo == "fine_periodo").sum()),
            "vincenti_pct": float((ops.rendimento > 0).mean() * 100.0),
            "rendimento_medio_pct": float(ops.rendimento.mean() * 100.0),
            "durata_media_giorni": float(ops.giorni.mean()),
        })
    return metriche


def _irr_annuo(versamenti: Sequence[float], valore_finale: float, n_mesi: int) -> Optional[float]:
    """IRR mensile dei flussi (versamenti negativi, valore finale positivo) -> annuo."""
    if n_mesi < 2 or valore_finale <= 0:
        return None
    # Versamenti = uscite dalla tasca dell'investitore (segno negativo);
    # prelievi = rientri (segno positivo). Il valore finale chiude la serie.
    flussi = [-v for v in versamenti] + [valore_finale]

    def van(tasso: float) -> float:
        # Con centinaia di flussi (1+r)**i esplode: si accumula in modo
        # incrementale e ci si ferma appena il fattore di sconto e' trascurabile.
        totale = 0.0
        fattore = 1.0
        for f in flussi:
            totale += f / fattore
            fattore *= (1.0 + tasso)
            if fattore > 1e250 or fattore <= 0:
                break
        return totale

    # Intervallo di ricerca sul tasso MENSILE. Con oltre 150 flussi, tassi
    # vicini a -1 mandano (1+r)**i in underflow: -0.5 al mese (-99,8% annuo)
    # e' gia' un limite inferiore assurdo per qualunque piano di accumulo.
    basso, alto = -0.5, 0.5
    try:
        if van(basso) * van(alto) > 0:
            return None
    except (ZeroDivisionError, OverflowError):
        return None
    for _ in range(200):
        medio = (basso + alto) / 2.0
        if van(basso) * van(medio) <= 0:
            alto = medio
        else:
            basso = medio
    mensile = (basso + alto) / 2.0
    return (1.0 + mensile) ** 12 - 1.0


# ---------------------------------------------------------------------------
# Report
# ---------------------------------------------------------------------------


def stampa_risultato(ris: Risultato, bench: Optional[Dict[str, float]] = None,
                     rel: Optional[Dict[str, float]] = None) -> None:
    m = ris.metriche
    p = ris.parametri
    print()
    print("=" * 68)
    print("  STRATEGIA  %s   |   %d titoli, %.0f EUR/mese" % (p.etichetta(), p.n_titoli, p.versamento_mensile))
    print("=" * 68)
    print("  Periodo                 %s -> %s (%.1f anni)" % (
        ris.equity.index[0].date(), ris.equity.index[-1].date(), m["anni"]))
    print("  Versato                 %10.0f EUR" % m["versato"])
    print("  Valore finale           %10.0f EUR" % m["valore_finale"])
    print("  Guadagno                %10.0f EUR  (%+.1f%% sul versato)" % (m["guadagno"], m["guadagno_pct"]))
    print("  Rendimento annuo (IRR)  %10.2f %%" % m["irr_annuo_pct"])
    print("  Rendimento annuo (TWR)  %10.2f %%" % m["twr_annuo_pct"])
    print("  Max drawdown            %10.2f %%" % m["max_drawdown_pct"])
    if "operazioni" in m:
        print("  " + "-" * 64)
        print("  Operazioni              %10.0f   (%.0f take profit, %.0f stop loss, "
              "%.0f trailing, %.0f delisting)" % (
                  m["operazioni"], m["take_profit"], m["stop_loss"],
                  m.get("trailing_stop", 0), m["delisting"]))
        print("  Ancora aperte a fine    %10.0f" % m["aperte_a_fine"])
        print("  Vincenti                %10.1f %%" % m["vincenti_pct"])
        print("  Rendimento medio/trade  %10.2f %%" % m["rendimento_medio_pct"])
        print("  Durata media            %10.0f giorni" % m["durata_media_giorni"])
    print("  Candidati scartati per duplicato: %.0f" % m["duplicati_scartati"])
    if "non_tradabili" in m:
        print("  Raccomandazioni non tradabili:     %.0f  (di cui %.0f nei primi 10 rank)" % (
            m["non_tradabili"], m.get("non_tradabili_top10", 0)))
    if m.get("prelevato", 0) > 0:
        print("  Prelevato lungo il percorso %6.0f EUR  (valore + prelievi: %.0f)" % (
            m["prelevato"], m["valore_piu_prelievi"]))
    print("  " + "-" * 64)
    print("  RISCHIO E CAPITALE IMPIEGATO")
    print("    Volatilita' annua     %10.2f %%" % m.get("volatilita_pct", float("nan")))
    print("    Sharpe                %10.2f      (< 0.5 debole, > 1 buono)" % m.get("sharpe", float("nan")))
    print("    Sortino               %10.2f      (penalizza solo le discese)" % m.get("sortino", float("nan")))
    print("    Calmar                %10.2f      (rendimento / drawdown)" % m.get("calmar", float("nan")))
    print("    Esposizione media     %10.1f %%    (il resto e' liquidita' ferma)" % (
        m.get("esposizione_media_pct", float("nan"))))
    print("    Giorni sotto l'80%%    %10.1f %%    di capitale investito" % (
        m.get("giorni_sotto_80pct_investito", float("nan"))))
    if bench:
        print("  " + "=" * 64)
        print("  CONFRONTO: accumulo su %s, stessi versamenti, mai vendendo" % bench.get("ticker", "indice"))
        print("    Valore finale         %10.0f EUR   (strategia: %.0f)" % (
            bench["valore_finale"], m["valore_finale"]))
        print("    Rendimento annuo IRR  %10.2f %%    (strategia: %.2f %%)" % (
            bench["irr_annuo_pct"], m["irr_annuo_pct"]))
        print("    Max drawdown          %10.2f %%    (strategia: %.2f %%)" % (
            bench["max_drawdown_pct"], m["max_drawdown_pct"]))
        delta = m["irr_annuo_pct"] - bench["irr_annuo_pct"]
        print("    ESITO: la strategia %s l'indice di %.2f punti annui" % (
            "batte" if delta > 0 else "perde contro", abs(delta)))
    if rel:
        print("  " + "-" * 64)
        print("  QUANTO E' MERCATO E QUANTO E' STRATEGIA")
        print("    Beta                  %10.2f      (1 = si muove come l'indice)" % rel.get("beta", float("nan")))
        print("    Alpha annuo           %10.2f %%    (cio' che resta togliendo il mercato)" % (
            rel.get("alpha_annuo_pct", float("nan"))))
        print("    R quadro              %10.2f      (quanta varianza spiega l'indice)" % rel.get("r_quadro", float("nan")))
        print("    Information ratio     %10.2f      (< 0.3 rumore, > 0.5 solido)" % (
            rel.get("information_ratio", float("nan"))))
        print("    t-stat extra rendim.  %10.2f      (|t| < 2: indistinguibile dal caso)" % (
            rel.get("t_stat_extra_rendimento", float("nan"))))
        if "twr_annuo_pari_esposizione_pct" in rel:
            print("    Indice a PARI esposizione: %.2f %% annuo contro %.2f %% della strategia" % (
                rel["twr_annuo_pari_esposizione_pct"], m.get("twr_annuo_pct", float("nan"))))
            print("      -> togliendo l'effetto della liquidita' ferma, la strategia %s" % (
                "resta indietro" if m.get("twr_annuo_pct", 0) < rel["twr_annuo_pari_esposizione_pct"]
                else "va meglio"))
    print()


def esegui_sweep(reco: pd.DataFrame, mercato: DatiMercato, base: Parametri,
                 griglia_tp: Sequence[float], griglia_sl: Sequence[Optional[float]]) -> pd.DataFrame:
    """Esplora la sensibilita' a X (take profit) e Y (stop loss)."""
    righe = []
    totale = len(griglia_tp) * len(griglia_sl)
    for i, tp in enumerate(griglia_tp):
        for j, sl in enumerate(griglia_sl):
            par = Parametri(**{**base.__dict__, "take_profit": tp, "stop_loss": sl})
            ris = Backtester(reco, mercato, par).esegui()
            m = ris.metriche
            LOGGER.info("[%2d/%2d] %-16s IRR %6.2f%%  finale %9.0f EUR  maxDD %6.1f%%",
                        i * len(griglia_sl) + j + 1, totale, par.etichetta(),
                        m["irr_annuo_pct"], m["valore_finale"], m["max_drawdown_pct"])
            righe.append({
                "take_profit": tp,
                "stop_loss": "hold" if sl is None else sl,
                "irr_annuo_pct": round(m["irr_annuo_pct"], 2),
                "twr_annuo_pct": round(m["twr_annuo_pct"], 2),
                "valore_finale": round(m["valore_finale"]),
                "guadagno_pct": round(m["guadagno_pct"], 1),
                "max_drawdown_pct": round(m["max_drawdown_pct"], 1),
                "operazioni": int(m.get("operazioni", 0)),
                "take_profit_n": int(m.get("take_profit", 0)),
                "stop_loss_n": int(m.get("stop_loss", 0)),
                "vincenti_pct": round(m.get("vincenti_pct", 0), 1),
                "durata_media_giorni": round(m.get("durata_media_giorni", 0)),
            })
    return pd.DataFrame(righe)


# ---------------------------------------------------------------------------
# CLI
# ---------------------------------------------------------------------------


def _parse_sl(testo: str) -> Optional[float]:
    testo = testo.strip().lower()
    return None if testo in ("none", "hold", "nessuno", "") else float(testo)


def parse_args(argv: Optional[Sequence[str]] = None) -> argparse.Namespace:
    p = argparse.ArgumentParser(
        description="Backtest della strategia DCA + Take Profit / Stop Loss.",
        formatter_class=argparse.ArgumentDefaultsHelpFormatter)
    p.add_argument("--reco", default="raccomandazioni_storiche.csv", help="Dataset delle raccomandazioni.")
    p.add_argument("--take-profit", type=_parse_sl, default=10.0,
                   help="X: guadagno percentuale a cui si vende ('none' per non vendere mai in guadagno).")
    p.add_argument("--stop-loss", type=_parse_sl, default=None,
                   help="Y: perdita percentuale a cui si vende ('none' per tenere il titolo).")
    p.add_argument("--capitale-iniziale", type=float, default=1000.0)
    p.add_argument("--versamento-mensile", type=float, default=1000.0)
    p.add_argument("--n-titoli", type=int, default=10, help="Titoli comprati ogni mese.")
    p.add_argument("--consenti-riacquisto", action="store_true",
                   help="Permette piu' pacchetti aperti sullo stesso titolo (toglie la de-duplicazione).")
    p.add_argument("--max-lotti-per-titolo", type=int, default=3,
                   help="Tetto ai pacchetti sullo stesso titolo, con --consenti-riacquisto.")
    p.add_argument("--vendita-parziale", type=float, default=100.0, dest="vendita_parziale_pct",
                   help="Percentuale del pacchetto venduta al take profit (50 = meta', il resto corre).")
    p.add_argument("--max-scaglioni", type=int, default=0,
                   help="Quante volte alleggerire un pacchetto (1 = dopo la prima vendita il resto "
                        "corre libero; 0 = a scala, senza limite).")
    p.add_argument("--dimensione-posizione", choices=("liquidita", "patrimonio", "volatilita"),
                   default="liquidita",
                   help="liquidita = la cassa si divide fra i titoli del mese (regola originale); "
                        "patrimonio = ogni posizione vale 1/n del portafoglio; "
                        "volatilita = come patrimonio, ma chi oscilla di piu' riceve meno capitale.")
    p.add_argument("--vol-finestra", type=int, default=60,
                   help="Giorni su cui misurare la volatilita' per il dimensionamento.")
    p.add_argument("--vol-limite", type=float, default=3.0,
                   help="Quanto una posizione puo' discostarsi dalla size base (3 = da un terzo al triplo).")
    p.add_argument("--trailing-stop", type=_parse_sl, default=None,
                   help="Z: vende se il titolo scende Z%% dal massimo toccato ('none' per disattivarlo).")
    p.add_argument("--reinvestimento", choices=("mensile", "subito"), default="mensile",
                   help="mensile = il ricavato aspetta il primo del mese; subito = reinvestito il giorno stesso.")
    p.add_argument("--versamento-solo-mensile", action="store_true",
                   help="Versa una sola volta al mese anche se i segnali sono giornalieri.")
    p.add_argument("--esecuzione", choices=("close", "intraday"), default="close",
                   help="close = soglie verificate sulla chiusura; intraday = su massimi/minimi.")
    p.add_argument("--commissione-pct", type=float, default=0.0, help="Commissione per operazione, in %%.")
    p.add_argument("--commissione-fissa", type=float, default=0.0, help="Commissione fissa per operazione, in EUR.")
    p.add_argument("--valuta", choices=("EUR", "USD", "locale"), default="EUR",
                   help="EUR converte da dollari a euro col cambio storico; "
                        "locale = i prezzi sono gia' nella valuta di conto (borse europee); "
                        "USD = nessuna conversione.")
    p.add_argument("--inizio", help="Prima data di selezione da usare (YYYY-MM-DD).")
    p.add_argument("--fine", help="Ultima data di selezione da usare (YYYY-MM-DD).")
    p.add_argument("--benchmark", default="SPY",
                   help="Ticker di confronto, oppure EQUIPESATO per un paniere a pesi uguali "
                        "degli stessi titoli della strategia (elimina l'effetto dividendi e "
                        "il diverso universo). 'none' per non calcolarlo.")
    p.add_argument("--escludi-ticker", default="",
                   help="Ticker da escludere, separati da virgola (es. simboli riciclati).")
    p.add_argument("--prelievo-annuo-pct", type=float, default=0.0,
                   help="Preleva ogni anno questa percentuale, per simulare un portafoglio da cui si spende.")
    p.add_argument("--prelievo-base", choices=("patrimonio", "versamenti"), default="patrimonio",
                   help="patrimonio = %% del valore totale; versamenti = %% dei versamenti annui.")
    p.add_argument("--tasso-privo-rischio", type=float, default=0.0,
                   help="Tasso annuo privo di rischio in %%, usato da Sharpe/Sortino/alpha.")
    p.add_argument("--output-regimi", help="Salva in CSV i rendimenti per fase di mercato.")
    p.add_argument("--cache-dir", default=".cache")
    p.add_argument("--refresh-prezzi", action="store_true", help="Riscarica i prezzi ignorando la cache.")
    p.add_argument("--sweep", action="store_true", help="Esplora una griglia di X e Y.")
    p.add_argument("--take-profit-grid", default="5,10,15,20,30")
    p.add_argument("--stop-loss-grid", default="none,10,15,20,30")
    p.add_argument("--output-operazioni", help="Salva il registro delle operazioni in CSV.")
    p.add_argument("--output-equity", help="Salva la curva del patrimonio in CSV.")
    p.add_argument("--output-sweep", default="sweep_risultati.csv", help="Dove salvare la tabella dello sweep.")
    p.add_argument("-q", "--quiet", action="store_true")
    return p.parse_args(argv)


def main(argv: Optional[Sequence[str]] = None) -> int:
    args = parse_args(argv)
    logging.basicConfig(level=logging.WARNING if args.quiet else logging.INFO,
                        format="%(asctime)s | %(levelname)-7s | %(message)s", datefmt="%H:%M:%S")

    if not os.path.exists(args.reco):
        LOGGER.error("Dataset non trovato: %s", args.reco)
        return 1

    esclusi = [t for t in args.escludi_ticker.split(",") if t.strip()]
    reco = carica_raccomandazioni(args.reco, args.inizio, args.fine, esclusi)
    tickers = sorted(reco.ticker.unique())
    benchmark = args.benchmark.strip().upper()
    if benchmark and benchmark not in ("NONE", "EQUIPESATO"):
        tickers = sorted(set(tickers) | {benchmark})
    inizio_prezzi = (reco.data_articolo.min() - pd.Timedelta(days=10)).strftime("%Y-%m-%d")
    fine_prezzi = pd.Timestamp.today().strftime("%Y-%m-%d")

    prezzi = price_data.load_prices(tickers, inizio_prezzi, fine_prezzi, args.cache_dir, args.refresh_prezzi)
    fx = (price_data.load_fx(inizio_prezzi, fine_prezzi, args.cache_dir, args.refresh_prezzi)
          if args.valuta == "EUR" else None)
    mercato = DatiMercato(prezzi, fx, args.valuta)
    if benchmark == "EQUIPESATO":
        serie = serie_equipesata(mercato, sorted(reco.ticker.unique()))
        if serie.empty:
            LOGGER.error("Impossibile costruire il benchmark equipesato.")
            return 1
        mercato.close["EQUIPESATO"] = serie
        mercato.tickers.add("EQUIPESATO")
        mercato._col["EQUIPESATO"] = mercato.close.columns.get_loc("EQUIPESATO")
        mercato._mat["close"] = mercato.close.to_numpy(dtype=float)
        mercato._ultimo_giorno["EQUIPESATO"] = serie.dropna().index[-1]
        LOGGER.info("Benchmark equipesato costruito su %d titoli: %.2f%% annuo composto.",
                    len(reco.ticker.unique()),
                    ((serie.iloc[-1] / serie.iloc[0]) ** (252.0 / len(serie)) - 1) * 100)
    qualita = report_qualita(reco, mercato)

    base = Parametri(
        take_profit=args.take_profit, stop_loss=args.stop_loss,
        capitale_iniziale=args.capitale_iniziale, versamento_mensile=args.versamento_mensile,
        n_titoli=args.n_titoli, dimensione_posizione=args.dimensione_posizione,
        vol_finestra=args.vol_finestra, vol_limite=args.vol_limite,
        consenti_riacquisto=args.consenti_riacquisto,
        max_lotti_per_titolo=args.max_lotti_per_titolo,
        vendita_parziale_pct=args.vendita_parziale_pct,
        max_scaglioni=args.max_scaglioni,
        esecuzione=args.esecuzione,
        trailing_stop=args.trailing_stop, reinvestimento=args.reinvestimento,
        prelievo_annuo_pct=args.prelievo_annuo_pct, prelievo_base=args.prelievo_base,
        versamento_solo_mensile=args.versamento_solo_mensile,
        commissione_pct=args.commissione_pct, commissione_fissa=args.commissione_fissa,
        valuta=args.valuta, inizio=args.inizio, fine=args.fine,
        escludi_ticker=tuple(t.strip().upper() for t in esclusi),
    )

    metriche_bench: Dict[str, float] = {}
    equity_bench = pd.DataFrame()
    if benchmark and benchmark != "NONE":
        date_versamento = sorted(reco.data_articolo.unique())
        metriche_bench, equity_bench = benchmark_dca(mercato, benchmark, date_versamento, base)

    if args.sweep:
        griglia_tp = [float(x) for x in args.take_profit_grid.split(",") if x.strip()]
        griglia_sl = [_parse_sl(x) for x in args.stop_loss_grid.split(",") if x.strip()]
        tabella = esegui_sweep(reco, mercato, base, griglia_tp, griglia_sl)
        tabella.to_csv(args.output_sweep, index=False)
        print()
        print(tabella.to_string(index=False))
        print("\nTabella salvata in %s" % args.output_sweep)
        migliore = tabella.loc[tabella.irr_annuo_pct.idxmax()]
        print("\nMigliore per IRR: TP %g%% / SL %s -> %.2f%% annuo (max drawdown %.1f%%)" % (
            migliore.take_profit, migliore.stop_loss, migliore.irr_annuo_pct, migliore.max_drawdown_pct))
        if metriche_bench:
            b = metriche_bench["irr_annuo_pct"]
            print("Accumulo su %s (mai vendendo): %.2f%% annuo, max drawdown %.1f%%" % (
                metriche_bench.get("ticker", "indice"), b, metriche_bench["max_drawdown_pct"]))
            sopra = tabella[tabella.irr_annuo_pct > b]
            print("Combinazioni che battono l'indice: %d su %d%s" % (
                len(sopra), len(tabella),
                "" if sopra.empty else " -> " + ", ".join(
                    "TP%g/SL%s" % (r.take_profit, r.stop_loss) for _, r in sopra.iterrows())))
        return 0

    ris = Backtester(reco, mercato, base).esegui()
    ris.metriche.update(qualita)

    relative: Dict[str, float] = {}
    if not equity_bench.empty:
        rend_str = mt.rendimenti_giornalieri(ris.equity)
        rend_ben = mt.rendimenti_giornalieri(equity_bench)
        relative = mt.metriche_relative(rend_str, rend_ben, args.tasso_privo_rischio)
        # Indice tenuto con la stessa esposizione giornaliera della strategia:
        # e' il confronto che neutralizza il "dead money".
        pari = mt.benchmark_a_pari_esposizione(ris.equity, rend_ben)
        if not pari.empty:
            anni = max(len(pari) / mt.GIORNI_BORSA_ANNO, 1e-9)
            crescita = float((1.0 + pari).prod())
            relative["twr_annuo_pari_esposizione_pct"] = (crescita ** (1.0 / anni) - 1.0) * 100.0
        if not ris.operazioni.empty:
            relative.update(mt.bootstrap_operazioni(ris.operazioni.rendimento))

    stampa_risultato(ris, metriche_bench, relative)

    if args.output_regimi or not args.quiet:
        rend_str = mt.rendimenti_giornalieri(ris.equity)
        rend_ben = mt.rendimenti_giornalieri(equity_bench) if not equity_bench.empty else None
        tabella_regimi = mt.rendimenti_per_regime(rend_str, rend_ben)
        if not tabella_regimi.empty:
            print("  TENUTA NELLE DIVERSE FASI DI MERCATO (rendimenti TWR, %)")
            print("\n".join("    " + r for r in tabella_regimi.to_string(index=False).splitlines()))
            print()
            if args.output_regimi:
                tabella_regimi.to_csv(args.output_regimi, index=False)
    if relative.get("bootstrap_p5_pct") is not None:
        print("  ROBUSTEZZA (bootstrap su %d operazioni)" % len(ris.operazioni))
        print("    Rendimento medio per operazione: %.2f%% | intervallo 5-95%%: %.2f%% .. %.2f%%" % (
            relative["trade_medio_pct"], relative["bootstrap_p5_pct"], relative["bootstrap_p95_pct"]))
        print("    Probabilita' che il trade medio sia negativo: %.1f%%\n" % (
            relative["prob_trade_medio_negativo_pct"]))
    if args.output_operazioni:
        ris.operazioni.to_csv(args.output_operazioni, index=False)
        LOGGER.info("Operazioni salvate in %s", args.output_operazioni)
    if args.output_equity:
        ris.equity.to_csv(args.output_equity)
        LOGGER.info("Curva del patrimonio salvata in %s", args.output_equity)
    return 0


if __name__ == "__main__":
    sys.exit(main())
