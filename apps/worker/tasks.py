from datetime import datetime

from database import SessionLocal
from models import BackupRun, RunStatus


def run_backup_job(run_id: int) -> None:
    db = SessionLocal()
    try:
        run = db.get(BackupRun, run_id)
        if run is None:
            return

        run.status = RunStatus.running
        run.message = "Worker picked up job"
        db.commit()

        # Placeholder execution: replace with plugin-based source backup logic.
        transferred = 1024 * 1024

        run.status = RunStatus.success
        run.bytes_transferred = transferred
        run.finished_at = datetime.utcnow()
        run.message = "Completed"
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
