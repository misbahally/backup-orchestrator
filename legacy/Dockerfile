FROM python:3.12-slim

# Install rclone and AWS CLI (both needed at runtime)
RUN apt-get update && apt-get install -y --no-install-recommends \
        curl \
        awscli \
    && curl -sL https://rclone.org/install.sh | sh \
    && apt-get clean \
    && rm -rf /var/lib/apt/lists/*

WORKDIR /app

# Install Python deps
COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt

COPY src/ src/
RUN python -c "import src.metrics; import src.config"  # smoke-test

# AWS credentials + rclone config are mounted at runtime
ENV PYTHONUNBUFFERED=1 \
    PYTHONDONTWRITEBYTECODE=1 \
    AWS_REGION=us-east-1 \
    SCHEDULE="0 2 * * *" \
    RETENTION_DAYS=30 \
    METRICS_PORT=9090

# Prometheus scrape endpoint + health check
EXPOSE 9090

ENTRYPOINT ["python", "-m", "src.orchestrator"]
