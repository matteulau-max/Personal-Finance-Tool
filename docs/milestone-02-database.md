# Milestone 2 — Database Schema & Migrations

## 1. Goal

Design and build the complete database schema: 14 tables, the migration system
that creates them, and 49 tests proving the rules hold.

Nothing connects to Plaid yet. This milestone is about getting the *shape* of
the data right.

## 2. Why this matters more than anything else we'll build

Application code is easy to change. You rewrite a function, redeploy, done.

A database schema is not. Once it holds two years of your real transactions,
changing it means migrating live data without losing or corrupting any of it.
Mistakes made here are the ones you live with.

So this milestone is where we spend the most care, and where the two hardest
requirements from your brief get solved:

> **Never duplicates transactions**
> **Allows manual corrections without losing the original data**

Both are solved structurally — by the shape of the tables — rather than by
careful application code. That distinction is the single most important idea
in this milestone, and section 5 explains why.

---

## 3. What we built

```
backend/app/
├── db/
│   ├── base.py           Shared conventions: UUID keys, timestamps, naming
│   └── session.py        Engine + the get_db() dependency
├── models/
│   ├── enums.py          Every enumerated type + the enum_column() helper
│   ├── user.py           users
│   ├── institution.py    institutions
│   ├── plaid_item.py     plaid_items       (holds the Plaid access token)
│   ├── account.py        accounts, account_balances
│   ├── transaction.py    transactions      ← read this one closely
│   ├── merchant.py       merchants, merchant_aliases
│   ├── category.py       categories        (a tree)
│   ├── tag.py            tags, transaction_tags
│   ├── rule.py           rules
│   ├── sync_history.py   sync_history
│   └── audit_log.py      audit_log
├── migrations/           Alembic: versioned schema changes
└── tests/                49 tests
```

Each model file opens with a comment explaining *why* it looks the way it
does. Those comments are the real teaching material — this document is the
map, the code is the territory.

### How the tables relate

```
User
 ├── PlaidItem ────── Institution        (one login at one bank)
 │     └── Account                       (checking, card, savings…)
 │           ├── Transaction
 │           └── AccountBalance          (a snapshot per sync = history)
 ├── Tag ──────┐
 ├── Rule      │  many-to-many
 └── Category  │
               └── TransactionTag
Merchant ── MerchantAlias                (learned name corrections)
SyncHistory                              (one row per sync attempt)
AuditLog                                 (append-only; survives deletion)
```

---

## 4. Run it

All commands run from `backend/`, with the virtualenv activated
(`source .venv/bin/activate`).

```bash
git pull origin claude/personal-finance-dashboard-qqb2rg

# New dependencies were added (Alembic, psycopg)
pip install -r requirements.txt

# Make sure PostgreSQL is running (from the repo root)
docker compose up -d

# Create all 14 tables and seed the built-in categories
alembic upgrade head
```

Expected output:

```
INFO  [alembic.runtime.migration] Running upgrade  -> 13005205e074, initial schema
INFO  [alembic.runtime.migration] Running upgrade 13005205e074 -> fc0c7beca47e, seed system categories
```

**Verify it worked:**

```bash
docker exec -it finance-postgres psql -U finance -d finance -c "\dt"
```

You should see 15 tables (our 14, plus `alembic_version`, which records which
migrations have run).

```bash
docker exec -it finance-postgres psql -U finance -d finance \
  -c "SELECT count(*) FROM categories;"
```

Should print **67** — the seeded category tree.

---

## 5. The four decisions worth understanding

### 5.1 Deduplication is enforced by the database, not by code

The obvious way to avoid duplicates is to check first:

```python
existing = db.query(Transaction).filter_by(plaid_transaction_id=txn_id).first()
if not existing:
    db.add(Transaction(...))        # ← looks correct. Is not.
```

This is a **race condition**. Two syncs running at the same moment — a Plaid
webhook and the nightly job, say — can both run the query, both find nothing,
and both insert. You now have a duplicate, and no amount of code review would
have caught it, because the code reads perfectly.

Our schema instead declares:

```python
UniqueConstraint("account_id", "plaid_transaction_id")
UniqueConstraint("account_id", "fingerprint")
```

A unique constraint **cannot be raced**. PostgreSQL serializes the check
internally; the second INSERT fails, every time, no matter how many processes
are running. We catch that failure and turn it into an update.

> **The general principle:** if a rule *must* hold, put it in the database.
> Application code can be bypassed by a script, a migration, a second server,
> or a future developer who doesn't know the rule exists. A constraint cannot.

Note the constraint is on *(account, id)*, not on the id alone. Plaid only
guarantees transaction ids are unique within one Item — a globally unique
constraint would eventually reject a legitimate transaction.

### 5.2 Three layers, so a correction never destroys the original

Every editable fact on a transaction exists in up to three columns:

| Layer | Written by | Example |
|---|---|---|
| `raw_*` | the sync engine only | `raw_amount = 52.30` |
| `auto_*` | our categorization engine | `auto_category_id = Groceries` |
| `user_*` | the human, explicitly | `user_category_id = Restaurants` |

What the app displays is `COALESCE(user_x, auto_x, raw_x)` — the first
non-NULL, reading right to left. Those are the `effective_*` properties on the
model.

So when you recategorize a transaction:

- `user_category_id` is set,
- `auto_category_id` keeps our guess (useful later for measuring how often our
  categorizer is wrong),
- `raw_*` is untouched — the bank's version is still there, exactly as sent.

"Reset to original" is just setting the `user_` column back to NULL. There is
no backup to restore and nothing that can go stale, because the original was
never modified in the first place.

On top of that, `raw_payload` stores the **entire** original JSON from Plaid.
A field we never thought to model today is still recoverable in two years —
by which time re-fetching it may be impossible.

### 5.3 Money is DECIMAL, never a float

```python
MONEY = Numeric(18, 4)
```

Floating-point numbers cannot represent 0.1 exactly. Add it a thousand times
and you drift. There is a test that does exactly this:

```python
def test_summing_many_small_amounts_stays_exact(db, account):
    for _ in range(1000):
        make_transaction(db, account, raw_amount="0.10")
    total = db.execute(select(func.sum(Transaction.raw_amount))).scalar_one()
    assert total == Decimal("100.00")     # a float would not be exactly this
```

A dashboard that disagrees with a bank statement by three cents destroys a
user's trust in every other number you show them. **Use `Decimal` for money.
Always. In every language, in every project.**

### 5.4 Nothing financial is ever hard-deleted

- Removed transactions get `status = REMOVED` and keep their row.
- `audit_log`'s foreign key to `users` is `ON DELETE SET NULL`, not `CASCADE`
  — the audit trail outlives the thing it describes. An audit log that
  disappears along with the deleted record would be worthless, since a
  deletion is precisely the event you most need to investigate.
- Balance snapshots accumulate, giving you net-worth-over-time for free. A
  balance you didn't record is gone forever — no bank API will tell you what
  your checking balance was last March.

---

## 6. Migrations, and why they exist

A migration is a versioned, repeatable description of a schema change. Alembic
stores them in `migrations/versions/` and records which have run in the
`alembic_version` table.

Without migrations, "update the database" means someone running SQL by hand,
and production drifting from your laptop in ways nobody can reconstruct.

### The commands you'll actually use

```bash
alembic upgrade head          # apply everything not yet applied
alembic current               # which revision is this database on?
alembic history               # list all migrations
alembic downgrade -1          # undo the most recent migration
```

### Making a schema change

1. Edit the model in `app/models/`.
2. **Add the model to `app/models/__init__.py`** if it's a new file. This is
   the one bookkeeping step with teeth: Alembic diffs against
   `Base.metadata`, so a model that was never imported is invisible — and
   Alembic will cheerfully generate a migration that DROPS the table it can't
   see.
3. Generate the migration:
   ```bash
   alembic revision --autogenerate -m "add budget table"
   ```
4. **Read the generated file before running it.** Autogenerate is a helpful
   assistant, not an oracle. It regularly gets renames wrong — it sees a
   dropped column and a new one, and generates `DROP` + `ADD`, which silently
   destroys the data instead of renaming it.
5. Apply and verify:
   ```bash
   alembic upgrade head
   alembic downgrade -1 && alembic upgrade head   # prove it reverses cleanly
   ```

### Two kinds of migration

`13005205e074_initial_schema.py` is a **schema** migration — it changes the
shape of tables.

`fc0c7beca47e_seed_system_categories.py` is a **data** migration — it changes
contents. Both belong in Alembic, because both must happen in a defined order
on every environment.

That seed file demonstrates two techniques worth stealing:

- **Deterministic UUIDs** via `uuid5(namespace, slug)`. "Groceries" has the
  same ID in every database, forever, so seed data is safe to reference from
  code and safe to re-run.
- **A frozen table definition.** It declares a minimal table with `sa.table()`
  instead of importing our model. A migration is a historical record and must
  keep working exactly as written — if it imported the live model and someone
  later added a NOT NULL column, this old migration would start failing on
  fresh databases, and no new developer could ever set the project up.

---

## 7. Testing

```bash
cd backend
source .venv/bin/activate
pytest
```

Expected: **49 passed**.

### Three things about how these tests are built

**They run against real PostgreSQL, not SQLite.** It's tempting to use
in-memory SQLite because it needs no setup. Don't. Our schema uses JSONB,
PostgreSQL UUID columns, and `NULLS NOT DISTINCT` — none of which SQLite has.
A suite that passes on SQLite proves nothing about production. `conftest.py`
creates a separate `finance_test` database so your development data is never
touched.

**The test database is built by running the real migrations.** So the suite
also proves the migrations work. A schema that exists only in the models but
has no working migration is useless — you could never deploy it.

**Each test is isolated by a transaction rollback.** Rebuilding the schema per
test would be far too slow, so each test runs inside a transaction that gets
rolled back. Tests can run in any order and a failing test can't poison the
next one. That's why the whole suite finishes in about 2 seconds.

### Try breaking something on purpose

This is the best way to see the guarantees are real:

```bash
docker exec -it finance-postgres psql -U finance -d finance
```

```sql
-- Find any transaction, then try to store nonsense in its status:
UPDATE transactions SET status = 'banana' WHERE id = (SELECT id FROM transactions LIMIT 1);
-- ERROR:  new row for relation "transactions" violates check constraint
--         "ck_transactions_ck_transactionstatus"
```

The database refused. Not the API, not Python — the database. That's the
guarantee.

Then open <http://localhost:3000>. The status card now shows **Database:
Connected** and the schema version (`fc0c7beca47e`).

---

## 8. Troubleshooting

| Symptom | Cause | Fix |
|---|---|---|
| `Can't load plugin: sqlalchemy.dialects:postgresql.psycopg` | Old dependencies | `pip install -r requirements.txt` |
| `connection refused` on port 5432 | Postgres not running | `docker compose up -d` from the repo root |
| `Target database is not up to date` | Pending migrations | `alembic upgrade head` |
| `Can't locate revision identified by '...'` | A migration file was deleted | `alembic history` to inspect; don't delete applied migrations |
| Autogenerate wants to DROP all your tables | Models not imported | Add the model to `app/models/__init__.py` |
| Tests fail with `relation "users" does not exist` | Test DB never migrated | Check `conftest.py` can reach PostgreSQL |
| `DROP DATABASE cannot run inside a transaction block` | Postgres restriction | Use `isolation_level="AUTOCOMMIT"` — `conftest.py` shows how |

---

## 9. Common mistakes

- **Editing a migration that has already been applied somewhere.** Once a
  migration has run on any environment other than your laptop, it is
  immutable. Write a new one.
- **Trusting `--autogenerate` blindly.** Always read the file. It cannot tell
  a rename from a drop-plus-add.
- **Forgetting to add a new model to `app/models/__init__.py`.**
- **Using `Float` for money.** Never.
- **Hard-deleting financial records.** Set a status; keep the row.
- **Assuming `Enum(...)` validates.** In SQLAlchemy 2.0 it does *not* by
  default — see below.

---

## 10. Two bugs found while building this

Worth showing, because both were invisible until specifically tested for, and
both are mistakes almost everyone makes.

**1. Enum columns had no validation at all.** The natural spelling —
`Enum(TransactionStatus, native_enum=False)` — looks like it constrains the
column. It doesn't: SQLAlchemy 2.0 defaults `create_constraint=False`, so the
column was a plain `VARCHAR` that would happily store `'banana'`. Fixed by
`create_constraint=True` in `enum_column()`, and locked down by
`test_an_invalid_enum_value_is_rejected_by_the_database`.

**2. Enums were being stored by NAME, not VALUE.** `TransactionStatus.POSTED`
has the value `"posted"`, but SQLAlchemy persists the *name* — so the database
held `"POSTED"`. That would have leaked uppercase strings into API responses
and broken any hand-written SQL filtering on the documented lowercase value.
Fixed with `values_callable`, locked down by
`test_enums_are_stored_as_lowercase_values_not_names`.

Both fixes live in one helper, `enum_column()` in `app/models/enums.py`, so
they can't be forgotten on the next enum column. **The lesson: "it looked
right" is not verification. Assert on what the database actually stored.**

---

## 11. Checklist before Milestone 3

- [ ] `pip install -r requirements.txt` completed
- [ ] `docker compose ps` shows `finance-postgres` healthy
- [ ] `alembic upgrade head` ran both migrations
- [ ] `\dt` in psql shows 15 tables
- [ ] `SELECT count(*) FROM categories;` returns 67
- [ ] `pytest` prints **49 passed**
- [ ] `alembic current` shows `fc0c7beca47e (head)`
- [ ] <http://localhost:8000/health/db> returns `"database":"connected"`
- [ ] <http://localhost:3000> shows Database: **Connected** + schema version
- [ ] You tried the `status = 'banana'` UPDATE and watched it be rejected
- [ ] You've skimmed `app/models/transaction.py` — especially the three
      decisions at the top

---

## What's next — Milestone 3: Authentication

Right now every table has a `user_id` column and nothing fills it in. Next we
add Clerk, and with it the rule that governs every endpoint we ever write:

**a request may only ever see rows belonging to the user who made it.**

We'll cover how JWTs actually work, why authorization must be enforced in the
data layer rather than the UI, and how to write a test that fails if an
endpoint ever forgets to filter by user — the single most dangerous bug class
in a multi-user financial application.
