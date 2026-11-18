# Architecture

## Vue d'ensemble

```
             ┌──────────────── Données ────────────────┐
             │ CSV historiques vérifiés (rejeu)         │
             │ Broker (DEMO/LIVE)  ·  Calendrier éco.   │
             └───────────────┬──────────────────────────┘
                             ▼  MarketContext (bougies clôturées M5..H4, cotation)
 ┌───────────────────────────────────────────────────────────────────────┐
 │ BOT 1 Market │ BOT 2 Technique │ BOT 3 Price Action │ BOT 4 Macro      │
 │ BOT 5 Risk (pré-contrôle) │ BOT 6 Optimizer │ BOT 7 Exécution (état)   │
 └───────────────────────────────┬───────────────────────────────────────┘
                                 ▼ AgentReport (immutables, statut + signal + raisons)
 ┌───────────────────────────────────────────────────────────────────────┐
 │ BOT MAÎTRE                                                             │
 │  DecisionEngine : candidats (setups Price Action / stratégies validées)│
 │  ConsensusEngine : règles dures → score pondéré → classement           │
 └───────────────────────────────┬───────────────────────────────────────┘
                                 ▼ TradeProposal
 ┌───────────────────────────────────────────────────────────────────────┐
 │ BOT 5 RISK MANAGER — veto absolu, limites gelées, approbation HMAC     │
 └───────────────────────────────┬───────────────────────────────────────┘
                                 ▼ RiskApproval signée (TTL 60 s)
 ┌───────────────────────────────────────────────────────────────────────┐
 │ BOT 7 — OrderManager (idempotent) → BrokerInterface → PositionManager  │
 │         réconciliation, break-even, trailing, invalidation             │
 └───────────────────────────────────────────────────────────────────────┘
 Transversal : SafetyManager (kill switch / mode sécurité), PortfolioManager (objectif,
 P&L session/jour/semaine), AlertManager, AuditLog chaîné, Repository (PostgreSQL).
```

## Cycle du Bot Maître (`MasterController.run_cycle`)

1. **Connectivité broker** — reconnexion ; échec → mode sécurité + alerte.
2. **Réconciliation** — positions broker vs locales : trades clôturés comptabilisés, position inconnue → anomalie +
   mode sécurité, stop manquant chez le broker → restauré, stop broker plus large → re-resserré.
3. **Compte** — equity, balance, drawdown ; limites journalière/hebdo/drawdown surveillées.
4. **Gestion des positions** — toujours active, même si les entrées sont bloquées.
5. **Blocages d'entrée** — kill switch, mode sécurité, objectif atteint, marché fermé, hors session.
6. **Analyse** de chaque paire par les 7 agents.
7. **Décision** — consensus, classement, revue Risk Manager, exécution des seules propositions approuvées.

## Règles dures du consensus (le Maître peut refuser à 6 bots contre 1)

1. Un agent en `BLOCK` (annonce macro, spread/volatilité, broker) → refus.
2. Un agent d'analyse en `ERROR` / `INSUFFICIENT_DATA` → refus (données non vérifiées).
3. Données macro indisponibles → refus (sauf simulation explicitement configurée).
4. Un agent directionnel opposé → refus (contradiction).
5. Moins de 2 agents directionnels favorables → refus.
6. Stratégie incompatible avec le régime (ex. mean-reversion en tendance) → refus.
7. Seulement ensuite : score pondéré ≥ 0,45.
8. DEMO/LIVE : stratégie validée sur **données réelles** ; LIVE : en plus, liste blanche opérateur.

Le score de confiance n'est jamais suffisant à lui seul.

## Contrôles du Risk Manager (tous obligatoires)

Kill switch · mode sécurité · objectif de session · marché ouvert · stop-loss présent et du bon côté ·
take-profit cohérent · stop minimum · R:R minimum **au prix exécutable actuel** · cotation fraîche · spread max ·
spread/stop · dérive d'entrée · volatilité (pic d'ATR) · drawdown (actuel et projeté) · perte journalière
(actuelle et projetée) · perte hebdomadaire · positions max · doublon de paire · risque par trade (dimensionné au
**pire fill accepté**, slippage max inclus) · risque total ouvert · exposition par devise · corrélation · marge.

Anti-martingale : le risque « exceptionnel » (0,5 %) n'est accordé que sans perte consécutive, journée positive et
drawdown < moitié du max. Le risque n'augmente **jamais** après une perte ni pour atteindre l'objectif.

## Exécution (BOT 7)

- `client_order_id` déterministe dérivé de la proposition → aucun doublon, même après redémarrage.
- Timeout / déconnexion → statut `UNKNOWN`, **interrogation du broker avant tout renvoi**.
- Rejet explicite → jamais renvoyé.
- Un ordre n'est « exécuté » qu'une fois `FILLED` **et** visible dans les positions du broker.
- Stop-loss : uniquement resserrés ; tout élargissement lève `RiskViolationError`.

## Modes

| Mode | Broker requis | Particularités |
|---|---|---|
| SIMULATION | `SimulatedBroker` | Rejeu de données historiques (ou synthétiques, signalées partout) |
| DEMO | connecteur `environment="practice"` | Stratégies validées sur données réelles uniquement |
| LIVE | connecteur `environment="live"` | Garde LIVE (voir DEMO_TO_LIVE.md) + liste blanche des stratégies |

Le mode est lu une seule fois au démarrage (`MasterController.mode` en lecture seule) ; aucune API ne permet de le
changer ; une incohérence mode/broker empêche le démarrage.

## Arborescence

```
backend/
  main.py                 application FastAPI (factory)
  runtime.py              câblage + boucle autonome
  config/settings.py      configuration (.env), RiskLimits gelées
  core/                   types, erreurs, logs JSON masqués, instruments, horaires
  analysis/               indicateurs et structure de marché
  agents/                 les 7 bots
  master/                 consensus, décision, contrôleur, port de persistance
  trading/                interface broker, broker simulé, ordres, positions, portefeuille
  strategies/             trend following, breakout, pullback, mean reversion, momentum
  backtesting/            moteur, métriques, validation (WF/OOS/MC), rapports
  risk/                   moteur de risque, dimensionnement, sécurité
  data/                   données de marché, calendrier, générateur synthétique (tests)
  database/               modèles, dépôt, migrations Alembic
  api/                    routes, sécurité, WebSocket
  alerts/                 alertes multi-canaux
  security/               audit chaîné, garde LIVE
  tests/                  178 tests
frontend/                 dashboard React/TS/Tailwind
docker/  docs/  scripts/  data/historical/
```
