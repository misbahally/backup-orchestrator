# Backup Control Plane

A service-oriented backup control plane for defining backup sources, destinations, and
scheduled bindings, then executing and monitoring backup runs through a job queue.

- `apps/api`: FastAPI control plane API (DB-backed config and run orchestration)
- `apps/worker`: RQ worker and cron-aware scheduler that execute queued backup runs
- `apps/web`: static frontend that visualizes source -> destination mappings and run status
- `libs/orchestrator_core`: shared SQLAlchemy models, database helpers, and secret
  resolution used by both `apps/api` and `apps/worker`

Each app has its own Poetry environment:
- `apps/api/pyproject.toml`
- `apps/worker/pyproject.toml`

## Services

- `postgres`: persistent metadata store
- `redis`: job queue broker
- `minio`: local S3-compatible object storage for testing
- `migrate`: one-shot service that runs Alembic migrations before api/worker/scheduler start
- `api`: REST API for configuration and run control
- `worker`: async execution worker
- `scheduler`: cron-aware loop that enqueues runs for active bindings
- `web`: static web UI for topology and config visibility

## Quick Start

1. Copy environment template:

```bash
cp .env.example .env
```

2. (Optional, for local development outside Docker) install each app with Poetry using
   Python 3.13:

```bash
cd apps/api && poetry env use 3.13 && poetry install --no-root
cd ../worker && poetry env use 3.13 && poetry install --no-root
```

3. Start all services (builds images, runs migrations, then starts api/worker/scheduler/web):

```bash
docker compose up --build
```

4. Open:

- Web UI: `http://localhost:8080`
- API docs: `http://localhost:8000/docs`
- MinIO API: `http://localhost:9000`
- MinIO Console: `http://localhost:9001`

The web UI is protected by a login screen. Sign in with the default account (`admin` /
`admin`) and change the password from Settings → Change Password immediately after first login.

## Test with MinIO

Use MinIO as a local S3-compatible destination for development/testing.

1. Start MinIO (and its dependencies if needed):

```bash
docker compose up -d minio
```

2. Open the MinIO Console at `http://localhost:9001` and sign in with:

- Username: `minioadmin` (or `MINIO_ROOT_USER` from `.env`)
- Password: `minioadmin` (or `MINIO_ROOT_PASSWORD` from `.env`)

3. Create a bucket (example: `backups`) in the MinIO Console.

4. Quick upload test with AWS CLI (optional):

```bash
echo "minio test" > /tmp/minio-test.txt
aws --endpoint-url http://localhost:9000 s3 cp /tmp/minio-test.txt s3://backups/minio-test.txt
aws --endpoint-url http://localhost:9000 s3 ls s3://backups/
```

If your shell session does not already have credentials set, export these first:

```bash
export AWS_ACCESS_KEY_ID=minioadmin
export AWS_SECRET_ACCESS_KEY=minioadmin
export AWS_DEFAULT_REGION=us-east-1
```

## Features

- DB-backed source/destination/binding configuration with Alembic-managed schema
- Job queue integration (RQ/Redis) with retries and full run state transitions
- Cron-aware scheduler that enqueues due bindings and guards against duplicate runs
- Session + API key authentication for the API and web UI
- Prometheus metrics endpoints for API/worker/scheduler
- Topology visualization in the web UI
- Shared models, database access, and secret resolution via `libs/orchestrator_core`
- Structured validation diagnostics and run detail inspection
- Source plugins for S3 copy, MySQL/PostgreSQL logical dump, and filesystem copy
  (EBS snapshots and RDS snapshots are implemented but temporarily disabled — see
  [docs/api-usage.md](docs/api-usage.md))

See [docs/README.md](docs/README.md) for full documentation, including
[getting started](docs/getting-started.md) and [API usage](docs/api-usage.md) guides.

## High-Level Data Model

- `destinations`: S3-compatible destination definitions
- `sources`: backup source definitions (S3/MySQL/PostgreSQL/File/EBS/RDS)
- `bindings`: source to destination configuration and schedule
- `backup_runs`: run history and status
