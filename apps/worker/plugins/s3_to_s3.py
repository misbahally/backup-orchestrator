import base64
import hashlib
import json
import os
from typing import Any

import boto3
from botocore.exceptions import ClientError

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


def _customer_key_headers(encryption: dict[str, Any]) -> dict[str, str]:
    if not encryption:
        return {}

    mode = str(encryption.get("mode", "")).upper()
    if mode not in {"SSE-C", "SSE_C", "CUSTOMER", "AES256-C"}:
        return {}

    customer_key_ref = encryption.get("customer_key_ref") or encryption.get("customer_key_secret_ref")
    customer_key = encryption.get("customer_key") or _load_text_secret(str(customer_key_ref or ""))
    if not customer_key:
        customer_key = os.environ.get("MY_KEY", "")

    key_bytes = customer_key.encode("utf-8") if isinstance(customer_key, str) else bytes(customer_key)
    encoded_key = base64.b64encode(key_bytes).decode("ascii")
    md5_bytes = hashlib.md5(key_bytes).digest()
    md5_key = base64.b64encode(md5_bytes).decode("ascii")

    headers: dict[str, str] = {
        "SSECustomerAlgorithm": str(encryption.get("algorithm", "AES256")),
        "SSECustomerKey": encoded_key,
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


def _exists_with_same_size(
    s3_client: Any,
    bucket: str,
    key: str,
    size: int,
    encryption: dict[str, Any] | None = None,
) -> bool:
    try:
        head_kwargs: dict[str, Any] = {"Bucket": bucket, "Key": key}
        head_kwargs.update(_customer_key_headers(encryption or {}))
        obj = s3_client.head_object(**head_kwargs)
        return int(obj.get("ContentLength", -1)) == int(size)
    except ClientError as exc:
        code = exc.response.get("Error", {}).get("Code", "")
        if code in ("404", "NoSuchKey", "NotFound"):
            return False
        raise


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

    scanned = 0
    copied = 0
    skipped = 0
    transferred_bytes = 0

    paginator = src.get_paginator("list_objects_v2")
    for page in paginator.paginate(Bucket=source_bucket, Prefix=src_prefix_for_list):
        for obj in page.get("Contents", []):
            key = obj["Key"]
            size = int(obj.get("Size", 0))
            scanned += 1

            target_key = _dest_key_for(key, source_prefix, dest_prefix)

            if _exists_with_same_size(dst, destination.bucket, target_key, size, destination_encryption):
                skipped += 1
                continue

            get_kwargs: dict[str, Any] = {"Bucket": source_bucket, "Key": key}
            get_kwargs.update(_customer_key_headers(source_encryption))
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

    return {
        "scanned_objects": scanned,
        "copied_objects": copied,
        "skipped_objects": skipped,
        "transferred_bytes": transferred_bytes,
    }
