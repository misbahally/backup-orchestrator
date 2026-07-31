import logging
import os

from prometheus_client import start_http_server
from redis import Redis
from rq import Connection, Queue, Worker

logging.basicConfig(level=getattr(logging, os.environ.get("LOG_LEVEL", "INFO").upper(), logging.INFO))


def main() -> None:
    metrics_port = int(os.environ.get("METRICS_PORT", "9090"))
    start_http_server(metrics_port)

    redis_url = os.environ.get("REDIS_URL", "redis://redis:6379/0")
    conn = Redis.from_url(redis_url)
    with Connection(conn):
        worker = Worker([Queue("backup-runs")])
        worker.work()


if __name__ == "__main__":
    main()
