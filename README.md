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

## Database Source Least Privileges (Scan + Dump)

For MySQL and PostgreSQL sources, create a dedicated backup user with the minimum
permissions needed for:

- Scanning database names in the UI/API (Scan Databases)
- Running logical dumps in the worker

### PostgreSQL

Scan databases:

- CONNECT on the probe database used for scan (defaults to postgres if settings.database is empty)
- Ability to read pg_database (default installs usually allow this; hardening may restrict it)

Dump selected databases:

- CONNECT on each selected database
- USAGE on schemas that contain objects to back up
- SELECT on tables/views/materialized views to be dumped
- SELECT on sequences used by dumped tables

Notes:

- Superuser is not required.
- If scan returns fewer databases than expected, permissions are likely restricted by policy.

Example (adjust DB names/schemas/host/user):

```sql
-- Run as an admin role.
CREATE ROLE backup_user LOGIN PASSWORD 'REPLACE_WITH_STRONG_PASSWORD';

-- Optional but recommended hardening.
ALTER ROLE backup_user NOSUPERUSER NOCREATEDB NOCREATEROLE NOREPLICATION;

-- Scan support: allow connecting to the probe DB used by scan.
GRANT CONNECT ON DATABASE postgres TO backup_user;

-- Repeat per database you plan to dump.
GRANT CONNECT ON DATABASE app_db TO backup_user;
\c app_db

-- Replace public with your actual schema(s).
GRANT USAGE ON SCHEMA public TO backup_user;
GRANT SELECT ON ALL TABLES IN SCHEMA public TO backup_user;
GRANT SELECT ON ALL SEQUENCES IN SCHEMA public TO backup_user;

-- Ensure future objects remain dump-readable.
ALTER DEFAULT PRIVILEGES IN SCHEMA public
GRANT SELECT ON TABLES TO backup_user;
ALTER DEFAULT PRIVILEGES IN SCHEMA public
GRANT SELECT ON SEQUENCES TO backup_user;
```

### MySQL / MariaDB

Scan databases:

- Permission to run SHOW DATABASES (or per-database visibility via granted privileges)

Dump selected databases (current worker uses mysqldump default behavior):

- SELECT on objects to be dumped
- SHOW VIEW for views
- TRIGGER for triggers
- LOCK TABLES on dumped databases (required by mysqldump default locking behavior)

Notes:

- Global admin privileges are not required.
- If you want to avoid LOCK TABLES, worker dump flags would need to change (for example single-transaction strategy for InnoDB).

Example (adjust host/db/user):

```sql
-- Run as an admin user.
CREATE USER 'backup_user'@'10.%' IDENTIFIED BY 'REPLACE_WITH_STRONG_PASSWORD';

-- Optional scan support for SHOW DATABASES.
GRANT SHOW DATABASES ON *.* TO 'backup_user'@'10.%';

-- Repeat per database you plan to dump.
GRANT SELECT, SHOW VIEW, TRIGGER, LOCK TABLES ON app_db.* TO 'backup_user'@'10.%';
GRANT SELECT, SHOW VIEW, TRIGGER, LOCK TABLES ON analytics_db.* TO 'backup_user'@'10.%';

FLUSH PRIVILEGES;
```

If you prefer host-specific access, use 'backup_user'@'db-backup-worker-host' instead of a wildcard host.

### Practical Guidance

- Grant privileges only on databases you actually plan to back up.
- Use a separate read-only backup account per environment.
- Rotate credentials and store them via source settings secret_ref rather than hardcoding.

## High-Level Data Model

- `destinations`: S3-compatible destination definitions
- `sources`: backup source definitions (S3/MySQL/PostgreSQL/File/EBS/RDS)
- `bindings`: source to destination configuration and schedule
- `backup_runs`: run history and status
