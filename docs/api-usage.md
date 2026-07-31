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

## Create a source

The source represents the backup origin. The current implementation supports `s3`,
`mysql`, `postgresql`, `file`, `ebs`, and `rds`.

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

- EFS and `other` are no longer valid source types.
- EBS and RDS are temporarily disabled and return HTTP 503 when used.
- For EFS workloads, mount EFS into the worker and use a `file` source.
- Secrets are resolved through the shared secret resolution helper used by both the API and worker.
