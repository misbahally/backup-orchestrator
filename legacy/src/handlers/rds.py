"""
AWS RDS backup handler.
Creates RDS automated backups (or snapshots for instance-type storage),
copies them to the destination, and manages retention.
"""
from __future__ import annotations

import subprocess
from datetime import datetime, timezone
from typing import Any

import boto3

from src.config import AWS_REGION, RETENTION_DAYS, RCLONE_REMOTE
from src.handlers.base import BackupHandler


class RDSBackupHandler(BackupHandler):
    source_type = "rds"

    def __init__(self, destination: str):
        super().__init__(destination)
        self.rds = boto3.client("rds", region_name=AWS_REGION)
        self.rdsd = boto3.client("rds-data", region_name=AWS_REGION)

    def list_backups(self) -> list[dict[str, Any]]:
        """List RDS automated backups and snapshots with the backup tag."""
        backups = []
        try:
            resp = self.rds.describe_db_snapshots(
                SnapshotType="automated",
                TagList=[{"Key": "Backup", "Value": "true"}],
            )
            for s in resp.get("DBSnapshots", []):
                backups.append({
                    "id": s["DBSnapshotIdentifier"],
                    "engine": s["Engine"],
                    "allocated_storage": s.get("AllocatedStorage", 0),
                    "created_at": s["SnapshotCreateTime"],
                    "status": s["Status"],
                    "tags": {},
                })
        except Exception:
            pass

        # Also check manual snapshots
        try:
            resp = self.rds.describe_db_snapshots(SnapshotType="manual")
            for s in resp.get("DBSnapshots", []):
                if s.get("TagList"):
                    tags = {t["Key"]: t["Value"] for t in s["TagList"]}
                    if tags.get("Backup") == "true":
                        backups.append({
                            "id": s["DBSnapshotIdentifier"],
                            "engine": s["Engine"],
                            "allocated_storage": s.get("AllocatedStorage", 0),
                            "created_at": s["SnapshotCreateTime"],
                            "status": s["Status"],
                            "tags": tags,
                        })
        except Exception:
            pass

        return backups

    def create_backup(self, db_identifier: str, **kwargs) -> dict[str, Any]:
        """
        Trigger a final snapshot for instance-type databases,
        or rely on automated backups for Provisioned IOPS.
        """
        kms_key_id = kwargs.get("kms_key_id", "")
        tags = kwargs.get("tags", [
            {"Key": "Backup", "Value": "true"},
            {"Key": "ManagedBy", "Value": "backup-orchestrator"},
        ])

        # Determine if this is a cluster (Aurora) or instance
        try:
            db = self.rds.describe_db_instances(DBInstanceIdentifier=db_identifier)["DBInstances"][0]
        except self.rds.exceptions.DBInstanceNotFoundFault:
            raise ValueError(f"DB instance {db_identifier} not found")

        if db["Engine"] in ("aurora", "aurora-mysql", "aurora-postgresql"):
            return self._create_aurora_snapshot(db_identifier, tags)
        else:
            return self._create_instance_snapshot(db_identifier, kms_key_id, tags)

    def _create_instance_snapshot(
        self, db_identifier: str, kms_key_id: str, tags: list[dict]
    ) -> dict[str, Any]:
        snap_id = f"{db_identifier}-{datetime.now(timezone.utc):%Y%m%d-%H%M}"
        kwargs = {
            "DBSnapshotIdentifier": snap_id,
            "DBInstanceIdentifier": db_identifier,
            "Tags": tags,
        }
        if kms_key_id:
            kwargs["KmsKeyId"] = kms_key_id

        resp = self.rds.create_db_snapshot(**kwargs)
        snap = resp["DBSnapshot"]

        # Wait for available
        self.rds.get_waiter("db_snapshot_available").wait(
            DBSnapshotIdentifier=snap_id
        )
        return {
            "id": snap_id,
            "engine": snap["Engine"],
            "allocated_storage": snap.get("AllocatedStorage", 0),
            "encrypted": snap["Encrypted"],
        }

    def _create_aurora_snapshot(self, cluster_id: str, tags: list[dict]) -> dict[str, Any]:
        """
        Aurora: snapshot the entire cluster (Writer + all Readers in one shot).
        """
        snap_id = f"{cluster_id}-cl-{datetime.now(timezone.utc):%Y%m%d-%H%M}"
        resp = self.rds.create_db_cluster_snapshot(
            DBClusterSnapshotIdentifier=snap_id,
            DBClusterIdentifier=cluster_id,
            Tags=tags,
        )
        cluster_snap = resp["DBClusterSnapshot"]
        self.rds.get_waiter("db_cluster_snapshot_available").wait(
            DBClusterSnapshotIdentifier=snap_id
        )
        return {
            "id": snap_id,
            "engine": cluster_snap["Engine"],
            "allocated_storage": 0,  # Aurora scales automatically
            "encrypted": cluster_snap["Encrypted"],
            "cluster_snapshot": True,
        }

    def copy_to_destination(self, backup_id: str, dest_prefix: str, **kwargs) -> int:
        """
        Copy RDS snapshot to destination via S3 pre-staging (preferred)
        or direct rclone if the RDS engine supports native export.

        Best practice: configure RDS to copy snapshots to an S3 bucket
        in the same region (near-zero egress), then rclone the S3 objects
        to the external destination.  Only changed bytes traverse the
        expensive external link.
        """
        # This requires enabling RDS snapshot export to S3:
        #   aws rds start-export-task --s3-bucket-name B --s3-prefix P \
        #     --source-arn ARN --export-only COMBINED
        # For simplicity, this handler assumes an S3 export lambda exists.
        raise NotImplementedError(
            "RDS snapshots must be exported to S3 before rclone can copy them. "
            "Enable RDS snapshot copying to S3 via the Console or CLI, "
            "then use the S3 handler to copy the exported objects. "
            "See README § RDS backup strategy."
        )

    def prune_local(self, backup_id: str, **kwargs) -> None:
        """Delete snapshots older than RETENTION_DAYS."""
        is_cluster = kwargs.get("cluster_snapshot", False)
        client = (
            self.rds.describe_db_cluster_snapshots
            if is_cluster else
            self.rds.describe_db_snapshots
        )
        kwargs2 = {
            "DBClusterSnapshotIdentifier" if is_cluster else "DBSnapshotIdentifier": backup_id
        }
        resp = client(**kwargs2)
        snap = (
            resp["DBClusterSnapshots"][0] if is_cluster else resp["DBSnapshots"][0]
        )
        created = snap["SnapshotCreateTime"].replace(tzinfo=timezone.utc)
        age_days = (datetime.now(timezone.utc) - created).days
        if age_days < RETENTION_DAYS:
            return

        if is_cluster:
            self.rds.delete_db_cluster_snapshot(DBClusterSnapshotIdentifier=backup_id)
        else:
            self.rds.delete_db_snapshot(DBSnapshotIdentifier=backup_id)
