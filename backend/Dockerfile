# Backend image.
#
# Two decisions here are security decisions rather than packaging ones, and
# both are easy to get wrong by following the shortest Dockerfile that works.
#
# 1. **It does not run as root.** The default in a container is uid 0, and a
#    remote-code-execution bug in a root container is root on the container's
#    filesystem -- including the ability to write the application's own code.
#    A dedicated unprivileged user costs three lines.
#
# 2. **Build tools are not in the final image.** Compiling wheels needs a C
#    toolchain; running the application does not. The multi-stage build below
#    leaves gcc behind in the builder, which both shrinks the image and
#    removes the most useful thing an attacker could find inside it.

# ---------------------------------------------------------------------------
# Stage 1: build the dependencies
# ---------------------------------------------------------------------------
FROM python:3.11-slim AS builder

ENV PIP_NO_CACHE_DIR=1 \
    PIP_DISABLE_PIP_VERSION_CHECK=1

# psycopg's C extension and cryptography need a compiler and libpq headers.
RUN apt-get update \
    && apt-get install -y --no-install-recommends build-essential libpq-dev \
    && rm -rf /var/lib/apt/lists/*

WORKDIR /build
COPY requirements.txt .

# Into a virtualenv so the whole dependency tree can be copied as one
# directory, without dragging the toolchain along with it.
RUN python -m venv /opt/venv \
    && /opt/venv/bin/pip install --upgrade pip \
    && /opt/venv/bin/pip install -r requirements.txt

# ---------------------------------------------------------------------------
# Stage 2: the image that actually runs
# ---------------------------------------------------------------------------
FROM python:3.11-slim

ENV PATH="/opt/venv/bin:$PATH" \
    # Never write .pyc files: they are useless in an immutable container and
    # would need a writable application directory, which we do not want.
    PYTHONDONTWRITEBYTECODE=1 \
    # Unbuffered, so logs appear in `docker logs` as they happen rather than
    # when a buffer fills -- which during an incident is the difference
    # between a log and a mystery.
    PYTHONUNBUFFERED=1

# libpq itself, not the headers: the runtime library psycopg links against.
RUN apt-get update \
    && apt-get install -y --no-install-recommends libpq5 \
    && rm -rf /var/lib/apt/lists/* \
    && useradd --create-home --uid 10001 appuser

COPY --from=builder /opt/venv /opt/venv

WORKDIR /app
COPY --chown=appuser:appuser . .

USER appuser

EXPOSE 8000

# No `--reload`, and no `--workers` either: process count belongs to the
# orchestrator, which knows how much CPU the container was given. Baking it
# in here means every deployment gets whatever number seemed right on a
# laptop.
CMD ["uvicorn", "app.main:app", "--host", "0.0.0.0", "--port", "8000"]
