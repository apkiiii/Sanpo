# Données historiques

Placez ici des données **vérifiées** (aucun fichier n'est fourni). Nommage :

- `<SYMBOL>_M1.csv` ou `<SYMBOL>_M5.csv` (base, rééchantillonnée vers M15/M30/H1/H4), ou
- `<SYMBOL>_<TF>.csv` pour un timeframe précis (ex. `EURUSD_H1.csv`).

Formats reconnus automatiquement :

| Format | Exemple de ligne | Fuseau |
|---|---|---|
| Générique avec en-tête | `time,open,high,low,close,volume` puis `2024-01-02T00:00:00Z,1.1040,...` | celui indiqué (UTC recommandé) |
| HistData ASCII M1 | `20240102 170000;1.10391;1.10395;1.10389;1.10392;0` | EST sans DST → converti en UTC (+5 h) |
| Export MetaTrader 5 | `<DATE>\t<TIME>\t<OPEN>\t<HIGH>\t<LOW>\t<CLOSE>\t<TICKVOL>...` | supposé UTC — **vérifiez le fuseau de votre broker** |

Chaque jeu est validé (`validate_ohlc`) : NaN, prix ≤ 0, high/low incohérents, doublons, ordre, trous.
Recommandation : au moins 3 à 5 ans de M5 par paire pour un walk-forward significatif.
