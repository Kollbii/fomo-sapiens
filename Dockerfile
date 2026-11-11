FROM python:3.11-slim

ENV PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1 \
    PIP_NO_CACHE_DIR=1

WORKDIR /app

# Install system utilities and uv for fast dependency resolution
RUN apt-get update && apt-get install -y --no-install-recommends \
    curl \
    && rm -rf /var/lib/apt/lists/* \
    && pip install --upgrade pip uv

# Copy project dependency definitions first for layer caching
COPY pyproject.toml AGENTS.md ./
COPY config/ ./config/
COPY docs/ ./docs/
COPY src/ ./src/

# Install dependencies into system Python inside container
RUN uv pip install --system ".[dev]"

# Ensure local data directories exist for CSV volume mounts
RUN mkdir -p /app/data/snapshots /app/data/latest /app/data/metadata /app/data/logs

EXPOSE 8000

CMD ["uvicorn", "src.main:app", "--host", "0.0.0.0", "--port", "8000"]