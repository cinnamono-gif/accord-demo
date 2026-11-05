"""Password authentication and revocable, short-lived sessions for the prototype."""

from datetime import timedelta
import hashlib
import hmac
import secrets
import jwt
from milestone.common import Problem, connect, now

# A fixed valid hash avoids a fast return for nonexistent accounts.
DUMMY_HASH = "36e88fdde5865d895b4fc850e41ec6c3:" + "0" * 128


def user_view(user):
    return dict(
        id=str(user["id"]), display_name=user["display_name"], roles=[user["role"]], is_demo=user["is_demo"]
    )


def token_hash(value):
    return hashlib.sha256(value.encode()).hexdigest()


def issue_session(c, cfg, user):
    issued = now()
    expires = issued + timedelta(hours=1)
    nonce = secrets.token_urlsafe(32)
    c.execute("DELETE FROM auth_sessions WHERE expires_at < clock_timestamp()")
    c.execute(
        "INSERT INTO auth_sessions(token_hash,user_id,expires_at) VALUES(%s,%s,%s)",
        (token_hash(nonce), user["id"], expires),
    )
    token = jwt.encode(
        dict(sub=str(user["id"]), jti=nonce, iat=issued, exp=expires, aud="milestone", iss="milestone-local"),
        cfg["jwt_secret"],
        algorithm="HS256",
    )
    return dict(access_token=token, token_type="Bearer", expires_in=3600, user=user_view(user))


def throttle(cfg, operation, login):
    # Durable across function instances; keyed by HMAC so stored buckets contain no email.
    subject = hmac.new(cfg["jwt_secret"].encode(), login.encode(), hashlib.sha256).hexdigest()
    allowed = True
    with connect(cfg["app_dsn"]) as c:
        c.execute("DELETE FROM auth_attempts WHERE opened_at < clock_timestamp()-interval '1 day'")
        for bucket, maximum in [(operation + ":all", 60), (operation + ":" + subject, 8)]:
            row = c.execute(
                """
                INSERT INTO auth_attempts VALUES(%s,clock_timestamp(),1)
                ON CONFLICT(bucket) DO UPDATE SET
                 attempts=CASE WHEN auth_attempts.opened_at<clock_timestamp()-interval '1 minute'
                   THEN 1 ELSE auth_attempts.attempts+1 END,
                 opened_at=CASE WHEN auth_attempts.opened_at<clock_timestamp()-interval '1 minute'
                   THEN clock_timestamp() ELSE auth_attempts.opened_at END RETURNING attempts
            """,
                (bucket,),
            ).fetchone()
            allowed = allowed and row["attempts"] <= maximum
    if not allowed:
        raise Problem(429, "RATE_LIMITED", "Too many attempts. Wait a minute and try again.")
