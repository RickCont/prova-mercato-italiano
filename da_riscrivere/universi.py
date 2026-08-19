#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
universi.py — liste dei costituenti dei principali indici, per Yahoo Finance.

Gli elenchi vengono ricavati dalle pagine Wikipedia degli indici e tradotti nei
simboli Yahoo (che aggiungono un suffisso di borsa: `.MI` Milano, `.DE`
Francoforte, `.PA` Parigi, `.T` Tokyo, `.HK` Hong Kong, `.NS` India...).

Nota: sono liste **correnti**, non point-in-time. Per una strategia tecnica su
un indice ampio l'effetto e' un survivorship bias moderato; e' comunque
dichiarato dal codice che le usa.

Uso:
    python universi.py --mercato ftsemib          # stampa e salva la lista
    python universi.py --mercato tutti --verifica # controlla quali hanno prezzi
"""

from __future__ import annotations

import argparse
import logging
import re
from typing import Dict, List, Optional, Sequence

import requests

LOGGER = logging.getLogger("universi")

# Suffissi di borsa gia' in formato Yahoo: se un simbolo ne ha uno, e' completo.
SUFFISSI_BORSA = (".MI", ".DE", ".PA", ".AS", ".BR", ".L", ".SW", ".MC", ".LS",
                  ".VI", ".ST", ".OL", ".CO", ".HE", ".IR", ".T", ".HK", ".NS", ".KS")

UA = ("Mozilla/5.0 (X11; Linux x86_64) AppleWebKit/537.36 "
      "(KHTML, like Gecko) Chrome/120 Safari/537.36")

# Per ogni mercato: pagina Wikipedia, suffisso Yahoo e indice di riferimento.
# La colonna col ticker NON e' fissata: le pagine cambiano struttura nel tempo,
# quindi viene riconosciuta provando le colonne candidate contro Yahoo.
MERCATI: Dict[str, Dict[str, object]] = {
    "sp500": {
        "url": "https://en.wikipedia.org/wiki/List_of_S%26P_500_companies",
        "colonna": 0, "suffisso": "", "benchmark": "^GSPC", "nome": "S&P 500 (USA)",
    },
    "ftsemib": {
        "url": "https://en.wikipedia.org/wiki/FTSE_MIB",
        "colonna": 1, "suffisso": ".MI", "benchmark": "FTSEMIB.MI", "nome": "FTSE MIB (Italia)",
    },
    "dax": {
        "url": "https://en.wikipedia.org/wiki/DAX",
        "colonna": 3, "suffisso": ".DE", "benchmark": "^GDAXI", "nome": "DAX (Germania)",
    },
    "cac40": {
        "url": "https://en.wikipedia.org/wiki/CAC_40",
        "colonna": 3, "suffisso": ".PA", "benchmark": "^FCHI", "nome": "CAC 40 (Francia)",
    },
    "ftse100": {
        "url": "https://en.wikipedia.org/wiki/FTSE_100_Index",
        "colonna": 1, "suffisso": ".L", "benchmark": "^FTSE", "nome": "FTSE 100 (Regno Unito)",
    },
    # Nota: la pagina Wikipedia del Nikkei 225 NON elenca i costituenti (contiene
    # la serie storica dell'indice), quindi il Giappone va fornito a mano con
    # --universe-file: un file con i codici a 4 cifre piu' il suffisso .T
    # (7203.T Toyota, 6758.T Sony, ...). I prezzi Yahoo ci sono dal 1999.
    "nifty": {
        "url": "https://en.wikipedia.org/wiki/NIFTY_50",
        "colonna": 1, "suffisso": ".NS", "benchmark": "^NSEI", "nome": "Nifty 50 (India)",
    },
    "hangseng": {
        "url": "https://en.wikipedia.org/wiki/Hang_Seng_Index",
        "colonna": 0, "suffisso": ".HK", "benchmark": "^HSI", "nome": "Hang Seng (Hong Kong)",
    },
}


def _scarica(url: str) -> str:
    resp = requests.get(url, headers={"User-Agent": UA}, timeout=30)
    resp.raise_for_status()
    return resp.text


def _tabelle(html: str) -> List[List[List[str]]]:
    """Estrae tutte le tabelle come liste di righe di celle testuali."""
    tabelle = []
    for blocco in re.findall(r"<table[^>]*>(.*?)</table>", html, re.S):
        righe = []
        for riga in re.findall(r"<tr[^>]*>(.*?)</tr>", blocco, re.S):
            celle = [re.sub(r"<[^>]+>", "", c).replace("&amp;", "&").strip()
                     for c in re.findall(r"<t[dh][^>]*>(.*?)</t[dh]>", riga, re.S)]
            if celle:
                righe.append(celle)
        if len(righe) > 5:
            tabelle.append(righe)
    return tabelle


def _normalizza(simbolo: str, suffisso: str) -> Optional[str]:
    """Traduce il simbolo della pagina nel formato Yahoo."""
    grezzo = simbolo.strip()
    if not grezzo or len(grezzo) > 14:
        return None
    # I nomi di societa' contengono spazi o minuscole: non sono ticker.
    if " " in grezzo or any(c.islower() for c in grezzo):
        return None
    simbolo = grezzo.upper().split(":")[-1]          # "BIT: ENI" -> "ENI"

    if suffisso == ".HK":
        # Hong Kong usa codici numerici, su Yahoo a 4 cifre con zeri iniziali.
        cifre = re.sub(r"\D", "", simbolo)
        return cifre.zfill(4) + ".HK" if cifre else None
    if suffisso == ".T":
        cifre = re.sub(r"\D", "", simbolo)
        return cifre + ".T" if len(cifre) == 4 else None

    if not re.fullmatch(r"[A-Z0-9][A-Z0-9.\-]{0,9}", simbolo):
        return None
    if suffisso == "":
        # Yahoo usa il trattino per le share class americane: BRK.B -> BRK-B.
        return simbolo.replace(".", "-")
    # Alcune pagine riportano gia' il simbolo in formato Yahoo (es. "AC.PA"),
    # e le societa' quotate su un'altra borsa portano il suffisso di quella
    # (Airbus e' AIR.PA anche dentro il DAX): in entrambi i casi non si aggiunge
    # nulla, altrimenti si ottengono mostri come "AIR.PA.DE".
    if any(simbolo.endswith(s) for s in SUFFISSI_BORSA):
        return simbolo
    return simbolo + suffisso


def _sono_progressivi(simboli: Sequence[str]) -> bool:
    """True se i simboli sono in maggioranza numeri consecutivi."""
    numeri = sorted({int(n) for n in
                     (re.sub(r"\D", "", s) for s in simboli) if n.isdigit()})
    if len(numeri) < 5 or len(numeri) < 0.8 * len(set(simboli)):
        return False
    consecutivi = sum(1 for a, b in zip(numeri, numeri[1:]) if b - a == 1)
    return consecutivi > 0.7 * (len(numeri) - 1)


def _colonne_candidate(html: str, suffisso: str) -> List[List[str]]:
    """Tutte le colonne di tutte le tabelle che sembrano contenere ticker.

    Ordinate per plausibilita': quante celle producono un simbolo valido e
    quanto sono corte (i ticker sono brevi, i nomi no).
    """
    candidate = []
    for righe in _tabelle(html):
        n_colonne = max(len(r) for r in righe)
        for col in range(n_colonne):
            simboli, celle = [], 0
            for riga in righe:
                if len(riga) <= col:
                    continue
                celle += 1
                simbolo = _normalizza(riga[col], suffisso)
                if simbolo:
                    simboli.append(simbolo)
            if celle < 5 or len(simboli) < 0.5 * celle or len(set(simboli)) < 5:
                continue
            if _sono_progressivi(simboli):
                # Colonne di anni o di numerazione delle righe: alcuni valori
                # esistono per caso come codici di borsa (la pagina del Nikkei
                # elenca gli anni 1914, 1915, ... e non i costituenti).
                continue
            lunghezza_media = sum(len(s) for s in simboli) / len(simboli)
            candidate.append((len(set(simboli)), -lunghezza_media, sorted(set(simboli))))
    candidate.sort(reverse=True)
    return [c[2] for c in candidate]


def costituenti(mercato: str, valida: bool = True) -> List[str]:
    """Costituenti correnti dell'indice, in simboli Yahoo.

    Con `valida=True` la colonna giusta viene scelta provando un campione di
    simboli su Yahoo: e' l'unico criterio affidabile, perche' le pagine
    Wikipedia cambiano struttura e la colonna "ticker" non e' sempre la stessa.
    """
    if mercato not in MERCATI:
        raise ValueError("Mercato sconosciuto: %s (scegli fra %s)" % (mercato, ", ".join(MERCATI)))
    conf = MERCATI[mercato]
    html = _scarica(str(conf["url"]))
    candidate = _colonne_candidate(html, str(conf["suffisso"]))
    if not candidate:
        raise ValueError("Nessuna colonna di ticker riconosciuta nella pagina.")
    if not valida:
        return candidate[0]

    # Si valutano tutte le colonne plausibili e si tiene quella con la quota
    # piu' alta di simboli realmente quotati: fermarsi alla prima sopra soglia
    # sceglieva colonne di numeri progressivi che per caso esistono come ticker.
    risultati = []
    for tentativo, simboli in enumerate(candidate[:5], start=1):
        campione = simboli[:: max(1, len(simboli) // 10)][:10]
        esito = verifica_prezzi(campione, inizio="2023-01-01")
        quota = len(esito["ok"]) / max(1, len(campione))
        LOGGER.debug("%s: colonna %d -> %d simboli, campione valido al %.0f%%",
                     mercato, tentativo, len(simboli), quota * 100)
        risultati.append((quota, len(simboli), tentativo, simboli))
    risultati.sort(reverse=True)
    quota, n, tentativo, simboli = risultati[0]
    if quota < 0.6:
        LOGGER.warning("%s: la colonna migliore valida solo il %.0f%% del campione.",
                       conf["nome"], quota * 100)
    LOGGER.info("%s: %d costituenti (colonna %d, campione valido al %.0f%%)",
                conf["nome"], n, tentativo, quota * 100)
    return simboli


def verifica_prezzi(tickers: Sequence[str], inizio: str = "2015-01-01") -> Dict[str, List[str]]:
    """Separa i ticker che hanno prezzi su Yahoo da quelli che non li hanno."""
    import warnings
    warnings.filterwarnings("ignore")
    logging.getLogger("yfinance").setLevel(logging.CRITICAL)
    import yfinance as yf

    ok, ko = [], []
    for i in range(0, len(tickers), 40):
        blocco = list(tickers[i:i + 40])
        dati = yf.download(blocco, start=inizio, progress=False, auto_adjust=True,
                           group_by="ticker", threads=True)
        for t in blocco:
            try:
                serie = dati[t]["Close"].dropna()
                (ok if len(serie) > 100 else ko).append(t)
            except Exception:
                ko.append(t)
    return {"ok": sorted(ok), "ko": sorted(ko)}


def salva(mercato: str, tickers: Sequence[str], path: Optional[str] = None) -> str:
    path = path or "universo_%s.txt" % mercato
    conf = MERCATI[mercato]
    with open(path, "w", encoding="utf-8") as fh:
        fh.write("# %s - costituenti correnti, simboli Yahoo Finance\n" % conf["nome"])
        fh.write("# benchmark: %s\n" % conf["benchmark"])
        fh.write("# ATTENZIONE: lista non point-in-time -> survivorship bias.\n")
        fh.write("\n".join(tickers) + "\n")
    return path


def main(argv: Optional[Sequence[str]] = None) -> int:
    p = argparse.ArgumentParser(description="Costruisce le liste dei costituenti degli indici.")
    p.add_argument("--mercato", default="ftsemib",
                   help="Uno fra: %s, oppure 'tutti'." % ", ".join(MERCATI))
    p.add_argument("--verifica", action="store_true", help="Controlla quali ticker hanno prezzi su Yahoo.")
    p.add_argument("--output", help="File di output (default: universo_<mercato>.txt).")
    args = p.parse_args(argv)

    logging.basicConfig(level=logging.INFO, format="%(asctime)s | %(levelname)-7s | %(message)s",
                        datefmt="%H:%M:%S")
    mercati = list(MERCATI) if args.mercato == "tutti" else [args.mercato]
    for mercato in mercati:
        try:
            tickers = costituenti(mercato)
        except Exception as exc:
            LOGGER.error("%s: impossibile ricavare i costituenti (%s)", mercato, exc)
            continue
        if args.verifica and tickers:
            esito = verifica_prezzi(tickers)
            LOGGER.info("%s: %d con prezzi, %d senza (%s)", mercato, len(esito["ok"]), len(esito["ko"]),
                        ", ".join(esito["ko"][:6]))
            tickers = esito["ok"]
        if tickers:
            path = salva(mercato, tickers, args.output if len(mercati) == 1 else None)
            LOGGER.info("%s -> %s (%d ticker)", mercato, path, len(tickers))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
