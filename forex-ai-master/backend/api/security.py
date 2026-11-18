"""Dashboard authentication, authorisation and request limiting.

* Passwords are verified against a bcrypt hash from the environment; no password
  is ever stored in clear and there is no default password (login is disabled
  until a hash is configured).
* Tokens are short-lived HS256 JWTs with a role claim (``admin`` or ``viewer``).
* Brute-force protection: after ``max_failures`` failed logins an IP is locked out.
* A sliding-window rate limiter protects every API route.
"""

from __future__ import annotations

import ipaddress
import json
import os
import secrets
import time
from collections import defaultdict, deque
from dataclasses import dataclass
from pathlib import Path
from datetime import timedelta

import bcrypt
import jwt
from fastapi import Depends, HTTPException, Request, status
from fastapi.security import HTTPAuthorizationCredentials, HTTPBearer

from backend.config.settings import Settings, TradingMode
from backend.core.errors import ConfigurationError
from backend.core.logging import get_logger
from backend.core.types import utcnow

log = get_logger(__name__)
ALGORITHM = "HS256"


@dataclass(frozen=True, slots=True)
class Principal:
    username: str
    role: str  # "admin" | "viewer"


class RateLimiter:
    def __init__(self, limit: int, window_s: float = 60.0) -> None:
        self.limit, self.window = limit, window_s
        self._hits: dict[str, deque[float]] = defaultdict(deque)

    def allow(self, key: str) -> bool:
        now = time.monotonic()
        q = self._hits[key]
        while q and now - q[0] > self.window:
            q.popleft()
        if len(q) >= self.limit:
            return False
        q.append(now)
        return True


class LoginGuard:
    def __init__(self, max_failures: int = 5, lockout_s: float = 900.0) -> None:
        self.max_failures, self.lockout_s = max_failures, lockout_s
        self._failures: dict[str, deque[float]] = defaultdict(deque)

    def locked(self, key: str) -> bool:
        now = time.monotonic()
        q = self._failures[key]
        while q and now - q[0] > self.lockout_s:
            q.popleft()
        return len(q) >= self.max_failures

    def failure(self, key: str) -> None:
        self._failures[key].append(time.monotonic())

    def success(self, key: str) -> None:
        self._failures.pop(key, None)


class AuthService:
    def __init__(self, settings: Settings) -> None:
        self.settings = settings
        secret = settings.jwt_secret.get_secret_value()
        if not secret:
            if settings.trading_mode is not TradingMode.SIMULATION:
                raise ConfigurationError("JWT_SECRET is required outside SIMULATION mode")
            log.warning("JWT_SECRET not set: using an ephemeral secret (sessions end at restart)")
            secret = secrets.token_urlsafe(48)
        elif len(secret) < 32:
            raise ConfigurationError("JWT_SECRET must be at least 32 characters")
        self._secret = secret
        self._users: dict[str, tuple[str, str]] = {}
        h = settings.dashboard_admin_password_hash.get_secret_value()
        if h:
            if not h.startswith("$2"):
                raise ConfigurationError("DASHBOARD_ADMIN_PASSWORD_HASH must be a bcrypt hash")
            self._users[settings.dashboard_admin_username] = (h, "admin")
        self._local_path = Path(settings.local_auth_path)
        if not h and self._local_path.exists():
            data = json.loads(self._local_path.read_text())
            if str(data.get("hash", "")).startswith("$2"):
                self._users[str(data["username"])] = (str(data["hash"]), "admin")
        self.guard = LoginGuard()

    @staticmethod
    def is_local(client: str) -> bool:
        try:
            return ipaddress.ip_address(client).is_loopback
        except ValueError:
            return client == "testclient"  # Starlette test client

    def setup_available(self, client: str) -> bool:
        return not self.login_enabled and self.is_local(client)

    def setup(self, username: str, password: str, client: str) -> str:
        """First-launch password creation (desktop). Only once, only from this computer."""
        if not self.setup_available(client):
            raise HTTPException(status.HTTP_403_FORBIDDEN, "setup not available")
        if len(password) < 12:
            raise HTTPException(status.HTTP_422_UNPROCESSABLE_ENTITY, "password must be at least 12 characters")
        h = bcrypt.hashpw(password.encode(), bcrypt.gensalt(rounds=12)).decode()
        self._local_path.parent.mkdir(parents=True, exist_ok=True)
        self._local_path.write_text(json.dumps({"username": username, "hash": h}))
        try:
            os.chmod(self._local_path, 0o600)
        except OSError:
            pass
        self._users[username] = (h, "admin")
        return self.authenticate(username, password, client)

    @property
    def login_enabled(self) -> bool:
        return bool(self._users)

    def add_user(self, username: str, password_hash: str, role: str) -> None:
        if role not in ("admin", "viewer"):
            raise ValueError("invalid role")
        self._users[username] = (password_hash, role)

    def authenticate(self, username: str, password: str, client: str) -> str:
        if not self.login_enabled:
            raise HTTPException(status.HTTP_503_SERVICE_UNAVAILABLE,
                                "login disabled: configure DASHBOARD_ADMIN_PASSWORD_HASH")
        if self.guard.locked(client):
            raise HTTPException(status.HTTP_429_TOO_MANY_REQUESTS, "too many failed attempts, try later")
        user = self._users.get(username)
        # constant work whether or not the user exists
        dummy = b"$2b$12$C6UzMDM.H6dfI/f/IKcEeO5Zc5D6s1k8G2b1Tq4pD4Xc3m1e2pZ5S"
        hashed = user[0].encode() if user else dummy
        try:
            ok = bcrypt.checkpw(password.encode(), hashed) and user is not None
        except ValueError:
            ok = False
        if not ok:
            self.guard.failure(client)
            log.warning("failed login", extra={"ctx": {"client": client}})
            raise HTTPException(status.HTTP_401_UNAUTHORIZED, "invalid credentials")
        self.guard.success(client)
        assert user is not None
        exp = utcnow() + timedelta(minutes=self.settings.jwt_expiry_minutes)
        return jwt.encode({"sub": username, "role": user[1], "exp": exp, "iat": utcnow()}, self._secret, ALGORITHM)

    def decode(self, token: str) -> Principal:
        try:
            payload = jwt.decode(token, self._secret, algorithms=[ALGORITHM], options={"require": ["exp", "sub"]})
        except jwt.PyJWTError as exc:
            raise HTTPException(status.HTTP_401_UNAUTHORIZED, "invalid or expired token") from exc
        role = payload.get("role")
        if role not in ("admin", "viewer") or payload["sub"] not in self._users:
            raise HTTPException(status.HTTP_401_UNAUTHORIZED, "invalid token subject")
        return Principal(payload["sub"], role)


bearer = HTTPBearer(auto_error=False)


def get_auth(request: Request) -> AuthService:
    return request.app.state.auth  # type: ignore[no-any-return]


def current_user(request: Request, creds: HTTPAuthorizationCredentials | None = Depends(bearer)) -> Principal:
    if creds is None or creds.scheme.lower() != "bearer":
        raise HTTPException(status.HTTP_401_UNAUTHORIZED, "authentication required")
    return get_auth(request).decode(creds.credentials)


def require_admin(user: Principal = Depends(current_user)) -> Principal:
    if user.role != "admin":
        raise HTTPException(status.HTTP_403_FORBIDDEN, "admin role required")
    return user
