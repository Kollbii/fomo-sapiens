# syntax=docker/dockerfile:1.7
# Reproducible, non-root runtime image for the read-only IBKR Options Analyzer.
#
# Stage 1 resolves dependencies strictly from uv.lock (no dev tools). Stage 2 contains only the
# virtualenv, application source and config. The app runs from /app (not site-packages) because
# `src.core.config.PROJECT_ROOT` resolves `config/settings.yaml` relative to the source tree.

ARG PYTHON_VERSION=3.11
ARG UV_VERSION=0.13

FROM ghcr.io/astral-sh/uv:${UV_VERSION} AS uv

FROM python:${PYTHON_VERSION}-slim AS builder
COPY --from=uv /uv /usr/local/bin/uv
ENV UV_COMPILE_BYTECODE=1 \
    UV_LINK_MODE=copy \
    UV_PYTHON_DOWNLOADS=never \
    UV_PROJECT_ENVIRONMENT=/opt/venv
WORKDIR /app
# Dependency layer: invalidated only when the lockfile or project metadata change.
COPY pyproject.toml uv.lock ./
RUN --mount=type=cache,target=/root/.cache/uv \
    uv sync --locked --no-dev --no-install-project

FROM python:${PYTHON_VERSION}-slim AS runtime
ENV PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1 \
    PATH="/opt/venv/bin:${PATH}"
RUN groupadd --system --gid 10001 app \
    && useradd --system --uid 10001 --gid app --home-dir /app --shell /usr/sbin/nologin app
WORKDIR /app
COPY --from=builder /opt/venv /opt/venv
COPY config/ ./config/
COPY src/ ./src/
# data/ is a bind mount in docker-compose; create it so the image also runs standalone.
RUN mkdir -p /app/data && chown app:app /app/data
USER app
EXPOSE 8000
HEALTHCHECK --interval=30s --timeout=5s --start-period=30s --retries=3 \
    CMD ["python", "-c", "import urllib.request; urllib.request.urlopen('http://127.0.0.1:8000/api/status', timeout=4)"]
# 0.0.0.0 is container-internal only; docker-compose publishes it on 127.0.0.1 exclusively.
CMD ["uvicorn", "src.main:app", "--host", "0.0.0.0", "--port", "8000"]
