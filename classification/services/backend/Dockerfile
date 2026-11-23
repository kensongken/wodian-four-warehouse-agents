FROM python:3.12-slim

ENV PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1 \
    PATH="/app/.venv/bin:$PATH"

RUN python -m pip install --no-cache-dir uv==0.8.15 \
    && useradd --create-home --uid 10001 app

WORKDIR /app

COPY --chown=app:app pyproject.toml uv.lock ./
RUN uv sync --frozen --no-dev --no-install-project

COPY --chown=app:app src ./src
COPY --chown=app:app migrations ./migrations
COPY --chown=app:app alembic.ini ./alembic.ini
RUN uv sync --frozen --no-dev

USER app

EXPOSE 8000

CMD [".venv/bin/uvicorn", "product_engine.api.main:app", "--host", "0.0.0.0", "--port", "8000"]
