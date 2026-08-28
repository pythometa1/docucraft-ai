import shutil
import uuid
from pathlib import Path

from fastapi import UploadFile

from app.config import settings


def save_upload(upload: UploadFile, subdir: str) -> tuple[str, int]:
    """Persist an uploaded file to local disk. Returns (relative_path, size_bytes)."""
    target_dir = settings.storage_dir / subdir
    target_dir.mkdir(parents=True, exist_ok=True)
    ext = Path(upload.filename or "file").suffix
    rel_path = f"{subdir}/{uuid.uuid4()}{ext}"
    abs_path = settings.storage_dir / rel_path
    with abs_path.open("wb") as f:
        shutil.copyfileobj(upload.file, f)
    return rel_path, abs_path.stat().st_size


def save_bytes(data: bytes, subdir: str, ext: str) -> str:
    target_dir = settings.storage_dir / subdir
    target_dir.mkdir(parents=True, exist_ok=True)
    rel_path = f"{subdir}/{uuid.uuid4()}{ext}"
    abs_path = settings.storage_dir / rel_path
    abs_path.write_bytes(data)
    return rel_path


def abs_path(rel_path: str) -> Path:
    return settings.storage_dir / rel_path
