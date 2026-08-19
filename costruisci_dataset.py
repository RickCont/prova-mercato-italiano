#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
costruisci_dataset.py — orchestratore: dalla lista dei ticker al pannello finito.

    python costruisci_dataset.py --da 2000-01-01

Passaggi:
  1. legge l'universo (universo.py lo produce dal product directory di Euronext)
  2. scarica i prezzi grezzi con dividendi e split (prezzi.py)
  3. butta le serie corrotte dall'aggiustamento retroattivo di Yahoo
  4. calcola gli indicatori, compresi quelli trasversali (indicatori.py)
  5. scrive il pannello e un riepilogo diagnostico

Il pannello NON e' filtrato: contiene anche i titoli illiquidi e le neoquotate.
I filtri di eleggibilita' (storia minima, controvalore minimo, sedute scambiate)
si applicano **giorno per giorno** quando si generano i segnali, cosi' si possono
cambiare senza riscaricare e si puo' misurare quanto contano.
"""

from __future__ import annotations

import argparse
import logging
import os
from typing import Optional, Sequence

import pandas as pd

import indicatori
import prezzi as mod_prezzi
import universo as mod_universo

LOGGER = logging.getLogger("dataset")


def serie_mercato(inizio: str, fine: str, cache: str,
                  ticker: str = mod_universo.BENCHMARK_PREZZO) -> pd.Series:
    """Rendimenti giornalieri dell'indice, per il calcolo del beta."""
    if os.path.exists(cache):
        df = pd.read_csv(cache, parse_dates=["data"])
    else:
        df = mod_prezzi.scarica([ticker], inizio, fine)
        df.to_csv(cache, index=False, compression="gzip")
    serie = df.set_index("data")["chiusura_agg"].sort_index()
    return serie.pct_change().rename("var_mercato")


def riepilogo(dati: pd.DataFrame) -> None:
    LOGGER.info("Pannello: %d righe, %d ticker, %s -> %s",
                len(dati), dati.ticker.nunique(), dati.data.min().date(), dati.data.max().date())
    copertura = dati.groupby("ticker").size()
    LOGGER.info("Sedute per ticker: min %d, mediana %d, max %d",
                copertura.min(), int(copertura.median()), copertura.max())

    # Quanti titoli sono investibili in ciascun anno: e' la diagnostica piu'
    # importante del dataset. Se l'ampiezza dell'universo cresce lungo il
    # campione, i risultati del backtest sono dominati dagli ultimi anni e i
    # sotto-periodi non sono confrontabili.
    ampiezza = (dati[dati.eleggibile == 1].groupby([dati.data.dt.year, "data"])["ticker"]
                .nunique().groupby(level=0).median())
    LOGGER.info("Titoli eleggibili per giorno, mediana per anno:")
    for anno, n in ampiezza.items():
        LOGGER.info("   %d: %3d %s", anno, int(n), "#" * int(n / 4))

    cali = int((dati["var_5g"] <= -0.10).sum())
    cali_el = int(((dati["var_5g"] <= -0.10) & (dati.eleggibile == 1)).sum())
    LOGGER.info("Cali >= 10%% in 5 sedute: %d in tutto il pannello, %d fra gli eleggibili (%.1f%%)",
                cali, cali_el, 100.0 * cali_el / max(1, cali))
    div = dati[dati.rendimento_dividendo_12m > 0]
    LOGGER.info("Righe con dividendo negli ultimi 12 mesi: %d (%.1f%%), rendimento mediano %.2f%%",
                len(div), 100.0 * len(div) / max(1, len(dati)),
                100.0 * div.rendimento_dividendo_12m.median())
    ragg = dati[dati.raggruppamento_24m > 0].ticker.nunique()
    LOGGER.info("Titoli con un raggruppamento azionario negli ultimi 24 mesi: %d", ragg)


def main(argv: Optional[Sequence[str]] = None) -> int:
    p = argparse.ArgumentParser(description="Costruisce il pannello degli indicatori.",
                                formatter_class=argparse.ArgumentDefaultsHelpFormatter)
    p.add_argument("--universo", default="dati/universo_italia.txt")
    p.add_argument("--da", default="2000-01-01")
    p.add_argument("--a", default=pd.Timestamp.today().strftime("%Y-%m-%d"))
    p.add_argument("--cache-prezzi", default="dati/prezzi_grezzi.csv.gz")
    p.add_argument("--cache-mercato", default="dati/indice_ftsemib.csv.gz")
    p.add_argument("--output", default="dati/pannello_italia.csv.gz")
    p.add_argument("--controvalore-minimo", type=float, default=50_000.0,
                   help="Soglia larga per il confronto trasversale, non il filtro operativo.")
    p.add_argument("--aggiorna", action="store_true")
    p.add_argument("--riparazione", action="store_true", help="repair=True di yfinance.")
    p.add_argument("--senza-beta", action="store_true")
    args = p.parse_args(argv)

    logging.basicConfig(level=logging.INFO, format="%(asctime)s | %(levelname)-7s | %(message)s",
                        datefmt="%H:%M:%S")

    with open(args.universo, encoding="utf-8") as fh:
        tickers = [r.strip() for r in fh if r.strip() and not r.startswith("#")]
    LOGGER.info("Universo: %d ticker da %s", len(tickers), args.universo)

    grezzi = mod_prezzi.carica(tickers, args.da, args.a, args.cache_prezzi,
                               args.aggiorna, args.riparazione)
    LOGGER.info("Prezzi grezzi: %d righe, %d ticker", len(grezzi), grezzi.ticker.nunique())
    puliti = mod_prezzi.sanifica(grezzi)
    LOGGER.info("Dopo la pulizia: %d righe, %d ticker", len(puliti), puliti.ticker.nunique())

    mercato = None if args.senza_beta else serie_mercato(args.da, args.a, args.cache_mercato)
    dati = indicatori.calcola(puliti, mercato, args.controvalore_minimo)
    riepilogo(dati)

    os.makedirs(os.path.dirname(args.output) or ".", exist_ok=True)
    dati.to_csv(args.output, index=False, compression="gzip")
    LOGGER.info("Scritto %s (%.1f MB, %d colonne)",
                args.output, os.path.getsize(args.output) / 1e6, len(dati.columns))
    print("\nColonne del pannello:")
    for col in dati.columns:
        print("  %s" % col)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
