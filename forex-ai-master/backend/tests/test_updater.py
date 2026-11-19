"""Desktop auto-update: version check, checksum verification, official source only."""

from __future__ import annotations

import hashlib
from pathlib import Path

import httpx
import pytest

from backend import updater
from backend.version import APP_VERSION, UPDATE_DOWNLOAD_PREFIX

pytestmark = pytest.mark.critical
PAYLOAD = b"MZ fake installer"


def manifest(version: str, sha: str | None = None, url: str | None = None) -> dict[str, str]:
    return {"version": version, "url": url or UPDATE_DOWNLOAD_PREFIX + "ForexAIMaster-Setup.exe",
            "sha256": sha or hashlib.sha256(PAYLOAD).hexdigest()}


def test_version_compare() -> None:
    assert updater.parse_version("1.10.0") > updater.parse_version("1.9.9")
    assert updater.parse_version("v1.1.0") == (1, 1, 0)


def test_check_reports_newer_version_only_in_desktop() -> None:
    updater._cache.update(t=0.0, manifest=None)
    st = updater.check(force=True, fetch=lambda: manifest("99.0.0"), desktop=True)
    assert st["available"] and st["latest"] == "99.0.0" and st["current"] == APP_VERSION
    assert not updater.check(force=True, fetch=lambda: manifest(APP_VERSION), desktop=True)["available"]
    assert not updater.check(force=True, fetch=lambda: manifest("99.0.0"), desktop=False)["supported"]

    def offline() -> dict[str, str]:
        raise httpx.ConnectError("offline")

    st = updater.check(force=True, fetch=offline, desktop=True)
    assert not st["available"] and "error" in st


def _client(body: bytes) -> httpx.Client:
    return httpx.Client(transport=httpx.MockTransport(lambda req: httpx.Response(200, content=body)))


def test_download_verifies_checksum_and_source() -> None:
    path = updater.download_verified(manifest("99.0.0"), _client(PAYLOAD))
    assert path.read_bytes() == PAYLOAD
    path.unlink()
    with pytest.raises(ValueError, match="checksum"):
        updater.download_verified(manifest("99.0.0"), _client(b"tampered"))
    with pytest.raises(ValueError, match="official"):
        updater.download_verified(manifest("99.0.0", url="https://evil.example/x.exe"), _client(PAYLOAD))


def test_manifest_from_other_host_rejected() -> None:
    body = b'{"version": "9.9.9", "url": "https://evil.example/x.exe", "sha256": "' + b"a" * 64 + b'"}'
    with pytest.raises(ValueError):
        updater.fetch_manifest(_client(body))


def test_install_runs_silent_installer_then_closes_app(tmp_path: Path) -> None:
    launched: list[list[str]] = []
    closed: list[bool] = []
    setup = tmp_path / "setup.exe"
    res = updater.install(lambda: closed.append(True), fetch=lambda: manifest("99.0.0"),
                          download=lambda m: setup, launch=launched.append)
    assert res["installing"] and closed == [True]
    assert launched[0][0] == str(setup) and "/SILENT" in launched[0] and "/RELAUNCH=1" in launched[0]
    same = updater.install(None, fetch=lambda: manifest(APP_VERSION), download=lambda m: setup, launch=launched.append)
    assert not same["installing"] and len(launched) == 1
