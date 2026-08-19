#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
anagrafica.py — la fotografia di oggi dei titoli, tenuta FUORI dal pannello.

Yahoo espone, oltre ai prezzi, un blocco di informazioni per titolo (`.info`):
settore, industria, capitalizzazione, denaro/lettera, azioni in circolazione.
Sono utili, ma sono **il valore di oggi**, senza storia. Usarle come segnale
dentro un backtest sarebbe look-ahead puro: nel 2005 non si conosceva la
capitalizzazione del 2026.

Per questo stanno in un file separato, una riga per ticker e non per giorno. La
separazione e' la parte importante: cosi' un dato-fotografia non puo' finire per
sbaglio in una colonna del pannello.

A cosa servono davvero:

* `settore` / `industria`: quasi statici, quindi gli unici utilizzabili anche
  storicamente. Servono a neutralizzare il settore nella classifica trasversale,
  altrimenti in una giornata di stress bancario la strategia compra otto banche e
  quello che ha in portafoglio e' una scommessa settoriale, non otto idee.

* `spread_pct`: la stima del costo di transazione vero. Misurato su un campione,
  lo spread denaro-lettera va dallo 0,02% di ENI al 34% di CLABO: tre ordini di
  grandezza nella stessa borsa. Con un take profit del 10% e' la differenza fra
  una strategia replicabile e una impossibile. Serve a calibrare il costo per
  fascia di liquidita' invece di usare una commissione piatta per tutti.

* `capitalizzazione`, `flottante`: solo per diagnostica e per raccontare da dove
  viene il rendimento. Mai come segnale.

Uso:
    python anagrafica.py                  # ~7-10 minuti per 400 titoli
"""

from __future__ import annotations

import argparse
import logging
import time
from typing import Dict, List, Optional, Sequence

import pandas as pd

LOGGER = logging.getLogger("anagrafica")

CAMPI = {
    "sector": "settore",
    "industry": "industria",
    "marketCap": "capitalizzazione",
    "sharesOutstanding": "azioni_in_circolazione",
    "floatShares": "flottante",
    "currency": "valuta",
    "quoteType": "tipo",
    "longName": "denominazione",
}


def _yfinance():
    import warnings
    warnings.filterwarnings("ignore")
    logging.getLogger("yfinance").setLevel(logging.CRITICAL)
    import yfinance
    return yfinance


def scarica(tickers: Sequence[str], pausa: float = 0.0) -> pd.DataFrame:
    """Una riga per ticker con i campi anagrafici e lo spread corrente."""
    yf = _yfinance()
    righe: List[Dict] = []
    for i, ticker in enumerate(tickers, start=1):
        if i % 50 == 0:
            LOGGER.info("Anagrafica %d di %d...", i, len(tickers))
        riga: Dict = {"ticker": ticker}
        try:
            info = yf.Ticker(ticker).info or {}
        except Exception as exc:
            LOGGER.debug("%s: %s", ticker, exc)
            info = {}
        for chiave, nome in CAMPI.items():
            riga[nome] = info.get(chiave)
        denaro, lettera = info.get("bid"), info.get("ask")
        # Lo spread ha senso solo se il libro e' quotato da entrambi i lati e non
        # e' incrociato: su titoli fermi Yahoo restituisce zeri o valori invertiti.
        if denaro and lettera and lettera > denaro > 0:
            riga["denaro"], riga["lettera"] = denaro, lettera
            riga["spread_pct"] = 100.0 * (lettera - denaro) / ((lettera + denaro) / 2.0)
        righe.append(riga)
        if pausa:
            time.sleep(pausa)

    df = pd.DataFrame(righe)
    LOGGER.info("Anagrafica: %d ticker. Settore noto per %d, spread per %d.",
                len(df), int(df.settore.notna().sum()), int(df.spread_pct.notna().sum()))
    return df


def costo_per_fascia(anagrafica: pd.DataFrame, pannello_liquidita: pd.DataFrame) -> pd.DataFrame:
    """Spread mediano per fascia di controvalore: la tabella di calibrazione dei costi.

    `pannello_liquidita` deve avere le colonne `ticker` e `controvalore_medio_20g`
    (l'ultimo valore disponibile per titolo).
    """
    fasce = [0, 1e4, 5e4, 2e5, 1e6, 1e7, 1e12]
    etichette = ["<10k", "10-50k", "50-200k", "200k-1M", "1-10M", ">10M"]
    m = anagrafica.merge(pannello_liquidita, on="ticker", how="inner")
    m["fascia"] = pd.cut(m.controvalore_medio_20g, fasce, labels=etichette)
    out = m.groupby("fascia", observed=True).agg(
        titoli=("ticker", "size"),
        spread_mediano=("spread_pct", "median"),
        spread_90=("spread_pct", lambda s: s.quantile(0.9)),
    ).round(3)
    return out


def main(argv: Optional[Sequence[str]] = None) -> int:
    p = argparse.ArgumentParser(description="Fotografia anagrafica dei titoli (settore, spread).",
                                formatter_class=argparse.ArgumentDefaultsHelpFormatter)
    p.add_argument("--universo", default="dati/universo_italia.txt")
    p.add_argument("--pannello", default="dati/pannello_italia.csv.gz",
                   help="Per la tabella di calibrazione dei costi. Opzionale.")
    p.add_argument("--output", default="dati/anagrafica.csv")
    p.add_argument("--pausa", type=float, default=0.0, help="Secondi fra due chiamate.")
    args = p.parse_args(argv)

    logging.basicConfig(level=logging.INFO, format="%(asctime)s | %(levelname)-7s | %(message)s",
                        datefmt="%H:%M:%S")
    with open(args.universo, encoding="utf-8") as fh:
        tickers = [r.strip() for r in fh if r.strip() and not r.startswith("#")]
    df = scarica(tickers, args.pausa)
    df.to_csv(args.output, index=False)
    LOGGER.info("Scritto %s", args.output)

    try:
        pannello = pd.read_csv(args.pannello, usecols=["data", "ticker", "controvalore_medio_20g"])
        ultimo = (pannello.sort_values("data").groupby("ticker")
                  .controvalore_medio_20g.last().reset_index())
        print("\nSpread denaro-lettera per fascia di controvalore giornaliero:")
        print(costo_per_fascia(df, ultimo).to_string())
        print("\nE' la tabella per calibrare il costo di transazione per fascia di liquidita'")
        print("invece di una commissione piatta uguale per tutti i titoli.")
    except (FileNotFoundError, ValueError) as exc:
        LOGGER.info("Tabella dei costi non prodotta (%s).", exc)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
