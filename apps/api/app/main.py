from datetime import datetime

from fastapi import Depends, FastAPI, HTTPException
from fastapi.middleware.cors import CORSMiddleware
from redis import Redis
from rq import Queue
from sqlalchemy.orm import Session

from .config import settings
from .database import Base, engine, get_db
from .models import BackupRun, Binding, Destination, RunStatus, Source
from .schemas import (
    BindingCreate,
    BindingRead,
    DestinationCreate,
    DestinationRead,
    RunRead,
    SourceCreate,
    SourceRead,
)

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


@app.post("/runs/trigger/{binding_id}", response_model=RunRead)
def trigger_run(binding_id: int, db: Session = Depends(get_db)) -> BackupRun:
    binding = db.get(Binding, binding_id)
    if binding is None:
        raise HTTPException(status_code=404, detail="binding not found")

    run = BackupRun(
        binding_id=binding_id,
        status=RunStatus.queued,
        started_at=datetime.utcnow(),
        message="Queued",
    )
    db.add(run)
    db.commit()
    db.refresh(run)

    q = queue()
    q.enqueue("tasks.run_backup_job", run.id)
    return run


@app.get("/runs", response_model=list[RunRead])
def list_runs(db: Session = Depends(get_db)) -> list[BackupRun]:
    return db.query(BackupRun).order_by(BackupRun.id.desc()).limit(100).all()


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
