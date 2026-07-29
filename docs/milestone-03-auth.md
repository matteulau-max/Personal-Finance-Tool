# Milestone 3 — Authentication & Authorization

## 1. Goal

Make the application multi-user and safe: real sign-in via Clerk, verified
tokens on every API request, and a hard guarantee that one user can never see
another's financial data.

## 2. Why this matters

Every table we built in Milestone 2 has a `user_id` column that nothing fills
in. Until that is wired up, there is only one thing the application can be: a
single-user toy.

More importantly, this is where the most dangerous bug class in the entire
project lives. Authentication bugs are *loud* — nobody can log in, you find
out in thirty seconds. Authorization bugs are *silent*: the app works, the
tests pass, the pages render, and everyone can see everyone's bank balances.

The two words, precisely:

- **Authentication** — *who are you?* (a verified token)
- **Authorization** — *what are you allowed to see?* (a scoped query)

---

## 3. Set up Clerk

Clerk handles passwords, email verification, MFA, and social login. Those are
genuinely hard to build correctly, and getting them wrong leaks credentials.
Delegating them is the right call.

**Step 3a.** Create a free account at <https://dashboard.clerk.com>.

**Step 3b.** Create an application. Enable **Email** and, optionally, Google.

**Step 3c.** Open **Configure → API keys** and collect three values:

| Value | Looks like | Goes where |
|---|---|---|
| Publishable key | `pk_test_...` | `frontend/.env.local` |
| Secret key | `sk_test_...` | `frontend/.env.local` |
| Frontend API URL | `https://xxx-yyy-42.clerk.accounts.dev` | `backend/.env` as `CLERK_ISSUER` |

> The Frontend API URL is sometimes shown under "Show API URLs" or in the
> JWKS URL — it is the domain part of `https://…/.well-known/jwks.json`.

**Step 3d.** Fill in your environment files.

`backend/.env`:

```bash
CLERK_ISSUER=https://your-instance.clerk.accounts.dev
CLERK_AUTHORIZED_PARTIES=["http://localhost:3000"]
```

`frontend/.env.local`:

```bash
NEXT_PUBLIC_CLERK_PUBLISHABLE_KEY=pk_test_...
CLERK_SECRET_KEY=sk_test_...
```

**The one rule that matters here:** `NEXT_PUBLIC_` means *published to the
browser*. The publishable key is designed for that. The secret key is not —
prefixing it with `NEXT_PUBLIC_` would bundle it into JavaScript served to
every visitor and hand over full control of your Clerk instance. Check that
prefix every time you add a variable.

---

## 4. Run it

```bash
git pull origin claude/personal-finance-dashboard-qqb2rg

# Backend
cd backend && source .venv/bin/activate
pip install -r requirements.txt      # PyJWT[crypto] is new
alembic upgrade head                 # adds users.last_seen_at
uvicorn app.main:app --reload --port 8000

# Frontend (second terminal)
cd frontend
npm install                          # @clerk/nextjs is new
npm run dev
```

Open <http://localhost:3000>, click **Sign in**, create an account, and you
land on `/dashboard` showing your email and an empty accounts list.

The application runs fine *without* Clerk keys — you get a "not configured"
banner instead of sign-in. That is deliberate: a project you cannot start
until you have finished signing up for a third-party service is a project
nobody can clone, and CI could never run it.

---

## 5. How a JWT actually works

A JSON Web Token is three base64 chunks joined by dots:

```
eyJhbGciOiJSUzI1NiJ9 . eyJzdWIiOiJ1c2VyXzEyMyJ9 . MEUCIQDx...
└─── header ───┘        └─── payload ───┘         └ signature ┘
```

**The payload is not encrypted.** Paste any token into <https://jwt.io> and
you will read the claims in plain text. So a token must never contain
anything secret.

What a JWT provides is *integrity*, not secrecy. Clerk signs it with a private
key only Clerk holds; we verify with the matching public key, fetched from
`https://your-instance.clerk.accounts.dev/.well-known/jwks.json`. Change one
byte of the payload and the signature stops matching.

That is the whole value: we can trust "this request is from user X" without a
database lookup or an API call to Clerk on every request.

### The five checks, and why each one matters

All five live in `backend/app/core/security.py`. Skipping any one turns
authentication into decoration:

| Check | Without it |
|---|---|
| **Signature** against Clerk's public key | Anyone mints any token they like |
| **Algorithm** pinned to RS256 | Attacker sets `"alg": "none"` and supplies no signature — a real, widespread vulnerability in early JWT libraries |
| **Expiry** (`exp`, `nbf`) | A leaked token works forever |
| **Issuer** (`iss`) | A token from *any* Clerk instance is accepted, including one the attacker created |
| **Authorized party** (`azp`) | A token minted for a different app on the same instance can be replayed here |

`tests/test_auth.py` has one test per row. That structure is deliberate: a
single "bad token is rejected" test still passes when four of the five checks
have been silently disabled.

---

## 6. The authorization design

### 6.1 Authentication is a type, not a convention

```python
@router.get("/me")
def read_current_user(current_user: CurrentUser):
    ...
```

`CurrentUser` is an annotated type that carries a FastAPI dependency. Writing
it on an endpoint *is* what makes that endpoint protected — there is no
separate step to remember. And you cannot read `current_user` in an endpoint
that did not authenticate, because the parameter would not exist.

### 6.2 The identity comes from the token, never from the client

There is no `GET /api/users/{id}`. There is only `GET /api/me`.

An endpoint that takes an id invites the classic broken-access-control bug:
the client passes an id, someone forgets to verify it matches the token, and
now anyone reads anyone's profile by changing a number. If the client
*cannot* say who it is asking about, that bug cannot exist.

### 6.3 Queries are scoped by construction

```python
# The bug, and it looks completely normal in review:
db.execute(select(Account))

# What we do instead:
db.execute(scoped_select(Account, current_user))
```

`scoped_select()` (in `app/db/scoping.py`) cannot be called without a user.
No default, no optional parameter, no "None means everyone". Ask for
user-owned data and you must say whose.

It also refuses models it does not recognize, rather than falling back to an
unfiltered query. Fail closed: a new table added without deciding how it
should be scoped raises an error instead of leaking.

### 6.4 "Not yours" and "not there" look identical

Fetching another user's account returns **404**, not 403.

A 403 confirms the id is real. An attacker can then enumerate ids and learn
how many accounts other people have and when they were created. Never let an
authorization failure be distinguishable from a missing record.

### 6.5 Response schemas are an allowlist

Endpoints return Pydantic schemas, never SQLAlchemy models. `UserResponse`
lists exactly the fields a client may see, so `clerk_user_id`, `is_active`,
and `last_seen_at` stay internal.

The important property is what happens *later*: if endpoints returned models
directly, every new column would be published to the internet by default.
With an allowlist, the safe behaviour is the default one.

---

## 7. The test that fails when someone forgets

`tests/test_authorization.py::test_every_endpoint_requires_authentication`

It walks FastAPI's route table and asserts every path either requires
`get_current_user` or appears in an explicit `PUBLIC_PATHS` allowlist. It
inspects routes rather than calling them, so it covers endpoints that do not
exist yet — write an unprotected one in Milestone 6 and this fails
immediately, naming the path.

I verified it works by adding a deliberately leaky endpoint:

```
AssertionError: These endpoints do not require authentication:
    ['GET'] /api/accounts/leaky/everything
```

**A guard test you have never seen fail is not a guard test.** When you write
one, break the thing on purpose once and watch it catch you.

---

## 8. Testing

```bash
cd backend && source .venv/bin/activate && pytest
```

Expected: **83 passed**.

### How the auth tests avoid calling Clerk

They generate an RSA key pair inside the test process, sign tokens with the
private half, and stub *only* the JWKS lookup to return the public half.
Everything else — signature verification, expiry, issuer, algorithm pinning,
`azp` — runs for real.

That is the shape of good integration testing: **substitute the boundary,
exercise everything inside it.** A suite that called Clerk for real would fail
whenever Clerk was slow, could not run offline or in CI without secrets, and
would train you to ignore red builds.

### Try it yourself

With the backend running:

```bash
curl -i http://localhost:8000/api/me
# HTTP/1.1 401 Unauthorized
# www-authenticate: Bearer

curl -i -H "Authorization: Bearer made-up-token" http://localhost:8000/api/me
# HTTP/1.1 401 Unauthorized

curl -i http://localhost:8000/health
# HTTP/1.1 200 OK   <- health stays public, load balancers cannot log in
```

Then in the browser, sign in and visit <http://localhost:3000/dashboard>.
Sign out and visit it again — you are redirected to sign-in.

---

## 9. Troubleshooting

| Symptom | Cause | Fix |
|---|---|---|
| Everything 401s with a real token | `CLERK_ISSUER` doesn't match your instance | Compare with the Frontend API URL in the Clerk dashboard, no trailing slash |
| `authentication is not configured` in logs | `CLERK_ISSUER` empty | Set it in `backend/.env` and restart |
| Sign-in button does nothing | Publishable key missing | Set `NEXT_PUBLIC_CLERK_PUBLISHABLE_KEY`, restart `npm run dev` |
| Auth silently never runs | File named `middleware.ts` | Next.js 16 renamed it to **`proxy.ts`** — see §10 |
| `SignedIn is not exported` | Clerk v7 removed it | Use `<Show when="signed-in">` |
| `could not resolve signing key` | Backend cannot reach Clerk's JWKS | Check outbound HTTPS from the backend host |
| 401 only after a while | Clock skew | `JWT_LEEWAY_SECONDS` already allows 30s; check the server clock |
| App won't start in production | Fail-closed config check | Set `CLERK_ISSUER`, `CLERK_AUTHORIZED_PARTIES`, and `DEBUG=false` |

---

## 10. Two Next.js 16 gotchas

Both will waste an afternoon if you hit them cold, and every tutorial online
still has the old versions:

1. **`middleware.ts` is now `proxy.ts`.** Next.js 16 renamed it. A file named
   `middleware.ts` is simply ignored — producing the baffling symptom of
   authentication not running at all, with no error.
2. **`<SignedIn>` / `<SignedOut>` no longer exist** in Clerk v7. The
   replacement is `<Show when="signed-in">`. Importing the old names is a
   build error.

Also worth internalizing: **proxy is not a security boundary.** It stops a
signed-out visitor seeing a broken empty dashboard. It does nothing about
someone calling the API directly with curl. The real enforcement is the token
check on the FastAPI side, which runs no matter who is asking. Next.js says
this explicitly in its own docs.

---

## 11. Common mistakes

- Putting a secret behind a `NEXT_PUBLIC_` name.
- Trusting a user id sent by the client instead of the one in the token.
- Returning 403 for "not yours" — it confirms the record exists.
- Returning SQLAlchemy models straight from endpoints.
- Telling the client *why* a token failed. Log it; don't return it.
- Treating the frontend redirect as the security control.
- Assuming `flush()` saves data. It does not — see below.

---

## 12. A bug found while building this

Just-in-time provisioning called `db.flush()` but never `db.commit()`.

The request succeeded. `/api/me` returned a perfectly good user object with an
id and email. Every test passed. And the `users` table was **empty** — the
session closed without committing, so the INSERT was rolled back, and the user
was silently re-created with a *different id* on every single request. Any
account or transaction created against the previous id would have been
orphaned.

I only caught it by querying PostgreSQL directly after an end-to-end run.

The reason no test caught it is worth understanding, because it applies to any
test suite built this way: our tests share one transaction with the request
and roll it back at the end. Inside that transaction, flushed and committed
data are indistinguishable. The blind spot is structural.

The fix was one line. The lasting fix is
`test_provisioned_user_is_actually_persisted`, which opens a **separate**
database connection after the request and asserts the row is really there. I
verified it works by removing the commit again and watching it fail:

```
AssertionError: user was returned by the API but never committed
```

**The lesson: a fast, isolated test suite buys speed by hiding the boundary
between "saved" and "appears saved". Keep at least one test that looks from
outside the transaction.**

---

## 13. Checklist before Milestone 4

- [ ] Clerk application created; three keys copied into the env files
- [ ] `backend/.env` has `CLERK_ISSUER` and `CLERK_AUTHORIZED_PARTIES`
- [ ] `frontend/.env.local` has both Clerk keys
- [ ] `alembic upgrade head` applied `7c3fd28a1580`
- [ ] `pytest` prints **83 passed**
- [ ] `curl http://localhost:8000/api/me` returns **401**
- [ ] `curl http://localhost:8000/health` returns **200**
- [ ] You signed up in the browser and reached `/dashboard`
- [ ] `/dashboard` while signed out redirects you to sign-in
- [ ] `select clerk_user_id, email from users;` shows your row — proving
      provisioning actually persisted
- [ ] `git status` shows no `.env` or `.env.local` staged

---

## What's next — Milestone 4: Plaid

The big one. Connecting real bank accounts, and building the sync engine that
uses the deduplication guarantees from Milestone 2 for real:

- Plaid Link, and the token exchange that never lets an access token reach the
  browser
- Encrypting access tokens at rest
- `/transactions/sync` and cursor-based incremental sync
- Reconciling pending charges into posted ones without duplicating them
- Webhooks, and how to verify one actually came from Plaid
- Making the whole thing idempotent, so a retried sync is always safe
