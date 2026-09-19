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

Colleagues are added to an existing organisation with `add-user`:

    python -m app.bootstrap add-user --org "Acme Life Sciences" \
        --email colleague@example.com --name "Their Name" \
        --role approver --password '...'

That second command exists because the separation-of-duties rules make a
one-person organisation unable to finish its own work. `check_manifest_approval`
will not let the compiler sign off its own manifest and
`check_document_review_resolution` will not let an author close the review of
their own letter -- both correct, and both unsatisfiable until there is somebody
else. There was no way to create that somebody: `bootstrap` refuses an org that
already exists, and the Team screen is read-only by design.

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

from app.authz import ROLE_CAPABILITIES
from app.db import SessionLocal
from app.models import LookupValue, Organization, User
from app.security import hash_password
from app.tenancy import release_org_scope, set_current_org

FUNCTIONS = [
    "Clinical", "Quality-CMC", "Safety", "Medical Affairs", "Marketing",
    "Quality", "Human Resources", "Legal", "Regulatory Affairs", "Finance", "Other",
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
    # The invoice service's vertical. Existing installs get these rows from the
    # a9d4f7e21c85 migration's backfill; this seed covers fresh databases.
    "Finance": ["Invoice", "Quotation", "Purchase Order"],
    # Any document the functions above do not cover; it runs the HR flow.
    "Other": ["General Document", "Letter", "Certificate", "Agreement", "Form", "Notice", "Report"],
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


def add_user(*, org_name: str, email: str, full_name: str, password: str,
             role_key: str, job_title: str | None = None) -> dict:
    """Add a colleague to an organisation that already exists.

    Refuses rather than overwrites, exactly as `bootstrap` does: re-running this
    must never quietly reset somebody's password.

    The role is checked against `ROLE_CAPABILITIES` rather than free text. A
    typo'd role is not a smaller mistake here than elsewhere -- `capabilities_of`
    fails closed, so `--role approvor` would create an account that silently
    cannot do anything and gives no hint why.
    """
    if len(password) < MIN_PASSWORD_LEN:
        raise ValueError(f"Password must be at least {MIN_PASSWORD_LEN} characters.")
    if role_key not in ROLE_CAPABILITIES:
        known = ", ".join(sorted(ROLE_CAPABILITIES))
        raise ValueError(f"Unknown role {role_key!r}. Known roles: {known}.")

    email = email.strip().lower()
    db = SessionLocal()
    try:
        org = db.scalar(select(Organization).where(Organization.name == org_name))
        if org is None:
            raise ValueError(
                f"Organization {org_name!r} does not exist. Create it with the default command "
                "first."
            )
        if db.scalar(select(User).where(User.email == email)):
            raise ValueError(f"A user with email {email!r} already exists -- refusing to modify it.")

        set_current_org(db, org.id)
        user = User(
            org_id=org.id, email=email, full_name=full_name,
            password_hash=hash_password(password),
            role_key=role_key, status="active", job_title=job_title,
        )
        db.add(user)
        db.commit()
        return {
            "org_id": org.id, "user_id": user.id, "email": email, "role": role_key,
            "capabilities": sorted(ROLE_CAPABILITIES[role_key]),
        }
    finally:
        release_org_scope(db)
        db.close()


def main(argv: list[str] | None = None) -> int:
    if argv is None:
        argv = sys.argv[1:]
    if argv and argv[0] == "add-user":
        return _add_user_main(argv[1:])

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


def _add_user_main(argv: list[str]) -> int:
    parser = argparse.ArgumentParser(
        prog="python -m app.bootstrap add-user",
        description="Add a colleague to an organisation that already exists.",
    )
    parser.add_argument("--org", required=True, help="Existing organisation name")
    parser.add_argument("--email", required=True, help="Their email (this is their login)")
    parser.add_argument("--name", required=True, help="Their full name")
    parser.add_argument("--password", required=True, help=f"Their password (min {MIN_PASSWORD_LEN} chars)")
    parser.add_argument(
        "--role", default="approver",
        help="Role key: " + ", ".join(sorted(ROLE_CAPABILITIES)),
    )
    parser.add_argument("--job-title", default=None)
    args = parser.parse_args(argv)

    try:
        result = add_user(
            org_name=args.org, email=args.email, full_name=args.name,
            password=args.password, role_key=args.role, job_title=args.job_title,
        )
    except ValueError as exc:
        print(f"error: {exc}", file=sys.stderr)
        return 1

    print(f"User        : {result['email']} ({result['user_id']})")
    print(f"Role        : {result['role']}")
    print(f"Can         : {', '.join(result['capabilities'])}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
