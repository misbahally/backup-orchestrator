import io
import os
import subprocess
import tempfile
from typing import Any

from secret_resolver import resolve_secret_mapping, resolve_secret_text


def _load_secret(secret_ref: str) -> dict[str, str]:
    return resolve_secret_mapping(secret_ref)


def _load_text_secret(secret_ref: str) -> str:
    return resolve_secret_text(secret_ref)


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


def _normalise_engine_name(source: Any) -> str:
    engine = str((source.settings or {}).get("engine", "") or "").strip().lower()
    if engine in {"postgres", "postgresql", "pg"}:
        return "postgres"
    if engine in {"mysql", "mariadb"}:
        return "mysql"
    return engine


def _build_dump_command(source: Any) -> list[str]:
    settings = source.settings or {}
    engine = _normalise_engine_name(source)
    host = str(settings.get("host", "")).strip()
    port = str(settings.get("port", "")).strip()
    database = str(settings.get("database", "")).strip()
    username = str(settings.get("username", "")).strip()
    password = str(settings.get("password", "") or settings.get("secret", "")).strip()

    if not database:
        raise ValueError("Database source requires settings.database")
    if not username:
        raise ValueError("Database source requires settings.username")

    if engine == "postgres":
        command = ["pg_dump"]
        if host:
            command.extend(["-h", host])
        if port:
            command.extend(["-p", port])
        if username:
            command.extend(["-U", username])
        command.append(database)
        env = os.environ.copy()
        if password:
            env["PGPASSWORD"] = password
        return command, env

    if engine == "mysql":
        command = ["mysqldump", "--defaults-file=/dev/null"]
        if host:
            command.extend(["--host", host])
        if port:
            command.extend(["--port", port])
        if username:
            command.extend(["--user", username])
        if password:
            command.extend(["--password=" + password])
        command.append(database)
        return command, os.environ.copy()

    raise ValueError(f"Unsupported database engine '{engine}'")


def run_database_dump_to_s3(source: Any, destination: Any, binding: Any) -> dict[str, int]:
    """Create a logical dump from a MySQL/PostgreSQL source and upload it to S3-compatible storage."""
    settings = source.settings or {}
    policy = binding.policy or {}
    engine = _normalise_engine_name(source)
    if engine not in {"mysql", "postgres"}:
        raise ValueError(f"Unsupported database engine '{engine}'")

    dest_prefix = str(policy.get("dest_prefix", "")).strip().strip("/")
    filename = f"{engine}-{str(settings.get('database','')).strip()}.sql"
    if dest_prefix:
        key = f"{dest_prefix}/{filename}"
    else:
        key = filename

    src_region = str(settings.get("region", destination.region or "us-east-1")).strip() or "us-east-1"
    src_endpoint = str(settings.get("endpoint", "")).strip()
    src_secret = _load_secret(str(settings.get("secret_ref", "")))
    dst_secret = _load_secret(destination.secret_ref)

    client = _make_s3_client(src_region, src_endpoint, src_secret)
    if not hasattr(client, "upload_fileobj"):
        raise ValueError("Configured destination does not support upload_fileobj")

    dump_bytes = io.BytesIO()
    with tempfile.TemporaryFile() as handle:
        command, env = _build_dump_command(source)
        completed = subprocess.run(command, stdout=handle, stderr=subprocess.PIPE, check=False, env=env)
        handle.seek(0)
        dump_bytes.write(handle.read())
    if completed.returncode != 0:
        raise RuntimeError(f"Database dump failed for {engine}: {completed.stderr.decode('utf-8', errors='replace').strip()}")

    dump_bytes.seek(0)
    dst = _make_s3_client(destination.region or "us-east-1", destination.endpoint or "", dst_secret)
    dst.upload_fileobj(dump_bytes, destination.bucket, key)

    transferred_bytes = len(dump_bytes.getvalue())
    return {"copied_objects": 1, "skipped_objects": 0, "transferred_bytes": transferred_bytes}
