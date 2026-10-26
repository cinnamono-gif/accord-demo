from datetime import timedelta
from milestone.common import Problem, audit, conflict, emit, identifier, jsonable


def visible_project(c, actor, project_id):
    p = c.execute("SELECT * FROM projects WHERE id=%s", (project_id,)).fetchone()
    if not p or (
        actor["role"] not in ("OPERATIONS", "FINANCIAL_APPROVER")
        and actor["id"] not in (p["client_id"], p["freelancer_id"])
    ):
        raise Problem(404, "NOT_FOUND", "Project not found.")
    return p


def context(c, actor, milestone_id, *, lock=False):
    m = c.execute("SELECT * FROM milestones WHERE id=%s", (milestone_id,)).fetchone()
    if not m:
        raise Problem(404, "NOT_FOUND", "Milestone not found.")
    p = visible_project(c, actor, m["project_id"])
    if lock:
        # All commands use participant -> project -> milestone -> payout -> account lock order.
        c.execute("SELECT lock_milestone_context(%s)", (milestone_id,))
        p = c.execute("SELECT * FROM projects WHERE id=%s", (p["id"],)).fetchone()
        m = c.execute("SELECT * FROM milestones WHERE id=%s", (milestone_id,)).fetchone()
    return p, m


def controls(c, p):
    people = c.execute(
        "SELECT active,control_allowed FROM users WHERE id IN (%s,%s)", (p["client_id"], p["freelancer_id"])
    ).fetchall()
    beneficiary = c.execute("SELECT * FROM beneficiaries WHERE id=%s", (p["beneficiary_id"],)).fetchone()
    if (
        len(people) != 2
        or not all(x["active"] and x["control_allowed"] for x in people)
        or not beneficiary["active"]
    ):
        raise Problem(409, "CONTROL_HOLD", "A participant or beneficiary is on hold.")


def project_view(c, p):
    ms = c.execute(
        "SELECT id,sequence_no,title,criteria AS acceptance_criteria,amount_minor,work_status,version FROM milestones WHERE project_id=%s ORDER BY sequence_no",
        (p["id"],),
    ).fetchall()
    return {
        k: p[k]
        for k in [
            "id",
            "title",
            "client_id",
            "freelancer_id",
            "beneficiary_id",
            "currency",
            "total_minor",
            "status",
            "version",
            "agreement_id",
        ]
    } | {"milestones": ms}


def submission_view(c, s):
    w = c.execute(
        "SELECT id,submission_id,opened_at,due_at,state AS status FROM review_windows WHERE submission_id=%s ORDER BY opened_at DESC LIMIT 1",
        (s["id"],),
    ).fetchone()
    version = c.execute("SELECT version FROM milestones WHERE id=%s", (s["milestone_id"],)).fetchone()[
        "version"
    ]
    return dict(
        id=s["id"],
        milestone_id=s["milestone_id"],
        submission_version=s["version"],
        description=s["description"],
        evidence_reference=s["evidence_ref"],
        submitted_at=s["submitted_at"],
        milestone_version=version,
        review_window=w,
    )


def payout_view(p):
    return dict(
        id=p["id"],
        reference=p["public_reference"],
        milestone_id=p["milestone_id"],
        amount_minor=p["amount_minor"],
        currency=p["currency"],
        state=p["state"],
        approval_mode=p["approval_mode"],
        authorised_at=p["authorised_at"],
        settled_at=p["settled_at"],
        previous_payout_id=p["previous_payout_id"],
        correlation_id=p["correlation_id"],
        reconciliation_status=p["reconciliation_status"],
        control_hold=p["control_hold"],
    )


def submit(c, actor, p, m, body, corr):
    if actor["id"] != p["freelancer_id"]:
        raise Problem(403, "FORBIDDEN", "Only the project freelancer may submit work.")
    if (
        m["version"] != body.expected_version
        or p["status"] != "FUNDED"
        or m["work_status"] not in ("AGREED", "REVISION_REQUESTED")
    ):
        conflict("This milestone is not awaiting a submission. Refresh its state.")
    controls(c, p)
    no_dispute(c, m)
    version = c.execute(
        "SELECT coalesce(max(version),0)+1 AS n FROM submissions WHERE milestone_id=%s", (m["id"],)
    ).fetchone()["n"]
    sid, wid = identifier(), identifier()
    s = c.execute(
        "INSERT INTO submissions(id,milestone_id,version,submitted_by,description,evidence_ref) VALUES(%s,%s,%s,%s,%s,%s) RETURNING *",
        (sid, m["id"], version, actor["id"], body.description, body.evidence_reference),
    ).fetchone()
    due = s["submitted_at"] + timedelta(seconds=604800)
    c.execute(
        "INSERT INTO review_windows(id,milestone_id,submission_id,agreement_id,opened_at,due_at,state) VALUES(%s,%s,%s,%s,%s,%s,'OPEN')",
        (wid, m["id"], sid, p["agreement_id"], s["submitted_at"], due),
    )
    c.execute("UPDATE milestones SET work_status='SUBMITTED',version=version+1 WHERE id=%s", (m["id"],))
    event = emit(
        c,
        "milestone.review_opened.v1",
        "milestone",
        m["id"],
        m["version"] + 1,
        corr,
        dict(
            project_id=p["id"],
            milestone_id=m["id"],
            submission_id=sid,
            review_window_id=wid,
            due_at=due,
            review_seconds=604800,
        ),
    )
    c.execute(
        "INSERT INTO notifications(id,user_id,event_id,review_window_id,kind,dedupe_key,text) VALUES(%s,%s,%s,%s,'REVIEW_OPENED',%s,%s)",
        (
            identifier(),
            p["client_id"],
            event,
            wid,
            f"{wid}:{p['client_id']}:REVIEW_OPENED",
            "Work submitted. Your seven-day review window is open.",
        ),
    )
    audit(
        c, actor["id"], "WORK_SUBMITTED", p["id"], corr, "Submission saved; seven-day review window opened."
    )
    return submission_view(c, s)


def review_now(c):
    """Authoritative database time; no public endpoint accepts a clock override."""
    return c.execute("SELECT clock_timestamp() AS value").fetchone()["value"]


def no_dispute(c, m):
    if c.execute("SELECT 1 FROM disputes WHERE milestone_id=%s AND status='OPEN'", (m["id"],)).fetchone():
        raise Problem(409, "CONTROL_HOLD", "An unresolved dispute blocks release and resubmission.")


def approve(c, actor, p, m, body, corr, *, timed=False):
    if not timed and actor["id"] != p["client_id"]:
        raise Problem(403, "FORBIDDEN", "Only the project client may approve work.")
    controls(c, p)
    no_dispute(c, m)
    if m["version"] != body.expected_version or m["work_status"] != "SUBMITTED" or p["status"] != "FUNDED":
        conflict("Milestone state changed. Refresh before approving.")
    w = c.execute(
        "SELECT * FROM review_windows WHERE id=%s AND milestone_id=%s AND submission_id=%s FOR UPDATE",
        (body.review_window_id, m["id"], body.submission_id),
    ).fetchone()
    if not w or w["state"] not in (("OPEN",) if timed else ("OPEN", "BLOCKED")):
        conflict("This review window is no longer eligible for approval.")
    if str(w["agreement_id"]) != str(p["agreement_id"]):
        conflict("The review does not belong to the current agreement.")
    if timed and review_now(c) < w["due_at"]:
        conflict("The seven-day deadline has not elapsed.")
    latest = c.execute(
        "SELECT id FROM submissions WHERE milestone_id=%s ORDER BY version DESC LIMIT 1", (m["id"],)
    ).fetchone()
    if not latest or str(latest["id"]) != str(body.submission_id):
        conflict("Only the latest submission can be approved.")
    consent = c.execute(
        "SELECT count(*) AS n FROM agreement_acceptances a JOIN project_agreements g ON g.id=a.agreement_id WHERE g.id=%s AND g.review_seconds=604800 AND a.accepted_hash=g.snapshot_hash AND a.user_id IN (%s,%s)",
        (p["agreement_id"], p["client_id"], p["freelancer_id"]),
    ).fetchone()["n"]
    if consent != 2:
        raise Problem(409, "CONSENT_REQUIRED", "Both parties must have accepted these terms.")
    if timed:
        agreement = c.execute(
            "SELECT snapshot FROM project_agreements WHERE id=%s", (p["agreement_id"],)
        ).fetchone()
        if (
            agreement["snapshot"].get("policy")
            != "Synthetic full funding; approval or safeguarded seven-day release; full milestones; no fees."
        ):
            raise Problem(
                409, "CONSENT_REQUIRED", "This agreement does not contain the supported timed-release policy."
            )
    if c.execute(
        "SELECT 1 FROM payouts WHERE milestone_id=%s AND state IN ('AUTHORISED','DISPATCHING','PENDING','SETTLED')",
        (m["id"],),
    ).fetchone():
        conflict("This milestone already has an active or settled payout.")
    reserved = c.execute(
        "SELECT balance_minor FROM ledger_accounts WHERE milestone_id=%s AND account_kind='RESERVED'",
        (m["id"],),
    ).fetchone()
    if not reserved or reserved["balance_minor"] < m["amount_minor"]:
        raise Problem(409, "INSUFFICIENT_FUNDS", "The full milestone reservation is not available.")
    decision, pid = identifier(), identifier()
    mode = "TIMED_POLICY" if timed else "CLIENT"
    c.execute(
        "INSERT INTO review_decisions(id,milestone_id,submission_id,window_id,actor_id,kind) VALUES(%s,%s,%s,%s,%s,%s)",
        (
            decision,
            m["id"],
            body.submission_id,
            body.review_window_id,
            None if timed else actor["id"],
            "TIMED_APPROVAL" if timed else "MANUAL_APPROVAL",
        ),
    )
    previous = c.execute(
        "SELECT id FROM payouts WHERE milestone_id=%s AND id<>%s AND state IN ('FAILED','REVERSED') ORDER BY authorised_at DESC LIMIT 1",
        (m["id"], pid),
    ).fetchone()
    pay = c.execute(
        "INSERT INTO payouts(id,public_reference,milestone_id,review_decision_id,beneficiary_id,amount_minor,currency,state,approval_mode,correlation_id,previous_payout_id) VALUES(%s,%s,%s,%s,%s,%s,'PKR','AUTHORISED',%s,%s,%s) RETURNING *",
        (
            pid,
            pid,
            m["id"],
            decision,
            p["beneficiary_id"],
            m["amount_minor"],
            mode,
            corr,
            previous["id"] if previous else None,
        ),
    ).fetchone()
    journal = c.execute("SELECT post_payment(%s,'AUTHORISE') AS id", (pid,)).fetchone()["id"]
    c.execute("UPDATE milestones SET work_status='APPROVED',version=version+1 WHERE id=%s", (m["id"],))
    c.execute(
        "UPDATE review_windows SET state='APPROVED',closed_at=clock_timestamp() WHERE id=%s",
        (body.review_window_id,),
    )
    emit(
        c,
        "payout.authorised.v1",
        "payout",
        pid,
        1,
        corr,
        payout_data(p, pay)
        | dict(beneficiary_id=p["beneficiary_id"], approval_mode=mode, journal_id=journal),
        producer="worker" if timed else "api",
        transaction=pid,
        consumers=("payout-dispatch", "notifications"),
    )
    audit(
        c,
        actor["id"],
        "PAYOUT_AUTHORISED",
        p["id"],
        corr,
        ("Undisputed seven-day policy" if timed else "Client approval")
        + " committed; payout queued, bank settlement not yet confirmed.",
        pid,
    )
    return payout_view(pay)


def block_review(c, p, m, w, reason, corr, case_id=None):
    c.execute(
        "UPDATE review_windows SET state='BLOCKED',blocked_reason=%s,closed_at=clock_timestamp() WHERE id=%s",
        (reason, w["id"]),
    )
    emit(
        c,
        "milestone.review_blocked.v1",
        "milestone",
        m["id"],
        m["version"] + 1,
        corr,
        dict(
            project_id=p["id"], milestone_id=m["id"], review_window_id=w["id"], reason=reason, case_id=case_id
        ),
    )


def dispute_view(c, row):
    version = c.execute("SELECT version FROM milestones WHERE id=%s", (row["milestone_id"],)).fetchone()[
        "version"
    ]
    return {k: row[k] for k in ("id", "milestone_id", "stage", "status", "reason", "opened_at")} | {
        "milestone_version": version
    }


def open_dispute(c, actor, p, m, body, corr):
    if actor["id"] not in (p["client_id"], p["freelancer_id"]):
        raise Problem(403, "FORBIDDEN", "Only a project participant may open a dispute.")
    if m["version"] != body.expected_version:
        conflict("Milestone state changed. Refresh before opening the case.")
    no_dispute(c, m)
    released = c.execute("SELECT 1 FROM payouts WHERE milestone_id=%s", (m["id"],)).fetchone()
    stage = "POST_RELEASE" if released else "PRE_RELEASE"
    owner = c.execute(
        "SELECT id FROM users WHERE role='OPERATIONS' AND active ORDER BY id LIMIT 1"
    ).fetchone()
    if not owner:
        raise Problem(409, "CONTROL_HOLD", "An operations owner must be available for this case.")
    row = c.execute(
        "INSERT INTO disputes(id,milestone_id,status,opened_by,reason,stage,owner_id) VALUES(%s,%s,'OPEN',%s,%s,%s,%s) RETURNING *",
        (identifier(), m["id"], actor["id"], body.reason, stage, owner["id"]),
    ).fetchone()
    w = c.execute(
        "SELECT * FROM review_windows WHERE milestone_id=%s AND state='OPEN'", (m["id"],)
    ).fetchone()
    if w:
        block_review(c, p, m, w, "DISPUTE", corr, row["id"])
    c.execute("UPDATE milestones SET version=version+1 WHERE id=%s", (m["id"],))
    summary = (
        "Dispute opened before authorisation. Release is blocked and reserved funds stay held."
        if stage == "PRE_RELEASE"
        else "Post-authorisation dispute opened for investigation. This does not cancel or reverse an authorised bank payment."
    )
    audit(c, actor["id"], "DISPUTE_OPENED", p["id"], corr, summary)
    for recipient in (p["client_id"], p["freelancer_id"]):
        c.execute(
            "INSERT INTO notifications(id,user_id,kind,dedupe_key,text) VALUES(%s,%s,'DISPUTE_OPENED',%s,%s)",
            (identifier(), recipient, f"{row['id']}:{recipient}", summary),
        )
    return dispute_view(c, row)


def request_revision(c, actor, p, m, body, corr):
    if actor["id"] != p["client_id"]:
        raise Problem(403, "FORBIDDEN", "Only the client may request revisions.")
    if m["version"] != body.expected_version or m["work_status"] != "SUBMITTED":
        conflict("Milestone state changed. Refresh before requesting revisions.")
    no_dispute(c, m)
    controls(c, p)
    w = c.execute(
        "SELECT * FROM review_windows WHERE id=%s AND submission_id=%s AND milestone_id=%s AND state IN ('OPEN','BLOCKED')",
        (body.review_window_id, body.submission_id, m["id"]),
    ).fetchone()
    if not w:
        conflict("This review is no longer eligible for revisions.")
    c.execute(
        "UPDATE review_windows SET state='REVISED',blocked_reason='REVISION_REQUESTED',closed_at=clock_timestamp() WHERE id=%s",
        (w["id"],),
    )
    row = c.execute(
        "INSERT INTO review_decisions(id,milestone_id,submission_id,window_id,actor_id,kind) VALUES(%s,%s,%s,%s,%s,'REQUEST_REVISIONS') RETURNING *",
        (identifier(), m["id"], body.submission_id, w["id"], actor["id"]),
    ).fetchone()
    c.execute(
        "UPDATE milestones SET work_status='REVISION_REQUESTED',version=version+1 WHERE id=%s", (m["id"],)
    )
    emit(
        c,
        "milestone.review_blocked.v1",
        "milestone",
        m["id"],
        m["version"] + 1,
        corr,
        dict(
            project_id=p["id"],
            milestone_id=m["id"],
            review_window_id=w["id"],
            reason="REVISION_REQUESTED",
            case_id=None,
        ),
    )
    audit(
        c,
        actor["id"],
        "REVISION_REQUESTED",
        p["id"],
        corr,
        "Revisions requested; old deadline cancelled. " + body.reason,
    )
    return {
        k: row[k] for k in ("id", "milestone_id", "submission_id", "kind", "actor_id", "decided_at")
    } | dict(review_window_id=w["id"], milestone_version=m["version"] + 1)


def payout_data(project, payout):
    return dict(
        project_id=project["id"],
        milestone_id=payout["milestone_id"],
        payout_id=payout["id"],
        payout_reference=payout["public_reference"],
        amount_minor=payout["amount_minor"],
        currency="PKR",
    )


def validate_bank_evidence(c, pay, evidence, outcome):
    destination = c.execute(
        "SELECT synthetic_account_ref FROM beneficiaries WHERE id=%s", (pay["beneficiary_id"],)
    ).fetchone()["synthetic_account_ref"]
    if (
        str(evidence.get("reference")) != str(pay["public_reference"])
        or evidence.get("amount_minor") != pay["amount_minor"]
        or evidence.get("currency") != "PKR"
        or evidence.get("beneficiary_account_ref") != destination
        or evidence.get("source_account_ref") != "SYNTH-SETTLEMENT-001"
        or evidence.get("outcome") != outcome
        or (outcome == "SUCCEEDED" and not evidence.get("posted_at"))
        or (
            outcome == "REJECTED"
            and (evidence.get("posted_at") is not None or not evidence.get("rejection_code"))
        )
    ):
        raise Problem(409, "STATE_CONFLICT", "Bank evidence does not match the authorised payout.")


def reject(c, p, pay, evidence, causation=None):
    validate_bank_evidence(c, pay, evidence, "REJECTED")
    if pay["state"] == "FAILED":
        return
    if pay["state"] not in ("DISPATCHING", "PENDING"):
        conflict("Payment is not eligible for rejection compensation.")
    journal = c.execute("SELECT post_payment(%s,'REJECT') AS id", (pay["id"],)).fetchone()["id"]
    updated = c.execute(
        "UPDATE payouts SET state='FAILED',bank_record_id=%s,version=version+1 WHERE id=%s RETURNING *",
        (evidence["bank_record_id"], pay["id"]),
    ).fetchone()
    emit(
        c,
        "payout.failed.v1",
        "payout",
        pay["id"],
        updated["version"],
        pay["correlation_id"],
        payout_data(p, pay)
        | dict(
            bank_record_id=evidence["bank_record_id"],
            failure_code=evidence["rejection_code"],
            compensation_journal_id=journal,
        ),
        producer="worker",
        transaction=pay["id"],
        causation=causation,
    )
    audit(
        c,
        None,
        "PAYOUT_FAILED",
        p["id"],
        pay["correlation_id"],
        "Definitive bank rejection; reservation restored once. A new payout requires a separately authorised retry.",
        pay["id"],
    )


def settle(c, p, pay, evidence, causation=None):
    validate_bank_evidence(c, pay, evidence, "SUCCEEDED")
    if pay["state"] == "SETTLED":
        return
    if pay["state"] not in ("DISPATCHING", "PENDING"):
        conflict("Payment is not eligible for settlement.")
    journal = c.execute("SELECT post_payment(%s,'SETTLE') AS id", (pay["id"],)).fetchone()["id"]
    updated = c.execute(
        "UPDATE payouts SET state='SETTLED',settled_at=clock_timestamp(),bank_record_id=%s,version=version+1 WHERE id=%s RETURNING *",
        (evidence["bank_record_id"], pay["id"]),
    ).fetchone()
    emit(
        c,
        "payout.settled.v1",
        "payout",
        pay["id"],
        updated["version"],
        pay["correlation_id"],
        payout_data(p, pay)
        | dict(
            bank_record_id=evidence["bank_record_id"],
            settlement_journal_id=journal,
            settled_at=updated["settled_at"],
        ),
        producer="worker",
        transaction=pay["id"],
        causation=causation,
    )
    audit(
        c,
        None,
        "PAYOUT_SETTLED",
        p["id"],
        pay["correlation_id"],
        "Matching bank evidence confirmed settlement.",
        pay["id"],
    )


def run_view(row):
    return jsonable(
        {
            k: row[k]
            for k in [
                "id",
                "state",
                "from_inclusive",
                "to_exclusive",
                "started_at",
                "completed_at",
                "matched_count",
                "exception_count",
            ]
        }
    )
