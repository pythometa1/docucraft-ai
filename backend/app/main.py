import logging
import uuid

from fastapi import Depends, FastAPI, Request
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import JSONResponse

from app.config import PRODUCTION_ENVS, settings
from app.rate_limit import rate_limit_by_org
from app.clinical.router import router as clinical_router
from app.cmc.router import router as cmc_router
from app.csr.router import router as csr_router
from app.finance.router import router as finance_router
from app.safety.router import router as safety_router
from app.routers import admin, auth, bindings, blueprints, chat, downloads, generation, guide, manifests, metrics, projects, review, reviews, sources, templates
from app.docgen.markers import DraftingFailed
from app.llm.provider import LLM_NOT_CONFIGURED_MESSAGE, LLMNotConfiguredError

log = logging.getLogger(__name__)


def _is_production(env: str) -> bool:
    return env.strip().lower() in PRODUCTION_ENVS


async def llm_not_configured_handler(request: Request, exc: LLMNotConfiguredError):
    """503, not 500: the server is fine, the capability is switched off.

    Registered centrally so no model-backed endpoint can accidentally degrade to
    a fabricated answer -- the only two outcomes are a real model response or an
    explicit refusal the caller can show the user. The reason (which key, which
    vendor) is for the operator's log, not for the person who clicked.
    """
    log.warning("AI capability refused: %s", exc)
    return JSONResponse(status_code=503, content={"error": {
        "code": "LLM_NOT_CONFIGURED", "message": LLM_NOT_CONFIGURED_MESSAGE, "details": {}}})


async def drafting_failed_handler(request: Request, exc: DraftingFailed):
    """502, not 500: the model refused or returned nothing, and the server is
    fine. Registered centrally for the same reason as the handler above -- every
    drafting module (CSR, CMC, Safety) raises this, and each one answering its
    own way is how one of them ends up reporting a model refusal as a crash.

    The message is passed through because every `DraftingFailed` is raised with
    a sentence written for the user; the provider's own error is logged where it
    is raised, never put into the exception."""
    return JSONResponse(status_code=502, content={"detail": {"error": {
        "code": "DRAFTING_FAILED", "message": str(exc), "details": {}}}})


async def unhandled_exception_handler(request: Request, exc: Exception):
    """An exception nobody anticipated. Its text is a stack detail -- a path, a
    SQL statement, a library's complaint -- so it goes to the log under a
    reference, and the client gets the reference to quote to support."""
    ref = uuid.uuid4().hex
    log.error("Unhandled error ref=%s on %s %s", ref, request.method, request.url.path, exc_info=exc)
    return JSONResponse(status_code=500, content={"error": {
        "code": "INTERNAL_ERROR",
        "message": ("Something went wrong on our side. If it keeps happening, contact support "
                    f"with reference {ref}."),
        "details": {"ref": ref}}})


def create_app(env: str | None = None) -> FastAPI:
    """Build the application for `env` (default: the configured one).

    A factory so a test can build the production shape without re-importing
    the settings under a different environment.
    """
    production = _is_production(settings.env if env is None else env)
    # The schema and the interactive docs list every route, parameter and error
    # code this API has. Useful in development; in production it is a map of
    # the product for anybody who asks, so it is not served.
    docs = {"docs_url": None, "redoc_url": None, "openapi_url": None} if production else {}
    app = FastAPI(title="DocuMind AI Backend", version="1.0.0", **docs)
    # Read by `require_internal_endpoints`, so a test can build the production
    # shape without changing the process-wide settings.
    app.state.env = settings.env if env is None else env

    app.add_middleware(
        CORSMiddleware,
        allow_origins=settings.cors_origins,  # "*" in dev (settings default); lock to real origins in prod
        allow_credentials=False,
        allow_methods=["*"],
        allow_headers=["*"],
    )
    app.add_exception_handler(LLMNotConfiguredError, llm_not_configured_handler)
    app.add_exception_handler(DraftingFailed, drafting_failed_handler)
    app.add_exception_handler(Exception, unhandled_exception_handler)

    # Schema is owned by Alembic migrations now (backend/alembic/), not create_all() --
    # run `alembic upgrade head` before starting the app (see README / §21).
    _rate_limited = [Depends(rate_limit_by_org)]

    app.include_router(auth.router, prefix="/api/v1")  # login/logout have their own IP-based limiter
    app.include_router(downloads.router, prefix="/api/v1")  # grant-authorised, so IP-limited rather than org-limited
    app.include_router(projects.router, prefix="/api/v1", dependencies=_rate_limited)
    app.include_router(templates.router, prefix="/api/v1", dependencies=_rate_limited)
    app.include_router(sources.router, prefix="/api/v1", dependencies=_rate_limited)
    app.include_router(generation.router, prefix="/api/v1", dependencies=_rate_limited)
    app.include_router(admin.router, prefix="/api/v1", dependencies=_rate_limited)
    app.include_router(chat.router, prefix="/api/v1", dependencies=_rate_limited)
    app.include_router(manifests.router, prefix="/api/v1", dependencies=_rate_limited)
    app.include_router(blueprints.router, prefix="/api/v1", dependencies=_rate_limited)
    app.include_router(bindings.router, prefix="/api/v1", dependencies=_rate_limited)
    app.include_router(finance_router, prefix="/api/v1", dependencies=_rate_limited)  # the invoice service: client book, numbering, registry
    app.include_router(clinical_router, prefix="/api/v1", dependencies=_rate_limited)  # the clinical service: study book, numbering, registry
    app.include_router(csr_router, prefix="/api/v1", dependencies=_rate_limited)  # the CSR module: ICH E3 drafting for medical writers
    app.include_router(cmc_router, prefix="/api/v1", dependencies=_rate_limited)  # the Quality/CMC module: dossier sections and verified quality data
    app.include_router(safety_router, prefix="/api/v1", dependencies=_rate_limited)  # the Safety/PV module: reporting intervals, the case store and its locks
    app.include_router(review.router, prefix="/api/v1", dependencies=_rate_limited)
    app.include_router(reviews.router, prefix="/api/v1", dependencies=_rate_limited)  # document reviews: a person objecting, as opposed to the engine asking
    app.include_router(metrics.router, prefix="/api/v1", dependencies=_rate_limited)  # §22 metrics and §18 SLOs, READ_AUDIT-gated
    app.include_router(guide.router, prefix="/api/v1", dependencies=_rate_limited)  # the authoring guide, login-only so it stays out of the public bundle

    app.add_event_handler("startup", _verify_tenant_isolation)
    app.add_api_route("/healthz", healthz, methods=["GET"])

    def readyz():
        ok, checks = _readiness()
        content = {"status": "ok" if ok else "unavailable"}
        # Which dependency is down is for us, not for whoever is probing the
        # public endpoint; the orchestrator only reads the status code.
        if not production:
            content["checks"] = checks
        return JSONResponse(status_code=200 if ok else 503, content=content)

    app.add_api_route("/readyz", readyz, methods=["GET"])
    return app


def _verify_tenant_isolation() -> None:
    """Refuse to serve production traffic if RLS does not actually apply.

    §16 asks for row-level security as the backstop "so a forgotten WHERE clause
    fails closed instead of leaking". A role with SUPERUSER or BYPASSRLS reads
    through every policy, and nothing about the deployment looks wrong when it
    does: the policies exist, FORCE is on, `pg_policies` lists them, and the
    tables simply do not filter. Verified once at startup, against the real
    connection, because it is not a property of the schema -- it is a property
    of who this process connects as, and no migration or test can settle it.
    """
    from app.db import SessionLocal
    from app.tenancy import verify_rls_enforced

    if not _is_production(settings.env):
        return
    db = SessionLocal()
    try:
        verify_rls_enforced(db)
    finally:
        db.close()


def healthz():
    return {"status": "ok"}


def _readiness() -> tuple[bool, dict]:
    from sqlalchemy import text

    from app.db import SessionLocal
    from app.redis_client import redis_client

    checks = {"database": False, "redis": False}
    try:
        db = SessionLocal()
        db.execute(text("SELECT 1"))
        db.close()
        checks["database"] = True
    except Exception:
        pass
    try:
        checks["redis"] = redis_client.ping()
    except Exception:
        pass
    return all(checks.values()), checks


app = create_app()


# Nothing is seeded on startup. A fresh database is genuinely empty until
# `python -m app.bootstrap` creates the first org and administrator, so every
# row a running instance holds was put there by someone using the application.
