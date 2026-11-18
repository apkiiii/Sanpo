# Rapport de sécurité

Périmètre : code de `forex-ai-master/` à la date de livraison. Vérifié par 178 tests automatisés (dont 123
critiques), un test d'intégration PostgreSQL 16 et un test navigateur de bout en bout (Playwright).

## 1. Protection du capital

| Mesure | Implémentation | Test |
|---|---|---|
| Veto absolu du Risk Manager | `RiskEngine.evaluate` : 26 contrôles, tous obligatoires | `test_risk.py` |
| Approbations infalsifiables | HMAC-SHA256 avec clé aléatoire privée au moteur, TTL 60 s, vérifiée par l'OrderManager (symbole, sens, stop, volume) | `test_forged_or_expired_approval_rejected`, `test_order_requires_valid_approval` |
| Limites non modifiables par le Maître | `RiskLimits` gelé (pydantic frozen), attribut privé, propriété en lecture seule, aucune route API | `test_master_cannot_modify_risk_limits`, `test_no_endpoint_can_change_mode_or_risk_limits` |
| Pas de trade sans stop-loss | contrôle `stop_loss` + stop attaché à l'ordre broker + restauration en réconciliation | `test_invalid_setups_rejected`, `test_reconciliation_restores_missing_stop` |
| Stop jamais élargi | `PositionManager.modify_stop` lève `RiskViolationError` | `test_stop_can_never_be_widened` |
| Risque réel ≤ limite même après dérive et slippage | dimensionnement au **pire fill accepté** (prix exécutable + slippage max) | `test_realised_risk_never_exceeds_limit_even_with_max_slippage`, `test_sizing_uses_executable_price_after_drift` |
| Anti-martingale | risque exceptionnel refusé après perte / journée négative / drawdown | `test_no_martingale_exceptional_risk` |
| Objectif sans effet sur le risque | la cible ne participe à aucun calcul de taille | `test_risk_never_depends_on_target_gap`, `test_target_change_is_audited_and_does_not_touch_risk` |
| Limites journalière, hebdo, drawdown (actuelles **et projetées**) | `RiskEngine` + backtest aligné | `test_daily_loss_limit`, `test_drawdown_limit_triggers_safety`, `test_daily_loss_halt_blocks_new_entries` |
| Exposition devise, corrélation, marge, positions max | `RiskEngine` | `test_total_open_risk_and_currency_exposure`, `test_correlation_check`, `test_margin_check` |

## 2. Fiabilité de l'exécution

- Identifiant client déterministe → aucun doublon (y compris après redémarrage) : `test_duplicate_submission_is_ignored`, `test_broker_level_idempotency`.
- Réponse perdue après exécution → statut `UNKNOWN` résolu par interrogation, sans renvoi : `test_timeout_after_execution_does_not_duplicate`.
- Rejets jamais rejoués : `test_rejected_order_is_not_retried`.
- Confirmation exigée (FILLED + position visible) : `test_successful_order_is_confirmed`.
- Réconciliation : positions inconnues → anomalie + mode sécurité ; positions disparues → signalées : `test_unknown_position_triggers_anomaly`, `test_reconciliation_unknown_and_missing`.
- Déconnexion broker → mode sécurité, levé automatiquement à la reconnexion : `test_broker_disconnection_enters_safety_mode_and_recovers`.
- Redémarrage : état et métadonnées restaurés, protections jamais levées par la restauration : `test_restart_restores_state_and_reconciles`, `test_safety_restore_never_clears`.

## 3. Kill switch

Active → (1) bloque les entrées, (2) annule les ordres en attente, (3) conserve la surveillance des positions,
(4) applique `EMERGENCY_CLOSE_POLICY`, (5) alerte critique, (6) entrée d'audit. Libération uniquement par un
opérateur avec confirmation explicite. Tests : `test_kill_switch_*`, `test_kill_switch_via_api`, test navigateur.

## 4. Mode LIVE

Désactivé par défaut ; exige simultanément `TRADING_MODE=live`, `LIVE_TRADING_ENABLED=true`, la phrase de
confirmation exacte, un connecteur `environment="live"`, un rapport de tests critiques récent (< 7 jours), sans
échec et **calculé sur le code actuel** (hash SHA-256 des sources), et ≥ 50 trades DEMO clôturés. Aucune bascule
automatique DEMO → LIVE ; aucune API pour changer de mode. Tests : `test_security.py`, `test_mode_must_match_broker_environment`.

## 5. Application et secrets

| Mesure | Détail |
|---|---|
| Secrets | uniquement via variables d'environnement ; `SecretStr` ; `.env` ignoré par Git ; `.env.example` sans valeur (testé) |
| Masquage | logs JSON et alertes passés par `redact()` (valeurs enregistrées + motifs clé/jeton/Bearer/token Telegram) |
| Mots de passe | bcrypt ; aucun mot de passe par défaut (connexion désactivée sans hash) ; temps constant si l'utilisateur n'existe pas |
| Authentification | JWT HS256, expiration courte, secret ≥ 32 caractères obligatoire hors simulation |
| Permissions | rôles `admin` (actions) / `viewer` (lecture) ; journal d'audit réservé à l'admin |
| Brute force | verrouillage après 5 échecs / 15 min par IP + limite dédiée sur `/api/auth/login` |
| Limitation de débit | fenêtre glissante par IP sur toute l'API |
| Validation des entrées | modèles Pydantic bornés (longueurs, plages) |
| En-têtes | `nosniff`, `X-Frame-Options: DENY`, CSP restrictive, `no-referrer`, `no-store` |
| Erreurs | aucune trace interne renvoyée au client |
| WebSocket | jeton exigé dans le premier message (pas dans l'URL, donc pas dans les logs d'accès) |
| Audit | chaîne de hachage SHA-256 ; altération détectée → **refus de démarrer** (`test_tampered_audit_log_is_detected`) |
| Agents IA | aucun accès au système de fichiers, au shell ou au broker : fonctions pures retournant des rapports |
| Docker | utilisateur non-root, données montées en lecture seule, dashboard publié sur 127.0.0.1 uniquement |
| Scan | test d'absence de secrets codés en dur dans les sources (`test_no_hardcoded_secrets_in_source`) |

## 6. Défauts trouvés et corrigés pendant le développement

1. Le backtest ne vérifiait que la perte journalière *réalisée* (le live vérifie aussi la perte projetée) → aligné.
2. Le dimensionnement utilisait l'entrée planifiée : après une dérive défavorable, le risque réel dépassait 0,25 % → dimensionnement au prix exécutable + slippage max.
3. L'OrderManager mesurait le slippage par rapport à l'entrée planifiée, ce qui rejetait des trades approuvés → mesuré par rapport au prix exécutable.
4. L'expiration des approbations utilisait l'horloge murale, incompatible avec le rejeu → horloge du cycle.

## 7. Risques résiduels et recommandations

- **Connecteur broker absent** : la sécurité réelle de l'exécution dépendra de sa qualité ; il doit passer
  `test_broker_contract.py` sur compte démo.
- **Processus unique** : l'état anti-brute-force et la limitation de débit sont en mémoire (suffisant pour un seul
  processus ; prévoir Redis si plusieurs instances).
- **TLS** non géré par l'application : placer un reverse proxy HTTPS devant le dashboard s'il est exposé hors de la
  machine ; idéalement accès via VPN.
- **Jours fériés / liquidité réduite** non modélisés : le drapeau `tradeable` du broker fait foi.
- **Hébergement 24/5** : prévoir supervision (redémarrage automatique Docker `unless-stopped`, alerte si le
  heartbeat s'arrête) et sauvegardes PostgreSQL.
- **Envois Telegram/e-mail** non testés avec de vrais identifiants.
- Faire réaliser un audit indépendant avant tout passage en LIVE.
