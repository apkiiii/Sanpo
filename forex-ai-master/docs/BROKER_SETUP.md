# Configuration du broker

## État actuel

**Aucun broker réel n'est connecté.** Le projet fournit :

- `backend/trading/broker_interface.py` — le contrat `BrokerInterface` (connect, disconnect, get_account_balance,
  get_account_equity, get_positions, get_market_data, get_symbol_info, calculate_margin, place_order,
  modify_order, close_position, get_order_status, get_transaction_history, + get_quote, cancel_pending_orders) ;
- `backend/trading/simulated_broker.py` — un broker papier complet (SIMULATION et tests, avec injection de pannes) ;
- `backend/tests/test_broker_contract.py` — la suite de tests que tout connecteur doit passer ;
- `backend/trading/broker_factory.py` — refuse explicitement un broker inconnu (aucun repli silencieux).

Aucune API de broker n'a été inventée. Le connecteur réel sera écrit **après** votre choix, à partir de la
documentation officielle du broker.

## Informations à fournir

1. **Nom du broker** et **pays de résidence** (régulation : ESMA/FCA → levier 30:1 ; USA/NFA → règle FIFO et
   interdiction du hedging, ce qui change la gestion des positions).
2. Type d'accès : API REST/streaming officielle, FIX, ou passerelle MetaTrader 5 (paquet Python officiel `MetaTrader5`,
   Windows uniquement).
3. Un **compte démo** et ses identifiants (jamais dans le chat ni dans Git : uniquement dans `.env` ou un gestionnaire
   de secrets).
4. Devise du compte, levier, taille de contrat, pas de volume minimal.
5. Limites de requêtes de l'API.

## Exigences pour un connecteur

| Exigence | Pourquoi |
|---|---|
| Utiliser l'API **officielle** et documentée | fiabilité, support |
| `environment` = `"practice"` pour la démo, `"live"` pour le réel | le Maître refuse un mode incohérent |
| Idempotence de `place_order` via l'identifiant client du broker (ou interrogation avant renvoi) | aucun doublon après coupure |
| `get_order_status(client_id)` retourne `None` uniquement si l'ordre n'a jamais été reçu | résolution des statuts `UNKNOWN` |
| Stop-loss **attaché à l'ordre d'entrée** côté broker | protection même si le système s'arrête |
| Exceptions : `BrokerConnectionError` / `BrokerTimeoutError` (transitoires), `OrderRejectedError` (définitif) | politique de retry correcte |
| Respect des limites de requêtes (file + backoff) | éviter le bannissement |
| Vérification des permissions du jeton au `connect()` (lecture/trading, compte attendu) | éviter de trader sur le mauvais compte |
| Volumes en **unités** (pas en lots), prix arrondis à la précision de l'instrument | cohérence du dimensionnement |
| Bougies en UTC, bougie en cours exclue | pas de look-ahead |

## Étapes d'intégration

1. Créer `backend/trading/connectors/<broker>.py` implémentant `BrokerInterface`.
2. L'enregistrer dans `IMPLEMENTED_CONNECTORS` (`broker_factory.py`).
3. Ajouter une fabrique dans `CONNECTORS` de `test_broker_contract.py` pointant sur le **compte démo** et faire
   passer toute la suite.
4. Configurer `.env` : `TRADING_MODE=demo`, `BROKER_NAME=<broker>`, `BROKER_API_KEY`, `BROKER_ACCOUNT_ID`,
   `BROKER_ENVIRONMENT=practice`.
5. Lancer en DEMO et suivre [DEMO_TO_LIVE.md](DEMO_TO_LIVE.md).
