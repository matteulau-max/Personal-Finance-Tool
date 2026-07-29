# Milestone 8 — Deployment & hardening

Every previous milestone added something a user can see. This one adds things
they can only notice by their absence: a database that refuses to hand over
another person's rows, a webhook that answers in milliseconds, an audit log
that will still be manageable in 2031, and an endpoint that costs money and
now has a budget.

Four changes, in descending order of how much they matter:

1. **PostgreSQL Row-Level Security** — the database enforces user isolation,
   not just the application.
2. **Rate limiting** — with `/api/insights` the reason it exists.
3. **A durable webhook queue** — Plaid's webhook no longer waits for a sync.
4. **Audit log partitioning** — done now, while the table is small.

Plus the ordinary deployment work: a container image, security headers,
logging that actually reaches a log, and a configuration that refuses to start
if production is missing a control.

---

## 1. Row-Level Security

### The bug this is really about

`app/db/scoping.py` opens by describing the failure it exists to prevent:

```python
# WRONG -- returns every user's accounts to whoever asks
db.execute(select(Account))

# Right
db.execute(select(Account).where(Account.user_id == current_user.id))
```

`scoped_select()` makes the second the convenient one. But it shares a
weakness with every control of its kind: **it only protects the queries that
use it**. Someone writes `select(Transaction)` in a hurry, the diff reads
fine, and the test suite passes because the test has one user in it — with a
single user, a missing filter returns exactly the right answer.

That file said, back in Milestone 3, that this was "the belt" and that
Milestone 8 would add the braces. This is the braces.

### What RLS does

A policy attached to a table makes PostgreSQL rewrite *every* query against
it to include the ownership predicate. Ours, an ORM's, a `psql` session's, a
SQL injection payload's. `SELECT * FROM transactions` stops being a data
breach and becomes a query that returns your own rows.

The policy for an owned table:

```sql
CREATE POLICY transactions_owner ON transactions
    USING (user_id = app_current_user_id())
    WITH CHECK (user_id = app_current_user_id());
```

`USING` filters what you can see and change. `WITH CHECK` is the half people
forget: without it you cannot *read* another user's rows but you can happily
*create* one — planting a transaction in somebody's financial history, which
is arguably worse than reading it. Both halves have a test.

### How the user identity gets in

`app_current_user_id()` reads a session variable that each request sets once,
immediately after authentication:

```python
def get_current_user(db, claims) -> User:
    user = get_or_create_user(db, claims)
    rls.activate(db, user.id)     # SET ROLE + SET app.current_user_id
    ...
```

This line is in `deps.py` rather than in any endpoint, and the placement is
the design. Provisioning has to run first — looking a user up by their Clerk
id cannot be filtered to a user we have not identified yet — so RLS switches
on the moment we know who is asking, and everything afterwards is filtered
whether or not the code that wrote it remembered to scope it.

It also inherits the property that made authentication reliable: an endpoint
declaring `CurrentUser` gets protection; an endpoint that does not declare it
never receives a user to leak data with.

### Why `SET ROLE` is part of it

PostgreSQL exempts two kinds of role from policies: superusers (and anything
with `BYPASSRLS`), and **the table's owner**. Migrations run as the owner —
they have to, they create the tables — so attaching policies protects nothing
if the application connects as that same role.

So each request switches: `SET ROLE finance_app` makes `current_user` a plain
role that owns nothing and bypasses nothing.

Be precise about what that buys. `SET ROLE` is reversible by whoever issued
it, so this is **not** a defence against an attacker who can already run
arbitrary SQL as our connection role — at that point they can `RESET ROLE`.
It is a defence against *our own code being wrong*, which is the failure that
actually happens. Closing the remaining gap is a deployment step, below.

### `SET`, not `SET LOCAL`

`SET LOCAL` scopes a setting to the current transaction. That sounds exactly
right and is a trap: endpoints call `db.commit()`, and the commit ends the
transaction. Every statement after the first commit would run with no user
context and — because we fail closed — return nothing. The symptom is a page
that works until you save something.

So the setting is session-scoped, which means it outlives the request and
would ride a pooled connection to whoever borrowed it next. That is a
cross-user leak with extra steps, and it is closed twice: `get_db` clears it
in a `finally`, and the connection pool clears it again on check-in.

**A finding from writing the test for that second layer:** `RESET ALL` does
*not* reset the current role. It clears every custom setting — including
`app.current_user_id` — so it looks like it worked while the connection stays
switched into `finance_app`. Both statements are needed:

```python
cursor.execute("RESET ROLE")
cursor.execute("RESET ALL")
```

The test that builds a one-connection pool and checks what comes back is what
caught it. Nothing else would have: with the identity cleared, every query
still returned the right rows.

### Which tables get which policy

| Kind | Tables | Rule |
|---|---|---|
| Owned | accounts, transactions, budgets, rules, tags, plaid_items, audit_log | `user_id = me` |
| Shared | categories, merchants, merchant_aliases | read mine or global; **write mine only** |
| Derived | account_balances, sync_history, transaction_tags, webhook_jobs | ownership through the parent row |
| Self | users | `id = me` |
| Exempt | institutions, alembic_version | contain no user data |

The shared tables are the interesting case, and they use per-command policies
rather than one blanket policy:

```sql
CREATE POLICY merchants_read ON merchants FOR SELECT
    USING (user_id IS NULL OR user_id = app_current_user_id());
CREATE POLICY merchants_modify ON merchants FOR UPDATE
    USING (user_id = app_current_user_id())
    WITH CHECK (user_id = app_current_user_id());
```

A global merchant is visible to everyone, so allowing `UPDATE` on one would
let any user rewrite what every other user sees. The application already
copies-on-write instead of editing (see `taxonomy.py`); this makes that the
only possibility rather than a rule that holds until someone writes a
convenient bulk-fix script.

`merchants` is also the one table where the application inserts a *global*
row on purpose — `get_or_create_merchant` does, because recognising
"WHOLEFDS MKT" once should help everybody — so its INSERT policy permits
`user_id IS NULL` and the others do not.

### Failing closed

If the session variable is unset, `current_setting(..., true)` returns NULL,
`user_id = NULL` is NULL, and NULL is not TRUE. A request that somehow skipped
authentication sees an **empty database**, not everybody's.

```python
def test_no_identity_means_no_rows_rather_than_all_rows(db):
    db.execute(text('SET ROLE "finance_app"'))
    assert db.execute(select(Transaction)).scalars().all() == []
```

Every access-control system fails eventually. What matters is which way, and
"the list is empty" is a bug report; "the list has someone else's money in
it" is an incident.

### Drift

Two guard tests, on the model of the authentication one from Milestone 3:

* Every table in `Base.metadata` must be classified as protected or exempt.
  Adding a table to the application is enough to fail it, so "does this hold
  user data?" is a question someone answers rather than one nobody asks.
* Every table classified as protected must **actually** have RLS enabled and
  at least one policy, checked against `pg_class` and `pg_policy`. A table
  listed as protected but never altered in a migration would pass the first
  test and leak in production.

And one that checks the role itself has neither `BYPASSRLS` nor `rolsuper`
nor ownership of any table — all three of which make every policy above
decorative, and all three of which are easy to grant while debugging.

---

## 2. Rate limiting

Three different problems get called rate limiting, and conflating them
produces a limiter that solves none:

1. **Cost.** `/api/insights` calls Claude. A loop in someone's script becomes
   a bill. This is the one endpoint in the application where a request spends
   real money, and it is why the module exists.
2. **Load.** Fifty concurrent syncs from one user is a denial of service
   against everyone else.
3. **Unauthenticated abuse.** The Plaid webhook has no login, so it needs a
   limit keyed by something other than a user.

Volumetric attacks are not on that list. A limiter inside the application has
already accepted the connection and parsed the request; stopping a flood is
the job of the layer in front.

### Token bucket, not a counter

The obvious implementation is a counter reset hourly. It has a nasty edge:
spend the whole allowance at 10:59, get a fresh one at 11:00, and the real
peak is twice the limit back to back. A token bucket refills continuously, so
the long-run average is the limit and a burst is still allowed up to capacity.

### The honest limitation

State lives in one process's memory. Four uvicorn workers means four sets of
counters, so a limit of 20/hour is really up to 80/hour depending on which
worker answers.

That is a real weakness, and it is written in the module docstring rather
than discovered in a bill. It is still worth having — 80/hour is bounded, and
unbounded is the actual danger — and the upgrade path is a shared counter in
Redis behind the same one-method interface.

### Keyed by user, not by address

```python
def test_one_users_limit_does_not_affect_another(client, authenticated_user, other_user):
```

Users share IP addresses — offices, carriers, a household. A per-IP limit
would let one person's runaway script lock out their colleagues. The webhook
is the exception, because there is no user to key on; the docstring for
`limit_by_ip` explains why it uses the peer address rather than trusting
`X-Forwarded-For`, which anyone able to reach the application directly can
forge into a way of locking out an address of their choosing.

### Where the limit is declared

```python
def ask(payload: QuestionRequest, current_user: RateLimitedInsightsUser, ...):
```

`RateLimitedInsightsUser` both authenticates and meters. Declaring the limit
as *the thing that produces the user* means it cannot be quietly dropped —
remove it and the endpoint has no user at all.

---

## 3. The webhook queue

### What was wrong

Until now the webhook handler did the sync inline:

```
Plaid POSTs the webhook  ->  we sync 90 days of transactions  ->  30s
                         ->  Plaid times out and retries
                         ->  a second sync of the same item starts
                         ->  both still running when the third arrives
```

Nothing corrupted — the sync engine is idempotent, which is what made this
survivable — but the database did the same expensive work three times while
Plaid concluded our endpoint was broken.

The handler now writes a row, commits, and returns 200. Plaid is satisfied in
milliseconds.

### Why a table rather than Celery or `BackgroundTasks`

`BackgroundTasks` is one line and loses the job on every deploy. For a
webhook that says "this bank has new transactions", a lost job means the
user's data is silently stale until something else triggers a sync.

Celery means Redis, a broker, a worker fleet, and their monitoring — the
right call at scale, and considerable machinery for a queue whose depth is in
single digits.

A table in the database we already run sits between the two. Jobs survive a
restart because they are rows; retries and backoff are columns rather than
infrastructure; and `SELECT ... FOR UPDATE SKIP LOCKED` lets several workers
share the queue without a distributed lock:

```python
select(WebhookJob)
    .where(WebhookJob.status == JobStatus.PENDING)
    .where(WebhookJob.next_attempt_at <= now)
    .order_by(WebhookJob.next_attempt_at)
    .limit(1)
    .with_for_update(skip_locked=True)
```

The first worker locks the row; the second *skips past it* rather than
blocking. `SKIP LOCKED` is the whole reason a table is a credible queue.

### Collapsing retries

A partial unique index:

```sql
CREATE UNIQUE INDEX uq_webhook_jobs_pending_item_code
    ON webhook_jobs (plaid_item_id, webhook_code)
    WHERE status = 'pending';
```

Plaid resends webhooks it thinks we missed, and a bank posting a batch can
emit several `DEFAULT_UPDATE`s in a row. At most one *pending* job per item
per code means one sync instead of five — enforced by the table rather than
by a function remembering to check, and combined with `ON CONFLICT DO
NOTHING` so the check-then-insert race cannot happen.

Only pending jobs are deduplicated. A completed job still blocking new ones
would mean the second day's transactions never sync — a bug that looks like
the feature working, once.

### A detail that would have broken the retries

`SyncEngine.sync_item` **never raises**. It records every failure as a
`SyncHistory` row and returns, deliberately, so that a sync failing on page 9
keeps pages 1–8. That is right for the engine, and it means the queue has to
read the *outcome*:

```python
run = SyncEngine(db, gateway).sync_item(item, SyncTrigger.WEBHOOK)
if run.status == SyncStatus.FAILED:
    if item.status in (PlaidItemStatus.LOGIN_REQUIRED, PlaidItemStatus.ERROR):
        return          # terminal -- a retry cannot fix a reconnect
    raise SyncFailed(f"sync failed: {run.error_code}")
```

Without this the job would be marked SUCCEEDED on a sync that fetched
nothing, and the retry logic would never run at all.

### Crash recovery

A job that is `RUNNING` and started more than thirty minutes ago is assumed
to belong to a worker that died — a deploy mid-sync, an OOM kill — and is
returned to the queue with its attempt already counted, so a job that
reliably kills its worker still exhausts its retries rather than looping.

Without that, the row sits in `RUNNING` forever: no worker will touch it, and
the user's transactions simply never arrive, with no error anywhere. That is
the kind of failure worth a test.

### Where the worker runs

By default, on a background thread inside the API process — one thing to
deploy, one thing to watch. The costs are real and stated in the module
docstring: shared fate with the request path, and one poller per uvicorn
worker.

`WEBHOOK_WORKER_ENABLED=false` plus `python scripts/run_worker.py` gives the
separation. The queue does not care which; that is the point of putting the
state in the database.

It polls rather than using `LISTEN`/`NOTIFY`. NOTIFY is delivered at most
once and only to sessions connected at that moment, so a worker restarting
during the notification misses it and the job waits for something else to
poll. Building the reliable version means polling anyway, at which point the
notification is an optimisation on a latency nobody is measuring.

---

## 4. Partitioning the audit log

The audit log grows faster than any other table — several rows per synced
transaction — and nothing ever deletes from it. Converting it now, while it
holds a handful of rows, turns a future all-night migration into a five-minute
one today.

It is partitioned by month on `created_at`, which buys two things: a
retention policy becomes `DROP TABLE audit_log_2025_01` (instant, and the
disk comes back) instead of a `DELETE` that rewrites the table, and no single
partition ever gets large enough to make `VACUUM` a problem.

### What it costs

The primary key becomes `(created_at, id)`. PostgreSQL requires the partition
key to be part of every unique constraint, because it will not scan every
partition to check. So `session.get(AuditLog, some_id)` no longer works and
two tests now query by `id` instead — a small, permanent tax, noted here
because it is the kind of change that otherwise surprises someone six months
later.

### The security hole partitioning introduces

This is the part worth reading twice.

RLS policies belong to the parent table. **A partition is a table in its own
right, with its own privileges and no policies of its own.** And the schema's
`ALTER DEFAULT PRIVILEGES` — added by the RLS migration — grants the
application role DML on every new table in the schema. A partition is a new
table.

So without a deliberate step, `SELECT * FROM audit_log_2026_07` returns every
user's audit trail, having stepped politely around the policy on `audit_log`.

The fix is to revoke direct access whenever a partition is created, both in
the migration and in the maintenance job:

```sql
REVOKE ALL ON audit_log_2026_07 FROM finance_app;
```

Revoking rather than duplicating the policy onto every partition: nothing in
the application has any reason to name a partition, and access through the
parent is checked against the parent. There is a test that tries the direct
read and expects `permission denied`.

### The default partition, and its catch

A range-partitioned table has no partition for a month nobody created, and an
INSERT with nowhere to go **fails**. Audit writes share a transaction with
the change they record, so a missing partition on the 1st does not lose a log
entry — it fails the user's edit.

Two defences: a `DEFAULT` partition catches anything homeless, and
`audit_partitions.ensure_partitions()` runs hourly on the worker to keep three
months ahead.

The catch, stated plainly because it is easy to be smug about the default
partition: while rows for July sit in it, PostgreSQL **refuses** to create the
July partition — it would have to move them, and `CREATE TABLE ... PARTITION
OF` will not. Recovery means detaching the default, moving rows by hand, and
reattaching.

So the default partition is a safety net whose purpose is to stay empty, and
`default_partition_row_count()` exists to be alerted on. A non-zero value
means maintenance stopped running and there are a few days to notice before it
becomes a manual data migration.

---

## 5. Deployment

### The container

Multi-stage, so the C toolchain needed to build wheels does not ship in the
image that runs — smaller, and without the most useful thing an attacker
could find inside. It runs as uid 10001, not root: a remote-code-execution
bug in a root container is root on the container filesystem, including the
application's own code.

`.dockerignore` excludes `.env`. Copying it in bakes production credentials
into a layer that anyone who can pull the image can read — including after
the file is "deleted" in a later layer, because layers are additive.

No `--workers` in the `CMD`. Process count belongs to whatever knows how much
CPU the container was given, not to whatever number seemed right on a laptop.

### Logging, and why it needed its own module

Under uvicorn, none of this project's log output appeared. Uvicorn configures
its own loggers and leaves the root logger alone, so records from `app.*`
propagate to a root logger with no handler and are discarded.

Every module here logs carefully — the worker logs an error when audit
partitions fall behind, `deps.py` logs why a token was rejected, the rate
limiter logs who was throttled — and none of it was reaching anywhere. An
application that looks quiet and healthy while telling you nothing is worse
than one with no logging at all, because alerting on a line that is never
emitted reads as reassurance.

Found by running the server and looking, not by running the tests.

`configure_logging()` also pins `sqlalchemy.engine` to WARNING. Turning it up
would put every statement — including the values bound into them, which are
somebody's transactions — into the application log.

### Response headers

`nosniff`, `no-referrer`, `DENY`, `no-store`, and HSTS in production only.
The reasoning for each is in `main.py`; the one worth calling out is
`Cache-Control: no-store`, because every response here is somebody's
financial data and an intermediary is entitled to cache anything not told
otherwise. There is a test that the headers are present on a **401** as well
as a 200 — middleware that only runs on the happy path is absent exactly when
a response is unusual.

Interactive docs are disabled in production. The schema protects nothing by
being secret, but publishing a browsable map of the API to anonymous visitors
helps nobody except someone looking for a way in.

### Refusing to start

`Settings` already refused to boot in production without Clerk, encryption
keys, or with `DEBUG=true`. Two more: `ALLOWED_HOSTS` must be set, and
`FRONTEND_ORIGIN` must be HTTPS. Each has a test, plus one asserting a
*valid* production config is accepted — without it, all six could pass for
the wrong reason.

### Deploying it properly

The one step this codebase cannot take for you. In production, connect as a
**login role that is a member of `finance_app` and owns nothing**:

```sql
CREATE ROLE finance_api LOGIN PASSWORD '...';
GRANT finance_app TO finance_api;
-- and NOT the owner of any table, and without BYPASSRLS
```

Then there is no owner privilege for a `RESET ROLE` to fall back to, and the
`SET ROLE` caveat from section 1 goes away. Migrations continue to run as the
owning role, separately, as they should.

Secrets come from the platform's secret store — Fly secrets, Railway
variables, AWS Secrets Manager — as environment variables. `.env` is for
laptops. `ENCRYPTION_KEYS` in particular is the one value whose loss makes
every stored Plaid connection permanently unusable; it belongs in a backup
that is not this repository and not the same account as the database.

---

## Verification

Not assumed:

* **425 tests pass** (413 before the hardening tests; 288 at Milestone 6).
* The RLS tests deliberately write the unscoped query — `select(Transaction)`
  with no WHERE clause — and assert the database refuses to leak. If RLS were
  quietly disabled, or one table's policy were missing, these fail and nothing
  else in the suite does.
* The pooled-connection scrub is tested against a real one-connection pool, so
  the second session provably gets the first one back. That test found the
  `RESET ALL` bug described above.
* The server was run under uvicorn and checked by hand: security headers
  present on real responses, 401 without a token, the worker logging its start
  and clean stop, and the audit partitions created through to July 2027 in the
  development database. The logging gap was found this way.
* The queue tests drive claim/run/fail/retry directly rather than waiting on a
  poller, so nothing here depends on timing.

## What is still not done

Stated because a hardening milestone that claims completeness is lying:

* **The rate limiter is per-process.** Documented above; a real fix is Redis.
* **`SET ROLE` is reversible** by anything that can execute arbitrary SQL as
  our connection role. The deployment step above closes it; nothing in this
  repository enforces that the step was taken.
* **No secret rotation procedure.** `ENCRYPTION_KEYS` supports rotation by
  design (Milestone 4) and nothing automates or documents the actual
  turning of the key.
* **No backup or restore drill.** A backup nobody has restored is a
  hypothesis.
* **The retention policy is a function nobody calls.**
  `drop_partitions_before()` is deliberately not automatic — how long to keep
  an audit trail is a policy decision, possibly a regulatory one, and not
  something a maintenance job should quietly enact.
* **No metrics or tracing.** The logs are now readable; there is no dashboard
  and no alert wired to `default_partition_row_count()`, which section 4 says
  should be alerted on.
