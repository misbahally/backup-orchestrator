import logging
import os
import time
from typing import TYPE_CHECKING

from botocore.exceptions import ClientError

from metrics import (
    BACKUP_FAILURES_TOTAL,
    BACKUP_LAST_SUCCESS_TIMESTAMP,
    BACKUP_OPERATION_DURATION_SECONDS,
    BACKUP_OPERATIONS_TOTAL,
    BACKUP_UPLOADED_BYTES_TOTAL,
)
from models import Binding, Destination, Source, SourceType
from plugins import run_database_dump_to_s3, run_ebs_snapshot, run_file_to_s3, run_rds_snapshot, run_s3_to_s3

if TYPE_CHECKING:
    from api_client import WorkerApiClient

logger = logging.getLogger("backup-worker")
TEMP_DISABLED_SOURCE_TYPES = {SourceType.ebs, SourceType.rds}


def _is_retryable(exc: Exception) -> bool:
    if isinstance(exc, NotImplementedError):
        return False
    if isinstance(exc, ValueError):
        return False
    if isinstance(exc, ClientError):
        code = str(exc.response.get("Error", {}).get("Code", ""))
        if code.startswith("4") and code not in {"429", "408"}:
            return False
    return True


def run_backup_job(context: dict, client: "WorkerApiClient") -> None:
    """Execute a claimed backup run described by the claim-response context dict."""
    run_id: int = context["run_id"]
    source_type_name: str = context.get("source_type", "unknown")
    binding_id = str(context.get("binding_id", ""))
    started = time.perf_counter()

    try:
        source = Source(
            source_type=SourceType(source_type_name),
            settings=context.get("source_settings") or {},
        )
        dest_data = context.get("destination") or {}
        destination = Destination(
            endpoint=dest_data.get("endpoint", ""),
            bucket=dest_data.get("bucket", ""),
            region=dest_data.get("region", "us-east-1"),
            secret_ref=dest_data.get("secret_ref", ""),
            encryption=dest_data.get("encryption") or {},
        )
        binding_data = context.get("binding") or {}
        binding = Binding(
            id=int(binding_data.get("id") or binding_id or 0),
            policy=binding_data.get("policy") or {},
        )

        if source.source_type in TEMP_DISABLED_SOURCE_TYPES:
            raise ValueError(f"Source type '{source.source_type.value}' is temporarily disabled")

        resp = client.report_status(run_id, "running", f"Starting {source_type_name} backup")
        if resp.get("cancel_requested"):
            client.report_status(run_id, "cancelled", "Cancelled before execution")
            BACKUP_OPERATIONS_TOTAL.labels(source_type=source_type_name, status="cancelled").inc()
            return

        transferred = 0
        copied = 0
        skipped = 0
        artifact_ref = ""

        if source.source_type == SourceType.s3:
            summary = run_s3_to_s3(source, destination, binding)
            transferred = int(summary.get("transferred_bytes", 0))
            copied = int(summary.get("copied_objects", 0))
            skipped = int(summary.get("skipped_objects", 0))
        elif source.source_type in {SourceType.mysql, SourceType.postgresql}:
            summary = run_database_dump_to_s3(source, destination, binding)
            transferred = int(summary.get("transferred_bytes", 0))
            copied = int(summary.get("copied_objects", 0))
            skipped = int(summary.get("skipped_objects", 0))
        elif source.source_type == SourceType.file:
            summary = run_file_to_s3(
                source, destination, binding,
                os.environ.get("FILE_SOURCE_ALLOWED_ROOTS", "/data:/mnt/backups"),
            )
            transferred = int(summary.get("transferred_bytes", 0))
            copied = int(summary.get("copied_objects", 0))
            skipped = int(summary.get("skipped_objects", 0))
        elif source.source_type == SourceType.ebs:
            summary = run_ebs_snapshot(source, destination, binding)
            artifact_ref = str(summary.get("artifact_ref", ""))
        elif source.source_type == SourceType.rds:
            summary = run_rds_snapshot(source, destination, binding)
            artifact_ref = str(summary.get("artifact_ref", ""))
        else:
            raise NotImplementedError(f"Source type '{source.source_type.value}' is not implemented yet")

        finish_message = (
            f"Completed snapshot: {artifact_ref}" if artifact_ref
            else f"Completed: copied={copied}, skipped={skipped}"
        )
        client.report_status(
            run_id, "success",
            message=finish_message,
            bytes_transferred=transferred,
            artifact_ref=artifact_ref,
        )

        BACKUP_OPERATIONS_TOTAL.labels(source_type=source_type_name, status="success").inc()
        BACKUP_UPLOADED_BYTES_TOTAL.labels(binding=binding_id).inc(transferred)
        BACKUP_LAST_SUCCESS_TIMESTAMP.labels(binding=binding_id).set(time.time())

    except Exception as exc:
        retryable = _is_retryable(exc)
        client.report_status(
            run_id, "failed",
            message=f"Failed: {exc}",
            retryable=retryable,
        )
        BACKUP_OPERATIONS_TOTAL.labels(source_type=source_type_name, status="failed").inc()
        BACKUP_FAILURES_TOTAL.labels(source_type=source_type_name, error_class=exc.__class__.__name__).inc()
        logger.exception("Backup run %s failed", run_id)
    finally:
        BACKUP_OPERATION_DURATION_SECONDS.labels(source_type=source_type_name).observe(time.perf_counter() - started)




