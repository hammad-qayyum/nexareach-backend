"""Admin command line.

python -m app.cli create-admin    Create the first admin (prompts for details)
python -m app.cli generate-key    Print a new ENCRYPTION_KEY
"""

import argparse
import getpass
import sys

from sqlalchemy import select

from app.core.crypto import generate_key
from app.core.db import SessionLocal
from app.core.security import MIN_PASSWORD_LENGTH, hash_password
from app.models import Role, User, UserStatus
from app.services.audit import SYSTEM, log_activity, snapshot
from app.services.auth import normalize_email


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


def main() -> None:
    parser = argparse.ArgumentParser(prog="python -m app.cli")
    sub = parser.add_subparsers(dest="command", required=True)
    sub.add_parser("create-admin", help="Create an admin user")
    sub.add_parser("generate-key", help="Print a new ENCRYPTION_KEY")
    args = parser.parse_args()

    if args.command == "create-admin":
        create_admin()
    elif args.command == "generate-key":
        print(generate_key())


if __name__ == "__main__":
    main()
