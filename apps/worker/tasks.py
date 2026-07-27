from datetime import datetime

from database import SessionLocal
from models import BackupRun, Binding, Destination, RunStatus, Source, SourceType
from plugins import run_database_dump_to_s3, run_s3_to_s3


def run_backup_job(run_id: int) -> None:
    db = SessionLocal()
    try:
        run = db.get(BackupRun, run_id)
        if run is None:
            return

        binding = db.get(Binding, run.binding_id)
        if binding is None:
            raise ValueError(f"Binding {run.binding_id} not found")

        source = db.get(Source, binding.source_id)
        if source is None:
            raise ValueError(f"Source {binding.source_id} not found")

        destination = db.get(Destination, binding.destination_id)
        if destination is None:
            raise ValueError(f"Destination {binding.destination_id} not found")

        if not source.is_active:
            raise ValueError(f"Source {source.id} is not active")
        if not destination.is_active:
            raise ValueError(f"Destination {destination.id} is not active")
        if not binding.is_active:
            raise ValueError(f"Binding {binding.id} is not active")

        run.status = RunStatus.running
        run.message = f"Running {source.source_type.value} backup"
        db.commit()

        transferred = 0
        copied = 0
        skipped = 0
        if source.source_type == SourceType.s3:
            summary = run_s3_to_s3(source, destination, binding)
            transferred = int(summary.get("transferred_bytes", 0))
            copied = int(summary.get("copied_objects", 0))
            skipped = int(summary.get("skipped_objects", 0))
            run.message = f"Completed: copied={copied}, skipped={skipped}"
        elif source.source_type in {SourceType.mysql, SourceType.postgresql}:
            summary = run_database_dump_to_s3(source, destination, binding)
            transferred = int(summary.get("transferred_bytes", 0))
            copied = int(summary.get("copied_objects", 0))
            skipped = int(summary.get("skipped_objects", 0))
            run.message = f"Completed: copied={copied}, skipped={skipped}"
        else:
            raise NotImplementedError(
                f"Source type '{source.source_type.value}' is not implemented yet"
            )

        run.status = RunStatus.success
        run.bytes_transferred = transferred
        run.finished_at = datetime.utcnow()
        db.commit()
    except Exception as exc:
        run = db.get(BackupRun, run_id)
        if run is not None:
            run.status = RunStatus.failed
            run.finished_at = datetime.utcnow()
            run.message = f"Failed: {exc}"
            db.commit()
        raise
    finally:
        db.close()
