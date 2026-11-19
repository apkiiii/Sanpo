"""Forex AI Master — desktop application (ForexAIMaster.exe).

* Native window (Microsoft Edge WebView2 on Windows) showing the dashboard; no console window.
* Data folder: %APPDATA%\\ForexAIMaster (Windows) or ~/.ForexAIMaster (other systems).
* SIMULATION mode only: fictitious demo account, no real order can be sent.
* The local server listens on 127.0.0.1 only and stops when the window is closed.
* A single instance runs at a time (the demo account is never opened twice).
* Messages are written to forex_ai_master.log in the data folder.

Options:
    --check              start the server, verify the API and the dashboard answer, exit (build check)
    --smoke-gui FILE     open the window, check the page loaded, write the result to FILE, exit (build check)
    --browser            use the default browser instead of the application window
    --port N             preferred port (default 8765)
"""

from __future__ import annotations

import argparse
import json
import logging
import os
import secrets
import socket
import sys
import threading
import time
import urllib.request
import webbrowser
from pathlib import Path

APP_NAME = "Forex AI Master"
LOCK_PORT = 8764  # held for the life of the process: guarantees a single instance

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

SETTINGS_TEMPLATE = """# Forex AI Master - réglages de l'application.
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

log = logging.getLogger("desktop")


# --------------------------------------------------------------------------- environment
def data_dir() -> Path:
    appdata = os.environ.get("APPDATA")
    d = Path(appdata) / "ForexAIMaster" if appdata else Path.home() / ".ForexAIMaster"
    d.mkdir(parents=True, exist_ok=True)
    return d


def setup_output(d: Path) -> None:
    """A windowed .exe has no console: send stdout/stderr and logs to a file."""
    log_file = d / "forex_ai_master.log"
    if log_file.exists() and log_file.stat().st_size > 5_000_000:
        log_file.replace(d / "forex_ai_master.old.log")
    stream = open(log_file, "a", encoding="utf-8", buffering=1)  # noqa: SIM115 - lives for the process
    if sys.stdout is None or not sys.stdout.isatty():
        sys.stdout = stream
    if sys.stderr is None or not sys.stderr.isatty():
        sys.stderr = stream
    logging.basicConfig(level=logging.INFO, stream=stream, format="%(asctime)s %(levelname)s %(name)s: %(message)s")


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


def acquire_single_instance() -> socket.socket | None:
    s = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
    try:
        s.bind(("127.0.0.1", LOCK_PORT))
        s.listen(1)
        return s
    except OSError:
        s.close()
        return None


def free_port(preferred: int) -> int:
    for port in [preferred, *range(preferred + 1, preferred + 50)]:
        with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as s:
            try:
                s.bind(("127.0.0.1", port))
                return port
            except OSError:
                continue
    raise RuntimeError("no free local port")


def message_box(text: str, title: str = APP_NAME, error: bool = False) -> None:
    """Native message box on Windows; log elsewhere."""
    log.info("message: %s", text)
    if sys.platform == "win32":
        import ctypes

        ctypes.windll.user32.MessageBoxW(None, text, title, 0x10 if error else 0x40)  # type: ignore[attr-defined]


def icon_path() -> str | None:
    base = Path(getattr(sys, "_MEIPASS", Path(__file__).resolve().parent))
    for p in (base / "assets" / "forex_ai_master.ico", Path(__file__).resolve().parent / "assets" / "forex_ai_master.ico"):
        if p.exists():
            return str(p)
    return None


# --------------------------------------------------------------------------- server
class LocalServer:
    def __init__(self, port: int) -> None:
        import uvicorn

        from backend.config.settings import Settings
        from backend.core.logging import configure_logging
        from backend.main import create_app

        settings = Settings(_env_file=None)
        configure_logging(settings.log_level)
        logging.getLogger().handlers[:] = [logging.StreamHandler(sys.stdout)]
        self.url = f"http://127.0.0.1:{port}/"
        self.server = uvicorn.Server(uvicorn.Config(create_app(settings), host="127.0.0.1", port=port,
                                                    log_level="warning", ws="websockets", lifespan="on",
                                                    log_config=None))
        self.thread = threading.Thread(target=self.server.run, daemon=True, name="server")

    def start(self, timeout_s: float = 60) -> bool:
        self.thread.start()
        deadline = time.time() + timeout_s
        while time.time() < deadline:
            if self.server.started:
                return True
            if not self.thread.is_alive():
                return False
            time.sleep(0.1)
        return False

    def stop(self) -> None:
        self.server.should_exit = True
        self.thread.join(timeout=15)


# --------------------------------------------------------------------------- modes
def run_check(srv: LocalServer) -> int:
    ok = True
    try:
        with urllib.request.urlopen(srv.url + "api/health", timeout=10) as r:
            ok &= r.status == 200
        with urllib.request.urlopen(srv.url, timeout=10) as r:
            ok &= r.status == 200 and b"Forex AI Master" in r.read()
    except Exception as exc:  # noqa: BLE001
        log.error("check failed: %s", exc)
        ok = False
    from backend.main import frontend_dist

    print("CHECK OK" if ok else "CHECK FAILED", "| dashboard:", frontend_dist(), flush=True)
    return 0 if ok else 1


def run_window(srv: LocalServer, smoke_file: str | None = None) -> int:
    import webview

    window = webview.create_window(APP_NAME, srv.url, width=1360, height=900, min_size=(980, 680),
                                   background_color="#0b1118", text_select=True)
    result: dict[str, object] = {}

    def smoke() -> None:
        # wait for the React app, then report what the window really shows
        for _ in range(60):
            try:
                state = window.evaluate_js(
                    "JSON.stringify({title: document.title, "
                    "login: !!document.querySelector('#password'), "
                    "text: (document.body.innerText || '').slice(0, 200)})")
                data = json.loads(state) if state else {}
                if data.get("login"):
                    result.update(data)
                    break
            except Exception as exc:  # noqa: BLE001
                result["error"] = str(exc)
            time.sleep(0.5)
        Path(smoke_file).write_text(json.dumps(result), encoding="utf-8")  # type: ignore[arg-type]
        window.destroy()

    kwargs: dict[str, object] = {"private_mode": False, "storage_path": str(data_dir() / "webview")}
    if sys.platform == "win32":
        kwargs["gui"] = "edgechromium"
    ico = icon_path()
    if ico:
        kwargs["icon"] = ico
    try:
        if smoke_file:
            webview.start(smoke, **kwargs)
        else:
            webview.start(**kwargs)
    except TypeError:  # older pywebview without some keyword
        kwargs.pop("icon", None)
        webview.start(smoke if smoke_file else None, **kwargs)
    if smoke_file:
        return 0 if result.get("login") and result.get("title") == APP_NAME else 1
    return 0


def run_browser(srv: LocalServer) -> int:
    webbrowser.open(srv.url)
    message_box(f"{APP_NAME} est ouvert dans votre navigateur :\n{srv.url}\n\n"
                "Cliquez sur OK pour arrêter le logiciel.")
    if sys.platform != "win32":  # no blocking dialog elsewhere: wait for Ctrl+C
        try:
            while srv.thread.is_alive():
                srv.thread.join(0.5)
        except KeyboardInterrupt:
            pass
    return 0


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--check", action="store_true")
    ap.add_argument("--smoke-gui", default="")
    ap.add_argument("--browser", action="store_true")
    ap.add_argument("--no-browser", action="store_true", help=argparse.SUPPRESS)  # kept for compatibility
    ap.add_argument("--port", type=int, default=8765)
    args = ap.parse_args()

    d = data_dir()
    setup_output(d)
    lock = acquire_single_instance()
    if lock is None:
        message_box(f"{APP_NAME} est déjà ouvert.\nUtilisez la fenêtre existante.")
        return 0
    try:
        prepare_environment(d)
        port = free_port(args.port)
        os.environ.setdefault("CORS_ORIGINS", f"http://127.0.0.1:{port}")
        srv = LocalServer(port)
        if not srv.start():
            message_box(f"{APP_NAME} n'a pas pu démarrer.\nDétails : {d / 'forex_ai_master.log'}", error=True)
            return 1
        log.info("server started on %s (data: %s)", srv.url, d)
        try:
            if args.check:
                return run_check(srv)
            if args.no_browser and not args.smoke_gui:
                srv.thread.join()
                return 0
            if args.browser:
                return run_browser(srv)
            try:
                return run_window(srv, args.smoke_gui or None)
            except Exception as exc:  # noqa: BLE001 - e.g. WebView2 runtime missing
                log.exception("application window unavailable, falling back to the browser")
                if args.smoke_gui:
                    Path(args.smoke_gui).write_text(json.dumps({"error": str(exc)}), encoding="utf-8")
                    return 1
                return run_browser(srv)
        finally:
            srv.stop()
            log.info("stopped")
    finally:
        lock.close()


if __name__ == "__main__":
    sys.exit(main())
