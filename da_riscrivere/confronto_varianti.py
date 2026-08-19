#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
confronto_varianti.py — mette a confronto varianti della strategia.

Carica dataset e prezzi una volta sola, poi esegue una serie di scenari e
stampa una tabella comparativa con il piano di accumulo sull'indice come
termine di paragone.

Uso:
    python confronto_varianti.py
    python confronto_varianti.py --commissione-fissa 1 --tasse 26
"""

from __future__ import annotations

import argparse
import logging
from typing import Dict, List, Optional, Sequence, Tuple

import pandas as pd

import backtest_strategy as bt
import metriche as mt
import price_data

LOGGER = logging.getLogger("confronto")


def varianti_tecniche() -> List[Tuple[str, Dict[str, object]]]:
    """Assetti pensati per un segnale tecnico su un indice piccolo.

    Qui il vincolo non e' scegliere i titoli giusti ma **restare investiti**:
    con 40 nomi e un segnale raro, la cassa ferma e' il primo nemico.
    """
    comune = dict(dimensione_posizione="patrimonio")
    return [
        ("originale: vendi tutto a +10%",
         dict(comune, take_profit=10, stop_loss=None)),
        ("nessun take profit",
         dict(comune, take_profit=None, stop_loss=None)),
        ("meta' una volta, resto libero",
         dict(comune, take_profit=10, stop_loss=None, vendita_parziale_pct=50, max_scaglioni=1)),
        ("+ riacquisto (max 3 pacchetti)",
         dict(comune, take_profit=10, stop_loss=None, vendita_parziale_pct=50, max_scaglioni=1,
              consenti_riacquisto=True, max_lotti_per_titolo=3)),
        ("LA TUA: + stop -25% sul resto",
         dict(comune, take_profit=10, stop_loss=25, vendita_parziale_pct=50, max_scaglioni=1,
              consenti_riacquisto=True, max_lotti_per_titolo=3)),
        ("la tua, con 6 pacchetti per titolo",
         dict(comune, take_profit=10, stop_loss=25, vendita_parziale_pct=50, max_scaglioni=1,
              consenti_riacquisto=True, max_lotti_per_titolo=6)),
        ("la tua, reinvestendo subito",
         dict(comune, take_profit=10, stop_loss=25, vendita_parziale_pct=50, max_scaglioni=1,
              consenti_riacquisto=True, max_lotti_per_titolo=3, reinvestimento="subito")),
        ("la tua, su 6 titoli invece di 10",
         dict(comune, take_profit=10, stop_loss=25, vendita_parziale_pct=50, max_scaglioni=1,
              consenti_riacquisto=True, max_lotti_per_titolo=3, n_titoli=6)),
    ]


def varianti_standard() -> List[Tuple[str, Dict[str, object]]]:
    """Gli scenari da confrontare: la regola originale e le modifiche proposte."""
    return [
        ("originale: TP +10%, nessuno stop",
         dict(take_profit=10, stop_loss=None)),
        ("originale + stop loss -20%",
         dict(take_profit=10, stop_loss=20)),
        ("1. nessun take profit (solo segnale)",
         dict(take_profit=None, stop_loss=None)),
        ("2. trailing stop -20%, nessun TP",
         dict(take_profit=None, stop_loss=None, trailing_stop=20)),
        ("2b. trailing stop -30%, nessun TP",
         dict(take_profit=None, stop_loss=None, trailing_stop=30)),
        ("3. TP +10% ma reinvesti subito",
         dict(take_profit=10, stop_loss=None, reinvestimento="subito")),
        ("4. TP +10% su 5 titoli",
         dict(take_profit=10, stop_loss=None, n_titoli=5)),
        ("4b. nessun TP su 5 titoli",
         dict(take_profit=None, stop_loss=None, n_titoli=5)),
        ("5. TP +20%, trailing -25%, subito",
         dict(take_profit=20, stop_loss=None, trailing_stop=25, reinvestimento="subito")),
        ("6. vendi META' a ogni +10%",
         dict(take_profit=10, stop_loss=None, vendita_parziale_pct=50)),
        ("7. vendi UN TERZO a ogni +10%",
         dict(take_profit=10, stop_loss=None, vendita_parziale_pct=33)),
        ("8. riacquisto stesso titolo (max 3)",
         dict(take_profit=10, stop_loss=None, consenti_riacquisto=True, max_lotti_per_titolo=3)),
        ("9. meta' + riacquisto (la proposta)",
         dict(take_profit=10, stop_loss=None, vendita_parziale_pct=50,
              consenti_riacquisto=True, max_lotti_per_titolo=3)),
        ("10. meta' + riacquisto + subito",
         dict(take_profit=10, stop_loss=None, vendita_parziale_pct=50,
              consenti_riacquisto=True, max_lotti_per_titolo=3, reinvestimento="subito")),
        ("11. vendi un quarto + riacquisto",
         dict(take_profit=10, stop_loss=None, vendita_parziale_pct=25,
              consenti_riacquisto=True, max_lotti_per_titolo=3)),
        ("12. meta' UNA VOLTA, il resto corre libero",
         dict(take_profit=10, stop_loss=None, vendita_parziale_pct=50, max_scaglioni=1)),
        ("13. come sopra + riacquisto",
         dict(take_profit=10, stop_loss=None, vendita_parziale_pct=50, max_scaglioni=1,
              consenti_riacquisto=True, max_lotti_per_titolo=3)),
        ("14. come sopra + stop -25% sul resto",
         dict(take_profit=10, stop_loss=25, vendita_parziale_pct=50, max_scaglioni=1,
              consenti_riacquisto=True, max_lotti_per_titolo=3)),
    ]


def applica_tasse(ris: bt.Risultato, aliquota: float) -> float:
    """Valore finale al netto dell'imposta sulle plusvalenze realizzate.

    Approssimazione a compensazione piena: si tassa il guadagno netto
    complessivo delle operazioni chiuse, come farebbe un regime amministrato
    che compensa minus e plusvalenze nello stesso anno.
    """
    ops = ris.operazioni
    if ops.empty or aliquota <= 0:
        return ris.metriche["valore_finale"]
    chiuse = ops[ops.motivo != "fine_periodo"]
    if chiuse.empty:
        return ris.metriche["valore_finale"]
    capitale = chiuse.quote * chiuse.prezzo_acquisto
    utile = float(((chiuse.prezzo_vendita - chiuse.prezzo_acquisto) * chiuse.quote).sum())
    return ris.metriche["valore_finale"] - max(0.0, utile) * aliquota / 100.0


def main(argv: Optional[Sequence[str]] = None) -> int:
    p = argparse.ArgumentParser(description="Confronta varianti della strategia.")
    p.add_argument("--reco", default="raccomandazioni_storiche.csv")
    p.add_argument("--capitale-iniziale", type=float, default=1000.0)
    p.add_argument("--versamento-mensile", type=float, default=1000.0)
    p.add_argument("--commissione-fissa", type=float, default=0.0)
    p.add_argument("--commissione-pct", type=float, default=0.0)
    p.add_argument("--tasse", type=float, default=0.0,
                   help="Aliquota sulle plusvalenze realizzate, in %% (26 per l'Italia).")
    p.add_argument("--esecuzione", choices=("close", "intraday"), default="close")
    p.add_argument("--benchmark", default="SPY",
                   help="Ticker di confronto, oppure EQUIPESATO (paniere a pesi uguali degli stessi titoli).")
    p.add_argument("--valuta", choices=("EUR", "USD", "locale"), default="EUR")
    p.add_argument("--versamento-solo-mensile", action="store_true")
    p.add_argument("--varianti", choices=("analisti", "tecnico"), default="analisti")
    p.add_argument("--tasso-privo-rischio", type=float, default=0.0,
                   help="Tasso annuo privo di rischio in %%, per Sharpe/Sortino/alpha.")
    p.add_argument("--cache-dir", default=".cache")
    p.add_argument("--output", default="confronto_varianti.csv")
    args = p.parse_args(argv)

    logging.basicConfig(level=logging.INFO, format="%(asctime)s | %(levelname)-7s | %(message)s",
                        datefmt="%H:%M:%S")

    reco = bt.carica_raccomandazioni(args.reco, None, None)
    benchmark = args.benchmark.strip().upper()
    tickers = sorted(reco.ticker.unique())
    if benchmark not in ("NONE", "EQUIPESATO"):
        tickers = sorted(set(tickers) | {benchmark})
    inizio = (reco.data_articolo.min() - pd.Timedelta(days=10)).strftime("%Y-%m-%d")
    fine = pd.Timestamp.today().strftime("%Y-%m-%d")

    prezzi = price_data.load_prices(tickers, inizio, fine, args.cache_dir)
    fx = price_data.load_fx(inizio, fine, args.cache_dir) if args.valuta == "EUR" else None
    mercato = bt.DatiMercato(prezzi, fx, args.valuta)
    if benchmark == "EQUIPESATO":
        serie = bt.serie_equipesata(mercato, sorted(reco.ticker.unique()))
        mercato.close["EQUIPESATO"] = serie
        mercato.tickers.add("EQUIPESATO")
        mercato._col["EQUIPESATO"] = mercato.close.columns.get_loc("EQUIPESATO")
        mercato._mat["close"] = mercato.close.to_numpy(dtype=float)
        mercato._ultimo_giorno["EQUIPESATO"] = serie.dropna().index[-1]
        LOGGER.info("Benchmark equipesato su %d titoli.", reco.ticker.nunique())
    bt.report_qualita(reco, mercato)

    comuni = dict(capitale_iniziale=args.capitale_iniziale,
                  versamento_mensile=args.versamento_mensile,
                  commissione_fissa=args.commissione_fissa,
                  commissione_pct=args.commissione_pct,
                  esecuzione=args.esecuzione, valuta=args.valuta,
                  versamento_solo_mensile=args.versamento_solo_mensile)

    date_versamento = sorted(reco.data_articolo.unique())
    bench, equity_bench = bt.benchmark_dca(mercato, benchmark, date_versamento,
                                           bt.Parametri(**comuni))
    rend_bench = mt.rendimenti_giornalieri(equity_bench) if not equity_bench.empty else None

    righe = []
    elenco = varianti_tecniche() if args.varianti == "tecnico" else varianti_standard()
    for nome, extra in elenco:
        par = bt.Parametri(**{**comuni, **extra})
        ris = bt.Backtester(reco, mercato, par).esegui()
        m = ris.metriche
        netto = applica_tasse(ris, args.tasse)
        rel = {}
        if rend_bench is not None:
            rend = mt.rendimenti_giornalieri(ris.equity)
            rel = mt.metriche_relative(rend, rend_bench, args.tasso_privo_rischio)
            pari = mt.benchmark_a_pari_esposizione(ris.equity, rend_bench)
            if not pari.empty:
                anni = max(len(pari) / mt.GIORNI_BORSA_ANNO, 1e-9)
                rel["pari_esposizione_pct"] = (float((1 + pari).prod()) ** (1 / anni) - 1) * 100
        LOGGER.info("%-38s IRR %6.2f%%  alpha %6.2f%%  finale %8.0f EUR",
                    nome, m["irr_annuo_pct"], rel.get("alpha_annuo_pct", float("nan")), m["valore_finale"])
        righe.append({
            "variante": nome,
            "irr_annuo_pct": round(m["irr_annuo_pct"], 2),
            "valore_finale": round(m["valore_finale"]),
            "prelevato": round(m.get("prelevato", 0)),
            "valore_netto_tasse": round(netto),
            "alpha_annuo_pct": round(rel.get("alpha_annuo_pct", float("nan")), 2),
            "beta": round(rel.get("beta", float("nan")), 2),
            "info_ratio": round(rel.get("information_ratio", float("nan")), 2),
            "t_stat": round(rel.get("t_stat_extra_rendimento", float("nan")), 2),
            "sharpe": round(m.get("sharpe", float("nan")), 2),
            "sortino": round(m.get("sortino", float("nan")), 2),
            "calmar": round(m.get("calmar", float("nan")), 2),
            "esposizione_pct": round(m.get("esposizione_media_pct", float("nan")), 1),
            "max_drawdown_pct": round(m["max_drawdown_pct"], 1),
            "operazioni": int(m.get("operazioni", 0)),
        })

    if bench:
        righe.append({
            "variante": "BENCHMARK: accumulo su %s, mai vendere" % benchmark,
            "irr_annuo_pct": round(bench["irr_annuo_pct"], 2),
            "valore_finale": round(bench["valore_finale"]),
            "prelevato": 0,
            "valore_netto_tasse": round(bench["valore_finale"]),  # nessuna vendita = nessuna imposta
            "alpha_annuo_pct": 0.0, "beta": 1.0, "info_ratio": float("nan"), "t_stat": float("nan"),
            "sharpe": round(bench.get("sharpe", float("nan")), 2),
            "sortino": round(bench.get("sortino", float("nan")), 2),
            "calmar": round(bench.get("calmar", float("nan")), 2),
            "esposizione_pct": 100.0,
            "max_drawdown_pct": round(bench["max_drawdown_pct"], 1),
            "operazioni": 0,
        })

    tabella = pd.DataFrame(righe).sort_values("irr_annuo_pct", ascending=False)
    tabella.to_csv(args.output, index=False)
    print()
    # Con i segnali giornalieri le date di acquisto sono molte piu' dei mesi:
    # i versamenti vanno contati sui mesi effettivi, non sulle date di segnale.
    mesi = len({(d.year, d.month) for d in pd.to_datetime(date_versamento)}) \
        if args.versamento_solo_mensile else len(date_versamento)
    print("Versato in totale: %.0f EUR in %d versamenti" % (
        args.capitale_iniziale + args.versamento_mensile * (mesi - 1), mesi))
    if args.commissione_fissa or args.commissione_pct:
        print("Commissioni: %.2f EUR fissi + %.2f%% per operazione" % (
            args.commissione_fissa, args.commissione_pct))
    if args.tasse:
        print("Imposta sulle plusvalenze realizzate: %.0f%%" % args.tasse)
    print()
    print(tabella.to_string(index=False))
    print("\nTabella salvata in %s" % args.output)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
