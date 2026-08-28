"""Redeeming a download grant.

Its own router because it is the one endpoint in the API that is deliberately
unauthenticated, and the shared router dependency is a per-organisation rate
limiter that requires a signed-in user. Mounting it here lets it carry a per-IP
limiter instead -- the right shape for a public endpoint -- rather than
weakening the limiter every other route depends on.

The grant *is* the authorisation: minted by an authenticated request for one
document, one organisation and one user, valid for two minutes, burned on first
use, and re-checked against the document's tenant on the way in. See
`app.downloads` for why it is stored rather than signed statelessly.
"""

from fastapi import APIRouter, Depends, Request
from fastapi.responses import FileResponse
from sqlalchemy.orm import Session

from app.audit.service import log_audit
from app.db import get_db
from app.downloads import redeem
from app.models import DocumentVersion, GeneratedDocument, Project, User
from app.rate_limit import rate_limit_by_ip
from app.security import error
from app.tenancy import set_current_org
from app.storage import abs_path

router = APIRouter(tags=["downloads"])

DOCX_MEDIA_TYPE = "application/vnd.openxmlformats-officedocument.wordprocessingml.document"


@router.get("/downloads/{token}")
def redeem_download(token: str, request: Request, db: Session = Depends(get_db)):
    """Serve a document against a grant, once."""
    rate_limit_by_ip(request)
    grant = redeem(token)

    # Every other route gets its tenant scope from `get_current_user`. This one
    # has no current user by design -- the grant is the authorisation -- so
    # without this line the session reaches PostgreSQL with `app.current_org`
    # unset, every row-level security policy evaluates against no tenant, and
    # the SELECTs below return nothing. The download would 404 for every valid
    # grant, in production only, because the suite runs SQLite where RLS is a
    # no-op. The grant names the org it was minted for, and the check below
    # still confirms the document agrees.
    set_current_org(db, grant.org_id)

    dv = db.get(DocumentVersion, grant.version_id)
    if dv is None or not dv.blob_path:
        raise error("VERSION_NOT_FOUND", "Document version not found", 404)
    gd = db.get(GeneratedDocument, dv.document_id)
    # The grant records the tenant it was issued for; a document that has since
    # moved organisation is not the document the grant was for.
    if gd is None or gd.org_id != grant.org_id:
        raise error("VERSION_NOT_FOUND", "Document version not found", 404)

    # Minting and fetching are separate events: a link requested and never
    # followed is a different fact from a document that actually left.
    user = db.get(User, grant.user_id)
    if user is not None:
        log_audit(db, user, "Downloaded a document", "document_version", dv.id, gd.project_id, "success")
        db.commit()

    project = db.get(Project, gd.project_id)
    filename = f"{project.name}_{project.display_id}_{gd.display_id}_{gd.language}.docx"
    return FileResponse(str(abs_path(dv.blob_path)), filename=filename, media_type=DOCX_MEDIA_TYPE)
