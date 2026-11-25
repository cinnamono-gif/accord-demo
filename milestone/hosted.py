"""Stateless hosting adapter. PostgreSQL, not a function's memory, owns durable work."""

from pathlib import Path
import hmac
import time
from typing import Literal

import httpx
from fastapi import Request
from fastapi.responses import JSONResponse
from fastapi.staticfiles import StaticFiles
from pydantic import BaseModel, ConfigDict
from starlette.concurrency import run_in_threadpool

from milestone.api import create_app as application
from milestone.bank import create_app as bank_application
from milestone.common import Problem, config, connect
from milestone.worker import Worker


def run_batch(cfg, *, worker_factory=Worker):
    """Bound invocation duration/work. A skipped or failed invocation loses no work."""
    with connect(cfg["app_dsn"]) as lock:
        acquired = lock.execute("SELECT pg_try_advisory_lock(30175610) AS ok").fetchone()["ok"]
        if not acquired:
            return {"processed": 0, "busy": True}
        worker = worker_factory(cfg)
        processed = 0
        deadline = time.monotonic() + 15
        try:
            while processed < 12 and time.monotonic() < deadline:
                if not worker.once():
                    break
                processed += 1
            return {"processed": processed, "busy": False}
        finally:
            worker.bank.close()
            lock.execute("SELECT pg_advisory_unlock(30175610)")


def login_allowed(cfg):
    with connect(cfg["app_dsn"]) as c:
        row = c.execute("""
            INSERT INTO hosted_login_limit VALUES('login',clock_timestamp(),1)
            ON CONFLICT(bucket) DO UPDATE SET
             attempts=CASE WHEN hosted_login_limit.opened_at<clock_timestamp()-interval '1 minute'
               THEN 1 ELSE hosted_login_limit.attempts+1 END,
             opened_at=CASE WHEN hosted_login_limit.opened_at<clock_timestamp()-interval '1 minute'
               THEN clock_timestamp() ELSE hosted_login_limit.opened_at END
            RETURNING attempts
        """).fetchone()
        return row["attempts"] <= 60


class DemoRole(BaseModel):
    model_config = ConfigDict(extra="forbid")
    role: Literal["client", "freelancer"]


def create_app(settings=None, *, static_dir=None, batch=None):
    cfg = settings or config()
    for key in ("app_dsn", "jwt_secret", "bank_key", "bank_url", "cron_secret"):
        if not cfg.get(key):
            raise RuntimeError(f"Missing hosted setting: {key.upper()}")
    if len(cfg["cron_secret"]) < 32:
        raise RuntimeError("CRON_SECRET must contain at least 32 characters")
    work = batch or (lambda: run_batch(cfg))
    app = application(cfg, after_command=work)
    app.routes[:] = [
        route
        for route in app.routes
        if getattr(route, "path", "") not in ("/docs", "/redoc", "/openapi.json", "/docs/oauth2-redirect")
    ]

    @app.middleware("http")
    async def public_headers(request: Request, call_next):
        # The demo route calls the token route internally, so counting only the token route avoids a duplicate database connection.
        if request.method == "POST" and request.url.path == "/api/v1/auth/token":
            if not await run_in_threadpool(login_allowed, cfg):
                return JSONResponse(
                    {"detail": "Demo sign-in is busy. Please try again in a minute."},
                    status_code=429,
                    headers={"Retry-After": "60", "Cache-Control": "no-store"},
                )
        response = await call_next(request)
        response.headers["X-Content-Type-Options"] = "nosniff"
        response.headers["X-Frame-Options"] = "DENY"
        response.headers["Referrer-Policy"] = "no-referrer"
        response.headers["X-Robots-Tag"] = "noindex, nofollow"
        if request.url.path.startswith(("/api/", "/__demo/")):
            response.headers["Cache-Control"] = "no-store"
        return response

    @app.api_route("/api/internal/worker", methods=["GET", "POST"])
    def scheduled(request: Request):
        supplied = request.headers.get("Authorization", "")
        if not hmac.compare_digest(supplied, "Bearer " + cfg["cron_secret"]):
            raise Problem(401, "UNAUTHENTICATED", "Invalid worker credentials.")
        return work()

    @app.post("/__demo/session")
    async def demo(body: DemoRole):
        if cfg.get("public_demo") != "1" or not cfg.get("demo_password"):
            raise Problem(404, "NOT_FOUND", "Public demo sign-in is disabled.")
        # Reuse the real password/token endpoint, restricting visitors to two seed identities.
        async with httpx.AsyncClient(
            transport=httpx.ASGITransport(app=app), base_url="http://internal"
        ) as client:
            response = await client.post(
                "/api/v1/auth/token",
                json={"login": body.role + "@demo.local", "password": cfg["demo_password"]},
            )
        return JSONResponse(response.json(), status_code=response.status_code)

    if static_dir:
        app.mount("/", StaticFiles(directory=Path(static_dir), html=True), name="frontend")
    return app


def create_bank(settings=None):
    cfg = settings or config()
    if not cfg.get("bank_dsn") or not cfg.get("bank_key"):
        raise RuntimeError("BANK_DSN and BANK_KEY are required")
    app = bank_application(cfg)
    app.routes[:] = [
        route
        for route in app.routes
        if getattr(route, "path", "") not in ("/docs", "/redoc", "/openapi.json", "/docs/oauth2-redirect")
    ]
    return app
