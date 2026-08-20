FROM python:3.11-slim AS base

WORKDIR /app

# System deps
RUN apt-get update && apt-get install -y --no-install-recommends \
    gcc libpq-dev git && \
    rm -rf /var/lib/apt/lists/*

# Python deps — PyPI packages only
RUN pip install --no-cache-dir \
    argon2-cffi>=23.1.0 \
    "PyJWT>=2.8.0" \
    prometheus-client>=0.20.0 \
    "psycopg[binary]>=3.0.0" \
    psycopg_pool \
    httpx>=0.24.0 \
    "celery[redis]>=5.5,<6" \
    cryptography>=42

RUN pip install --no-cache-dir git+https://github.com/Dek1m/argenta-logging.git

# App code
COPY . .

# Библиотека. HTTP — belle (main.py). Воркер — python -m modules.worker
CMD ["python", "-c", "from core.application import Application"]
