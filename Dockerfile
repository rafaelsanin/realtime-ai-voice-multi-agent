FROM python:3.12-slim

# uv manages the venv/lockfile locally; reuse it here instead of pip so the
# container gets the exact versions from uv.lock.
COPY --from=ghcr.io/astral-sh/uv:0.9.6 /uv /uvx /bin/

WORKDIR /app

# Dependencies first so code-only changes don't bust the layer cache.
COPY pyproject.toml uv.lock ./
RUN uv sync --locked --no-install-project --no-dev

COPY bot.py call_state.py db.py dispatcher.py reservations.py session.py settings.py workers.py ./
RUN uv sync --locked --no-dev

# Log format isn't pinned here -- settings.py detects Fly.io/ECS and switches
# to JSON on its own; set LOG_FORMAT explicitly to override.

RUN useradd --create-home --uid 10001 app && chown -R app:app /app
USER app

# Not `uv run`, which re-syncs (and would reinstall dev deps) on every start.
CMD ["/app/.venv/bin/python", "bot.py"]
