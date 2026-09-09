"""Who may make a pharmacovigilance judgment on this product.

A second dimension beside `app.authz`, not a replacement for it. The org role
answers "may this person approve documents in this tenant"; this answers "is
this person the qualified person for THIS product" -- the one who may confirm
an expectedness determination, clear the de-identification gate, or sign a
report off.

They are separate because they are separate facts. An organisation
administrator administers the tenant; a qualified person carries a named
regulatory responsibility for one product's safety. Someone can be either
without being the other, and a system that inferred the second from the first
would be assigning a regulatory role by accident.

**Nothing is implicit.** There is no fallback that makes an `org_admin` a
qualified person, and no rule that the product's creator becomes one. §2's
first principle is that these determinations are human-owned with no silent
defaults, and a role that arrives by inference is the definition of a silent
default -- it would mean the first person to click "create product" quietly
acquired the authority to confirm causality on it.

The creator is seeded as a `writer`, which is the role that does the work of
drafting, and somebody has to grant the rest.
"""

from sqlalchemy import select

from app.models import PvMember
from app.safety.registry import PV_ROLES
from app.security import error

WRITER, REVIEWER, QUALIFIED_PERSON = PV_ROLES

#: What each role is for, in the words the grant screen shows.
ROLE_LABELS = {
    WRITER: "Writer — drafts sections and enters data",
    REVIEWER: "Reviewer — reviews drafts and comments",
    QUALIFIED_PERSON: (
        "Qualified person — confirms expectedness, seriousness and causality, "
        "clears the de-identification gate, and signs reports off"
    ),
}


def _rank(pv_role: str | None) -> int:
    """Position in `PV_ROLES`, or -1 for somebody with no role at all.

    Ranked rather than set-based because these roles genuinely nest: a
    qualified person may do a writer's work, and a writer may not do theirs.
    """
    try:
        return PV_ROLES.index(pv_role or "")
    except ValueError:
        return -1


def membership(db, pv_product_id: str, user) -> PvMember | None:
    return db.scalar(select(PvMember).where(
        PvMember.pv_product_id == pv_product_id,
        PvMember.user_id == user.id,
        PvMember.org_id == user.org_id))


def role_of(db, pv_product_id: str, user) -> str | None:
    member = membership(db, pv_product_id, user)
    return member.pv_role if member else None


def has_pv_role(db, pv_product_id: str, user, minimum: str) -> bool:
    return _rank(role_of(db, pv_product_id, user)) >= _rank(minimum)


def require_pv_role(db, pv_product_id: str, user, minimum: str, *, action: str):
    """Raise unless this person holds at least `minimum` on this product.

    403 rather than 404, unlike the tenancy guards. The distinction is
    deliberate and matches `app.authz.require`: a tenancy failure must not
    confirm that a resource exists, but by the time this runs the caller has
    already been shown the product. Hiding the reason here would only leave
    somebody guessing why a button does nothing.
    """
    actual = role_of(db, pv_product_id, user)
    if _rank(actual) >= _rank(minimum):
        return actual
    raise error(
        "PV_ROLE_REQUIRED",
        f"{action} requires the {minimum.replace('_', ' ')} role on this product."
        + (f" You hold {actual.replace('_', ' ')}." if actual
           else " You hold no role on it."),
        403,
        {"required_role": minimum, "actual_role": actual,
         "pv_product_id": pv_product_id},
    )


def grant(db, *, pv_product_id: str, org_id: str, user_id: str, pv_role: str,
          granted_by: str) -> PvMember:
    """Give somebody a role, or change the one they have."""
    if pv_role not in PV_ROLES:
        raise error("PV_BAD_ROLE",
                    f"pv_role must be one of {', '.join(PV_ROLES)}.", 422)
    existing = db.scalar(select(PvMember).where(
        PvMember.pv_product_id == pv_product_id, PvMember.user_id == user_id))
    if existing is not None:
        existing.pv_role = pv_role
        existing.granted_by = granted_by
        return existing
    member = PvMember(org_id=org_id, pv_product_id=pv_product_id, user_id=user_id,
                      pv_role=pv_role, granted_by=granted_by)
    db.add(member)
    db.flush()
    return member
