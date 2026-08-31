# API usage

The control plane API is the main interface for configuring and running backups.

## Base URL

When running locally, the API is available at:

```text
http://localhost:8000
```

The interactive Swagger UI is available at:

```text
http://localhost:8000/docs
```

If `API_KEYS` is set, include the header below in every request:

```text
X-API-Key: <your-key>
```

If `API_KEYS` is not set, the API instead requires a signed-in session. Log in and use the
returned token as a bearer token on subsequent requests:

```bash
curl -X POST http://localhost:8000/auth/login \
  -H 'Content-Type: application/json' \
  -d '{"username":"admin","password":"admin"}'

curl -H 'Authorization: Bearer <token from login>' http://localhost:8000/topology
```

There is only a single built-in user, `admin`, seeded with the password `admin`. Change it
immediately after first login via `POST /auth/change-password` (or the Settings dialog in the
web UI).

## Health check

```bash
curl http://localhost:8000/health
```

Example response:

```json
{"status": "ok"}
```

## Worker registration

Workers register automatically on startup using the `WORKER_REGISTRATION_TOKEN` environment
variable. The token is shared between the API and all worker containers; each worker receives
a unique per-worker bearer token in return.

To register a worker manually (useful for scripting or debugging):

```bash
curl -X POST http://localhost:8000/workers/register \
  -H 'Content-Type: application/json' \
  -H 'X-Worker-Registration-Token: <WORKER_REGISTRATION_TOKEN>' \
  -d '{"name": "worker-1"}'
```

Example response:

```json
{"worker_id": 1, "token": "<per-worker-token>", "name": "worker-1"}
```

Re-registering an existing name rotates its token (useful for credential rotation).

## Create a source

The source represents the backup origin. The current implementation supports `s3`,
`mysql`, `postgresql`, `file`, `ebs`, and `rds`.

Every source must be assigned to an active worker via `worker_id`. The worker will
claim and execute any runs triggered for bindings that reference this source.

To find the ID of a registered worker:

```bash
curl -H 'X-API-Key: dev-key' http://localhost:8000/workers
```

```bash
curl -X POST http://localhost:8000/sources \
  -H 'Content-Type: application/json' \
  -H 'X-API-Key: dev-key' \
  -d '{
    "name": "my-source",
    "source_type": "s3",
    "settings": {
      "bucket": "source-bucket",
      "region": "us-east-1",
      "endpoint": "http://host.docker.internal:9000",
      "secret_ref": "local-minio"
    },
    "worker_id": 1,
    "is_active": true
  }'
```

## Create a file source

```bash
curl -X POST http://localhost:8000/sources \
  -H 'Content-Type: application/json' \
  -H 'X-API-Key: dev-key' \
  -d '{
    "name": "local-files",
    "source_type": "file",
    "settings": {
      "root_path": "/data",
      "include_globs": ["**/*.sql", "**/*.gz"],
      "exclude_globs": ["**/*.tmp"],
      "follow_symlinks": false,
      "key_prefix": "file/local-files"
    },
    "worker_id": 1,
    "is_active": true
  }'
```

## Create a destination

```bash
curl -X POST http://localhost:8000/destinations \
  -H 'Content-Type: application/json' \
  -H 'X-API-Key: dev-key' \
  -d '{
    "name": "my-destination",
    "provider": "s3-compatible",
    "endpoint": "http://host.docker.internal:9000",
    "bucket": "backup-bucket",
    "region": "us-east-1",
    "secret_ref": "local-minio",
    "encryption": {},
    "is_active": true
  }'
```

## Create a binding

A binding connects a source to a destination and defines the schedule.

```bash
curl -X POST http://localhost:8000/bindings \
  -H 'Content-Type: application/json' \
  -H 'X-API-Key: dev-key' \
  -d '{
    "source_id": 1,
    "destination_id": 1,
    "schedule_cron": "0 2 * * *",
    "policy": {},
    "is_active": true
  }'
```

## Validate configuration

You can validate a source, destination, or binding before triggering a run.

```bash
curl -H 'X-API-Key: dev-key' http://localhost:8000/validate/source/1
curl -H 'X-API-Key: dev-key' http://localhost:8000/validate/destination/1
curl -H 'X-API-Key: dev-key' http://localhost:8000/validate/binding/1
```

## Trigger a run

```bash
curl -X POST -H 'X-API-Key: dev-key' http://localhost:8000/runs/trigger/1
```

## List and inspect runs

```bash
curl -H 'X-API-Key: dev-key' http://localhost:8000/runs
curl -H 'X-API-Key: dev-key' http://localhost:8000/runs/1
```

## Cancel a run

```bash
curl -X POST -H 'X-API-Key: dev-key' http://localhost:8000/runs/1/cancel
```

## Topology view

The topology endpoint returns nodes and edges for the web UI.

```bash
curl -H 'X-API-Key: dev-key' http://localhost:8000/topology

## Metrics

```bash
curl http://localhost:8000/metrics
```
```

## Notes

- Every source must be assigned to a registered, active worker via `worker_id`. The scheduler
  will skip bindings whose source has no assigned worker, and queued runs will remain in `queued`
  until that worker connects and claims them.
- Cancelling a `queued` run sets it to `cancelled` immediately. Cancelling a `running` run
  sets `cancel_requested`; the worker observes this flag between transfer phases and stops
  cooperatively.
- EFS and `other` are no longer valid source types.
- EBS and RDS are temporarily disabled and return HTTP 503 when used.
- For EFS workloads, mount EFS into the worker and use a `file` source.
- Secrets are resolved through the shared secret resolution helper in `libs/orchestrator_core`.

## Worker endpoints

Workers are managed automatically by the worker container, but you can inspect and
manage them via the API:

```bash
# List all workers with online/offline status
curl -H 'X-API-Key: dev-key' http://localhost:8000/workers

# Deactivate a worker
curl -X PUT http://localhost:8000/workers/1 \
  -H 'Content-Type: application/json' \
  -H 'X-API-Key: dev-key' \
  -d '{"is_active": false}'
```

To run a second worker, uncomment the `worker-2` block in `docker-compose.yml` and set
`WORKER_NAME=worker-2` (or any unique name) in its environment.
