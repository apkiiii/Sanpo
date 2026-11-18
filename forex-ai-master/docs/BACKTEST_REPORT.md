# Rapport de backtesting

## Résumé honnête

**Aucun backtest sur données historiques réelles n'a été réalisé.** L'environnement de développement bloquait
l'accès à toutes les sources de données de marché testées (Yahoo Finance, Dukascopy, Stooq, HistData, OANDA). Les
seuls backtests exécutés l'ont été sur des **prix synthétiques** générés aléatoirement. Ils démontrent que le moteur
fonctionne de bout en bout ; ils ne disent **rien** sur la rentabilité d'une stratégie sur le vrai marché.

Conséquence technique : le Bot 6 marque toute validation sur données synthétiques comme **non éligible**, et le Bot
Maître refuse en DEMO/LIVE toute stratégie qui n'a pas été validée sur des données réelles.

## Ce que fait le moteur (`backend/backtesting/engine.py`)

| Élément | Modélisation |
|---|---|
| Exécution | signal à la clôture de la bougie *t*, exécution à l'ouverture de *t+1* |
| Spread | par paire (EURUSD 0,8 pip … NZDUSD 1,6) ou colonne `spread` des données |
| Commissions | 3,5 USD par lot et par côté (configurable) |
| Slippage | aléatoire défavorable sur les ordres au marché + supplément sur les stops |
| Latence | 150 ms par défaut, convertie en slippage additionnel |
| Swaps | à chaque rollover 22:00 UTC, triple le mercredi (−0,6 pip/nuit par défaut, prudent) |
| Gaps | ouverture au-delà du stop → sortie à l'ouverture (pire que le stop) |
| Ambiguïté intra-barre | stop et objectif touchés dans la même barre → **stop supposé en premier** |
| Fills partiels | au-delà d'un seuil de liquidité, le reliquat est exécuté à un prix dégradé |
| Rejets d'ordres | probabilité configurable |
| Sessions | pas d'entrée marché fermé, ni vendredi après 20:00 UTC, ni à la réouverture du dimanche |
| Risque | mêmes règles que le live : 0,25 %/trade, perte journalière projetée ≤ 1 %, drawdown projeté ≤ 5 % |

Métriques : profit net, profit factor, win rate, espérance (USD et R), drawdown max, Sharpe, Sortino, nombre de
trades, durée moyenne, ventilation par paire, stratégie et session, coûts totaux.

Validation (`backend/backtesting/validation.py`) : walk-forward glissant (optimisation sur un segment, test sur le
suivant), sélection par **score ajusté du risque** (espérance en R × √n − pénalité de drawdown, pas le profit brut),
ratio de « plateau » de paramètres (robustesse), efficacité walk-forward, Monte Carlo (1 000 rééchantillonnages).

Critères d'éligibilité : ≥ 30 trades hors échantillon, PF ≥ 1,1, espérance > 0, efficacité WF ≥ 0,3, drawdown
Monte Carlo p95 ≤ 5 %, plateau ≥ 40 %, **et données réelles**.

## Contrôle anti look-ahead

Sur une marche aléatoire **sans dérive** (aucun avantage possible), les 5 stratégies doivent avoir une espérance
nulle ou négative après coûts. Résultat mesuré (8 séries × 12 000 bougies H1) :

| Stratégie | Trades | Espérance (R) | Erreur-type |
|---|---|---|---|
| trend_following | 461 | +0,051 | 0,067 (non significatif) |
| breakout | 2 001 | −0,043 | 0,032 |
| pullback | 2 925 | −0,073 | 0,026 |
| mean_reversion | 240 | −0,073 | 0,088 |
| momentum | 2 396 | −0,087 | 0,029 |

Aucun avantage significatif → pas de fuite d'information future détectée. Ce contrôle est un test automatique
permanent (`test_no_edge_on_pure_random_walk`), complété par un test de causalité (tronquer le futur ne modifie pas
les signaux passés).

## Run de vérification du pipeline (données synthétiques)

`python scripts/run_backtest.py --synthetic --timeframe H1` — 7 paires × 5 stratégies, 12 000 bougies H1 chacune,
4 plis walk-forward, résultats hors échantillon uniquement. Rapport complet :
[reports/backtest_synthetic_example.md](reports/backtest_synthetic_example.md).

| Stratégie | Trades OOS (7 paires) | Profit net cumulé | Paires avec PF > 1 | Éligibles |
|---|---|---|---|---|
| trend_following | 310 | −2 865 | 1/7 | 0/7 |
| breakout | 1 191 | +36 833 | 6/7 | 0/7 |
| pullback | 1 483 | +106 144 | 5/7 | 0/7 |
| mean_reversion | 458 | −27 080 | 1/7 | 0/7 |
| momentum | 1 277 | +84 500 | 7/7 | 0/7 |

**Ces chiffres n'ont aucune valeur.** Le générateur synthétique injecte volontairement des régimes de dérive
(tendances artificielles) que les stratégies de suivi exploitent mécaniquement ; sur une marche aléatoire pure,
ces mêmes stratégies perdent les coûts (tableau précédent). 0 stratégie sur 35 n'est éligible, comme attendu.

## Pour obtenir un vrai rapport

1. Placer 3 à 5 ans de données M1/M5 vérifiées dans `data/historical/` (format dans `data/historical/README.md`).
2. `python scripts/run_backtest.py --data-dir data/historical --timeframe M15`
3. Examiner le rapport : résultats **hors échantillon** uniquement, stabilité entre plis, sensibilité aux coûts
   (relancer avec spreads/commissions majorés), Monte Carlo.
4. Même une stratégie éligible n'est pas activée en LIVE automatiquement : elle doit être validée en DEMO puis ajoutée
   explicitement à `LIVE_APPROVED_STRATEGIES`.

Des résultats historiques, même réels, ne sont jamais une promesse de profit futur.
