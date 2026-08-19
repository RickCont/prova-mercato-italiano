#!/usr/bin/env python3
"""Test del motore di backtest con prezzi sintetici: nessuna rete.

Ogni scenario e' costruito perche' il risultato corretto sia calcolabile a mano.

Uso:  python tests/test_backtest.py
"""
import os
import sys

import pandas as pd

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
import backtest_strategy as bt

FALLITI = []


def check(cond, label):
    print(("  OK   " if cond else "  FAIL ") + label)
    if not cond:
        FALLITI.append(label)


def prezzi(serie_per_ticker, giorni):
    """DataFrame lungo con open=high=low=close (nessun gap, nessuna oscillazione)."""
    righe = []
    for ticker, valori in serie_per_ticker.items():
        for giorno, prezzo in zip(giorni, valori):
            righe.append({"date": giorno, "ticker": ticker, "open": prezzo,
                          "high": prezzo, "low": prezzo, "close": prezzo})
    return pd.DataFrame(righe)


def mercato(serie, giorni):
    return bt.DatiMercato(prezzi(serie, giorni), None, "USD")


def reco(righe):
    """righe = [(data, [ticker1, ticker2, ...]), ...] in ordine di preferenza."""
    out = []
    for data, tickers in righe:
        for i, t in enumerate(tickers, start=1):
            out.append({"data_articolo": pd.Timestamp(data), "ticker": t, "rank": i,
                        "rating_score": 5.0 - i * 0.01, "recommendation": "Buy"})
    return pd.DataFrame(out)


GIORNI = pd.bdate_range("2020-01-01", periods=70)
D0, D1 = GIORNI[0], GIORNI[22]          # due date di selezione, a ~un mese di distanza

# --- 1. Contabilita' di base -----------------------------------------------
print("[1] Contabilita': versamenti, quote, liquidita'")
piatto = {t: [100.0] * len(GIORNI) for t in ["AAA", "BBB"]}
m = mercato(piatto, GIORNI)
r = reco([(D0, ["AAA", "BBB"]), (D1, ["AAA", "BBB"])])
par = bt.Parametri(take_profit=10, stop_loss=None, capitale_iniziale=1000,
                   versamento_mensile=1000, n_titoli=2, valuta="USD")
ris = bt.Backtester(r, m, par).esegui()
eq = ris.equity
check(abs(eq.iloc[0]["investito"] - 1000) < 1e-6, "primo mese: investiti 1000 (capitale iniziale)")
check(abs(eq.iloc[0]["liquidita"]) < 1e-6, "liquidita' azzerata dopo gli acquisti")
check(abs(eq.iloc[0]["versato"] - 1000) < 1e-6, "primo versamento = capitale iniziale, non 2000")
check(abs(eq.loc[D1]["versato"] - 2000) < 1e-6, "secondo mese: versati altri 1000")
check(abs(eq.iloc[-1]["totale"] - 2000) < 1e-6, "mercato piatto -> patrimonio = versato")
check(abs(ris.metriche["irr_annuo_pct"]) < 0.01, "mercato piatto -> IRR nullo")

# --- 2. De-duplicazione ----------------------------------------------------
print("[2] De-duplicazione: mai due posizioni sullo stesso titolo")
r2 = reco([(D0, ["AAA", "BBB", "CCC"]), (D1, ["AAA", "BBB", "CCC"])])
serie = {t: [100.0] * len(GIORNI) for t in ["AAA", "BBB", "CCC"]}
par2 = bt.Parametri(take_profit=10, stop_loss=None, capitale_iniziale=1000,
                    versamento_mensile=1000, n_titoli=2, valuta="USD")
ris2 = bt.Backtester(r2, mercato(serie, GIORNI), par2).esegui()
aperte_fine = ris2.operazioni[ris2.operazioni.motivo == "fine_periodo"]
check(sorted(aperte_fine.ticker) == ["AAA", "BBB", "CCC"], "il 2o mese scala al 3o candidato")
check(len(aperte_fine.ticker) == len(set(aperte_fine.ticker)), "nessun ticker duplicato in portafoglio")
check(ris2.metriche["duplicati_scartati"] == 2, "2 candidati scartati perche' gia' in portafoglio")

# --- 3. Take profit --------------------------------------------------------
print("[3] Take profit")
salita = [100.0] * 5 + [111.0] * (len(GIORNI) - 5)     # +11% dal 6o giorno
serie3 = {"AAA": salita, "BBB": [100.0] * len(GIORNI)}
par3 = bt.Parametri(take_profit=10, stop_loss=None, capitale_iniziale=1000,
                    versamento_mensile=0, n_titoli=2, valuta="USD")
ris3 = bt.Backtester(reco([(D0, ["AAA", "BBB"])]), mercato(serie3, GIORNI), par3).esegui()
tp = ris3.operazioni[ris3.operazioni.motivo == "take_profit"]
check(len(tp) == 1 and tp.iloc[0].ticker == "AAA", "AAA venduto in take profit")
check(tp.iloc[0].data_vendita == GIORNI[5], "venduto il primo giorno in cui la soglia e' superata")
check(abs(tp.iloc[0].rendimento - 0.11) < 1e-9, "rendimento realizzato +11%")
check(abs(ris3.equity.iloc[-1]["liquidita"] - 555.0) < 1e-6, "incasso 500 * 1.11 torna in liquidita'")

# --- 4. Stop loss ----------------------------------------------------------
print("[4] Stop loss")
discesa = [100.0] * 5 + [84.0] * (len(GIORNI) - 5)     # -16%
serie4 = {"AAA": discesa, "BBB": [100.0] * len(GIORNI)}
par4 = bt.Parametri(take_profit=10, stop_loss=15, capitale_iniziale=1000,
                    versamento_mensile=0, n_titoli=2, valuta="USD")
ris4 = bt.Backtester(reco([(D0, ["AAA", "BBB"])]), mercato(serie4, GIORNI), par4).esegui()
sl = ris4.operazioni[ris4.operazioni.motivo == "stop_loss"]
check(len(sl) == 1 and abs(sl.iloc[0].rendimento + 0.16) < 1e-9, "stop loss scattato a -16%")
par4b = bt.Parametri(**{**par4.__dict__, "stop_loss": None})
ris4b = bt.Backtester(reco([(D0, ["AAA", "BBB"])]), mercato(serie4, GIORNI), par4b).esegui()
check((ris4b.operazioni.motivo == "stop_loss").sum() == 0, "stop 'none' -> il titolo si tiene")
check(abs(ris4b.equity.iloc[-1]["totale"] - 920.0) < 1e-6, "senza stop la perdita resta a mercato")

# --- 5. Reinvestimento del ricavato ---------------------------------------
print("[5] La liquidita' rientrata viene reinvestita il mese dopo")
serie5 = {"AAA": [100.0] * 5 + [111.0] * (len(GIORNI) - 5),
          "BBB": [100.0] * len(GIORNI), "CCC": [100.0] * len(GIORNI)}
r5 = reco([(D0, ["AAA", "BBB", "CCC"]), (D1, ["CCC", "AAA", "BBB"])])
par5 = bt.Parametri(take_profit=10, stop_loss=None, capitale_iniziale=1000,
                    versamento_mensile=1000, n_titoli=2, valuta="USD")
ris5 = bt.Backtester(r5, mercato(serie5, GIORNI), par5).esegui()
# Al 2o mese: 555 rientrati + 1000 versati = 1555, divisi su 2 titoli.
check(abs(ris5.equity.loc[D1]["liquidita"]) < 1e-6, "tutta la liquidita' reinvestita")
check(abs(ris5.equity.loc[D1]["investito"] - 2055.0) < 1e-6, "investito = 500 (BBB) + 1555 (nuovi)")

# --- 6. Commissioni --------------------------------------------------------
print("[6] Commissioni")
par6 = bt.Parametri(take_profit=10, stop_loss=None, capitale_iniziale=1000,
                    versamento_mensile=0, n_titoli=2, valuta="USD",
                    commissione_fissa=5.0)
ris6 = bt.Backtester(reco([(D0, ["AAA", "BBB"])]), mercato(serie3, GIORNI), par6).esegui()
tp6 = ris6.operazioni[ris6.operazioni.motivo == "take_profit"].iloc[0]
# 500 investiti - 5 di commissione = 495 -> +11% = 549.45 - 5 = 544.45 su 500 = +8.89%
check(abs(tp6.rendimento - (549.45 - 5) / 500 + 1) < 1e-6, "le commissioni erodono il rendimento")
check(tp6.rendimento < 0.11, "rendimento netto inferiore a quello lordo")

# --- 7. Modalita' intraday e gap ------------------------------------------
print("[7] Intraday: soglie su massimi/minimi, gap eseguiti in apertura")
n = len(GIORNI)
righe = []
for i, g in enumerate(GIORNI):
    if i < 5:
        o = h = l = c = 100.0
    elif i == 5:
        o, h, l, c = 100.0, 115.0, 99.0, 101.0     # tocca +15% in giornata e rientra
    else:
        o = h = l = c = 101.0
    righe.append({"date": g, "ticker": "AAA", "open": o, "high": h, "low": l, "close": c})
    righe.append({"date": g, "ticker": "BBB", "open": 100.0, "high": 100.0, "low": 100.0, "close": 100.0})
m7 = bt.DatiMercato(pd.DataFrame(righe), None, "USD")
par7 = bt.Parametri(take_profit=10, stop_loss=None, capitale_iniziale=1000, versamento_mensile=0,
                    n_titoli=2, valuta="USD", esecuzione="intraday")
ris7 = bt.Backtester(reco([(D0, ["AAA", "BBB"])]), m7, par7).esegui()
tp7 = ris7.operazioni[ris7.operazioni.motivo == "take_profit"]
check(len(tp7) == 1 and abs(tp7.iloc[0].rendimento - 0.10) < 1e-9,
      "intraday: venduto esattamente alla soglia toccata in giornata")
par7b = bt.Parametri(**{**par7.__dict__, "esecuzione": "close"})
ris7b = bt.Backtester(reco([(D0, ["AAA", "BBB"])]), m7, par7b).esegui()
check((ris7b.operazioni.motivo == "take_profit").sum() == 0,
      "in modalita' close lo stesso movimento non fa scattare nulla")

# gap: apre gia' sotto lo stop -> esegue in apertura, perdita maggiore della soglia
righe_gap = []
for i, g in enumerate(GIORNI):
    if i < 5:
        o = h = l = c = 100.0
    else:
        o, h, l, c = 70.0, 72.0, 68.0, 71.0        # apertura in gap a -30%
    righe_gap.append({"date": g, "ticker": "AAA", "open": o, "high": h, "low": l, "close": c})
    righe_gap.append({"date": g, "ticker": "BBB", "open": 100.0, "high": 100.0, "low": 100.0, "close": 100.0})
m8 = bt.DatiMercato(pd.DataFrame(righe_gap), None, "USD")
par8 = bt.Parametri(take_profit=10, stop_loss=15, capitale_iniziale=1000, versamento_mensile=0,
                    n_titoli=2, valuta="USD", esecuzione="intraday")
ris8 = bt.Backtester(reco([(D0, ["AAA", "BBB"])]), m8, par8).esegui()
sl8 = ris8.operazioni[ris8.operazioni.motivo == "stop_loss"].iloc[0]
check(abs(sl8.rendimento + 0.30) < 1e-9, "gap in apertura: eseguito a -30%, non a -15%")

# --- 8. Delisting ----------------------------------------------------------
print("[8] Delisting: la posizione si chiude all'ultimo prezzo noto")
righe9 = []
for i, g in enumerate(GIORNI):
    if i <= 10:
        righe9.append({"date": g, "ticker": "AAA", "open": 100.0, "high": 100.0, "low": 100.0, "close": 100.0})
    righe9.append({"date": g, "ticker": "BBB", "open": 100.0, "high": 100.0, "low": 100.0, "close": 100.0})
m9 = bt.DatiMercato(pd.DataFrame(righe9), None, "USD")
par9 = bt.Parametri(take_profit=10, stop_loss=None, capitale_iniziale=1000, versamento_mensile=0,
                    n_titoli=2, valuta="USD")
ris9 = bt.Backtester(reco([(D0, ["AAA", "BBB"])]), m9, par9).esegui()
check((ris9.operazioni.motivo == "delisting").sum() == 1, "posizione chiusa per delisting")
check(ris9.operazioni[ris9.operazioni.motivo == "delisting"].iloc[0].data_vendita == GIORNI[10],
      "chiusa all'ultimo giorno quotato")

# --- 9. Conversione valutaria ---------------------------------------------
print("[9] Cambio EUR/USD")
fx = pd.DataFrame({"date": GIORNI, "eurusd": [2.0] * len(GIORNI)})   # 1 EUR = 2 USD
m10 = bt.DatiMercato(prezzi({"AAA": [100.0] * len(GIORNI), "BBB": [100.0] * len(GIORNI)}, GIORNI), fx, "EUR")
check(abs(float(m10.close.iloc[0]["AAA"]) - 50.0) < 1e-9, "prezzo in dollari convertito in euro")

# --- 10. Sweep -------------------------------------------------------------
print("[10] Sweep sulla griglia X/Y")
tab = bt.esegui_sweep(reco([(D0, ["AAA", "BBB"])]), mercato(serie3, GIORNI),
                      bt.Parametri(capitale_iniziale=1000, versamento_mensile=0, n_titoli=2, valuta="USD"),
                      [5, 10], [None, 20])
check(len(tab) == 4, "4 combinazioni testate")
check(set(tab.columns) >= {"take_profit", "stop_loss", "irr_annuo_pct", "valore_finale"}, "colonne dello sweep")
check(list(tab.stop_loss) == ["hold", 20, "hold", 20], "lo stop 'none' e' etichettato 'hold'")

# --- 11. Take profit disattivato -------------------------------------------
print("[11] Nessun take profit: il titolo si tiene e continua a correre")
crescita = [100.0 + i * 2 for i in range(len(GIORNI))]      # sale sempre
serie11 = {"AAA": crescita, "BBB": [100.0] * len(GIORNI)}
par11 = bt.Parametri(take_profit=None, stop_loss=None, capitale_iniziale=1000,
                     versamento_mensile=0, n_titoli=2, valuta="USD")
ris11 = bt.Backtester(reco([(D0, ["AAA", "BBB"])]), mercato(serie11, GIORNI), par11).esegui()
check((ris11.operazioni.motivo == "take_profit").sum() == 0, "nessuna vendita in guadagno")
atteso = 500 * crescita[-1] / crescita[0] + 500
check(abs(ris11.equity.iloc[-1]["totale"] - atteso) < 1e-6, "il guadagno resta interamente a mercato")
par11b = bt.Parametri(**{**par11.__dict__, "take_profit": 10})
ris11b = bt.Backtester(reco([(D0, ["AAA", "BBB"])]), mercato(serie11, GIORNI), par11b).esegui()
check(ris11b.equity.iloc[-1]["totale"] < ris11.equity.iloc[-1]["totale"],
      "col take profit si guadagna meno su un titolo che sale sempre")

# --- 12. Trailing stop -----------------------------------------------------
print("[12] Trailing stop: lascia correre, poi protegge il guadagno")
# sale a 150, poi scende: con trailing 10% deve vendere a 135.
salita_discesa = [100.0] * 3 + [150.0] * 3 + [134.0] * (len(GIORNI) - 6)
serie12 = {"AAA": salita_discesa, "BBB": [100.0] * len(GIORNI)}
par12 = bt.Parametri(take_profit=None, stop_loss=None, trailing_stop=10, capitale_iniziale=1000,
                     versamento_mensile=0, n_titoli=2, valuta="USD")
ris12 = bt.Backtester(reco([(D0, ["AAA", "BBB"])]), mercato(serie12, GIORNI), par12).esegui()
ts = ris12.operazioni[ris12.operazioni.motivo == "trailing_stop"]
check(len(ts) == 1, "trailing stop scattato una volta")
check(abs(ts.iloc[0].rendimento - 0.34) < 1e-9, "venduto a 134: +34%, non +10%")
check(ts.iloc[0].data_vendita == GIORNI[6], "venduto il giorno in cui scende sotto il massimo -10%")

# --- 13. Reinvestimento immediato -----------------------------------------
print("[13] Reinvestimento immediato contro attesa del mese nuovo")
# AAA tocca +11% al 6o giorno (scatta il take profit) e poi continua a salire:
# chi reinveste subito ricompra e cavalca il resto della salita, chi aspetta il
# mese nuovo resta liquido. Il riacquisto dello stesso titolo e' lecito: la
# regola vieta due posizioni *contemporanee*, non il rientro dopo la vendita.
serie13 = {"AAA": [100.0] * 5 + [111.0 + i for i in range(len(GIORNI) - 5)],
           "BBB": [100.0] * len(GIORNI),
           "CCC": [100.0] * len(GIORNI)}
r13 = reco([(D0, ["AAA", "BBB", "CCC"])])
comune = dict(take_profit=10, stop_loss=None, capitale_iniziale=1000, versamento_mensile=0,
              n_titoli=2, valuta="USD")
ris_mens = bt.Backtester(r13, mercato(serie13, GIORNI), bt.Parametri(**comune)).esegui()
ris_sub = bt.Backtester(r13, mercato(serie13, GIORNI),
                        bt.Parametri(**{**comune, "reinvestimento": "subito"})).esegui()
check(abs(ris_mens.equity.iloc[-1]["liquidita"] - 555.0) < 1e-6,
      "modalita' mensile: l'incasso resta liquido fino al mese nuovo")
check(ris_sub.equity.iloc[-1]["liquidita"] < 1.0, "modalita' subito: la liquidita' viene reimpiegata")
check(ris_sub.equity.iloc[-1]["totale"] > ris_mens.equity.iloc[-1]["totale"],
      "reinvestire subito su un mercato che sale rende piu' che restare liquidi")
check((ris_sub.operazioni.ticker == "AAA").sum() >= 2,
      "AAA riacquistata dopo la vendita: una posizione per ogni chiusura")
posizioni_sub = ris_sub.equity["posizioni"].max()
check(posizioni_sub <= 2, "il numero di posizioni aperte non cresce oltre gli slot")

# --- 14. Prelievo annuo ----------------------------------------------------
print("[14] Prelievo annuo: simula un portafoglio da cui si spende")
GIORNI_LUNGHI = pd.bdate_range("2020-01-01", periods=600)
D_2020, D_2021, D_2022 = GIORNI_LUNGHI[0], GIORNI_LUNGHI[261], GIORNI_LUNGHI[522]
piatto_lungo = {t: [100.0] * len(GIORNI_LUNGHI) for t in ["AAA", "BBB"]}
m14 = mercato(piatto_lungo, GIORNI_LUNGHI)
r14 = reco([(D_2020, ["AAA", "BBB"]), (D_2021, ["AAA", "BBB"]), (D_2022, ["AAA", "BBB"])])
base14 = dict(take_profit=10, stop_loss=None, capitale_iniziale=1000, versamento_mensile=0,
              n_titoli=2, valuta="USD")
senza = bt.Backtester(r14, m14, bt.Parametri(**base14)).esegui()
con = bt.Backtester(r14, m14, bt.Parametri(**{**base14, "prelievo_annuo_pct": 10})).esegui()
check(abs(senza.equity.iloc[-1]["totale"] - 1000) < 1e-6, "senza prelievi il patrimonio resta 1000")
# Due prelievi (2021 e 2022), 10% del patrimonio: 1000 -> 900 -> 810.
check(abs(con.equity.iloc[-1]["totale"] - 810.0) < 1e-6, "due prelievi del 10%: patrimonio 810")
check(abs(con.metriche["prelevato"] - 190.0) < 1e-6, "prelevati 100 + 90 = 190")
check(abs(con.metriche["valore_piu_prelievi"] - 1000.0) < 1e-6,
      "valore + prelievi = patrimonio senza prelievi (mercato piatto)")
check(con.equity["prelevato"].is_monotonic_increasing, "i prelievi sono cumulati e non calano")

# Il prelievo attinge prima alla liquidita', poi riduce le posizioni pro-quota.
salita14 = {"AAA": [100.0] * 5 + [111.0] * (len(GIORNI_LUNGHI) - 5),
            "BBB": [100.0] * len(GIORNI_LUNGHI)}
con2 = bt.Backtester(r14, mercato(salita14, GIORNI_LUNGHI),
                     bt.Parametri(**{**base14, "prelievo_annuo_pct": 10})).esegui()
check(con2.metriche["prelevato"] > 0, "prelievo eseguito anche con liquidita' in cassa")
# Le posizioni ridotte restano aperte: il prelievo non deve chiudere titoli.
aperte_fine = con2.operazioni[con2.operazioni.motivo == "fine_periodo"]
check(len(aperte_fine) > 0, "il prelievo riduce le posizioni ma non le chiude tutte")
check(abs(bt.Parametri(prelievo_annuo_pct=10).etichetta().count("-10%anno") - 1) < 1,
      "l'etichetta riporta il prelievo")

# Base "versamenti": preleva una quota dei versamenti annui, non del patrimonio.
con3 = bt.Backtester(r14, m14, bt.Parametri(**{**base14, "capitale_iniziale": 10000,
                                              "versamento_mensile": 100,
                                              "prelievo_annuo_pct": 50,
                                              "prelievo_base": "versamenti"})).esegui()
check(abs(con3.metriche["prelevato"] - 2 * 0.5 * 100 * 12) < 1e-6,
      "base versamenti: 50% di 1200 per due anni = 1200")

# Se il portafoglio non basta, si preleva solo quel che c'e': niente scoperti.
con4 = bt.Backtester(r14, m14, bt.Parametri(**{**base14, "capitale_iniziale": 1000,
                                              "versamento_mensile": 100,
                                              "prelievo_annuo_pct": 50,
                                              "prelievo_base": "versamenti"})).esegui()
check(con4.metriche["prelevato"] < 2 * 0.5 * 100 * 12,
      "prelievo limitato al patrimonio disponibile")
check(con4.equity["totale"].min() >= -1e-9, "il patrimonio non va mai negativo")

# --- 15. Metriche di rischio e relative ------------------------------------
print("[15] Metriche: rischio, esposizione, beta e alpha")
import metriche as mt
rend = mt.rendimenti_giornalieri(senza.equity)
check(abs(float((1 + rend).prod() - 1)) < 1e-9, "mercato piatto -> TWR nullo")
esp = mt.metriche_esposizione(senza.equity)
check(abs(esp["esposizione_media_pct"] - 100.0) < 1e-6, "sempre investito -> esposizione 100%")

# Strategia identica al benchmark: beta 1, alpha 0, information ratio nullo.
import numpy as np
idx = pd.bdate_range("2020-01-01", periods=300)
serie_b = pd.Series(np.random.default_rng(7).normal(0.0004, 0.01, len(idx)), index=idx)
rel = mt.metriche_relative(serie_b, serie_b)
check(abs(rel["beta"] - 1.0) < 1e-9, "serie identiche -> beta 1")
check(abs(rel["alpha_annuo_pct"]) < 1e-6, "serie identiche -> alpha nullo")
check(abs(rel["r_quadro"] - 1.0) < 1e-9, "serie identiche -> R quadro 1")
# Strategia = metà dell'esposizione: beta 0.5.
rel_meta = mt.metriche_relative(serie_b * 0.5, serie_b)
check(abs(rel_meta["beta"] - 0.5) < 1e-9, "esposizione dimezzata -> beta 0.5")

# Benchmark a pari esposizione: se investi al 50%, rende metà.
eq = pd.DataFrame({"totale": [1000.0] * len(idx), "investito": [500.0] * len(idx),
                   "liquidita": [500.0] * len(idx), "versato": [1000.0] * len(idx),
                   "prelevato": [0.0] * len(idx)}, index=idx)
pari = mt.benchmark_a_pari_esposizione(eq, serie_b)
check(abs(float(pari.iloc[10]) - float(serie_b.iloc[10]) * 0.5) < 1e-12,
      "indice a pari esposizione: rendimenti dimezzati")

boot = mt.bootstrap_operazioni(pd.Series([0.10] * 50))
check(abs(boot["trade_medio_pct"] - 10.0) < 1e-9, "bootstrap su rendimenti costanti")
check(boot["prob_trade_medio_negativo_pct"] == 0.0, "nessuna probabilita' di media negativa")

pen = mt.penalita_ricerca(36, sharpe_migliore=0.5, n_osservazioni=3600)
check(pen["soglia_sharpe_da_battere"] > 0, "la penalita' per ricerca multipla e' calcolata")

# --- 16. Vendita parziale: si vende meta' e il resto corre -----------------
print("[16] Vendita parziale a scaglioni")
# Il titolo sale a gradini: 100 -> 110 -> 121 -> 133.1 (ogni gradino e' +10%).
gradini = [100.0]*3 + [110.0]*3 + [121.0]*3 + [133.1]*(len(GIORNI)-9)
serie16 = {"AAA": gradini, "BBB": [100.0]*len(GIORNI)}
base16 = dict(take_profit=10, stop_loss=None, capitale_iniziale=1000,
              versamento_mensile=0, n_titoli=2, valuta="USD")
r_tutto = bt.Backtester(reco([(D0, ["AAA","BBB"])]), mercato(serie16, GIORNI),
                        bt.Parametri(**base16)).esegui()
r_meta = bt.Backtester(reco([(D0, ["AAA","BBB"])]), mercato(serie16, GIORNI),
                       bt.Parametri(**{**base16, "vendita_parziale_pct": 50})).esegui()
tp_tutto = r_tutto.operazioni[r_tutto.operazioni.motivo == "take_profit"]
check(len(tp_tutto) == 1 and abs(tp_tutto.iloc[0].rendimento - 0.10) < 1e-9,
      "vendendo tutto: una sola operazione a +10%")
check(r_meta.equity.iloc[-1]["totale"] > r_tutto.equity.iloc[-1]["totale"],
      "vendendo meta' si guadagna di piu' su un titolo che continua a salire")
# 500 investiti: 250 escono a 110 (+10%), 125 a 121 (+21%), il resto a 133.1.
atteso = 500 * (0.5*1.1 + 0.25*1.21 + 0.25*1.331) + 500
check(abs(r_meta.equity.iloc[-1]["totale"] - atteso) < 0.5,
      "scaglioni: 50%% a +10%%, 25%% a +21%%, 25%% a +33%% (atteso %.0f)" % atteso)
# La riga registrata e' una sola, con il rendimento dell'intero pacchetto.
op_meta = r_meta.operazioni[r_meta.operazioni.ticker == "AAA"]
check(len(op_meta) == 1, "il pacchetto produce una sola riga nel registro, non una per scaglione")
check(op_meta.iloc[0].rendimento > 0.10, "il rendimento registrato e' quello complessivo del pacchetto")
# Senza il riferimento che sale, la meta' rimasta si svuoterebbe ogni giorno.
piatto16 = {"AAA": [100.0]*3 + [110.0]*(len(GIORNI)-3), "BBB": [100.0]*len(GIORNI)}
r_fermo = bt.Backtester(reco([(D0, ["AAA","BBB"])]), mercato(piatto16, GIORNI),
                        bt.Parametri(**{**base16, "vendita_parziale_pct": 50})).esegui()
# 500 su AAA -> meta' venduta a 110 (275 in cassa), resta meta' che vale 275;
# piu' i 500 fermi di BBB: 775 investiti in tutto.
check(abs(r_fermo.equity.iloc[-1]["investito"] - 775.0) < 0.5,
      "prezzo fermo dopo il primo scaglione: resta meta' pacchetto, non si svuota")
check(abs(r_fermo.equity.iloc[-1]["liquidita"] - 275.0) < 0.5,
      "l'incasso del primo scaglione e' in cassa")

# --- 17. Riacquisto dello stesso titolo ------------------------------------
print("[17] Riacquisto: piu' pacchetti sullo stesso titolo")
piatto17 = {t: [100.0]*len(GIORNI) for t in ["AAA","BBB"]}
r17 = reco([(D0, ["AAA","BBB"]), (D1, ["AAA","BBB"])])
base17 = dict(take_profit=10, stop_loss=None, capitale_iniziale=1000,
              versamento_mensile=1000, n_titoli=2, valuta="USD")
senza = bt.Backtester(r17, mercato(piatto17, GIORNI), bt.Parametri(**base17)).esegui()
con = bt.Backtester(r17, mercato(piatto17, GIORNI),
                    bt.Parametri(**{**base17, "consenti_riacquisto": True})).esegui()
check(senza.metriche["duplicati_scartati"] == 2, "senza riacquisto: i due candidati sono scartati")
check(con.metriche["duplicati_scartati"] == 0, "con riacquisto: nessuno scarto")
check(int(con.equity.iloc[-1]["posizioni"]) == 4, "quattro pacchetti aperti (due per titolo)")
check(abs(con.equity.iloc[-1]["totale"] - 2000) < 1e-6, "contabilita' invariata a mercato piatto")

# Il tetto per titolo impedisce di concentrare tutto su un nome.
r_molti = reco([(pd.Timestamp(g), ["AAA"]) for g in [D0, D1, GIORNI[44], GIORNI[66]]])
tetto = bt.Backtester(r_molti, mercato(piatto17, GIORNI),
                      bt.Parametri(**{**base17, "consenti_riacquisto": True,
                                      "max_lotti_per_titolo": 2})).esegui()
check(int(tetto.equity.iloc[-1]["posizioni"]) == 2, "tetto rispettato: al massimo 2 pacchetti su AAA")
check(tetto.metriche["duplicati_scartati"] >= 2, "i candidati oltre il tetto vengono scartati")

# --- 18. Dimensionamento sulla volatilita' ---------------------------------
print("[18] Size inversa alla volatilita'")
import numpy as np
_g = pd.bdate_range("2020-01-01", periods=200)
_rng = np.random.default_rng(3)
_a = 100 * np.cumprod(1 + _rng.normal(0, 0.02, len(_g)))   # oscilla il doppio
_b = 100 * np.cumprod(1 + _rng.normal(0, 0.01, len(_g)))
_righe = []
for _t, _s in (("AAA", _a), ("BBB", _b)):
    for _d, _p in zip(_g, _s):
        _righe.append({"date": _d, "ticker": _t, "open": _p, "high": _p, "low": _p, "close": _p})
m18 = bt.DatiMercato(pd.DataFrame(_righe), None, "USD")
d18 = _g[150]
vol_a, vol_b = m18.volatilita(d18, "AAA", 60), m18.volatilita(d18, "BBB", 60)
check(vol_a > vol_b * 1.5, "la volatilita' misurata riconosce il titolo piu' mosso")

# La volatilita' non deve includere il giorno stesso dell'acquisto.
serie_fino_a_ieri = pd.Series(_a, index=_g).pct_change().iloc[:_g.get_loc(d18)].tail(60)
atteso = float(serie_fino_a_ieri.std() * (252 ** 0.5))
check(abs(vol_a - atteso) < 1e-9, "usa solo i rendimenti precedenti al giorno di acquisto")

r18 = pd.DataFrame([{"data_articolo": d18, "ticker": t, "rank": i, "rating_score": 5,
                     "recommendation": "x"} for i, t in enumerate(["AAA", "BBB"], 1)])
def capitale_per_titolo(modo, limite=3.0):
    p = bt.Parametri(take_profit=999, stop_loss=None, capitale_iniziale=1000, versamento_mensile=0,
                     n_titoli=2, valuta="USD", dimensione_posizione=modo, vol_limite=limite)
    o = bt.Backtester(r18, m18, p).esegui().operazioni.set_index("ticker")
    return {t: float(o.loc[t, "quote"]) * float(o.loc[t, "prezzo_acquisto"]) for t in ("AAA", "BBB")}

pari = capitale_per_titolo("patrimonio")
check(abs(pari["AAA"] - pari["BBB"]) < 1.0, "a parti uguali i due titoli ricevono lo stesso capitale")
pesato = capitale_per_titolo("volatilita")
check(pesato["AAA"] < pari["AAA"], "il titolo piu' volatile riceve meno capitale di prima")
check(pesato["BBB"] > pari["BBB"], "quello piu' tranquillo ne riceve di piu'")
check(abs(sum(pesato.values()) - sum(pari.values())) < 1.0,
      "il capitale totale investito non cambia: si ridistribuisce soltanto")
stretto = capitale_per_titolo("volatilita", limite=1.0)
check(abs(stretto["AAA"] - stretto["BBB"]) < 1.0, "con tetto 1 il criterio torna a parti uguali")

# --- 19. La cassa non puo' mai andare in negativo --------------------------
print("[19] Nessuna leva involontaria")
# Caso critico: portafoglio grande e cassa piccola. Con il dimensionamento sul
# patrimonio la size di ogni posizione e' 1/n del totale, che puo' superare di
# molto la liquidita' disponibile: se non la si ricalcola dopo ogni acquisto si
# spende n volte la cassa e il conto va a debito.
_g19 = pd.bdate_range("2020-01-01", periods=300)
_d19 = [_g19[0], _g19[100], _g19[200]]
_serie19 = {t: [100.0 + i * 0.5 for i in range(len(_g19))] for t in ["AAA","BBB","CCC","DDD","EEE","FFF"]}
_m19 = mercato(_serie19, _g19)
_r19 = reco([(d, ["AAA","BBB","CCC","DDD","EEE","FFF"]) for d in _d19])
for modo in ("liquidita", "patrimonio", "volatilita"):
    ris19 = bt.Backtester(_r19, _m19, bt.Parametri(
        take_profit=10, stop_loss=None, capitale_iniziale=1000, versamento_mensile=1000,
        n_titoli=6, valuta="USD", dimensione_posizione=modo,
        consenti_riacquisto=True, max_lotti_per_titolo=3)).esegui()
    minimo = float(ris19.equity["liquidita"].min())
    check(minimo >= -1e-6, "modo '%s': la cassa non scende sotto zero (minimo %.2f)" % (modo, minimo))
    speso = float(ris19.equity["investito"].iloc[0] + ris19.equity["liquidita"].iloc[0])
    check(abs(speso - 1000) < 1e-6, "modo '%s': il primo mese non si investe piu' del versato" % modo)

print()
if FALLITI:
    print("TEST FALLITI: %d -> %s" % (len(FALLITI), FALLITI))
    sys.exit(1)
print("Tutti i test del backtester sono passati.")
