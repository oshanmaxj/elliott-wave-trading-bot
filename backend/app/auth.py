import base64
import hashlib
import hmac
import os
import secrets
from datetime import datetime, timedelta, timezone
from fastapi import Depends, HTTPException, Request
from sqlalchemy import select
from sqlalchemy.orm import Session
from app.core.config import get_settings
from app.database.session import get_db
from app.models import AuthSession, User

SESSION_COOKIE = "wavescope_session"
CSRF_COOKIE = "wavescope_csrf"


def hash_password(password: str) -> str:
    if len(password) < 12:
        raise ValueError("Password must contain at least 12 characters")
    salt = os.urandom(16)
    n, r, p = 16384, 8, 1
    derived = hashlib.scrypt(password.encode(), salt=salt, n=n, r=r, p=p, dklen=32)
    return f"scrypt${n}${r}${p}${base64.urlsafe_b64encode(salt).decode()}${base64.urlsafe_b64encode(derived).decode()}"


def verify_password(password: str, encoded: str) -> bool:
    try:
        kind, n, r, p, salt, wanted = encoded.split("$")
        if kind != "scrypt":
            return False
        actual = hashlib.scrypt(
            password.encode(),
            salt=base64.urlsafe_b64decode(salt),
            n=int(n),
            r=int(r),
            p=int(p),
            dklen=32,
        )
        return hmac.compare_digest(actual, base64.urlsafe_b64decode(wanted))
    except (ValueError, TypeError):
        return False


def token_hash(token: str) -> str:
    return hashlib.sha256(token.encode()).hexdigest()


def current_user(request: Request, db: Session = Depends(get_db)) -> User:
    raw = request.cookies.get(SESSION_COOKIE)
    if not raw:
        raise HTTPException(401, "Authentication required")
    session = db.scalar(
        select(AuthSession).where(AuthSession.token_hash == token_hash(raw))
    )
    now = datetime.now(timezone.utc)
    if not session:
        raise HTTPException(401, "Session is invalid")
    expires = (
        session.expires_at
        if session.expires_at.tzinfo
        else session.expires_at.replace(tzinfo=timezone.utc)
    )
    if expires <= now:
        db.delete(session)
        db.commit()
        raise HTTPException(401, "Session has expired")
    user = db.get(User, session.user_id)
    if not user or not user.is_active:
        raise HTTPException(401, "Account is disabled")
    if request.method not in {"GET", "HEAD", "OPTIONS"}:
        supplied = request.headers.get("X-CSRF-Token", "")
        cookie = request.cookies.get(CSRF_COOKIE, "")
        if (
            not supplied
            or not cookie
            or not hmac.compare_digest(supplied, cookie)
            or not hmac.compare_digest(supplied, session.csrf_token)
        ):
            raise HTTPException(403, "CSRF validation failed")
    session.last_seen_at = now
    db.commit()
    return user


def require_roles(*roles):
    def check(user: User = Depends(current_user)):
        if user.role not in roles:
            raise HTTPException(403, "Your account cannot perform this action")
        return user.username

    return check


def new_session(db: Session, user: User):
    settings = get_settings()
    raw = secrets.token_urlsafe(48)
    csrf = secrets.token_urlsafe(32)
    row = AuthSession(
        user_id=user.id,
        token_hash=token_hash(raw),
        csrf_token=csrf,
        expires_at=datetime.now(timezone.utc)
        + timedelta(hours=settings.auth_session_hours),
    )
    db.add(row)
    user.last_login_at = datetime.now(timezone.utc)
    db.commit()
    return raw, csrf, row.expires_at
