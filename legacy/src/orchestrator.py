"""
Main orchestrator entry point.

Usage:
  python -m src.orchestrator run       # run all enabled backups
  python -m src.orchestrator status    # print last backup status (from in-memory state)
  python -m src.orchestrator list       # list existing backups in destination
"""
from __future__ import annotations

import json
import logging
import os
import sys
import time
import traceback
from datetime import datetime, timezone
from threading import Thread
from typing import Any

from src.config import AWS_REGION, DEST_ENDPOINT, SOURCES, METRICS_PORT
from src.destinations.rclone import DestinationHandler
from src.handlers import HANDLERS
from src.metrics import (
    APP_INFO,
    DESTINATION_AVAILABLE,
    LAST_SUCCESS_EPOCH,
    SNAPSHOT_AGE_HOURS,
    start_metrics_server,
)
from src.metrics import BACKUP_FAILURE  # noqa: F401 — exported for completeness

log = logging.getLogger("orchestrator")
logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(name)s — %(message)s",
    datefmt="%Y-%m-%dT%H:%M:%SZ",
)


# ── In-memory state (survives across calls in long-running mode) ──────────────

_state: dict[str, Any] = {
    "runs": [],
    "last_run": None,
}


# ── CLI ────────────────────────────────────────────────────────────────────────

def cli() -> None:
    if len(sys.argv) < 2 or sys.argv[1] == "run":
        run_all()
    elif sys.argv[1] == "status":
        print_status()
    elif sys.argv[1] == "list":
        list_dest_backups()
    else:
        print(f"Unknown command: {sys.argv[1]}", file=sys.stderr)
        sys.exit(1)


def run_all() -> None:
    """
    Run all enabled backup sources in sequence.
    In production, run this via a scheduled task (EventBridge, cron, or systemd timer).
    """
    global _state
    log.info("=== Backup Orchestrator starting ===")
    log.info("AWS region : %s", AWS_REGION)
    log.info("Destination: %s", DEST_ENDPOINT)

    # Check destination availability
    dest = DestinationHandler()
    if not dest.is_available():
        log.error("Destination is not reachable. Aborting.")
        sys.exit(1)

    results = []
    start_time = time.time()

    for source_type, cfg in SOURCES.items():
        if not cfg.get("enabled"):
            log.info("Skipping %s (not enabled)", source_type)
            continue

        handler_cls = HANDLERS.get(source_type)
        if not handler_cls:
            log.warning("No handler registered for source type: %s", source_type)
            continue

        handler = handler_cls(destination=DEST_ENDPOINT)

        # Resolve source IDs for this type
        source_ids = _resolve_source_ids(source_type, cfg)
        if not source_ids:
            log.warning("No source IDs found for %s. Skipping.", source_type)
            continue

        log.info("Running %s backups for %d source(s): %s",
                 source_type, len(source_ids), source_ids)

        for source_id in source_ids:
            result = _run_single(handler, source_type, source_id, cfg)
            results.append(result)
            _update_state_metrics(result)

    elapsed = time.time() - start_time
    _state["last_run"] = datetime.now(timezone.utc).isoformat()
    _state["runs"].append({
        "timestamp": _state["last_run"],
        "elapsed_seconds": round(elapsed, 1),
        "results": results,
    })

    _print_summary(results, elapsed)

    # Exit with error if any backup failed
    if any(r["status"] == "failed" for r in results):
        sys.exit(1)


def _run_single(
    handler: Any,
    source_type: str,
    source_id: str,
    cfg: dict[str, Any],
) -> dict[str, Any]:
    """Run one backup for one source ID. Handles its own exception trapping."""
    log.info("  → %s/%s: starting backup", source_type, source_id)
    t0 = time.time()
    try:
        # Pull out the handler-specific kwargs from config
        kwargs = {k: v for k, v in cfg.items() if k not in ("enabled", source_type)}
        result = handler.run(source_id, **kwargs)
    except Exception as exc:
        result = {
            "run_id": handler.run_id,
            "source_type": source_type,
            "source_id": source_id,
            "phase": "unknown",
            "status": "failed",
            "error": str(exc),
            "error_class": exc.__class__.__name__,
            "elapsed_seconds": round(time.time() - t0, 1),
        }
        log.error("  ✗ %s/%s: FAILED — %s: %s",
                  source_type, source_id, exc.__class__.__name__, exc)
        log.debug(traceback.format_exc())

    elapsed = time.time() - t0
    if result["status"] == "success":
        log.info("  ✓ %s/%s: OK  (%.0fs, %s bytes)",
                 source_type, source_id, elapsed,
                 result.get("bytes_transferred", 0))
    else:
        log.error("  ✗ %s/%s: FAILED after %.0fs — %s",
                  source_type, source_id, elapsed,
                  result.get("error", "unknown error"))

    return result


def _resolve_source_ids(source_type: str, cfg: dict[str, Any]) -> list[str]:
    """
    Return the explicit list of source IDs from config.
    Future: replace with auto-discovery (describe all EFS, list all buckets, etc.)
    """
    key_map = {
        "efs": "fs_ids",
        "s3":  "buckets",
        "ebs": "volume_ids",
        "rds": "db_identifiers",
    }
    key = key_map.get(source_type, "")
    ids = cfg.get(key, [])
    if ids:
        return ids

    # Auto-discovery fallback
    if source_type == "s3":
        import boto3
        s3 = boto3.client("s3", region_name=AWS_REGION)
        return [b["Name"] for b in s3.list_buckets().get("Buckets", [])]

    return []


def _update_state_metrics(result: dict[str, Any]) -> None:
    st = result["source_type"]
    sid = result["source_id"]
    if result["status"] == "success":
        LAST_SUCCESS_EPOCH.labels(st, sid).set(time.time())
        SNAPSHOT_AGE_HOURS.labels(st, sid).set(0.0)
    else:
        # Record the failure but leave last-success where it was
        pass


def _print_summary(results: list[dict[str, Any]], elapsed: float) -> None:
    n = len(results)
    ok = sum(1 for r in results if r["status"] == "success")
    fail = n - ok
    log.info("=== Backup Orchestrator finished in %.0fs ===", elapsed)
    log.info("  Total : %d", n)
    log.info("  OK    : %d", ok)
    log.info("  Failed: %d", fail)
    if fail:
        for r in results:
            if r["status"] == "failed":
                log.info("    ✗ %s/%s: %s", r["source_type"], r["source_id"], r.get("error", ""))

    # Prometheus scrape endpoint — start in background thread
    t = Thread(target=start_metrics_server, args=(METRICS_PORT,), daemon=True)
    t.start()


def print_status() -> None:
    print(json.dumps(_state, indent=2, default=str))


def list_dest_backups() -> None:
    dest = DestinationHandler()
    items = dest.list_backups()
    if not items:
        print("No backups found in destination.")
        return
    print(f"{'Name':<60} {'Size (bytes)':>15}  {'Modified'}")
    print("-" * 90)
    for item in items:
        print(f"{item['name']:<60} {item['size']:>15}  {item['iso']}")


if __name__ == "__main__":
    cli()
