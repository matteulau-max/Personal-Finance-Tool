#!/usr/bin/env bash
#
# Start everything: database, API, web app. One command, one terminal.
#
# Usage, from anywhere:
#
#     ./start.sh
#
# Press Ctrl+C once to stop all of it.
#
# WHY THIS EXISTS
# ---------------
# Starting this project by hand means seven commands across two terminals, in
# the right order, from the right directories, with a virtualenv activated in
# one of them. That is fine once and miserable on the fourth restart -- and
# cloud development environments restart often, because they stop themselves
# when idle. Every step below is one that is easy to forget and confusing to
# diagnose:
#
#   - Ports left occupied by a *suspended* process. Ctrl+Z does not stop a
#     server, it freezes it, and a frozen server keeps its port. Next.js then
#     quietly starts on 3001 instead, the URL you have open shows nothing, and
#     nothing anywhere says why. We kill by port, with the signal a suspended
#     process cannot ignore.
#   - Forgetting `source .venv/bin/activate`, which makes `uvicorn` and
#     `alembic` look like they are not installed when they are.
#   - Forgetting `alembic upgrade head` after pulling new code, which fails
#     later as a confusing database error rather than immediately.
#
# Deliberately NOT here: anything that installs. `npm install` and
# `pip install` belong to setup, not to starting, and running them on every
# boot would turn a two-second start into a two-minute one. See
# docs/getting-started-codespaces.md for first-time setup.

set -euo pipefail

# Resolve the repository root from this script's own location, so the script
# works regardless of the directory you run it from.
ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
cd "$ROOT"

API_PORT="${API_PORT:-8000}"
WEB_PORT="${WEB_PORT:-3000}"

info() { printf '\033[1;36m==>\033[0m %s\n' "$1"; }
warn() { printf '\033[1;33m==>\033[0m %s\n' "$1"; }
fail() { printf '\033[1;31m==>\033[0m %s\n' "$1" >&2; exit 1; }

# --- preflight -------------------------------------------------------------

[[ -f backend/.env ]] || fail "backend/.env is missing. Copy backend/.env.example to backend/.env and fill it in -- see docs/getting-started-codespaces.md"
[[ -f frontend/.env.local ]] || fail "frontend/.env.local is missing. Copy frontend/.env.example to frontend/.env.local and fill it in -- see docs/getting-started-codespaces.md"
[[ -d backend/.venv ]] || fail "backend/.venv is missing. Run the one-time setup in docs/getting-started-codespaces.md first."
[[ -d frontend/node_modules ]] || fail "frontend/node_modules is missing. Run 'cd frontend && npm install' first."

# --- free the ports -------------------------------------------------------
#
# -9 is deliberate. A process suspended with Ctrl+Z cannot act on the polite
# termination signal -- it is not running, so it cannot handle anything -- but
# it still holds the port. Only SIGKILL, which the kernel applies without the
# process's cooperation, reliably clears it.

free_port() {
  local port="$1"
  if command -v fuser >/dev/null 2>&1; then
    fuser -k -9 "${port}/tcp" >/dev/null 2>&1 || true
  else
    # No fuser (it lives in psmisc, not always installed). lsof is the usual
    # alternative; if neither exists we simply proceed and let the server
    # report the conflict itself.
    command -v lsof >/dev/null 2>&1 && kill -9 $(lsof -t -i ":${port}" 2>/dev/null) 2>/dev/null || true
  fi
}

info "Clearing ports ${API_PORT} and ${WEB_PORT}"
free_port "$API_PORT"
free_port "$WEB_PORT"

# --- database -------------------------------------------------------------
#
# Two ways PostgreSQL might be running: the docker-compose service, or a
# server installed directly in the container. Codespaces images often include
# PostgreSQL already, in which case Docker is unnecessary. Try in order of
# least surprise and stop as soon as something answers.

DB_HOST=localhost
DB_PORT=5432

db_ready() {
  if command -v pg_isready >/dev/null 2>&1; then
    pg_isready -h "$DB_HOST" -p "$DB_PORT" -q
  else
    # Fall back to a plain TCP check. Bash's /dev/tcp needs no extra tooling.
    (exec 3<>"/dev/tcp/${DB_HOST}/${DB_PORT}") 2>/dev/null
  fi
}

if db_ready; then
  info "PostgreSQL already running"
elif command -v docker >/dev/null 2>&1 && docker info >/dev/null 2>&1; then
  info "Starting PostgreSQL via docker compose"
  docker compose up -d
elif command -v pg_ctlcluster >/dev/null 2>&1; then
  info "Starting the system PostgreSQL server"
  sudo pg_ctlcluster "$(pg_lsclusters -h | awk 'NR==1{print $1}')" main start || true
else
  sudo service postgresql start >/dev/null 2>&1 || true
fi

# Give it a few seconds to accept connections. A server that has been asked to
# start is not the same as a server ready to answer, and migrations run next.
for _ in $(seq 1 20); do
  db_ready && break
  sleep 0.5
done
db_ready || fail "PostgreSQL is not accepting connections on ${DB_HOST}:${DB_PORT}. See docs/getting-started-codespaces.md"
info "PostgreSQL ready"

# --- migrations -----------------------------------------------------------
#
# Activating the virtualenv rather than calling .venv/bin/uvicorn directly:
# the test suite shells out to a bare `alembic`, so having the venv on PATH is
# the state the project expects anyway.

# shellcheck disable=SC1091
source backend/.venv/bin/activate

info "Applying database migrations"
(cd backend && alembic upgrade head)

# --- run both -------------------------------------------------------------
#
# Both servers run in this one terminal, their output prefixed so you can tell
# which is complaining. The trap means a single Ctrl+C takes down both rather
# than orphaning one to hold its port for next time.

pids=()

cleanup() {
  trap - INT TERM EXIT
  info "Stopping"
  for pid in "${pids[@]:-}"; do
    [[ -n "$pid" ]] && kill "$pid" 2>/dev/null || true
  done
  # The servers spawn children of their own (Next.js in particular), so also
  # sweep the ports rather than trusting the parent to tidy up after itself.
  free_port "$API_PORT"
  free_port "$WEB_PORT"
  wait 2>/dev/null || true
}
trap cleanup INT TERM EXIT

info "Starting API on port ${API_PORT}"
(
  cd backend
  exec uvicorn app.main:app --reload --port "$API_PORT" 2>&1
) | sed -u 's/^/[api] /' &
pids+=("$!")

info "Starting web app on port ${WEB_PORT}"
(
  cd frontend
  exec npm run dev -- --port "$WEB_PORT" 2>&1
) | sed -u 's/^/[web] /' &
pids+=("$!")

cat <<BANNER

  Web app   http://localhost:${WEB_PORT}
  API       http://localhost:${API_PORT}/health

  In a Codespace, open the forwarded URL from the PORTS tab rather than
  these -- and use the SAME address every time. Switching between the
  localhost one and the github.dev one changes the browser's origin, which
  Server Actions check.

  Ctrl+C stops both. Not Ctrl+Z: that freezes them still holding the ports.

BANNER

# Wait for either server to exit. Without this the script would finish
# immediately and the trap would shut down the very servers it just started.
wait
