from uuid import UUID
from fastapi import Request
from fastapi.exceptions import RequestValidationError
from fastapi.responses import JSONResponse
import psycopg
from milestone.common import Problem, identifier, jsonable, audit, connect


def install(app):
    @app.middleware("http")
    async def correlation(request: Request, call_next):
        value = request.headers.get("X-Correlation-ID")
        request.state.correlation = identifier()
        if value:
            try:
                request.state.correlation = str(UUID(value))
            except ValueError:
                return problem_response(
                    request, Problem(422, "VALIDATION_ERROR", "X-Correlation-ID must be a UUID.")
                )
        response = await call_next(request)
        response.headers["X-Correlation-ID"] = request.state.correlation
        return response

    @app.exception_handler(Problem)
    async def problem(request, exc):
        if exc.status in (401, 403) and hasattr(app.state, "settings"):
            with connect(app.state.settings["app_dsn"]) as c:
                audit(
                    c,
                    getattr(request.state, "actor_id", None),
                    "ACCESS_DENIED",
                    None,
                    request.state.correlation,
                    request.method + " " + request.url.path[:160],
                    outcome=exc.code,
                )
        return problem_response(request, exc)

    @app.exception_handler(RequestValidationError)
    async def invalid(request, exc):
        # Do not echo passwords or arbitrary request bodies into error responses.
        fields = ", ".join(".".join(str(p) for p in e["loc"]) for e in exc.errors())
        return problem_response(
            request, Problem(422, "VALIDATION_ERROR", "Invalid request fields: " + fields)
        )

    @app.exception_handler(psycopg.errors.UniqueViolation)
    async def duplicate(request, exc):
        return problem_response(
            request,
            Problem(409, "STATE_CONFLICT", "An equivalent resource already exists; reload its state."),
        )

    @app.exception_handler(psycopg.OperationalError)
    async def unavailable(request, exc):
        return problem_response(
            request,
            Problem(
                503,
                "SERVICE_UNAVAILABLE",
                "Database temporarily unavailable; retry with the same idempotency key.",
            ),
        )


def problem_response(request, exc):
    return JSONResponse(
        dict(
            type="urn:milestone:problem:" + exc.code.lower().replace("_", "-"),
            title=exc.code.replace("_", " ").title(),
            status=exc.status,
            code=exc.code,
            detail=exc.detail,
            correlation_id=getattr(request.state, "correlation", identifier()),
            retryable=exc.status in (429, 503),
        ),
        status_code=exc.status,
        media_type="application/problem+json",
    )


def result(value, status=200, replay=False):
    return JSONResponse(
        jsonable(value), status_code=status, headers={"Idempotency-Replayed": str(replay).lower()}
    )


def page(rows, limit, cursor):
    try:
        offset = int(cursor or "0")
        if offset < 0:
            raise ValueError
    except ValueError:
        raise Problem(422, "VALIDATION_ERROR", "Invalid cursor.") from None
    return dict(
        items=rows[offset : offset + limit],
        next_cursor=str(offset + limit) if offset + limit < len(rows) else None,
    )
