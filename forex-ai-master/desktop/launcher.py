"""Forex AI Master — desktop launcher (built into ForexAIMaster.exe).

* Data folder: %APPDATA%\\ForexAIMaster (Windows) or ~/.ForexAIMaster (other systems).
* Runs in SIMULATION mode with a fictitious demo account (no real order can be sent).
* The server listens on 127.0.0.1 only; the dashboard opens in the default browser.
* Settings can be changed in settings.env inside the data folder.

Options:
    --no-browser   do not open the browser
    --check        start, verify the API and the dashboard answer, then exit (used by the build)
    --port N       preferred port (default 8765)
"""

from __future__ import annotations

import argparse
import os
import secrets
import socket
import sys
import threading
import time
import urllib.request
import webbrowser
from pathlib import Path

DEFAULTS = {
    "TRADING_MODE": "simulation",
    "SIMULATION_DATA_SOURCE": "synthetic",
    "SIMULATION_STEP_SECONDS": "1",
    "INITIAL_BALANCE": "40",
    "MIN_TRADE_UNITS": "1",
    "TRADE_UNITS_STEP": "1",
    "BLOCK_WHEN_CALENDAR_UNAVAILABLE": "false",
    "LOG_LEVEL": "WARNING",
}

SETTINGS_TEMPLATE = """# Forex AI Master - réglages de l'application de bureau.
# Modifiez une valeur, enregistrez, puis redémarrez le logiciel.
# Les limites de risque se règlent ici (elles ne sont jamais modifiables depuis le logiciel).

# Capital fictif au tout premier lancement (ensuite : boutons Déposer / Retirer)
INITIAL_BALANCE=40
# Plus petite position en unités (1000 = micro-lot ; 1 chez certains brokers)
MIN_TRADE_UNITS=1
TRADE_UNITS_STEP=1
# Vitesse du marché simulé : secondes réelles par bougie de 5 minutes
SIMULATION_STEP_SECONDS=1

RISK_MAX_RISK_PER_TRADE_PCT=0.25
RISK_MAX_DAILY_LOSS_PCT=1.0
RISK_MAX_DRAWDOWN_PCT=5.0
RISK_MAX_OPEN_POSITIONS=3

TARGET_SESSION_PROFIT=1000
TARGET_ENABLED=true
"""


def data_dir() -> Path:
    base = os.environ.get("APPDATA") or str(Path.home())
    d = Path(base) / ("ForexAIMaster" if os.environ.get("APPDATA") else ".ForexAIMaster")
    d.mkdir(parents=True, exist_ok=True)
    return d


def prepare_environment(d: Path) -> None:
    settings_file = d / "settings.env"
    if not settings_file.exists():
        settings_file.write_text(SETTINGS_TEMPLATE, encoding="utf-8")
    for line in settings_file.read_text(encoding="utf-8").splitlines():
        line = line.strip()
        if line and not line.startswith("#") and "=" in line:
            key, value = line.split("=", 1)
            os.environ.setdefault(key.strip(), value.strip())
    secret_file = d / "secret.key"
    if not secret_file.exists():
        secret_file.write_text(secrets.token_urlsafe(48), encoding="utf-8")
    os.environ.setdefault("JWT_SECRET", secret_file.read_text(encoding="utf-8").strip())
    for k, v in DEFAULTS.items():
        os.environ.setdefault(k, v)
    os.environ.setdefault("DATABASE_URL", f"sqlite:///{(d / 'forex_ai_master.db').as_posix()}")
    os.environ.setdefault("LOCAL_AUTH_PATH", str(d / "local_auth.json"))
    os.environ.setdefault("STRATEGY_VALIDATION_PATH", str(d / "strategy_validation.json"))
    os.environ.setdefault("HISTORICAL_DATA_DIR", str(d / "historical"))
    os.environ["TRADING_MODE"] = "simulation"  # the desktop build never trades real money
    os.chdir(d)


def free_port(preferred: int) -> int:
    for port in [preferred, *range(preferred + 1, preferred + 50)]:
        with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as s:
            try:
                s.bind(("127.0.0.1", port))
                return port
            except OSError:
                continue
    raise SystemExit("no free local port")


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--no-browser", action="store_true")
    ap.add_argument("--check", action="store_true")
    ap.add_argument("--port", type=int, default=8765)
    args = ap.parse_args()

    d = data_dir()
    prepare_environment(d)
    port = free_port(args.port)
    os.environ.setdefault("CORS_ORIGINS", f"http://127.0.0.1:{port}")

    import uvicorn

    from backend.config.settings import Settings
    from backend.core.logging import configure_logging
    from backend.main import create_app, frontend_dist

    settings = Settings(_env_file=None)
    configure_logging(settings.log_level)
    app = create_app(settings)
    server = uvicorn.Server(uvicorn.Config(app, host="127.0.0.1", port=port, log_level="warning",
                                           ws="websockets", lifespan="on"))
    thread = threading.Thread(target=server.run, daemon=True)
    thread.start()
    for _ in range(600):
        if server.started or not thread.is_alive():
            break
        time.sleep(0.1)
    if not server.started:
        print("Le serveur n'a pas démarré. Consultez les messages ci-dessus.")
        return 1
    url = f"http://127.0.0.1:{port}/"

    if args.check:
        ok = True
        try:
            with urllib.request.urlopen(url + "api/health", timeout=10) as r:
                ok &= r.status == 200
            with urllib.request.urlopen(url, timeout=10) as r:
                ok &= r.status == 200 and b"Forex AI Master" in r.read()
        except Exception as exc:  # noqa: BLE001
            print(f"check failed: {exc}")
            ok = False
        print("CHECK OK" if ok else "CHECK FAILED", "| dashboard:", frontend_dist())
        server.should_exit = True
        thread.join(timeout=15)
        return 0 if ok else 1

    sys.stdout.reconfigure(line_buffering=True)  # type: ignore[union-attr]
    print("=" * 64)
    print(" Forex AI Master - compte démo fictif (aucun ordre réel)")
    print(f" Tableau de bord : {url}")
    print(f" Données et réglages : {d}")
    print(" Fermez cette fenêtre pour arrêter le logiciel.")
    print("=" * 64)
    if not args.no_browser:
        webbrowser.open(url)
    try:
        while thread.is_alive():
            thread.join(0.5)
    except KeyboardInterrupt:
        server.should_exit = True
        thread.join(timeout=15)
    return 0


if __name__ == "__main__":
    sys.exit(main())
