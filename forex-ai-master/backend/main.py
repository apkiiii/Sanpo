"""Application entry point: ``uvicorn backend.main:app``."""

from __future__ import annotations

from collections.abc import AsyncIterator
from contextlib import asynccontextmanager

from fastapi import FastAPI, Request
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import JSONResponse, Response

from backend.api import routes, websocket
from backend.api.security import AuthService, RateLimiter
from backend.config.settings import Settings, get_settings
from backend.core.errors import ForexAIError
from backend.core.logging import configure_logging, get_logger
from backend.database.repository import Repository
from backend.runtime import TradingRuntime

log = get_logger(__name__)

SECURITY_HEADERS = {
    "X-Content-Type-Options": "nosniff",
    "X-Frame-Options": "DENY",
    "Referrer-Policy": "no-referrer",
    "Content-Security-Policy": "default-src 'none'; frame-ancestors 'none'",
    "Cache-Control": "no-store",
}


def create_app(settings: Settings | None = None, runtime: TradingRuntime | None = None,
               start_loop: bool = True) -> FastAPI:
    settings = settings or get_settings()

    @asynccontextmanager
    async def lifespan(app: FastAPI) -> AsyncIterator[None]:
        if app.state.runtime is None:
            repo = Repository(settings.database_url, create_schema=settings.database_url.startswith("sqlite"))
            app.state.runtime = TradingRuntime(settings, repo)
        if start_loop:
            await app.state.runtime.start()
        yield
        if start_loop:
            await app.state.runtime.stop()

    app = FastAPI(title="Forex AI Master", version="0.1.0", lifespan=lifespan,
                  docs_url="/api/docs" if settings.trading_mode.value == "simulation" else None, redoc_url=None)
    app.state.runtime = runtime
    app.state.auth = AuthService(settings)
    limiter = RateLimiter(settings.api_rate_limit_per_minute)
    login_limiter = RateLimiter(10)

    app.add_middleware(CORSMiddleware, allow_origins=settings.cors_origins, allow_credentials=False,
                       allow_methods=["GET", "POST", "PUT"], allow_headers=["Authorization", "Content-Type"])

    @app.middleware("http")
    async def guard(request: Request, call_next):  # type: ignore[no-untyped-def]
        client = request.client.host if request.client else "unknown"
        if request.url.path.startswith("/api"):
            if not limiter.allow(client) or (request.url.path == "/api/auth/login" and not login_limiter.allow(client)):
                return JSONResponse({"detail": "rate limit exceeded"}, status_code=429)
        response: Response = await call_next(request)
        for k, v in SECURITY_HEADERS.items():
            if not (k == "Content-Security-Policy" and request.url.path == "/api/docs"):
                response.headers.setdefault(k, v)
        return response

    @app.exception_handler(ForexAIError)
    async def domain_error(_: Request, exc: ForexAIError) -> JSONResponse:
        return JSONResponse({"detail": str(exc), "type": type(exc).__name__}, status_code=409)

    @app.exception_handler(Exception)
    async def unexpected(_: Request, exc: Exception) -> JSONResponse:
        log.exception("unhandled API error")
        return JSONResponse({"detail": "internal error"}, status_code=500)  # no stack traces to clients

    app.include_router(routes.router)
    app.include_router(websocket.router)
    return app


def get_app() -> FastAPI:
    """Factory for ``uvicorn --factory backend.main:get_app``."""
    s = get_settings()
    configure_logging(s.log_level)
    return create_app(s)
