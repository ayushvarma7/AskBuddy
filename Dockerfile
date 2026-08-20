# Ask Buddy — Slack listener image.
#
# Socket Mode means no inbound port and no public URL: the container dials out
# to Slack, so it needs no ports published and no ingress.
#
# Build:  docker build -t askbuddy .
# Run:    docker compose -f docker-compose.askbuddy.yml up -d

FROM python:3.13-slim AS base

# uv is the project's package manager; copy the released binary rather than
# pip-installing it, so the image doesn't carry pip's resolver too.
COPY --from=ghcr.io/astral-sh/uv:latest /uv /uvx /bin/

ENV PYTHONUNBUFFERED=1 \
    PYTHONDONTWRITEBYTECODE=1 \
    UV_COMPILE_BYTECODE=1 \
    UV_LINK_MODE=copy \
    UV_PROJECT_ENVIRONMENT=/app/.venv \
    PATH="/app/.venv/bin:$PATH"

WORKDIR /app

# psycopg2-binary ships wheels, so no build toolchain is needed. libpq is
# still wanted at runtime for the SQLAlchemy job store's connections.
RUN apt-get update \
    && apt-get install --no-install-recommends -y libpq5 \
    && rm -rf /var/lib/apt/lists/*

# Dependencies first, in their own layer: application edits then rebuild in
# seconds instead of re-resolving the whole tree.
COPY pyproject.toml uv.lock ./
RUN uv sync --no-dev --no-install-project

COPY src/ ./src/
COPY data/ ./data/
COPY main.py ./

# Install the project itself now that its sources are present.
RUN uv sync --no-dev

# Don't run as root — the bot needs no privileges beyond outbound network.
RUN useradd --create-home --uid 10001 askbuddy \
    && chown -R askbuddy:askbuddy /app
USER askbuddy

# JSON logs by default in a container, where something is collecting stdout.
ENV ASK_BUDDY_LOG_FORMAT=json

# No HTTP surface to probe, so liveness is "can the process import its own
# modules and reach its config" rather than a request.
HEALTHCHECK --interval=60s --timeout=10s --start-period=20s --retries=3 \
    CMD python -c "import src.ask_buddy.lifecycle, src.ask_buddy.db; import os; \
raise SystemExit(0 if os.environ.get('ASK_BUDDY_DB_DSN') else 1)"

# Exec form so the process is PID 1 and receives SIGTERM directly — that is
# what triggers the graceful shutdown in lifecycle.install_signal_handlers.
CMD ["python", "-m", "src.ask_buddy.slack_listener"]
