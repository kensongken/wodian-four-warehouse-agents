FROM ghcr.io/astral-sh/uv:0.8.22@sha256:9874eb7afe5ca16c363fe80b294fe700e460df29a55532bbfea234a0f12eddb1 AS uv
FROM python:3.12.11-slim-bookworm@sha256:519591d6871b7bc437060736b9f7456b8731f1499a57e22e6c285135ae657bf7
COPY --from=uv /uv /usr/local/bin/uv
WORKDIR /app
ENV UV_COMPILE_BYTECODE=1 UV_LINK_MODE=copy PYTHONUNBUFFERED=1 APP_ENV=production
COPY pyproject.toml uv.lock ./
RUN uv sync --frozen --no-dev --no-install-project
COPY src ./src
COPY alembic-postgresql.ini ./
# Keep both migration lineages in their original directories.
COPY alembic ./alembic
COPY alembic_postgresql ./alembic_postgresql
COPY alembic.ini ./
RUN uv sync --frozen --no-dev
# CloudBase may preserve a historical service-level command containing only
# `api`. Provide a real executable for that override instead of relying on the
# image CMD being retained by every deployment version.
RUN printf '%s\n' '#!/bin/sh' 'exec python -m opc_style.production_bootstrap' > /usr/local/bin/api \
    && chmod 0755 /usr/local/bin/api
RUN groupadd -g 10001 style && useradd -u 10001 -g style -M style
ARG SOURCE_SHA
ENV SOURCE_SHA=${SOURCE_SHA} PATH=/app/.venv/bin:$PATH
USER 10001:10001
EXPOSE 8080
HEALTHCHECK --interval=30s --timeout=5s --start-period=30s CMD python -c "import os, urllib.request; urllib.request.urlopen('http://127.0.0.1:' + os.environ.get('PORT', '8080') + '/health', timeout=3)"
# Bind liveness first, then load the full API graph behind a fail-closed gate.
# Migration/grant/executor versions still override this command explicitly.
CMD ["python", "-m", "opc_style.production_bootstrap"]
