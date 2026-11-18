# Plan de développement — Forex AI Master

## 1. Analyse de l'environnement (constats réels)

| Élément | Constat |
|---|---|
| Dépôt existant | Contient une application web sans rapport (SANPO). Le projet est créé dans `forex-ai-master/` sans modifier l'existant. |
| Python | 3.13 disponible (le code cible 3.12+). |
| Node.js | 22.x disponible (frontend React/Vite). |
| PostgreSQL | Client 16 disponible ; les tests utilisent SQLite en mémoire, la prod PostgreSQL. |
| Réseau | **Les sources de données de marché (Yahoo, Dukascopy, Stooq, HistData, OANDA, ForexFactory) sont bloquées** dans l'environnement de développement. Aucun backtest sur données réelles n'a donc pu être exécuté ici. |
| Broker | **Non choisi.** Interface abstraite + broker simulé uniquement. |

## 2. Dépendances retenues (minimales)

Backend : `fastapi`, `uvicorn`, `pydantic`, `pydantic-settings`, `sqlalchemy`, `alembic`, `psycopg[binary]` (prod), `pandas`, `numpy`, `httpx` (Telegram + tests API), `bcrypt`, `pyjwt`.
Tests : `pytest`, `pytest-asyncio`, `aiosqlite` n'est pas nécessaire (accès DB synchrone isolé).
Frontend : `react`, `react-dom`, `vite`, `typescript`, `tailwindcss`, `postcss`, `autoprefixer`.
Volontairement **non** inclus : Redis (inutile à ce stade : un seul processus), bibliothèques de backtesting tierces (moteur maison pour maîtriser spread/slippage/swap/gaps), bibliothèques d'indicateurs (implémentations vectorisées testées).

## 3. Architecture

```
                 ┌───────────────────────────────────────────┐
 Données marché ─► Agents d'analyse (1 Market, 2 Technique,   │
 (broker / CSV)  │ 3 Price Action, 4 Macro, 6 Optimizer)      │
                 └───────────────┬───────────────────────────┘
                                 │ AgentReport (immutables)
                 ┌───────────────▼───────────────────────────┐
                 │ BOT MAÎTRE : ConsensusEngine →             │
                 │ DecisionEngine → MasterController          │
                 └───────────────┬───────────────────────────┘
                                 │ TradeProposal
                 ┌───────────────▼───────────────────────────┐
                 │ BOT 5 RISK MANAGER (veto absolu,           │
                 │ limites gelées, non modifiables par Maître)│
                 └───────────────┬───────────────────────────┘
                                 │ RiskApproval (signée par le RiskManager)
                 ┌───────────────▼───────────────────────────┐
                 │ BOT 7 EXECUTION : OrderManager (idempotent)│
                 │ PositionManager, réconciliation            │
                 └───────────────┬───────────────────────────┘
                                 │ BrokerInterface
                 Simulé │ Démo (à brancher) │ Live (verrouillé)
```

Transversal : `SafetyManager` (kill switch, mode sécurité), `SessionTracker` (objectif 1 000 USD), `AlertManager`, `AuditLog` chaîné par hash, API FastAPI + WebSocket, dashboard React.

## 4. Risques techniques identifiés

1. **Absence de données réelles** → toute performance mesurée ici est sans valeur ; il faut fournir des données historiques vérifiées.
2. **Broker non choisi** → l'exécution démo/live n'est pas branchée. Chaque API broker a ses particularités (unités vs lots, FIFO US, hedging, arrondis de prix).
3. **Lookahead bias / overfitting** en backtest → signaux calculés sur barre clôturée, exécution à l'ouverture suivante, walk-forward + out-of-sample + Monte Carlo.
4. **Ordres dupliqués** lors de déconnexions → identifiants client uniques + interrogation du statut avant tout renvoi.
5. **Divergence état local / broker** → réconciliation au démarrage et périodique, positions inconnues = anomalie + mode sécurité.
6. **Données macro** non vérifiées → aucune donnée inventée ; calendrier indisponible = blocage des entrées (configurable).
7. **Objectif 1 000 USD** : avec 0,25 % de risque par trade, il faut un capital très important (≈ 400 000 USD pour 1 000 USD = 1 R). Sur un petit compte l'objectif ne sera presque jamais atteint ; c'est voulu : le risque n'est jamais augmenté pour l'atteindre.

## 5. Informations manquantes (à fournir)

- Broker choisi (et pays de résidence : contraintes réglementaires, ex. FIFO aux USA).
- Devise du compte, capital de départ, levier.
- Source de données historiques (fichiers CSV M5 sur plusieurs années).
- Source de calendrier économique (fournisseur + clé API) ou export CSV/JSON.
- Horaires de « session » souhaités (ex. Londres + New York).
- Identifiants Telegram / SMTP pour les alertes.

## 6. Étapes

1. Analyse & architecture ✅
2. Configuration, types, logs, erreurs, instruments ✅
3. Indicateurs + 7 bots (testables isolément) ✅
4. Bot Maître (consensus, décision, contradictions) ✅
5. Moteur de trading en simulation (broker simulé, ordres, positions, réconciliation, kill switch) ✅
6. Backtesting (coûts réalistes, walk-forward, Monte Carlo, rapports) ✅ — moteur prêt, **aucune donnée réelle utilisée**
7. Connecteur broker : interface + simulé + tests de contrat ✅ — **connecteur réel en attente du choix du broker**
8. Dashboard (API FastAPI, WebSocket, React) ✅
9. Tests de sécurité et scénarios extrêmes, garde LIVE ✅
10. Documentation ✅
