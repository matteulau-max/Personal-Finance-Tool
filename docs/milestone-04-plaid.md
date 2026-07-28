# Milestone 4 — Plaid Integration & the Sync Engine

## 1. Goal

Connect real bank accounts and build the sync engine that keeps them current
— correctly, idempotently, and without ever destroying a user's corrections.

## 2. Why this matters

This is where Milestone 2 stops being theory. Those unique constraints and
three-layer columns now face live data arriving repeatedly, out of order, and
sometimes twice.

A sync engine is one of those components where "it works" and "it is correct"
are very different claims. It will appear to work with a handful of
transactions and then quietly corrupt a year of history the first time a
webhook and a scheduled job overlap. So the bulk of this milestone is spent on
the failure modes rather than the happy path.

---

## 3. Set up Plaid

**Step 3a.** Create a free account at <https://dashboard.plaid.com/signup>.
No approval, no credit card — sandbox access is immediate.

**Step 3b.** Go to **Developers → Keys** and copy:

| Value | Looks like |
|---|---|
| `client_id` | `65f...` |
| Sandbox `secret` | `a1b...` |

**Step 3c.** Generate an encryption key:

```bash
cd backend && source .venv/bin/activate
python -c "from cryptography.fernet import Fernet; print(Fernet.generate_key().decode())"
```

**Step 3d.** Fill in `backend/.env`:

```bash
PLAID_CLIENT_ID=your_client_id
PLAID_SECRET=your_sandbox_secret
PLAID_ENV=sandbox
ENCRYPTION_KEYS=["the-key-you-just-generated"]
```

> **Back that key up somewhere other than this repository.** Lose it and every
> stored access token becomes unreadable — every bank connection would have to
> be re-linked by hand. It is the one value in this project with no recovery
> path.

---

## 4. Run it

```bash
git pull origin claude/personal-finance-dashboard-qqb2rg

cd backend && source .venv/bin/activate
pip install -r requirements.txt        # plaid-python is new
uvicorn app.main:app --reload --port 8000

cd ../frontend
npm install                            # react-plaid-link is new
npm run dev
```

Open <http://localhost:3000/dashboard> and click **Connect a bank**.

**Sandbox credentials** (these work for every institution in the picker):

```
username: user_good
password: pass_good
```

Pick any bank, sign in, and you should land back on the dashboard with
accounts, balances, and a net worth figure.

> No schema changes this milestone — Milestone 2's design was sufficient. That
> is the payoff for spending the time on it: the hardest part of the sync
> engine was already solved before the code was written.

---

## 5. The token exchange

```
browser                 our backend                Plaid
  |                          |                       |
  |-- POST /link-token ----->|                       |
  |                          |-- link_token/create ->|
  |<------ link_token -------|                       |
  |                                                  |
  |----------- user picks a bank, logs in ---------->|
  |<---------------- public_token -------------------|
  |                          |                       |
  |-- POST /exchange ------->|                       |
  |   {public_token}         |-- exchange ---------->|
  |                          |<---- ACCESS TOKEN ----|
  |                          |   (encrypt, store)    |
  |<------- 201 Created -----|                       |
```

Three properties worth naming explicitly:

1. **Bank credentials never touch our servers.** They are typed into Plaid's
   iframe. We never see them, cannot log them, and cannot leak them. That is
   the main reason to use Plaid at all.
2. **The browser only ever holds a `public_token`** — single-use, expires in
   minutes, worthless without our Plaid secret.
3. **The `access_token` never leaves the server.** It grants ongoing read
   access to real accounts. It is encrypted before it reaches the database and
   appears in no API response. There is a test asserting exactly that.

---

## 6. The four rules the sync engine is built on

All in `backend/app/services/sync.py`, which is the file to read closely.

### 6.1 The cursor advances only when its data is committed

`/transactions/sync` is a cursor feed: send the last cursor, get back what
changed. The engine writes the page **and** the new cursor in one transaction:

```python
item.transactions_cursor = cursor
self.db.commit()          # page data + cursor, atomically
```

If the process dies mid-sync, PostgreSQL rolls back both together and the next
run re-fetches that page. Saving the cursor separately — or first — means a
crash silently skips transactions **permanently**. Plaid will never send them
again, nothing reports an error, and you find out when someone reconciles a
statement by hand months later.

Two tests pin this down: one proves partial progress survives a mid-sync
failure, the other proves the cursor does *not* move when the first page fails.

### 6.2 Writes are upserts, so replaying is harmless

```sql
INSERT ... ON CONFLICT (account_id, plaid_transaction_id) DO UPDATE SET ...
```

Re-running a sync, retrying after a timeout, or a webhook racing the nightly
job cannot create a duplicate — the constraint from Milestone 2 arbitrates.
This is what **idempotent** means: doing it twice equals doing it once.

### 6.3 An upsert only ever touches `raw_*` columns

The `SET` clause names them explicitly. No `user_*` column appears, so a sync
*physically cannot* overwrite a human's correction — not by oversight, not by
a future refactor, not when a bank restates a record.

`test_a_sync_never_overwrites_a_user_correction` is the most important test in
the milestone. If it ever fails, every manual correction a user has made is one
sync away from silent erasure.

### 6.4 The duplicate that isn't a duplicate

This one is worth understanding properly, because no constraint catches it.

When a pending charge posts, **Plaid does not update it**. It issues a
brand-new transaction with a *different id* and sets `pending_transaction_id`
pointing at the old one. For a moment you legitimately hold two rows for one
coffee — and both unique constraints are satisfied, because the ids genuinely
differ.

Do nothing and every card purchase appears twice; every total is inflated.

`_reconcile_pending()` retires the pending row when its posted replacement
arrives. It handles both orderings and both delivery patterns, and running it
twice is harmless.

---

## 7. Webhook verification

The webhook endpoint is the only public, unauthenticated route in the
application. Plaid cannot log in, so a signature is the entire security
boundary. Without it, anyone who finds the URL can force unlimited syncs and
exhaust your Plaid quota.

Five checks, in `services/plaid_webhooks.py`:

| Check | Skipping it means |
|---|---|
| Algorithm is ES256 | The `alg: none` forgery works — same trap as Milestone 3 |
| Signature verifies against Plaid's key | Anyone can send anything |
| `iat` within 5 minutes | A captured webhook replays forever |
| SHA-256 of the **raw body** matches `request_body_sha256` | A valid signature can be attached to a body of the attacker's choosing |
| Constant-time comparison (`hmac.compare_digest`) | Timing differences leak how much of the hash was correct |

One implementation detail that bites people: the hash must be computed over
the **raw bytes**. Parsing JSON and re-serializing it changes whitespace and
key order, the hash never matches, and every webhook is rejected — a bug that
looks like a signing problem and is actually plumbing. There is a test for it.

Local development needs no webhook: Plaid cannot reach `localhost`, and
syncing works fine on demand. Leave `PLAID_WEBHOOK_URL` empty until deployment.

Source: <https://plaid.com/docs/api/webhooks/webhook-verification/>

---

## 8. Encryption at rest

`backend/app/core/crypto.py`. Fernet, via `MultiFernet` so keys can be rotated:

```
1. Prepend a new key:  ENCRYPTION_KEYS=["new", "old"]   # new writes use "new"
2. Re-encrypt existing rows with rotate()
3. Drop the old key:   ENCRYPTION_KEYS=["new"]
```

Designing for rotation on day one costs nothing. Retrofitting it *after* a key
leaks, under pressure, is genuinely painful — and by then you cannot tell which
rows are safe. `test_key_rotation_keeps_old_data_readable` walks the whole
sequence, including the failure you get if you drop the old key too early.

Worth being precise about what this protects: a leaked database backup, a
snapshot with wrong permissions, a stolen laptop with a dump on it. It does
**not** protect against an attacker who has both the database and the running
server's environment. That is what least privilege and secrets management are
for, in Milestone 8.

---

## 9. Testing

```bash
cd backend && source .venv/bin/activate && pytest
```

Expected: **150 passed**.

### Why the tests use a fake Plaid

Plaid's sandbox is genuinely useful for a manual check — §4 has you do exactly
that. It is a poor foundation for an automated suite:

- **You cannot ask it for the scenarios that matter.** "A pending charge posts
  with a different amount." "The same page arrives twice." "The connection
  expires halfway through page 3." These are where sync engines break.
- It needs credentials, so CI cannot run without secrets.
- It is slow and occasionally flaky, which trains you to ignore red builds.

So `tests/fake_plaid.py` implements the same `PlaidGateway` protocol and is
*scripted*: each test states exactly what Plaid returns, including failures.
Every line of the real engine runs.

**The two kinds of verification are complements, not substitutes.** The fake
proves our logic is correct given an API. The sandbox proves our understanding
of that API is correct. You need both, and neither replaces the other.

### Try it yourself

After connecting a sandbox bank, click **Sync now** twice and watch the sync
history. The second run adds nothing — that is idempotency, visible.

Then prove corrections survive:

```sql
-- Pick a transaction and correct it
UPDATE transactions SET user_amount = 1.00, user_notes = 'my correction'
WHERE id = (SELECT id FROM transactions LIMIT 1);
```

Sync again, then check: `raw_amount` is whatever the bank says, `user_amount`
is still 1.00, and `user_notes` is intact.

---

## 10. Troubleshooting

| Symptom | Cause | Fix |
|---|---|---|
| 503 from `/link-token` | Plaid keys not set | Fill in `PLAID_CLIENT_ID` / `PLAID_SECRET`, restart |
| `ENCRYPTION_KEYS is not configured` | Missing key | Generate one (§3c) and add it to `.env` |
| `Could not decrypt stored secret` | Key changed or lost | Restore the old key, or re-link the bank |
| Link dialog opens then immediately closes | Wrong `PLAID_ENV` for the keys | Sandbox keys need `PLAID_ENV=sandbox` |
| `INVALID_PUBLIC_TOKEN` | Token expired (minutes) or already used | Restart the Link flow |
| `ITEM_LOGIN_REQUIRED` | Sandbox item deliberately expired, or real credentials changed | Re-link the connection |
| Sync succeeds, no transactions | Sandbox accounts can be sparse | Check `/api/plaid/items/{id}/syncs` for the counts |
| Webhooks never arrive locally | Plaid cannot reach localhost | Expected — use ngrok, or ignore until deployment |
| Every webhook 401s | Body re-serialized before hashing | Hash the raw bytes (see §7) |

---

## 11. Common mistakes

- **Passing an empty-string cursor on the first sync.** Plaid rejects it;
  "start from the beginning" means omitting the field entirely.
- **Saving the cursor before the data.** Silently loses transactions forever.
- **Letting a sync write `user_*` columns.** Erases corrections.
- **Ignoring `pending_transaction_id`.** Every card purchase appears twice.
- **Hard-deleting removed transactions.** No recovery from a sync bug.
- **Returning 500 when Plaid fails.** It is not your bug — 502/503 says so.
- **Trusting a webhook without verifying it.** It is a public URL.
- **Storing the access token in plaintext**, or returning it in any response.

---

## 12. Two problems found while building this

**1. Audit values were formatted inconsistently.** The "before" value for an
amount was read back from a `Numeric(18, 4)` column as `"4.5000"`, while the
"after" came straight from Plaid as `"5.25"`. Same field, same currency, two
spellings. Not merely untidy: it makes before/after diffs impossible to compare
programmatically, so any future "what changed?" report would see spurious
differences on every unchanged field. Fixed by quantizing money at the single
point where audit values are written.

**2. `-> None` on a 204 endpoint crashed the app at import.** This project uses
`from __future__ import annotations`, so the annotation resolves to the *class*
`NoneType` — which is truthy. FastAPI read it as a real response model and
asserted that a 204 must not have a body. `response_model=None` states the
intent unambiguously.

Both were caught by running the code rather than reading it. The general
lesson, again: **assert on what actually happened, not on what should have.**

---

## 13. Checklist before Milestone 5

- [ ] Plaid sandbox keys in `backend/.env`
- [ ] `ENCRYPTION_KEYS` generated and backed up outside the repo
- [ ] `pip install -r requirements.txt` and `npm install` completed
- [ ] `pytest` prints **150 passed**
- [ ] You connected a sandbox bank with `user_good` / `pass_good`
- [ ] Accounts and balances appear on `/dashboard`
- [ ] **Sync now** twice — the second run adds 0 transactions
- [ ] `select count(*) from plaid_items where access_token_encrypted like '%access-%';`
      returns **0** (the token is ciphertext)
- [ ] `select count(*) from account_balances;` grows by one per account per sync
- [ ] You corrected a transaction, re-synced, and the correction survived
- [ ] Disconnect works, and your transactions are still there afterwards

---

## What's next — Milestone 5: Categorization, Merchants, Rules & Tags

Your transactions are currently raw bank descriptors: `WHOLEFDS MKT #10259`.
Next we make them meaningful:

- Merchant normalization — `WHOLEFDS MKT #10259`, `Whole Foods #125`, and
  `WHOLE FOODS MARKET` all becoming one merchant
- Automatic categorization, and mapping Plaid's categories onto our tree
- The rules engine: "if the description contains X, categorize as Y and tag it Z"
- Tags, and bulk editing
- Learning from corrections, so fixing something once fixes it forever
