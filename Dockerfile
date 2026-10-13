# syntax=docker/dockerfile:1
FROM caddy:2.11.7-builder-alpine AS caddy-builder
RUN CGO_ENABLED=0 xcaddy build v2.11.7 --output /usr/bin/caddy \
    --with github.com/mholt/caddy-ratelimit@5625512f24f6f59d6f64fb3aafe5eecff0b286db

FROM python:3.14-slim AS builder
RUN apt-get update && apt-get install -y --no-install-recommends gettext \
    && rm -rf /var/lib/apt/lists/*
COPY --from=ghcr.io/astral-sh/uv:0.12.10 /uv /usr/local/bin/uv
ENV UV_COMPILE_BYTECODE=1 UV_LINK_MODE=copy UV_PYTHON_DOWNLOADS=never
WORKDIR /app
COPY pyproject.toml uv.lock ./
RUN uv sync --frozen --no-dev
COPY . .
RUN .venv/bin/python manage.py compilemessages -l fa --ignore=.venv --settings=config.settings.build --verbosity 0 \
    && .venv/bin/python manage.py collectstatic --noinput --settings=config.settings.build

FROM python:3.14-slim AS runtime
ARG COTINGA_REVISION=""
ENV PYTHONDONTWRITEBYTECODE=1 PYTHONUNBUFFERED=1 \
    COTINGA_REVISION=${COTINGA_REVISION} \
    DJANGO_SETTINGS_MODULE=config.settings.production \
    DJANGO_SQLITE_PATH=/data/db.sqlite3 \
    PATH="/app/.venv/bin:$PATH"
WORKDIR /app
COPY --from=caddy-builder /usr/bin/caddy /usr/local/bin/caddy
RUN groupadd --gid 10001 app && useradd --uid 10001 --gid app app \
    && mkdir /data && chown app:app /data
COPY --from=builder --chown=app:app /app /app
USER app
EXPOSE 8000
CMD ["sh", "/app/deploy/entrypoint.sh"]
