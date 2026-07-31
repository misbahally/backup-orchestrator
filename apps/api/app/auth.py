import hashlib
import hmac
import secrets
from collections.abc import Iterable
from datetime import datetime, timedelta, timezone

from fastapi import HTTPException, Request
from sqlalchemy.orm import Session

from .config import settings
from .database import SessionLocal
from .models import User, UserSession

ADMIN_USERNAME = "admin"
PBKDF2_ITERATIONS = 390_000
SESSION_TTL = timedelta(hours=12)


def configured_api_keys() -> list[str]:
    return [k.strip() for k in settings.api_keys.split(",") if k.strip()]


def _matches_any(token: str, keys: Iterable[str]) -> bool:
    return any(secrets.compare_digest(token, key) for key in keys)


def hash_password(password: str) -> str:
    salt = secrets.token_bytes(16)
    digest = hashlib.pbkdf2_hmac("sha256", password.encode("utf-8"), salt, PBKDF2_ITERATIONS)
    return f"pbkdf2_sha256${PBKDF2_ITERATIONS}${salt.hex()}${digest.hex()}"


def verify_password(password: str, password_hash: str) -> bool:
    try:
        algorithm, iterations, salt_hex, digest_hex = password_hash.split("$")
        if algorithm != "pbkdf2_sha256":
            return False
        salt = bytes.fromhex(salt_hex)
        expected = bytes.fromhex(digest_hex)
    except (ValueError, AttributeError):
        return False
    candidate = hashlib.pbkdf2_hmac("sha256", password.encode("utf-8"), salt, int(iterations))
    return hmac.compare_digest(candidate, expected)


def _hash_token(token: str) -> str:
    return hashlib.sha256(token.encode("utf-8")).hexdigest()


def create_session(db: Session, user: User) -> str:
    token = secrets.token_urlsafe(32)
    expires_at = datetime.now(timezone.utc) + SESSION_TTL
    db.add(UserSession(user_id=user.id, token_hash=_hash_token(token), expires_at=expires_at))
    db.commit()
    return token


def revoke_session(db: Session, token: str) -> None:
    db.query(UserSession).filter(UserSession.token_hash == _hash_token(token)).delete()
    db.commit()


def get_user_from_token(db: Session, token: str) -> User | None:
    if not token:
        return None
    session_row = db.query(UserSession).filter(UserSession.token_hash == _hash_token(token)).one_or_none()
    if session_row is None:
        return None
    expires_at = session_row.expires_at
    if expires_at.tzinfo is None:
        expires_at = expires_at.replace(tzinfo=timezone.utc)
    if expires_at < datetime.now(timezone.utc):
        db.delete(session_row)
        db.commit()
        return None
    return db.get(User, session_row.user_id)


def _bearer_token(request: Request) -> str:
    header = request.headers.get("Authorization", "")
    if header.startswith("Bearer "):
        return header[len("Bearer ") :].strip()
    return ""


def enforce_api_key(request: Request) -> HTTPException | None:
    """Check authentication for a request.

    Returns an `HTTPException` describing the failure, or `None` if the request is
    authenticated. Callers must not rely on this function raising - raising an
    `HTTPException` from within a `@app.middleware("http")` function is not converted
    into a proper HTTP response by FastAPI and results in an unhandled 500 error.
    """
    path = request.url.path
    if path == "/health":
        return None
    if settings.expose_docs and (path.startswith("/docs") or path.startswith("/openapi.json") or path.startswith("/redoc")):
        return None
    if path == "/auth/login":
        return None

    keys = configured_api_keys()
    if keys:
        token = request.headers.get("X-API-Key", "")
        if token and _matches_any(token, keys):
            return None

    session_token = _bearer_token(request)
    if session_token:
        db = SessionLocal()
        try:
            user = get_user_from_token(db, session_token)
        finally:
            db.close()
        if user is not None:
            request.state.user = user
            return None

    return HTTPException(status_code=401, detail="authentication required", headers={"WWW-Authenticate": "Bearer"})
