"""Human-governed operational controls; no route grants automatic financial authority."""

from typing import Annotated
from uuid import UUID
from fastapi import Depends, Header, Request
from psycopg.types.json import Jsonb
from milestone import domain
from milestone.common import Problem, audit, conflict, connect, digest, emit, idempotent, identifier, jsonable
from milestone.http_support import result
from milestone.models import ActionCommand, AssessmentCommand, DecisionCommand


SUMMARY_RULES = {
    "BANK_OUTCOME_UNKNOWN": "Keep funds payable. Enquire using the existing bank reference; do not create another transfer.",
    "BANK_OUTCOME_REQUIRES_REVIEW": "Compare authoritative bank evidence before changing financial state.",
    "CONTROL_HOLD": "Review recorded control evidence. Clearance requires a separate approver.",
    "MISSING_EXTERNAL": "Obtain an independent bank statement and investigate the missing external record.",
    "MISSING_INTERNAL": "Investigate the bank record without inventing an internal transaction.",
    "AMOUNT_MISMATCH": "Compare the agreed amount, journal entries and independent bank statement.",
    "STATUS_MISMATCH": "Compare bank outcome and local journal evidence before resolving this exception.",
    "DUPLICATE": "Investigate duplicate statement evidence; do not post another payment.",
}


def summarize(case):
    # No free-text evidence or user instructions are interpreted. Deterministic mock AI.
    known = case["category"] in SUMMARY_RULES
    return dict(
        engine="deterministic-exception-assistant-v1",
        exception_id=str(case["id"]),
        category=case["category"],
        state=case["state"],
        owner_id=str(case["owner_id"]),
        payout_id=str(case["payout_id"]) if case["payout_id"] else None,
        abstained=not known,
        confidence=1.0 if known else 0.0,
        recommendation=SUMMARY_RULES.get(
            case["category"], "Unsupported case. Ask a human investigator to review the source evidence."
        ),
        human_review_required=True,
        permitted_actions=[],
        source_version=case["version"],
    )


def execute_action(c, action, actor, corr):
    kind, target = action["kind"], action["target_id"]
    project_id = None
    if kind.startswith("DISPUTE_"):
        case = c.execute("SELECT * FROM disputes WHERE id=%s", (target,)).fetchone()
        if not case:
            conflict("Dispute no longer exists.")
        p, m = domain.context(c, actor, case["milestone_id"], lock=True)
        case = c.execute("SELECT * FROM disputes WHERE id=%s FOR UPDATE", (target,)).fetchone()
        if case["status"] != "OPEN":
            conflict("Dispute is already resolved.")
        project_id = p["id"]
        if kind == "DISPUTE_REWORK":
            if c.execute(
                "SELECT 1 FROM payouts WHERE milestone_id=%s AND state NOT IN ('FAILED','REVERSED')",
                (m["id"],),
            ).fetchone():
                conflict("An authorised or settled payout cannot be undone through dispute resolution.")
            c.execute(
                "UPDATE review_windows SET state='REVISED',closed_at=clock_timestamp(),blocked_reason='GOVERNED_REWORK' WHERE milestone_id=%s AND state IN ('OPEN','BLOCKED')",
                (m["id"],),
            )
            c.execute(
                "UPDATE milestones SET work_status='REVISION_REQUESTED',version=version+1 WHERE id=%s",
                (m["id"],),
            )
        elif case["stage"] != "POST_RELEASE":
            conflict("Pre-release cases require rework; closing a case must not revive an old deadline.")
        c.execute(
            "UPDATE disputes SET status='RESOLVED',version=version+1,resolution_action_id=%s WHERE id=%s",
            (action["id"], target),
        )
        for user in (p["client_id"], p["freelancer_id"]):
            c.execute(
                "INSERT INTO notifications(id,user_id,kind,dedupe_key,text) VALUES(%s,%s,'DISPUTE_RESOLVED',%s,%s)",
                (
                    identifier(),
                    user,
                    f"resolution:{target}:{user}",
                    "Human-reviewed case resolution recorded. Rework requires a new submission and fresh review. No funds were released by this decision.",
                ),
            )
    elif kind == "CONTROL_CLEAR":
        c.execute("SELECT id FROM users WHERE id=%s FOR UPDATE", (target,))
        assessment = c.execute(
            "SELECT * FROM control_assessments WHERE user_id=%s ORDER BY created_at DESC,id DESC LIMIT 1",
            (target,),
        ).fetchone()
        if not assessment or assessment["decision"] != "CLEAR_RECOMMENDED":
            conflict("Latest mock control assessment does not recommend clearance.")
        c.execute("UPDATE users SET control_allowed=true WHERE id=%s", (target,))
    elif kind == "PAYOUT_RETURN":
        pay = c.execute("SELECT * FROM payouts WHERE id=%s", (target,)).fetchone()
        if not pay:
            conflict("Payout not found.")
        p, _ = domain.context(c, actor, pay["milestone_id"], lock=True)
        pay = c.execute("SELECT * FROM payouts WHERE id=%s FOR UPDATE", (target,)).fetchone()
        if pay["state"] != "SETTLED":
            conflict("Only bank-confirmed settled payouts can be returned.")
        if c.execute("SELECT 1 FROM return_requests WHERE payout_id=%s", (target,)).fetchone():
            conflict("A return already exists; recover that reference rather than creating another.")
        project_id = p["id"]
        c.execute("INSERT INTO return_requests(id,payout_id) VALUES(%s,%s)", (action["id"], target))
        emit(
            c,
            "payout.return_requested.v1",
            "payout",
            target,
            pay["version"],
            corr,
            domain.payout_data(p, pay) | {"return_id": action["id"]},
            transaction=target,
            consumers=("payout-return", "notifications"),
        )
    elif kind == "PAYOUT_RETRY":
        pay = c.execute("SELECT * FROM payouts WHERE id=%s", (target,)).fetchone()
        if not pay:
            conflict("Payout not found.")
        p, m = domain.context(c, actor, pay["milestone_id"], lock=True)
        pay = c.execute("SELECT * FROM payouts WHERE id=%s FOR UPDATE", (target,)).fetchone()
        domain.controls(c, p)
        domain.no_dispute(c, m)
        if pay["state"] != "FAILED" or m["work_status"] != "APPROVED":
            conflict("Only a definitively rejected payout awaiting retry is eligible.")
        if c.execute(
            "SELECT 1 FROM payouts WHERE milestone_id=%s AND state NOT IN ('FAILED','REVERSED')", (m["id"],)
        ).fetchone():
            conflict("Another payment is active.")
        c.execute(
            "UPDATE milestones SET work_status='REVISION_REQUESTED',version=version+1 WHERE id=%s", (m["id"],)
        )
        project_id = p["id"]
    elif kind == "CANCEL_UNRELEASED":
        p, m = domain.context(c, actor, target, lock=True)
        domain.no_dispute(c, m)
        if (
            m["work_status"] == "CANCELLED"
            or c.execute(
                "SELECT 1 FROM payouts WHERE milestone_id=%s AND state NOT IN ('FAILED','REVERSED')",
                (target,),
            ).fetchone()
        ):
            conflict("Authorised or settled payments cannot be cancelled this way.")
        c.execute("SELECT cancel_reservation(%s,%s)", (target, corr))
        c.execute(
            "UPDATE review_windows SET state='CANCELLED',closed_at=clock_timestamp() WHERE milestone_id=%s AND state IN ('OPEN','BLOCKED')",
            (target,),
        )
        c.execute("UPDATE milestones SET work_status='CANCELLED',version=version+1 WHERE id=%s", (target,))
        project_id = p["id"]
    elif kind == "DELIVERY_REPLAY":
        updated = c.execute(
            "UPDATE event_deliveries SET state='READY',lease_until=NULL,lease_token=NULL,attempts=0,retry_cycle=retry_cycle+1,next_attempt_at=clock_timestamp() WHERE event_id=%s AND state='DEAD' RETURNING event_id",
            (target,),
        ).fetchone()
        if not updated:
            conflict("There is no dead delivery to replay.")
    elif kind == "EXCEPTION_CLOSE":
        case = c.execute("SELECT * FROM exceptions WHERE id=%s FOR UPDATE", (target,)).fetchone()
        if not case or case["state"] == "RESOLVED" or not case["payout_id"]:
            conflict("This exception requires investigation; no matched payout supports closure.")
        latest = c.execute(
            "SELECT x.id,x.completed_at,bool_and(r.classification='MATCHED') AS matched FROM reconciliation_results r JOIN reconciliation_runs x ON x.id=r.run_id WHERE r.payout_id=%s AND x.state='COMPLETED' GROUP BY x.id ORDER BY x.completed_at DESC LIMIT 1",
            (case["payout_id"],),
        ).fetchone()
        if not latest or not latest["matched"] or latest["completed_at"] < case["opened_at"]:
            conflict("A fresh matched reconciliation is required before closure.")
        c.execute("UPDATE exceptions SET state='RESOLVED',version=version+1 WHERE id=%s", (target,))
        c.execute(
            "UPDATE payouts SET reconciliation_status=CASE WHEN EXISTS(SELECT 1 FROM exceptions WHERE payout_id=%s AND state<>'RESOLVED') THEN 'EXCEPTION_OPEN' ELSE 'MATCHED' END WHERE id=%s",
            (case["payout_id"], case["payout_id"]),
        )
    audit(
        c,
        actor["id"],
        kind,
        project_id,
        corr,
        "Executed separately approved action " + str(action["id"]) + ". " + action["reason"],
    )


def register(app, cfg, actor, role, command_result):
    Actor = Annotated[dict, Depends(actor)]
    Key = Annotated[str, Header(alias="Idempotency-Key", min_length=8, max_length=128)]

    @app.get("/api/v1/ops/dashboard", tags=["operations"])
    def dashboard(a: Actor):
        role(a, "OPERATIONS", "FINANCIAL_APPROVER")
        with connect(cfg["app_dsn"]) as c:
            metrics = dict(
                payouts=c.execute(
                    "SELECT state,count(*) AS count,coalesce(sum(amount_minor),0) AS amount_minor FROM payouts GROUP BY state ORDER BY state"
                ).fetchall(),
                deliveries=c.execute(
                    "SELECT state,count(*) AS count FROM event_deliveries GROUP BY state ORDER BY state"
                ).fetchall(),
                balances=c.execute(
                    "SELECT account_kind,sum(balance_minor) AS amount_minor FROM ledger_accounts GROUP BY account_kind ORDER BY account_kind"
                ).fetchall(),
                oldest_ready_seconds=c.execute(
                    "SELECT coalesce(extract(epoch FROM clock_timestamp()-min(o.created_at)),0) AS value FROM event_deliveries d JOIN outbox_events o USING(event_id) WHERE d.state IN ('READY','PROCESSING')"
                ).fetchone()["value"],
            )
            return result(
                dict(
                    metrics=metrics,
                    users=c.execute(
                        "SELECT id,display_name,role,active,control_allowed FROM users WHERE role IN ('CLIENT','FREELANCER') ORDER BY id"
                    ).fetchall(),
                    disputes=c.execute(
                        "SELECT d.*,m.title AS milestone_title FROM disputes d JOIN milestones m ON m.id=d.milestone_id ORDER BY opened_at DESC LIMIT 100"
                    ).fetchall(),
                    exceptions=c.execute(
                        "SELECT * FROM exceptions ORDER BY opened_at DESC LIMIT 100"
                    ).fetchall(),
                    actions=c.execute(
                        "SELECT * FROM action_requests ORDER BY created_at DESC LIMIT 100"
                    ).fetchall(),
                    payouts=c.execute(
                        "SELECT id,state,amount_minor,public_reference,reconciliation_status FROM payouts ORDER BY authorised_at DESC LIMIT 100"
                    ).fetchall(),
                    returns=c.execute("SELECT * FROM return_requests ORDER BY id LIMIT 100").fetchall(),
                    dead_deliveries=c.execute(
                        "SELECT event_id,consumer_name,attempts,last_error,retry_cycle FROM event_deliveries WHERE state='DEAD' ORDER BY event_id LIMIT 100"
                    ).fetchall(),
                    reconciliation_runs=c.execute(
                        "SELECT * FROM reconciliation_runs ORDER BY started_at DESC NULLS FIRST LIMIT 30"
                    ).fetchall(),
                    milestones=c.execute(
                        "SELECT m.id,m.title,m.work_status,p.title AS project_title FROM milestones m JOIN projects p ON p.id=m.project_id WHERE p.status='FUNDED' ORDER BY p.id,m.sequence_no LIMIT 100"
                    ).fetchall(),
                    assessments=c.execute(
                        "SELECT * FROM control_assessments ORDER BY created_at DESC LIMIT 100"
                    ).fetchall(),
                )
            )

    @app.post("/api/v1/ops/users/{user_id}/assessments", tags=["controls"])
    def assess(user_id: UUID, body: AssessmentCommand, request: Request, a: Actor, key: Key):
        role(a, "OPERATIONS")
        with connect(cfg["app_dsn"]) as c:

            def perform():
                user = c.execute(
                    "SELECT * FROM users WHERE id=%s AND role IN ('CLIENT','FREELANCER') FOR UPDATE",
                    (user_id,),
                ).fetchone()
                if not user:
                    raise Problem(404, "NOT_FOUND", "Participant not found.")
                decision = "CLEAR_RECOMMENDED" if body.identity_verified and body.risk_score < 70 else "HOLD"
                row = c.execute(
                    "INSERT INTO control_assessments(id,user_id,actor_id,identity_verified,risk_score,evidence_ref,decision) VALUES(%s,%s,%s,%s,%s,%s,%s) RETURNING *",
                    (
                        identifier(),
                        user_id,
                        a["id"],
                        body.identity_verified,
                        body.risk_score,
                        body.evidence_reference,
                        decision,
                    ),
                ).fetchone()
                if decision == "HOLD":
                    c.execute("UPDATE users SET control_allowed=false WHERE id=%s", (user_id,))
                audit(
                    c,
                    a["id"],
                    "CONTROL_ASSESSED",
                    None,
                    request.state.correlation,
                    "Synthetic rule decision: " + decision,
                )
                return row

            status, value, replay = idempotent(
                c, a["id"], f"assess:{user_id}", key, body.model_dump(mode="json"), 201, perform
            )
        return command_result(value, status, replay)

    @app.post("/api/v1/ops/actions", tags=["governance"])
    def propose(body: ActionCommand, request: Request, a: Actor, key: Key):
        role(a, "OPERATIONS")
        with connect(cfg["app_dsn"]) as c:

            def perform():
                tables = {
                    "DISPUTE_REWORK": "disputes",
                    "DISPUTE_CLOSE": "disputes",
                    "CONTROL_CLEAR": "users",
                    "PAYOUT_RETURN": "payouts",
                    "EXCEPTION_CLOSE": "exceptions",
                    "DELIVERY_REPLAY": "outbox_events",
                    "CANCEL_UNRELEASED": "milestones",
                    "PAYOUT_RETRY": "payouts",
                }
                from psycopg import sql

                table = tables[body.kind]
                field = "event_id" if table == "outbox_events" else "id"
                if not c.execute(
                    sql.SQL("SELECT 1 FROM {} WHERE {}=%s").format(
                        sql.Identifier(table), sql.Identifier(field)
                    ),
                    (body.target_id,),
                ).fetchone():
                    raise Problem(404, "NOT_FOUND", "Action target not found.")
                row = c.execute(
                    "INSERT INTO action_requests(id,kind,target_id,maker_id,reason,evidence_ref) VALUES(%s,%s,%s,%s,%s,%s) RETURNING *",
                    (identifier(), body.kind, body.target_id, a["id"], body.reason, body.evidence_reference),
                ).fetchone()
                audit(
                    c,
                    a["id"],
                    "ACTION_PROPOSED",
                    None,
                    request.state.correlation,
                    "Proposed " + body.kind + "; no execution yet.",
                )
                return row

            status, value, replay = idempotent(
                c, a["id"], "propose-action", key, body.model_dump(mode="json"), 201, perform
            )
        return command_result(value, status, replay)

    @app.post("/api/v1/ops/actions/{action_id}/decisions", tags=["governance"])
    def decide(action_id: UUID, body: DecisionCommand, request: Request, a: Actor, key: Key):
        role(a, "FINANCIAL_APPROVER")
        with connect(cfg["app_dsn"]) as c:

            def perform():
                action = c.execute(
                    "SELECT * FROM action_requests WHERE id=%s FOR UPDATE", (action_id,)
                ).fetchone()
                if not action:
                    raise Problem(404, "NOT_FOUND", "Action not found.")
                if action["maker_id"] == a["id"]:
                    raise Problem(403, "FORBIDDEN", "Maker cannot approve their own action.")
                if action["state"] != "PROPOSED" or action["version"] != body.expected_version:
                    conflict("Action state changed; refresh before deciding.")
                if body.approve:
                    execute_action(c, action, a, request.state.correlation)
                row = c.execute(
                    "UPDATE action_requests SET state=%s,checker_id=%s,version=version+1,decided_at=clock_timestamp() WHERE id=%s RETURNING *",
                    ("APPROVED" if body.approve else "REJECTED", a["id"], action_id),
                ).fetchone()
                audit(
                    c,
                    a["id"],
                    "ACTION_DECIDED",
                    None,
                    request.state.correlation,
                    "Action " + str(action_id) + " " + row["state"],
                )
                return row

            status, value, replay = idempotent(
                c, a["id"], f"decide:{action_id}", key, body.model_dump(mode="json"), 200, perform
            )
        return command_result(value, status, replay)

    @app.post("/api/v1/ops/exceptions/{case_id}/summary", tags=["assistant"])
    def summary(case_id: UUID, request: Request, a: Actor):
        role(a, "OPERATIONS", "FINANCIAL_APPROVER")
        with connect(cfg["app_dsn"]) as c:
            case = c.execute("SELECT * FROM exceptions WHERE id=%s", (case_id,)).fetchone()
            if not case:
                raise Problem(404, "NOT_FOUND", "Exception not found.")
            value = summarize(case)
            c.execute(
                "INSERT INTO assistant_summaries(id,exception_id,actor_id,engine,input_hash,result) VALUES(%s,%s,%s,%s,%s,%s)",
                (identifier(), case_id, a["id"], value["engine"], digest(jsonable(case)), Jsonb(value)),
            )
            audit(
                c,
                a["id"],
                "ASSISTANT_SUMMARY",
                None,
                request.state.correlation,
                "Read-only deterministic summary recorded; human review required.",
            )
            return result(value)
