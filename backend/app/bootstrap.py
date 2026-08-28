"""Create the one org and one real administrator a fresh install needs.

This replaces the demo seeder that used to run on every startup. That seeder
invented ten colleagues, thirteen projects and a generated offer letter
addressed to a person who does not exist, which meant a dashboard full of
numbers proved nothing about whether the application worked. Everything a
running instance now contains was put there by someone using it.

Deliberately not a startup hook: creating a login credential is an explicit,
audited act, and an app that silently mints an administrator on boot has no
meaningful authentication story.

    python -m app.bootstrap --org "Acme Life Sciences" \
        --email you@example.com --name "Your Name" --password '...'

The only rows this writes besides the org and the user are the lookup taxonomy
(functions, regions, languages, document types). Those are configuration, not
records -- the create-project form reads them from `GET /lookups`, so an empty
table means projects cannot be created at all. Edit LOOKUPS below to match your
own organisation; nothing downstream depends on these particular values.
"""

from __future__ import annotations

import argparse
import sys

from sqlalchemy import select

from app.db import SessionLocal
from app.models import LookupValue, Organization, User
from app.security import hash_password
from app.tenancy import release_org_scope, set_current_org

FUNCTIONS = [
    "Clinical", "Quality-CMC", "Safety", "Medical Affairs", "Marketing",
    "Quality", "Human Resources", "Legal", "Regulatory Affairs",
]

REGIONS = ["Europe", "North America", "Asia Pacific", "Latin America", "Middle East & Africa", "Global"]

LANGUAGES = ["English", "Spanish", "French", "German", "Japanese", "Chinese"]

DOCUMENT_TYPES = {
    "Human Resources": ["HR Letters", "Offer Letter", "Termination Letter", "Promotion Memo", "Policy Update"],
    "Clinical": ["Clinical Study Report", "Protocol Amendment", "Informed Consent", "Investigator Brochure"],
    "Quality-CMC": ["CMC Section", "Batch Record", "Deviation Report"],
    "Quality": ["Quality Report", "SOP", "Audit Report"],
    "Safety": ["Adverse Event Report", "PSUR", "Safety Communication"],
    "Medical Affairs": ["Medical Letter", "Publication Summary", "SRD"],
    "Marketing": ["Product Brief", "Campaign Copy", "Localized Content"],
    "Legal": ["Contract", "NDA", "Legal Memo"],
    "Regulatory Affairs": ["Regulatory Cover Letter", "Submission Package"],
}

MIN_PASSWORD_LEN = 12


def _write_lookups(db, org_id: str) -> int:
    written = 0
    for kind, values in (("function", FUNCTIONS), ("region", REGIONS), ("language", LANGUAGES)):
        for i, value in enumerate(values):
            db.add(LookupValue(org_id=org_id, kind=kind, value=value, sort_order=i))
            written += 1
    for function, doc_types in DOCUMENT_TYPES.items():
        for i, doc_type in enumerate(doc_types):
            db.add(LookupValue(org_id=org_id, kind="document_type", value=doc_type, parent_value=function, sort_order=i))
            written += 1
    return written


def bootstrap(*, org_name: str, email: str, full_name: str, password: str, region: str = "Europe") -> dict:
    """Create the org, its first administrator, and the lookup taxonomy.

    Refuses rather than overwrites: re-running this against a live instance
    must never silently reset a password or duplicate an org.
    """
    if len(password) < MIN_PASSWORD_LEN:
        raise ValueError(f"Password must be at least {MIN_PASSWORD_LEN} characters.")

    email = email.strip().lower()
    db = SessionLocal()
    try:
        if db.scalar(select(Organization).where(Organization.name == org_name)):
            raise ValueError(f"Organization {org_name!r} already exists -- refusing to modify it.")
        if db.scalar(select(User).where(User.email == email)):
            raise ValueError(f"A user with email {email!r} already exists -- refusing to modify it.")

        org = Organization(
            name=org_name,
            region_default=region,
            settings={"public_workspace": False, "require_approval_before_export": True},
        )
        db.add(org)
        db.flush()

        # §16 row-level security: `lookup_values` below is behind a policy keyed
        # on `app.current_org`, so this session has to declare the tenant it just
        # created or the inserts are silently filtered out on PostgreSQL. Inert
        # on SQLite. See `app/tenancy.py`.
        set_current_org(db, org.id)

        user = User(
            org_id=org.id, email=email, full_name=full_name,
            password_hash=hash_password(password),
            role_key="org_admin", status="active",
        )
        db.add(user)
        db.flush()

        lookup_count = _write_lookups(db, org.id)
        db.commit()
        return {"org_id": org.id, "user_id": user.id, "email": email, "lookup_values": lookup_count}
    finally:
        release_org_scope(db)
        db.close()


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="Create the first organisation and administrator.")
    parser.add_argument("--org", required=True, help="Organisation name")
    parser.add_argument("--email", required=True, help="Administrator email (this is the login)")
    parser.add_argument("--name", required=True, help="Administrator full name")
    parser.add_argument("--password", required=True, help=f"Administrator password (min {MIN_PASSWORD_LEN} chars)")
    parser.add_argument("--region", default="Europe", help="Default region for the organisation")
    args = parser.parse_args(argv)

    try:
        result = bootstrap(
            org_name=args.org, email=args.email, full_name=args.name,
            password=args.password, region=args.region,
        )
    except ValueError as exc:
        print(f"error: {exc}", file=sys.stderr)
        return 1

    print(f"Organisation : {args.org} ({result['org_id']})")
    print(f"Administrator: {result['email']} ({result['user_id']})")
    print(f"Lookup values: {result['lookup_values']}")
    print("\nNo projects, templates or documents were created. Sign in and upload your own.")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
