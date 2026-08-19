#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
build_technical_signals.py — trasforma una regola tecnica in un file di segnali.

Produce lo **stesso schema** di `raccomandazioni_storiche.csv`
(data_articolo, ticker, rank, rating_score, recommendation), cosi' il
backtester gia' scritto funziona senza modifiche: cambia solo cosa sceglie i
titoli, non la meccanica di acquisto, take profit, stop loss e
de-duplicazione.

Regole disponibili:
  caduta        il titolo ha perso almeno X% negli ultimi N giorni
  dal-massimo   il titolo sta almeno X% sotto il massimo delle 52 settimane
  rsi           RSI sotto la soglia (ipervenduto)
  giorni-rossi  almeno N sedute negative consecutive
  momentum      il titolo ha guadagnato di piu' negli ultimi N giorni (l'opposto)

Il punteggio (`rating_score`) e' l'intensita' del segnale riportata sulla scala
1-5, in modo che il `rank` metta per primo il segnale piu' forte.

Esempi:
    python build_technical_signals.py --prezzi prezzi_ftsemib.csv.gz \\
        --regola caduta --soglia 10 --orizzonte 5

    python build_technical_signals.py --prezzi prezzi_sp500.csv.gz \\
        --regola dal-massimo --soglia 20 --frequenza giornaliera
"""

from __future__ import annotations

import argparse
import logging
from typing import Optional, Sequence

import numpy as np
import pandas as pd

LOGGER = logging.getLogger("segnali")

COLONNE_OUTPUT = ["data_articolo", "ticker", "rank", "rating_score", "recommendation"]

# Ogni regola: colonna del dataset, verso del confronto, e se il segnale e'
# tanto piu' forte quanto piu' il valore e' basso (ribassi) o alto (momentum).
REGOLE = {
    "caduta":       {"colonna": "var_{orizzonte}g", "direzione": "sotto", "etichetta": "calo"},
    "dal-massimo":  {"colonna": "dal_massimo_52s", "direzione": "sotto", "etichetta": "sotto il massimo"},
    "rsi":          {"colonna": "rsi_14", "direzione": "sotto", "etichetta": "RSI"},
    "giorni-rossi": {"colonna": "giorni_rossi_consecutivi", "direzione": "sopra", "etichetta": "sedute rosse"},
    "momentum":     {"colonna": "var_{orizzonte}g", "direzione": "sopra", "etichetta": "rialzo"},
}

ORIZZONTI_VALIDI = (1, 5, 20, 60, 252)


def _colonna_regola(regola: str, orizzonte: int) -> str:
    modello = str(REGOLE[regola]["colonna"])
    return modello.format(orizzonte=orizzonte)


def date_di_selezione(dati: pd.DataFrame, frequenza: str) -> pd.DatetimeIndex:
    """Giorni in cui la strategia puo' comprare."""
    giorni = pd.DatetimeIndex(sorted(dati["data"].unique()))
    if frequenza == "giornaliera":
        return giorni
    # Mensile: il primo giorno di borsa di ogni mese, come nel dataset delle
    # raccomandazioni, cosi' i due dataset sono confrontabili.
    serie = pd.Series(giorni, index=giorni)
    return pd.DatetimeIndex(serie.groupby([giorni.year, giorni.month]).first().values)


def genera(dati: pd.DataFrame, regola: str, soglia: float, orizzonte: int,
           frequenza: str, top_n: int, min_prezzo: float = 1.0) -> pd.DataFrame:
    """Applica la regola e produce la classifica dei candidati per ogni data."""
    conf = REGOLE[regola]
    colonna = _colonna_regola(regola, orizzonte)
    if colonna not in dati.columns:
        raise ValueError("Colonna %s assente: rigenera il dataset dei prezzi." % colonna)

    selezione = date_di_selezione(dati, frequenza)
    quadro = dati[dati["data"].isin(selezione)].copy()
    # Titoli troppo economici o senza prezzo non sono investibili in pratica.
    quadro = quadro[(quadro["chiusura"] >= min_prezzo) & quadro[colonna].notna()]

    if conf["direzione"] == "sotto":
        # Per "caduta" e "dal-massimo" la soglia si intende in negativo:
        # --soglia 10 significa "ha perso almeno il 10%".
        limite = -abs(soglia) / 100.0 if colonna.startswith(("var_", "dal_")) else soglia
        candidati = quadro[quadro[colonna] <= limite].copy()
        candidati["intensita"] = -candidati[colonna]      # piu' e' sceso, piu' e' forte
    else:
        limite = abs(soglia) / 100.0 if colonna.startswith("var_") else soglia
        candidati = quadro[quadro[colonna] >= limite].copy()
        candidati["intensita"] = candidati[colonna]

    if candidati.empty:
        raise ValueError("Nessun segnale con questi parametri: prova una soglia meno severa.")

    candidati = candidati.sort_values(["data", "intensita", "ticker"],
                                      ascending=[True, False, True], kind="mergesort")
    top = candidati.groupby("data", sort=True, group_keys=False).head(top_n).copy()
    top["rank"] = top.groupby("data").cumcount() + 1

    # L'intensita' viene riportata sulla scala 1-5 usata dal backtester: 5 al
    # segnale piu' forte osservato, 1 al piu' debole. E' solo una convenzione di
    # formato, l'ordinamento e' gia' fissato dal rank.
    minimo, massimo = top["intensita"].min(), top["intensita"].max()
    ampiezza = (massimo - minimo) or 1.0
    top["rating_score"] = (1.0 + 4.0 * (top["intensita"] - minimo) / ampiezza).round(4)

    etichetta = str(conf["etichetta"])
    out = pd.DataFrame({
        "data_articolo": top["data"].dt.strftime("%Y-%m-%d"),
        "ticker": top["ticker"],
        "rank": top["rank"].astype(int),
        "rating_score": top["rating_score"],
        "recommendation": ["%s %.1f%%" % (etichetta, v * 100) if abs(v) < 10 else "%s %.0f" % (etichetta, v)
                           for v in top["intensita"]],
    })
    return out.reset_index(drop=True)


def riepilogo(segnali: pd.DataFrame, top_n: int) -> None:
    per_data = segnali.groupby("data_articolo").size()
    LOGGER.info("Segnali: %d righe su %d date (%s -> %s)",
                len(segnali), len(per_data), per_data.index.min(), per_data.index.max())
    LOGGER.info("Candidati per data: min %d, mediana %d, max %d (richiesti %d)",
                per_data.min(), int(per_data.median()), per_data.max(), top_n)
    vuote = int((per_data < top_n).sum())
    if vuote:
        LOGGER.warning("%d date su %d hanno meno di %d candidati: in quei mesi la "
                       "strategia restera' parzialmente liquida.", vuote, len(per_data), top_n)
    LOGGER.info("Titoli distinti coinvolti: %d", segnali.ticker.nunique())


def main(argv: Optional[Sequence[str]] = None) -> int:
    p = argparse.ArgumentParser(
        description="Genera un file di segnali tecnici nel formato del backtester.",
        formatter_class=argparse.ArgumentDefaultsHelpFormatter)
    p.add_argument("--prezzi", default="prezzi_ftsemib.csv.gz", help="Dataset da build_price_dataset.py.")
    p.add_argument("--regola", choices=tuple(REGOLE), default="caduta")
    p.add_argument("--soglia", type=float, default=10.0,
                   help="Percentuale per caduta/dal-massimo/momentum; valore assoluto per rsi e giorni-rossi.")
    p.add_argument("--orizzonte", type=int, default=5, choices=ORIZZONTI_VALIDI,
                   help="Giorni su cui misurare la variazione (regole caduta e momentum).")
    p.add_argument("--frequenza", choices=("mensile", "giornaliera"), default="mensile",
                   help="Quando la strategia puo' comprare.")
    p.add_argument("--top-n", type=int, default=25, help="Candidati per data (margine per la de-duplicazione).")
    p.add_argument("--min-prezzo", type=float, default=1.0, help="Esclude i titoli sotto questo prezzo.")
    p.add_argument("--output", default="segnali_tecnici.csv")
    p.add_argument("-q", "--quiet", action="store_true")
    args = p.parse_args(argv)

    logging.basicConfig(level=logging.WARNING if args.quiet else logging.INFO,
                        format="%(asctime)s | %(levelname)-7s | %(message)s", datefmt="%H:%M:%S")

    # Su un mercato ampio il dataset supera i due milioni di righe: si leggono
    # solo le colonne che la regola usa davvero, altrimenti la memoria esplode.
    colonne = ["data", "ticker", "chiusura", _colonna_regola(args.regola, args.orizzonte)]
    dati = pd.read_csv(args.prezzi, parse_dates=["data"], usecols=sorted(set(colonne)),
                       dtype={"ticker": "category"})
    dati["ticker"] = dati["ticker"].astype(str)
    LOGGER.info("Prezzi: %d righe, %d ticker, %s -> %s",
                len(dati), dati.ticker.nunique(), dati.data.min().date(), dati.data.max().date())

    segnali = genera(dati, args.regola, args.soglia, args.orizzonte,
                     args.frequenza, args.top_n, args.min_prezzo)
    riepilogo(segnali, args.top_n)
    segnali.to_csv(args.output, index=False)
    LOGGER.info("Scritto %s", args.output)
    print()
    print(segnali.head(8).to_string(index=False))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
