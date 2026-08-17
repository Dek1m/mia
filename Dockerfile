FROM python:3.11-slim AS base

WORKDIR /app

# System deps
RUN apt-get update && apt-get install -y --no-install-recommends \
    gcc libpq-dev && \
    rm -rf /var/lib/apt/lists/*

# Python deps — install from requirements, not pyproject
RUN pip install --no-cache-dir \
    argenta-logging>=0.1.0 \
    argon2-cffi>=23.1.0 \
    PyJWT>=2.8.0 \
    prometheus-client>=0.20.0 \
    asyncpg>=0.29.0 \
    fastapi>=0.100.0 \
    uvicorn>=0.23.0 \
    httpx>=0.24.0

# App code
COPY . .

EXPOSE 8000

CMD ["python", "-m", "modules.rest.server", "--host", "0.0.0.0", "--port", "8000"]
