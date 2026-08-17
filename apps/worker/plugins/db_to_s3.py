import io
import os
import subprocess
from typing import Any

from boto3.s3.transfer import TransferConfig

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


def _normalise_engine_name(source: Any) -> str:
    engine = str((source.settings or {}).get("engine", "") or "").strip().lower()
    if engine in {"postgres", "postgresql", "pg"}:
        return "postgres"
    if engine in {"mysql", "mariadb"}:
        return "mysql"
    return engine


def _selected_databases(source_settings: dict[str, Any]) -> list[str]:
    selected: list[str] = []

    raw_list = source_settings.get("databases")
    if isinstance(raw_list, list):
        for value in raw_list:
            name = str(value or "").strip()
            if name and name not in selected:
                selected.append(name)

    single_database = str(source_settings.get("database", "")).strip()
    if single_database and single_database not in selected:
        selected.append(single_database)

    return selected


def _build_dump_command(source: Any, database: str) -> tuple[list[str], dict[str, str]]:
    source_settings = source.settings or {}
    engine = _normalise_engine_name(source)
    host = str(source_settings.get("host", "")).strip()
    port = str(source_settings.get("port", "")).strip()
    username = str(source_settings.get("username", "")).strip()
    password = str(source_settings.get("password", "") or source_settings.get("secret", "")).strip()

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
        command.extend(["-U", username, database])
        env = os.environ.copy()
        if password:
            env["PGPASSWORD"] = password
        return command, env

    if engine == "mysql":
        # Use least-privilege friendly defaults: avoid PROCESS (tablespaces)
        # and explicit table locks.
        command = [
            "mysqldump",
            "--defaults-file=/dev/null",
            "--no-tablespaces",
            "--single-transaction"
        ]
        if host:
            command.extend(["--host", host])
        if port:
            command.extend(["--port", port])
        command.extend(["--user", username])
        if password:
            command.append("--password=" + password)
        command.append(database)
        return command, os.environ.copy()

    raise ValueError(f"Unsupported database engine '{engine}'")


class _CallbackCounter:
    def __init__(self) -> None:
        self.total = 0

    def __call__(self, bytes_amount: int) -> None:
        self.total += int(bytes_amount)


def run_database_dump_to_s3(source: Any, destination: Any, binding: Any) -> dict[str, int]:
    source_settings = source.settings or {}
    policy = binding.policy or {}
    engine = _normalise_engine_name(source)
    if engine not in {"mysql", "postgres"}:
        raise ValueError(f"Unsupported database engine '{engine}'")

    compress = bool(source_settings.get("compress", True))
    selected_databases = _selected_databases(source_settings)
    if not selected_databases:
        raise ValueError("Database source requires settings.database or settings.databases")

    dest_prefix = str(policy.get("dest_prefix", "")).strip().strip("/")

    source_region = str(source_settings.get("region", destination.region or "us-east-1")).strip() or "us-east-1"
    source_endpoint = str(source_settings.get("endpoint", "")).strip()
    source_creds = _load_secret(str(source_settings.get("secret_ref", "")))
    destination_creds = _load_secret(destination.secret_ref)

    _ = _make_s3_client(source_region, source_endpoint, source_creds)
    destination_client = _make_s3_client(destination.region or "us-east-1", destination.endpoint or "", destination_creds)

    copied_objects = 0
    transferred_bytes = 0

    for source_database in selected_databases:
        filename = f"{engine}-{source_database}.sql.gz" if compress else f"{engine}-{source_database}.sql"
        key = f"{dest_prefix}/{filename}" if dest_prefix else filename

        command, env = _build_dump_command(source, source_database)
        dump_proc = subprocess.Popen(command, stdout=subprocess.PIPE, stderr=subprocess.PIPE, env=env)
        gzip_proc: subprocess.Popen[bytes] | None = None
        stream = dump_proc.stdout

        if stream is None:
            raise RuntimeError("Dump command did not expose stdout")

        try:
            if compress:
                gzip_proc = subprocess.Popen(["gzip", "-c"], stdin=stream, stdout=subprocess.PIPE, stderr=subprocess.PIPE)
                stream = gzip_proc.stdout
                if stream is None:
                    raise RuntimeError("gzip command did not expose stdout")

            counter = _CallbackCounter()
            transfer_config = TransferConfig(multipart_threshold=64 * 1024 * 1024, multipart_chunksize=64 * 1024 * 1024)
            extra_args = _upload_extra_args(_normalise_encryption_config(policy.get("encryption") or destination.encryption or {}))
            upload_kwargs: dict[str, Any] = {"Config": transfer_config, "Callback": counter}
            if extra_args:
                upload_kwargs["ExtraArgs"] = extra_args

            destination_client.upload_fileobj(stream, destination.bucket, key, **upload_kwargs)
        except Exception:
            try:
                destination_client.delete_object(Bucket=destination.bucket, Key=key)
            except Exception:
                pass
            raise
        finally:
            if dump_proc.stdout:
                dump_proc.stdout.close()
            if gzip_proc and gzip_proc.stdout:
                gzip_proc.stdout.close()

        dump_stderr = b""
        gzip_stderr = b""
        if dump_proc.stderr:
            dump_stderr = dump_proc.stderr.read()
        dump_rc = dump_proc.wait()

        if gzip_proc:
            if gzip_proc.stderr:
                gzip_stderr = gzip_proc.stderr.read()
            gzip_rc = gzip_proc.wait()
            if gzip_rc != 0:
                destination_client.delete_object(Bucket=destination.bucket, Key=key)
                msg = gzip_stderr.decode("utf-8", errors="replace").strip()
                raise RuntimeError(f"gzip failed for {source_database}: {msg or 'non-zero exit'}")

        if dump_rc != 0:
            destination_client.delete_object(Bucket=destination.bucket, Key=key)
            msg = dump_stderr.decode("utf-8", errors="replace").strip()
            raise RuntimeError(f"Database dump failed for {engine}/{source_database}: {msg or 'non-zero exit'}")

        copied_objects += 1
        transferred_bytes += counter.total

    return {"copied_objects": copied_objects, "skipped_objects": 0, "transferred_bytes": transferred_bytes}
