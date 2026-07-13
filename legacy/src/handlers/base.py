"""
Base handler interface and shared utilities.
"""
from __future__ import annotations

import time
import uuid
from abc import ABC, abstractmethod
from datetime import datetime, timezone
from typing import Any

from src.metrics import (
    BACKUP_FAILURE,
    BACKUP_SUCCESS,
    LAST_SUCCESS_EPOCH,
    OPERATION_DURATION,
    SNAPSHOT_AGE_HOURS,
    UPLOAD_BYTES,
)


class BackupHandler(ABC):
    """Abstract base for all backup source handlers."""

    def __init__(self, destination: str):
        self.destination = destination
        self.run_id = str(uuid.uuid4())[:8]

    @property
    @abstractmethod
    def source_type(self) -> str:
        ...

    @abstractmethod
    def list_backups(self) -> list[dict[str, Any]]:
        """
        Return existing snapshots/backups for this source.
        Each dict must have: id, created_at (datetime), tags (dict).
        """
        ...

    @abstractmethod
    def create_backup(self, source_id: str, **kwargs) -> dict[str, Any]:
        """Create a new backup and return metadata (id, size_bytes, etc.)."""
        ...

    @abstractmethod
    def copy_to_destination(self, backup_id: str, dest_prefix: str, **kwargs) -> int:
        """
        Copy a completed backup to the external S3-compatible destination.
        Returns the number of bytes transferred.
        """
        ...

    def prune_local(self, backup_id: str, **kwargs) -> None:
        """Delete local/origin-side backup artifact. Override if needed."""
        pass

    def run(self, source_id: str, **kwargs) -> dict[str, Any]:
        """
        Full pipeline: create → copy → prune → record metrics.
        Returns a summary dict.
        """
        phase = "create"
        start = time.time()
        try:
            # 1. Create
            meta = self.create_backup(source_id, **kwargs)
            backup_id = meta["id"]

            with OPERATION_DURATION.labels(source_type=self.source_type, phase="copy").time():
                # 2. Copy to destination
                phase = "copy"
                bytes_transferred = self.copy_to_destination(
                    backup_id, dest_prefix=self._dest_prefix(source_id), **kwargs
                )

            # 3. Prune origin if desired
            phase = "prune"
            self.prune_local(backup_id, **kwargs)

            # 4. Metrics
            BACKUP_SUCCESS.labels(
                source_type=self.source_type,
                source_id=source_id,
                destination=self.destination,
            ).inc()
            UPLOAD_BYTES.labels(
                source_type=self.source_type,
                destination=self.destination,
            ).inc(bytes_transferred)
            LAST_SUCCESS_EPOCH.labels(
                source_type=self.source_type,
                source_id=source_id,
            ).set(time.time())
            SNAPSHOT_AGE_HOURS.labels(
                source_type=self.source_type,
                source_id=source_id,
            ).set(0.0)  # fresh

            elapsed = time.time() - start
            return {
                "run_id": self.run_id,
                "source_type": self.source_type,
                "source_id": source_id,
                "backup_id": backup_id,
                "bytes_transferred": bytes_transferred,
                "elapsed_seconds": round(elapsed, 1),
                "status": "success",
            }

        except Exception as exc:
            BACKUP_FAILURE.labels(
                source_type=self.source_type,
                source_id=source_id,
                destination=self.destination,
                error_class=exc.__class__.__name__,
            ).inc()
            elapsed = time.time() - start
            return {
                "run_id": self.run_id,
                "source_type": self.source_type,
                "source_id": source_id,
                "phase": phase,
                "elapsed_seconds": round(elapsed, 1),
                "status": "failed",
                "error": str(exc),
                "error_class": exc.__class__.__name__,
            }

    def _dest_prefix(self, source_id: str) -> str:
        ts = datetime.now(timezone.utc).strftime("%Y/%m/%d")
        return f"{self.source_type}/{source_id}/{ts}"


def parse_timestamp(value: Any) -> datetime:
    """Normalise various timestamp formats to datetime."""
    if isinstance(value, datetime):
        return value.replace(tzinfo=timezone.utc) if value.tzinfo is None else value
    if isinstance(value, str):
        return datetime.fromisoformat(value.replace("Z", "+00:00"))
    raise ValueError(f"Cannot parse timestamp: {value!r}")
