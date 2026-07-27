# Backup Orchestrator Documentation

This directory contains operational guides for the backup control plane.

## What this project does

The backup orchestrator is a Docker-based control plane for defining backup sources, destinations, and bindings, then executing backup runs through a worker queue.

## Documentation map

- [Getting started](./getting-started.md) — install prerequisites, start the stack, and perform your first backup run.
- [API usage](./api-usage.md) — create sources, destinations, bindings, and trigger or monitor runs through the API.

## Main components

- API: exposes configuration, validation, and run management endpoints
- Worker: executes queued backup jobs
- Scheduler: periodically evaluates active bindings
- Web UI: simple topology view for sources and destinations
- MinIO: local S3-compatible storage for development and testing

## Quick links

- Web UI: http://localhost:8080
- API docs: http://localhost:8000/docs
- MinIO Console: http://localhost:9001
