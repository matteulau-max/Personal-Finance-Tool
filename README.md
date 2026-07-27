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
| Aggregation | Plaid |
| Auth | Clerk *(planned)* |
| Hosting | Vercel (web) · Railway/Render (API + database) |

## Repository layout

```
backend/     FastAPI service — API routes, models, services, tests
frontend/    Next.js application
docs/        Step-by-step milestone guides
docker-compose.yml   Local PostgreSQL
```

## Quick start

Full, beginner-oriented instructions live in
[`docs/milestone-01-setup.md`](docs/milestone-01-setup.md). The short version:

```bash
# Database
docker compose up -d

# Backend  →  http://localhost:8000
cd backend
python3 -m venv .venv && source .venv/bin/activate
pip install -r requirements.txt
cp .env.example .env
uvicorn app.main:app --reload --port 8000

# Frontend →  http://localhost:3000
cd frontend
npm install
cp .env.example .env.local
npm run dev
```

Visit <http://localhost:3000> — the status card should report the backend as
**Connected**.

## Tests

```bash
cd backend && source .venv/bin/activate && pytest
```

## Project status

| Milestone | Scope | Status |
|---|---|---|
| 1 | Development environment & project skeleton | ✅ Complete |
| 2 | Database schema & migrations | ⬜ Next |
| 3 | Authentication | ⬜ |
| 4 | Plaid integration & transaction sync | ⬜ |
| 5 | Categorization, merchants, rules, tags | ⬜ |
| 6 | Dashboards & analytics | ⬜ |
| 7 | AI insights | ⬜ |
| 8 | Deployment & hardening | ⬜ |

## Security

- No secrets in source control. `.env` is git-ignored; `.env.example` documents
  required variables with placeholder values only.
- CORS is restricted to a single explicit origin — never `*`.
- Every request is authenticated and scoped to its owning user *(from
  Milestone 3)*.
