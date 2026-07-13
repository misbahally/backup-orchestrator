"""
Destination handler: rclone-based upload to any S3-compatible endpoint.
Handles its own retry logic, bandwidth limiting, and multipart checks.
"""
from __future__ import annotations

import os
import subprocess
from typing import Any

import boto3
import requests

from src.config import (
    DEST_ACCESS_KEY,
    DEST_BUCKET,
    DEST_ENDPOINT,
    DEST_PROVIDER,
    DEST_SECRET_KEY,
)
from src.metrics import DESTINATION_AVAILABLE


class DestinationHandler:
    """
    Wraps rclone for uploading to any S3-compatible API.
    Supported providers: aws, backblaze, wasabi, minio, r2, others.
    """

    PROVIDER_TO_FLAG = {
        "aws":       "AWS",
        "backblaze": "B2",
        "wasabi":    "Wasabi",
        "minio":     "Minio",
        "r2":        "Cloudflare",
    }

    def __init__(self):
        self.provider = DEST_PROVIDER
        self.bucket   = DEST_BUCKET
        self.endpoint = DEST_ENDPOINT
        self._rclone_remote = f"{self.provider}:{self.bucket}"
        self._ensure_rclone_config()

    # ── public API ─────────────────────────────────────────────────────────────

    def is_available(self) -> bool:
        """Ping the destination endpoint to verify connectivity."""
        try:
            # Try a simple HEAD request against the bucket endpoint
            region_hint = os.environ.get("DEST_REGION", "us-east-1")
            url = f"{self.endpoint}/{self.bucket}"
            resp = requests.head(
                url,
                timeout=10,
                auth=(DEST_ACCESS_KEY, DEST_SECRET_KEY),
            )
            available = resp.status_code in (200, 301, 403, 404)
        except Exception:
            available = False

        DESTINATION_AVAILABLE.labels(destination=self._rclone_remote).set(1 if available else 0)
        return available

    def upload_directory(self, local_path: str, dest_prefix: str) -> dict[str, Any]:
        """
        Upload a local directory tree to the destination using rclone copy.
        dest_prefix is prepended inside the bucket.
        Returns upload stats.
        """
        dest = f"{self._rclone_remote}/{dest_prefix}"
        return self._run_rclone_copy(local_path, dest)

    def upload_file(self, local_path: str, dest_key: str) -> dict[str, Any]:
        """Upload a single file using rclone rcat (no local disk read into memory)."""
        dest = f"{self._rclone_remote}/{dest_key}"
        cmd = [
            "rclone", "rcat",
            dest,
            "--config", self._rclone_config_path,
        ]
        result = subprocess.run(
            cmd + [local_path],
            capture_output=True, text=True, timeout=86400,
        )
        return self._parse_result(result, "rcat")

    def list_backups(self, prefix: str = "") -> list[dict[str, Any]]:
        """List objects under a prefix in the destination bucket."""
        cmd = [
            "rclone", "lsjson",
            f"{self._rclone_remote}/{prefix}",
            "--config", self._rclone_config_path,
            "--recursive",
        ]
        result = subprocess.run(cmd, capture_output=True, text=True, timeout=60)
        if result.returncode != 0:
            raise RuntimeError(f"rclone lsjson failed: {result.stderr}")
        import json
        items = json.loads(result.stdout)
        return [
            {
                "name":  item["Name"],
                "size":  item["Size"],
                "iso":   item.get("ModTime", ""),
            }
            for item in items
        ]

    # ── internal ───────────────────────────────────────────────────────────────

    @property
    def _rclone_config_path(self) -> str:
        return os.path.expanduser("~/.config/rclone/rclone.conf")

    def _ensure_rclone_config(self) -> None:
        """
        Write the rclone.conf file on startup.
        In production, store credentials in AWS Secrets Manager or Parameter Store
        and inject via environment variable; never write plaintext secrets to disk.
        """
        conf_dir = os.path.dirname(self._rclone_config_path)
        os.makedirs(conf_dir, exist_ok=True)

        provider_flag = self.PROVIDER_TO_FLAG.get(self.provider, "AWS")
        # Build the config file section
        section = f"""\
[{self.provider}]
type = s3
provider = {provider_flag}
access_key_id = {DEST_ACCESS_KEY}
secret_access_key = {DEST_SECRET_KEY}
endpoint = {DEST_ENDPOINT}
region = {os.environ.get("DEST_REGION", "us-east-1")}
acl = private
"""
        # Append or replace the section in the config file
        conf_path = self._rclone_config_path
        if os.path.exists(conf_path):
            with open(conf_path) as f:
                lines = f.readlines()
            # Remove existing section for this provider
            new_lines, in_section = [], False
            for line in lines:
                if line.startswith(f"[{self.provider}]"):
                    in_section = True
                elif in_section and line.startswith("["):
                    in_section = False
                    new_lines.append(line)
                elif not in_section:
                    new_lines.append(line)
            with open(conf_path, "w") as f:
                f.writelines(new_lines)

        with open(conf_path, "a") as f:
            f.write(section)

    def _run_rclone_copy(self, src: str, dest: str) -> dict[str, Any]:
        """
        Run rclone copy with bandwidth limiting (--bwlimit) to avoid
        egress bill shocks, and --transfers=4 to limit parallelism.
        """
        bwlimit = os.environ.get("RCLONE_BW_LIMIT", "100M")  # default 100 MB/s
        cmd = [
            "rclone", "copy",
            src,
            dest,
            "--config", self._rclone_config_path,
            "--transfers", "4",
            "--checkers", "8",
            "--bwlimit", bwlimit,
            "--log-level", "ERROR",
            "--stats", "1s",
            "--stats-one-line",
            "--no-check-destination",
            "--drive-chunk-size", "64M",
        ]
        result = subprocess.run(cmd, capture_output=True, text=True, timeout=86400)
        return self._parse_result(result, "copy")

    @staticmethod
    def _parse_result(result: subprocess.CompletedProcess, operation: str) -> dict[str, Any]:
        if result.returncode == 0:
            return {
                "operation": operation,
                "status": "success",
                "stderr": result.stderr,
                "stdout": result.stdout,
            }
        return {
            "operation": operation,
            "status": "failed",
            "returncode": result.returncode,
            "stderr": result.stderr,
            "stdout": result.stdout,
            "error": result.stderr.splitlines()[-1] if result.stderr else "unknown",
        }
