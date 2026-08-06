"""Create or reset the first WaveScope administrator from environment variables."""

import os
from getpass import getpass
from sqlalchemy import select
from app.auth import hash_password
from app.database.session import SessionLocal
from app.models import AuthSession, User


def main():
    username = (
        (
            os.getenv("WAVESCOPE_ADMIN_USERNAME")
            or input("Administrator username/email: ")
        )
        .strip()
        .lower()
    )
    password = os.getenv("WAVESCOPE_ADMIN_PASSWORD") or getpass(
        "Administrator password: "
    )
    if not username or not password:
        raise SystemExit("Username and password are required")
    try:
        password_hash = hash_password(password)
    except ValueError as exc:
        raise SystemExit(str(exc))
    with SessionLocal.begin() as db:
        user = db.scalar(select(User).where(User.username == username))
        if user:
            user.password_hash = password_hash
            user.role = "admin"
            user.is_active = True
            for session in db.scalars(
                select(AuthSession).where(AuthSession.user_id == user.id)
            ):
                db.delete(session)
            action = "reset"
        else:
            db.add(
                User(
                    username=username,
                    password_hash=password_hash,
                    role="admin",
                    is_active=True,
                )
            )
            action = "created"
    print(f"Administrator {username} {action}. Existing sessions were revoked.")


if __name__ == "__main__":
    main()
