#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
universo.py — l'elenco completo delle azioni quotate a Milano.

A differenza delle liste dei costituenti di un indice (40 titoli per il FTSE MIB),
qui si prende **tutto il listino**. La fonte non e' Wikipedia ma il product
directory di Euronext, che espone un gateway JSON dietro la pagina
"Equities list":

    POST https://live.euronext.com/en/product_directory/data/stocks-all-places?mics=...

I MIC italiani sono due:
  MTAA  Euronext Milan (il mercato principale, segmento STAR compreso)
  EXGM  Euronext Growth Milan (l'ex AIM Italia)

Da NON includere, anche se la pagina li offre:
  MTAH  Trading After Hours: azioni **estere** scambiate a Milano fuori orario
        (3M, 3D Systems...). Non sono titoli italiani.
  ETLX  EuroTLX: in massima parte obbligazioni.
  MERK  e' il Merkur Market di **Oslo**, non di Milano. Trappola facile: sta
        nella stessa lista e ha 87 righe con ISIN norvegesi.

Il simbolo Euronext piu' il suffisso `.MI` **e' esattamente** il ticker Yahoo:
verificato sui 40 del FTSE MIB, 40 corrispondenze su 40, compresi i casi non
ovvi (STMMI, STLAM, TIT, PST). Nessuna tabella di traduzione da mantenere.

Uso:
    python universo.py                      # scarica, valida su Yahoo e salva
    python universo.py --no-verifica        # solo l'elenco Euronext, senza Yahoo
"""

from __future__ import annotations

import argparse
import json
import logging
import re
from typing import Dict, List, Optional, Sequence

import requests

LOGGER = logging.getLogger("universo")

GATEWAY = "https://live.euronext.com/en/product_directory/data/stocks-all-places"
MIC_ITALIANI = ("MTAA", "EXGM", "MIVX")
BENCHMARK_PREZZO = "FTSEMIB.MI"      # indice price: NON contiene i dividendi
BENCHMARK_TOTAL_RETURN = "IMIB.MI"   # ETF sul FTSE MIB: l'Adj Close e' total return, dal 2008

UA = ("Mozilla/5.0 (X11; Linux x86_64) AppleWebKit/537.36 "
      "(KHTML, like Gecko) Chrome/120 Safari/537.36")
PAGINA = 100    # il gateway ignora length oltre qualche centinaio: si pagina

# I warrant stanno nella stessa lista delle azioni ma non sono azioni: hanno una
# scadenza, un prezzo che e' un premio e volumi quasi nulli. Su Milano il nome
# segue sempre lo schema "W EMITTENTE 24-27" o "WARR EMITTENTE 23-28".
#
# Il pattern deve restare stretto. Una versione piu' larga (`^W[A-Z]{2,}\s`)
# classificava come warrant anche "WEBUILD RSP", che sono le azioni di risparmio
# di Webuild: una classe azionaria vera, con vent'anni di prezzi.
_WARRANT = re.compile(r"^(W|WARR|WARRANT)[\s.]|\d{2}-\d{2}$")
# Classi azionarie legittime che non vanno mai scartate, qualunque cosa dica il nome.
_CLASSI_AZIONARIE = re.compile(r"\b(RSP|RISP|PRIV|PRIVIL)\b")


def _pagina(mics: Sequence[str], start: int) -> Dict:
    corpo = "draw=1&start=%d&length=%d&iDisplayLength=%d&iDisplayStart=%d" % (
        start, PAGINA, PAGINA, start)
    resp = requests.post(
        GATEWAY, params={"mics": ",".join(mics)}, data=corpo, timeout=90,
        headers={"User-Agent": UA, "X-Requested-With": "XMLHttpRequest",
                 "Content-Type": "application/x-www-form-urlencoded",
                 "Referer": "https://live.euronext.com/en/products/equities/list"})
    resp.raise_for_status()
    return resp.json()


def _testo(html: str) -> str:
    return re.sub(r"<[^>]+>", "", html).replace("&amp;", "&").strip()


def listino(mics: Sequence[str] = MIC_ITALIANI) -> List[Dict[str, str]]:
    """Tutte le righe quotate sui mercati indicati, warrant compresi.

    Restituisce dizionari con ticker Yahoo, ISIN, nome e MIC del mercato.
    """
    righe: Dict[str, Dict[str, str]] = {}
    start, atteso = 0, None
    while atteso is None or start < atteso:
        dati = _pagina(mics, start)
        atteso = atteso or int(dati["iTotalRecords"])
        blocco = dati.get("aaData") or []
        if not blocco:
            break     # difesa: se il gateway smette di restituire righe non si cicla
        for r in blocco:
            simbolo, isin, mic = r[2].strip(), r[1].strip(), _testo(r[3])
            if mic not in mics:
                continue
            righe[isin + mic] = {"ticker": simbolo + ".MI", "isin": isin,
                                 "nome": _testo(r[0]), "mercato": mic}
        start += PAGINA
        LOGGER.debug("Euronext: %d/%s righe", len(righe), atteso)
    LOGGER.info("Euronext: %d righe sui mercati %s (dichiarate %s)",
                len(righe), "+".join(mics), atteso)
    return sorted(righe.values(), key=lambda r: r["ticker"])


def _e_warrant(nome: str) -> bool:
    nome = nome.upper()
    return bool(_WARRANT.search(nome)) and not _CLASSI_AZIONARIE.search(nome)


def separa_warrant(righe: Sequence[Dict[str, str]]) -> tuple:
    """Divide le azioni dai warrant, in base al nome."""
    azioni = [r for r in righe if not _e_warrant(r["nome"])]
    warrant = [r for r in righe if _e_warrant(r["nome"])]
    LOGGER.info("Azioni: %d. Warrant scartati: %d.", len(azioni), len(warrant))
    return azioni, warrant


def controlla_warrant(warrant: Sequence[Dict[str, str]], sedute_sospette: int = 60) -> None:
    """Segnala i presunti warrant che hanno troppa storia per essere warrant.

    E' la rete di sicurezza sulla classificazione per nome: un warrant vero
    scambia pochissimo e per definizione scade, quindi se ne trova uno con mesi
    di prezzi e' quasi certamente un'azione classificata male.
    """
    esito = verifica_su_yahoo(warrant)
    sospetti = [r for r in esito if r["sedute"] >= sedute_sospette]
    if sospetti:
        LOGGER.warning("Presunti warrant con oltre %d sedute, da controllare a mano: %s",
                       sedute_sospette,
                       ", ".join("%s (%s, %d sedute)" % (r["ticker"], r["nome"], r["sedute"])
                                 for r in sospetti))
    else:
        LOGGER.info("Nessun presunto warrant ha piu' di %d sedute: classificazione coerente.",
                    sedute_sospette)


def verifica_su_yahoo(righe: Sequence[Dict[str, str]], inizio: str = "2000-01-01",
                      minimo_sedute: int = 252) -> List[Dict[str, str]]:
    """Aggiunge a ogni riga le sedute disponibili su Yahoo e la prima data.

    `minimo_sedute` non filtra qui: l'informazione viene scritta nell'anagrafica
    e il filtro si applica **giorno per giorno** nel passaggio dei segnali. Un
    titolo quotato da sei mesi oggi ne avra' abbastanza fra un anno, e togliere
    dalla lista i suoi dati significherebbe non poterlo mai piu' reinserire.
    """
    import warnings
    warnings.filterwarnings("ignore")
    logging.getLogger("yfinance").setLevel(logging.CRITICAL)
    import pandas as pd
    import yfinance as yf

    out = []
    tickers = [r["ticker"] for r in righe]
    for i in range(0, len(tickers), 40):
        blocco = tickers[i:i + 40]
        LOGGER.info("Verifica %d-%d di %d su Yahoo...", i + 1, min(i + 40, len(tickers)), len(tickers))
        dati = yf.download(blocco, start=inizio, progress=False, auto_adjust=True,
                           group_by="ticker", threads=True)
        for riga in righe[i:i + 40]:
            try:
                serie = dati[riga["ticker"]]["Close"].dropna()
            except Exception:
                serie = pd.Series(dtype=float)
            riga = dict(riga)
            riga["sedute"] = len(serie)
            riga["prima_seduta"] = str(serie.index.min().date()) if len(serie) else ""
            out.append(riga)

    con_prezzi = [r for r in out if r["sedute"] > 0]
    stagionati = [r for r in con_prezzi if r["sedute"] >= minimo_sedute]
    LOGGER.info("Con prezzi su Yahoo: %d su %d. Con almeno %d sedute: %d.",
                len(con_prezzi), len(out), minimo_sedute, len(stagionati))
    senza = [r["ticker"] for r in out if r["sedute"] == 0]
    if senza:
        LOGGER.warning("Senza prezzi: %s", ", ".join(senza))
    return out


def salva(righe: Sequence[Dict[str, str]], path_lista: str, path_anagrafica: str,
          minimo_sedute: int = 1) -> None:
    """Scrive la lista dei ticker (per lo scarico) e l'anagrafica completa."""
    import pandas as pd

    tenuti = [r for r in righe if r.get("sedute", 1) >= minimo_sedute]
    with open(path_lista, "w", encoding="utf-8") as fh:
        fh.write("# Azioni quotate a Milano: Euronext Milan (MTAA) + Euronext Growth Milan (EXGM).\n")
        fh.write("# Fonte: %s\n" % GATEWAY)
        fh.write("# %d ticker, warrant esclusi. Benchmark: %s (price), %s (total return).\n"
                 % (len(tenuti), BENCHMARK_PREZZO, BENCHMARK_TOTAL_RETURN))
        fh.write("# ATTENZIONE: listino di OGGI, non point-in-time. I delistati mancano,\n")
        fh.write("# quindi c'e' survivorship bias, pesante sulla parte Growth.\n")
        fh.write("\n".join(r["ticker"] for r in tenuti) + "\n")
    pd.DataFrame(righe).to_csv(path_anagrafica, index=False)
    LOGGER.info("Scritti %s (%d ticker) e %s (%d righe)",
                path_lista, len(tenuti), path_anagrafica, len(righe))


def main(argv: Optional[Sequence[str]] = None) -> int:
    p = argparse.ArgumentParser(description="Elenco completo delle azioni quotate a Milano.",
                                formatter_class=argparse.ArgumentDefaultsHelpFormatter)
    p.add_argument("--lista", default="dati/universo_italia.txt")
    p.add_argument("--anagrafica", default="dati/listino_euronext.csv")
    p.add_argument("--no-verifica", action="store_true", help="Salta il controllo su Yahoo.")
    p.add_argument("--minimo-sedute", type=int, default=1,
                   help="Sedute minime su Yahoo per entrare nella lista da scaricare.")
    args = p.parse_args(argv)

    logging.basicConfig(level=logging.INFO, format="%(asctime)s | %(levelname)-7s | %(message)s",
                        datefmt="%H:%M:%S")
    righe = listino()
    azioni, warrant = separa_warrant(righe)
    if not args.no_verifica:
        azioni = verifica_su_yahoo(azioni)
        controlla_warrant(warrant)
    salva(azioni, args.lista, args.anagrafica, args.minimo_sedute)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
