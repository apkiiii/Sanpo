"""Desktop auto-update (Windows installer build only).

* :func:`check` reads the manifest ``latest.json`` published with each build: version, installer URL, SHA-256.
* :func:`install` downloads the installer from the fixed release location only, verifies its SHA-256, starts it
  silently with ``/RELAUNCH=1`` (the installer reopens the application) and asks the application to close so
  that its files can be replaced. The demo account lives in the data folder and is kept.
"""

from __future__ import annotations

import hashlib
import json
import os
import subprocess
import sys
import tempfile
import time
from collections.abc import Callable
from pathlib import Path
from typing import Any

import httpx

from backend.core.logging import get_logger
from backend.version import APP_VERSION, UPDATE_DOWNLOAD_PREFIX, UPDATE_MANIFEST_URL

log = get_logger(__name__)
_cache: dict[str, Any] = {"t": 0.0, "manifest": None}


def is_desktop() -> bool:
    return os.environ.get("FAM_DESKTOP") == "1" and sys.platform == "win32"


def parse_version(v: str) -> tuple[int, ...]:
    return tuple(int(p) for p in v.strip().lstrip("v").split(".") if p.isdigit())


def fetch_manifest(client: httpx.Client | None = None) -> dict[str, Any]:
    c = client or httpx.Client(timeout=8.0, follow_redirects=True)
    r = c.get(UPDATE_MANIFEST_URL)
    r.raise_for_status()
    data = json.loads(r.text)
    if not str(data.get("url", "")).startswith(UPDATE_DOWNLOAD_PREFIX) or len(str(data.get("sha256", ""))) != 64:
        raise ValueError("invalid update manifest")
    return data


def check(force: bool = False, fetch: Callable[[], dict[str, Any]] = fetch_manifest,
          desktop: bool | None = None) -> dict[str, Any]:
    status: dict[str, Any] = {"current": APP_VERSION, "latest": None, "available": False, "supported": False}
    if not (is_desktop() if desktop is None else desktop):
        return status
    status["supported"] = True
    try:
        if force or _cache["manifest"] is None or time.time() - _cache["t"] > 3600:
            _cache["manifest"], _cache["t"] = fetch(), time.time()
        m = _cache["manifest"]
        status["latest"] = m["version"]
        status["available"] = parse_version(m["version"]) > parse_version(APP_VERSION)
        status["notes"] = m.get("notes", "")
    except Exception as exc:  # noqa: BLE001 - offline etc.: the app keeps working
        status["error"] = f"vérification impossible : {exc}"
    return status


def download_verified(manifest: dict[str, Any], client: httpx.Client | None = None) -> Path:
    url = manifest["url"]
    if not url.startswith(UPDATE_DOWNLOAD_PREFIX):
        raise ValueError("refused: installer not from the official release")
    c = client or httpx.Client(timeout=120.0, follow_redirects=True)
    target = Path(tempfile.gettempdir()) / f"ForexAIMaster-Setup-{manifest['version']}.exe"
    h = hashlib.sha256()
    with c.stream("GET", url) as r, open(target, "wb") as f:
        r.raise_for_status()
        for chunk in r.iter_bytes(1 << 16):
            h.update(chunk)
            f.write(chunk)
    if h.hexdigest().lower() != str(manifest["sha256"]).lower():
        target.unlink(missing_ok=True)
        raise ValueError("refused: installer checksum mismatch")
    return target


def install(shutdown: Callable[[], None] | None, fetch: Callable[[], dict[str, Any]] = fetch_manifest,
            download: Callable[[dict[str, Any]], Path] = download_verified,
            launch: Callable[[list[str]], None] | None = None) -> dict[str, Any]:
    manifest = fetch()
    if parse_version(manifest["version"]) <= parse_version(APP_VERSION):
        return {"installing": False, "reason": "already up to date", "current": APP_VERSION}
    setup = download(manifest)
    args = [str(setup), "/SILENT", "/SUPPRESSMSGBOXES", "/NORESTART", "/CURRENTUSER", "/CLOSEAPPLICATIONS",
            "/RELAUNCH=1"]
    if launch is None:
        flags = getattr(subprocess, "DETACHED_PROCESS", 0) | getattr(subprocess, "CREATE_NEW_PROCESS_GROUP", 0)
        subprocess.Popen(args, creationflags=flags, close_fds=True)  # noqa: S603 - verified installer
    else:
        launch(args)
    log.warning("update %s started, closing the application", manifest["version"])
    if shutdown is not None:
        shutdown()
    return {"installing": True, "version": manifest["version"]}
