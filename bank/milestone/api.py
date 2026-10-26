from datetime import timedelta
from typing import Annotated
from uuid import UUID
import jwt
from fastapi import Depends, FastAPI, Header, Query, Request
from fastapi.security import HTTPAuthorizationCredentials, HTTPBearer
from starlette.background import BackgroundTask
from milestone import domain
from milestone.common import Problem, audit, config, connect, idempotent, identifier, now, verify_password
from milestone.http_support import install, page, result
from milestone.models import (
    ApprovalCommand,
    ReconciliationCommand,
    SubmissionCommand,
    TokenRequest,
    DisputeCommand,
    RevisionCommand,
)

Bearer = HTTPBearer(auto_error=False)
Key = Annotated[str, Header(alias="Idempotency-Key", min_length=8, max_length=128)]


def create_app(settings=None, *, after_command=None):
    cfg = settings or config()
    app = FastAPI(
        title="Accord — governed milestone payments",
        version="0.3.0",
        description="Synthetic funds. Agreements, full-budget funding, safeguarded seven-day release, governed dispute rework, bank payments and returns, reconciliation, recovery and a deterministic exception assistant. No automatic dispute adjudication.",
    )
    app.state.settings = cfg
    install(app)

    def command_result(value, status, replay):
        response = result(value, status, replay)
        if after_command:
            response.background = BackgroundTask(after_command)
        return response

    def actor(request: Request, credentials: Annotated[HTTPAuthorizationCredentials | None, Depends(Bearer)]):
        if not credentials:
            raise Problem(401, "UNAUTHENTICATED", "Sign in to continue.")
        try:
            token = jwt.decode(
                credentials.credentials,
                cfg["jwt_secret"],
                algorithms=["HS256"],
                audience="milestone",
                issuer="milestone-local",
                options={"require": ["exp", "sub", "iat", "aud", "iss"]},
            )
            user_id = UUID(token["sub"])
        except (jwt.PyJWTError, ValueError, KeyError):
            raise Problem(401, "UNAUTHENTICATED", "Invalid or expired session.") from None
        with connect(cfg["app_dsn"]) as c:
            user = c.execute("SELECT * FROM users WHERE id=%s AND active", (user_id,)).fetchone()
        if not user:
            raise Problem(401, "UNAUTHENTICATED", "This account is inactive.")
        request.state.actor_id = user["id"]
        return user

    Actor = Annotated[dict, Depends(actor)]

    def role(a, *roles):
        if a["role"] not in roles:
            raise Problem(403, "FORBIDDEN", "Your role cannot perform this action.")

    @app.get("/health", tags=["health"])
    def health():
        with connect(cfg["app_dsn"]) as c:
            c.execute("SELECT 1")
        return {"status": "ok", "service": "application", "slice": "guarded-review"}

    @app.post("/api/v1/auth/token", tags=["identity"])
    def login(body: TokenRequest, request: Request):
        with connect(cfg["app_dsn"]) as c:
            u = c.execute("SELECT * FROM users WHERE login=%s AND active", (body.login,)).fetchone()
        if not u or not verify_password(body.password, u["password_hash"]):
            raise Problem(401, "UNAUTHENTICATED", "Invalid credentials.")
        issued = now()
        token = jwt.encode(
            dict(
                sub=str(u["id"]),
                iat=issued,
                exp=issued + timedelta(hours=1),
                aud="milestone",
                iss="milestone-local",
            ),
            cfg["jwt_secret"],
            algorithm="HS256",
        )
        return dict(access_token=token, token_type="Bearer", expires_in=3600)

    @app.get("/api/v1/me", tags=["identity"])
    def me(a: Actor):
        return result(dict(id=a["id"], display_name=a["display_name"], roles=[a["role"]]))

    @app.get("/api/v1/projects", tags=["project"])
    def projects(a: Actor, limit: int = Query(25, ge=1, le=100), cursor: str | None = None):
        with connect(cfg["app_dsn"]) as c:
            ps = c.execute(
                "SELECT * FROM projects WHERE client_id=%s OR freelancer_id=%s OR %s ORDER BY id",
                (a["id"], a["id"], a["role"] in ("OPERATIONS", "FINANCIAL_APPROVER")),
            ).fetchall()
            return result(page([domain.project_view(c, p) for p in ps], limit, cursor))

    @app.get("/api/v1/projects/{project_id}", tags=["project"])
    def project(project_id: UUID, a: Actor):
        with connect(cfg["app_dsn"]) as c:
            return result(domain.project_view(c, domain.visible_project(c, a, project_id)))

    @app.get("/api/v1/wallets/me", tags=["project"])
    def wallet(a: Actor):
        role(a, "CLIENT")
        with connect(cfg["app_dsn"]) as c:
            w = c.execute("SELECT * FROM wallets WHERE owner_user_id=%s", (a["id"],)).fetchone()
            if not w:
                raise Problem(404, "NOT_FOUND", "Wallet not found.")
            available = c.execute(
                "SELECT balance_minor FROM ledger_accounts WHERE wallet_id=%s", (w["id"],)
            ).fetchone()["balance_minor"]
            reserved = c.execute(
                "SELECT coalesce(sum(l.balance_minor),0) AS n FROM ledger_accounts l JOIN milestones m ON m.id=l.milestone_id JOIN projects p ON p.id=m.project_id WHERE p.client_id=%s",
                (a["id"],),
            ).fetchone()["n"]
            payable = c.execute(
                "SELECT coalesce(sum(l.balance_minor),0) AS n FROM ledger_accounts l JOIN payouts t ON t.id=l.payout_id JOIN milestones m ON m.id=t.milestone_id JOIN projects p ON p.id=m.project_id WHERE p.client_id=%s",
                (a["id"],),
            ).fetchone()["n"]
            return result(
                dict(
                    id=w["id"],
                    currency="PKR",
                    available_minor=int(available),
                    reserved_minor=int(reserved),
                    payable_minor=int(payable),
                )
            )

    @app.post("/api/v1/milestones/{milestone_id}/submissions", status_code=201, tags=["work"])
    def submission(milestone_id: UUID, body: SubmissionCommand, request: Request, a: Actor, key: Key):
        role(a, "FREELANCER")
        with connect(cfg["app_dsn"]) as c:
            p, m = domain.context(c, a, milestone_id, lock=True)
            status, value, replay = idempotent(
                c,
                a["id"],
                f"submit:{milestone_id}",
                key,
                body.model_dump(mode="json"),
                201,
                lambda: domain.submit(c, a, p, m, body, request.state.correlation),
            )
        return command_result(value, status, replay)

    @app.get("/api/v1/milestones/{milestone_id}/submissions", tags=["work"])
    def submissions(
        milestone_id: UUID, a: Actor, limit: int = Query(25, ge=1, le=100), cursor: str | None = None
    ):
        with connect(cfg["app_dsn"]) as c:
            domain.context(c, a, milestone_id)
            rows = c.execute(
                "SELECT * FROM submissions WHERE milestone_id=%s ORDER BY version DESC", (milestone_id,)
            ).fetchall()
            return result(page([domain.submission_view(c, s) for s in rows], limit, cursor))

    @app.post("/api/v1/milestones/{milestone_id}/approvals", status_code=202, tags=["payment"])
    def approval(milestone_id: UUID, body: ApprovalCommand, request: Request, a: Actor, key: Key):
        role(a, "CLIENT")
        with connect(cfg["app_dsn"]) as c:
            p, m = domain.context(c, a, milestone_id, lock=True)
            status, value, replay = idempotent(
                c,
                a["id"],
                f"approve:{milestone_id}",
                key,
                body.model_dump(mode="json"),
                202,
                lambda: domain.approve(c, a, p, m, body, request.state.correlation),
            )
        return command_result(value, status, replay)

    @app.post("/api/v1/milestones/{milestone_id}/disputes", status_code=201, tags=["review"])
    def dispute(milestone_id: UUID, body: DisputeCommand, request: Request, a: Actor, key: Key):
        role(a, "CLIENT", "FREELANCER")
        with connect(cfg["app_dsn"]) as c:
            p, m = domain.context(c, a, milestone_id, lock=True)
            status, value, replay = idempotent(
                c,
                a["id"],
                f"dispute:{milestone_id}",
                key,
                body.model_dump(mode="json"),
                201,
                lambda: domain.open_dispute(c, a, p, m, body, request.state.correlation),
            )
        return command_result(value, status, replay)

    @app.get("/api/v1/milestones/{milestone_id}/disputes", tags=["review"])
    def disputes(
        milestone_id: UUID, a: Actor, limit: int = Query(25, ge=1, le=100), cursor: str | None = None
    ):
        with connect(cfg["app_dsn"]) as c:
            domain.context(c, a, milestone_id)
            rows = c.execute(
                "SELECT * FROM disputes WHERE milestone_id=%s ORDER BY opened_at DESC,id", (milestone_id,)
            ).fetchall()
            return result(page([domain.dispute_view(c, row) for row in rows], limit, cursor))

    @app.post("/api/v1/milestones/{milestone_id}/revisions", status_code=201, tags=["review"])
    def revision(milestone_id: UUID, body: RevisionCommand, request: Request, a: Actor, key: Key):
        role(a, "CLIENT")
        with connect(cfg["app_dsn"]) as c:
            p, m = domain.context(c, a, milestone_id, lock=True)
            status, value, replay = idempotent(
                c,
                a["id"],
                f"revision:{milestone_id}",
                key,
                body.model_dump(mode="json"),
                201,
                lambda: domain.request_revision(c, a, p, m, body, request.state.correlation),
            )
        return command_result(value, status, replay)

    @app.get("/api/v1/payouts/{payout_id}", tags=["payment"])
    def payout(payout_id: UUID, a: Actor):
        with connect(cfg["app_dsn"]) as c:
            p = c.execute("SELECT * FROM payouts WHERE id=%s", (payout_id,)).fetchone()
            if not p:
                raise Problem(404, "NOT_FOUND", "Payout not found.")
            domain.context(c, a, p["milestone_id"])
            return result(domain.payout_view(p))

    @app.get("/api/v1/projects/{project_id}/timeline", tags=["evidence"])
    def timeline(project_id: UUID, a: Actor, limit: int = Query(25, ge=1, le=100), cursor: str | None = None):
        with connect(cfg["app_dsn"]) as c:
            domain.visible_project(c, a, project_id)
            rows = c.execute(
                "SELECT id,action AS kind,occurred_at,summary,payout_id,correlation_id FROM audit_events WHERE project_id=%s ORDER BY occurred_at,id",
                (project_id,),
            ).fetchall()
            return result(page(rows, limit, cursor))

    @app.get("/api/v1/projects/{project_id}/ledger", tags=["evidence"])
    def ledger(project_id: UUID, a: Actor, limit: int = Query(25, ge=1, le=100), cursor: str | None = None):
        with connect(cfg["app_dsn"]) as c:
            domain.visible_project(c, a, project_id)
            rows = c.execute(
                "SELECT e.batch_id,e.account_id,l.account_kind,e.side,e.amount_minor,e.currency,b.posted_at,b.payout_id FROM journal_entries e JOIN journal_batches b ON b.id=e.batch_id JOIN ledger_accounts l ON l.id=e.account_id WHERE b.project_id=%s ORDER BY b.posted_at,e.id",
                (project_id,),
            ).fetchall()
            return result(page(rows, limit, cursor))

    @app.get("/api/v1/notifications", tags=["evidence"])
    def notifications(a: Actor, limit: int = Query(25, ge=1, le=100), cursor: str | None = None):
        with connect(cfg["app_dsn"]) as c:
            rows = c.execute(
                "SELECT id,kind,text,created_at,read_at FROM notifications WHERE user_id=%s ORDER BY created_at DESC,id",
                (a["id"],),
            ).fetchall()
            return result(page(rows, limit, cursor))

    @app.post("/api/v1/ops/reconciliation-runs", status_code=202, tags=["reconciliation"])
    def reconcile(body: ReconciliationCommand, request: Request, a: Actor, key: Key):
        role(a, "OPERATIONS")
        with connect(cfg["app_dsn"]) as c:

            def perform():
                row = c.execute(
                    "INSERT INTO reconciliation_runs(id,from_inclusive,to_exclusive,correlation_id) VALUES(%s,%s,%s,%s) RETURNING *",
                    (identifier(), body.from_inclusive, body.to_exclusive, request.state.correlation),
                ).fetchone()
                audit(
                    c,
                    a["id"],
                    "RECONCILIATION_QUEUED",
                    None,
                    request.state.correlation,
                    "Independent bank comparison queued.",
                )
                return domain.run_view(row)

            status, value, replay = idempotent(
                c, a["id"], "reconcile", key, body.model_dump(mode="json"), 202, perform
            )
        return command_result(value, status, replay)

    @app.get("/api/v1/ops/reconciliation-runs/{run_id}", tags=["reconciliation"])
    def reconciliation(run_id: UUID, a: Actor):
        role(a, "OPERATIONS", "FINANCIAL_APPROVER")
        with connect(cfg["app_dsn"]) as c:
            r = c.execute("SELECT * FROM reconciliation_runs WHERE id=%s", (run_id,)).fetchone()
            if not r:
                raise Problem(404, "NOT_FOUND", "Reconciliation run not found.")
            return result(domain.run_view(r))

    @app.get("/api/v1/ops/exceptions", tags=["reconciliation"])
    def exceptions(a: Actor, limit: int = Query(25, ge=1, le=100), cursor: str | None = None):
        role(a, "OPERATIONS", "FINANCIAL_APPROVER")
        with connect(cfg["app_dsn"]) as c:
            rows = c.execute(
                "SELECT id,category,NULL::uuid AS milestone_id,payout_id,owner_id,state,category AS summary,version FROM exceptions ORDER BY opened_at DESC,id"
            ).fetchall()
            return result(page(rows, limit, cursor))

    from milestone.operations import register

    register(app, cfg, actor, role, command_result)
    from milestone.projects import register as register_projects

    register_projects(app, cfg, actor, role, command_result)
    return app
