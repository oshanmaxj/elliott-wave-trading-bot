from fastapi import APIRouter, Depends, HTTPException, Request, Response
from pydantic import BaseModel, Field
from sqlalchemy import select
from sqlalchemy.orm import Session
from app.auth import (
    CSRF_COOKIE,
    SESSION_COOKIE,
    current_user,
    new_session,
    token_hash,
    verify_password,
)
from app.core.config import get_settings
from app.database.session import get_db
from app.models import AuthSession, User

router = APIRouter(prefix="/api/auth", tags=["authentication"])
settings = get_settings()


class LoginRequest(BaseModel):
    username: str = Field(min_length=1, max_length=254)
    password: str = Field(min_length=1, max_length=1024)


def public(user):
    return {"id": user.id, "username": user.username, "role": user.role}


@router.post("/login")
def login(body: LoginRequest, response: Response, db: Session = Depends(get_db)):
    user = db.scalar(select(User).where(User.username == body.username.strip().lower()))
    if (
        not user
        or not user.is_active
        or not verify_password(body.password, user.password_hash)
    ):
        raise HTTPException(401, "Invalid username or password")
    raw, csrf, expires = new_session(db, user)
    max_age = settings.auth_session_hours * 3600
    response.set_cookie(
        SESSION_COOKIE,
        raw,
        max_age=max_age,
        httponly=True,
        secure=settings.auth_cookie_secure,
        samesite="strict",
        path="/",
    )
    response.set_cookie(
        CSRF_COOKIE,
        csrf,
        max_age=max_age,
        httponly=False,
        secure=settings.auth_cookie_secure,
        samesite="strict",
        path="/",
    )
    return {"user": public(user), "expires_at": expires}


@router.get("/me")
def me(user: User = Depends(current_user)):
    return public(user)


@router.post("/logout")
def logout(
    request: Request,
    response: Response,
    db: Session = Depends(get_db),
    user: User = Depends(current_user),
):
    raw = request.cookies.get(SESSION_COOKIE)
    row = (
        db.scalar(select(AuthSession).where(AuthSession.token_hash == token_hash(raw)))
        if raw
        else None
    )
    if row:
        db.delete(row)
        db.commit()
    response.delete_cookie(
        SESSION_COOKIE, path="/", secure=settings.auth_cookie_secure, samesite="strict"
    )
    response.delete_cookie(
        CSRF_COOKIE, path="/", secure=settings.auth_cookie_secure, samesite="strict"
    )
    return {"logged_out": True}
