# Getting started

## Prerequisites

Before you begin, make sure you have:

- Docker and Docker Compose installed
- A working shell with access to the repository
- Optional: the AWS CLI if you want to test against MinIO manually

## 1. Configure environment variables

Create a local environment file from the project template if it exists:

```bash
cp .env.example .env
```

If you are using the defaults in the compose file, the stack will start with:

- PostgreSQL on port 5432
- Redis on port 6379
- MinIO on ports 9000 and 9001
- API on port 8000
- Web UI on port 8080

Set these important variables in `.env` before startup:

- `API_KEYS` (recommended in any non-local environment)
- `FILE_SOURCE_ALLOWED_ROOTS` (paths that file sources are allowed to back up)

## 2. Prepare the app environments

Create the app-specific Poetry environments with Python 3.13 before starting the stack:

```bash
cd apps/api && poetry env use 3.13 && poetry install --no-root
cd ../worker && poetry env use 3.13 && poetry install --no-root
```

## 3. Start the stack

```bash
docker compose up --build
```

The `migrate` one-shot service runs `alembic upgrade head` before API/worker/scheduler start.

After the services are running, open:

- Web UI: http://localhost:8080
- API docs: http://localhost:8000/docs
- MinIO Console: http://localhost:9001

## 4. Test with MinIO

MinIO is useful for local development and smoke testing.

1. Start MinIO if it is not already running:

```bash
docker compose up -d minio
```

2. Sign in to the MinIO Console with the default credentials:

- Username: `minioadmin`
- Password: `minioadmin`

3. Create a bucket such as `backups`.

4. Optionally upload a test object:

```bash
export AWS_ACCESS_KEY_ID=minioadmin
export AWS_SECRET_ACCESS_KEY=minioadmin
export AWS_DEFAULT_REGION=us-east-1

echo "hello" > /tmp/minio-test.txt
aws --endpoint-url http://localhost:9000 s3 cp /tmp/minio-test.txt s3://backups/minio-test.txt
```

## 5. Create your first backup configuration

The workflow is:

1. Create a source
2. Create a destination
3. Create a binding between them
4. Trigger a backup run

The API endpoints for this workflow are documented in [API usage](./api-usage.md).

## 6. Monitor backup runs

You can inspect runs through the API or the web UI. The worker will update run status from queued to running to success or failed.

Useful endpoints:

- `GET /runs`
- `GET /runs/{run_id}`
- `POST /runs/trigger/{binding_id}`
- `POST /runs/{run_id}/cancel`

## 7. File source setup

When using source type `file`, mount host directories into the worker and API
containers and keep source `settings.root_path` inside `FILE_SOURCE_ALLOWED_ROOTS`.

Example compose mount:

```yaml
services:
	worker:
		volumes:
			- ./data:/data:ro
	api:
		volumes:
			- ./data:/data:ro
```
