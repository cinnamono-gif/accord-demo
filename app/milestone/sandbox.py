"""Opt-in synthetic onboarding; fixed self-only grant, recoverable across two databases."""

from typing import Annotated, Literal
from uuid import UUID
import httpx
from fastapi import Depends, Request
from psycopg.types.json import Jsonb
from milestone.common import Problem, connect
from milestone.http_support import result
from milestone.identity import throttle
from milestone.models import Command
from milestone.worker import BankClient


class SetupConsent(Command):
    acknowledgement: Literal["SYNTHETIC_ONLY"]


class BankSetup(Command):
    user_id: UUID
    role: Literal["CLIENT", "FREELANCER"]


def enabled(cfg):
    return str(cfg.get("sandbox_setup_enabled", "false")).lower() == "true"


def view(c, cfg, actor):
    setup = c.execute("SELECT * FROM sandbox_setups WHERE user_id=%s", (actor["id"],)).fetchone()
    user = c.execute("SELECT active,control_allowed FROM users WHERE id=%s", (actor["id"],)).fetchone()
    return dict(
        enabled=enabled(cfg),
        state=setup["state"] if setup else "NOT_STARTED",
        synthetic_only=True,
        grant_minor=6000000 if actor["role"] == "CLIENT" else 0,
        control_allowed=user["active"] and user["control_allowed"],
    )


def register(app, cfg, actor, role, bank_client=None):
    Actor = Annotated[dict, Depends(actor)]

    def participant(a):
        role(a, "CLIENT", "FREELANCER")
        if a["is_demo"]:
            raise Problem(403, "FORBIDDEN", "Shared demo accounts already have their own fixture.")

    @app.get("/api/v1/me/sandbox", tags=["sandbox"])
    def status(a: Actor):
        participant(a)
        with connect(cfg["app_dsn"]) as c:
            return result(view(c, cfg, a))

    @app.post("/api/v1/me/sandbox", tags=["sandbox"])
    def setup(body: SetupConsent, request: Request, a: Actor):
        participant(a)
        if not enabled(cfg):
            raise Problem(409, "SANDBOX_DISABLED", "Sandbox setup has not been enabled on this deployment.")
        throttle(cfg, "sandbox", str(a["id"]))
        with connect(cfg["app_dsn"]) as c:
            c.execute("SELECT begin_sandbox_setup(%s,%s)", (a["id"], request.state.correlation))
            pending = c.execute("SELECT * FROM sandbox_setups WHERE user_id=%s", (a["id"],)).fetchone()
            if pending["state"] == "READY":
                return result(view(c, cfg, a))
        # Durable PENDING commits before any external call. Retry uses the same user ID.
        bank = bank_client or BankClient(cfg)
        try:
            receipt = bank.request(
                "POST",
                "/sandbox/accounts",
                pending["correlation_id"],
                json=dict(user_id=str(a["id"]), role=a["role"]),
            )
            expected = dict(
                user_id=str(a["id"]),
                role=a["role"],
                synthetic_only=True,
                grant_minor=6000000 if a["role"] == "CLIENT" else 0,
                account_ref="SYNTH-SETTLEMENT-001"
                if a["role"] == "CLIENT"
                else "SYNTH-PERSONAL-" + str(a["id"]),
            )
            if receipt != expected:
                raise ValueError("Unexpected synthetic receipt")
        except httpx.HTTPStatusError as exc:
            if exc.response.status_code == 409:
                raise Problem(
                    409,
                    "SANDBOX_CAPACITY",
                    "The shared sandbox has reached its account limit. Ask the demo operator for help.",
                ) from None
            raise Problem(
                503, "SANDBOX_PENDING", "Setup is pending. Retry setup safely in a moment."
            ) from None
        except (httpx.HTTPError, ValueError, KeyError):
            raise Problem(
                503, "SANDBOX_PENDING", "Setup is pending. Retry setup safely in a moment."
            ) from None
        finally:
            if bank_client is None:
                bank.close()
        with connect(cfg["app_dsn"]) as c:
            c.execute("SELECT finish_sandbox_setup(%s,%s)", (a["id"], Jsonb(receipt)))
            return result(view(c, cfg, a))
