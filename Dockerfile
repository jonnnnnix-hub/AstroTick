# syntax=docker/dockerfile:1.6
#
# Dashboard image for Fly.io / any container host.
# Runs the read-only AstroTick web UI from dashboard.py via gunicorn.
# The bot itself (bot.py) is NOT started here - the dashboard will show
# "waiting for data" until bot.py writes dashboard_state.json into /app.
# To run the bot in the same container, override CMD (see fly.toml comments).

FROM python:3.12-slim AS base

ENV PYTHONUNBUFFERED=1 \
    PYTHONDONTWRITEBYTECODE=1 \
    PIP_NO_CACHE_DIR=1 \
    PIP_DISABLE_PIP_VERSION_CHECK=1 \
    PORT=8080

WORKDIR /app

# Minimal system deps; most wheels are prebuilt for slim-bookworm.
RUN apt-get update \
 && apt-get install -y --no-install-recommends curl ca-certificates \
 && rm -rf /var/lib/apt/lists/*

# Only the pieces the dashboard needs at runtime.
# Copy requirements first so Docker caches the pip layer.
COPY requirements.txt ./
RUN pip install --no-cache-dir flask gunicorn python-dotenv

# Copy the dashboard and anything it touches.
COPY dashboard.py ./
# trades.csv is optional at build time; create an empty header if missing.
COPY trades.csv* ./

# Run as non-root.
RUN useradd --create-home --uid 1000 astrotick \
 && chown -R astrotick:astrotick /app
USER astrotick

EXPOSE 8080

HEALTHCHECK --interval=30s --timeout=5s --start-period=10s --retries=3 \
  CMD curl -fsS "http://127.0.0.1:${PORT}/api/health" || exit 1

# gunicorn serves the Flask app defined as `app` in dashboard.py.
# PORT is supplied by Fly.io (and defaults to 8080).
CMD ["sh", "-c", "gunicorn -w 2 -b 0.0.0.0:${PORT} --access-logfile - dashboard:app"]
