import json
import os
from typing import Any


def resolve_secret_text(secret_ref: str | None) -> str:
    if not secret_ref:
        return ""

    ref = str(secret_ref).strip()
    if not ref:
        return ""

    if ref.startswith("env:"):
        return os.environ.get(ref[4:], "").strip()

    if ref in os.environ:
        return os.environ.get(ref, "").strip()

    return ref


def resolve_secret_mapping(secret_ref: str | None) -> dict[str, str]:
    raw = resolve_secret_text(secret_ref)
    if not raw:
        return {}

    raw = raw.strip()
    if raw.startswith("{"):
        try:
            parsed = json.loads(raw)
        except json.JSONDecodeError:
            return {}
        if isinstance(parsed, dict):
            return {
                "aws_access_key_id": str(parsed.get("aws_access_key_id") or parsed.get("access_key") or ""),
                "aws_secret_access_key": str(parsed.get("aws_secret_access_key") or parsed.get("secret_key") or ""),
                "aws_session_token": str(parsed.get("aws_session_token") or parsed.get("session_token") or ""),
            }
        return {}

    if ":" in raw:
        access, secret = raw.split(":", 1)
        return {
            "aws_access_key_id": access,
            "aws_secret_access_key": secret,
            "aws_session_token": "",
        }

    return {}
