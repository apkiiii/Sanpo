# Forex AI Master — 7 bots + 1 Bot Maître

Système de trading Forex multi-agents, modulaire et sécurisé : 7 agents spécialisés analysent le marché, un **Bot
Maître** décide, un **Risk Manager** doté d'un veto absolu valide chaque ordre, et un moteur d'exécution
idempotent gère les positions. Un tableau de bord (React) permet de tout suivre en temps réel.

> **Avertissements importants**
> - Ce logiciel **ne garantit aucun profit**. L'objectif de 1 000 USD par session est une *cible*, jamais une promesse.
>   Il ne modifie jamais la taille des positions ni le risque.
> - **Aucun broker réel n'est connecté.** Seuls une interface abstraite et un broker *simulé* sont fournis. Les modes DEMO
>   et LIVE nécessitent un connecteur à écrire sur l'API officielle du broker que vous choisirez (voir
>   [docs/BROKER_SETUP.md](docs/BROKER_SETUP.md)).
> - **Aucun backtest sur données historiques réelles n'a été effectué** : l'environnement de développement n'avait pas
>   accès aux sources de données. Les seuls résultats produits l'ont été sur des prix **synthétiques** et n'ont aucune
>   valeur prédictive (voir [docs/BACKTEST_REPORT.md](docs/BACKTEST_REPORT.md)).
> - Le mode LIVE est désactivé par défaut et verrouillé par plusieurs conditions (voir [docs/DEMO_TO_LIVE.md](docs/DEMO_TO_LIVE.md)).

## Ce qui est implémenté et testé

| Composant | Fichiers | État |
|---|---|---|
| BOT 1 Market Analyst (multi-TF, tendance, régime, S/R, cassures, volatilité, liquidité) | `backend/agents/market_analyst.py` | ✅ testé |
| BOT 2 Technical Analyst (RSI, MACD, EMA 20/50/200, ATR, Bollinger, ADX, VWAP) | `backend/agents/technical_analyst.py` | ✅ testé |
| BOT 3 Price Action (BOS, CHoCH, pullback, rejets, patterns, entrée/SL/TP/invalidation) | `backend/agents/price_action_analyst.py` | ✅ testé |
| BOT 4 News & Macro (fenêtres d'annonces, blocage si calendrier absent/périmé) | `backend/agents/macro_analyst.py` | ✅ testé — nécessite une source de calendrier |
| BOT 5 Risk Manager (veto, 20+ contrôles, approbations signées HMAC) | `backend/agents/risk_manager.py`, `backend/risk/` | ✅ testé |
| BOT 6 Strategy Optimizer (walk-forward, OOS, Monte Carlo, plateau de paramètres) | `backend/agents/strategy_optimizer.py`, `backend/backtesting/` | ✅ testé |
| BOT 7 Execution & Trade Monitor (idempotence, statut réel, réconciliation) | `backend/agents/execution_monitor.py`, `backend/trading/` | ✅ testé |
| BOT MAÎTRE (consensus à règles dures, contradictions, classement, décisions motivées) | `backend/master/` | ✅ testé |
| Objectif de session / jour / semaine, P&L brut/net, commissions, swaps, slippage | `backend/trading/portfolio_manager.py` | ✅ testé |
| Kill switch, mode sécurité, journal d'audit chaîné (SHA-256) | `backend/risk/safety_manager.py`, `backend/security/` | ✅ testé |
| Backtest (spread, commissions, slippage, latence, swaps, gaps, fills partiels, rejets) | `backend/backtesting/engine.py` | ✅ testé (+ contrôle anti look-ahead) |
| API FastAPI (JWT, rôles, rate limit, anti brute-force) + WebSocket | `backend/api/`, `backend/main.py` | ✅ testé |
| Dashboard React + TypeScript + Tailwind | `frontend/` | ✅ build + test navigateur |
| PostgreSQL / SQLAlchemy / Alembic | `backend/database/` | ✅ migration + test d'intégration PostgreSQL 16 |
| Alertes Dashboard / Telegram / e-mail (secrets masqués) | `backend/alerts/manager.py` | ✅ testé (envoi réel non testé : pas d'identifiants) |
| Docker Compose | `docker-compose.yml`, `docker/` | ⚠️ configuration validée, images non construites (pas de démon Docker ici) |
| Connecteur broker réel | — | ❌ volontairement absent : broker non choisi |

**Tests : 181** (180 + 1 test d'intégration PostgreSQL activé par variable d'environnement), dont **123 critiques**
(`pytest -m critical`) qui conditionnent l'activation du mode LIVE.

## Démarrage rapide (simulation)

```bash
cd forex-ai-master
python3.12 -m venv .venv && . .venv/bin/activate
pip install -r requirements-dev.txt
pytest                                   # toute la suite
cp .env.example .env                     # puis renseigner JWT_SECRET et DASHBOARD_ADMIN_PASSWORD_HASH
python scripts/generate_secret.py        # -> JWT_SECRET
python scripts/hash_password.py          # -> DASHBOARD_ADMIN_PASSWORD_HASH (entre apostrophes si le fichier est "sourcé")

# Démonstration du pipeline sur données synthétiques (aucune valeur prédictive) :
SIMULATION_DATA_SOURCE=synthetic uvicorn --factory backend.main:get_app --port 8000
cd frontend && npm install && npm run dev   # http://localhost:5173
```

### Compte démo fictif (entraînement complet, aucun argent réel)

```bash
python scripts/run_demo.py                          # 100 000 $ fictifs, 10 jours de marché simulé
python scripts/run_demo.py --balance 10000 --days 20 --target 0
python scripts/run_demo.py --data csv --days 60     # sur vos données historiques réelles
```

Le script fait tourner les 7 bots, le Bot Maître, le Risk Manager et l'exécution sur le broker simulé, puis écrit un
bilan dans `reports/` (exemple : [docs/reports/demo_example.md](docs/reports/demo_example.md)). Sur données
synthétiques, le bilan valide le fonctionnement du système, pas sa rentabilité.

Pour une simulation sérieuse, placez des données historiques vérifiées dans `data/historical/`
(format : [data/historical/README.md](data/historical/README.md)), lancez `python scripts/run_backtest.py`, puis
`SIMULATION_DATA_SOURCE=csv`.

## Documentation

- [docs/PLAN.md](docs/PLAN.md) — analyse, risques techniques, informations manquantes, plan
- [docs/ARCHITECTURE.md](docs/ARCHITECTURE.md) — architecture, flux de décision, règles
- [docs/INSTALL.md](docs/INSTALL.md) — installation, lancement, configuration, Docker
- [docs/BROKER_SETUP.md](docs/BROKER_SETUP.md) — configuration et intégration d'un broker
- [docs/SECURITY_REPORT.md](docs/SECURITY_REPORT.md) — rapport de sécurité
- [docs/BACKTEST_REPORT.md](docs/BACKTEST_REPORT.md) — rapport de backtesting
- [docs/DEMO_TO_LIVE.md](docs/DEMO_TO_LIVE.md) — guide de passage DEMO → LIVE
