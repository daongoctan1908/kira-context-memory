# syntax=docker/dockerfile:1

FROM ghcr.io/astral-sh/uv:0.11.2 AS uv

FROM python:3.11-slim-bookworm AS builder

ENV UV_COMPILE_BYTECODE=1 \
    UV_LINK_MODE=copy \
    UV_PYTHON_DOWNLOADS=never

WORKDIR /app

COPY --from=uv /uv /uvx /bin/
COPY pyproject.toml uv.lock README.md ./
COPY app ./app
COPY alembic.ini ./
COPY migrations ./migrations
COPY worker ./worker

RUN uv sync --frozen --no-dev --no-editable

FROM python:3.11-slim-bookworm AS runtime

ARG APP_VERSION=0.1.0

LABEL org.opencontainers.image.title="kira-context-memory" \
      org.opencontainers.image.version="${APP_VERSION}"

ENV APP_VERSION=${APP_VERSION} \
    PATH="/app/.venv/bin:${PATH}" \
    PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1

WORKDIR /app

RUN groupadd --system --gid 10001 kira \
    && useradd --system --uid 10001 --gid kira --home-dir /app --shell /usr/sbin/nologin kira

COPY --from=builder --chown=kira:kira /app/.venv /app/.venv
COPY --chown=kira:kira app ./app
COPY --chown=kira:kira alembic.ini ./
COPY --chown=kira:kira migrations ./migrations
COPY --chown=kira:kira worker ./worker

USER kira

EXPOSE 8000

HEALTHCHECK --interval=30s --timeout=3s --start-period=10s --retries=3 \
    CMD ["python", "-c", "import urllib.request; urllib.request.urlopen('http://127.0.0.1:8000/health', timeout=2).read()"]

CMD ["uvicorn", "app.presentation.api.main:app", "--host", "0.0.0.0", "--port", "8000"]
