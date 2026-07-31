from prometheus_client import Counter, Gauge, Histogram

BACKUP_OPERATIONS_TOTAL = Counter(
    "backup_operations_total",
    "Count of backup operations",
    ["source_type", "status"],
)
BACKUP_FAILURES_TOTAL = Counter(
    "backup_failures_total",
    "Count of failed backup operations",
    ["source_type", "error_class"],
)
BACKUP_UPLOADED_BYTES_TOTAL = Counter(
    "backup_uploaded_bytes_total",
    "Total uploaded bytes",
    ["binding"],
)
BACKUP_LAST_SUCCESS_TIMESTAMP = Gauge(
    "backup_last_success_timestamp",
    "Unix timestamp of last successful backup",
    ["binding"],
)
BACKUP_OPERATION_DURATION_SECONDS = Histogram(
    "backup_operation_duration_seconds",
    "Backup operation duration",
    ["source_type"],
)
SCHEDULER_ENQUEUED_RUNS_TOTAL = Counter(
    "scheduler_enqueued_runs_total",
    "Runs enqueued by scheduler",
)
