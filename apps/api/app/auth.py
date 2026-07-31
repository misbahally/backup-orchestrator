import secrets
from collections.abc import Iterable

from fastapi import HTTPException, Request

from .config import settings


def configured_api_keys() -> list[str]:
    return [k.strip() for k in settings.api_keys.split(",") if k.strip()]


def _matches_any(token: str, keys: Iterable[str]) -> bool:
    return any(secrets.compare_digest(token, key) for key in keys)


def enforce_api_key(request: Request) -> None:
    path = request.url.path
    if path == "/health":
        return
    if settings.expose_docs and (path.startswith("/docs") or path.startswith("/openapi.json") or path.startswith("/redoc")):
        return

    keys = configured_api_keys()
    if not keys:
        return

    token = request.headers.get("X-API-Key", "")
    if not token or not _matches_any(token, keys):
        raise HTTPException(status_code=401, detail="invalid or missing api key", headers={"WWW-Authenticate": "X-API-Key"})
