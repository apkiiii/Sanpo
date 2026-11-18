# Installation, lancement et configuration

## Prérequis

- Python 3.12 ou plus récent (testé avec 3.13)
- Node.js 20+ (testé avec 22) pour le dashboard
- PostgreSQL 16 (production) — SQLite suffit en local
- Docker + Docker Compose (optionnel)

## 1. Installation locale

```bash
cd forex-ai-master
python3.12 -m venv .venv
. .venv/bin/activate
pip install -r requirements-dev.txt     # inclut uvicorn[standard] (nécessaire au WebSocket)
pytest                                  # 177 tests + 1 test PostgreSQL optionnel
```

## 2. Configuration

```bash
cp .env.example .env
python scripts/generate_secret.py       # copier dans JWT_SECRET (>= 32 caractères)
python scripts/hash_password.py         # copier dans DASHBOARD_ADMIN_PASSWORD_HASH
```

> Le hash bcrypt contient des `$`. Dans `.env` lu par l'application ou Docker Compose, collez-le tel quel. Si vous
> chargez le fichier avec `source .env` dans un shell, mettez la valeur entre **apostrophes** :
> `DASHBOARD_ADMIN_PASSWORD_HASH='$2b$12$...'`.

Paramètres principaux (tous documentés dans `.env.example`) :

| Variable | Défaut | Rôle |
|---|---|---|
| `TRADING_MODE` | `simulation` | `simulation`, `demo` ou `live` |
| `RISK_*` | voir spec | limites de risque (gelées au démarrage) |
| `TARGET_SESSION_PROFIT` / `TARGET_ENABLED` | 1000 / true | cible de session (indicateur seulement) |
| `TARGET_REACHED_POLICY` | `breakeven` | `hold`, `breakeven` ou `close_all` une fois la cible atteinte |
| `SESSION_START_UTC` / `SESSION_END_UTC` | 07:00 / 21:00 | fenêtre de session |
| `EMERGENCY_CLOSE_POLICY` | `keep_with_stops` | action du kill switch sur les positions |
| `BLOCK_WHEN_CALENDAR_UNAVAILABLE` | true | bloque les entrées sans calendrier économique |
| `ECONOMIC_CALENDAR_PATH` | — | export CSV/JSON vérifié (voir ci-dessous) |
| `SIMULATION_DATA_SOURCE` | `csv` | `csv` (historique vérifié) ou `synthetic` (démo) |

### Calendrier économique

Le Bot 4 n'invente aucune donnée. Fournissez un export (CSV ou JSON) régulièrement mis à jour :

```json
{"generated_at": "2026-11-18T06:00:00Z",
 "events": [{"time": "2026-11-18T13:30:00Z", "currency": "USD", "impact": "high", "title": "CPI m/m"}]}
```

Sans fichier, ou si le fichier a plus de 7 jours, les entrées sont bloquées (`BLOCK_WHEN_CALENDAR_UNAVAILABLE=true`).

### Données historiques

Voir [data/historical/README.md](../data/historical/README.md). Puis :

```bash
python scripts/run_backtest.py --data-dir data/historical --timeframe M15
```

Ce script écrit le rapport dans `reports/` et la table de validation dans `var/strategy_validation.json`, utilisée
au démarrage par le Bot 6.

## 3. Lancement

```bash
# Backend (API + boucle de trading)
uvicorn --factory backend.main:get_app --host 127.0.0.1 --port 8000

# Dashboard (développement)
cd frontend && npm install && npm run dev      # http://localhost:5173

# Dashboard (production statique)
cd frontend && npm run build                   # dist/ servi par nginx (voir docker/nginx.conf)
```

Base PostgreSQL :

```bash
export DATABASE_URL=postgresql+psycopg://forex:<mot_de_passe>@localhost:5432/forex
alembic upgrade head
```

## 4. Docker Compose

```bash
cp .env.example .env    # renseigner JWT_SECRET, DASHBOARD_ADMIN_PASSWORD_HASH, POSTGRES_PASSWORD
docker compose up -d --build
# Dashboard : http://127.0.0.1:8080 (publié sur localhost uniquement)
```

Le conteneur backend applique les migrations au démarrage, tourne en utilisateur non-root, et monte
`data/historical` en lecture seule. *Les images n'ont pas pu être construites dans l'environnement de
développement (pas de démon Docker) : faites un premier `docker compose build` de vérification.*

## 5. Tests

```bash
pytest                         # tout
pytest -m critical             # tests critiques
python scripts/run_critical_tests.py   # génère var/critical_tests.json (requis pour LIVE)
FAM_TEST_POSTGRES_URL=postgresql+psycopg://user@host/db pytest backend/tests/test_postgres_integration.py
```

## 6. Exploitation

- Logs : JSON sur la sortie standard, secrets masqués.
- Alertes : dashboard toujours ; Telegram (`TELEGRAM_BOT_TOKEN`, `TELEGRAM_CHAT_ID`, niveau ≥ warning) ;
  e-mail (`SMTP_*`, niveau critique).
- Kill switch : onglet Sécurité du dashboard ou `POST /api/killswitch/activate` (admin).
- Redémarrage : l'état (kill switch, mode sécurité, P&L, positions et leurs métadonnées) est restauré depuis la base
  puis réconcilié avec le broker ; le journal d'audit est vérifié (démarrage refusé s'il a été altéré).
