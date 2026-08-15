import base64
import hashlib
import json
import os
import re
from datetime import datetime
from typing import Any

import boto3

from secret_resolver import resolve_secret_mapping, resolve_secret_text


def _load_secret(secret_ref: str) -> dict[str, str]:
    return resolve_secret_mapping(secret_ref)


def _load_text_secret(secret_ref: str) -> str:
    return resolve_secret_text(secret_ref)


def _normalise_encryption_config(raw: Any) -> dict[str, Any]:
    if not raw:
        return {}
    if isinstance(raw, str):
        raw = raw.strip()
        if not raw:
            return {}
        return json.loads(raw)
    if isinstance(raw, dict):
        return raw
    raise TypeError(f"Unsupported encryption config type: {type(raw)!r}")


def _resolve_sse_customer_key(encryption: dict[str, Any], region: str, creds: dict[str, str] | None = None) -> str:
    if not encryption:
        return ""

    aws_secrets_arn = encryption.get("aws_secrets_arn") or encryption.get("customer_key_ref") or encryption.get("customer_key_secret_ref")
    if aws_secrets_arn:
        aws_secrets_arn = str(aws_secrets_arn)
        if aws_secrets_arn.startswith("arn:aws:secretsmanager:"):
            secret_region = str(encryption.get("aws_secrets_region") or region or "us-east-1").strip() or "us-east-1"
            secret_client = boto3.client(
                "secretsmanager",
                region_name=secret_region,
            )
            response = secret_client.get_secret_value(SecretId=aws_secrets_arn)
            secret_value = response.get("SecretString", "")
            if isinstance(secret_value, str):
                return secret_value.strip()
            return ""
        return _load_text_secret(aws_secrets_arn)

    customer_key = encryption.get("customer_key")
    if customer_key:
        return str(customer_key)

    return os.environ.get("MY_KEY", "")


def _customer_key_headers(encryption: dict[str, Any], region: str = "us-east-1", creds: dict[str, str] | None = None) -> dict[str, str]:
    if not encryption:
        return {}

    mode = str(encryption.get("mode", "")).upper()
    if mode not in {"SSE-C", "SSE_C", "CUSTOMER", "AES256-C"}:
        return {}

    algorithm = str(encryption.get("algorithm", "AES256") or "AES256").upper()
    customer_key = _resolve_sse_customer_key(encryption, region, creds)

    if not isinstance(customer_key, str):
        raise ValueError("SSE-C key must be a base64-encoded string for AES256")

    raw_key = customer_key.strip()
    if not raw_key or not re.fullmatch(r"[A-Za-z0-9+/]+=*", raw_key):
        raise ValueError("SSE-C key must be base64-encoded AES256 key material")

    normalized_key = raw_key + "=" * ((4 - len(raw_key) % 4) % 4)
    try:
        key_bytes = base64.b64decode(normalized_key, validate=True)
    except ValueError as exc:
        raise ValueError("SSE-C key must be valid base64-encoded AES256 key material") from exc

    if algorithm == "AES256" and len(key_bytes) != 32:
        raise ValueError("SSE-C key must decode to exactly 32 bytes for AES256")

    md5_bytes = hashlib.md5(key_bytes).digest()
    md5_key = base64.b64encode(md5_bytes).decode("ascii")

    headers: dict[str, str] = {
        "SSECustomerAlgorithm": algorithm,
        "SSECustomerKey": normalized_key,
    }

    customer_key_md5 = encryption.get("customer_key_md5")
    if customer_key_md5:
        headers["SSECustomerKeyMD5"] = str(customer_key_md5)
    else:
        headers["SSECustomerKeyMD5"] = md5_key

    return headers


def _upload_extra_args(encryption: dict[str, Any]) -> dict[str, Any]:
    if not encryption:
        return {}

    mode = str(encryption.get("mode", "")).upper()
    if mode in {"SSE-S3", "SSE_S3", "AES256"}:
        return {"ServerSideEncryption": "AES256"}

    if mode in {"SSE-KMS", "SSE_KMS", "AWS:KMS", "AWS-KMS"}:
        extra_args: dict[str, Any] = {"ServerSideEncryption": "aws:kms"}
        kms_key_id = encryption.get("kms_key_id") or encryption.get("kms_key_arn")
        if kms_key_id:
            extra_args["SSEKMSKeyId"] = str(kms_key_id)
        if encryption.get("bucket_key_enabled") is not None:
            extra_args["BucketKeyEnabled"] = bool(encryption.get("bucket_key_enabled"))
        return extra_args

    if mode in {"SSE-C", "SSE_C", "CUSTOMER", "AES256-C"}:
        return _customer_key_headers(encryption)

    return {}


def _make_s3_client(region: str, endpoint: str, creds: dict[str, str]) -> Any:
    kwargs: dict[str, Any] = {
        "service_name": "s3",
        "region_name": region or "us-east-1",
    }

    if endpoint:
        kwargs["endpoint_url"] = endpoint

    if creds.get("aws_access_key_id") and creds.get("aws_secret_access_key"):
        kwargs["aws_access_key_id"] = creds["aws_access_key_id"]
        kwargs["aws_secret_access_key"] = creds["aws_secret_access_key"]
    if creds.get("aws_session_token"):
        kwargs["aws_session_token"] = creds["aws_session_token"]

    return boto3.client(**kwargs)


def _dest_key_for(source_key: str, source_prefix: str, dest_prefix: str) -> str:
    if source_prefix:
        rel = source_key[len(source_prefix):].lstrip("/")
    else:
        rel = source_key
    return "/".join(part for part in (dest_prefix, rel) if part)


def _list_objects(
    s3_client: Any,
    bucket: str,
    prefix: str,
) -> dict[str, dict[str, Any]]:
    paginator = s3_client.get_paginator("list_objects_v2")
    objects: dict[str, dict[str, Any]] = {}
    for page in paginator.paginate(Bucket=bucket, Prefix=prefix):
        for obj in page.get("Contents", []):
            objects[obj["Key"]] = {
                "size": int(obj.get("Size", 0)),
                "last_modified": obj.get("LastModified"),
            }
    return objects


def _should_copy(
    source_size: int,
    source_last_modified: datetime | None,
    destination_meta: dict[str, Any] | None,
    *,
    size_only: bool,
    exact_timestamps: bool,
) -> bool:
    if not destination_meta:
        return True

    destination_size = int(destination_meta.get("size", -1))
    if destination_size != int(source_size):
        return True

    if size_only:
        return False

    destination_last_modified = destination_meta.get("last_modified")
    if not isinstance(source_last_modified, datetime) or not isinstance(destination_last_modified, datetime):
        return False

    if exact_timestamps:
        return source_last_modified != destination_last_modified

    return source_last_modified > destination_last_modified


def run_s3_to_s3(source: Any, destination: Any, binding: Any) -> dict[str, int]:
    """Copy objects from source S3 bucket to destination S3-compatible bucket."""
    source_settings = source.settings or {}
    policy = binding.policy or {}

    source_bucket = source_settings.get("bucket", "").strip()
    if not source_bucket:
        raise ValueError("S3 source requires settings.bucket")

    source_prefix = str(source_settings.get("prefix", "")).strip().strip("/")
    dest_prefix = str(policy.get("dest_prefix", "")).strip().strip("/")

    source_encryption = _normalise_encryption_config(source_settings.get("encryption") or source_settings.get("sse"))
    destination_encryption = _normalise_encryption_config(
        policy.get("encryption")
        or policy.get("destination_encryption")
        or getattr(destination, "encryption", None)
        or destination.__dict__.get("encryption")
    )

    src_region = source_settings.get("region", "us-east-1")
    src_endpoint = source_settings.get("endpoint", "")
    src_secret_ref = source_settings.get("secret_ref", "")
    src_creds = _load_secret(src_secret_ref)

    dst_region = destination.region or "us-east-1"
    dst_endpoint = destination.endpoint or ""
    dst_creds = _load_secret(destination.secret_ref)

    src = _make_s3_client(src_region, src_endpoint, src_creds)
    dst = _make_s3_client(dst_region, dst_endpoint, dst_creds)

    src_prefix_for_list = f"{source_prefix}/" if source_prefix else ""
    dst_prefix_for_list = f"{dest_prefix}/" if dest_prefix else ""

    size_only = bool(policy.get("size_only", False))
    exact_timestamps = bool(policy.get("exact_timestamps", False))
    delete_extraneous = bool(policy.get("delete", False))

    scanned = 0
    copied = 0
    skipped = 0
    deleted = 0
    transferred_bytes = 0

    source_objects = _list_objects(src, source_bucket, src_prefix_for_list)
    destination_objects = _list_objects(dst, destination.bucket, dst_prefix_for_list)

    source_to_destination_keys: dict[str, str] = {}
    for source_key, source_meta in source_objects.items():
        source_to_destination_keys[source_key] = _dest_key_for(source_key, source_prefix, dest_prefix)

        size = int(source_meta.get("size", 0))
        source_last_modified = source_meta.get("last_modified")
        scanned += 1

        target_key = source_to_destination_keys[source_key]
        destination_meta = destination_objects.get(target_key)

        if not _should_copy(
            size,
            source_last_modified,
            destination_meta,
            size_only=size_only,
            exact_timestamps=exact_timestamps,
        ):
            skipped += 1
            continue

        get_kwargs: dict[str, Any] = {"Bucket": source_bucket, "Key": source_key}
        get_kwargs.update(_customer_key_headers(source_encryption, src_region, src_creds))
        response = src.get_object(**get_kwargs)
        body = response["Body"]
        try:
            extra_args = _upload_extra_args(destination_encryption)
            if extra_args:
                dst.upload_fileobj(body, destination.bucket, target_key, ExtraArgs=extra_args)
            else:
                dst.upload_fileobj(body, destination.bucket, target_key)
        finally:
            body.close()

        copied += 1
        transferred_bytes += size

    if delete_extraneous:
        expected_destination_keys = set(source_to_destination_keys.values())
        for target_key in destination_objects:
            if target_key not in expected_destination_keys:
                dst.delete_object(Bucket=destination.bucket, Key=target_key)
                deleted += 1

    return {
        "scanned_objects": scanned,
        "copied_objects": copied,
        "skipped_objects": skipped,
        "deleted_objects": deleted,
        "transferred_bytes": transferred_bytes,
    }
