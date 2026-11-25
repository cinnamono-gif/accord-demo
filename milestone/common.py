from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path
from uuid import UUID, uuid4
import hashlib
import hmac
import json
import os
import secrets

import psycopg
from psycopg.rows import dict_row
from psycopg.types.json import Jsonb

ROOT = Path(__file__).resolve().parents[2]


def config():
    path = Path(os.environ.get("MILESTONE_CONFIG", ROOT / ".local/config.json"))
    values = json.loads(path.read_text()) if path.exists() else {}
    for key in (
        "app_dsn",
        "bank_dsn",
        "app_owner_dsn",
        "bank_owner_dsn",
        "bank_url",
        "bank_key",
        "jwt_secret",
        "cron_secret",
        "demo_password",
        "public_demo",
        "sandbox_setup_enabled",
    ):
        if os.environ.get(key.upper()):
            values[key] = os.environ[key.upper()]
    return values


def connect(dsn):
    return psycopg.connect(dsn, row_factory=dict_row, connect_timeout=5)


def now():
    return datetime.now(timezone.utc)


def identifier():
    return str(uuid4())


def jsonable(value):
    return json.loads(
        json.dumps(value, default=lambda x: x.isoformat() if isinstance(x, datetime) else str(x))
    )


def canonical(value):
    return json.dumps(jsonable(value), sort_keys=True, separators=(",", ":"))


def digest(value):
    return hashlib.sha256(canonical(value).encode()).hexdigest()


def hash_password(password, salt=None):
    salt = salt or secrets.token_hex(16)
    derived = hashlib.scrypt(password.encode(), salt=bytes.fromhex(salt), n=16384, r=8, p=1).hex()
    return salt + ":" + derived


def verify_password(password, stored):
    return hmac.compare_digest(hash_password(password, stored.split(":")[0]), stored)


@dataclass
class Problem(Exception):
    status: int
    code: str
    detail: str


def conflict(detail):
    raise Problem(409, "STATE_CONFLICT", detail)


def audit(c, actor, action, project, correlation, summary, payout=None, outcome="SUCCESS"):
    c.execute(
        "INSERT INTO audit_events(id,actor_id,action,project_id,payout_id,correlation_id,outcome,summary) VALUES(%s,%s,%s,%s,%s,%s,%s,%s)",
        (identifier(), actor, action, project, payout, correlation, outcome, summary),
    )


def emit(
    c,
    kind,
    aggregate_type,
    aggregate_id,
    version,
    correlation,
    data,
    *,
    producer="api",
    transaction=None,
    causation=None,
    consumers=("notifications",),
):
    event_id = identifier()
    envelope = jsonable(
        dict(
            event_id=event_id,
            event_type=kind,
            schema_version=1,
            occurred_at=now(),
            producer=producer,
            aggregate_type=aggregate_type,
            aggregate_id=aggregate_id,
            aggregate_version=version,
            transaction_id=transaction,
            correlation_id=correlation,
            causation_id=causation,
            data=data,
        )
    )
    c.execute("INSERT INTO outbox_events(event_id,envelope) VALUES(%s,%s)", (event_id, Jsonb(envelope)))
    for consumer in consumers:
        c.execute("INSERT INTO event_deliveries(event_id,consumer_name) VALUES(%s,%s)", (event_id, consumer))
    return event_id


def idempotent(c, actor, operation, key, body, status, perform):
    # Transaction-scoped advisory lock serialises simultaneous first use of the same key.
    c.execute("SELECT pg_advisory_xact_lock(hashtextextended(%s,0))", (f"{actor}:{operation}:{key}",))
    old = c.execute(
        "SELECT * FROM idempotency_records WHERE actor_id=%s AND operation=%s AND key=%s",
        (actor, operation, key),
    ).fetchone()
    request_hash = digest(body)
    if old:
        if old["request_hash"] != request_hash:
            raise Problem(
                409, "IDEMPOTENCY_KEY_REUSED", "This key was already used with a different request."
            )
        return old["response_status"], old["response_body"], True
    result = jsonable(perform())
    c.execute(
        "INSERT INTO idempotency_records VALUES(%s,%s,%s,%s,%s,%s)",
        (actor, operation, key, request_hash, status, Jsonb(result)),
    )
    return status, result, False


def as_uuid(value):
    return str(UUID(str(value)))
