# Guide de passage SIMULATION → DEMO → LIVE

Le système ne passe **jamais** seul d'un mode à l'autre. Chaque étape est une décision humaine, prise après
vérification de critères objectifs. Si un critère n'est pas rempli, on ne passe pas.

## Étape 1 — SIMULATION (aucun ordre réel)

1. `pytest` : toute la suite passe.
2. Données historiques réelles dans `data/historical/` ; `python scripts/run_backtest.py`.
3. Examiner le rapport : au moins une stratégie éligible **hors échantillon**, résultats stables entre plis,
   toujours positifs avec des coûts majorés de 50 %.
4. Rejeu complet en `SIMULATION_DATA_SOURCE=csv` sur une période non utilisée par l'optimisation ; vérifier dans le
   dashboard : aucun dépassement de limite, alertes cohérentes, kill switch testé.

## Étape 2 — DEMO (compte de démonstration du broker)

1. Choisir le broker, écrire et tester le connecteur ([BROKER_SETUP.md](BROKER_SETUP.md)) — `test_broker_contract.py`
   doit passer contre le compte démo.
2. Fournir un calendrier économique vérifié (`ECONOMIC_CALENDAR_PATH`) et l'actualiser automatiquement.
3. `.env` : `TRADING_MODE=demo`, `BROKER_ENVIRONMENT=practice`, PostgreSQL, alertes Telegram/e-mail.
4. Durée minimale recommandée : **3 mois** et ≥ 50 trades clôturés (exigé par la garde LIVE).
5. Tests de sécurité **en démo**, à faire volontairement :
   - couper le réseau pendant une position ouverte → mode sécurité, stop toujours présent chez le broker, reprise propre ;
   - redémarrer le backend avec positions ouvertes → réconciliation sans doublon ni anomalie ;
   - ouvrir manuellement une position sur le compte → anomalie détectée, entrées bloquées ;
   - activer le kill switch → comportement conforme à `EMERGENCY_CLOSE_POLICY` ;
   - vérifier que la perte journalière bloque bien les entrées.
6. Comparer les résultats démo au backtest (même période) : un écart important (slippage, spreads, fréquence des
   trades) invalide le backtest — retour à l'étape 1.

## Étape 3 — LIVE (argent réel)

Prérequis cumulatifs, vérifiés automatiquement au démarrage (`backend/security/live_guard.py`) :

| Condition | Comment la remplir |
|---|---|
| `TRADING_MODE=live` | `.env` |
| `LIVE_TRADING_ENABLED=true` | `.env` |
| `LIVE_CONFIRMATION_PHRASE` exacte | `I UNDERSTAND THAT LIVE TRADING CAN LOSE REAL MONEY` |
| Connecteur `environment="live"` | `BROKER_ENVIRONMENT=live` dans votre connecteur |
| Rapport de tests critiques < 7 jours, 0 échec, sur le code actuel | `python scripts/run_critical_tests.py` |
| ≥ `LIVE_MIN_DEMO_TRADES` trades DEMO clôturés en base | étape 2 |

Et une condition supplémentaire côté décision : seules les stratégies listées dans `LIVE_APPROVED_STRATEGIES`
(par exemple `pullback,breakout`) peuvent trader en LIVE.

Recommandations avant d'activer :

1. Commencer avec un capital que vous pouvez perdre entièrement, et des limites **plus strictes** que la démo
   (par ex. `RISK_MAX_RISK_PER_TRADE_PCT=0.10`, `RISK_MAX_OPEN_POSITIONS=1`).
2. Désactiver ou baisser l'objectif (`TARGET_ENABLED=false`) les premières semaines : il n'a aucun effet sur le risque
   mais évite toute pression psychologique.
3. Surveiller quotidiennement les premières semaines ; garder le kill switch accessible (dashboard / API).
4. Toute modification du code invalide le rapport de tests : relancer `run_critical_tests.py`, et idéalement repasser
   par la démo.

## Ordre de grandeur de l'objectif de 1 000 USD

Avec 0,25 % de risque par trade et un ratio de 2:1, un trade gagnant rapporte au mieux 0,5 % du capital (avant
coûts). Atteindre 1 000 USD nets en une session demande donc environ **200 000 USD** de capital et un trade gagnant
sans aucune perte, ou 100 000 USD et deux gains sans aucune perte — davantage en conditions normales. Sur un compte plus petit, l'objectif ne sera généralement pas atteint — c'est le
comportement voulu : le système n'augmente jamais le risque pour l'atteindre.

## Revenir en arrière

À tout moment : kill switch (immédiat), puis `TRADING_MODE=demo` ou `simulation` et redémarrage. Les positions
réelles éventuellement ouvertes restent protégées par leurs stops côté broker.
