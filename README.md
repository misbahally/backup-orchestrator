# Backup Control Plane (Work in Progress)

Service-oriented scaffold for a backup control plane:

- `apps/api`: FastAPI control plane API (DB-backed config and run orchestration)
- `apps/worker`: RQ worker that executes queued backup runs
- `apps/web`: Separate frontend that visualizes source -> destination mappings
- `legacy/`: previous LLM-generated implementation kept for reference only

## Services

- `postgres`: persistent metadata store
- `redis`: job queue broker
- `api`: REST API for configuration and run control
- `worker`: async execution worker
- `web`: static web UI for topology and config visibility

## Quick Start

1. Copy environment template:

```bash
cp .env.example .env
```

2. Start all services:

```bash
docker compose up --build
```

3. Open:

- Web UI: `http://localhost:8080`
- API docs: `http://localhost:8000/docs`

## Current Scope

This scaffold provides:

- DB-backed source/destination/binding configuration
- Job queue integration with run state transitions
- Topology visualization in a separate frontend service

It intentionally does **not** yet implement cloud-specific backup logic; that will be added as source plugins in the worker.

## High-Level Data Model

- `destinations`: S3-compatible destination definitions
- `sources`: backup source definitions (S3/EFS/EBS/RDS/other)
- `bindings`: source to destination configuration and schedule
- `backup_runs`: run history and status

## Legacy Code

Previous code is preserved in `legacy/` and is not used by the new stack.
