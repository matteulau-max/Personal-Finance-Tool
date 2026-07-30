# Personal Finance Tool

A personal finance application that aggregates bank, credit card, and payment
accounts via [Plaid](https://plaid.com), keeps a complete and de-duplicated
transaction history, and surfaces spending analytics and AI-driven insights.

Built to production standards, in priority order: **security → data integrity →
maintainability → performance → UX.**

## Stack

| Layer | Technology |
|---|---|
| Frontend | Next.js 16, React 19, TypeScript, Tailwind CSS |
| Backend | Python 3.11, FastAPI |
| Database | PostgreSQL 16, SQLAlchemy 2, Alembic |
| Aggregation | Plaid (`/transactions/sync`, cursor-based) |
| Auth | Clerk (JWT / JWKS verification) |
| Hosting | Vercel (web) · Railway/Render (API + database) |

## Repository layout

```
backend/     FastAPI service — API routes, models, services, tests
frontend/    Next.js application
docs/        Step-by-step milestone guides
docker-compose.yml   Local PostgreSQL
```

## Quick start

Already set up? `./start.sh` starts the database, applies migrations, and runs
both servers in one terminal.

Starting from nothing, and would rather not install anything on your own
machine? [`docs/getting-started-codespaces.md`](docs/getting-started-codespaces.md)
is a step-by-step guide for GitHub Codespaces, written for someone new to this.

Full local instructions live in
[`docs/milestone-01-setup.md`](docs/milestone-01-setup.md). The short version:

```bash
# Database
docker compose up -d

# Backend  →  http://localhost:8000
cd backend
python3 -m venv .venv && source .venv/bin/activate
pip install -r requirements.txt
cp .env.example .env

# Generate an encryption key and paste it into ENCRYPTION_KEYS in .env.
# The placeholder in .env.example is not a valid key, and the server now
# refuses to start with it rather than failing later, when you link a bank.
python -c "from cryptography.fernet import Fernet; print(Fernet.generate_key().decode())"

alembic upgrade head          # create tables + seed categories
uvicorn app.main:app --reload --port 8000
```

Then, in a **second terminal** (the backend keeps running in the first):

```bash
# Frontend →  http://localhost:3000
cd frontend                   # from the repository root, not from backend/
npm install
cp .env.example .env.local
npm run dev
```

Visit <http://localhost:3000> — the status card should report the backend as
**Connected** and the database as **Connected**, with authentication listed as
*Not configured* until you add Clerk keys. That state is expected and the app
runs in it; you just cannot sign in yet.

To go further you need two free accounts, and their keys go in different
places:

| Keys | From | Put them in |
|---|---|---|
| Clerk publishable + secret | Clerk dashboard → API keys | `frontend/.env.local` |
| Plaid client ID + secret | Plaid dashboard → Developers → Keys | `backend/.env` |

Start with `PLAID_ENV=sandbox`: fake banks, test credentials Plaid gives you,
and no real money anywhere near it.

## Tests

```bash
cd backend && source .venv/bin/activate && pytest
```

Tests run against a real PostgreSQL database (`finance_test`, created and
dropped automatically) and apply the real Alembic migrations, so the suite
also verifies that the schema is deployable.

## Database

```bash
alembic upgrade head     # apply migrations
alembic current          # show the applied revision
alembic downgrade -1     # undo the last migration
```

Schema design and the reasoning behind it:
[`docs/milestone-02-database.md`](docs/milestone-02-database.md).

## Analytics

Aggregation happens in PostgreSQL, not in Python: measured at ~50x faster, and
the difference between a 76ms panel and a 3.9s one at 100,000 transactions
(`backend/scripts/benchmark_analytics.py` reproduces it). Transfers are excluded
from spending, income is separated rather than netted, and every figure uses
effective (user-corrected) values. See
[`docs/milestone-06-analytics.md`](docs/milestone-06-analytics.md).

## AI insights

Ask questions in plain English and get answers where **every number is
traceable to a query**. The model chooses which analytics function to call;
PostgreSQL computes the figure; the model only phrases the answer. Before an
answer is returned, every number in it is checked against what the queries
actually produced — anything unaccounted for is discarded rather than shown.
Off unless `ANTHROPIC_API_KEY` is set. See
[`docs/milestone-07-ai-insights.md`](docs/milestone-07-ai-insights.md).

## Categorization

Transactions are enriched as they arrive: merchants are normalized, then
categorized by (in order) a matching rule, the merchant's default category, or
Plaid's suggestion. Enrichment writes only `auto_*` columns, so re-running it
across all history can never destroy a manual correction. See
[`docs/milestone-05-categorization.md`](docs/milestone-05-categorization.md).

## Syncing

Transactions arrive via Plaid's cursor-based `/transactions/sync`. The engine
is idempotent: writes are upserts against the deduplication constraints, the
cursor advances only in the same transaction as the data it covers, and a
sync can never overwrite a user's manual corrections. See
[`docs/milestone-04-plaid.md`](docs/milestone-04-plaid.md) and
`backend/app/services/sync.py`.

## Hardening

The database enforces user isolation itself: every table holding user data
carries a PostgreSQL Row-Level Security policy, and each request switches into
an unprivileged role and declares whose data it is. An unscoped
`select(Transaction)` returns your own rows; a request with no identity returns
none. Plaid webhooks are queued rather than synced inline, so the endpoint
answers in milliseconds and a failed sync retries with backoff instead of
being redelivered forever. The audit log is partitioned by month. See
[`docs/milestone-08-hardening.md`](docs/milestone-08-hardening.md).

## Deployment

`backend/Dockerfile` builds the API image: multi-stage so no compiler ships in
the runtime layer, and running as an unprivileged user. Two processes are
possible from the same image — the API, and `python scripts/run_worker.py` if
you would rather the webhook queue drained somewhere other than the request
path (`WEBHOOK_WORKER_ENABLED=false` on the API in that case).

One step the code cannot take for itself: in production, connect as a login
role that is a *member* of `finance_app` and owns no tables. PostgreSQL exempts
a table's owner from its own policies, so connecting as the migration role
would leave Row-Level Security switched on and doing nothing. The reasoning
and the SQL are in
[`docs/milestone-08-hardening.md`](docs/milestone-08-hardening.md).

## Project status

| Milestone | Scope | Status |
|---|---|---|
| 1 | Development environment & project skeleton | ✅ Complete |
| 2 | Database schema & migrations | ✅ Complete |
| 3 | Authentication & authorization | ✅ Complete |
| 4 | Plaid integration & transaction sync | ✅ Complete |
| 5 | Categorization, merchants, rules, tags | ✅ Complete |
| 6 | Dashboards & analytics | ✅ Complete |
| 7 | AI insights | ✅ Complete |
| 8 | Deployment & hardening | ✅ Complete |

## Security

- No secrets in source control. `.env` is git-ignored; `.env.example` documents
  required variables with placeholder values only.
- CORS is restricted to a single explicit origin — never `*`.
- Every request carries a Clerk-issued JWT, verified against Clerk's JWKS on
  signature, algorithm, expiry, issuer, and authorized party.
- Queries against user-owned tables go through `scoped_select()`, which cannot
  be called without a user. A guard test fails the build if any endpoint is
  added without authentication.
- Authorization failures return 404, never 403, so record existence is not
  disclosed.
- Plaid access tokens are encrypted at rest with rotatable Fernet keys, never
  returned by any endpoint, and never logged.
- Webhooks are verified by ES256 signature, raw-body hash, and a five-minute
  replay window before any payload is parsed.
- The AI layer is read-only and bound to one user: no tool writes, and no tool
  schema accepts a user id, so there is no argument a prompt injection could
  set to reach another person's data.
- PostgreSQL Row-Level Security backs up application-level scoping. Policies
  cover every table with user data, requests run as a role that owns nothing
  and bypasses nothing, and a missing identity yields no rows rather than
  everyone's. Guard tests fail the build if a new table has neither a policy
  nor an explicit exemption.
- `/api/insights` — the one endpoint where a request costs real money — is
  rate limited per user, as are syncing and Plaid link-token creation. The
  public webhook is limited by address.
- Responses carry `nosniff`, `no-referrer`, `DENY` and `no-store`; production
  adds HSTS, disables the interactive docs, and checks the `Host` header.
- Production refuses to start without a host allowlist, an HTTPS frontend
  origin, encryption keys, Clerk configuration, or with `DEBUG=true`.
