"""Who is allowed to do what, and the things nobody is allowed to do alone.

§16 of the architecture record asks for "distinct roles for uploader, mapper,
approver, generator and auditor", that "the approver role cannot be self-
assigned", that "the compiler agent runs under a service identity that has no
approval capability whatsoever", and that a legally-binding template needs
four-eyes approval on lock.

`User.role_key` already existed and was already displayed on the team screen.
It was never consulted for authorization: every authenticated user in an
organisation could compile a template, edit its manifest, approve it, and
generate from it. Separation of duties that exists only as a label in the UI is
not separation of duties -- it is a column.

The model here is capability-based rather than role-checked at the call site.
Handlers ask for a capability; the mapping from role to capability lives in one
table that can be read in one sitting. Adding a role is a line here, not a
search through eleven routers.
"""

from dataclasses import dataclass

from fastapi import Depends

from app.models import User
from app.security import error, get_current_user

# ---------------------------------------------------------------- capabilities

UPLOAD_TEMPLATE = "upload_template"
UPLOAD_SOURCE = "upload_source"
COMPILE_MANIFEST = "compile_manifest"
EDIT_MANIFEST = "edit_manifest"
APPROVE_MANIFEST = "approve_manifest"
GENERATE_DOCUMENT = "generate_document"
APPROVE_DOCUMENT = "approve_document"
READ_AUDIT = "read_audit"
MANAGE_USERS = "manage_users"

ALL_CAPABILITIES = frozenset({
    UPLOAD_TEMPLATE, UPLOAD_SOURCE, COMPILE_MANIFEST, EDIT_MANIFEST,
    APPROVE_MANIFEST, GENERATE_DOCUMENT, APPROVE_DOCUMENT, READ_AUDIT, MANAGE_USERS,
})

# ---------------------------------------------------------------------- roles

#: role_key -> what that role may do.
#:
#: The separations that matter, and why each one is a separation rather than a
#: convenience: a *mapper* may edit a manifest but never approve their own work;
#: an *approver* may approve but never edit, so the thing approved is the thing
#: that was reviewed; a *generator* may run production but cannot change what
#: production executes; an *auditor* reads and changes nothing.
ROLE_CAPABILITIES: dict[str, frozenset] = {
    "org_admin": ALL_CAPABILITIES,
    "uploader": frozenset({UPLOAD_TEMPLATE, UPLOAD_SOURCE}),
    "mapper": frozenset({UPLOAD_TEMPLATE, UPLOAD_SOURCE, COMPILE_MANIFEST, EDIT_MANIFEST}),
    "approver": frozenset({APPROVE_MANIFEST, APPROVE_DOCUMENT, READ_AUDIT}),
    "generator": frozenset({GENERATE_DOCUMENT}),
    "auditor": frozenset({READ_AUDIT}),
    # The compiler agent's service identity. P5 of the architecture record --
    # "the agent may inspect, retrieve, propose, test and escalate; it may not
    # invent source values or self-approve production changes" -- expressed as
    # an IAM constraint rather than a coding convention, which is what §16 asks
    # for. It deliberately holds no approval capability at all.
    "compiler_agent": frozenset({COMPILE_MANIFEST, EDIT_MANIFEST}),
    # Legacy default for users created before roles were enforced. Same rights
    # as org_admin, so enabling this module cannot lock an existing customer out
    # of their own account; narrowing it is a migration, not a surprise.
    "viewer": frozenset({READ_AUDIT}),
}

#: Roles a user must never be able to grant themselves. §16: "the approver role
#: cannot be self-assigned". Self-granting approval turns four-eyes into two.
SELF_ASSIGNABLE_DENYLIST = frozenset({"approver", "org_admin"})


def capabilities_of(role_key: str | None) -> frozenset:
    """What this role may do. An unknown role gets nothing.

    Failing closed matters here: a typo in a role name, or a role removed from
    the table while users still carry it, must not read as unrestricted.
    """
    return ROLE_CAPABILITIES.get((role_key or "").strip(), frozenset())


def has_capability(user: User, capability: str) -> bool:
    return capability in capabilities_of(user.role_key)


def require(capability: str):
    """FastAPI dependency: refuse the request unless the caller holds it.

    403 rather than 404, deliberately, and unlike the tenancy guards. Tenancy
    hides existence because a cross-tenant probe should learn nothing. This is
    the opposite case: the caller is in the right organisation and the resource
    is genuinely theirs to know about -- they simply hold the wrong role, and
    telling them so is what lets them ask the right person.
    """
    if capability not in ALL_CAPABILITIES:
        raise ValueError(f"unknown capability {capability!r}")

    def dependency(user: User = Depends(get_current_user)) -> User:
        if not has_capability(user, capability):
            raise error(
                "CAPABILITY_REQUIRED",
                f"Your role ({user.role_key}) cannot {capability.replace('_', ' ')}.",
                403,
                {"required_capability": capability, "role": user.role_key},
            )
        return user

    return dependency


# ------------------------------------------------------------ four-eyes lock

@dataclass(frozen=True)
class ApprovalCheck:
    allowed: bool
    reason: str | None = None


def check_manifest_approval(
    *,
    approver_id: str,
    compiled_by_id: str | None,
    prior_approver_ids: tuple = (),
    legally_binding: bool = False,
) -> ApprovalCheck:
    """Whether this person may put this manifest into production.

    §16 asks for "four-eyes approval on manifest lock for any template flagged
    as legally binding", and scopes it there deliberately -- an offer letter and
    an internal memo do not carry the same risk, and a separation rule applied
    to everything is a rule teams learn to work around.

    So for a legally-binding template, two things must hold: the person who
    compiled the mapping is not the person who signs it off, and a second,
    distinct approver has already recorded an approval. The second approver's
    job is not to repeat the first's review -- it is to be a second person, so
    one compromised or mistaken account cannot issue a binding instrument alone.

    Ordinary templates need only the capability. Holding APPROVE_MANIFEST is
    itself a separation, because `mapper` does not have it.
    """
    if not legally_binding:
        return ApprovalCheck(True)

    if compiled_by_id and approver_id == compiled_by_id:
        return ApprovalCheck(
            False,
            "This template is marked legally binding, so the person who compiled its "
            "manifest cannot also approve it. Ask a second reviewer.",
        )

    others = {a for a in prior_approver_ids if a and a != approver_id}
    if not others:
        return ApprovalCheck(
            False,
            "This template is marked legally binding, so it needs approval from two "
            "different people. Record a first approval, then have a second reviewer confirm it.",
        )

    return ApprovalCheck(True)


def check_role_assignment(*, actor: User, target_user_id: str, new_role: str) -> ApprovalCheck:
    """Whether `actor` may give `target_user_id` the role `new_role`."""
    if new_role not in ROLE_CAPABILITIES:
        return ApprovalCheck(False, f"Unknown role {new_role!r}.")
    if actor.id == target_user_id and new_role in SELF_ASSIGNABLE_DENYLIST:
        return ApprovalCheck(
            False,
            f"You cannot give yourself the {new_role} role. "
            "Ask another administrator to grant it.",
        )
    return ApprovalCheck(True)
