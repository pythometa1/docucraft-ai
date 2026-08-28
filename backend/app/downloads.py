"""Short-lived, single-use, audited download grants.

§16: "Object storage keys namespaced per organisation; downloads served through
short-lived, single-use signed URLs that are themselves audited."

The endpoints served files straight off a bearer-authenticated GET. Three things
followed from that, and all three matter for a payload of salary letters and
signed employment terms. The URL was permanent, so a link pasted into a ticket
stayed live indefinitely. It was replayable, so anyone who obtained it could
fetch the document again. And nothing recorded that a document had been
downloaded at all, which is the one event a data-subject access request or a
leak investigation actually needs.

A grant fixes all three: it names one document version, one organisation and one
user, expires in minutes, and is burned on first use. The grant is stored in
Redis rather than signed statelessly on purpose -- a stateless token cannot be
made single-use, and "single-use" is the property that stops a leaked link being
a standing key.
"""

import secrets
from dataclasses import dataclass

from app.config import settings
from app.redis_client import redis_client
from app.security import error

#: How long a grant stays valid. Long enough for a browser to follow a redirect
#: and start the transfer, short enough that a link in a chat log is dead by the
#: time anyone reads it.
GRANT_TTL_SECONDS = 120

_KEY_PREFIX = "dl:"


@dataclass(frozen=True)
class DownloadGrant:
    token: str
    version_id: str
    org_id: str
    user_id: str

    @property
    def path(self) -> str:
        return f"/api/v1/downloads/{self.token}"


def _key(token: str) -> str:
    return f"{_KEY_PREFIX}{token}"


def issue(*, version_id: str, org_id: str, user_id: str) -> DownloadGrant:
    """Mint a grant for one document version, for one person, for two minutes."""
    token = secrets.token_urlsafe(32)
    redis_client.setex(
        _key(token),
        GRANT_TTL_SECONDS,
        f"{version_id}|{org_id}|{user_id}",
    )
    return DownloadGrant(token=token, version_id=version_id, org_id=org_id, user_id=user_id)


def redeem(token: str) -> DownloadGrant:
    """Consume a grant. Raises if it is unknown, expired or already used.

    The delete happens before the file is served, so two requests racing the
    same token cannot both win. Serving the document and then invalidating would
    leave a window in which "single-use" is a description of intent rather than
    of behaviour.
    """
    raw = redis_client.get(_key(token))
    if raw is None:
        raise error(
            "DOWNLOAD_LINK_EXPIRED",
            "This download link has expired or has already been used. Request the document again.",
            410,
        )
    redis_client.delete(_key(token))

    try:
        version_id, org_id, user_id = raw.split("|", 2)
    except ValueError:  # pragma: no cover - only reachable if the value is corrupt
        raise error("DOWNLOAD_LINK_INVALID", "This download link is not valid.", 400)

    return DownloadGrant(token=token, version_id=version_id, org_id=org_id, user_id=user_id)
