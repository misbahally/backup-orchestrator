import logging
import time
from datetime import datetime, timezone

from rq import Queue
from redis import Redis

from database import SessionLocal
from models import Binding, BackupRun, RunStatus

logger = logging.getLogger("scheduler")


def enqueue_due_bindings() -> int:
    redis_conn = Redis.from_url("redis://redis:6379/0")
    queue = Queue("backup-runs", connection=redis_conn)
    db = SessionLocal()
    try:
        bindings = db.query(Binding).filter(Binding.is_active.is_(True)).all()
        enqueued = 0
        for binding in bindings:
            if not binding.schedule_cron:
                continue
            run = BackupRun(
                binding_id=binding.id,
                status=RunStatus.queued,
                started_at=datetime.now(timezone.utc),
                message=f"Scheduled enqueue for binding {binding.id}",
            )
            db.add(run)
            db.commit()
            db.refresh(run)
            job = queue.enqueue("tasks.run_backup_job", run.id)
            run.message = f"Queued (job {job.id})"
            db.commit()
            logger.info("Enqueued run %s for binding %s at %s", run.id, binding.id, datetime.now(timezone.utc))
            enqueued += 1
        return enqueued
    finally:
        db.close()


def run_scheduler_loop(interval_seconds: int = 60) -> None:
    while True:
        try:
            enqueue_due_bindings()
        except Exception as exc:  # pragma: no cover - background loop
            logger.exception("Scheduler loop failed: %s", exc)
        time.sleep(interval_seconds)
