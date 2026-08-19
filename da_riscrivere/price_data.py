#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
price_data.py — cache locale dei prezzi giornalieri per il backtester.

Scarica una sola volta da Yahoo Finance le serie dei ticker che compaiono nel
dataset delle raccomandazioni e le salva in `.cache/prezzi.csv.gz` (formato
lungo: date, ticker, open, high, low, close). Le serie sono **aggiustate per split e
dividendi** (`auto_adjust=True`), quindi rappresentano il total return: senza
questo, uno split 4:1 farebbe scattare stop loss inesistenti.

Salva anche il cambio EUR/USD, necessario perche' i versamenti sono in euro
mentre i titoli quotano in dollari.

Uso diretto (per pre-scaricare la cache):
    python price_data.py --reco raccomandazioni_storiche.csv
"""

from __future__ import annotations

import argparse
import logging
import os
import warnings
from typing import List, Optional, Sequence

import pandas as pd

import universi

LOGGER = logging.getLogger("price_data")

PRICES_FILE = "prezzi.csv.gz"
FX_FILE = "cambio_eurusd.csv.gz"
CHUNK = 40


def _yfinance():
    """Import pigro + silenziamento dei 404 attesi sui delistati."""
    warnings.filterwarnings("ignore")
    logging.getLogger("yfinance").setLevel(logging.CRITICAL)
    import yfinance
    return yfinance


def yahoo_symbol(ticker: str) -> str:
    """Traduce un ticker nel formato Yahoo.

    Yahoo usa il trattino per le share class americane (BF.B -> BF-B) ma il
    punto per i suffissi di borsa (A2A.MI, SHEL.L, 7203.T): convertire alla
    cieca romperebbe tutti i mercati non americani.
    """
    ticker = ticker.upper()
    if any(ticker.endswith(s) for s in universi.SUFFISSI_BORSA):
        return ticker
    return ticker.replace(".", "-")


def download_prices(tickers: Sequence[str], start: str, end: str) -> pd.DataFrame:
    """Scarica a blocchi e restituisce un DataFrame lungo (date, ticker, open, high, low, close).

    La colonna `ticker` conserva il simbolo del dataset delle raccomandazioni,
    anche quando su Yahoo si chiama diversamente.
    """
    yf = _yfinance()
    frames: List[pd.DataFrame] = []
    missing: List[str] = []
    tickers = sorted(set(tickers))

    for i in range(0, len(tickers), CHUNK):
        chunk = tickers[i:i + CHUNK]
        simboli = {yahoo_symbol(t): t for t in chunk}
        LOGGER.info("Prezzi %d-%d di %d...", i + 1, min(i + CHUNK, len(tickers)), len(tickers))
        raw = yf.download(list(simboli), start=start, end=end, progress=False,
                          auto_adjust=True, group_by="ticker", threads=True)
        for simbolo, ticker in simboli.items():
            try:
                sub = raw[simbolo][["Open", "High", "Low", "Close"]].dropna(how="all")
            except (KeyError, TypeError):
                missing.append(ticker)
                continue
            if sub.empty:
                missing.append(ticker)
                continue
            sub = sub.reset_index()
            sub.columns = ["date", "open", "high", "low", "close"]
            sub["ticker"] = ticker
            frames.append(sub)

    if missing:
        LOGGER.warning("Nessun prezzo per %d ticker: %s", len(missing), ", ".join(missing[:10]))
    if not frames:
        # Puo' succedere quando si chiede solo un ticker delistato: non e' fatale,
        # il backtester salta i titoli senza prezzo.
        LOGGER.warning("Nessun prezzo scaricato per i ticker richiesti.")
        return pd.DataFrame(columns=["date", "open", "high", "low", "close", "ticker"])

    out = pd.concat(frames, ignore_index=True)
    out["date"] = pd.to_datetime(out["date"], utc=True).dt.tz_localize(None).dt.normalize()
    out = out.dropna(subset=["close"]).sort_values(["ticker", "date"]).reset_index(drop=True)
    LOGGER.info("Prezzi: %d righe, %d ticker, %s -> %s",
                len(out), out.ticker.nunique(), out.date.min().date(), out.date.max().date())
    return out


def download_fx(start: str, end: str) -> pd.DataFrame:
    """Cambio EUR/USD giornaliero (quanti dollari per un euro)."""
    yf = _yfinance()
    fx = yf.Ticker("EURUSD=X").history(start=start, end=end, auto_adjust=False)
    if fx.empty:
        raise RuntimeError("Cambio EUR/USD non scaricato.")
    out = fx.reset_index()[["Date", "Close"]]
    out.columns = ["date", "eurusd"]
    out["date"] = pd.to_datetime(out["date"]).dt.tz_localize(None).dt.normalize()
    LOGGER.info("Cambio EUR/USD: %d giorni, %s -> %s", len(out), out.date.min().date(), out.date.max().date())
    return out


def sanifica(df: pd.DataFrame, minimo_giorni: int = 100) -> pd.DataFrame:
    """Scarta la parte corrotta delle serie aggiustate.

    Sui titoli con raggruppamenti azionari e aumenti di capitale ripetuti (le
    banche italiane sono il caso tipico) l'aggiustamento retroattivo di Yahoo
    degenera: si trovano prezzi di centinaia di milioni e persino **negativi**.
    Un prezzo negativo non esiste, quindi tutto cio' che sta prima dell'ultima
    quotazione impossibile viene buttato e si tiene solo la coda sana.
    """
    if df.empty:
        return df
    colonne = [c for c in ("open", "high", "low", "close") if c in df.columns]
    non_valido = (df[colonne] <= 0).any(axis=1)
    if not non_valido.any():
        return df

    tagliati, rimossi = [], []
    pezzi = []
    for ticker, gruppo in df.groupby("ticker", sort=False):
        g = gruppo.sort_values("date")
        cattive = g.loc[(g[colonne] <= 0).any(axis=1), "date"]
        if cattive.empty:
            pezzi.append(g)
            continue
        g = g[g["date"] > cattive.max()]
        if len(g) < minimo_giorni:
            rimossi.append(ticker)
            continue
        tagliati.append("%s (dal %s)" % (ticker, g["date"].min().date()))
        pezzi.append(g)

    if tagliati:
        LOGGER.warning("Serie corrotte accorciate su %d ticker: %s",
                       len(tagliati), ", ".join(tagliati[:8]))
    if rimossi:
        LOGGER.warning("Ticker scartati, storia inutilizzabile: %s", ", ".join(rimossi))
    return pd.concat(pezzi, ignore_index=True) if pezzi else df.iloc[0:0]


def load_prices(tickers: Sequence[str], start: str, end: str, cache_dir: str = ".cache",
                refresh: bool = False) -> pd.DataFrame:
    """Prezzi dalla cache; scarica solo se manca o se `refresh`."""
    os.makedirs(cache_dir, exist_ok=True)
    path = os.path.join(cache_dir, PRICES_FILE)
    if os.path.exists(path) and not refresh:
        df = pd.read_csv(path, parse_dates=["date"])
        mancanti = sorted(set(tickers) - set(df.ticker.unique()))
        if not mancanti:
            LOGGER.info("Prezzi letti dalla cache: %d righe, %d ticker.", len(df), df.ticker.nunique())
            return sanifica(df)
        LOGGER.info("Cache incompleta: scarico %d ticker mancanti.", len(mancanti))
        nuovi = download_prices(mancanti, start, end)
        if nuovi.empty:
            # Nulla di nuovo da aggiungere: si evita di riscrivere la cache a ogni run.
            LOGGER.warning("%d ticker restano senza prezzi: %s", len(mancanti), ", ".join(mancanti[:10]))
            return df
        df = pd.concat([df, nuovi], ignore_index=True)
    else:
        df = download_prices(tickers, start, end)
    df.to_csv(path, index=False, compression="gzip")
    LOGGER.info("Cache prezzi scritta: %s (%.1f MB)", path, os.path.getsize(path) / 1e6)
    return sanifica(df)


def load_fx(start: str, end: str, cache_dir: str = ".cache", refresh: bool = False) -> pd.DataFrame:
    os.makedirs(cache_dir, exist_ok=True)
    path = os.path.join(cache_dir, FX_FILE)
    if os.path.exists(path) and not refresh:
        return pd.read_csv(path, parse_dates=["date"])
    fx = download_fx(start, end)
    fx.to_csv(path, index=False, compression="gzip")
    return fx


def main(argv: Optional[Sequence[str]] = None) -> int:
    parser = argparse.ArgumentParser(description="Pre-scarica la cache dei prezzi per il backtester.")
    parser.add_argument("--reco", default="raccomandazioni_storiche.csv")
    parser.add_argument("--start", default="2011-06-01")
    parser.add_argument("--end", default=pd.Timestamp.today().strftime("%Y-%m-%d"))
    parser.add_argument("--cache-dir", default=".cache")
    parser.add_argument("--refresh", action="store_true")
    args = parser.parse_args(argv)

    logging.basicConfig(level=logging.INFO, format="%(asctime)s | %(levelname)-7s | %(message)s",
                        datefmt="%H:%M:%S")
    tickers = sorted(pd.read_csv(args.reco).ticker.unique())
    LOGGER.info("Ticker dal dataset: %d", len(tickers))
    load_prices(tickers, args.start, args.end, args.cache_dir, args.refresh)
    load_fx(args.start, args.end, args.cache_dir, args.refresh)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
