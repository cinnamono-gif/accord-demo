"""Agreements and full-budget reservation from authoritative wallet funds."""

from typing import Annotated
from uuid import UUID
from fastapi import Depends, Header, Request
from pydantic import Field
from psycopg.types.json import Jsonb
from milestone import domain
from milestone.common import Problem, audit, conflict, connect, digest, emit, idempotent, identifier
from milestone.models import Command, Short, Text, Version
from milestone.http_support import result

POLICY = "Synthetic full funding; approval or safeguarded seven-day release; full milestones; no fees."


class MilestoneDraft(Command):
    title: Short
    criteria: Text
    amount_minor: Annotated[int, Field(strict=True, ge=1, le=100000000)]


class ProjectDraft(Command):
    title: Short
    freelancer_id: UUID
    milestones: Annotated[list[MilestoneDraft], Field(min_length=1, max_length=10)]
    accept_on_create: Annotated[bool, Field(strict=True)] = False


class ProjectVersion(Command):
    expected_version: Version


class AcceptAgreement(ProjectVersion):
    snapshot_hash: Annotated[str, Field(min_length=64, max_length=64)]


def register(app, cfg, actor, role, command_result):
    Actor = Annotated[dict, Depends(actor)]
    Key = Annotated[str, Header(alias="Idempotency-Key", min_length=8, max_length=128)]

    @app.get("/api/v1/freelancers", tags=["project"])
    def freelancers(a: Actor):
        role(a, "CLIENT")
        with connect(cfg["app_dsn"]) as c:
            return result(
                c.execute(
                    """SELECT u.id,u.display_name FROM users u WHERE role='FREELANCER' AND active
                    AND u.is_demo=%s AND (u.is_demo OR EXISTS(
                      SELECT 1 FROM projects p WHERE p.client_id=%s AND p.freelancer_id=u.id)) ORDER BY u.id""",
                    (a["is_demo"], a["id"]),
                ).fetchall()
            )

    @app.post("/api/v1/projects", tags=["project"])
    def create(body: ProjectDraft, request: Request, a: Actor, key: Key):
        role(a, "CLIENT")
        with connect(cfg["app_dsn"]) as c:

            def perform():
                person = c.execute(
                    "SELECT id FROM users WHERE id=%s AND role='FREELANCER' AND active AND is_demo=%s",
                    (body.freelancer_id, a["is_demo"]),
                ).fetchone()
                beneficiary = c.execute(
                    "SELECT id FROM beneficiaries WHERE owner_user_id=%s ORDER BY active DESC,id LIMIT 1",
                    (body.freelancer_id,),
                ).fetchone()
                wallet = c.execute("SELECT id FROM wallets WHERE owner_user_id=%s", (a["id"],)).fetchone()
                if not person or not beneficiary or not wallet:
                    conflict(
                        "Use the account ID shared by a freelancer in the same personal or demo workspace."
                    )
                pid, gid = identifier(), identifier()
                p = c.execute(
                    "INSERT INTO projects(id,client_id,freelancer_id,beneficiary_id,title,total_minor,currency,status) VALUES(%s,%s,%s,%s,%s,%s,'PKR','DRAFT') RETURNING *",
                    (
                        pid,
                        a["id"],
                        body.freelancer_id,
                        beneficiary["id"],
                        body.title,
                        sum(m.amount_minor for m in body.milestones),
                    ),
                ).fetchone()
                terms = body.model_dump(mode="json", exclude={"accept_on_create"}) | dict(
                    policy=POLICY,
                    review_seconds=604800,
                    client_id=str(a["id"]),
                    beneficiary_id=str(beneficiary["id"]),
                )
                c.execute(
                    "INSERT INTO project_agreements(id,project_id,version,review_seconds,snapshot,snapshot_hash) VALUES(%s,%s,1,604800,%s,%s)",
                    (gid, pid, Jsonb(terms), digest(terms)),
                )
                c.execute("UPDATE projects SET agreement_id=%s WHERE id=%s", (gid, pid))
                for i, m in enumerate(body.milestones, 1):
                    c.execute(
                        "INSERT INTO milestones(id,project_id,sequence_no,title,criteria,amount_minor,work_status) VALUES(%s,%s,%s,%s,%s,%s,'AGREED')",
                        (identifier(), pid, i, m.title, m.criteria, m.amount_minor),
                    )
                audit(
                    c,
                    a["id"],
                    "PROJECT_CREATED",
                    pid,
                    request.state.correlation,
                    "Immutable draft terms created; both parties must accept before funding.",
                )
                if body.accept_on_create:
                    c.execute(
                        "INSERT INTO agreement_acceptances(agreement_id,user_id,accepted_hash) VALUES(%s,%s,%s)",
                        (gid, a["id"], digest(terms)),
                    )
                    c.execute("UPDATE projects SET version=version+1 WHERE id=%s", (pid,))
                    audit(
                        c,
                        a["id"],
                        "AGREEMENT_ACCEPTED",
                        pid,
                        request.state.correlation,
                        "Client reviewed and accepted immutable terms when sending: " + digest(terms),
                    )
                return domain.project_view(
                    c, p | dict(agreement_id=gid, version=2 if body.accept_on_create else 1)
                )

            status, value, replay = idempotent(
                c, a["id"], "create-project", key, body.model_dump(mode="json"), 201, perform
            )
        return command_result(value, status, replay)

    @app.get("/api/v1/projects/{project_id}/agreement", tags=["project"])
    def agreement(project_id: UUID, a: Actor):
        with connect(cfg["app_dsn"]) as c:
            p = domain.visible_project(c, a, project_id)
            g = c.execute("SELECT * FROM project_agreements WHERE id=%s", (p["agreement_id"],)).fetchone()
            blockers = []
            for person in c.execute(
                """SELECT u.id,u.active,u.control_allowed,u.is_demo,s.state FROM users u
                LEFT JOIN sandbox_setups s ON s.user_id=u.id WHERE u.id IN (%s,%s) ORDER BY u.role""",
                (p["client_id"], p["freelancer_id"]),
            ):
                label = "Client" if person["id"] == p["client_id"] else "Freelancer"
                if not person["is_demo"] and person["state"] != "READY":
                    blockers.append(label + " needs to finish sandbox setup.")
                elif not person["active"] or not person["control_allowed"]:
                    blockers.append(label + " has a control hold requiring operations review.")
            beneficiary = c.execute(
                "SELECT active FROM beneficiaries WHERE id=%s", (p["beneficiary_id"],)
            ).fetchone()
            if not beneficiary["active"] and not any("Freelancer" in b for b in blockers):
                blockers.append("The payout destination requires operations review.")
            return result(
                g
                | dict(
                    funding_blockers=blockers,
                    project_version=p["version"],
                    acceptances=c.execute(
                        "SELECT user_id,accepted_at FROM agreement_acceptances WHERE agreement_id=%s",
                        (g["id"],),
                    ).fetchall(),
                )
            )

    @app.post("/api/v1/projects/{project_id}/acceptances", tags=["project"])
    def accept(project_id: UUID, body: AcceptAgreement, request: Request, a: Actor, key: Key):
        role(a, "CLIENT", "FREELANCER")
        with connect(cfg["app_dsn"]) as c:
            domain.visible_project(c, a, project_id)

            def perform():
                p = c.execute("SELECT * FROM projects WHERE id=%s FOR UPDATE", (project_id,)).fetchone()
                g = c.execute("SELECT * FROM project_agreements WHERE id=%s", (p["agreement_id"],)).fetchone()
                if (
                    p["version"] != body.expected_version
                    or p["status"] != "DRAFT"
                    or g["snapshot_hash"] != body.snapshot_hash
                ):
                    conflict("Terms or project state changed. Read the agreement again.")
                c.execute(
                    "INSERT INTO agreement_acceptances(agreement_id,user_id,accepted_hash) VALUES(%s,%s,%s) ON CONFLICT DO NOTHING",
                    (g["id"], a["id"], body.snapshot_hash),
                )
                n = c.execute(
                    "SELECT count(*) AS n FROM agreement_acceptances WHERE agreement_id=%s", (g["id"],)
                ).fetchone()["n"]
                c.execute(
                    "UPDATE projects SET status=%s,version=version+1 WHERE id=%s",
                    ("AGREED" if n == 2 else "DRAFT", project_id),
                )
                audit(
                    c,
                    a["id"],
                    "AGREEMENT_ACCEPTED",
                    project_id,
                    request.state.correlation,
                    "Participant accepted immutable agreement hash " + body.snapshot_hash,
                )
                return dict(accepted=True, project_version=p["version"] + 1)

            status, value, replay = idempotent(
                c, a["id"], f"accept:{project_id}", key, body.model_dump(mode="json"), 200, perform
            )
        return command_result(value, status, replay)

    @app.post("/api/v1/projects/{project_id}/funding", tags=["project"])
    def fund(project_id: UUID, body: ProjectVersion, request: Request, a: Actor, key: Key):
        role(a, "CLIENT")
        with connect(cfg["app_dsn"]) as c:
            domain.visible_project(c, a, project_id)
            # Narrow security-definer lock function avoids granting account updates.
            mid = c.execute(
                "SELECT id FROM milestones WHERE project_id=%s ORDER BY id LIMIT 1", (project_id,)
            ).fetchone()["id"]
            c.execute("SELECT lock_milestone_context(%s)", (mid,))

            def perform():
                p = c.execute("SELECT * FROM projects WHERE id=%s FOR UPDATE", (project_id,)).fetchone()
                if p["version"] != body.expected_version or p["status"] != "AGREED":
                    conflict("Both participants must accept before funding.")
                domain.controls(c, p)
                balance = c.execute(
                    "SELECT l.balance_minor FROM ledger_accounts l JOIN wallets w ON w.id=l.wallet_id WHERE w.owner_user_id=%s AND account_kind='AVAILABLE'",
                    (a["id"],),
                ).fetchone()
                if not balance or balance["balance_minor"] < p["total_minor"]:
                    raise Problem(
                        409,
                        "INSUFFICIENT_FUNDS",
                        "The full project budget must be available in your synthetic wallet.",
                    )
                c.execute("SELECT reserve_project(%s)", (project_id,))
                emit(
                    c,
                    "project.funded.v1",
                    "project",
                    project_id,
                    p["version"] + 1,
                    request.state.correlation,
                    dict(project_id=project_id, total_minor=p["total_minor"], currency="PKR"),
                )
                audit(
                    c,
                    a["id"],
                    "PROJECT_FUNDED",
                    project_id,
                    request.state.correlation,
                    "Full budget moved from available wallet balance to milestone reservations.",
                )
                return domain.project_view(
                    c, c.execute("SELECT * FROM projects WHERE id=%s", (project_id,)).fetchone()
                )

            status, value, replay = idempotent(
                c, a["id"], f"fund:{project_id}", key, body.model_dump(mode="json"), 200, perform
            )
        return command_result(value, status, replay)
