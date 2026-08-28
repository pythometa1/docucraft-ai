from sqlalchemy.orm import Session

from app.models import AuditLog, User


def log_audit(db: Session, user: User, event: str, entity_type: str, entity_id: str | None = None, project_id: str | None = None, severity: str = "info", target: str | None = None) -> None:
    db.add(AuditLog(
        org_id=user.org_id, actor_id=user.id, actor_name=user.full_name, event=event,
        severity=severity, entity_type=entity_type, entity_id=entity_id, project_id=project_id, target=target,
    ))
