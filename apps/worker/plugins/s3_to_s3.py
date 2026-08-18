import base64
import gzip
import hashlib
import json
import mimetypes
import os
import re
import tempfile
from concurrent.futures import ThreadPoolExecutor, as_completed
from datetime import datetime
from typing import Any

import boto3
from boto3.s3.transfer import TransferConfig
from botocore.config import Config

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

    kwargs["config"] = Config(
        retries={"mode": "adaptive", "max_attempts": 10},
        max_pool_connections=64,
    )

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


def _int_policy(policy: dict[str, Any], key: str, default: int, *, minimum: int, maximum: int) -> int:
    value = policy.get(key, default)
    try:
        parsed = int(value)
    except (TypeError, ValueError):
        parsed = default
    return max(minimum, min(maximum, parsed))


def _compression_policy(policy: dict[str, Any]) -> dict[str, Any]:
    compression = policy.get("compression") or {}
    if not isinstance(compression, dict):
        compression = {}

    enabled = bool(compression.get("enabled", False))
    algorithm = str(compression.get("algorithm", "gzip") or "gzip").lower()
    try:
        level = int(compression.get("level", 6) or 6)
    except (TypeError, ValueError):
        level = 6
    level = max(1, min(9, level))
    try:
        min_size_bytes = int(compression.get("min_size_bytes", 1024) or 1024)
    except (TypeError, ValueError):
        min_size_bytes = 1024
    min_size_bytes = max(0, min_size_bytes)

    def normalize_ext(value: Any) -> str:
        ext = str(value).strip().lower()
        if not ext:
            return ""
        return ext if ext.startswith(".") else f".{ext}"

    include_extensions = compression.get("include_extensions")
    if isinstance(include_extensions, list) and include_extensions:
        normalized_include = [normalize_ext(x) for x in include_extensions]
        normalized_include = [x for x in normalized_include if x]
    else:
        normalized_include = [
            ".txt",
            ".log",
            ".json",
            ".csv",
            ".sql",
            ".xml",
            ".yaml",
            ".yml",
            ".md",
            ".html",
            ".js",
            ".css",
        ]

    exclude_extensions = compression.get("exclude_extensions")
    if isinstance(exclude_extensions, list):
        normalized_exclude = [normalize_ext(x) for x in exclude_extensions]
        normalized_exclude = [x for x in normalized_exclude if x]
    else:
        normalized_exclude = []

    return {
        "enabled": enabled,
        "algorithm": algorithm,
        "level": level,
        "min_size_bytes": min_size_bytes,
        "include_extensions": normalized_include,
        "exclude_extensions": normalized_exclude,
    }


def _should_compress(source_key: str, size: int, compression: dict[str, Any]) -> bool:
    if not compression.get("enabled"):
        return False
    if compression.get("algorithm") != "gzip":
        return False
    if int(size) < int(compression.get("min_size_bytes", 0)):
        return False

    _, ext = os.path.splitext(source_key)
    ext = ext.lower()
    include_extensions = compression.get("include_extensions") or []
    exclude_extensions = compression.get("exclude_extensions") or []
    if ext in exclude_extensions:
        return False
    if not include_extensions:
        return True
    return ext in include_extensions


def _normalized_iso8601(value: datetime | None) -> str:
    if not isinstance(value, datetime):
        return ""
    return value.isoformat()


def _head_object_if_exists(s3_client: Any, bucket: str, key: str) -> dict[str, Any] | None:
    try:
        return s3_client.head_object(Bucket=bucket, Key=key)
    except Exception:
        return None


def _should_copy_with_compression(
    source_size: int,
    source_last_modified: datetime | None,
    destination_head: dict[str, Any] | None,
    *,
    size_only: bool,
    exact_timestamps: bool,
) -> bool:
    if not destination_head:
        return True

    destination_meta = destination_head.get("Metadata") or {}
    source_size_meta = str(destination_meta.get("source_size", "")).strip()
    source_last_modified_meta = str(destination_meta.get("source_last_modified", "")).strip()

    if source_size_meta != str(int(source_size)):
        return True
    if size_only:
        return False

    source_last_modified_iso = _normalized_iso8601(source_last_modified)
    if not source_last_modified_iso or not source_last_modified_meta:
        return True

    if exact_timestamps:
        return source_last_modified_iso != source_last_modified_meta

    try:
        destination_seen = datetime.fromisoformat(source_last_modified_meta)
    except ValueError:
        return True

    if not isinstance(source_last_modified, datetime):
        return False
    return source_last_modified > destination_seen


def _gzip_to_tempfile(stream: Any, compression_level: int) -> tuple[str, int]:
    fd, temp_path = tempfile.mkstemp(prefix="s3-sync-", suffix=".gz")
    os.close(fd)
    total = 0
    try:
        with open(temp_path, "wb") as raw_out:
            with gzip.GzipFile(fileobj=raw_out, mode="wb", compresslevel=compression_level, mtime=0) as gz_out:
                while True:
                    chunk = stream.read(8 * 1024 * 1024)
                    if not chunk:
                        break
                    gz_out.write(chunk)
                    total += len(chunk)
        return temp_path, total
    except Exception:
        try:
            os.remove(temp_path)
        except OSError:
            pass
        raise


def _copy_one_object(
    *,
    source_key: str,
    source_meta: dict[str, Any],
    source_bucket: str,
    source_prefix: str,
    dest_prefix: str,
    src_client: Any,
    dst_client: Any,
    source_encryption: dict[str, Any],
    destination_encryption: dict[str, Any],
    src_region: str,
    src_creds: dict[str, str],
    destination_bucket: str,
    destination_objects: dict[str, dict[str, Any]],
    transfer_config: TransferConfig,
    size_only: bool,
    exact_timestamps: bool,
    compression: dict[str, Any],
) -> dict[str, Any]:
    size = int(source_meta.get("size", 0))
    source_last_modified = source_meta.get("last_modified")
    target_key = _dest_key_for(source_key, source_prefix, dest_prefix)

    compress_this = _should_compress(source_key, size, compression)

    if compress_this:
        destination_head = None
        if target_key in destination_objects:
            destination_head = _head_object_if_exists(dst_client, destination_bucket, target_key)
        should_copy = _should_copy_with_compression(
            size,
            source_last_modified,
            destination_head,
            size_only=size_only,
            exact_timestamps=exact_timestamps,
        )
    else:
        should_copy = _should_copy(
            size,
            source_last_modified,
            destination_objects.get(target_key),
            size_only=size_only,
            exact_timestamps=exact_timestamps,
        )

    if not should_copy:
        return {
            "source_key": source_key,
            "target_key": target_key,
            "copied": 0,
            "skipped": 1,
            "transferred_bytes": 0,
        }

    get_kwargs: dict[str, Any] = {"Bucket": source_bucket, "Key": source_key}
    get_kwargs.update(_customer_key_headers(source_encryption, src_region, src_creds))
    response = src_client.get_object(**get_kwargs)
    body = response["Body"]
    try:
        extra_args = _upload_extra_args(destination_encryption)
        if compress_this:
            source_metadata = response.get("Metadata") or {}
            merged_metadata = {str(k): str(v) for k, v in source_metadata.items()}
            merged_metadata["source_size"] = str(size)
            merged_metadata["source_last_modified"] = _normalized_iso8601(source_last_modified)
            merged_metadata["compression"] = "gzip"

            content_type = response.get("ContentType") or mimetypes.guess_type(source_key)[0] or "application/octet-stream"
            upload_extra_args = dict(extra_args)
            upload_extra_args["ContentEncoding"] = "gzip"
            upload_extra_args["ContentType"] = content_type
            upload_extra_args["Metadata"] = merged_metadata

            temp_path = ""
            try:
                temp_path, original_bytes = _gzip_to_tempfile(body, int(compression.get("level", 6)))
                dst_client.upload_file(
                    temp_path,
                    destination_bucket,
                    target_key,
                    ExtraArgs=upload_extra_args,
                    Config=transfer_config,
                )
                return {
                    "source_key": source_key,
                    "target_key": target_key,
                    "copied": 1,
                    "skipped": 0,
                    "transferred_bytes": int(original_bytes),
                }
            finally:
                if temp_path:
                    try:
                        os.remove(temp_path)
                    except OSError:
                        pass
        else:
            if extra_args:
                dst_client.upload_fileobj(
                    body,
                    destination_bucket,
                    target_key,
                    ExtraArgs=extra_args,
                    Config=transfer_config,
                )
            else:
                dst_client.upload_fileobj(
                    body,
                    destination_bucket,
                    target_key,
                    Config=transfer_config,
                )
            return {
                "source_key": source_key,
                "target_key": target_key,
                "copied": 1,
                "skipped": 0,
                "transferred_bytes": size,
            }
    finally:
        body.close()


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
    parallel_workers = _int_policy(policy, "parallel_workers", 4, minimum=1, maximum=64)
    multipart_threshold_mb = _int_policy(policy, "multipart_threshold_mb", 16, minimum=5, maximum=512)
    multipart_chunk_mb = _int_policy(policy, "multipart_chunk_mb", 8, minimum=5, maximum=128)
    max_transfer_concurrency = _int_policy(policy, "max_transfer_concurrency", max(4, parallel_workers), minimum=1, maximum=64)
    use_transfer_threads = bool(policy.get("use_transfer_threads", True))
    compression = _compression_policy(policy)

    transfer_config = TransferConfig(
        multipart_threshold=multipart_threshold_mb * 1024 * 1024,
        multipart_chunksize=multipart_chunk_mb * 1024 * 1024,
        max_concurrency=max_transfer_concurrency,
        use_threads=use_transfer_threads,
    )

    scanned = 0
    copied = 0
    skipped = 0
    deleted = 0
    transferred_bytes = 0

    source_objects = _list_objects(src, source_bucket, src_prefix_for_list)
    destination_objects = _list_objects(dst, destination.bucket, dst_prefix_for_list)

    source_to_destination_keys: dict[str, str] = {}
    tasks: list[tuple[str, dict[str, Any]]] = []
    for source_key, source_meta in source_objects.items():
        source_to_destination_keys[source_key] = _dest_key_for(source_key, source_prefix, dest_prefix)
        tasks.append((source_key, source_meta))
        scanned += 1

    with ThreadPoolExecutor(max_workers=parallel_workers) as executor:
        futures = [
            executor.submit(
                _copy_one_object,
                source_key=source_key,
                source_meta=source_meta,
                source_bucket=source_bucket,
                source_prefix=source_prefix,
                dest_prefix=dest_prefix,
                src_client=src,
                dst_client=dst,
                source_encryption=source_encryption,
                destination_encryption=destination_encryption,
                src_region=src_region,
                src_creds=src_creds,
                destination_bucket=destination.bucket,
                destination_objects=destination_objects,
                transfer_config=transfer_config,
                size_only=size_only,
                exact_timestamps=exact_timestamps,
                compression=compression,
            )
            for source_key, source_meta in tasks
        ]

        for future in as_completed(futures):
            result = future.result()
            copied += int(result.get("copied", 0))
            skipped += int(result.get("skipped", 0))
            transferred_bytes += int(result.get("transferred_bytes", 0))

    if delete_extraneous:
        expected_destination_keys = set(source_to_destination_keys.values())
        delete_batch: list[dict[str, str]] = []
        for target_key in destination_objects:
            if target_key not in expected_destination_keys:
                delete_batch.append({"Key": target_key})
                if len(delete_batch) == 1000:
                    dst.delete_objects(Bucket=destination.bucket, Delete={"Objects": delete_batch, "Quiet": True})
                    deleted += len(delete_batch)
                    delete_batch = []
        if delete_batch:
            dst.delete_objects(Bucket=destination.bucket, Delete={"Objects": delete_batch, "Quiet": True})
            deleted += len(delete_batch)

    return {
        "scanned_objects": scanned,
        "copied_objects": copied,
        "skipped_objects": skipped,
        "deleted_objects": deleted,
        "transferred_bytes": transferred_bytes,
    }
