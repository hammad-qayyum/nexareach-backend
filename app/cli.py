"""Admin command line.

python -m app.cli create-admin    Create the first admin (prompts for details)
python -m app.cli change-login    Change a user's sign-in email and/or password (prompts for details)
python -m app.cli generate-key    Print a new ENCRYPTION_KEY
"""

import argparse
import getpass
import sys

from sqlalchemy import select
from sqlalchemy.orm import Session

from app.core.crypto import generate_key
from app.core.db import SessionLocal
from app.core.security import MIN_PASSWORD_LENGTH, hash_password
from app.models import Role, User, UserStatus
from app.services.audit import SYSTEM, log_activity, snapshot
from app.services.auth import normalize_email, revoke_user_sessions


def create_admin() -> None:
    name = input("Name: ").strip()
    email = normalize_email(input("Email: "))
    password = getpass.getpass(f"Password (min {MIN_PASSWORD_LENGTH} characters): ")
    if not name or "@" not in email:
        sys.exit("Name and a valid email are required.")
    if len(password) < MIN_PASSWORD_LENGTH:
        sys.exit(f"Password must be at least {MIN_PASSWORD_LENGTH} characters.")
    if getpass.getpass("Repeat password: ") != password:
        sys.exit("Passwords don't match.")

    with SessionLocal() as db:
        if db.scalar(select(User.id).where(User.email == email)) is not None:
            sys.exit(f"A user with email {email} already exists.")
        user = User(
            name=name,
            email=email,
            password_hash=hash_password(password),
            role=Role.ADMIN,
            status=UserStatus.ACTIVE,
        )
        db.add(user)
        db.flush()
        log_activity(
            db,
            SYSTEM,
            "user.created_by_cli",
            entity_type="user",
            entity_id=user.id,
            after=snapshot(user, ["name", "email", "role", "status"]),
        )
        db.commit()
    print(f"Admin {email} created. They'll set up 2-step login at first sign-in.")


class LoginChangeError(Exception):
    pass


def apply_login_change(
    db: Session, current_email: str, new_email: str | None, new_password: str | None
) -> User:
    """Change a user's sign-in email and/or password, sign them out everywhere and log it. The caller commits.

    2-step sign-in is kept: the same authenticator app keeps working.
    """
    user = db.scalar(select(User).where(User.email == normalize_email(current_email)))
    if user is None:
        raise LoginChangeError(f"No user with email {normalize_email(current_email)}.")
    email = normalize_email(new_email) if new_email and new_email.strip() else user.email
    if "@" not in email:
        raise LoginChangeError("That isn't a valid email.")
    if email != user.email and db.scalar(select(User.id).where(User.email == email)) is not None:
        raise LoginChangeError(f"A user with email {email} already exists.")
    if new_password is not None and len(new_password) < MIN_PASSWORD_LENGTH:
        raise LoginChangeError(f"Password must be at least {MIN_PASSWORD_LENGTH} characters.")

    before = snapshot(user, ["email"])
    user.email = email
    if new_password is not None:
        user.password_hash = hash_password(new_password)
    revoke_user_sessions(db, user.id)
    log_activity(
        db,
        SYSTEM,
        "user.login_changed_by_cli",
        entity_type="user",
        entity_id=user.id,
        before=before,
        after={"email": email, "password_changed": new_password is not None},
    )
    return user


def change_login() -> None:
    current = input("Current email of the user: ")
    new_email = input("New email (leave empty to keep it): ")
    password = getpass.getpass(
        f"New password (min {MIN_PASSWORD_LENGTH} characters, leave empty to keep it): "
    )
    if password and getpass.getpass("Repeat new password: ") != password:
        sys.exit("Passwords don't match.")
    with SessionLocal() as db:
        try:
            user = apply_login_change(db, current, new_email, password or None)
        except LoginChangeError as e:
            sys.exit(str(e))
        db.commit()
        print(f"Saved. {user.name} now signs in as {user.email}; they were signed out everywhere.")


def main() -> None:
    parser = argparse.ArgumentParser(prog="python -m app.cli")
    sub = parser.add_subparsers(dest="command", required=True)
    sub.add_parser("create-admin", help="Create an admin user")
    sub.add_parser("change-login", help="Change a user's sign-in email and/or password")
    sub.add_parser("generate-key", help="Print a new ENCRYPTION_KEY")
    args = parser.parse_args()

    if args.command == "create-admin":
        create_admin()
    elif args.command == "change-login":
        change_login()
    elif args.command == "generate-key":
        print(generate_key())


if __name__ == "__main__":
    main()
