from datetime import datetime, timezone
from typing import Any

import boto3

from secret_resolver import resolve_secret_mapping


def run_ebs_snapshot(source: Any, destination: Any, binding: Any) -> dict[str, str]:
    source_settings = source.settings or {}
    region = str(source_settings.get("region", "us-east-1")).strip() or "us-east-1"
    volume_id = str(source_settings.get("volume_id", "")).strip()
    if not volume_id:
        raise ValueError("ebs source requires settings.volume_id")

    creds = resolve_secret_mapping(str(source_settings.get("secret_ref", "")))
    client = boto3.client("ec2", region_name=region, **{k: v for k, v in creds.items() if v})

    desc = client.describe_volumes(VolumeIds=[volume_id])
    if not desc.get("Volumes"):
        raise ValueError(f"volume '{volume_id}' not found")

    snapshot = client.create_snapshot(
        VolumeId=volume_id,
        Description=f"backup-orchestrator:{source.name}:{datetime.now(timezone.utc).isoformat()}",
        TagSpecifications=[
            {
                "ResourceType": "snapshot",
                "Tags": [
                    {"Key": "managed-by", "Value": "backup-orchestrator"},
                    {"Key": "binding-id", "Value": str(binding.id)},
                ],
            }
        ],
    )
    snapshot_id = snapshot["SnapshotId"]
    waiter = client.get_waiter("snapshot_completed")
    waiter.wait(SnapshotIds=[snapshot_id])

    return {"artifact_ref": snapshot_id}
