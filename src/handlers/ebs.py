"""
AWS EBS backup handler.
Creates EBS snapshots, copies them to destination via DataSync/file-level copy,
then deletes expired local snapshots.
"""
from __future__ import annotations

import subprocess
from datetime import datetime, timezone
from typing import Any

import boto3

from src.config import AWS_REGION, RETENTION_DAYS, RCLONE_REMOTE
from src.handlers.base import BackupHandler


class EBSBackupHandler(BackupHandler):
    source_type = "ebs"

    def __init__(self, destination: str):
        super().__init__(destination)
        self.ec2 = boto3.client("ec2", region_name=AWS_REGION)

    def list_backups(self) -> list[dict[str, Any]]:
        """List snapshots owned by this account matching the backup prefix."""
        resp = self.ec2.describe_snapshots(
            Filters=[
                {"Name": "tag:Backup", "Values": ["true"]},
            ],
            OwnerIds=["self"],
        )
        return [
            {
                "id": s["SnapshotId"],
                "created_at": s["StartTime"],
                "size_gib": s.get("VolumeSize", 0),
                "tags": {t["Key"]: t["Value"] for t in s.get("Tags", [])},
            }
            for s in resp["Snapshots"]
        ]

    def create_backup(self, volume_id: str, **kwargs) -> dict[str, Any]:
        """Create an EBS snapshot of the volume with standardised tags."""
        prefix = kwargs.get("snap_prefix", "Backup")
        resp = self.ec2.create_snapshot(
            VolumeId=volume_id,
            TagSpecifications=[
                {
                    "ResourceType": "snapshot",
                    "Tags": [
                        {"Key": "Backup", "Value": "true"},
                        {"Key": "ManagedBy", "Value": "backup-orchestrator"},
                        {"Key": "SourceVolume", "Value": volume_id},
                        {"Key": "CreatedBy", "Value": "ebs-backup-handler"},
                    ],
                },
            ],
        )
        snap_id = resp["SnapshotId"]

        # Wait for completion (EBS snapshots are async)
        self.ec2.get_waiter("snapshot_completed").wait(SnapshotIds=[snap_id])
        desc = self.ec2.describe_snapshots(SnapshotIds=[snap_id])["Snapshots"][0]

        return {
            "id": snap_id,
            "volume_id": volume_id,
            "size_gib": desc["VolumeSize"],
            "encrypted": desc["Encrypted"],
        }

    def copy_to_destination(self, snap_id: str, dest_prefix: str, **kwargs) -> int:
        """
        EBS snapshots live in AWS and can't be directly accessed.
        Strategy:
          1. Create a temporary volume from the snapshot
          2. Attach it to a small EC2 instance (t3.micro — cheapest)
          3. dd/tar the contents through rclone to the destination
          4. Terminate the temp instance
        This keeps costs minimal (only EBS storage + small instance time).
        """
        size_gib = kwargs.get("size_gib", 100)
        temp_instance_id = self._launch_temp_instance(size_gib)
        try:
            volume_id = self._create_temp_volume(snap_id, size_gib)
            device = self._attach_volume(volume_id, temp_instance_id)
            bytes_transferred = self._transfer_volume(device, dest_prefix)
        finally:
            self._terminate_instance(temp_instance_id)
        return bytes_transferred

    def prune_local(self, snap_id: str, **kwargs) -> None:
        """Delete the EBS snapshot after successful copy to destination."""
        # Check retention policy before deleting
        desc = self.ec2.describe_snapshots(SnapshotIds=[snap_id])["Snapshots"][0]
        start = desc["StartTime"]
        age_days = (datetime.now(timezone.utc) - start.replace(tzinfo=timezone.utc)).days
        if age_days < RETENTION_DAYS:
            return  # keep for local recovery window
        self.ec2.delete_snapshot(SnapshotId=snap_id)

    # ── internal helpers ────────────────────────────────────────────────────────

    def _launch_temp_instance(self, size_gib: int) -> str:
        instance_type = "t3.micro" if size_gib <= 30 else "t3.small"
        resp = self.ec2.run_instances(
            ImageId="ami-0c55b159cbfafe1f0",  # Amazon Linux 2 (update to your region)
            InstanceType=instance_type,
            MinCount=1,
            MaxCount=1,
            TagSpecifications=[{"ResourceType": "instance", "Tags": [{"Key": "Name", "Value": "backup-temp"}]}],
        )
        inst_id = resp["Instances"][0]["InstanceId"]
        self.ec2.get_waiter("instance_running").wait(InstanceIds=[inst_id])
        return inst_id

    def _create_temp_volume(self, snap_id: str, size_gib: int) -> str:
        resp = self.ec2.create_volume(
            SnapshotId=snap_id,
            Size=size_gib,
            AvailabilityZone=self._get_instance_az(),
            Encrypted=True,
        )
        vol_id = resp["VolumeId"]
        self.ec2.get_waiter("volume_available").wait(VolumeIds=[vol_id])
        return vol_id

    def _get_instance_az(self) -> str:
        # cheap way to get AZ without a second describe call
        return "us-east-1a"  # TODO: resolve dynamically from attached instance

    def _attach_volume(self, volume_id: str, instance_id: str) -> str:
        resp = self.ec2.attach_volume(VolumeId=volume_id, InstanceId=instance_id, Device="/dev/sdf")
        self.ec2.get_waiter("volume_in_use").wait(VolumeIds=[volume_id])
        return "/dev/sdf"

    def _transfer_volume(self, device: str, dest_prefix: str) -> int:
        dest = f"{RCLONE_REMOTE}/{dest_prefix}"
        # Stream tar archive directly to rclone (never touches local disk)
        cmd = [
            "dd", f"if={device}", "bs=4M", "status=progress",
            "|", "rclone", "rcat", f"{dest}/data.img.tar",
            "--log-level", "ERROR",
        ]
        # Using tar + rclone rcat for a streaming approach
        tar_cmd = [
            "tar", "cf", "-", "-C", "/",
            # list of mount points or block device contents
        ]
        # For a real implementation, mount the volume and tar its contents:
        # Option A: attach volume → mount → tar contents → rcat
        # Option B: snapshot → S3 export → rclone
        # We use Option B (snapshot-to-S3 export) via EBS Direct APIs if available
        # Fallback to the EBS→S3→rclone path
        raise NotImplementedError(
            "EBS backup requires EBS Direct APIs or S3 import/export. "
            "Pre-stage snapshots to S3 using EBS Snapshots Archive, "
            "then use the S3 handler's copy_to_destination. "
            "See README § EBS backup strategy."
        )

    def _terminate_instance(self, instance_id: str) -> None:
        self.ec2.terminate_instances(InstanceIds=[instance_id])
        self.ec2.get_waiter("instance_terminated").wait(InstanceIds=[instance_id])
