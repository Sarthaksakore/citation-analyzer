"""Authentication helpers for email/password sessions."""

from __future__ import annotations

import hashlib
import os
import re
import secrets
from datetime import datetime, timedelta, timezone

import bcrypt
from dotenv import load_dotenv
from fastapi import HTTPException, Request
from sqlalchemy.orm import Session

from app.storage import DBAuthSession, DBUser

# Load configuration before evaluating the module-level security settings.  The
# API imports this module before its own startup hook runs.
load_dotenv()

EMAIL_RE = re.compile(r"^[^@\s]+@[^@\s]+\.[^@\s]+$")
SESSION_COOKIE = "citation_session"
SESSION_TTL_HOURS = max(1, int(os.getenv("SESSION_TTL_HOURS", "168")))
SESSION_TTL_SECONDS = SESSION_TTL_HOURS * 60 * 60
AUTH_COOKIE_SECURE = os.getenv("AUTH_COOKIE_SECURE", "false").lower() in {"1", "true", "yes"}


def normalize_email(email: str) -> str:
    return email.strip().lower()


def validate_email(email: str) -> str:
    normalized = normalize_email(email)
    if len(normalized) > 320 or not EMAIL_RE.fullmatch(normalized):
        raise HTTPException(status_code=422, detail="Enter a valid email address.")
    return normalized


def hash_password(password: str) -> str:
    if not password or len(password) < 8:
        raise HTTPException(status_code=422, detail="Password must be at least 8 characters.")
    if len(password.encode("utf-8")) > 72:
        raise HTTPException(status_code=422, detail="Password must be 72 bytes or fewer.")
    return bcrypt.hashpw(password.encode("utf-8"), bcrypt.gensalt()).decode("ascii")


def verify_password(password: str, password_hash: str) -> bool:
    try:
        return bcrypt.checkpw(password.encode("utf-8"), password_hash.encode("ascii"))
    except (ValueError, TypeError, UnicodeEncodeError):
        return False


def _token_digest(token: str) -> str:
    return hashlib.sha256(token.encode("ascii")).hexdigest()


def create_session(user: DBUser, db: Session) -> str:
    token = secrets.token_urlsafe(32)
    db.add(DBAuthSession(
        token_hash=_token_digest(token),
        user_id=user.id,
        expires_at=datetime.now(timezone.utc) + timedelta(seconds=SESSION_TTL_SECONDS),
    ))
    db.commit()
    return token


def _as_utc(value: datetime) -> datetime:
    """Normalize SQLite's naive timestamps and timezone-aware DB values."""
    if value.tzinfo is None:
        return value.replace(tzinfo=timezone.utc)
    return value.astimezone(timezone.utc)


def get_current_user(request: Request, db: Session) -> DBUser:
    token = request.cookies.get(SESSION_COOKIE)
    if not token:
        raise HTTPException(status_code=401, detail="Authentication required.")
    try:
        token_hash = _token_digest(token)
    except UnicodeEncodeError:
        raise HTTPException(status_code=401, detail="Session expired or invalid.")
    session = db.query(DBAuthSession).filter_by(token_hash=token_hash).first()
    now = datetime.now(timezone.utc)
    if session is None or _as_utc(session.expires_at) <= now:
        if session is not None:
            db.delete(session)
            db.commit()
        raise HTTPException(status_code=401, detail="Session expired or invalid.")
    user = db.query(DBUser).filter_by(id=session.user_id).first()
    if user is None:
        raise HTTPException(status_code=401, detail="Authentication required.")
    return user


def delete_session(request: Request, db: Session) -> None:
    token = request.cookies.get(SESSION_COOKIE)
    if not token:
        return
    try:
        token_hash = _token_digest(token)
    except UnicodeEncodeError:
        raise HTTPException(status_code=401, detail="Session expired or invalid.")
    session = db.query(DBAuthSession).filter_by(token_hash=token_hash).first()
    if session is not None:
        db.delete(session)
        db.commit()






