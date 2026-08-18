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
    fastapi>=0.100.0 \
    uvicorn>=0.23.0 \
    httpx>=0.24.0

# argenta-logging from GitHub (internal package)
RUN pip install --no-cache-dir git+https://github.com/Dek1m/argenta-logging.git 2>/dev/null || \
    echo "argenta-logging not available, using stdlib logging"

# App code
COPY . .

EXPOSE 8000

CMD ["python", "-m", "modules.rest.server", "--host", "0.0.0.0", "--port", "8000"]
