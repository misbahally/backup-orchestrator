import logging
import os
import time
from datetime import datetime, timedelta, timezone

from croniter import croniter
from prometheus_client import start_http_server
from redis import Redis
from rq import Queue, Retry
from rq.job import Job

from database import SessionLocal
from metrics import SCHEDULER_ENQUEUED_RUNS_TOTAL
from models import BackupRun, Binding, RunStatus

logger = logging.getLogger("scheduler")
logging.basicConfig(level=getattr(logging, os.environ.get("LOG_LEVEL", "INFO").upper(), logging.INFO))


def _utcnow() -> datetime:
    return datetime.now(timezone.utc)


def _extract_job_id(message: str) -> str:
    if not message or "Queued (job " not in message:
        return ""
    return message.removeprefix("Queued (job ").removesuffix(")")


def _get_run_job_id(run: BackupRun) -> str:
    return (run.queue_job_id or _extract_job_id(run.message) or "").strip()


def _has_active_run(db, binding_id: int) -> bool:
    existing = (
        db.query(BackupRun)
        .filter(BackupRun.binding_id == binding_id)
        .filter(BackupRun.status.in_([RunStatus.queued, RunStatus.running]))
        .first()
    )
    return existing is not None


def _should_enqueue(binding: Binding, now: datetime, interval_seconds: int) -> bool:
    schedule = (binding.schedule_cron or "").strip()
    if not schedule:
        return False
    if not croniter.is_valid(schedule):
        logger.warning("Invalid cron for binding %s: %s", binding.id, schedule)
        return False

    last = binding.last_scheduled_at or (now - timedelta(seconds=interval_seconds * 2))
    next_fire = croniter(schedule, last).get_next(datetime)
    return next_fire <= now


def enqueue_due_bindings() -> int:
    redis_url = os.environ.get("REDIS_URL", "redis://redis:6379/0")
    max_retries = int(os.environ.get("MAX_RETRIES", "3"))
    job_timeout = os.environ.get("RQ_JOB_TIMEOUT", "6h")

    redis_conn = Redis.from_url(redis_url)
    work_queue = Queue("backup-runs", connection=redis_conn)
    db = SessionLocal()
    interval_seconds = int(os.environ.get("SCHEDULER_INTERVAL_SECONDS", "60"))

    try:
        now = _utcnow()
        bindings = db.query(Binding).filter(Binding.is_active.is_(True)).all()
        enqueued = 0
        for binding in bindings:
            if not _should_enqueue(binding, now, interval_seconds):
                continue
            if _has_active_run(db, binding.id):
                logger.info("Skipping binding %s due to active run", binding.id)
                continue

            run = BackupRun(
                binding_id=binding.id,
                status=RunStatus.queued,
                started_at=now,
                message=f"Scheduled enqueue for binding {binding.id}",
                attempts=0,
                max_attempts=max_retries + 1,
            )
            db.add(run)
            db.flush()
            retry = Retry(max=max_retries, interval=[60, 300, 900]) if max_retries > 0 else None
            job = work_queue.enqueue("tasks.run_backup_job", run.id, retry=retry, job_timeout=job_timeout)
            run.queue_job_id = job.id
            run.message = f"Queued (job {job.id})"
            binding.last_scheduled_at = now
            db.commit()
            logger.info("Enqueued run %s for binding %s", run.id, binding.id)
            enqueued += 1
            SCHEDULER_ENQUEUED_RUNS_TOTAL.inc()
        return enqueued
    finally:
        db.close()


def reconcile_orphaned_runs(redis_conn=None, cutoff: datetime | None = None) -> int:
    db = SessionLocal()
    marked = 0
    try:
        query = db.query(BackupRun).filter(BackupRun.status.in_([RunStatus.running, RunStatus.queued]))
        if cutoff is not None:
            query = query.filter(BackupRun.started_at < cutoff)

        runs = query.all()
        for run in runs:
            job_id = _get_run_job_id(run)
            if not job_id:
                run.status = RunStatus.failed
                run.finished_at = _utcnow()
                run.message = "Failed: worker lost / timed out"
                marked += 1
                continue

            active = True
            try:
                job = Job.fetch(job_id, connection=redis_conn)
                status = job.get_status(refresh=True)
                active = status in {"started", "queued", "deferred", "scheduled", "busy"}
            except Exception:
                active = False

            if not active:
                run.status = RunStatus.failed
                run.finished_at = _utcnow()
                run.message = "Failed: worker lost / timed out"
                marked += 1

        if marked:
            db.commit()
        return marked
    finally:
        db.close()


def reap_stale_running_runs() -> int:
    timeout_raw = os.environ.get("RQ_JOB_TIMEOUT", "6h")
    if timeout_raw.endswith("h"):
        max_seconds = int(timeout_raw[:-1]) * 3600
    elif timeout_raw.endswith("m"):
        max_seconds = int(timeout_raw[:-1]) * 60
    else:
        max_seconds = int(timeout_raw)
    grace_seconds = int(os.environ.get("RUN_STALE_GRACE_SECONDS", "300"))
    cutoff = _utcnow() - timedelta(seconds=max_seconds + grace_seconds)

    redis_url = os.environ.get("REDIS_URL", "redis://redis:6379/0")
    redis_conn = Redis.from_url(redis_url)
    return reconcile_orphaned_runs(redis_conn=redis_conn, cutoff=cutoff)


def run_scheduler_loop() -> None:
    interval_seconds = int(os.environ.get("SCHEDULER_INTERVAL_SECONDS", "60"))
    metrics_port = int(os.environ.get("METRICS_PORT", "9090"))
    start_http_server(metrics_port)

    while True:
        try:
            enqueue_due_bindings()
            reap_stale_running_runs()
            with open("/tmp/scheduler.heartbeat", "w", encoding="utf-8") as handle:
                handle.write(_utcnow().isoformat())
        except Exception as exc:  # pragma: no cover
            logger.exception("Scheduler loop failed: %s", exc)
        time.sleep(interval_seconds)


if __name__ == "__main__":
    run_scheduler_loop()
