"""Admin command line.

python -m app.cli create-admin    Create the first admin (prompts for details)
python -m app.cli change-login    Change a user's sign-in email and/or password (prompts for details)
python -m app.cli generate-key    Print a new ENCRYPTION_KEY

Development only (refused unless APP_ENV=development and the database is on this computer):
python -m app.cli seed-dev        Fill an empty database with sample users, Mailpit mailboxes and leads
python -m app.cli reset-dev       Wipe the development database, rebuild it and seed it
"""

import argparse
import getpass
import os
import sys
from urllib.parse import urlparse

from sqlalchemy import select
from sqlalchemy.orm import Session

from app.core.config import get_settings
from app.core.crypto import encrypt, generate_key
from app.core.db import SessionLocal
from app.core.security import MIN_PASSWORD_LENGTH, hash_password
from app.models import (
    DncKind,
    DoNotContact,
    Lead,
    LeadGroup,
    Mailbox,
    MailboxStatus,
    Role,
    SmtpSecurity,
    User,
    UserStatus,
)
from app.services import geo, providers
from app.services.app_settings import FOOTER_BUSINESS_NAME, FOOTER_POSTAL_ADDRESS, set_value
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


# --- Development data ----------------------------------------------------------------------------------

DEV_PASSWORD = "devpassword123"  # noqa: S105 - fake accounts that only exist in development
DEV_USERS = [
    ("Dev Admin", "dev@example.com", Role.ADMIN),
    ("Dev Member", "member@example.com", Role.MEMBER),
    ("Dev Viewer", "viewer@example.com", Role.VIEWER),
]
DEV_MAILBOXES = [("sender1@example.com", "Dev Sender One"), ("sender2@example.com", "Dev Sender Two")]
# Real-estate sample leads. Every address is on example.com, which can never receive email.
DEV_LEADS = [
    (
        "ahmed@emaar-brokers.example.com",
        "Ahmed",
        "Al Mansoori",
        "Emaar Brokers",
        "UAE",
        {"city": "Dubai", "property_type": "luxury villas", "area": "Palm Jumeirah", "budget": "AED 5M"},
    ),
    (
        "sophia@knight-partners.example.com",
        "Sophia",
        "Clarke",
        "Knight Partners",
        "UK",
        {"city": "London", "property_type": "apartments", "area": "Canary Wharf", "budget": "GBP 900k"},
    ),
    (
        "bilal@zameen-partners.example.com",
        "Bilal",
        "Khan",
        "Zameen Partners",
        "Pakistan",
        {
            "city": "Lahore",
            "property_type": "residential plots",
            "area": "DHA Phase 6",
            "budget": "PKR 4 crore",
        },
    ),
    (
        "maria@sunset-homes.example.com",
        "Maria",
        "Lopez",
        "Sunset Homes",
        "USA",
        {"city": "Miami", "property_type": "condos", "area": "Brickell", "budget": "USD 750k"},
    ),
    (
        "omar@pearl-living.example.com",
        "Omar",
        None,
        None,
        "Qatar",
        {"city": "Doha", "property_type": "townhouses", "area": "The Pearl"},
    ),
    (
        "layla@gulf-coast.example.com",
        None,
        None,
        "Gulf Coast Realty",
        "UAE",
        {"city": "Abu Dhabi", "property_type": "penthouses", "area": "Saadiyat Island", "budget": "AED 8M"},
    ),
    (
        "zamna@skyline.example.com",
        "Zamna",
        "Razzaq",
        "Skyline Properties",
        "Pakistan",
        {"city": "Karachi", "property_type": "apartments", "area": "Clifton", "budget": "PKR 3 crore"},
    ),
    (
        "hina@bahria-estates.example.com",
        "Hina",
        "Shah",
        "Bahria Town Estates",
        "Pakistan",
        {"city": "Karachi", "property_type": "farmhouses", "area": "Bahria Town", "budget": "PKR 2 crore"},
    ),
    (
        "rashid@prime-estates.example.com",
        "Rashid",
        "Hamdan",
        "Prime Estates",
        "UAE",
        {"city": "Dubai", "property_type": "villas", "area": "Arabian Ranches", "budget": "AED 3M"},
    ),
    (
        "tom@blocked-agency.example.com",
        "Tom",
        "Reed",
        "Blocked Agency",
        "USA",
        {"city": "Austin", "property_type": "ranches", "area": "Hill Country", "budget": "USD 2M"},
    ),
]
DEV_DNC = [
    ("rashid@prime-estates.example.com", DncKind.EMAIL, "Asked to stop (sample)"),
    ("blocked-agency.example.com", DncKind.DOMAIN, "Competitor (sample)"),
]


class DevOnlyError(Exception):
    pass


def require_development() -> None:
    """Development commands only run on a development copy whose database is on this computer."""
    settings = get_settings()
    if not settings.is_development:
        raise DevOnlyError("This command only runs when APP_ENV=development (check your .env).")
    host = urlparse(settings.database_url.replace("+psycopg", "")).hostname or ""
    if host not in {"localhost", "127.0.0.1", "::1"}:
        raise DevOnlyError(f"The database is on {host}, not this computer. Refusing to touch it.")


def apply_dev_seed(db: Session) -> dict[str, int]:
    """Fill an empty development database. The caller commits."""
    require_development()
    if db.scalar(select(User.id).limit(1)) is not None:
        raise DevOnlyError("The database already has users. Use reset-dev to start again from empty.")

    users = []
    for name, email, role in DEV_USERS:
        user = User(
            name=name,
            email=email,
            password_hash=hash_password(DEV_PASSWORD),
            role=role,
            status=UserStatus.ACTIVE,
        )
        db.add(user)
        users.append(user)
    db.flush()
    admin = users[0]
    set_value(db, FOOTER_BUSINESS_NAME, "FastNexa (development)", admin.id)
    set_value(db, FOOTER_POSTAL_ADDRESS, "Office 4, 12 Main Boulevard, Lahore, Pakistan", admin.id)

    for email, name in DEV_MAILBOXES:
        db.add(
            Mailbox(
                email=email,
                display_name=name,
                smtp_host="localhost",
                smtp_port=1025,
                smtp_security=SmtpSecurity.NONE,
                smtp_username=email,
                smtp_password_encrypted=encrypt("mailpit"),
                status=MailboxStatus.ACTIVE,
                created_by_id=admin.id,
            )
        )

    found = providers.detect_many({providers.domain_of(e) for e, *_ in DEV_LEADS})
    group = LeadGroup(name="Real estate agents (sample)", source="seed-dev", created_by_id=admin.id)
    for email, first, last, company, country, custom in DEV_LEADS:
        group.leads.append(
            Lead(
                email=email,
                first_name=first,
                last_name=last,
                company=company,
                country=country,
                timezone=geo.timezone_for(None, country),
                email_provider=found.get(providers.domain_of(email)),
                custom=custom,
            )
        )
    db.add(group)
    for value, kind, reason in DEV_DNC:
        db.add(DoNotContact(value=value, kind=kind, reason=reason, created_by_id=admin.id))
    db.flush()

    summary = {
        "users": len(users),
        "mailboxes": len(DEV_MAILBOXES),
        "leads": len(DEV_LEADS),
        "do_not_contact": len(DEV_DNC),
    }
    log_activity(db, SYSTEM, "dev.seeded", entity_type="database", after=summary)
    return summary


def seed_dev() -> None:
    with SessionLocal() as db:
        try:
            apply_dev_seed(db)
        except DevOnlyError as e:
            sys.exit(str(e))
        db.commit()
    print(
        "Development data added.\n"
        f"  Sign in at {get_settings().app_base_url} as dev@example.com / {DEV_PASSWORD} (admin; set up\n"
        f"  2-step sign-in on first login), member@example.com or viewer@example.com / {DEV_PASSWORD}.\n"
        "  Two mailboxes send to Mailpit: open http://localhost:8025 to see every email."
    )


def reset_dev() -> None:
    from alembic import command
    from alembic.config import Config
    from sqlalchemy import text

    from app.core.db import engine

    try:
        require_development()
    except DevOnlyError as e:
        sys.exit(str(e))
    name = urlparse(get_settings().database_url.replace("+psycopg", "")).path.lstrip("/")
    print(f"This deletes EVERYTHING in the development database '{name}' and fills it with sample data.")
    print("Stop the worker first.")
    if input(f"Type {name} to continue: ").strip() != name:
        sys.exit("Cancelled. Nothing was changed.")

    root = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
    with engine.begin() as conn:
        conn.execute(text("DROP SCHEMA public CASCADE; CREATE SCHEMA public;"))
        cfg = Config(os.path.join(root, "alembic.ini"))
        cfg.set_main_option("script_location", os.path.join(root, "migrations"))
        cfg.attributes["connection"] = conn
        command.upgrade(cfg, "head")
    print("Database rebuilt.")
    seed_dev()


def main() -> None:
    parser = argparse.ArgumentParser(prog="python -m app.cli")
    sub = parser.add_subparsers(dest="command", required=True)
    sub.add_parser("create-admin", help="Create an admin user")
    sub.add_parser("change-login", help="Change a user's sign-in email and/or password")
    sub.add_parser("generate-key", help="Print a new ENCRYPTION_KEY")
    sub.add_parser("seed-dev", help="Development only: add sample users, Mailpit mailboxes and leads")
    sub.add_parser("reset-dev", help="Development only: wipe, rebuild and seed the development database")
    args = parser.parse_args()

    if args.command == "create-admin":
        create_admin()
    elif args.command == "change-login":
        change_login()
    elif args.command == "seed-dev":
        seed_dev()
    elif args.command == "reset-dev":
        reset_dev()
    elif args.command == "generate-key":
        print(generate_key())


if __name__ == "__main__":
    main()
