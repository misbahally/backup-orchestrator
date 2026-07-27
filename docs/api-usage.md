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

## Health check

```bash
curl http://localhost:8000/health
```

Example response:

```json
{"status": "ok"}
```

## Create a source

The source represents the backup origin. The current implementation supports S3-compatible sources.

```bash
curl -X POST http://localhost:8000/sources \
  -H 'Content-Type: application/json' \
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

## Create a destination

```bash
curl -X POST http://localhost:8000/destinations \
  -H 'Content-Type: application/json' \
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
curl http://localhost:8000/validate/source/1
curl http://localhost:8000/validate/destination/1
curl http://localhost:8000/validate/binding/1
```

## Trigger a run

```bash
curl -X POST http://localhost:8000/runs/trigger/1
```

## List and inspect runs

```bash
curl http://localhost:8000/runs
curl http://localhost:8000/runs/1
```

## Cancel a run

```bash
curl -X POST http://localhost:8000/runs/1/cancel
```

## Topology view

The topology endpoint returns nodes and edges for the web UI.

```bash
curl http://localhost:8000/topology
```

## Notes

- The current worker implementation focuses on S3-to-S3 backup jobs.
- Other source types such as EFS, EBS, and RDS are defined in the schema but are not fully implemented yet.
- Secrets are resolved through the shared secret resolution helper used by both the API and worker.
