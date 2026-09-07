# Backup Orchestrator Documentation

This directory contains operational guides for the backup control plane.

## What this project does

The backup orchestrator is a Docker-based control plane for defining backup sources,
destinations, and bindings, then executing backup runs through registered workers.

The API and worker services each use their own Poetry environment under [apps/api](../apps/api)
and [apps/worker](../apps/worker), both targeting Python 3.13 for compatibility with the
current dependency stack.

## Documentation map

- [Getting started](./getting-started.md) — install prerequisites, start the stack, and perform your first backup run.
- [API usage](./api-usage.md) — register workers, create sources/destinations/bindings, and trigger or monitor runs.

## Main components

- API: exposes configuration, validation, run management, and worker endpoints; hosts the
  cron scheduler and stale-run reaper as asyncio background tasks
- Worker: registers with the API on startup, then polls to claim and execute backup runs;
  communicates exclusively over HTTP — no direct database or Redis access
- Web UI: topology view, configuration (including Workers tab), and run operations
- MinIO: local S3-compatible storage for development and testing

## How workers connect

Each worker container presents a shared `WORKER_REGISTRATION_TOKEN` to `POST /workers/register`
and receives a per-worker bearer token. It then polls `POST /workers/runs/claim` to atomically
claim queued runs that are pinned to it via their source’s `worker_id`. Multiple workers can
run simultaneously; each only claims runs for its own sources.

## Quick links

- Web UI: http://localhost:8080
- API docs: http://localhost:8001/docs
- MinIO Console: http://localhost:9001
