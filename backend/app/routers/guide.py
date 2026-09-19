"""GET /guide: the full authoring guide, behind a login."""

from fastapi import APIRouter, Depends

from app.guide import load_guide
from app.models import User
from app.security import get_current_user

router = APIRouter(tags=["guide"])


@router.get("/guide")
def get_guide(user: User = Depends(get_current_user)):
    # `user` is unused on purpose: the dependency is the access check.
    return load_guide()
