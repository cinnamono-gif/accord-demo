"""Durable polling worker: each external call occurs outside the local transaction."""

from datetime import timedelta
import argparse
import json
import time
from uuid import UUID
import httpx
from psycopg.types.json import Jsonb
from milestone import domain
from milestone.common import Problem, audit, config, connect, digest, emit, identifier, jsonable, now
from milestone.models import ApprovalCommand


class BankClient:
    def __init__(self, cfg, client=None):
        self.cfg = cfg
        self.client = client or httpx.Client(timeout=5.0)
        self.owned = client is None

    def close(self):
        if self.owned:
            self.client.close()

    def request(self, method, path, corr, **kwargs):
        response = self.client.request(
            method,
            self.cfg["bank_url"] + path,
            headers={"X-Service-Key": self.cfg["bank_key"], "X-Correlation-ID": str(corr)},
            **kwargs,
        )
        response.raise_for_status()
        return response.json()

    def transfer(self, payload, corr, enquire=False):
        if enquire:
            try:
                return self.request("GET", "/transfers/" + str(payload["reference"]), corr)
            except httpx.HTTPStatusError as exc:
                if exc.response.status_code != 404:
                    raise
                # Not-found is uncertain. Resending the SAME bank-idempotent reference is safe.
        return self.request("POST", "/transfers", corr, json=payload)

    def statement(self, start, end, corr):
        result = None
        cursor = None
        rows = []
        seen = set()
        while True:
            params = {"from_inclusive": start.isoformat(), "to_exclusive": end.isoformat()}
            if cursor:
                params["cursor"] = cursor
            part = self.request("GET", "/statements", corr, params=params)
            if digest(part["rows"]) != part["content_hash"]:
                raise ValueError("Bank statement content hash mismatch")
            result = part
            rows.extend(part["rows"])
            cursor = part.get("next_cursor")
            if not cursor:
                break
            if cursor in seen or len(seen) >= 1000:
                raise ValueError("Invalid statement pagination")
            seen.add(cursor)
        return result | {"rows": rows, "content_hash": digest(rows)}


class Worker:
    def __init__(self, cfg=None, bank=None):
        self.cfg = cfg or config()
        self.bank = bank or BankClient(self.cfg)

    def claim(self):
        with connect(self.cfg["app_dsn"]) as c:
            row = c.execute(
                "SELECT d.*,o.envelope FROM event_deliveries d JOIN outbox_events o USING(event_id) WHERE d.state IN ('READY','PROCESSING') AND d.next_attempt_at<=clock_timestamp() AND (d.lease_until IS NULL OR d.lease_until<clock_timestamp()) ORDER BY d.next_attempt_at,o.created_at,d.consumer_name LIMIT 1 FOR UPDATE OF d SKIP LOCKED"
            ).fetchone()
            if not row:
                return None
            token = identifier()
            c.execute(
                "UPDATE event_deliveries SET state='PROCESSING',lease_token=%s,lease_until=clock_timestamp()+interval '30 seconds',attempts=attempts+1 WHERE event_id=%s AND consumer_name=%s",
                (token, row["event_id"], row["consumer_name"]),
            )
            return row | {"lease_token": token, "attempts": row["attempts"] + 1}

    @staticmethod
    def done(c, job):
        c.execute(
            "UPDATE event_deliveries SET state='DONE',lease_until=NULL,processed_at=clock_timestamp(),last_error=NULL WHERE event_id=%s AND consumer_name=%s AND lease_token=%s",
            (job["event_id"], job["consumer_name"], job["lease_token"]),
        )

    def later(self, job, error):
        dead = job["attempts"] >= 8
        delay = min(300, 2 ** min(job["attempts"], 9))
        with connect(self.cfg["app_dsn"]) as c:
            changed = c.execute(
                "UPDATE event_deliveries SET state=%s,lease_until=NULL,next_attempt_at=clock_timestamp()+%s*interval '1 second',last_error=%s WHERE event_id=%s AND consumer_name=%s AND lease_token=%s AND state='PROCESSING' RETURNING event_id",
                (
                    "DEAD" if dead else "READY",
                    delay,
                    error[:160],
                    job["event_id"],
                    job["consumer_name"],
                    job["lease_token"],
                ),
            ).fetchone()
            if dead and changed:
                audit(
                    c,
                    None,
                    "DELIVERY_DEAD",
                    None,
                    job["envelope"]["correlation_id"],
                    "Retry budget exhausted for "
                    + job["consumer_name"]
                    + "; funds unchanged. Governed replay required.",
                )

    def return_payment(self, job):
        rid = job["envelope"]["data"]["return_id"]
        with connect(self.cfg["app_dsn"]) as c:
            r = c.execute("SELECT * FROM return_requests WHERE id=%s", (rid,)).fetchone()
            if not r:
                raise ValueError("Return request missing")
            p, pay = self.lock_payout(c, r["payout_id"])
            if r["state"] == "CONFIRMED":
                self.done(c, job)
                return
            if pay["state"] != "SETTLED":
                raise ValueError("Original payout is not settled")
        # Bank idempotency covers bank commit followed by a lost response.
        proof = self.bank.request(
            "POST",
            "/returns",
            pay["correlation_id"],
            json=dict(reference=str(rid), original_reference=str(pay["public_reference"])),
        )
        if (
            str(proof.get("reference")) != str(rid)
            or str(proof.get("original_reference")) != str(pay["public_reference"])
            or proof.get("amount_minor") != pay["amount_minor"]
            or proof.get("currency") != "PKR"
            or not proof.get("posted_at")
        ):
            raise ValueError("Return evidence mismatch")
        UUID(proof["bank_record_id"])
        with connect(self.cfg["app_dsn"]) as c:
            p, pay = self.lock_payout(c, r["payout_id"])
            r = c.execute("SELECT * FROM return_requests WHERE id=%s FOR UPDATE", (rid,)).fetchone()
            if r["state"] == "CONFIRMED":
                self.done(c, job)
                return
            if pay["state"] != "SETTLED":
                raise ValueError("Original state changed")
            c.execute(
                "UPDATE return_requests SET state='CONFIRMED',bank_record_id=%s,evidence=%s,confirmed_at=clock_timestamp() WHERE id=%s",
                (proof["bank_record_id"], Jsonb(proof), rid),
            )
            journal = c.execute("SELECT post_confirmed_return(%s) AS id", (rid,)).fetchone()["id"]
            c.execute(
                "UPDATE payouts SET state='REVERSED',version=version+1,reconciliation_status='UNCHECKED' WHERE id=%s",
                (pay["id"],),
            )
            c.execute(
                "UPDATE milestones SET work_status='REVISION_REQUESTED',version=version+1 WHERE id=%s",
                (pay["milestone_id"],),
            )
            emit(
                c,
                "payout.reversed.v1",
                "payout",
                pay["id"],
                pay["version"] + 1,
                pay["correlation_id"],
                domain.payout_data(p, pay)
                | dict(return_id=rid, bank_record_id=proof["bank_record_id"], journal_id=journal),
                transaction=pay["id"],
                producer="worker",
            )
            audit(
                c,
                None,
                "PAYOUT_REVERSED",
                p["id"],
                pay["correlation_id"],
                "Bank-confirmed full return restored reserved funds once.",
                pay["id"],
            )
            self.done(c, job)

    @staticmethod
    def lock_payout(c, pid):
        pay = c.execute("SELECT * FROM payouts WHERE id=%s", (pid,)).fetchone()
        if not pay:
            raise ValueError("Missing payout")
        p, m = domain.context(c, {"role": "OPERATIONS", "id": None}, pay["milestone_id"], lock=True)
        pay = c.execute("SELECT * FROM payouts WHERE id=%s FOR UPDATE", (pid,)).fetchone()
        return p, pay

    def pending(self, c, p, pay, event, reason):
        if pay["state"] in ("SETTLED", "FAILED", "REVERSED"):
            return
        if pay["state"] != "PENDING":
            updated = c.execute(
                "UPDATE payouts SET state='PENDING',version=version+1 WHERE id=%s RETURNING version",
                (pay["id"],),
            ).fetchone()
            emit(
                c,
                "payout.pending.v1",
                "payout",
                pay["id"],
                updated["version"],
                pay["correlation_id"],
                domain.payout_data(p, pay)
                | dict(reason=reason, next_enquiry_at=now() + timedelta(seconds=2)),
                producer="worker",
                transaction=pay["id"],
                causation=event,
                consumers=("payout-enquiry", "notifications"),
            )
            audit(
                c,
                None,
                "PAYOUT_PENDING",
                p["id"],
                pay["correlation_id"],
                "Bank outcome uncertain; payable funds retained and same-reference enquiry scheduled.",
                pay["id"],
            )

    def dispatch(self, job):
        event = job["envelope"]
        pid = event["data"]["payout_id"]
        with connect(self.cfg["app_dsn"]) as c:
            p, pay = self.lock_payout(c, pid)
            if pay["state"] in ("SETTLED", "FAILED", "REVERSED"):
                self.done(c, job)
                return
            enquire = pay["state"] != "AUTHORISED"
            if not enquire:
                try:
                    domain.controls(c, p)
                except Problem:
                    c.execute("UPDATE payouts SET control_hold=true WHERE id=%s", (pid,))
                    self.exception(c, "CONTROL_HOLD", pay, "Dispatch blocked by current controls.")
                    c.execute(
                        "UPDATE event_deliveries SET state='READY',lease_until=NULL,next_attempt_at=clock_timestamp()+interval '10 seconds',last_error='CONTROL_HOLD' WHERE event_id=%s AND consumer_name=%s AND lease_token=%s",
                        (job["event_id"], job["consumer_name"], job["lease_token"]),
                    )
                    return
                c.execute(
                    "UPDATE payouts SET state='DISPATCHING',dispatched_at=clock_timestamp(),version=version+1,control_hold=false WHERE id=%s",
                    (pid,),
                )
                audit(
                    c,
                    None,
                    "PAYOUT_DISPATCHING",
                    p["id"],
                    pay["correlation_id"],
                    "Durable dispatch claim saved before bank request.",
                    pid,
                )
            destination = c.execute(
                "SELECT synthetic_account_ref FROM beneficiaries WHERE id=%s", (pay["beneficiary_id"],)
            ).fetchone()["synthetic_account_ref"]
            payload = jsonable(
                dict(
                    reference=pay["public_reference"],
                    source_account_ref="SYNTH-SETTLEMENT-001",
                    beneficiary_account_ref=destination,
                    amount_minor=pay["amount_minor"],
                    currency="PKR",
                )
            )
        try:
            evidence = self.bank.transfer(payload, pay["correlation_id"], enquire=enquire)
            UUID(evidence["bank_record_id"])
            with connect(self.cfg["app_dsn"]) as c:
                p, current = self.lock_payout(c, pid)
                if evidence.get("outcome") == "SUCCEEDED":
                    domain.settle(c, p, current, evidence, job["event_id"])
                    self.done(c, job)
                elif evidence.get("outcome") == "REJECTED":
                    domain.reject(c, p, current, evidence, job["event_id"])
                    self.done(c, job)
                else:
                    self.pending(c, p, current, job["event_id"], "ACKNOWLEDGEMENT_UNCERTAIN")
                    self.exception(
                        c,
                        "BANK_OUTCOME_REQUIRES_REVIEW",
                        current,
                        "Bank processing or unexpected evidence; retain payable funds for investigation.",
                    )
            if evidence.get("outcome") not in ("SUCCEEDED", "REJECTED"):
                self.later(job, "Bank outcome requires review; reservation not released")
        except (httpx.HTTPError, ValueError, KeyError, Problem) as exc:
            with connect(self.cfg["app_dsn"]) as c:
                p, current = self.lock_payout(c, pid)
                self.pending(
                    c,
                    p,
                    current,
                    job["event_id"],
                    "TRANSPORT_TIMEOUT"
                    if isinstance(exc, httpx.TimeoutException)
                    else "ACKNOWLEDGEMENT_UNCERTAIN",
                )
                if job["attempts"] >= 4:
                    self.exception(
                        c, "BANK_OUTCOME_UNKNOWN", current, "Repeated uncertainty; funds remain payable."
                    )
            self.later(job, type(exc).__name__)

    @staticmethod
    def exception(c, category, pay, evidence):
        owner = c.execute(
            "SELECT id FROM users WHERE role='OPERATIONS' AND active ORDER BY id LIMIT 1"
        ).fetchone()["id"]
        pid = pay["id"] if pay else None
        key = category + ":" + str(pid) + ":" + (digest(evidence) if pid is None else "open")
        row = c.execute(
            "INSERT INTO exceptions(id,dedupe_key,category,payout_id,owner_id,evidence) VALUES(%s,%s,%s,%s,%s,%s) ON CONFLICT(dedupe_key) DO UPDATE SET evidence=excluded.evidence,state='OPEN',opened_at=clock_timestamp(),version=exceptions.version+1 RETURNING id,version,(xmax=0) AS inserted",
            (identifier(), key, category, pid, owner, Jsonb(jsonable(evidence))),
        ).fetchone()
        return row

    def notify(self, job):
        event = job["envelope"]
        with connect(self.cfg["app_dsn"]) as c:
            receipt = c.execute(
                "SELECT state,lease_token FROM event_deliveries WHERE event_id=%s AND consumer_name=%s FOR UPDATE",
                (job["event_id"], job["consumer_name"]),
            ).fetchone()
            if receipt["state"] == "DONE" or str(receipt["lease_token"]) != str(job["lease_token"]):
                return
            project_id = event["data"].get("project_id")
            if project_id:
                p = c.execute("SELECT * FROM projects WHERE id=%s", (project_id,)).fetchone()
                for recipient in [p["client_id"], p["freelancer_id"]]:
                    kind = event["event_type"]
                    key = f"{event['event_id']}:{recipient}"
                    if kind == "milestone.review_opened.v1":
                        if recipient != p["client_id"]:
                            continue
                        key = f"{event['data']['review_window_id']}:{recipient}:REVIEW_OPENED"
                    c.execute(
                        "INSERT INTO notifications(id,user_id,event_id,kind,dedupe_key,text) VALUES(%s,%s,%s,%s,%s,%s) ON CONFLICT(dedupe_key) DO NOTHING",
                        (
                            identifier(),
                            recipient,
                            job["event_id"],
                            kind,
                            key,
                            "Recorded: " + kind + ". Open the project for current status.",
                        ),
                    )
            self.done(c, job)

    def reconciliation(self):
        with connect(self.cfg["app_dsn"]) as c:
            run = c.execute(
                "SELECT * FROM reconciliation_runs WHERE state IN ('QUEUED','RUNNING') AND (lease_until IS NULL OR lease_until<clock_timestamp()) ORDER BY id LIMIT 1 FOR UPDATE SKIP LOCKED"
            ).fetchone()
            if not run:
                return False
            c.execute(
                "UPDATE reconciliation_runs SET state='RUNNING',started_at=coalesce(started_at,clock_timestamp()),lease_until=clock_timestamp()+interval '30 seconds' WHERE id=%s",
                (run["id"],),
            )
        try:
            statement = self.bank.statement(run["from_inclusive"], run["to_exclusive"], run["correlation_id"])
            # Bank acknowledgement can be lost. Fetch corroborating destination-aware evidence
            # before opening the comparison transaction; never do network IO under row locks.
            recovered = {}
            for row in statement["rows"]:
                if row["outcome"] != "SUCCEEDED" or row["direction"] != "OUTGOING":
                    continue
                with connect(self.cfg["app_dsn"]) as c:
                    unresolved = c.execute(
                        "SELECT id FROM payouts WHERE public_reference=%s AND state IN ('DISPATCHING','PENDING')",
                        (row["transfer_reference"],),
                    ).fetchone()
                if unresolved:
                    proof = self.bank.request(
                        "GET", "/transfers/" + row["transfer_reference"], run["correlation_id"]
                    )
                    if (
                        proof.get("bank_record_id") == row["record_id"]
                        and proof.get("amount_minor") == row["amount_minor"]
                        and proof.get("currency") == row["currency"]
                    ):
                        recovered[str(unresolved["id"])] = proof
            with connect(self.cfg["app_dsn"]) as c:
                locked = c.execute(
                    "SELECT state FROM reconciliation_runs WHERE id=%s FOR UPDATE", (run["id"],)
                ).fetchone()
                if locked["state"] == "COMPLETED":
                    return True
                for pid in sorted(recovered):
                    project, pay = self.lock_payout(c, pid)
                    domain.settle(c, project, pay, recovered[pid])
                c.execute(
                    "INSERT INTO bank_statement_imports(id,run_id,source_reference,content_hash,evidence) VALUES(%s,%s,%s,%s,%s)",
                    (
                        identifier(),
                        run["id"],
                        statement["source_reference"],
                        statement["content_hash"],
                        Jsonb(statement),
                    ),
                )
                matched, exceptions = self.compare(c, run, statement["rows"])
                c.execute(
                    "UPDATE reconciliation_runs SET state='COMPLETED',completed_at=clock_timestamp(),lease_until=NULL,matched_count=%s,exception_count=%s WHERE id=%s",
                    (matched, exceptions, run["id"]),
                )
                audit(
                    c,
                    None,
                    "RECONCILIATION_COMPLETED",
                    None,
                    run["correlation_id"],
                    f"Independent statement: {matched} matches, {exceptions} exceptions.",
                )
        except Exception as exc:
            with connect(self.cfg["app_dsn"]) as c:
                c.execute(
                    "UPDATE reconciliation_runs SET state='FAILED',completed_at=clock_timestamp(),lease_until=NULL,last_error=%s WHERE id=%s AND state<>'COMPLETED'",
                    (type(exc).__name__, run["id"]),
                )
            print(
                json.dumps(
                    {"action": "reconciliation_failed", "run_id": str(run["id"]), "error": type(exc).__name__}
                ),
                flush=True,
            )
        return True

    def compare(self, c, run, rows):
        returns = [r for r in rows if r["direction"] == "INCOMING_RETURN"]
        rows = [r for r in rows if r["direction"] != "INCOMING_RETURN"]
        grouped = {}
        for row in rows:
            grouped.setdefault(row["transfer_reference"], []).append(row)
        pays = c.execute(
            "SELECT p.* FROM payouts p WHERE p.dispatched_at<%s AND ((p.settled_at>=%s AND p.settled_at<%s) OR p.state IN ('DISPATCHING','PENDING')) ORDER BY p.id",
            (run["to_exclusive"], run["from_inclusive"], run["to_exclusive"]),
        ).fetchall()
        internal = {str(p["public_reference"]): p for p in pays}
        matched = exceptions = 0
        for reference in sorted(set(grouped) | set(internal)):
            bankrows = grouped.get(reference, [])
            pay = internal.get(reference)
            if not pay:
                pay = c.execute("SELECT * FROM payouts WHERE public_reference=%s", (reference,)).fetchone()
            row = bankrows[0] if bankrows else None
            if not pay:
                classification = "MISSING_INTERNAL"
            elif not bankrows:
                classification = "MISSING_EXTERNAL"
            elif len(bankrows) > 1:
                classification = "DUPLICATE"
            elif row["amount_minor"] != pay["amount_minor"] or row["currency"] != "PKR":
                classification = "AMOUNT_MISMATCH"
            elif (
                row["outcome"] != "SUCCEEDED"
                or row["direction"] != "OUTGOING"
                or pay["state"] not in ("SETTLED", "REVERSED")
                or str(pay["bank_record_id"]) != row["record_id"]
            ):
                classification = "STATUS_MISMATCH"
            elif (
                c.execute(
                    "SELECT coalesce(sum(e.amount_minor),0) AS n FROM journal_entries e JOIN journal_batches b ON b.id=e.batch_id JOIN ledger_accounts a ON a.id=e.account_id WHERE b.payout_id=%s AND b.kind='SETTLE' AND a.account_kind='SETTLEMENT_ASSET' AND e.side='CREDIT'",
                    (pay["id"],),
                ).fetchone()["n"]
                != pay["amount_minor"]
            ):
                classification = "STATUS_MISMATCH"
            else:
                classification = "MATCHED"
            evidence = jsonable(
                dict(
                    bank_rows=bankrows,
                    local_state=pay["state"] if pay else None,
                    expected_minor=pay["amount_minor"] if pay else None,
                )
            )
            c.execute(
                "INSERT INTO reconciliation_results VALUES(%s,%s,%s,%s,%s,%s)",
                (
                    identifier(),
                    run["id"],
                    pay["id"] if pay else None,
                    row["record_id"] if row else None,
                    classification,
                    Jsonb(evidence),
                ),
            )
            if classification == "MATCHED":
                matched += 1
                # An unresolved earlier exception remains visible even if a later statement matches.
                c.execute(
                    "UPDATE payouts SET reconciliation_status=CASE WHEN EXISTS(SELECT 1 FROM exceptions e WHERE e.payout_id=payouts.id AND e.state<>'RESOLVED') THEN 'EXCEPTION_OPEN' ELSE 'MATCHED' END WHERE id=%s",
                    (pay["id"],),
                )
            else:
                exceptions += 1
                case = self.exception(c, classification, pay, evidence)
                if pay:
                    c.execute(
                        "UPDATE payouts SET reconciliation_status='EXCEPTION_OPEN' WHERE id=%s", (pay["id"],)
                    )
                if case["inserted"]:
                    eid = case["id"]
                    emit(
                        c,
                        "reconciliation.exception_opened.v1",
                        "exception",
                        eid,
                        case["version"],
                        run["correlation_id"],
                        dict(
                            exception_id=eid,
                            run_id=run["id"],
                            payout_id=pay["id"] if pay else None,
                            classification=classification,
                            expected_minor=pay["amount_minor"] if pay else None,
                            actual_minor=row["amount_minor"] if row else None,
                        ),
                        producer="worker",
                    )
        return_matched, return_exceptions = self.compare_returns(c, run, returns)
        return matched + return_matched, exceptions + return_exceptions

    def compare_returns(self, c, run, rows):
        groups = {}
        for row in rows:
            groups.setdefault(row.get("return_reference"), []).append(row)
        expected = c.execute(
            "SELECT * FROM return_requests WHERE state='CONFIRMED' AND (evidence->>'posted_at')::timestamptz >= %s AND (evidence->>'posted_at')::timestamptz < %s",
            (run["from_inclusive"], run["to_exclusive"]),
        ).fetchall()
        for r in expected:
            groups.setdefault(str(r["id"]), [])
        matched = exceptions = 0
        for ref, bankrows in groups.items():
            r = c.execute("SELECT * FROM return_requests WHERE id=%s", (ref,)).fetchone() if ref else None
            pay = c.execute("SELECT * FROM payouts WHERE id=%s", (r["payout_id"],)).fetchone() if r else None
            row = bankrows[0] if bankrows else None
            posted = (
                c.execute(
                    "SELECT 1 FROM journal_batches WHERE payout_id=%s AND kind='RETURN'", (pay["id"],)
                ).fetchone()
                if pay
                else None
            )
            if not r:
                classification = "MISSING_INTERNAL"
            elif not row:
                classification = "MISSING_EXTERNAL"
            elif len(bankrows) > 1:
                classification = "DUPLICATE"
            elif row["amount_minor"] != pay["amount_minor"] or row["currency"] != "PKR":
                classification = "AMOUNT_MISMATCH"
            elif (
                r["state"] != "CONFIRMED"
                or pay["state"] != "REVERSED"
                or not posted
                or str(row["record_id"]) != str(r["bank_record_id"])
                or str(row["transfer_reference"]) != str(pay["public_reference"])
                or row["outcome"] != "SUCCEEDED"
            ):
                classification = "STATUS_MISMATCH"
            else:
                classification = "MATCHED"
            evidence = jsonable(dict(return_reference=ref, bank_rows=bankrows))
            c.execute(
                "INSERT INTO reconciliation_results VALUES(%s,%s,%s,%s,%s,%s)",
                (
                    identifier(),
                    run["id"],
                    pay["id"] if pay else None,
                    row["record_id"] if row else None,
                    classification,
                    Jsonb(evidence),
                ),
            )
            if classification == "MATCHED":
                matched += 1
            else:
                exceptions += 1
                self.exception(c, classification, pay, evidence)
            if pay:
                c.execute(
                    "UPDATE payouts SET reconciliation_status=CASE WHEN EXISTS(SELECT 1 FROM exceptions WHERE payout_id=%s AND state<>'RESOLVED') THEN 'EXCEPTION_OPEN' ELSE 'MATCHED' END WHERE id=%s",
                    (pay["id"], pay["id"]),
                )
        return matched, exceptions

    def reviews(self):
        """Discover durable deadlines; lock and recheck before any financial effect."""
        with connect(self.cfg["app_dsn"]) as c:
            at = domain.review_now(c)
            candidate = c.execute(
                "SELECT w.* FROM review_windows w WHERE w.state='OPEN' AND (w.due_at<=%s OR (w.due_at<=%s AND NOT EXISTS (SELECT 1 FROM notifications n WHERE n.review_window_id=w.id AND n.kind='REVIEW_REMINDER'))) ORDER BY w.due_at,w.id LIMIT 1",
                (at, at + timedelta(hours=24)),
            ).fetchone()
            if not candidate:
                return False
            actor = {"role": "OPERATIONS", "id": None}
            p, m = domain.context(c, actor, candidate["milestone_id"], lock=True)
            w = c.execute(
                "SELECT * FROM review_windows WHERE id=%s FOR UPDATE", (candidate["id"],)
            ).fetchone()
            at = domain.review_now(c)
            if w["state"] != "OPEN" or m["work_status"] != "SUBMITTED":
                return False
            corr = identifier()
            try:
                # Roll back a failed authorisation before recording the persistent block.
                with c.transaction():
                    domain.controls(c, p)
                    domain.no_dispute(c, m)
                    if at >= w["due_at"]:
                        body = ApprovalCommand(
                            submission_id=w["submission_id"],
                            review_window_id=w["id"],
                            expected_version=m["version"],
                        )
                        domain.approve(c, actor, p, m, body, corr, timed=True)
                        return True
            except Problem as exc:
                domain.block_review(c, p, m, w, "CONTROL_HOLD", corr)
                c.execute("UPDATE milestones SET version=version+1 WHERE id=%s", (m["id"],))
                audit(c, None, "REVIEW_BLOCKED", p["id"], corr, "Automatic release blocked: " + exc.detail)
                return True
            inserted = c.execute(
                "INSERT INTO notifications(id,user_id,review_window_id,kind,dedupe_key,text) VALUES(%s,%s,%s,'REVIEW_REMINDER',%s,%s) ON CONFLICT(dedupe_key) DO NOTHING RETURNING id",
                (
                    identifier(),
                    p["client_id"],
                    w["id"],
                    f"{w['id']}:REVIEW_REMINDER",
                    "Your milestone review is due within 24 hours. Approve, request revisions or open a dispute. Undisputed work becomes eligible for automatic release at the deadline.",
                ),
            ).fetchone()
            if inserted:
                audit(
                    c,
                    None,
                    "REVIEW_REMINDER",
                    p["id"],
                    corr,
                    "One in-app reminder recorded within 24 hours of the review deadline.",
                )
            return bool(inserted)

    def once(self):
        if self.reviews():
            return True
        job = self.claim()
        if job:
            try:
                if job["consumer_name"] == "notifications":
                    self.notify(job)
                elif job["consumer_name"] in ("payout-dispatch", "payout-enquiry"):
                    self.dispatch(job)
                elif job["consumer_name"] == "payout-return":
                    self.return_payment(job)
                else:
                    raise ValueError("Unsupported consumer")
            except Exception as exc:
                self.later(job, type(exc).__name__)
                print(
                    json.dumps(
                        {
                            "action": "delivery_retry",
                            "event_id": str(job["event_id"]),
                            "error": type(exc).__name__,
                        }
                    ),
                    flush=True,
                )
            return True
        return self.reconciliation()


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--once", action="store_true")
    args = parser.parse_args()
    worker = Worker()
    try:
        if args.once:
            worker.once()
        else:
            while True:
                if not worker.once():
                    time.sleep(0.5)
    finally:
        worker.bank.close()


if __name__ == "__main__":
    main()
