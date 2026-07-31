# Backup Control Plane (Work in Progress)

Service-oriented scaffold for a backup control plane:

- `apps/api`: FastAPI control plane API (DB-backed config and run orchestration)
- `apps/worker`: RQ worker that executes queued backup runs
- `apps/web`: Separate frontend that visualizes source -> destination mappings
- `legacy/`: previous LLM-generated implementation kept for reference only

Each app now has its own Poetry environment:
- `apps/api/pyproject.toml`
- `apps/worker/pyproject.toml`

## Services

- `postgres`: persistent metadata store
- `redis`: job queue broker
- `minio`: local S3-compatible object storage for testing
- `api`: REST API for configuration and run control
- `worker`: async execution worker
- `web`: static web UI for topology and config visibility

## Quick Start

1. Copy environment template:

```bash
cp .env.example .env
```

2. Install each service with Poetry using Python 3.13:

```bash
cd apps/api && poetry env use 3.13 && poetry install --no-root
cd ../worker && poetry env use 3.13 && poetry install --no-root
```

3. Start all services:

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

## Current Scope

This scaffold provides:

- DB-backed source/destination/binding configuration
- Job queue integration with run state transitions
- Topology visualization in a separate frontend service
- Shared secret resolution for API and worker
- Structured validation diagnostics and run detail inspection
- Cron-aware scheduler entry point for active bindings
- API key authentication support
- Prometheus metrics endpoints for API/worker/scheduler
- Source plugins for S3 copy, MySQL/PostgreSQL logical dump, filesystem copy, EBS snapshots, and RDS snapshots

## High-Level Data Model

- `destinations`: S3-compatible destination definitions
- `sources`: backup source definitions (S3/MySQL/PostgreSQL/File/EBS/RDS)
- `bindings`: source to destination configuration and schedule
- `backup_runs`: run history and status

## Legacy Code

Previous code is preserved in `legacy/` and is not used by the new stack.
