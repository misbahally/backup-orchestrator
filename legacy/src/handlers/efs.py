"""
AWS EFS backup handler.
Uses AWS DataSync for cheap intra-AWS transfers, then rclone for the final hop.
"""
from __future__ import annotations

import json
import subprocess
import time
from datetime import datetime, timezone
from typing import Any

import boto3

from src.config import AWS_REGION, RCLONE_REMOTE
from src.handlers.base import BackupHandler
from src.metrics import OPERATION_DURATION


class EFSBackupHandler(BackupHandler):
    source_type = "efs"

    def __init__(self, destination: str):
        super().__init__(destination)
        self.datasync = boto3.client("datasync", region_name=AWS_REGION)
        self.s3 = boto3.client("s3", region_name=AWS_REGION)

    def list_backups(self) -> list[dict[str, Any]]:
        """List DataSync task executions (historical backups)."""
        # If no task ARN is configured, fall back to describing all tasks
        tasks = []
        try:
            resp = self.datasync.list_tasks()
            tasks = resp.get("Tasks", [])
        except Exception:
            pass
        backups = []
        for task in tasks:
            arn = task["TaskArn"]
            resp = self.datasync.list_task_executions(TaskArn=arn)
            for exec_item in resp.get("TaskExecutions", []):
                backups.append({
                    "id": exec_item["TaskExecutionArn"],
                    "created_at": parse_timestamp(exec_item.get("StartedAt", "")),
                    "status": exec_item.get("Status", "UNKNOWN"),
                    "task_arn": arn,
                })
        return backups

    def create_backup(self, fs_id: str, **kwargs) -> dict[str, Any]:
        """
        Trigger a DataSync task for the EFS file system.
        The task must be pre-created with source EFS and an intermediate S3 location.
        """
        task_arn = kwargs.get("data_sync_task_arn") or self._get_or_create_task(fs_id)
        resp = self.datasync.start_task_execution(TaskArn=task_arn)
        exec_arn = resp["TaskExecutionArn"]

        # Poll until completion (max 30 min for large file systems)
        status = self._wait_for_execution(exec_arn)
        if status != "SUCCESS":
            raise RuntimeError(f"DataSync execution {exec_arn} ended with status: {status}")

        # Extract S3 location written by DataSync
        location = self._get_execution_location(exec_arn)
        return {
            "id": exec_arn,
            "s3_location": location,
            "fs_id": fs_id,
        }

    def copy_to_destination(self, backup_id: str, dest_prefix: str, **kwargs) -> int:
        """
        rclone copies the DataSync S3 output to the external S3-compatible destination.
        Uses copy (not sync) so source is never modified.
        """
        s3_loc = kwargs.get("s3_location", "")
        if not s3_loc:
            raise ValueError("s3_location not returned from create_backup")

        src = self._parse_s3_uri(s3_loc)
        dest = f"{RCLONE_REMOTE}/{dest_prefix}"

        # --copy is incremental: only new/changed bytes cross the wire
        cmd = [
            "rclone", "copy",
            f"s3:{src['bucket']}/{src['key']}",
            dest,
            "--s3-provider", "aws",
            "--s3-region", AWS_REGION,
            "--transfers", "4",
            "--checkers", "8",
            "--log-level", "ERROR",
            "--no-check-destination",
        ]
        completed = subprocess.run(
            cmd, capture_output=True, text=True, timeout=7200
        )
        if completed.returncode != 0:
            raise RuntimeError(f"rclone failed: {completed.stderr}")

        # Estimate bytes transferred (rclone doesn't make this easy; use listing)
        return self._estimate_copy_bytes(src, dest_prefix)

    def _estimate_copy_bytes(self, src: dict, dest_prefix: str) -> int:
        """Sum object sizes in the S3 prefix as a rough byte count."""
        try:
            resp = self.s3.list_objects_v2(
                Bucket=src["bucket"],
                Prefix=src["key"],
            )
            return sum(obj["Size"] for obj in resp.get("Contents", []))
        except Exception:
            return 0

    # ── helpers ─────────────────────────────────────────────────────────────────

    def _get_or_create_task(self, fs_id: str) -> str:
        """Stub: in production, tasks are pre-provisioned via Terraform/CloudFormation."""
        raise NotImplementedError(
            "DataSync tasks must be created beforehand. "
            "Set DATA_SYNC_TASK_ARN in the environment."
        )

    def _wait_for_execution(self, exec_arn: str, timeout: int = 1800) -> str:
        for _ in range(timeout // 15):
            resp = self.datasync.describe_task_execution(TaskExecutionArn=exec_arn)
            status = resp["TaskExecution"]["Status"]
            if status in ("SUCCESS", "ERROR"):
                return status
            time.sleep(15)
        raise TimeoutError(f"DataSync execution {exec_arn} did not complete in {timeout}s")

    def _get_execution_location(self, exec_arn: str) -> str:
        resp = self.datasync.describe_task_execution(TaskExecutionArn=exec_arn)
        src = resp["TaskExecution"]["SourceLocation"]
        return f"s3://{src['S3Bucket']}/{src.get('S3Prefix', '')}"

    @staticmethod
    def _parse_s3_uri(uri: str) -> dict:
        # handles: s3://bucket/prefix or s3:bucket/prefix
        uri = uri.replace("s3:/", "")
        parts = uri.split("/", 1)
        return {"bucket": parts[0], "key": parts[1] if len(parts) > 1 else ""}


# re-export
parse_timestamp = lambda v: datetime.fromisoformat(
    v.replace("Z", "+00:00") if isinstance(v, str) else str(v)
)
