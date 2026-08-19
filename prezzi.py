#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
prezzi.py — scarico dei prezzi con dividendi e split conservati.

La differenza rispetto alla versione precedente del progetto e' `auto_adjust=False`.
Con `auto_adjust=True` Yahoo restituisce un solo prezzo, quello aggiustato per
split **e dividendi**, e butta via tutto il resto. Sembra comodo ma nasconde due
problemi:

1. **Il turnover risulta sbagliato andando indietro nel tempo.** Il volume che
   Yahoo restituisce e' aggiustato per gli split ma non per i dividendi
   (verificato: il volume di NVDA prima dello split 10:1 e' ~400M invece dei ~40M
   reali, e il controvalore resta continuo attraverso lo split). Moltiplicare il
   prezzo *aggiustato per i dividendi* per quel volume sottostima il controvalore
   esattamente del fattore di aggiustamento cumulato, che su Milano e' enorme:

       ENI.MI   2000: 0,225   2010: 0,349   2026: 0,978
       ISP.MI   2000: 0,192   2010: 0,389   2026: 0,967

   Il controvalore di ENI nel 2000 risulterebbe 4,4 volte piu' basso del vero. Un
   filtro "almeno 100.000 EUR al giorno" diventerebbe di fatto 440.000 nel 2000 e
   100.000 oggi: l'universo si restringerebbe da solo andando indietro, in modo
   invisibile. Il controvalore va calcolato sulla chiusura **grezza**.

2. **La serie dei dividendi e degli split non arriva.** Sono le due sole
   informazioni quasi-fondamentali che Yahoo fornisce su tutta la storia e senza
   look-ahead: il rendimento da dividendo e il raggruppamento azionario.

Quindi si tengono, per ogni giorno e ogni titolo:

    chiusura_agg   Adj Close: split + dividendi. Base di TUTTI i rendimenti.
    chiusura       Close: aggiustata per gli split, non per i dividendi.
                   Serve per il controvalore, il prezzo reale e il tick.
    apertura/massimo/minimo   riscalati col rapporto Adj Close / Close, per
                   restare coerenti con la serie dei rendimenti.
    volume, dividendo, split
"""

from __future__ import annotations

import argparse
import logging
import os
from typing import List, Optional, Sequence

import pandas as pd

LOGGER = logging.getLogger("prezzi")

BLOCCO = 40
COLONNE_YAHOO = ["Open", "High", "Low", "Close", "Adj Close", "Volume", "Dividends", "Stock Splits"]
RINOMINA = {"Open": "apertura", "High": "massimo", "Low": "minimo", "Close": "chiusura",
            "Adj Close": "chiusura_agg", "Volume": "volume",
            "Dividends": "dividendo", "Stock Splits": "split"}


def _yfinance():
    import warnings
    warnings.filterwarnings("ignore")
    logging.getLogger("yfinance").setLevel(logging.CRITICAL)
    import yfinance
    return yfinance


def scarica(tickers: Sequence[str], inizio: str, fine: str,
            riparazione: bool = False) -> pd.DataFrame:
    """Prezzi giornalieri in formato lungo, con dividendi e split.

    `riparazione` attiva la correzione di yfinance per gli errori noti di Yahoo
    (prezzi sbagliati di 100x, aggiustamenti mancanti). Rallenta lo scarico, ma
    su un listino pieno di raggruppamenti azionari puo' recuperare storia che
    altrimenti va buttata.
    """
    yf = _yfinance()
    pezzi: List[pd.DataFrame] = []
    assenti: List[str] = []
    tickers = sorted(set(tickers))

    for i in range(0, len(tickers), BLOCCO):
        blocco = tickers[i:i + BLOCCO]
        LOGGER.info("Scarico %d-%d di %d...", i + 1, min(i + BLOCCO, len(tickers)), len(tickers))
        grezzo = yf.download(blocco, start=inizio, end=fine, progress=False,
                             auto_adjust=False, actions=True, group_by="ticker",
                             threads=True, repair=riparazione)
        for ticker in blocco:
            try:
                sub = grezzo[ticker][COLONNE_YAHOO].dropna(how="all")
            except (KeyError, TypeError):
                assenti.append(ticker)
                continue
            if sub.empty:
                assenti.append(ticker)
                continue
            sub = sub.reset_index().rename(columns=RINOMINA)
            sub = sub.rename(columns={sub.columns[0]: "data"})
            sub["ticker"] = ticker
            pezzi.append(sub)

    if assenti:
        LOGGER.warning("Nessun prezzo per %d ticker: %s", len(assenti), ", ".join(assenti[:10]))
    if not pezzi:
        # Non e' un errore fatale: capita chiedendo solo ticker senza quotazioni
        # (AIM2.MI e' quotato da un giorno). Sollevare qui faceva fallire ogni
        # ricostruzione successiva, perche' quei ticker non entrano mai nella
        # cache e venivano richiesti di nuovo a ogni giro. Decide chi chiama.
        LOGGER.warning("Nessun prezzo scaricato per i %d ticker richiesti.", len(tickers))
        return pd.DataFrame(columns=["data"] + list(RINOMINA.values())
                            + ["ticker", "fattore_dividendi"])

    out = pd.concat(pezzi, ignore_index=True)
    out["data"] = pd.to_datetime(out["data"], utc=True).dt.tz_localize(None).dt.normalize()
    out = out.dropna(subset=["chiusura", "chiusura_agg"])
    out = out.sort_values(["ticker", "data"]).reset_index(drop=True)
    return riscala_ohlc(out)


def riscala_ohlc(df: pd.DataFrame) -> pd.DataFrame:
    """Porta apertura/massimo/minimo sulla scala della chiusura aggiustata.

    Yahoo aggiusta solo la chiusura: l'Adj Close e' la serie coerente, mentre
    Open/High/Low restano sulla scala grezza. Senza questo passaggio l'escursione
    giornaliera e l'ATR sarebbero calcolati su prezzi di scala diversa dalla
    chiusura, e su un titolo con 26 anni di dividendi l'errore e' del 300%.
    """
    fattore = (df["chiusura_agg"] / df["chiusura"]).replace([float("inf"), -float("inf")], pd.NA)
    for col in ("apertura", "massimo", "minimo"):
        df[col] = df[col] * fattore
    df["fattore_dividendi"] = fattore.round(6)
    return df


def sanifica(df: pd.DataFrame, minimo_sedute: int = 252, rapporto_massimo: float = 1000.0,
             quota_incoerenti: float = 0.01, salto_massimo: float = 2.5,
             salto_senza_scambi: float = 0.20) -> pd.DataFrame:
    """Butta la parte corrotta delle serie aggiustate.

    Sui titoli con raggruppamenti azionari e aumenti di capitale ripetuti (le
    banche e le small cap italiane sono il caso da manuale) l'aggiustamento
    retroattivo di Yahoo degenera. Non e' un dettaglio: BES.MI arriva a un
    prezzo aggiustato di **-900.400 EUR** nel 2000 e di 43 milioni nel 2002, con
    un ultimo prezzo di 0,058. Qualunque variazione calcolata su quei numeri e'
    rumore puro.

    Tre criteri di corruzione **sistematica**, che tagliano tutta la storia
    precedente all'ultima occorrenza:

    * un prezzo <= 0, che non esiste;
    * `fattore_dividendi` > 1: l'Adj Close non puo' superare il Close, perche' i
      dividendi *abbassano* i prezzi storici. UCG.MI nel 2000 ha fattore 1091;
    * un prezzo aggiustato oltre `rapporto_massimo` volte l'ultimo prezzo. Serve
      perche' la cascata di aggiustamenti prosegue anche dove nessun prezzo e'
      negativo: su BES.MI il rapporto massimo/ultimo vale 748 milioni.
    * un salto giornaliero oltre `salto_massimo` volte **senza uno split
      registrato** a fianco: e' un'operazione sul capitale che Yahoo non ha
      annotato. Sono 7 casi in tutto il listino, ma ognuno avvelena i 252 giorni
      di finestra mobile che lo seguono: OPS.MI ha un +24.900% il 2024-02-01, con
      una volatilita' implicita del 88.000% e beta -34 per un anno intero.

    E un criterio di **incoerenza interna**: la chiusura aggiustata deve stare
    fra minimo e massimo della seduta. Se capita su meno di `quota_incoerenti`
    delle righe e' un errore isolato di Yahoo e si buttano solo quelle righe
    (ENI ne ha 2 su 6.800: tagliare tutta la storia precedente sarebbe assurdo);
    se capita piu' spesso la serie e' compromessa e si taglia (BES.MI: 277).

    Infine le **stampe fantasma**: un prezzo che si muove di oltre
    `salto_senza_scambi` in una seduta a volume zero. Senza scambi non c'e'
    prezzo, quindi la riga e' inventata e si butta (101 righe su 17 ticker, 60
    delle quali su EPH.MI). Buttarla ricongiunge i due prezzi veri ai suoi lati,
    che e' esattamente cio' che si vuole.
    """
    if df.empty:
        return df
    colonne = ["apertura", "massimo", "minimo", "chiusura", "chiusura_agg"]
    accorciati, rimossi, isolate, pezzi = [], [], 0, []

    for ticker, gruppo in df.groupby("ticker", sort=False):
        g = gruppo.sort_values("data")
        ultimo = g["chiusura_agg"].iloc[-1]

        incoerente = ((g["chiusura_agg"] < g["minimo"] * 0.998)
                      | (g["chiusura_agg"] > g["massimo"] * 1.002))
        # Uno split registrato a fianco spiega il salto: si guarda anche il
        # giorno prima e dopo, perche' Yahoo non e' preciso sulla data.
        split_vicino = g["split"].rolling(3, center=True, min_periods=1).max() > 0
        salto = g["chiusura_agg"].pct_change()
        sistematica = ((g[colonne] <= 0).any(axis=1)
                       | (g["fattore_dividendi"] > 1.0 + 1e-6)
                       | (g["chiusura_agg"] > rapporto_massimo * max(ultimo, 1e-9))
                       | ((salto > salto_massimo) & ~split_vicino))
        if incoerente.mean() > quota_incoerenti:
            sistematica = sistematica | incoerente

        if sistematica.any():
            taglio = g.loc[sistematica, "data"].max()
            g = g[g["data"] > taglio]
            if len(g) < minimo_sedute:
                rimossi.append("%s (%d sedute sane)" % (ticker, len(g)))
                continue
            accorciati.append("%s (dal %s)" % (ticker, g["data"].min().date()))

        # Le incoerenze isolate e le stampe fantasma si buttano riga per riga.
        residue = ((g["chiusura_agg"] < g["minimo"] * 0.998)
                   | (g["chiusura_agg"] > g["massimo"] * 1.002)
                   | ((g["volume"] == 0)
                      & (g["chiusura_agg"].pct_change().abs() > salto_senza_scambi)))
        if residue.any():
            isolate += int(residue.sum())
            g = g[~residue]
        pezzi.append(g)

    if accorciati:
        LOGGER.warning("Serie accorciate per corruzione dell'aggiustamento (%d): %s",
                       len(accorciati), ", ".join(accorciati[:12]))
    if isolate:
        LOGGER.warning("Righe singole incoerenti buttate: %d", isolate)
    if rimossi:
        LOGGER.warning("Ticker scartati, storia inutilizzabile (%d): %s",
                       len(rimossi), ", ".join(rimossi[:12]))
    return (pd.concat(pezzi, ignore_index=True).sort_values(["ticker", "data"]).reset_index(drop=True)
            if pezzi else df.iloc[0:0])


def carica(tickers: Sequence[str], inizio: str, fine: str, cache: str,
           aggiorna: bool = False, riparazione: bool = False) -> pd.DataFrame:
    """Prezzi dalla cache; scarica solo cio' che manca."""
    if os.path.exists(cache) and not aggiorna:
        df = pd.read_csv(cache, parse_dates=["data"])
        mancanti = sorted(set(tickers) - set(df.ticker.unique()))
        if not mancanti:
            LOGGER.info("Prezzi dalla cache: %d righe, %d ticker.", len(df), df.ticker.nunique())
            return df
        LOGGER.info("Cache incompleta: scarico %d ticker mancanti.", len(mancanti))
        nuovi = scarica(mancanti, inizio, fine, riparazione)
        if nuovi.empty:
            # Restano senza prezzi: si usa la cache cosi' com'e' e non la si
            # riscrive, altrimenti a ogni giro si rifa' un file da 36 MB per nulla.
            LOGGER.warning("%d ticker restano senza prezzi: %s",
                           len(mancanti), ", ".join(mancanti[:10]))
            return df.sort_values(["ticker", "data"]).reset_index(drop=True)
        df = pd.concat([df, nuovi], ignore_index=True)
    else:
        df = scarica(tickers, inizio, fine, riparazione)
        if df.empty:
            raise RuntimeError("Nessun prezzo scaricato per l'intero universo.")
    df = df.sort_values(["ticker", "data"]).reset_index(drop=True)
    os.makedirs(os.path.dirname(cache) or ".", exist_ok=True)
    df.to_csv(cache, index=False, compression="gzip")
    LOGGER.info("Cache scritta: %s (%.1f MB)", cache, os.path.getsize(cache) / 1e6)
    return df


def main(argv: Optional[Sequence[str]] = None) -> int:
    p = argparse.ArgumentParser(description="Scarica i prezzi grezzi con dividendi e split.",
                                formatter_class=argparse.ArgumentDefaultsHelpFormatter)
    p.add_argument("--universo", default="dati/universo_italia.txt")
    p.add_argument("--da", default="2000-01-01")
    p.add_argument("--a", default=pd.Timestamp.today().strftime("%Y-%m-%d"))
    p.add_argument("--cache", default="dati/prezzi_grezzi.csv.gz")
    p.add_argument("--aggiorna", action="store_true", help="Riscarica tutto da zero.")
    p.add_argument("--riparazione", action="store_true", help="Attiva repair=True di yfinance.")
    args = p.parse_args(argv)

    logging.basicConfig(level=logging.INFO, format="%(asctime)s | %(levelname)-7s | %(message)s",
                        datefmt="%H:%M:%S")
    with open(args.universo, encoding="utf-8") as fh:
        tickers = [r.strip() for r in fh if r.strip() and not r.startswith("#")]
    LOGGER.info("Universo: %d ticker", len(tickers))
    df = carica(tickers, args.da, args.a, args.cache, args.aggiorna, args.riparazione)
    LOGGER.info("Prezzi: %d righe, %d ticker, %s -> %s",
                len(df), df.ticker.nunique(), df.data.min().date(), df.data.max().date())
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
