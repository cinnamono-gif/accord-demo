from datetime import datetime
from typing import Annotated
from uuid import UUID
import hmac
from fastapi import Depends, FastAPI, Header, Request
from milestone.common import Problem, config, connect, digest, identifier, jsonable
from milestone.http_support import install, result
from milestone.models import ReconciliationCommand, TransferCommand, ReturnCommand


def transfer_view(row):
    return {
        k: row[k]
        for k in [
            "reference",
            "bank_record_id",
            "outcome",
            "amount_minor",
            "currency",
            "source_account_ref",
            "beneficiary_account_ref",
            "recorded_at",
            "posted_at",
            "rejection_code",
        ]
    }


def create_app(settings=None):
    cfg = settings or config()
    app = FastAPI(
        title="Independent synthetic bank",
        version="0.1.0",
        description="Private local mock. Own database, account balances and postings. No real payments.",
    )
    install(app)

    def service(key: Annotated[str | None, Header(alias="X-Service-Key")] = None):
        if not key or not hmac.compare_digest(key, cfg["bank_key"]):
            raise Problem(401, "UNAUTHENTICATED", "Invalid service credentials.")

    @app.get("/health")
    def health():
        with connect(cfg["bank_dsn"]) as c:
            c.execute("SELECT 1")
        return {"status": "ok", "service": "bank"}

    @app.post("/bank/v1/transfers", status_code=201, dependencies=[Depends(service)])
    def transfer(body: TransferCommand, request: Request):
        payload = body.model_dump(mode="json")
        with connect(cfg["bank_dsn"]) as c:
            c.execute("SELECT pg_advisory_xact_lock(hashtextextended(%s,0))", (str(body.reference),))
            old = c.execute("SELECT * FROM bank_transfers WHERE reference=%s", (body.reference,)).fetchone()
            if old:
                if old["request_hash"] != digest(payload):
                    raise Problem(
                        409, "IDEMPOTENCY_KEY_REUSED", "Transfer reference was used with different terms."
                    )
                return result(transfer_view(old), 201)
            accounts = c.execute(
                "SELECT * FROM bank_accounts WHERE reference IN (%s,%s) ORDER BY reference FOR UPDATE",
                (body.source_account_ref, body.beneficiary_account_ref),
            ).fetchall()
            balances = {r["reference"]: r["balance_minor"] for r in accounts}
            if body.source_account_ref != "SYNTH-SETTLEMENT-001":
                raise Problem(403, "FORBIDDEN", "Service may debit only its settlement account.")
            if len(balances) != 2:
                raise Problem(422, "VALIDATION_ERROR", "Unknown synthetic account.")
            succeeded = balances[body.source_account_ref] >= body.amount_minor
            row = c.execute(
                "INSERT INTO bank_transfers(reference,bank_record_id,source_account_ref,beneficiary_account_ref,amount_minor,currency,outcome,posted_at,rejection_code,request_hash) VALUES(%s,%s,%s,%s,%s,%s,%s,CASE WHEN %s THEN clock_timestamp() ELSE NULL END,%s,%s) RETURNING *",
                (
                    body.reference,
                    identifier(),
                    body.source_account_ref,
                    body.beneficiary_account_ref,
                    body.amount_minor,
                    "PKR",
                    "SUCCEEDED" if succeeded else "REJECTED",
                    succeeded,
                    None if succeeded else "INSUFFICIENT_FUNDS",
                    digest(payload),
                ),
            ).fetchone()
            if succeeded:
                for account, delta in [
                    (body.source_account_ref, -body.amount_minor),
                    (body.beneficiary_account_ref, body.amount_minor),
                ]:
                    c.execute(
                        "UPDATE bank_accounts SET balance_minor=balance_minor+%s WHERE reference=%s",
                        (delta, account),
                    )
                    c.execute(
                        "INSERT INTO bank_postings(transfer_reference,account_ref,delta_minor) VALUES(%s,%s,%s)",
                        (body.reference, account, delta),
                    )
        return result(transfer_view(row), 201)

    @app.get("/bank/v1/transfers/{reference}", dependencies=[Depends(service)])
    def enquiry(reference: UUID):
        with connect(cfg["bank_dsn"]) as c:
            row = c.execute("SELECT * FROM bank_transfers WHERE reference=%s", (reference,)).fetchone()
            if not row:
                raise Problem(
                    404, "NOT_FOUND", "Reference not currently visible; this is not a terminal rejection."
                )
            return result(transfer_view(row))

    @app.post("/bank/v1/returns", dependencies=[Depends(service)])
    def return_transfer(body: ReturnCommand):
        with connect(cfg["bank_dsn"]) as c:
            c.execute("SELECT pg_advisory_xact_lock(hashtextextended(%s,0))", (str(body.original_reference),))
            old = c.execute(
                "SELECT * FROM bank_returns WHERE reference=%s OR original_reference=%s",
                (body.reference, body.original_reference),
            ).fetchone()
            if old:
                if old["reference"] != body.reference or old["original_reference"] != body.original_reference:
                    raise Problem(
                        409, "STATE_CONFLICT", "Original transfer already returned under another reference."
                    )
                return result(old)
            original = c.execute(
                "SELECT * FROM bank_transfers WHERE reference=%s AND outcome='SUCCEEDED'",
                (body.original_reference,),
            ).fetchone()
            if not original:
                raise Problem(409, "STATE_CONFLICT", "A successful original transfer is required.")
            accounts = c.execute(
                "SELECT * FROM bank_accounts WHERE reference IN (%s,%s) ORDER BY reference FOR UPDATE",
                (original["source_account_ref"], original["beneficiary_account_ref"]),
            ).fetchall()
            balances = {a["reference"]: a["balance_minor"] for a in accounts}
            if balances[original["beneficiary_account_ref"]] < original["amount_minor"]:
                raise Problem(
                    409, "INSUFFICIENT_FUNDS", "Return cannot post; beneficiary funds are unavailable."
                )
            row = c.execute(
                "INSERT INTO bank_returns(reference,original_reference,bank_record_id,amount_minor,currency) VALUES(%s,%s,%s,%s,%s) RETURNING *",
                (body.reference, body.original_reference, identifier(), original["amount_minor"], "PKR"),
            ).fetchone()
            for account, delta in (
                (original["source_account_ref"], original["amount_minor"]),
                (original["beneficiary_account_ref"], -original["amount_minor"]),
            ):
                c.execute(
                    "UPDATE bank_accounts SET balance_minor=balance_minor+%s WHERE reference=%s",
                    (delta, account),
                )
                c.execute(
                    "INSERT INTO bank_return_postings(return_reference,account_ref,delta_minor) VALUES(%s,%s,%s)",
                    (body.reference, account, delta),
                )
            return result(row)

    @app.get("/bank/v1/statements", dependencies=[Depends(service)])
    def statement(from_inclusive: datetime, to_exclusive: datetime, cursor: str | None = None):
        try:
            ReconciliationCommand(from_inclusive=from_inclusive, to_exclusive=to_exclusive)
        except ValueError:
            raise Problem(422, "VALIDATION_ERROR", "Invalid statement interval.") from None
        if cursor:
            raise Problem(422, "VALIDATION_ERROR", "This bounded synthetic statement has no further pages.")
        with connect(cfg["bank_dsn"]) as c:
            rows = c.execute(
                "SELECT t.bank_record_id AS record_id,t.reference AS transfer_reference,NULL::uuid AS return_reference,'OUTGOING' AS direction,-p.delta_minor AS amount_minor,t.currency,t.outcome,t.posted_at FROM bank_postings p JOIN bank_transfers t ON t.reference=p.transfer_reference WHERE p.account_ref='SYNTH-SETTLEMENT-001' AND p.delta_minor<0 AND t.posted_at >= %s AND t.posted_at < %s ORDER BY t.posted_at,t.reference",
                (from_inclusive, to_exclusive),
            ).fetchall()
            rows += c.execute(
                "SELECT bank_record_id AS record_id,original_reference AS transfer_reference,reference AS return_reference,'INCOMING_RETURN' AS direction,amount_minor,currency,'SUCCEEDED' AS outcome,posted_at FROM bank_returns WHERE posted_at>=%s AND posted_at<%s ORDER BY posted_at,reference",
                (from_inclusive, to_exclusive),
            ).fetchall()
        rows = jsonable(rows)
        return result(
            dict(
                source_reference="synthetic:" + digest([from_inclusive, to_exclusive])[:32],
                content_hash=digest(rows),
                from_inclusive=from_inclusive,
                to_exclusive=to_exclusive,
                rows=rows,
                next_cursor=None,
            )
        )

    return app
