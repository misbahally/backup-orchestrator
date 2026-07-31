from fnmatch import fnmatch
import os
from pathlib import Path
from typing import Any

from plugins.s3_to_s3 import _normalise_encryption_config, _upload_extra_args
from secret_resolver import resolve_secret_mapping


def _load_secret(secret_ref: str) -> dict[str, str]:
    return resolve_secret_mapping(secret_ref)


def _make_s3_client(region: str, endpoint: str, creds: dict[str, str]) -> Any:
    import boto3

    kwargs: dict[str, Any] = {"service_name": "s3", "region_name": region or "us-east-1"}
    if endpoint:
        kwargs["endpoint_url"] = endpoint
    if creds.get("aws_access_key_id") and creds.get("aws_secret_access_key"):
        kwargs["aws_access_key_id"] = creds["aws_access_key_id"]
        kwargs["aws_secret_access_key"] = creds["aws_secret_access_key"]
    if creds.get("aws_session_token"):
        kwargs["aws_session_token"] = creds["aws_session_token"]
    return boto3.client(**kwargs)


def _is_allowed(root_path: Path, allowed_roots: str) -> bool:
    allow = [Path(p).resolve() for p in allowed_roots.split(":") if p.strip()]
    resolved = root_path.resolve()
    return any(resolved == item or item in resolved.parents for item in allow)


def _matches(path: str, include_globs: list[str], exclude_globs: list[str]) -> bool:
    if include_globs and not any(fnmatch(path, pattern) for pattern in include_globs):
        return False
    if exclude_globs and any(fnmatch(path, pattern) for pattern in exclude_globs):
        return False
    return True


def _same_remote(s3_client: Any, bucket: str, key: str, size: int, mtime: str) -> bool:
    try:
        info = s3_client.head_object(Bucket=bucket, Key=key)
        remote_size = int(info.get("ContentLength", -1))
        remote_mtime = str((info.get("Metadata") or {}).get("mtime", ""))
        return remote_size == size and remote_mtime == mtime
    except Exception:
        return False


def run_file_to_s3(source: Any, destination: Any, binding: Any, allowed_roots: str) -> dict[str, int]:
    source_settings = source.settings or {}
    root_path = Path(str(source_settings.get("root_path", "")).strip())
    if not root_path:
        raise ValueError("file source requires settings.root_path")
    if not _is_allowed(root_path, allowed_roots):
        raise ValueError(f"root_path '{root_path}' is outside FILE_SOURCE_ALLOWED_ROOTS")
    if not root_path.exists() or not root_path.is_dir():
        raise ValueError(f"root_path '{root_path}' does not exist or is not a directory")

    include_globs = [str(x) for x in source_settings.get("include_globs", []) if str(x).strip()]
    exclude_globs = [str(x) for x in source_settings.get("exclude_globs", []) if str(x).strip()]
    follow_symlinks = bool(source_settings.get("follow_symlinks", False))
    key_prefix = str(source_settings.get("key_prefix", f"file/{source.name}/")).strip().strip("/")

    destination_creds = _load_secret(destination.secret_ref)
    s3_client = _make_s3_client(destination.region or "us-east-1", destination.endpoint or "", destination_creds)
    encryption = _normalise_encryption_config((binding.policy or {}).get("encryption") or destination.encryption or {})
    extra_args = _upload_extra_args(encryption)

    copied = 0
    skipped = 0
    transferred = 0

    for base_dir, _, files in os.walk(root_path, followlinks=follow_symlinks):
        for filename in files:
            full_path = Path(base_dir) / filename
            rel_path = str(full_path.relative_to(root_path)).replace(os.sep, "/")
            if not _matches(rel_path, include_globs, exclude_globs):
                continue

            stat = full_path.stat()
            mtime = str(int(stat.st_mtime))
            size = int(stat.st_size)
            key = "/".join([p for p in (key_prefix, rel_path) if p])

            if _same_remote(s3_client, destination.bucket, key, size, mtime):
                skipped += 1
                continue

            upload_args = dict(extra_args)
            upload_args["Metadata"] = {"mtime": mtime}
            if upload_args:
                s3_client.upload_file(str(full_path), destination.bucket, key, ExtraArgs=upload_args)
            else:
                s3_client.upload_file(str(full_path), destination.bucket, key)

            copied += 1
            transferred += size

    return {"copied_objects": copied, "skipped_objects": skipped, "transferred_bytes": transferred}
