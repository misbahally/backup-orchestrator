# Backup Orchestrator — Disaster Recovery to S3-Compatible Cold Storage

Single application that backs up **EFS**, **S3**, **EBS**, and **RDS** from AWS to any
S3-compatible endpoint (Backblaze B2, Wasabi, Cloudflare R2, MinIO, etc.) and
exposes Prometheus metrics for Grafana monitoring.

---

## Architecture

```
┌─────────────────────────────────────────────────────────────────┐
│                         AWS Source Region                        │
│  ┌──────┐  ┌──────┐  ┌──────┐  ┌──────┐                      │
│  │  EFS │  │  S3  │  │  EBS │  │  RDS │                       │
│  └──┬───┘  └──┬───┘  └──┬───┘  └──┬───┘                       │
│     │         │         │         │                              │
│  DataSync  S3 Direct  EBS Snap  RDS Snap                       │
│     │         │         │         │                              │
│     └────┬────┴────┬────┴────┬────┘                              │
│          │         │         │                                   │
└───────────────────▼──────────────────────────────────────────────┘
                    │ direct rclone copy to destination
          ┌─────────▼─────────────────────────────────┐
          │   External S3-Compatible API              │
          │   (Backblaze B2 / Wasabi / R2 / MinIO)   │
          │   Region: us-west-2 (DR region)           │
          └───────────────────────────────────────────┘
```

**Cost optimization highlights:**

| Source | Strategy | Why it's cheap |
|--------|----------|---------------|
| **EFS** | DataSync → destination | Incremental copy; only changed bytes transfer |
| **S3** | Direct rclone copy → destination | No staging bucket to manage |
| **EBS** | Snapshot → S3 export → rclone | Incremental block-level; only changed blocks |
| **RDS** | Snapshot → S3 export → rclone | Single full snapshot + incremental logs |
| **All** | rclone `--copy` (not `sync`) | Only new/changed bytes cross the external link |
| **All** | `--bwlimit 100M` (default) | Prevents egress bill spikes |

---

## Quick Start

### 1. Prerequisites

```bash
# Install
brew install rclone awscli docker docker-compose  # macOS
sudo apt install rclone awscli docker.io docker-compose  # Ubuntu/Debian

# Clone
git clone https://github.com/YOUR_ORG/backup-orchestrator.git
cd backup-orchestrator
```

### 2. Configure credentials

Create `.env` from the template:

```bash
cp .env.example .env
# Edit .env with your values
```

Key variables:

```env
# ── Source AWS ───────────────────────────────────────────────────────────────
AWS_REGION=us-east-1
AWS_ACCESS_KEY_ID=AKIA...
AWS_SECRET_ACCESS_KEY=...

# ── Destination (S3-compatible) ─────────────────────────────────────────────
DEST_PROVIDER=backblaze          # aws | backblaze | wasabi | minio | r2
DEST_ENDPOINT=https://s3.us-west-004.backblazeb2.com
DEST_REGION=us-west-2
DEST_BUCKET=my-dr-bucket
DEST_ACCESS_KEY=your-key
DEST_SECRET_KEY=your-secret

# ── Enable sources ───────────────────────────────────────────────────────────
ENABLE_EFS=true
EFS_FS_IDS=fs-0123456789abcdef,fs-fedcba9876543210
DATA_SYNC_TASK_ARN=arn:aws:datasync:us-east-1:123456789012:task/task-0123456789abcdef

ENABLE_S3=true
S3_BUCKETS=my-app-bucket,my-data-bucket

ENABLE_EBS=false
EBS_VOLUME_IDS=vol-0123456789abcdef

ENABLE_RDS=false
RDS_IDENTIFIERS=prod-db-01,prod-db-02
RDS_KMS_KEY_ID=arn:aws:kms:us-east-1:123456789012:key/mrk-...

RETENTION_DAYS=30
RCLONE_BW_LIMIT=100M
```

### 3. Install rclone config

```bash
# The app auto-generates ~/.config/rclone/rclone.conf at startup.
# For a pre-existing config:
cp rclone.conf ~/.config/rclone/rclone.conf
```

### 4. Run locally

```bash
docker compose up -d
docker compose logs -f backup-orchestrator
```

### 5. Verify metrics

```bash
# Prometheus targets
curl http://localhost:9090/metrics

# Grafana (admin/admin)
open http://localhost:3000
# Import dashboard: infra/grafana/provisioning/dashboards/backup-overview.json
```

---

## Deployment to AWS ECS Fargate

```bash
# Build and push Docker image to ECR
aws ecr get-login-password --region us-east-1 | \
  docker login --username AWS --password-stdin $ACCOUNT.dkr.ecr.us-east-1.amazonaws.com

docker build -t backup-orchestrator .
docker tag backup-orchestrator:latest $ACCOUNT.dkr.ecr.us-east-1.amazonaws.com/backup-orchestrator:latest
docker push $ACCOUNT.dkr.ecr.us-east-1.amazonaws.com/backup-orchestrator:latest

# Apply Terraform
cd infra/terraform
terraform init
terraform plan -var="aws_region=us-east-1"
terraform apply -var="aws_region=us-east-1"
```

Set your secrets in **AWS Secrets Manager** before running:
- `backup/aws-access-key` — AWS source credentials
- `backup/dest-credentials` — destination S3-compatible credentials

---

## Supported Backup Sources

### EFS
- Uses **AWS DataSync** (recommended) or `rsync` via a small EC2 instance
- DataSync task must be pre-created in the AWS console or Terraform
- DataSync output is copied directly to the DR destination
- **Cost**: DataSync + incremental destination transfer of changed bytes

### S3
- Uses direct rclone `copy` from source bucket to DR destination
- No staging bucket and no S3 Batch Replication job required
- **Cost**: source egress + destination ingest; incremental copy means only changed files transfer

### EBS
- Snapshots are created via the EC2 API
- Snapshots are exported to S3 via EBS Snapshots Archive (Amazon Data Lifecycle Manager or manual)
- rclone copies the exported snapshot files to DR destination
- **Cost**: EBS snapshots ~$0.05/GB/month; S3 archive storage ~$0.004/GB/month

### RDS
- Final snapshots (or Aurora cluster snapshots) are created
- Snapshots are exported to S3 via `aws rds start-export-task`
- rclone copies from the S3 export prefix to DR destination
- **Cost**: Same as EBS — snapshot storage + S3 archive storage

---

## Cost Estimation (monthly, approximate)

Assumptions: 1 TB of source data, 10% daily change rate, 30-day retention

| Source | AWS Cost | Destination Cost | Total |
|--------|----------|-----------------|-------|
| EFS → B2 | ~$30 (DataSync) | ~$7 (B2 at $0.006/GB) | ~$37 |
| S3 → B2 | ~$20 (S3 CRR) | ~$7 (B2) | ~$27 |
| EBS → B2 | ~$50 (snapshots) | ~$7 (B2) | ~$57 |
| RDS → B2 | ~$50 (snapshots) | ~$7 (B2) | ~$57 |

---

## Prometheus Metrics

| Metric | Type | Labels | Description |
|--------|------|--------|-------------|
| `backup_operations_total` | Counter | source_type, source_id, destination | Successful backups |
| `backup_failures_total` | Counter | source_type, source_id, destination, error_class | Failed backups |
| `backup_uploaded_bytes_total` | Counter | source_type, destination | Bytes transferred |
| `backup_last_success_timestamp` | Gauge | source_type, source_id | Unix epoch of last success |
| `snapshot_age_hours` | Gauge | source_type, source_id | Age of latest snapshot |
| `destination_available` | Gauge | destination | 1=up, 0=down |
| `backup_operation_duration_seconds` | Histogram | source_type, phase | Operation timing |

---

## Alert Rules (Prometheus)

| Alert | Condition | Severity |
|-------|-----------|----------|
| `BackupJobFailing` | >0 failures in 24h | critical |
| `NoSuccessfulBackups` | Last success >48h ago | warning |
| `DestinationUnreachable` | Destination down >5min | critical |
| `BackupOperationSlow` | p95 duration >1h | warning |
| `HighBackupFailureRate` | >50% failure rate in 1h | warning |

---

## File Layout

```
backup-orchestrator/
├── src/
│   ├── orchestrator.py          # Main entry point
│   ├── config.py                # Environment-based configuration
│   ├── metrics.py               # Prometheus metrics
│   ├── handlers/
│   │   ├── base.py              # BackupHandler abstract base
│   │   ├── efs.py              # EFS via DataSync
│   │   ├── s3.py              # S3 direct copy to destination
│   │   ├── ebs.py             # EBS snapshots
│   │   └── rds.py             # RDS snapshots
│   └── destinations/
│       └── rclone.py           # S3-compatible destination
├── infra/
│   ├── prometheus/
│   │   ├── prometheus.yml
│   │   └── alert_rules.yml
│   ├── grafana/provisioning/
│   │   ├── datasources/prometheus.yml
│   │   └── dashboards/backup-overview.json
│   └── terraform/main.tf       # AWS infrastructure
├── docker-compose.yml
├── Dockerfile
├── requirements.txt
└── README.md
```

---

## Adding a New Source Type

1. Create a new handler in `src/handlers/` extending `BackupHandler`
2. Register it in `src/handlers/__init__.py`
3. Add config keys in `src/config.py`
4. Add source type case in `src/orchestrator.py` → `_resolve_source_ids()`
5. Add Prometheus alert rules in `infra/prometheus/alert_rules.yml`
6. Add panels to the Grafana dashboard JSON

---

## Limitations & Known Issues

- **EBS direct copy**: The EBS handler's `_transfer_volume` method raises `NotImplementedError` — the
  recommended path is EBS Snapshots → S3 Archive → S3 handler → rclone. Pre-stage snapshots to S3.
- **RDS direct copy**: Similarly, the RDS handler's `copy_to_destination` is a stub. Use the
  `start-export-task` flow described in `src/handlers/rds.py`.
- **Credentials**: Never commit `.env` or real credentials to version control.
  Use AWS Secrets Manager (ECS) or AWS SSM Parameter Store.
