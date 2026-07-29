# Milestone 5 — Categorization, Merchants, Rules & Tags

## 1. Goal

Turn raw bank descriptors into meaningful data:

```
WHOLEFDS MKT #10259   →   Whole Foods  ·  Groceries  ·  #reimbursable
```

## 2. Why this matters

After Milestone 4 you have transactions, and they are almost unreadable.
`SQ *TST* BLUE BOTTLE 4821 SEATTLE WA` tells you nothing you can total,
chart, or ask a question about.

Every feature still to come — dashboards, trends, subscriptions, "why did I
spend more this month?" — depends on transactions being grouped correctly.
Categorization is not a nicety layered on top; it is the substrate everything
else stands on.

---

## 3. Run it

```bash
git pull origin claude/personal-finance-dashboard-qqb2rg

cd backend && source .venv/bin/activate
alembic upgrade head            # case-insensitive tags + search indexes
uvicorn app.main:app --reload --port 8000

cd ../frontend && npm run dev
```

No new dependencies this milestone. Open
<http://localhost:3000/transactions>.

---

## 4. The four layers of categorization

Ordered by how much we trust them:

| Layer | Source | Beats |
|---|---|---|
| 1 | **The user** (`user_category_id`) | everything |
| 2 | **A rule** — "SQ *BLUE BOTTLE → Coffee" | 3 and 4 |
| 3 | **The merchant's default** — Whole Foods → Groceries | 4 |
| 4 | **Plaid's guess** — `personal_finance_category` | — |

Layers 2–4 all write `auto_category_id`. Layer 1 lives in a different column
entirely, and the displayed value is `COALESCE(user, auto)`.

That separation is structural, not a rule someone has to remember: the
enrichment code cannot overwrite a user's choice because the column it would
have to write is one it never touches. `auto_category_source` records which of
2–4 decided, which is what lets the UI say "set by a rule" or "from this
merchant".

---

## 5. Merchant normalization, and what it cannot do

`normalize_descriptor()` strips everything that varies between visits:

| Stripped | Example |
|---|---|
| Processor prefixes | `SQ *`, `TST*`, `PAYPAL *` |
| Store/terminal numbers | `#10259`, `STORE 3421` |
| Card and reference numbers | `XXXX1234`, long digit runs |
| Trailing city and state | `SEATTLE WA` |
| Dates and times | `03/14`, `14:22` |
| Accents and punctuation | `CAFÉ` → `cafe` |

So `Whole Foods #125`, `WHOLE FOODS 3421 SEATTLE WA` and `SQ *WHOLE FOODS`
all reduce to `whole foods`.

### The limitation, stated plainly

`WHOLEFDS MKT` does **not** reduce to `whole foods`, and no amount of string
cleanup will make it. That is an *abbreviation*, not noise — resolving it
needs a dictionary of the world's merchants, which a regular expression
cannot contain.

I originally wrote a test asserting it did, and it failed. That was the test
being wrong, not the code, and fixing it honestly is more useful than chasing
an ever-cleverer regex. There are three layers, and normalization is the
weakest:

1. **`MerchantAlias`** — an explicit past correction by this user. Exact.
2. **Plaid's `merchant_name`** — Plaid resolves descriptors against its own
   merchant database, so all those variants arrive with
   `merchant_name = "Whole Foods Market"`. `resolve_merchant()` keys on this
   when present, which is what actually unifies them in production.
3. **Normalization** — the fallback for manual and CSV imports, where nobody
   has enriched anything.

Finding that gap is what prompted layer 2 to be used for *matching* rather
than just for the display label. The test that failed made the product better.

---

## 6. Learning from corrections — and why it is opt-in

When you correct a transaction, the checkbox **"apply to all from this
merchant"** decides what happens next:

- **Unchecked** (default): only this transaction changes.
- **Checked**: we record a `MerchantAlias` and/or set the merchant's default
  category, so every past and future transaction from that merchant follows.

It is tempting to learn from every correction silently. Don't. A user fixing
*one* transaction — "this particular Amazon order was a gift" — does not mean
every Amazon order is a gift. Applying that across three years of history
would be both surprising and hard to undo.

**Only the user knows whether a correction is a one-off or a pattern, so the
system asks instead of guessing.** Two tests pin both halves of this down.

Corrections are also always scoped to *you*. Two users can legitimately
disagree about what a shared descriptor means, and writing to the global list
would let one silently change the other's data.

---

## 7. The rules engine

A rule is "if a transaction looks like X, do Y":

```json
{
  "conditions": {
    "operator": "AND",
    "conditions": [
      {"field": "raw_name", "op": "contains", "value": "BLUE BOTTLE"},
      {"field": "amount", "op": "gt", "value": "5.00"}
    ]
  },
  "actions": {"set_category_id": "…", "add_tag_ids": ["…"]}
}
```

### Validation moved, it did not disappear

Milestone 2 stored rules as JSONB and stated the trade-off honestly: **the
database can no longer validate what is inside that JSON.** `app/schemas/
rules.py` is where that guarantee moved to.

Without it, a malformed rule surfaces only when the engine runs — during a
background sync, where the failure is invisible and the user has no idea
their rule never worked. Validating at the API boundary means it is rejected
the moment it is written.

> **The general principle: when you give up a database guarantee, you owe an
> equivalent guarantee somewhere else.**

### Why there is no regex operator

A user-supplied pattern like `(a+)+$` backtracks catastrophically. One rule
could hang the sync engine for every user on the server — a denial of service
with no attacker required, just an unlucky pattern. `contains`,
`starts_with`, `ends_with` and friends cover every realistic need.

### Ordering is explicit

Rules run by `(priority, created_at)`, lowest number first. Without a defined
order, which of two conflicting rules wins depends on the order the database
happened to return rows — and the symptom is a transaction categorized
differently on Tuesday than on Monday, with nothing having changed.

`stop_processing` lets a rule declare itself the final word.

### Preview before you apply

`POST /api/rules/{id}/preview` reports what a rule *would* match, changing
nothing. Applying an untested rule to years of history is a frightening
button to press; seeing "matches 12 transactions, e.g. these three" first
makes it an informed decision — and a rule matching 4,000 rows when you
expected 12 is obviously wrong before it touches anything.

---

## 8. Re-running enrichment is always safe

`POST /api/rules/apply` re-enriches your transactions. Run it after adding a
rule, correcting a merchant, or improving the normalizer — as often as you
like.

It is safe for one structural reason: **enrichment writes only `auto_*`
columns.** It cannot destroy a manual correction, because the columns it
would have to write are ones it does not touch. Three tests assert this from
different angles, including one that runs the full sync → correct → re-sync
cycle.

---

## 9. Tags vs categories

|  | Category | Tag |
|---|---|---|
| How many | Exactly one | Unlimited |
| Answers | *What kind of spending?* | *Anything else you care about* |
| Example | Restaurants | Business, Tax Deductible, Reimbursable |

One dinner can be Restaurants **and** Business **and** Tax Deductible **and**
Reimbursable. Forcing that into a single field is precisely the limitation
that makes most budgeting apps frustrating.

Tag names are now **case-insensitively unique**, enforced by a functional
index (`UNIQUE (user_id, lower(name))`) added in migration `5d94c4e6a26a` —
paying off a comment left in Milestone 2. Without it a user ends up with
"Vacation" and "vacation", two tags they believe are one, and their vacation
report silently shows half their spending. Names are stored as typed; only
the comparison is folded.

---

## 10. Testing

```bash
cd backend && source .venv/bin/activate && pytest
```

Expected: **242 passed**.

### Try it yourself

1. Open <http://localhost:3000/transactions>. Transactions already have
   merchants and categories — enrichment ran during the sync.
2. Change a category from the dropdown. Leave the checkbox unchecked.
3. Change another, **with** "apply to all from this merchant" checked.
4. Create a rule via the API and preview it before applying:

```bash
curl -X POST localhost:8000/api/rules \
  -H "Content-Type: application/json" -H "Authorization: Bearer <token>" \
  -d '{"name":"Coffee","conditions":{"conditions":[
        {"field":"raw_name","op":"contains","value":"STARBUCKS"}]},
       "actions":{"set_category_id":"<coffee-id>"}}'
```

5. Correct a category by hand, then `POST /api/rules/apply` and confirm your
   correction survived. **That is the invariant this milestone rests on.**

---

## 11. Troubleshooting

| Symptom | Cause | Fix |
|---|---|---|
| Everything is "Uncategorized" | Enrichment has not run over old rows | `POST /api/rules/apply` |
| A rule matches nothing | Value does not appear in `raw_name` | Preview it; check `raw_name`, not the display name |
| A rule matches everything | Empty or overly broad condition | Preview before applying |
| 409 creating a tag | Case-insensitive duplicate | "vacation" collides with "Vacation" |
| 422 creating a rule | Malformed conditions/actions | The response body names the field |
| Two merchants that should be one | Abbreviated descriptor | Correct one transaction with "apply to all" ticked |
| Corrections seem to vanish | They shouldn't — file a bug | Check `user_category_id` directly in psql |

---

## 12. What I got wrong while building this

**A test asserted something impossible.** I wrote
`assert "whole foods" in normalize_descriptor("WHOLEFDS MKT #10259")` — and
it failed, correctly. String cleanup cannot expand an abbreviation. Rather
than weakening the test and moving on, the fix was to make
`resolve_merchant()` key on Plaid's `merchant_name` when present, which is
what genuinely unifies those descriptors, and to replace the test with an
honest one that documents the limitation. **The failing test made the product
better.**

**Two smaller ones**, both caught by running the code:

- A Pydantic field named `date` shadowed the imported `date` type. Because
  `date: date | None = None` assigns `date = None` in the class body, the
  annotation then evaluated as `None | None` and the model failed to build.
  Aliasing the module (`import datetime as dt`) sidesteps it.
- Every export in a `"use server"` file must be declared `async`. Returning a
  Promise from a sync function is not enough — the build fails with "Server
  Actions must be async functions".

---

## 13. Checklist before Milestone 6

- [ ] `alembic upgrade head` applied `5d94c4e6a26a`
- [ ] `pytest` prints **242 passed**
- [ ] `/transactions` shows merchants and categories filled in automatically
- [ ] Search, category, tag and date filters all work
- [ ] You changed a category and saw the "edited" badge appear
- [ ] You used "apply to all from this merchant" and saw it stick
- [ ] You created a tag, and a second one differing only by case was rejected
- [ ] You previewed a rule, then applied it
- [ ] You corrected a category, ran `POST /api/rules/apply`, and the
      correction survived
- [ ] "Reset to original" restored the bank's version

---

## What's next — Milestone 6: Dashboards & Analytics

With transactions categorized, the numbers become computable:

- Monthly spend, cash flow, savings rate, income tracking
- Net worth over time, from the balance snapshots collected since Milestone 4
- Category trends, month-over-month and year-over-year
- Merchant analysis and recurring-subscription detection
- Budget vs actual, rolling 30/90-day windows, burn rate and cash runway

The interesting engineering problem there is aggregation performance: doing
this in Python over every transaction works at a thousand rows and collapses
at a hundred thousand. We will push the work into SQL and measure it.
