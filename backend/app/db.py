from sqlalchemy import create_engine, event
from sqlalchemy.orm import DeclarativeBase, sessionmaker

from app.config import settings
from app.tenancy import apply_scope_to_session, release_org_scope

def _connect_args() -> dict:
    """Driver options that make the two backends agree about time.

    Every `DateTime` column in this schema is TIMESTAMP WITHOUT TIME ZONE, and
    the application writes timezone-aware UTC into them. PostgreSQL converts an
    aware value to the *session's* timezone before stripping the offset, so a
    server running in Asia/Kolkata stores 09:30 UTC as 15:00 and hands it back
    as 15:00, which the application then reads as UTC. Every timestamp drifts by
    the server's offset: approval times, audit entries, retention schedules,
    token expiry.

    Nothing about it looks wrong -- the values are plausible, self-consistent,
    and out by five and a half hours. SQLite has no session timezone and stores
    what it is given, so the whole class of defect is invisible until the code
    runs on the database it ships on.

    Pinning the session to UTC makes the conversion an identity operation, and
    makes a deployment's behaviour independent of where its server happens to
    think it is.
    """
    if settings.database_url.startswith("sqlite"):
        return {"check_same_thread": False}
    if settings.database_url.startswith("postgresql"):
        return {"options": "-c timezone=UTC"}
    return {}


engine = create_engine(settings.database_url, connect_args=_connect_args())
SessionLocal = sessionmaker(bind=engine, autoflush=False, autocommit=False)


# Re-assert the request's tenant at the start of every transaction.
#
# `set_current_org` sets a PostgreSQL session variable, which lives on the
# *connection*. A Session does not keep one connection: `commit()` returns it to
# the pool, and the next statement checks out whichever is free. So a handler
# that creates a row, commits, then refreshes it was reading through a
# connection that had never declared a tenant -- row-level security hid the row
# it had just written, and the create looked like it had failed.
#
# Hooking `after_begin` puts the scope wherever the session actually is, every
# time, which is the only place it is reliably correct.
@event.listens_for(SessionLocal, "after_begin")
def _reapply_tenant_scope(session, _transaction, connection):
    apply_scope_to_session(session, connection)


class Base(DeclarativeBase):
    pass


def get_db():
    """One session per request, scoped to one tenant for its whole life.

    The scope itself is set by `security.get_current_user`, which is the first
    point at which the tenant is known. Releasing it here rather than there is
    deliberate: the release has to happen on every exit path including the ones
    that never reached a handler, and a connection that goes back to the pool
    still carrying `app.current_org` hands the next request the previous
    request's tenant. See `app/tenancy.py`.
    """
    db = SessionLocal()
    try:
        yield db
    finally:
        release_org_scope(db)
        db.close()
