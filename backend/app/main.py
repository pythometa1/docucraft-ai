from fastapi import Depends, FastAPI, Request
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import JSONResponse

from app.config import settings
from app.rate_limit import rate_limit_by_org
from app.clinical.router import router as clinical_router
from app.cmc.router import router as cmc_router
from app.csr.router import router as csr_router
from app.finance.router import router as finance_router
from app.safety.router import router as safety_router
from app.routers import admin, auth, bindings, blueprints, chat, downloads, generation, manifests, metrics, projects, review, reviews, sources, templates
from app.llm.provider import LLMNotConfiguredError

app = FastAPI(title="DocuMind AI Backend", version="1.0.0")

app.add_middleware(
    CORSMiddleware,
    allow_origins=settings.cors_origins,  # "*" in dev (settings default); lock to real origins in prod
    allow_credentials=False,
    allow_methods=["*"],
    allow_headers=["*"],
)


@app.exception_handler(LLMNotConfiguredError)
async def llm_not_configured_handler(request: Request, exc: LLMNotConfiguredError):
    """503, not 500: the server is fine, the capability is switched off.

    Registered centrally so no model-backed endpoint can accidentally degrade to
    a fabricated answer -- the only two outcomes are a real model response or an
    explicit refusal the caller can show the user.
    """
    return JSONResponse(status_code=503, content={"error": {"code": "LLM_NOT_CONFIGURED", "message": str(exc), "details": {}}})


@app.exception_handler(Exception)
async def unhandled_exception_handler(request: Request, exc: Exception):
    return JSONResponse(status_code=500, content={"error": {"code": "INTERNAL_ERROR", "message": str(exc), "details": {}}})


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


@app.on_event("startup")
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
    from app.config import PRODUCTION_ENVS
    from app.db import SessionLocal
    from app.tenancy import verify_rls_enforced

    if settings.env.strip().lower() not in PRODUCTION_ENVS:
        return
    db = SessionLocal()
    try:
        verify_rls_enforced(db)
    finally:
        db.close()


@app.get("/healthz")
def healthz():
    return {"status": "ok"}


@app.get("/readyz")
def readyz():
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
    ok = all(checks.values())
    return JSONResponse(status_code=200 if ok else 503, content={"status": "ok" if ok else "degraded", "checks": checks})


# Nothing is seeded on startup. A fresh database is genuinely empty until
# `python -m app.bootstrap` creates the first org and administrator, so every
# row a running instance holds was put there by someone using the application.
