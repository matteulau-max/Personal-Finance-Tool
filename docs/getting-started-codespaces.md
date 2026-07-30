# Getting started in GitHub Codespaces

A plain-English guide to running this project **without installing anything on
your own computer**. Everything runs on a machine GitHub rents you; you reach
it through your browser.

Written for someone who has not done this before. If a step assumes knowledge
you do not have, that is a bug in this document.

---

## The idea

A Codespace is a computer in the cloud with your code already on it, which you
control through a browser window that looks like a code editor. It has three
parts you will use:

| Part | What it is |
|---|---|
| **File explorer** (left) | The files. Click one to edit it. |
| **Terminal** (bottom) | Where you type commands. Text goes in, text comes out. |
| **Ports tab** | The web addresses your running app is reachable at. |

Two things about terminals that cause most early confusion:

- **A terminal is not a file.** Typing `CLERK_ISSUER=https://...` into a
  terminal appears to work — no error, nothing happens. The setting is thrown
  away the moment the command ends, and the app never sees it. Settings must
  be **saved into a file**.
- **Commands run from a folder.** `npm run dev` works in `frontend/` and fails
  in the repository root with `ENOENT ... Could not read package.json`. `ENOENT`
  means "no such file" and nearly always means *you are in the wrong folder*,
  not that anything is broken.

Codespaces **stops itself when you stop using it.** When you come back the
terminals are empty and your servers are gone. Nothing is lost — your files,
your saved keys, and your database survive. Only the running programs stop, and
`./start.sh` brings them back.

---

## One-time setup

Do this once per Codespace. It takes about ten minutes, most of it waiting.

### 1. Open a Codespace

On the repository page on GitHub: **Code** → **Codespaces** → **Create
codespace on main**. Wait for the editor to appear.

### 2. Install the dependencies

Open a terminal (**Terminal** → **New Terminal**) and paste these one at a
time, waiting for each to finish:

```bash
cd /workspaces/Personal-Finance-Tool/backend
python3 -m venv .venv
source .venv/bin/activate
pip install -r requirements.txt
```

```bash
cd /workspaces/Personal-Finance-Tool/frontend
npm install
```

`python3 -m venv .venv` creates a private folder of Python packages for this
project alone, so it cannot conflict with anything else. `source
.venv/bin/activate` switches your terminal into it — **that one has to be
redone in every new terminal**, and forgetting it is what makes `uvicorn` and
`alembic` look like they are not installed when they are. `start.sh` handles it
for you.

### 3. Create your settings files

```bash
cd /workspaces/Personal-Finance-Tool
cp backend/.env.example backend/.env
cp frontend/.env.example frontend/.env.local
```

These two new files are where your keys go. They are **git-ignored**, which
means they never leave your Codespace and never reach GitHub. That is
deliberate and you should not change it.

### 4. Get your keys

Two free accounts. The keys go in **different files** — this is the single
most common mistake:

| Key | Where to get it | Which file |
|---|---|---|
| Clerk publishable key (`pk_test_…`) | Clerk dashboard → API keys | `frontend/.env.local` |
| Clerk secret key (`sk_test_…`) | Clerk dashboard → API keys | `frontend/.env.local` |
| Clerk issuer URL | Clerk dashboard → API keys → "Frontend API URL" | `backend/.env` |
| Plaid client ID | Plaid dashboard → Developers → Keys | `backend/.env` |
| Plaid **sandbox** secret | Plaid dashboard → Developers → Keys | `backend/.env` |

To edit a file: find it in the file explorer on the left, click it, type, then
save with **Ctrl+S**. Files starting with a dot may be hidden — if you cannot
see `.env`, open it from the terminal instead with
`code backend/.env`.

**A note on secret keys.** The publishable key (`pk_`) is designed to be
public and appears in your web page's source; there is nothing to protect. The
secret key (`sk_`) is a password. Never paste it into a chat, an issue, or a
commit. If you do, rotate it in the Clerk dashboard — which invalidates the old
one — and carry on. A leaked `sk_test_` key only reaches a development
instance with no real users, so it is a small problem; the habit matters
because Plaid production keys reach real bank data, where it is not.

### 5. Generate an encryption key

This key encrypts your Plaid access tokens in the database. Run:

```bash
python3 -c "import base64, os; print(base64.urlsafe_b64encode(os.urandom(32)).decode())"
```

Copy the output into `backend/.env`, keeping the quotes and brackets:

```
ENCRYPTION_KEYS=["paste-the-key-here"]
```

**Back this up somewhere outside the repository** — a password manager is
ideal. It is the one value in this project with no recovery path: lose it and
every stored bank connection becomes permanently unreadable, and you will have
to link every account again.

### 6. Tell the backend where the frontend lives

Your Codespace has its own web address, and the backend refuses requests from
addresses it does not recognise. That is a real security control, not
red tape — it is what stops another website from making requests as you — so it
has to be told about yours rather than switched off.

Find your Codespace's name:

```bash
echo "$CODESPACE_NAME"
```

Then in `backend/.env`, set these two lines, substituting your name:

```
FRONTEND_ORIGIN=http://localhost:3000
CLERK_AUTHORIZED_PARTIES=["http://localhost:3000","https://YOUR-CODESPACE-NAME-3000.app.github.dev"]
```

Both entries are needed because either address may be the one your browser
uses, depending on how you open the app. See "Why two addresses" below.

**Save the file, and start the backend afterwards.** A server reads its
settings once, at startup. Editing `.env` while it runs changes nothing, and
the resulting failure looks like a new bug rather than a stale setting.

### 7. Make the start script runnable

```bash
chmod +x /workspaces/Personal-Finance-Tool/start.sh
```

---

## Every time after that

```bash
cd /workspaces/Personal-Finance-Tool
./start.sh
```

That is the whole thing. It starts the database, applies any new migrations,
and runs both servers in one terminal with their output labelled `[api]` and
`[web]`.

**Stop it with Ctrl+C.** Not **Ctrl+Z** — Ctrl+Z *freezes* a program instead of
stopping it, and a frozen program keeps holding its port. The next start then
finds port 3000 occupied, quietly uses 3001 instead, and the address you have
open in your browser shows nothing at all. (If it happens anyway,
`./start.sh` clears the ports for you on the next run.)

Then open the **Ports** tab, find port 3000, and click the globe icon to open
it in your browser.

---

## Linking a bank

Start in Plaid's **sandbox**: fake banks, fake money, free, and no way to touch
a real account.

1. Sign up / sign in through Clerk.
2. On the dashboard, click **Connect a bank**.
3. If Plaid asks for a phone number, it will reject your real one — sandbox is
   not connected to the phone network. Use `+1 415 555 0011`, and `123456` for
   the code. (Or turn off the returning-user flow in Plaid dashboard → Link →
   Link Customization.)
4. Pick any bank. They are all fictional.
5. Username `user_good`, password `pass_good`. Any six digits for a code.
6. Back on the dashboard, click **Sync now** on the connection.

Linking a bank creates the **accounts**. **Sync now** fetches the
**transactions** — two separate steps, which is why a freshly linked bank looks
empty until you sync.

### Moving to real accounts

Sandbox uses `PLAID_ENV=sandbox` and your sandbox secret. Real banks need
**Plaid Production**, which is a separate application in the Plaid dashboard
that a human reviews — expect days, not minutes, and they will ask what your
app does and how you handle data.

Once approved you get a *different* secret — same dashboard page as the sandbox
one, different column. `PLAID_CLIENT_ID` is unchanged. In `backend/.env`:

```
PLAID_ENV=production
PLAID_SECRET=your-production-secret
```

**Change only those two.** `ENVIRONMENT` and `PLAID_ENV` are separate settings
and it is tempting to set both:

| Setting | Means | In a Codespace |
|---|---|---|
| `PLAID_ENV` | Which Plaid to talk to | `production` once approved |
| `ENVIRONMENT` | How this deployment is hosted | stays `local` |

Setting `ENVIRONMENT=production` makes the app refuse to start without an
HTTPS frontend origin, an `ALLOWED_HOSTS` list, an HTTPS webhook URL and
`DEBUG=false` — none of which a Codespace has. That check is doing its job; it
exists so a real deployment cannot go out misconfigured. It is simply not the
switch that selects real banks.

Restart and link again. Sandbox connections do not carry over: the old access
token is not valid against production, so disconnect it and link fresh.

Nothing changes in the frontend — Plaid Link takes its environment from the
link token the backend mints.

Two things to know before real financial data lands in a Codespace:

- **A deleted Codespace takes the database and the encryption key with it.**
  Relinking and resyncing rebuilds the transaction history, but manual
  categorizations and corrections are not recoverable. This is the point at
  which deploying somewhere permanent starts to be worth it.
- **Check the Ports tab.** Both ports should show visibility **Private**.
  That is the default, but the data behind them is no longer fictional, so it
  is worth confirming rather than assuming.

Also confirm `DEBUG` is still `true` only because this is a local environment —
it keeps the interactive API docs and detailed errors switched on, which is
fine behind a private forwarded port and is exactly what `ENVIRONMENT=production`
turns off when you deploy for real.

Before you point this at real financial data, read
[`milestone-08-hardening.md`](milestone-08-hardening.md), particularly the part
about connecting as a database role that owns no tables. PostgreSQL exempts a
table's owner from that table's security policies, so deploying as the
migration role leaves row-level security switched on and doing nothing.

---

## Why two addresses

Worth understanding, because it produces the project's most confusing error.

Your app is reachable two ways, and both are normal:

- `https://your-codespace-3000.app.github.dev` — the public forwarded URL.
- `http://localhost:3000` — when the editor forwards the port to your own
  machine, so your browser genuinely believes it is talking to localhost.

Next.js protects form submissions by checking that the address your browser
*claims* to be visiting matches the one the server *thinks* it is serving.
Behind Codespaces' forwarding, those two differ, and the request is rejected
with:

> Invalid Server Actions request.

Which mentions neither addresses nor Codespaces, and sends you looking in the
wrong file. `frontend/next.config.ts` fixes it by trusting both, derived from
the environment so it works in anyone's Codespace. Off Codespaces the list is
empty and the strict default applies, so this is not a weakness in a deployed
app.

The practical advice: **pick one address and stay on it.** Switching mid-session
changes your browser's origin and can resurrect the error.

---

## When something breaks

**Look at the terminal, not the browser.** The browser shows a sanitised
message; the terminal names the actual cause. Almost every problem in this
project is diagnosed in the last ten lines of `[api]` or `[web]` output.

| Symptom | Cause |
|---|---|
| `ENOENT ... Could not read package.json` | Wrong folder. `cd frontend`. |
| `Port 3000 is in use, using 3001` | An old server is still holding it — usually one you Ctrl+Z'd. Restart with `./start.sh`. |
| `HTTP ERROR 504` on the github.dev URL | Nothing is listening. The frontend is not running. |
| **Connect a bank** greyed out | Requesting a link token failed. Check `[api]` — usually missing Plaid keys or an address not in `CLERK_AUTHORIZED_PARTIES`. |
| `Invalid Server Actions request` | Address mismatch. See "Why two addresses". |
| `uvicorn: command not found` | The virtualenv is not active. `source backend/.venv/bin/activate`, or just use `./start.sh`. |
| Clerk warns about development keys | Expected with `pk_test_` keys. Not a problem. |
| Plaid warns `link-initialize.js embedded more than once` | Harmless, from a retried load. |

A trick for checking what is actually running:

```bash
curl -s -o /dev/null -w '%{http_code}\n' http://localhost:8000/health
curl -s -o /dev/null -w '%{http_code}\n' http://localhost:3000
```

`200` means running. `000` means nothing is listening. Paste these **one at a
time** — pasting two lines together sometimes loses the newline and joins them
into one nonsense command, which then reports a misleading result.

The API's health endpoints are `/health` and `/health/db`. There is no
`/api/health`, so a 404 there is expected and tells you nothing.

## Running the tests

```bash
cd backend
source .venv/bin/activate
pytest
```

429 tests, against a real PostgreSQL database, applying the real migrations.
The `source` line is required: two of the tests run `alembic` as a command, and
without the virtualenv on your PATH they fail with `FileNotFoundError` even
though everything is installed correctly.
