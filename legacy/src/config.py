"""
Central configuration — single source of truth.
All values come from environment variables so the app stays stateless.
"""
from __future__ import annotations
import os
from dataclasses import dataclass, field
from typing import Literal

# ── Source AWS ─────────────────────────────────────────────────────────────────

AWS_REGION = os.environ["AWS_REGION"]  # e.g. us-east-1

# ── Destination (S3-compatible) ────────────────────────────────────────────────

DEST_PROVIDER: Literal["aws", "backblaze", "wasabi", "minio", "r2"] = os.environ.get(
    "DEST_PROVIDER", "backblaze"
)
DEST_ENDPOINT = os.environ["DEST_ENDPOINT"]          # e.g. https://s3.us-west-004.backblazeb2.com
DEST_REGION   = os.environ.get("DEST_REGION", "us-west-2")
DEST_BUCKET   = os.environ["DEST_BUCKET"]
DEST_ACCESS_KEY = os.environ["DEST_ACCESS_KEY"]
DEST_SECRET_KEY = os.environ["DEST_SECRET_KEY"]

# rclone config section name — set in ~/.config/rclone/rclone.conf on the host
RCLONE_REMOTE = f"{DEST_PROVIDER}:{DEST_BUCKET}"

# ── Backup sources ─────────────────────────────────────────────────────────────

SOURCES = {
    "efs": {
        "enabled":   os.environ.get("ENABLE_EFS", "false").lower() == "true",
        "fs_ids":    [f.strip() for f in os.environ.get("EFS_FS_IDS", "").split(",") if f.strip()],
        "data_sync_task_arn": os.environ.get("DATA_SYNC_TASK_ARN", ""),
    },
    "s3": {
        "enabled":   os.environ.get("ENABLE_S3", "false").lower() == "true",
        "buckets":   [b.strip() for b in os.environ.get("S3_BUCKETS", "").split(",") if b.strip()],
    },
    "ebs": {
        "enabled":      os.environ.get("ENABLE_EBS", "false").lower() == "true",
        "volume_ids":   [v.strip() for v in os.environ.get("EBS_VOLUME_IDS", "").split(",") if v.strip()],
        "snap_prefix":  os.environ.get("EBS_SNAP_PREFIX", "Backup"),
    },
    "rds": {
        "enabled":     os.environ.get("ENABLE_RDS", "false").lower() == "true",
        "db_identifiers": [d.strip() for d in os.environ.get("RDS_IDENTIFIERS", "").split(",") if d.strip()],
        "kms_key_id":  os.environ.get("RDS_KMS_KEY_ID", ""),
        "opt_in_chars": ["final", "stop"],
    },
}

# ── Snapshot retention ────────────────────────────────────────────────────────

RETENTION_DAYS = int(os.environ.get("RETENTION_DAYS", "30"))

# ── Schedule (cron expression, UTC) ───────────────────────────────────────────
# Default: daily at 02:00 UTC
SCHEDULE = os.environ.get("SCHEDULE", "0 2 * * *")

# ── Prometheus metrics port ────────────────────────────────────────────────────

METRICS_PORT = int(os.environ.get("METRICS_PORT", "9090"))
