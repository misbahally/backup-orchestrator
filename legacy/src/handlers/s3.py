"""
AWS S3 backup handler.
Performs direct rclone copy from source S3 bucket to the configured
S3-compatible destination, without an intermediate staging bucket.
"""
from __future__ import annotations

import subprocess
from datetime import datetime, timezone
from typing import Any

import boto3

from src.config import AWS_REGION, RCLONE_REMOTE
from src.handlers.base import BackupHandler


class S3BackupHandler(BackupHandler):
    source_type = "s3"

    def __init__(self, destination: str):
        super().__init__(destination)
        self.s3 = boto3.client("s3", region_name=AWS_REGION)

    def list_backups(self) -> list[dict[str, Any]]:
        """List available source buckets from AWS account."""
        try:
            resp = self.s3.list_buckets()
        except Exception:
            return []

        now = datetime.now(timezone.utc)
        return [
            {
                "id": b["Name"],
                "created_at": b.get("CreationDate", now),
                "status": "available",
                "bucket": b["Name"],
            }
            for b in resp.get("Buckets", [])
        ]

    def create_backup(self, bucket: str, **kwargs) -> dict[str, Any]:
        """Validate source bucket access before running direct copy."""
        self.s3.head_bucket(Bucket=bucket)
        run_id = datetime.now(timezone.utc).strftime("%Y%m%d-%H%M%S")
        return {"id": f"direct-{bucket}-{run_id}", "bucket": bucket}

    def copy_to_destination(self, backup_id: str, dest_prefix: str, **kwargs) -> int:
        """
        Direct rclone copy from source AWS S3 bucket to external destination.
        This path does not use a staging bucket.
        """
        bucket = kwargs.get("bucket", "")
        if not bucket:
            raise ValueError("bucket is required for S3 direct backup")
        dest = f"{RCLONE_REMOTE}/{dest_prefix}"

        cmd = [
            "rclone", "copy",
            f"s3:{bucket}",
            dest,
            "--s3-env-auth",
            "--s3-provider", "aws",
            "--s3-region", AWS_REGION,
            "--transfers", "4",
            "--checkers", "8",
            "--log-level", "ERROR",
            "--no-check-destination",
        ]
        completed = subprocess.run(
            cmd, capture_output=True, text=True, timeout=28800
        )
        if completed.returncode != 0:
            raise RuntimeError(f"rclone failed: {completed.stderr}")

        return self._sum_bucket_size(bucket)

    def _sum_bucket_size(self, bucket: str) -> int:
        total = 0
        paginator = self.s3.get_paginator("list_objects_v2")
        for page in paginator.paginate(Bucket=bucket):
            for obj in page.get("Contents", []):
                total += obj.get("Size", 0)
        return total
