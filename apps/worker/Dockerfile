FROM python:3.14-slim

WORKDIR /app

ENV POETRY_VIRTUALENVS_CREATE=false \
    POETRY_NO_INTERACTION=1

RUN pip install --no-cache-dir poetry

COPY pyproject.toml poetry.lock* ./
RUN poetry install --only main --no-root

COPY . .

CMD ["poetry", "run", "rq", "worker", "backup-runs", "--url", "redis://redis:6379/0"]
