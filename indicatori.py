#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
indicatori.py — dai prezzi grezzi al pannello degli indicatori.

Una riga per (data, ticker). Tutti gli indicatori usano **solo dati fino a quel
giorno**: nessuno guarda avanti, quindi il pannello e' usabile in backtest senza
ulteriori precauzioni.

Rispetto alla versione precedente del progetto cambiano tre cose, tutte imposte
dal passaggio da 40 titoli del FTSE MIB a 400 di tutto il listino:

* **Liquidita'.** Su 40 blue chip era un problema inesistente; su 400 titoli 108
  scambiano meno di 10.000 EUR al giorno e 135 hanno almeno il 10% delle sedute a
  volume zero. Un prezzo fermo produce **finto ritorno alla media**: la "caduta"
  del giorno X e' spesso la stampa arretrata di una discesa gia' avvenuta, e il
  "recupero" del giorno dopo e' meccanico. Da qui `controvalore_medio_20g` e
  `sedute_scambiate_20g`, che servono a escludere quei titoli.

* **Normalizzazione per volatilita'.** Una soglia in percentuale uguale per tutti
  ("e' sceso del 10%") pesca sempre i titoli piu' volatili, che sono anche i piu'
  illiquidi: e' il meccanismo che sul S&P 500 aveva prodotto beta 1,61 e alpha
  negativo. `var_*_in_atr` misura la caduta in unita' di volatilita' del titolo,
  cosi' una utility e una microcap sono confrontabili.

* **Confronto trasversale.** Con 400 titoli, in una giornata di panico "sceso del
  10%" scatta su decine di titoli insieme e la soglia fissa non seleziona piu'
  nulla. `pct_*` dice quanto e' estremo il movimento **rispetto agli altri titoli
  di quel giorno**; `var_*_rel` lo depura del movimento del mercato.
"""

from __future__ import annotations

import logging
from typing import Optional

import numpy as np
import pandas as pd

LOGGER = logging.getLogger("indicatori")

ANNO = 252


def _rsi(chiusure: pd.Series, periodi: int) -> pd.Series:
    """RSI di Wilder: sotto 30 ipervenduto, sopra 70 ipercomprato."""
    delta = chiusure.diff()
    su = delta.clip(lower=0.0)
    giu = -delta.clip(upper=0.0)
    media_su = su.ewm(alpha=1.0 / periodi, adjust=False).mean()
    media_giu = giu.ewm(alpha=1.0 / periodi, adjust=False).mean()
    rs = media_su / media_giu.replace(0.0, np.nan)
    return (100.0 - 100.0 / (1.0 + rs)).fillna(50.0)


def _atr(g: pd.DataFrame, periodi: int = 14) -> pd.Series:
    """Average True Range di Wilder, sui prezzi riscalati.

    Il true range tiene conto del salto fra la chiusura precedente e la seduta
    corrente, quindi non sottostima i gap di apertura come farebbe massimo-minimo.
    """
    prec = g["chiusura_agg"].shift()
    tr = pd.concat([g["massimo"] - g["minimo"],
                    (g["massimo"] - prec).abs(),
                    (g["minimo"] - prec).abs()], axis=1).max(axis=1)
    return tr.ewm(alpha=1.0 / periodi, adjust=False, min_periods=periodi).mean()


def per_titolo(g: pd.DataFrame) -> pd.DataFrame:
    """Tutti gli indicatori che si calcolano guardando un solo titolo."""
    g = g.sort_values("data").copy()
    c = g["chiusura_agg"]

    # --- variazioni: la base di qualunque regola "e' sceso del X%" -------------
    for n in (1, 5, 20, 60, 252):
        g["var_%dg" % n] = c.pct_change(n)

    # --- distanza dagli estremi: la misura piu' usata per comprare il ribasso --
    massimo = c.rolling(ANNO, min_periods=20).max()
    minimo = c.rolling(ANNO, min_periods=20).min()
    g["massimo_52s"] = massimo
    g["minimo_52s"] = minimo
    g["dal_massimo_52s"] = c / massimo - 1.0
    g["dal_minimo_52s"] = c / minimo - 1.0

    # --- trend: distanza percentuale, non il livello, cosi' i titoli sono
    #     confrontabili a prescindere dal prezzo unitario -----------------------
    for finestra in (20, 50, 200):
        sma = c.rolling(finestra, min_periods=finestra // 2).mean()
        g["dist_sma%d" % finestra] = c / sma - 1.0

    # --- oscillatori ---------------------------------------------------------
    g["rsi_14"] = _rsi(c, 14)
    # RSI a 2 periodi: per il ritorno alla media di brevissimo e' molto piu'
    # reattivo del 14, che su un crollo di tre sedute non arriva a scendere.
    g["rsi_2"] = _rsi(c, 2)
    sma20 = c.rolling(20, min_periods=10).mean()
    std20 = c.rolling(20, min_periods=10).std()
    g["zscore_20g"] = (c - sma20) / std20.replace(0.0, np.nan)

    # --- rischio -------------------------------------------------------------
    g["volatilita_20g"] = g["var_1g"].rolling(20, min_periods=10).std() * np.sqrt(ANNO)
    g["volatilita_60g"] = g["var_1g"].rolling(60, min_periods=30).std() * np.sqrt(ANNO)
    # Sopra 1 la volatilita' recente e' superiore a quella di medio periodo: uno
    # shock improvviso, non un titolo cronicamente agitato.
    g["rapporto_volatilita"] = g["volatilita_20g"] / g["volatilita_60g"].replace(0.0, np.nan)
    g["escursione_media_20g"] = ((g["massimo"] - g["minimo"]) / c).rolling(20, min_periods=10).mean()
    atr = _atr(g)
    g["atr_14"] = atr
    g["atr_pct_14"] = atr / c
    # La caduta misurata in ATR invece che in percentuale: -3 significa "ha perso
    # tre volte la sua escursione tipica", che e' confrontabile fra titoli con
    # volatilita' diverse. La percentuale non lo e'.
    for n in (5, 20):
        g["var_%dg_in_atr" % n] = (c - c.shift(n)) / atr.replace(0.0, np.nan)

    # --- eventi di seduta ----------------------------------------------------
    # I crolli su notizia arrivano come salto di apertura: dentro var_1g si
    # confondono col movimento intraday.
    g["gap_apertura"] = g["apertura"] / c.shift() - 1.0
    perdita = (g["var_1g"] < 0).astype(int)
    g["giorni_rossi_consecutivi"] = perdita.groupby((perdita != perdita.shift()).cumsum()).cumsum() * perdita

    # --- liquidita': calcolata sulla chiusura GREZZA -------------------------
    # Il volume Yahoo e' aggiustato per gli split ma non per i dividendi: usare
    # la chiusura aggiustata sottostimerebbe il controvalore del fattore di
    # aggiustamento cumulato, che su ENI nel 2000 vale 0,225 (cioe' 4,4 volte).
    controvalore = g["chiusura"] * g["volume"]
    g["controvalore_medio_20g"] = controvalore.rolling(20, min_periods=10).median()
    g["sedute_scambiate_20g"] = (g["volume"] > 0).rolling(20, min_periods=10).sum()
    volume_medio = g["volume"].rolling(20, min_periods=10).mean()
    g["volume_relativo"] = g["volume"] / volume_medio.replace(0, np.nan)

    # --- dividendi: l'unico segnale quasi-fondamentale senza look-ahead ------
    # Il dividendo e' noto nel momento in cui viene staccato, quindi sommarlo
    # sugli ultimi 252 giorni non guarda avanti. Discrimina cio' che alla regola
    # "caduta" manca: "sceso del 10% ma rende il 7%" da "sceso del 10% e non
    # paga nulla" -- che su Milano e' la differenza fra le due meta' del listino.
    dividendo_12m = g["dividendo"].rolling(ANNO, min_periods=1).sum()
    g["dividendo_12m"] = dividendo_12m
    g["rendimento_dividendo_12m"] = dividendo_12m / g["chiusura"]
    # Confronto con i 12 mesi precedenti: un taglio del dividendo e' un segnale
    # di difficolta' molto piu' tempestivo di qualunque bilancio.
    precedente = dividendo_12m.shift(ANNO)
    g["variazione_dividendo"] = np.where(precedente > 0, dividendo_12m / precedente - 1.0, np.nan)

    # --- raggruppamenti azionari --------------------------------------------
    # Uno split inverso (fattore < 1) e' uno dei migliori indicatori gratuiti di
    # difficolta': lo si fa quando il prezzo e' crollato sotto la soglia di
    # decenza. Ed e' esattamente la popolazione che "compra il ribasso" raccoglie.
    inverso = ((g["split"] > 0) & (g["split"] < 1)).astype(int)
    g["raggruppamento_24m"] = inverso.rolling(2 * ANNO, min_periods=1).sum()

    # --- anzianita' di quotazione -------------------------------------------
    # Serve alla regola di stagionatura: i primi 12 mesi dopo la quotazione sono
    # un regime di prezzo diverso (stabilizzazione del collocatore, scadenza del
    # lock-up, flottante minimo), non un prezzo di mercato.
    g["sedute_di_storia"] = np.arange(1, len(g) + 1)
    return g


def trasversali(dati: pd.DataFrame, controvalore_minimo: float = 50_000.0,
                sedute_minime: int = 252) -> pd.DataFrame:
    """Indicatori che confrontano i titoli fra loro dentro la stessa giornata.

    Il confronto si limita ai titoli **eleggibili** quel giorno (abbastanza
    storia, abbastanza scambi): includere i titoli fermi falserebbe i percentili,
    perche' un prezzo che non si muove finirebbe sempre a meta' classifica e
    sposterebbe tutti gli altri. La soglia usata qui e' volutamente larga: il
    filtro operativo, piu' severo, si applica nel passaggio dei segnali.
    """
    eleggibile = ((dati["sedute_di_storia"] >= sedute_minime)
                  & (dati["controvalore_medio_20g"] >= controvalore_minimo)
                  & (dati["volume"] > 0))
    dati["eleggibile"] = eleggibile.astype(int)
    LOGGER.info("Righe eleggibili per il confronto trasversale: %d su %d (%.1f%%)",
                int(eleggibile.sum()), len(dati), 100.0 * eleggibile.mean())

    base = dati[eleggibile]
    for col in ("var_5g", "var_20g", "dal_massimo_52s", "var_5g_in_atr", "rendimento_dividendo_12m"):
        # Percentile dentro la giornata: 0 = il piu' basso del listino, 1 = il piu' alto.
        dati["pct_" + col] = base.groupby("data")[col].rank(pct=True)
    for col in ("var_5g", "var_20g"):
        # Movimento depurato dal mercato: si usa la mediana dei titoli eleggibili
        # e non un indice, perche' l'indice FTSE MIB e' un price index mentre i
        # nostri prezzi sono total return -- sottrarlo introdurrebbe una deriva
        # sistematica di circa 2,4 punti l'anno.
        #
        # La mediana e' UNA sola per giornata, quindi si assegna a **tutte** le
        # righe e non solo a quelle eleggibili: serve anche sui titoli illiquidi,
        # che sono l'oggetto del test di robustezza. I percentili invece restano
        # definiti solo dentro l'insieme eleggibile, dove hanno senso.
        mediana_giorno = base.groupby("data")[col].median()
        dati[col + "_rel"] = dati[col] - dati["data"].map(mediana_giorno)
    return dati


def beta(dati: pd.DataFrame, mercato: pd.Series, finestra: int = ANNO) -> pd.DataFrame:
    """Beta mobile rispetto al mercato, su `finestra` sedute.

    Serve per poter filtrare o pesare in base al beta invece di scoprire a
    posteriori di aver comprato leva: sul S&P 500 la strategia "compra il
    ribasso" aveva beta 1,61 e alpha negativo, e il beta non era stato scelto.
    """
    rm = mercato.rename("var_mercato")
    dati = dati.merge(rm, left_on="data", right_index=True, how="left")
    pezzi = []
    for _, g in dati.groupby("ticker", sort=False):
        g = g.sort_values("data").copy()
        cov = g["var_1g"].rolling(finestra, min_periods=finestra // 2).cov(g["var_mercato"])
        var = g["var_mercato"].rolling(finestra, min_periods=finestra // 2).var()
        g["beta_252g"] = cov / var.replace(0.0, np.nan)
        pezzi.append(g)
    return pd.concat(pezzi, ignore_index=True)


def calcola(prezzi: pd.DataFrame, mercato: Optional[pd.Series] = None,
            controvalore_minimo: float = 50_000.0) -> pd.DataFrame:
    """Pannello completo degli indicatori."""
    pezzi = [per_titolo(g) for _, g in prezzi.groupby("ticker", sort=True)]
    dati = pd.concat(pezzi, ignore_index=True)
    dati = dati.sort_values(["data", "ticker"]).reset_index(drop=True)
    dati = trasversali(dati, controvalore_minimo)
    if mercato is not None:
        dati = beta(dati, mercato)
    arrotonda = {c: 6 for c in dati.columns if dati[c].dtype.kind == "f"}
    return dati.round(arrotonda).sort_values(["data", "ticker"]).reset_index(drop=True)
