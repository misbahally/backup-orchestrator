from datetime import datetime, timezone
import os
import time

from botocore.exceptions import ClientError
from rq import get_current_job

from database import SessionLocal
from metrics import (
    BACKUP_FAILURES_TOTAL,
    BACKUP_LAST_SUCCESS_TIMESTAMP,
    BACKUP_OPERATION_DURATION_SECONDS,
    BACKUP_OPERATIONS_TOTAL,
    BACKUP_UPLOADED_BYTES_TOTAL,
)
from models import BackupRun, Binding, Destination, RunStatus, Source, SourceType
from plugins import run_database_dump_to_s3, run_ebs_snapshot, run_file_to_s3, run_rds_snapshot, run_s3_to_s3


TEMP_DISABLED_SOURCE_TYPES = {SourceType.ebs, SourceType.rds}


def _utcnow() -> datetime:
    return datetime.now(timezone.utc)


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


def run_backup_job(run_id: int) -> None:
    db = SessionLocal()
    started = time.perf_counter()
    source_type_name = "unknown"
    binding_label = ""
    try:
        run = db.get(BackupRun, run_id)
        if run is None:
            return

        binding = db.get(Binding, run.binding_id)
        if binding is None:
            raise ValueError(f"Binding {run.binding_id} not found")
        binding_label = str(binding.id)

        source = db.get(Source, binding.source_id)
        if source is None:
            raise ValueError(f"Source {binding.source_id} not found")
        source_type_name = source.source_type.value

        destination = db.get(Destination, binding.destination_id)
        if destination is None:
            raise ValueError(f"Destination {binding.destination_id} not found")

        if not source.is_active:
            raise ValueError(f"Source {source.id} is not active")
        if source.source_type in TEMP_DISABLED_SOURCE_TYPES:
            raise ValueError(f"Source type '{source.source_type.value}' is temporarily disabled")
        if not destination.is_active:
            raise ValueError(f"Destination {destination.id} is not active")
        if not binding.is_active:
            raise ValueError(f"Binding {binding.id} is not active")

        run.status = RunStatus.running
        run.attempts = int(run.attempts or 0) + 1
        run.message = f"Running {source.source_type.value} backup (attempt {run.attempts})"
        db.commit()

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
            summary = run_file_to_s3(source, destination, binding, os.environ.get("FILE_SOURCE_ALLOWED_ROOTS", "/data:/mnt/backups"))
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

        run.status = RunStatus.success
        run.bytes_transferred = transferred
        run.artifact_ref = artifact_ref
        run.finished_at = _utcnow()
        run.message = f"Completed: copied={copied}, skipped={skipped}" if not artifact_ref else f"Completed snapshot: {artifact_ref}"
        db.commit()

        BACKUP_OPERATIONS_TOTAL.labels(source_type=source_type_name, status="success").inc()
        BACKUP_UPLOADED_BYTES_TOTAL.labels(binding=binding_label).inc(transferred)
        BACKUP_LAST_SUCCESS_TIMESTAMP.labels(binding=binding_label).set(run.finished_at.timestamp())
    except Exception as exc:
        run = db.get(BackupRun, run_id)
        job = get_current_job()
        retries_left = int(getattr(job, "retries_left", 0) or 0)
        retryable = _is_retryable(exc)

        if run is not None:
            if retryable and retries_left > 0:
                run.status = RunStatus.queued
                run.message = f"Retrying ({retries_left} retries left): {exc}"
                db.commit()
            else:
                run.status = RunStatus.failed
                run.finished_at = _utcnow()
                run.message = f"Failed: {exc}"
                db.commit()

        BACKUP_OPERATIONS_TOTAL.labels(source_type=source_type_name, status="failed").inc()
        BACKUP_FAILURES_TOTAL.labels(source_type=source_type_name, error_class=exc.__class__.__name__).inc()
        raise
    finally:
        BACKUP_OPERATION_DURATION_SECONDS.labels(source_type=source_type_name).observe(time.perf_counter() - started)
        db.close()
