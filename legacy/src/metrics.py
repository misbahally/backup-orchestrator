"""
Prometheus metrics — one registry, no per-run state leaks.
"""
from __future__ import annotations

import time
from prometheus_client import Counter, Gauge, Histogram, Info, start_http_server

# ── Counters ───────────────────────────────────────────────────────────────────

BACKUP_SUCCESS = Counter(
    "backup_operations_total",
    "Total successful backup operations",
    ["source_type", "source_id", "destination"],
)

BACKUP_FAILURE = Counter(
    "backup_failures_total",
    "Total failed backup operations",
    ["source_type", "source_id", "destination", "error_class"],
)

UPLOAD_BYTES = Counter(
    "backup_uploaded_bytes_total",
    "Total bytes uploaded to destination",
    ["source_type", "destination"],
)

# ── Gauges ─────────────────────────────────────────────────────────────────────

LAST_SUCCESS_EPOCH = Gauge(
    "backup_last_success_timestamp",
    "Unix epoch of last successful backup per source",
    ["source_type", "source_id"],
)

SNAPSHOT_AGE_HOURS = Gauge(
    "snapshot_age_hours",
    "Age of the most recent snapshot in hours",
    ["source_type", "source_id"],
)

DESTINATION_AVAILABLE = Gauge(
    "destination_available",
    "Whether the destination is reachable (1=yes, 0=no)",
    ["destination"],
)

# ── Histograms ────────────────────────────────────────────────────────────────

OPERATION_DURATION = Histogram(
    "backup_operation_duration_seconds",
    "Time spent on each backup operation",
    ["source_type", "phase"],
    buckets=[1, 5, 15, 30, 60, 120, 300, 600, 1800, 3600],
)

# ── Info ────────────────────────────────────────────────────────────────────────

APP_INFO = Info("backup_orchestrator", "Backup orchestrator build info")
APP_INFO.info({"version": "1.0.0", "orchestrator": "standalone"})


def start_metrics_server(port: int) -> None:
    """Start the Prometheus HTTP server in a background thread."""
    start_http_server(port)
    print(f"[metrics] Prometheus server listening on :{port}")
