FROM python:3.11-slim AS base

WORKDIR /app

# System deps
RUN apt-get update && apt-get install -y --no-install-recommends \
    gcc libpq-dev && \
    rm -rf /var/lib/apt/lists/*

# Python deps
COPY pyproject.toml .
RUN pip install --no-cache-dir -e ".[metrics,db]" 2>/dev/null || \
    pip install --no-cache-dir argenta-logging argon2-cffi PyJWT prometheus-client asyncpg

# App code
COPY . .

EXPOSE 8000

CMD ["python", "-m", "modules.rest.server", "--host", "0.0.0.0", "--port", "8000"]
