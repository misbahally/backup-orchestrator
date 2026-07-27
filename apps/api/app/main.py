from datetime import datetime, timezone
import json
from typing import Any

import boto3
from botocore.exceptions import ClientError, NoCredentialsError
from fastapi import Depends, FastAPI, HTTPException
from fastapi.middleware.cors import CORSMiddleware
from redis import Redis
from rq import Queue
from rq.command import send_stop_job_command
from rq.job import Job
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import Session

from .config import settings
from .database import Base, engine, get_db
from .models import BackupRun, Binding, Destination, RunStatus, Source
from .schemas import (
    BindingCreate,
    BindingRead,
    DestinationCreate,
    DestinationRead,
    RunCancelRequest,
    RunRead,
    SourceCreate,
    SourceRead,
)
from .secret_resolver import resolve_secret_mapping, resolve_secret_text

app = FastAPI(title="Backup Control Plane API", version="0.1.0")

app.add_middleware(
    CORSMiddleware,
    allow_origins=[o.strip() for o in settings.cors_origins.split(",") if o.strip()],
    allow_credentials=True,
    allow_methods=["*"],
    allow_headers=["*"],
)


@app.on_event("startup")
def startup() -> None:
    Base.metadata.create_all(bind=engine)


def queue() -> Queue:
    redis_conn = Redis.from_url(settings.redis_url)
    return Queue("backup-runs", connection=redis_conn)


def _load_text_secret(secret_ref: str) -> str:
    return resolve_secret_text(secret_ref)


def _load_secret(secret_ref: str) -> dict[str, str]:
    return resolve_secret_mapping(secret_ref)


def _make_s3_client(region: str, endpoint: str, creds: dict[str, str]) -> Any:
    kwargs: dict[str, Any] = {
        "service_name": "s3",
        "region_name": region or "us-east-1",
    }

    if endpoint:
        kwargs["endpoint_url"] = endpoint

    if creds.get("aws_access_key_id") and creds.get("aws_secret_access_key"):
        kwargs["aws_access_key_id"] = creds["aws_access_key_id"]
        kwargs["aws_secret_access_key"] = creds["aws_secret_access_key"]
    if creds.get("aws_session_token"):
        kwargs["aws_session_token"] = creds["aws_session_token"]

    return boto3.client(**kwargs)


def _encryption_mode(config: dict[str, Any]) -> str:
    return str((config or {}).get("mode", "")).upper()


def _validate_sse_config(name: str, config: dict[str, Any]) -> None:
    mode = _encryption_mode(config)
    if not mode:
        return

    if mode in {"SSE-S3", "SSE_S3", "AES256"}:
        return

    if mode in {"SSE-KMS", "SSE_KMS", "AWS:KMS", "AWS-KMS"}:
        if not config.get("kms_key_id") and not config.get("kms_key_arn"):
            raise HTTPException(status_code=400, detail=f"{name} encryption requires kms_key_id or kms_key_arn")
        return

    if mode in {"SSE-C", "SSE_C", "CUSTOMER", "AES256-C"}:
        if not (config.get("customer_key") or config.get("customer_key_ref") or config.get("customer_key_secret_ref")):
            raise HTTPException(status_code=400, detail=f"{name} SSE-C requires customer_key or customer_key_ref")
        return

    raise HTTPException(status_code=400, detail=f"{name} has unsupported encryption mode: {config.get('mode')}")


def _source_encryption(source: Source) -> dict[str, Any]:
    settings = source.settings or {}
    return settings.get("encryption") or settings.get("sse") or {}


def _destination_encryption(binding: Binding, destination: Destination) -> dict[str, Any]:
    policy = binding.policy or {}
    return policy.get("encryption") or policy.get("destination_encryption") or {}


def _validate_source_connection(source: Source) -> dict[str, Any]:
    source_type = source.source_type.value
    if source_type == "s3":
        settings = source.settings or {}
        bucket = str(settings.get("bucket", "")).strip()
        if not bucket:
            raise HTTPException(status_code=400, detail="S3 source requires settings.bucket")

        _validate_sse_config("source", _source_encryption(source))

        client = _make_s3_client(
            settings.get("region", "us-east-1"),
            settings.get("endpoint", ""),
            _load_secret(str(settings.get("secret_ref", ""))),
        )

        try:
            client.head_bucket(Bucket=bucket)
        except NoCredentialsError:
            raise HTTPException(status_code=400, detail="source credentials could not be resolved")
        except ClientError as exc:
            code = exc.response.get("Error", {}).get("Code", "")
            raise HTTPException(status_code=400, detail=f"source validation failed: {code or str(exc)}")

        return {"ok": True, "message": f"Source bucket {bucket} is reachable", "details": {"bucket": bucket, "source_id": source.id}}

    if source_type in {"mysql", "postgresql"}:
        settings = source.settings or {}
        missing = [key for key in ("host", "database", "username") if not str(settings.get(key, "")).strip()]
        if missing:
            raise HTTPException(status_code=400, detail=f"{source_type} source requires settings.{', settings.'.join(missing)}")
        return {
            "ok": True,
            "message": f"{source_type.title()} source settings look complete",
            "details": {"source_id": source.id, "engine": source_type, "host": settings.get("host"), "database": settings.get("database")},
        }

    return {"ok": True, "message": f"No connection test implemented for {source_type}"}


def _validate_destination_connection(destination: Destination, binding: Binding | None = None) -> dict[str, Any]:
    _validate_sse_config("destination", _destination_encryption(binding, destination) if binding else {})

    client = _make_s3_client(
        destination.region,
        destination.endpoint,
        _load_secret(destination.secret_ref),
    )

    try:
        client.head_bucket(Bucket=destination.bucket)
    except NoCredentialsError:
        raise HTTPException(status_code=400, detail="destination credentials could not be resolved")
    except ClientError as exc:
        code = exc.response.get("Error", {}).get("Code", "")
        raise HTTPException(status_code=400, detail=f"destination validation failed: {code or str(exc)}")

    return {"ok": True, "message": f"Destination bucket {destination.bucket} is reachable", "details": {"bucket": destination.bucket, "destination_id": destination.id}}


@app.get("/health")
def health() -> dict:
    return {"status": "ok"}


@app.post("/destinations", response_model=DestinationRead)
def create_destination(payload: DestinationCreate, db: Session = Depends(get_db)) -> Destination:
    item = Destination(**payload.model_dump())
    db.add(item)
    db.commit()
    db.refresh(item)
    return item


@app.get("/destinations", response_model=list[DestinationRead])
def list_destinations(db: Session = Depends(get_db)) -> list[Destination]:
    return db.query(Destination).order_by(Destination.id.desc()).all()


@app.put("/destinations/{destination_id}", response_model=DestinationRead)
def update_destination(destination_id: int, payload: DestinationCreate, db: Session = Depends(get_db)) -> Destination:
    item = db.get(Destination, destination_id)
    if item is None:
        raise HTTPException(status_code=404, detail="destination not found")

    for key, value in payload.model_dump().items():
        setattr(item, key, value)

    db.commit()
    db.refresh(item)
    return item


@app.delete("/destinations/{destination_id}")
def delete_destination(destination_id: int, db: Session = Depends(get_db)) -> dict[str, bool]:
    item = db.get(Destination, destination_id)
    if item is None:
        raise HTTPException(status_code=404, detail="destination not found")

    db.delete(item)
    try:
        db.commit()
    except IntegrityError:
        db.rollback()
        raise HTTPException(status_code=409, detail="destination is used by one or more bindings")

    return {"ok": True}


@app.post("/sources", response_model=SourceRead)
def create_source(payload: SourceCreate, db: Session = Depends(get_db)) -> Source:
    item = Source(**payload.model_dump())
    db.add(item)
    db.commit()
    db.refresh(item)
    return item


@app.get("/sources", response_model=list[SourceRead])
def list_sources(db: Session = Depends(get_db)) -> list[Source]:
    return db.query(Source).order_by(Source.id.desc()).all()


@app.put("/sources/{source_id}", response_model=SourceRead)
def update_source(source_id: int, payload: SourceCreate, db: Session = Depends(get_db)) -> Source:
    item = db.get(Source, source_id)
    if item is None:
        raise HTTPException(status_code=404, detail="source not found")

    for key, value in payload.model_dump().items():
        setattr(item, key, value)

    db.commit()
    db.refresh(item)
    return item


@app.delete("/sources/{source_id}")
def delete_source(source_id: int, db: Session = Depends(get_db)) -> dict[str, bool]:
    item = db.get(Source, source_id)
    if item is None:
        raise HTTPException(status_code=404, detail="source not found")

    db.delete(item)
    try:
        db.commit()
    except IntegrityError:
        db.rollback()
        raise HTTPException(status_code=409, detail="source is used by one or more bindings")

    return {"ok": True}


@app.post("/bindings", response_model=BindingRead)
def create_binding(payload: BindingCreate, db: Session = Depends(get_db)) -> Binding:
    source = db.get(Source, payload.source_id)
    dest = db.get(Destination, payload.destination_id)
    if source is None:
        raise HTTPException(status_code=404, detail="source not found")
    if dest is None:
        raise HTTPException(status_code=404, detail="destination not found")

    item = Binding(**payload.model_dump())
    db.add(item)
    db.commit()
    db.refresh(item)
    return item


@app.get("/bindings", response_model=list[BindingRead])
def list_bindings(db: Session = Depends(get_db)) -> list[Binding]:
    return db.query(Binding).order_by(Binding.id.desc()).all()


@app.put("/bindings/{binding_id}", response_model=BindingRead)
def update_binding(binding_id: int, payload: BindingCreate, db: Session = Depends(get_db)) -> Binding:
    item = db.get(Binding, binding_id)
    if item is None:
        raise HTTPException(status_code=404, detail="binding not found")

    source = db.get(Source, payload.source_id)
    dest = db.get(Destination, payload.destination_id)
    if source is None:
        raise HTTPException(status_code=404, detail="source not found")
    if dest is None:
        raise HTTPException(status_code=404, detail="destination not found")

    for key, value in payload.model_dump().items():
        setattr(item, key, value)

    db.commit()
    db.refresh(item)
    return item


@app.delete("/bindings/{binding_id}")
def delete_binding(binding_id: int, db: Session = Depends(get_db)) -> dict[str, bool]:
    item = db.get(Binding, binding_id)
    if item is None:
        raise HTTPException(status_code=404, detail="binding not found")

    db.delete(item)
    try:
        db.commit()
    except IntegrityError:
        db.rollback()
        raise HTTPException(status_code=409, detail="binding has existing runs and cannot be deleted")

    return {"ok": True}


@app.get("/validate/source/{source_id}")
def validate_source(source_id: int, db: Session = Depends(get_db)) -> dict:
    source = db.get(Source, source_id)
    if source is None:
        raise HTTPException(status_code=404, detail="source not found")
    return _validate_source_connection(source)


@app.get("/validate/destination/{destination_id}")
def validate_destination(destination_id: int, db: Session = Depends(get_db)) -> dict:
    destination = db.get(Destination, destination_id)
    if destination is None:
        raise HTTPException(status_code=404, detail="destination not found")
    return _validate_destination_connection(destination)


@app.get("/validate/binding/{binding_id}")
def validate_binding(binding_id: int, db: Session = Depends(get_db)) -> dict:
    binding = db.get(Binding, binding_id)
    if binding is None:
        raise HTTPException(status_code=404, detail="binding not found")

    source = db.get(Source, binding.source_id)
    destination = db.get(Destination, binding.destination_id)
    if source is None:
        raise HTTPException(status_code=404, detail="source not found")
    if destination is None:
        raise HTTPException(status_code=404, detail="destination not found")

    source_result = _validate_source_connection(source)
    destination_result = _validate_destination_connection(destination, binding)

    return {
        "ok": True,
        "source": source_result,
        "destination": destination_result,
        "message": "Binding is valid",
    }


@app.post("/validate/source")
def validate_source_payload(payload: SourceCreate) -> dict:
    source = Source(**payload.model_dump())
    return _validate_source_connection(source)


@app.post("/validate/destination")
def validate_destination_payload(payload: DestinationCreate) -> dict:
    destination = Destination(**payload.model_dump())
    return _validate_destination_connection(destination)


@app.post("/validate/binding")
def validate_binding_payload(payload: BindingCreate, db: Session = Depends(get_db)) -> dict:
    source = db.get(Source, payload.source_id)
    destination = db.get(Destination, payload.destination_id)
    if source is None:
        raise HTTPException(status_code=404, detail="source not found")
    if destination is None:
        raise HTTPException(status_code=404, detail="destination not found")

    binding = Binding(**payload.model_dump())
    source_result = _validate_source_connection(source)
    destination_result = _validate_destination_connection(destination, binding)

    return {
        "ok": True,
        "source": source_result,
        "destination": destination_result,
        "message": "Binding is valid",
    }


@app.post("/runs/trigger/{binding_id}", response_model=RunRead)
def trigger_run(binding_id: int, db: Session = Depends(get_db)) -> BackupRun:
    binding = db.get(Binding, binding_id)
    if binding is None:
        raise HTTPException(status_code=404, detail="binding not found")

    run = BackupRun(
        binding_id=binding_id,
        status=RunStatus.queued,
        started_at=datetime.now(timezone.utc),
        message="Queued",
    )
    db.add(run)
    db.commit()
    db.refresh(run)

    q = queue()
    job = q.enqueue("tasks.run_backup_job", run.id)
    run.message = f"Queued (job {job.id})"
    db.commit()
    return run


def _cancel_run(run: BackupRun, q: Queue, db: Session) -> tuple[bool, str]:
    if run.status.value not in {"queued", "running"}:
        return False, f"run is {run.status.value}"

    job_id = ""
    if run.message and "Queued (job " in run.message:
        job_id = run.message.removeprefix("Queued (job ").removesuffix(")")

    if not job_id:
        return False, "no queue job id found for this run"

    try:
        job = Job.fetch(job_id, connection=q.connection)
    except Exception:
        if run.status.value == "queued":
            return False, "queue job no longer exists"
        run.message = "Cancellation requested (worker is already running)"
        db.commit()
        return True, "cancellation requested"

    status = job.get_status(refresh=True)

    if status in {"queued", "deferred", "scheduled"}:
        job.cancel()
        run.status = RunStatus.cancelled
        run.finished_at = datetime.now(timezone.utc)
        run.message = "Cancelled before execution"
        db.commit()
        return True, "cancelled"

    if status in {"started", "busy"}:
        send_stop_job_command(q.connection, job_id)
        run.message = "Cancellation requested"
        db.commit()
        return True, "cancellation requested"

    return False, f"job already {status}"


@app.post("/runs/cancel")
def cancel_runs(payload: RunCancelRequest, db: Session = Depends(get_db)) -> dict[str, Any]:
    if not payload.run_ids:
        raise HTTPException(status_code=400, detail="run_ids cannot be empty")

    q = queue()
    cancelled: list[dict[str, Any]] = []
    not_cancelled: list[dict[str, Any]] = []

    for run_id in payload.run_ids:
        run = db.get(BackupRun, run_id)
        if run is None:
            not_cancelled.append({"run_id": run_id, "reason": "run not found"})
            continue

        ok, reason = _cancel_run(run, q, db)
        if ok:
            cancelled.append({"run_id": run_id, "result": reason})
        else:
            not_cancelled.append({"run_id": run_id, "reason": reason})

    return {"cancelled": cancelled, "not_cancelled": not_cancelled}


@app.post("/runs/{run_id}/cancel")
def cancel_single_run(run_id: int, db: Session = Depends(get_db)) -> dict[str, Any]:
    run = db.get(BackupRun, run_id)
    if run is None:
        raise HTTPException(status_code=404, detail="run not found")

    ok, reason = _cancel_run(run, queue(), db)
    if not ok:
        raise HTTPException(status_code=409, detail=reason)
    return {"ok": True, "result": reason}


@app.get("/runs", response_model=list[RunRead])
def list_runs(db: Session = Depends(get_db)) -> list[BackupRun]:
    return db.query(BackupRun).order_by(BackupRun.id.desc()).limit(100).all()


@app.get("/runs/{run_id}")
def read_run(run_id: int, db: Session = Depends(get_db)) -> dict[str, Any]:
    run = db.get(BackupRun, run_id)
    if run is None:
        raise HTTPException(status_code=404, detail="run not found")

    return {
        "id": run.id,
        "binding_id": run.binding_id,
        "status": run.status.value,
        "started_at": run.started_at,
        "finished_at": run.finished_at,
        "bytes_transferred": run.bytes_transferred,
        "message": run.message,
    }


@app.get("/topology")
def topology(db: Session = Depends(get_db)) -> dict:
    sources = db.query(Source).all()
    destinations = db.query(Destination).all()
    bindings = db.query(Binding).all()

    nodes = []
    edges = []

    for s in sources:
        nodes.append(
            {
                "id": f"source-{s.id}",
                "label": s.name,
                "kind": "source",
                "type": s.source_type.value,
                "active": s.is_active,
            }
        )

    for d in destinations:
        nodes.append(
            {
                "id": f"dest-{d.id}",
                "label": d.name,
                "kind": "destination",
                "type": d.provider,
                "active": d.is_active,
            }
        )

    for b in bindings:
        edges.append(
            {
                "id": f"binding-{b.id}",
                "from": f"source-{b.source_id}",
                "to": f"dest-{b.destination_id}",
                "schedule": b.schedule_cron,
                "active": b.is_active,
            }
        )

    return {"nodes": nodes, "edges": edges}
