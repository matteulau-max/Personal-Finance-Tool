# Milestone 1 — Development Environment & Project Skeleton

## 1. Goal

Get a working development environment on your machine, and prove that all
three pieces of the system can talk to each other:

```
Next.js frontend  ──HTTP──▶  FastAPI backend  ──SQL──▶  PostgreSQL
   (port 3000)                  (port 8000)              (port 5432)
```

No Plaid, no auth, no database tables yet. Just a skeleton that runs.

## 2. Why this matters

The single most common way beginner projects die is that setup problems and
feature problems get tangled together. When your page is blank, you need to
already know that "the servers run and can reach each other" — so the bug
must be in the code you just wrote.

This milestone buys you that certainty, permanently.

---

## 3. Install the required software

Check what you already have. Open a terminal and run each line:

```bash
git --version        # need 2.30+
python3 --version    # need 3.11+
node --version       # need 20+
docker --version     # any recent version
```

Anything that says "command not found" needs installing:

| Tool | macOS | Windows | What it's for |
|---|---|---|---|
| Git | `brew install git` | [git-scm.com](https://git-scm.com/download/win) | Version control |
| Python 3.11+ | `brew install python@3.11` | [python.org](https://www.python.org/downloads/) — **tick "Add Python to PATH"** | Backend |
| Node 20+ | `brew install node` | [nodejs.org](https://nodejs.org) (LTS) | Frontend |
| Docker Desktop | [docker.com](https://www.docker.com/products/docker-desktop/) | same | Local database |

> **Windows users:** everything below works in PowerShell, but the virtual
> environment activation command differs — it's noted where it matters.

---

## 4. Get the code

```bash
git clone https://github.com/matteulau-max/personal-finance-tool.git
cd personal-finance-tool
git checkout claude/personal-finance-dashboard-qqb2rg
```

`clone` downloads the repository. `checkout` switches to the branch this work
lives on. More on branches in §9.

---

## 5. Understanding the folder structure

```
personal-finance-tool/
├── backend/                 ← Python / FastAPI
│   ├── app/
│   │   ├── main.py          ← assembles the app; stays tiny forever
│   │   ├── api/routes/      ← HTTP endpoints, grouped by topic
│   │   ├── core/            ← config, security, shared plumbing
│   │   ├── db/              ← database session & engine  (Milestone 3)
│   │   ├── models/          ← SQLAlchemy tables          (Milestone 3)
│   │   ├── schemas/         ← Pydantic request/response shapes
│   │   └── services/        ← business logic (Plaid sync, categorization)
│   ├── tests/               ← pytest lives here
│   ├── requirements.txt     ← pinned Python dependencies
│   └── .env.example         ← template for secrets (safe to commit)
├── frontend/                ← Next.js / React / TypeScript
│   └── src/
│       ├── app/             ← routes; each folder = a URL
│       └── lib/             ← shared helpers (API client, formatting)
├── docs/                    ← these milestone guides
└── docker-compose.yml       ← local PostgreSQL
```

**The design decision worth understanding:** notice that `models/`,
`services/`, and `api/routes/` are separate. A route's only job is to handle
HTTP (read the request, return a response). A service's job is to know the
*rules* — how to deduplicate a transaction, how to normalize a merchant name.
Keeping them apart means you can test the rules without HTTP, and reuse them
later from a background sync job that has no HTTP at all.

---

## 6. Run the backend

**Step 6a — create a virtual environment.** This is a private folder of Python
packages that belongs to this project only, so two projects can't fight over
versions.

```bash
cd backend
python3 -m venv .venv
```

**Step 6b — activate it.** You must do this in *every new terminal window*.

```bash
source .venv/bin/activate          # macOS / Linux
.venv\Scripts\Activate.ps1         # Windows PowerShell
```

Your prompt now starts with `(.venv)`. That's how you know it worked.

**Step 6c — install dependencies:**

```bash
pip install -r requirements.txt
```

**Step 6d — create your local config file:**

```bash
cp .env.example .env               # macOS / Linux
copy .env.example .env             # Windows
```

`.env` is git-ignored. It is where real secrets will go later. `.env.example`
is committed, and must never contain a real value.

**Step 6e — run the server:**

```bash
uvicorn app.main:app --reload --port 8000
```

`--reload` restarts the server whenever you save a file.

**Verify:** open <http://localhost:8000/health> — you should see:

```json
{"status":"ok","app_name":"Personal Finance API","environment":"local"}
```

Then open <http://localhost:8000/docs>. That interactive API explorer was
generated automatically from the type hints in the code. You get it free, and
it stays correct as long as your type hints are correct.

---

## 7. Run the tests

Leave the server running and open a **second terminal**:

```bash
cd backend
source .venv/bin/activate
pytest
```

Expected: `3 passed`.

Now deliberately break it — in `app/api/routes/health.py`, change
`status="ok"` to `status="fine"`, save, and run `pytest` again. One test
fails, and pytest shows you exactly which assertion and what it got instead.
**Change it back.** That feedback loop is the entire value of testing.

---

## 8. Run the frontend and the database

**Database** (new terminal, from the repository root):

```bash
docker compose up -d
docker compose ps          # should show finance-postgres as healthy
```

Nothing uses it yet — we're just confirming it starts. Milestone 3 fills it.

**Frontend** (new terminal):

```bash
cd frontend
npm install
cp .env.example .env.local     # copy .env.example .env.local  on Windows
npm run dev
```

**Verify:** open <http://localhost:3000>. You should see a status card
reading:

- Frontend: **Running**
- Backend API: **Connected**
- Environment: **local**

If the backend says "Not reachable", your backend terminal isn't running —
go back to step 6e. That page is a permanent smoke test: any time something
feels broken later, check it first.

You now have four terminals open (backend, tests, database, frontend). That
is normal and expected for full-stack work.

---

## 9. Git, from the beginning

Git records **snapshots** of your project. Each snapshot is a *commit*, and
you can return to any of them. Three places code lives:

```
working directory  →  staging area  →  repository  →  GitHub
   (your edits)         (git add)      (git commit)   (git push)
```

The staging area exists so you can commit *some* of your changes and not
others — it lets a commit be one coherent idea rather than "everything I did
today."

One-time setup:

```bash
git config --global user.name "Your Name"
git config --global user.email "matteulau@gmail.com"
```

The everyday loop:

```bash
git status                     # what changed? run this constantly
git diff                       # show me the actual line-by-line changes
git add backend/app/main.py    # stage one file
git add .                      # or stage everything
git commit -m "Add health endpoint"
git push -u origin claude/personal-finance-dashboard-qqb2rg
```

**Branches.** A branch is an independent line of work. `main` should always
be code that works. New work happens on its own branch and merges back when
it's proven:

```bash
git switch -c my-new-feature      # create a branch and move onto it
git switch main                   # go back
git branch                        # list branches; * marks where you are
```

**Pull requests** (GitHub's merge review step) come in Milestone 2, when
there is real work to review.

**Commit message rule:** imperative mood, explains *why* when it isn't
obvious. `Add rule-based categorization engine` — not `stuff`, not `fix`.

---

## 10. Security concepts introduced here

Three habits start now and never stop:

1. **Secrets live in environment variables, never in code.** A password typed
   into a source file is in your Git history *forever*, even if you delete it
   in a later commit. `.env` is git-ignored; `.env.example` documents which
   variables exist without revealing values.
2. **`.gitignore` before your first commit.** Check `git status` before every
   commit and make sure nothing secret is listed.
3. **CORS is a whitelist, not a wildcard.** `app/main.py` allows exactly
   `http://localhost:3000`. When we deploy, that becomes your one real
   domain. `allow_origins=["*"]` combined with credentials is a genuine
   vulnerability, and you'll see it recommended all over the internet.

---

## 11. Troubleshooting

| Symptom | Cause | Fix |
|---|---|---|
| `command not found: python3` | Not installed, or not on PATH | Reinstall; on Windows tick "Add Python to PATH" |
| `ModuleNotFoundError: fastapi` | venv not activated | `source .venv/bin/activate`, then `pip install -r requirements.txt` |
| `Address already in use` (8000) | Old server still running | `lsof -ti:8000 \| xargs kill` (mac/Linux) or use `--port 8001` |
| Frontend shows "Not reachable" | Backend down, or wrong port | Confirm <http://localhost:8000/health> loads |
| CORS error in browser console | Frontend origin not whitelisted | Check `FRONTEND_ORIGIN` in `backend/.env` matches exactly, no trailing slash |
| `docker: Cannot connect to the Docker daemon` | Docker Desktop not started | Launch Docker Desktop, wait for the whale icon |
| `pytest: command not found` | venv not activated | Activate it |

**How to read an error:** start at the *bottom* of a Python traceback — that's
the actual error. The lines above are the path that led there.

---

## 12. Common mistakes

- Forgetting to activate the venv in a new terminal (by far #1).
- Committing `.env`. Check `git status` first, every time.
- Editing `.env.example` instead of `.env` — real values go in `.env`.
- Running `pytest` from the repo root instead of `backend/`.
- Killing the backend terminal and wondering why the frontend broke.

---

## 13. Checklist before Milestone 2

- [ ] `git --version`, `python3 --version`, `node --version`, `docker --version` all work
- [ ] Repository cloned, on branch `claude/personal-finance-dashboard-qqb2rg`
- [ ] <http://localhost:8000/health> returns `{"status":"ok",...}`
- [ ] <http://localhost:8000/docs> loads the interactive API explorer
- [ ] `pytest` prints `3 passed`
- [ ] You broke a test on purpose, watched it fail, and fixed it
- [ ] `docker compose ps` shows `finance-postgres` healthy
- [ ] <http://localhost:3000> shows Backend API: **Connected**
- [ ] `git status` shows no `.env` file waiting to be committed
- [ ] You've run `git status`, `git diff`, and `git log` at least once each

---

## What's next — Milestone 2 preview

**Database design.** We'll model Users, Institutions, Accounts, Transactions,
Categories, Merchants, Tags, and the audit trail — and I'll explain the two
decisions that make or break this application:

- how to guarantee a transaction is **never** imported twice, and
- how to let you correct a transaction while **never** destroying what the
  bank originally sent.
