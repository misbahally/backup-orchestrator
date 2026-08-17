from datetime import datetime, timezone
import json
import logging
from pathlib import Path
import time
from typing import Any

import boto3
from botocore.exceptions import ClientError, NoCredentialsError
from croniter import croniter
from fastapi import Depends, FastAPI, HTTPException, Request
from fastapi import Response
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import JSONResponse
from prometheus_client import CONTENT_TYPE_LATEST, Counter, Histogram, generate_latest
from pymysql import connect as mysql_connect
from redis import Redis
from rq import Queue, Retry
from rq.command import send_stop_job_command
from rq.job import Job
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import Session
import psycopg2

from .auth import (
    ADMIN_USERNAME,
    DUMMY_PASSWORD_HASH,
    create_session,
    enforce_api_key,
    hash_password,
    revoke_session,
    verify_password,
)
from .config import settings
from .database import get_db
from .models import BackupRun, Binding, Destination, RunStatus, Source, SourceType, User
from .schemas import (
    BindingCreate,
    BindingRead,
    ChangePasswordRequest,
    DestinationCreate,
    DestinationRead,
    LoginRequest,
    LoginResponse,
    RunCancelRequest,
    RunRead,
    SourceCreate,
    SourceDatabaseScanRequest,
    SourceRead,
)
from .secret_resolver import resolve_secret_mapping, resolve_secret_text

logging.basicConfig(level=getattr(logging, settings.log_level.upper(), logging.INFO))
logger = logging.getLogger("backup-api")

REQUEST_COUNT = Counter("api_requests_total", "HTTP requests", ["method", "path", "status"])
REQUEST_LATENCY = Histogram("api_request_duration_seconds", "HTTP request latency", ["method", "path"])
TEMP_DISABLED_SOURCE_TYPES = {SourceType.ebs, SourceType.rds}

app = FastAPI(
    title="Backup Control Plane API",
    version="0.2.0",
    docs_url="/docs" if settings.expose_docs else None,
    redoc_url="/redoc" if settings.expose_docs else None,
    openapi_url="/openapi.json" if settings.expose_docs else None,
)

app.add_middleware(
    CORSMiddleware,
    allow_origins=[o.strip() for o in settings.cors_origins.split(",") if o.strip()],
    allow_credentials=True,
    allow_methods=["*"],
    allow_headers=["*"],
)


@app.middleware("http")
async def auth_and_metrics(request: Request, call_next):
    start = time.perf_counter()
    path = request.url.path
    auth_error = enforce_api_key(request)
    if auth_error is not None:
        REQUEST_COUNT.labels(request.method, path, str(auth_error.status_code)).inc()
        REQUEST_LATENCY.labels(request.method, path).observe(time.perf_counter() - start)
        return JSONResponse(
            status_code=auth_error.status_code,
            content={"detail": auth_error.detail},
            headers=auth_error.headers,
        )

    response = await call_next(request)
    REQUEST_COUNT.labels(request.method, path, str(response.status_code)).inc()
    REQUEST_LATENCY.labels(request.method, path).observe(time.perf_counter() - start)
    return response


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
        if not (
            config.get("customer_key")
            or config.get("customer_key_ref")
            or config.get("customer_key_secret_ref")
            or config.get("aws_secrets_arn")
        ):
            raise HTTPException(status_code=400, detail=f"{name} SSE-C requires customer_key, customer_key_ref, or aws_secrets_arn")
        return

    raise HTTPException(status_code=400, detail=f"{name} has unsupported encryption mode: {config.get('mode')}")


def _source_encryption(source: Source) -> dict[str, Any]:
    source_settings = source.settings or {}
    return source_settings.get("encryption") or source_settings.get("sse") or {}


def _destination_encryption(binding: Binding, destination: Destination) -> dict[str, Any]:
    policy = binding.policy or {}
    return policy.get("encryption") or policy.get("destination_encryption") or destination.encryption or {}


def _validate_file_source(source: Source) -> dict[str, Any]:
    checks: list[dict[str, Any]] = []
    source_settings = source.settings or {}
    root_path = str(source_settings.get("root_path", "")).strip()
    if not root_path:
        raise HTTPException(status_code=400, detail="file source requires settings.root_path")

    allowed_roots = [Path(p).resolve() for p in settings.file_source_allowed_roots.split(":") if p.strip()]
    resolved = Path(root_path).resolve()
    in_allow_list = any(root == resolved or root in resolved.parents for root in allowed_roots)
    checks.append({"name": "allow_list", "passed": in_allow_list, "detail": str(resolved)})

    exists = resolved.exists()
    checks.append({"name": "exists", "passed": exists, "detail": str(resolved)})

    is_dir = exists and resolved.is_dir()
    checks.append({"name": "is_directory", "passed": is_dir, "detail": str(resolved)})

    readable = exists and is_dir and resolved.stat().st_mode is not None
    checks.append({"name": "readable", "passed": readable, "detail": str(resolved)})

    ok = all(c["passed"] for c in checks)
    return {"ok": ok, "message": "File source validated" if ok else "File source validation failed", "checks": checks}


def _selected_databases_from_settings(source_settings: dict[str, Any]) -> list[str]:
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


def _scan_db_databases(source_type: SourceType, source_settings: dict[str, Any]) -> dict[str, Any]:
    engine_name = source_type.value
    if engine_name not in {"mysql", "postgresql"}:
        raise HTTPException(status_code=400, detail="database scan supports only mysql and postgresql sources")

    host = str(source_settings.get("host", "")).strip()
    username = str(source_settings.get("username", "")).strip()
    password = str(source_settings.get("password", "")).strip()
    port = int(source_settings.get("port", 5432 if engine_name == "postgresql" else 3306))
    selected = _selected_databases_from_settings(source_settings)

    if not host or not username:
        missing = [k for k, v in (("host", host), ("username", username)) if not v]
        raise HTTPException(status_code=400, detail=f"{engine_name} source requires settings.{', settings.'.join(missing)}")

    try:
        if engine_name == "postgresql":
            probe_db = str(source_settings.get("database", "")).strip() or "postgres"
            conn = psycopg2.connect(host=host, port=port, dbname=probe_db, user=username, password=password, connect_timeout=5)
            try:
                with conn.cursor() as cur:
                    cur.execute(
                        "SELECT datname FROM pg_database "
                        "WHERE datallowconn = true AND datistemplate = false "
                        "ORDER BY datname"
                    )
                    databases = [str(row[0]) for row in cur.fetchall() if row and row[0]]
            finally:
                conn.close()
        else:
            conn_kwargs: dict[str, Any] = {
                "host": host,
                "port": port,
                "user": username,
                "password": password,
                "connect_timeout": 5,
            }
            probe_db = str(source_settings.get("database", "")).strip()
            if probe_db:
                conn_kwargs["database"] = probe_db
            conn = mysql_connect(**conn_kwargs)
            try:
                with conn.cursor() as cur:
                    cur.execute("SHOW DATABASES")
                    databases = [str(row[0]) for row in cur.fetchall() if row and row[0]]
            finally:
                conn.close()
    except Exception as exc:
        detail = str(exc)
        if password:
            detail = detail.replace(password, "***")
        raise HTTPException(status_code=400, detail=f"{engine_name} database scan failed: {detail}")

    return {
        "ok": True,
        "engine": engine_name,
        "databases": databases,
        "selected_databases": selected,
        "message": f"Found {len(databases)} databases",
    }


def _validate_db_source(source: Source, engine_name: str) -> dict[str, Any]:
    source_settings = source.settings or {}
    host = str(source_settings.get("host", "")).strip()
    selected_databases = _selected_databases_from_settings(source_settings)
    database = selected_databases[0] if selected_databases else ""
    username = str(source_settings.get("username", "")).strip()
    port = int(source_settings.get("port", 5432 if engine_name == "postgresql" else 3306))
    password = str(source_settings.get("password", "")).strip()

    checks: list[dict[str, Any]] = []
    if not host or not username or not selected_databases:
        missing = [k for k, v in (("host", host), ("database", database), ("username", username)) if not v]
        raise HTTPException(status_code=400, detail=f"{engine_name} source requires settings.{', settings.'.join(missing)}")

    try:
        if engine_name == "postgresql":
            conn = psycopg2.connect(host=host, port=port, dbname=database, user=username, password=password, connect_timeout=5)
            try:
                with conn.cursor() as cur:
                    cur.execute("SELECT 1")
                    cur.fetchone()
            finally:
                conn.close()
        else:
            conn = mysql_connect(host=host, port=port, database=database, user=username, password=password, connect_timeout=5)
            try:
                with conn.cursor() as cur:
                    cur.execute("SELECT 1")
                    cur.fetchone()
            finally:
                conn.close()
        checks.append({"name": "connect", "passed": True, "detail": f"Connected to {host}:{port}/{database}"})
        checks.append({"name": "selected_databases", "passed": True, "detail": ", ".join(selected_databases)})
        return {"ok": True, "message": f"{engine_name} source is reachable", "checks": checks}
    except Exception as exc:
        checks.append({"name": "connect", "passed": False, "detail": str(exc).replace(password, "***") if password else str(exc)})
        return {"ok": False, "message": f"{engine_name} source validation failed", "checks": checks}


def _validate_ebs_source(source: Source) -> dict[str, Any]:
    source_settings = source.settings or {}
    region = str(source_settings.get("region", "us-east-1")).strip() or "us-east-1"
    volume_id = str(source_settings.get("volume_id", "")).strip()
    if not volume_id:
        raise HTTPException(status_code=400, detail="ebs source requires settings.volume_id")

    checks: list[dict[str, Any]] = []
    try:
        client = boto3.client("ec2", region_name=region, **_load_secret(str(source_settings.get("secret_ref", ""))))
        response = client.describe_volumes(VolumeIds=[volume_id])
        found = bool(response.get("Volumes"))
        checks.append({"name": "volume_exists", "passed": found, "detail": volume_id})
        return {"ok": found, "message": "EBS source validated" if found else "EBS source not found", "checks": checks}
    except Exception as exc:
        checks.append({"name": "volume_exists", "passed": False, "detail": str(exc)})
        return {"ok": False, "message": "EBS source validation failed", "checks": checks}


def _validate_rds_source(source: Source) -> dict[str, Any]:
    source_settings = source.settings or {}
    region = str(source_settings.get("region", "us-east-1")).strip() or "us-east-1"
    instance_id = str(source_settings.get("db_instance_identifier", "")).strip()
    cluster_id = str(source_settings.get("db_cluster_identifier", "")).strip()
    if not instance_id and not cluster_id:
        raise HTTPException(status_code=400, detail="rds source requires db_instance_identifier or db_cluster_identifier")

    checks: list[dict[str, Any]] = []
    try:
        client = boto3.client("rds", region_name=region, **_load_secret(str(source_settings.get("secret_ref", ""))))
        if instance_id:
            response = client.describe_db_instances(DBInstanceIdentifier=instance_id)
            found = bool(response.get("DBInstances"))
            checks.append({"name": "instance_exists", "passed": found, "detail": instance_id})
        else:
            response = client.describe_db_clusters(DBClusterIdentifier=cluster_id)
            found = bool(response.get("DBClusters"))
            checks.append({"name": "cluster_exists", "passed": found, "detail": cluster_id})
        return {"ok": found, "message": "RDS source validated" if found else "RDS source not found", "checks": checks}
    except Exception as exc:
        checks.append({"name": "rds_exists", "passed": False, "detail": str(exc)})
        return {"ok": False, "message": "RDS source validation failed", "checks": checks}


def _validate_source_connection(source: Source) -> dict[str, Any]:
    source_type = source.source_type.value
    if source_type == "s3":
        source_settings = source.settings or {}
        bucket = str(source_settings.get("bucket", "")).strip()
        if not bucket:
            raise HTTPException(status_code=400, detail="S3 source requires settings.bucket")

        _validate_sse_config("source", _source_encryption(source))

        client = _make_s3_client(
            source_settings.get("region", "us-east-1"),
            source_settings.get("endpoint", ""),
            _load_secret(str(source_settings.get("secret_ref", ""))),
        )

        try:
            client.head_bucket(Bucket=bucket)
        except NoCredentialsError:
            raise HTTPException(status_code=400, detail="source credentials could not be resolved")
        except ClientError as exc:
            code = exc.response.get("Error", {}).get("Code", "")
            raise HTTPException(status_code=400, detail=f"source validation failed: {code or str(exc)}")

        return {"ok": True, "message": f"Source bucket {bucket} is reachable", "checks": [{"name": "bucket", "passed": True, "detail": bucket}]}

    if source_type == "mysql":
        return _validate_db_source(source, "mysql")

    if source_type == "postgresql":
        return _validate_db_source(source, "postgresql")

    if source_type == "file":
        return _validate_file_source(source)

    if source_type == "ebs":
        return _validate_ebs_source(source)

    if source_type == "rds":
        return _validate_rds_source(source)

    raise HTTPException(status_code=400, detail=f"Unsupported source type {source_type}")


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

    return {"ok": True, "message": f"Destination bucket {destination.bucket} is reachable", "checks": [{"name": "bucket", "passed": True, "detail": destination.bucket}]}


def _validate_schedule(schedule_cron: str) -> None:
    if schedule_cron and not croniter.is_valid(schedule_cron):
        raise HTTPException(status_code=422, detail="invalid schedule_cron expression")


def _ensure_source_type_enabled(source_type: SourceType) -> None:
    if source_type in TEMP_DISABLED_SOURCE_TYPES:
        raise HTTPException(
            status_code=503,
            detail=f"source type '{source_type.value}' is temporarily disabled",
        )


def _extract_job_id(message: str) -> str:
    if not message or "Queued (job " not in message:
        return ""
    return message.removeprefix("Queued (job ").removesuffix(")")


def _get_run_job_id(run: BackupRun) -> str:
    return (run.queue_job_id or _extract_job_id(run.message) or "").strip()


def _enqueue_run(run: BackupRun, q: Queue) -> BackupRun:
    retry = Retry(max=max(settings.max_retries, 0), interval=[60, 300, 900]) if settings.max_retries > 0 else None
    job = q.enqueue(
        "tasks.run_backup_job",
        run.id,
        retry=retry,
        job_timeout=settings.rq_job_timeout,
    )
    run.queue_job_id = job.id
    run.message = f"Queued (job {job.id})"
    run.max_attempts = max(settings.max_retries, 0) + 1
    return run


@app.get("/health")
def health() -> dict[str, str]:
    return {"status": "ok"}


@app.post("/auth/login", response_model=LoginResponse)
def login(payload: LoginRequest, db: Session = Depends(get_db)) -> LoginResponse:
    user = db.query(User).filter(User.username == payload.username).one_or_none()
    # Always run verify_password, even for an unknown username, using a dummy hash
    # so the response time does not reveal whether the username exists.
    password_hash = user.password_hash if user is not None else DUMMY_PASSWORD_HASH
    password_ok = verify_password(payload.password, password_hash)
    if user is None or not password_ok:
        raise HTTPException(status_code=401, detail="invalid username or password")
    token = create_session(db, user)
    return LoginResponse(token=token, username=user.username)


@app.post("/auth/logout")
def logout(request: Request, db: Session = Depends(get_db)) -> dict[str, bool]:
    header = request.headers.get("Authorization", "")
    if header.startswith("Bearer "):
        revoke_session(db, header[len("Bearer ") :].strip())
    return {"ok": True}


@app.get("/auth/me")
def me(request: Request) -> dict[str, str]:
    user = getattr(request.state, "user", None)
    if user is not None:
        return {"username": user.username}
    # Reached via a valid X-API-Key rather than a user session; there is no
    # individual user identity in that case.
    return {"username": ADMIN_USERNAME}


@app.post("/auth/change-password")
def change_password(payload: ChangePasswordRequest, db: Session = Depends(get_db)) -> dict[str, bool]:
    user = db.query(User).filter(User.username == ADMIN_USERNAME).one_or_none()
    if user is None:
        raise HTTPException(status_code=404, detail="admin user not found")
    if not verify_password(payload.current_password, user.password_hash):
        raise HTTPException(status_code=401, detail="current password is incorrect")
    user.password_hash = hash_password(payload.new_password)
    db.commit()
    return {"ok": True}


@app.get("/metrics")
def metrics() -> Any:
    return Response(content=generate_latest(), media_type=CONTENT_TYPE_LATEST)


@app.post("/destinations", response_model=DestinationRead)
def create_destination(payload: DestinationCreate, db: Session = Depends(get_db)) -> Destination:
    item = Destination(**payload.destination_kwargs())
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

    for key, value in payload.destination_kwargs().items():
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
    _ensure_source_type_enabled(payload.source_type)
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
    _ensure_source_type_enabled(payload.source_type)
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
    _ensure_source_type_enabled(source.source_type)
    if dest is None:
        raise HTTPException(status_code=404, detail="destination not found")

    _validate_schedule(payload.schedule_cron)

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
    _ensure_source_type_enabled(source.source_type)
    if dest is None:
        raise HTTPException(status_code=404, detail="destination not found")

    _validate_schedule(payload.schedule_cron)

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
def validate_source(source_id: int, db: Session = Depends(get_db)) -> dict[str, Any]:
    source = db.get(Source, source_id)
    if source is None:
        raise HTTPException(status_code=404, detail="source not found")
    _ensure_source_type_enabled(source.source_type)
    return _validate_source_connection(source)


@app.get("/validate/destination/{destination_id}")
def validate_destination(destination_id: int, db: Session = Depends(get_db)) -> dict[str, Any]:
    destination = db.get(Destination, destination_id)
    if destination is None:
        raise HTTPException(status_code=404, detail="destination not found")
    return _validate_destination_connection(destination)


@app.get("/validate/binding/{binding_id}")
def validate_binding(binding_id: int, db: Session = Depends(get_db)) -> dict[str, Any]:
    binding = db.get(Binding, binding_id)
    if binding is None:
        raise HTTPException(status_code=404, detail="binding not found")

    source = db.get(Source, binding.source_id)
    destination = db.get(Destination, binding.destination_id)
    if source is None:
        raise HTTPException(status_code=404, detail="source not found")
    _ensure_source_type_enabled(source.source_type)
    if destination is None:
        raise HTTPException(status_code=404, detail="destination not found")

    source_result = _validate_source_connection(source)
    destination_result = _validate_destination_connection(destination, binding)

    return {
        "ok": bool(source_result.get("ok")) and bool(destination_result.get("ok")),
        "source": source_result,
        "destination": destination_result,
        "message": "Binding is valid" if source_result.get("ok") and destination_result.get("ok") else "Binding validation failed",
    }


@app.post("/validate/source")
def validate_source_payload(payload: SourceCreate) -> dict[str, Any]:
    _ensure_source_type_enabled(payload.source_type)
    source = Source(**payload.model_dump())
    return _validate_source_connection(source)


@app.post("/sources/scan-databases")
def scan_source_databases(payload: SourceDatabaseScanRequest) -> dict[str, Any]:
    _ensure_source_type_enabled(payload.source_type)
    return _scan_db_databases(payload.source_type, payload.settings or {})


@app.post("/validate/destination")
def validate_destination_payload(payload: DestinationCreate) -> dict[str, Any]:
    destination = Destination(**payload.destination_kwargs())
    return _validate_destination_connection(destination)


@app.post("/validate/binding")
def validate_binding_payload(payload: BindingCreate, db: Session = Depends(get_db)) -> dict[str, Any]:
    _validate_schedule(payload.schedule_cron)

    source = db.get(Source, payload.source_id)
    destination = db.get(Destination, payload.destination_id)
    if source is None:
        raise HTTPException(status_code=404, detail="source not found")
    _ensure_source_type_enabled(source.source_type)
    if destination is None:
        raise HTTPException(status_code=404, detail="destination not found")

    binding = Binding(**payload.model_dump())
    source_result = _validate_source_connection(source)
    destination_result = _validate_destination_connection(destination, binding)

    return {
        "ok": bool(source_result.get("ok")) and bool(destination_result.get("ok")),
        "source": source_result,
        "destination": destination_result,
        "message": "Binding is valid" if source_result.get("ok") and destination_result.get("ok") else "Binding validation failed",
    }


@app.post("/runs/trigger/{binding_id}", response_model=RunRead)
def trigger_run(binding_id: int, db: Session = Depends(get_db)) -> BackupRun:
    binding = db.get(Binding, binding_id)
    if binding is None:
        raise HTTPException(status_code=404, detail="binding not found")

    source = db.get(Source, binding.source_id)
    if source is None:
        raise HTTPException(status_code=404, detail="source not found")
    _ensure_source_type_enabled(source.source_type)

    run = BackupRun(
        binding_id=binding_id,
        status=RunStatus.queued,
        started_at=datetime.now(timezone.utc),
        message="Queued",
        attempts=0,
        max_attempts=max(settings.max_retries, 0) + 1,
    )
    db.add(run)
    db.commit()
    db.refresh(run)

    run = _enqueue_run(run, queue())
    db.commit()
    db.refresh(run)
    return run


def _cancel_run(run: BackupRun, q: Queue, db: Session) -> tuple[bool, str]:
    if run.status.value not in {"queued", "running"}:
        return False, f"run is {run.status.value}"

    job_id = _get_run_job_id(run)

    if not job_id:
        return False, "no queue job id found for this run"

    try:
        job = Job.fetch(job_id, connection=q.connection)
    except Exception:
        if run.status.value == "queued":
            return False, "queue job no longer exists"
        run.status = RunStatus.cancelled
        run.finished_at = datetime.now(timezone.utc)
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
        run.status = RunStatus.cancelled
        run.finished_at = datetime.now(timezone.utc)
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
        "attempts": run.attempts,
        "max_attempts": run.max_attempts,
        "artifact_ref": run.artifact_ref,
    }


@app.get("/topology")
def topology(db: Session = Depends(get_db)) -> dict[str, Any]:
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
