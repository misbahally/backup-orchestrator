from datetime import datetime, timezone
from typing import Any

import boto3

from secret_resolver import resolve_secret_mapping


def run_rds_snapshot(source: Any, destination: Any, binding: Any) -> dict[str, str]:
    source_settings = source.settings or {}
    region = str(source_settings.get("region", "us-east-1")).strip() or "us-east-1"
    instance_id = str(source_settings.get("db_instance_identifier", "")).strip()
    cluster_id = str(source_settings.get("db_cluster_identifier", "")).strip()
    if not instance_id and not cluster_id:
        raise ValueError("rds source requires db_instance_identifier or db_cluster_identifier")

    creds = resolve_secret_mapping(str(source_settings.get("secret_ref", "")))
    client = boto3.client("rds", region_name=region, **{k: v for k, v in creds.items() if v})

    snapshot_suffix = datetime.now(timezone.utc).strftime("%Y%m%d%H%M%S")
    if instance_id:
        snapshot_id = f"backup-orchestrator-{instance_id}-{snapshot_suffix}".lower()[:255]
        client.create_db_snapshot(DBSnapshotIdentifier=snapshot_id, DBInstanceIdentifier=instance_id)
        waiter = client.get_waiter("db_snapshot_available")
        waiter.wait(DBSnapshotIdentifier=snapshot_id)
        return {"artifact_ref": snapshot_id}

    cluster_snapshot_id = f"backup-orchestrator-{cluster_id}-{snapshot_suffix}".lower()[:255]
    client.create_db_cluster_snapshot(DBClusterSnapshotIdentifier=cluster_snapshot_id, DBClusterIdentifier=cluster_id)
    waiter = client.get_waiter("db_cluster_snapshot_available")
    waiter.wait(DBClusterSnapshotIdentifier=cluster_snapshot_id)
    return {"artifact_ref": cluster_snapshot_id}
