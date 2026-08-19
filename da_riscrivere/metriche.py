#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
metriche.py — indicatori per giudicare se una strategia ha senso.

Il problema centrale: in un mercato che sale, **qualunque** strategia long-only
guadagna. Per capire se le regole aggiungono valore o se stanno solo cavalcando
il mercato servono misure che separino le tre componenti del rendimento:

  1. **esposizione** — quanto capitale era davvero investito (il resto e' "dead
     money" che non rende nulla);
  2. **beta** — quanto del rendimento e' semplicemente il mercato;
  3. **alpha** — quel che resta, ed e' l'unica parte attribuibile alle regole.

Il benchmark giusto non e' quindi "l'indice", ma **l'indice tenuto con la stessa
esposizione media della strategia**: senza questa correzione una strategia che
resta al 60% liquida sembra prudente quando invece e' solo poco investita.
"""

from __future__ import annotations

from typing import Dict, Optional

import numpy as np
import pandas as pd

GIORNI_BORSA_ANNO = 252


# ---------------------------------------------------------------------------
# Rendimenti depurati dai flussi
# ---------------------------------------------------------------------------


def flussi_netti(equity: pd.DataFrame) -> pd.Series:
    """Flussi esterni giornalieri: versamenti positivi, prelievi negativi.

    Servono per depurare i rendimenti: un patrimonio che sale perche' hai
    versato 1.000 euro non ha "reso" nulla.
    """
    versato = equity["versato"].diff()
    versato.iloc[0] = equity["versato"].iloc[0]
    prelevato = (equity["prelevato"].diff().fillna(0.0)
                 if "prelevato" in equity.columns else pd.Series(0.0, index=equity.index))
    if "prelevato" in equity.columns:
        prelevato.iloc[0] = equity["prelevato"].iloc[0]
    return versato.fillna(0.0) - prelevato


def rendimenti_giornalieri(equity: pd.DataFrame) -> pd.Series:
    """Serie dei rendimenti giornalieri al netto dei flussi (base del TWR)."""
    totale = equity["totale"]
    flussi = flussi_netti(equity)
    rendimenti = (totale - flussi) / totale.shift(1) - 1.0
    return (rendimenti.iloc[1:]
            .replace([np.inf, -np.inf], 0.0)
            .fillna(0.0))


# ---------------------------------------------------------------------------
# Metriche di rischio (senza benchmark)
# ---------------------------------------------------------------------------


def metriche_rischio(rendimenti: pd.Series, tasso_privo_rischio: float = 0.0) -> Dict[str, float]:
    """Volatilita', Sharpe, Sortino, Calmar e drawdown.

    `tasso_privo_rischio` e' annuo in percentuale (2.0 = 2%).
    """
    if rendimenti.empty or rendimenti.std() == 0:
        return {}

    rf_giornaliero = (1.0 + tasso_privo_rischio / 100.0) ** (1.0 / GIORNI_BORSA_ANNO) - 1.0
    eccesso = rendimenti - rf_giornaliero
    vol = float(rendimenti.std() * np.sqrt(GIORNI_BORSA_ANNO))

    # Sortino: al denominatore solo la volatilita' delle giornate negative.
    # Per una strategia asimmetrica (take profit + stop loss) e' piu' onesto
    # dello Sharpe, che penalizza allo stesso modo oscillazioni su e giu'.
    giu = rendimenti[rendimenti < 0]
    vol_giu = float(giu.std() * np.sqrt(GIORNI_BORSA_ANNO)) if len(giu) > 1 else float("nan")

    cumulato = (1.0 + rendimenti).cumprod()
    picco = cumulato.cummax()
    drawdown = cumulato / picco - 1.0
    max_dd = float(drawdown.min())

    anni = len(rendimenti) / GIORNI_BORSA_ANNO
    cagr = float(cumulato.iloc[-1] ** (1.0 / anni) - 1.0) if anni > 0 else float("nan")

    return {
        "twr_annuo_pct": cagr * 100.0,
        "volatilita_pct": vol * 100.0,
        "sharpe": float(eccesso.mean() / rendimenti.std() * np.sqrt(GIORNI_BORSA_ANNO)),
        "sortino": float(eccesso.mean() * GIORNI_BORSA_ANNO / vol_giu) if vol_giu and vol_giu == vol_giu else float("nan"),
        "calmar": cagr / abs(max_dd) if max_dd else float("nan"),
        "max_drawdown_twr_pct": max_dd * 100.0,
        "giorni_peggiore_pct": float(rendimenti.min() * 100.0),
    }


# ---------------------------------------------------------------------------
# Metriche relative a un benchmark
# ---------------------------------------------------------------------------


def metriche_relative(strategia: pd.Series, benchmark: pd.Series,
                      tasso_privo_rischio: float = 0.0) -> Dict[str, float]:
    """Beta, alpha di Jensen, information ratio, tracking error, t-stat.

    Il **beta** dice quanta parte del movimento e' mercato: beta 0,6 significa
    che la strategia si muove per il 60% con l'indice, e che confrontarla con
    l'indice pieno e' scorretto.

    L'**alpha di Jensen** e' il rendimento annuo che resta dopo aver sottratto
    la parte spiegata dal mercato: e' la misura piu' vicina a "le regole
    aggiungono valore".

    L'**information ratio** rapporta il rendimento in eccesso alla sua
    variabilita': sotto 0,3 e' rumore, sopra 0,5 e' un risultato solido.

    Il **t-stat** dice se l'eccesso di rendimento e' distinguibile dal caso:
    sotto 2 in valore assoluto, no.
    """
    comune = strategia.index.intersection(benchmark.index)
    s, b = strategia.loc[comune], benchmark.loc[comune]
    if len(s) < 30 or b.std() == 0:
        return {}

    rf = (1.0 + tasso_privo_rischio / 100.0) ** (1.0 / GIORNI_BORSA_ANNO) - 1.0
    beta = float(np.cov(s - rf, b - rf)[0, 1] / np.var(b - rf, ddof=1))
    alpha_g = float((s - rf).mean() - beta * (b - rf).mean())
    attivo = s - b
    correlazione = float(np.corrcoef(s, b)[0, 1])

    return {
        "beta": beta,
        "alpha_annuo_pct": ((1.0 + alpha_g) ** GIORNI_BORSA_ANNO - 1.0) * 100.0,
        "r_quadro": correlazione ** 2,
        "tracking_error_pct": float(attivo.std() * np.sqrt(GIORNI_BORSA_ANNO) * 100.0),
        "information_ratio": float(attivo.mean() / attivo.std() * np.sqrt(GIORNI_BORSA_ANNO))
        if attivo.std() else float("nan"),
        "t_stat_extra_rendimento": float(attivo.mean() / (attivo.std() / np.sqrt(len(attivo))))
        if attivo.std() else float("nan"),
        "giorni_sopra_benchmark_pct": float((attivo > 0).mean() * 100.0),
    }


# ---------------------------------------------------------------------------
# Esposizione: il capitale che lavora davvero
# ---------------------------------------------------------------------------


def metriche_esposizione(equity: pd.DataFrame) -> Dict[str, float]:
    """Quanta parte del patrimonio era investita, e quanto e' costata la liquidita'."""
    if equity.empty:
        return {}
    quota = (equity["investito"] / equity["totale"].replace(0, np.nan)).clip(0, 1)
    return {
        "esposizione_media_pct": float(quota.mean() * 100.0),
        "esposizione_minima_pct": float(quota.min() * 100.0),
        "giorni_sotto_80pct_investito": float((quota < 0.8).mean() * 100.0),
        "liquidita_media_eur": float(equity["liquidita"].mean()),
    }


def benchmark_a_pari_esposizione(equity: pd.DataFrame, rend_benchmark: pd.Series) -> pd.Series:
    """Rendimenti dell'indice tenuto con la stessa esposizione giornaliera.

    E' il confronto corretto per una strategia che tiene liquidita': risponde a
    "quanto avrei ottenuto restando altrettanto poco investito, ma senza
    selezionare titoli e senza regole di vendita?".
    """
    quota = (equity["investito"] / equity["totale"].replace(0, np.nan)).clip(0, 1).fillna(0.0)
    comune = rend_benchmark.index.intersection(quota.index)
    return rend_benchmark.loc[comune] * quota.loc[comune]


# ---------------------------------------------------------------------------
# Tenuta nei diversi regimi di mercato
# ---------------------------------------------------------------------------


# Fasi con comportamento molto diverso: se una strategia funziona solo nelle
# righe in salita, quello che stai misurando e' il mercato, non la strategia.
REGIMI = [
    ("2012-2014 toro post-crisi", "2012-01-01", "2014-12-31"),
    ("2015-2016 laterale", "2015-01-01", "2016-12-31"),
    ("2017 toro tranquillo", "2017-01-01", "2017-12-31"),
    ("2018 correzione", "2018-01-01", "2018-12-31"),
    ("2019 toro", "2019-01-01", "2019-12-31"),
    ("2020 crollo Covid e rimbalzo", "2020-01-01", "2020-12-31"),
    ("2021 toro", "2021-01-01", "2021-12-31"),
    ("2022 orso", "2022-01-01", "2022-12-31"),
    ("2023-2026 toro AI", "2023-01-01", "2026-12-31"),
]


def rendimenti_per_regime(strategia: pd.Series, benchmark: Optional[pd.Series] = None) -> pd.DataFrame:
    """Rendimento della strategia e dell'indice in ciascuna fase di mercato."""
    righe = []
    for nome, inizio, fine in REGIMI:
        finestra = strategia.loc[(strategia.index >= inizio) & (strategia.index <= fine)]
        if len(finestra) < 20:
            continue
        riga = {"regime": nome,
                "strategia_pct": round(float((1 + finestra).prod() - 1) * 100, 1)}
        if benchmark is not None:
            fb = benchmark.loc[(benchmark.index >= inizio) & (benchmark.index <= fine)]
            riga["benchmark_pct"] = round(float((1 + fb).prod() - 1) * 100, 1)
            riga["differenza"] = round(riga["strategia_pct"] - riga["benchmark_pct"], 1)
        righe.append(riga)
    return pd.DataFrame(righe)


def rendimenti_annuali(strategia: pd.Series, benchmark: Optional[pd.Series] = None) -> pd.DataFrame:
    """Rendimento anno per anno, per vedere quanto e' costante il risultato."""
    righe = []
    for anno, gruppo in strategia.groupby(strategia.index.year):
        riga = {"anno": int(anno), "strategia_pct": round(float((1 + gruppo).prod() - 1) * 100, 1)}
        if benchmark is not None:
            fb = benchmark[benchmark.index.year == anno]
            riga["benchmark_pct"] = round(float((1 + fb).prod() - 1) * 100, 1)
            riga["differenza"] = round(riga["strategia_pct"] - riga["benchmark_pct"], 1)
        righe.append(riga)
    return pd.DataFrame(righe)


# ---------------------------------------------------------------------------
# Robustezza statistica
# ---------------------------------------------------------------------------


def bootstrap_operazioni(rendimenti_trade: pd.Series, n_simulazioni: int = 2000,
                         seed: int = 12345) -> Dict[str, float]:
    """Ricampiona le operazioni per capire quanto del risultato e' fortuna.

    Estrae con reimmissione lo stesso numero di operazioni e ricompone il
    rendimento medio: se l'intervallo al 5-95% contiene lo zero, il risultato
    non e' distinguibile dal caso.
    """
    if rendimenti_trade.empty or len(rendimenti_trade) < 20:
        return {}
    rng = np.random.default_rng(seed)
    valori = rendimenti_trade.to_numpy(dtype=float)
    medie = np.array([rng.choice(valori, size=len(valori), replace=True).mean()
                      for _ in range(n_simulazioni)])
    return {
        "trade_medio_pct": float(valori.mean() * 100),
        "bootstrap_p5_pct": float(np.percentile(medie, 5) * 100),
        "bootstrap_p95_pct": float(np.percentile(medie, 95) * 100),
        "prob_trade_medio_negativo_pct": float((medie < 0).mean() * 100),
    }


def penalita_ricerca(n_combinazioni_provate: int, sharpe_migliore: float,
                     n_osservazioni: int) -> Dict[str, float]:
    """Correzione per il fatto di aver provato molte combinazioni.

    Se si testano 36 combinazioni e si tiene la migliore, quel risultato e' in
    parte fortuna di selezione. La soglia sotto riporta lo Sharpe atteso dalla
    migliore fra N strategie prive di valore: se lo Sharpe trovato non la supera,
    la "scoperta" e' rumore.
    """
    if n_combinazioni_provate < 2 or n_osservazioni < 30:
        return {}
    # Valore atteso del massimo di N variabili normali standard (approssimazione
    # di Bailey-Lopez de Prado): sqrt(2*ln(N)), riscalato per l'errore standard.
    atteso_max = np.sqrt(2.0 * np.log(n_combinazioni_provate))
    errore_standard = 1.0 / np.sqrt(n_osservazioni / GIORNI_BORSA_ANNO)
    soglia = atteso_max * errore_standard
    return {
        "sharpe_migliore": sharpe_migliore,
        "soglia_sharpe_da_battere": float(soglia),
        "supera_la_penalita": float(sharpe_migliore > soglia),
    }
