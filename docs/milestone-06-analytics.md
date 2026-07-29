# Milestone 6 — Dashboards & Analytics

## 1. Goal

Turn categorized transactions into numbers you can act on: cash flow, net
worth over time, category trends, budgets, subscriptions, burn rate, and
runway — computed in SQL and rendered as charts that are legible in both
light and dark, and for colourblind readers.

## 2. Why this matters

Everything so far has been about getting the data *right*. This milestone is
about making it *useful*.

It is also where a naive implementation quietly becomes unusable. The obvious
way to compute "spending by category" is to load the transactions and add
them up in Python. That works beautifully at a thousand rows and falls apart
at a hundred thousand — and it falls apart *gradually*, so nobody notices
until the dashboard takes four seconds and everyone has learned to avoid it.

So this milestone has a measurement, not an assumption.

---

## 3. Run it

```bash
git pull origin claude/personal-finance-dashboard-qqb2rg

cd backend && source .venv/bin/activate
alembic upgrade head        # adds the budgets table
uvicorn app.main:app --reload --port 8000

cd ../frontend && npm run dev
```

Open <http://localhost:3000/insights>.

No new dependencies — the charts are hand-written SVG (see §6).

---

## 4. SQL vs Python — the measurement

`backend/scripts/benchmark_analytics.py` builds a throwaway database, fills
it with synthetic transactions, and times both implementations. Run it
yourself:

```bash
cd backend && source .venv/bin/activate
PYTHONPATH=. python scripts/benchmark_analytics.py --rows 100000
```

Measured on this machine, spending-by-category over three years:

| rows | SQL | Python | ratio |
|---:|---:|---:|---:|
| 10,000 | 8 ms | 367 ms | 45× |
| 25,000 | 18 ms | 880 ms | 49× |
| 100,000 | **76 ms** | **3,884 ms** | 51× |

Both scale roughly linearly, so the *ratio* stays broadly constant — around
50×. I originally wrote "two orders of magnitude, and the gap widens"; the
measurement said otherwise and the comment was corrected. **That is the point
of measuring.**

What actually changes with size is the absolute number, and that is what
decides whether the product is usable. At 100,000 transactions the Python
version takes nearly four seconds to render *one panel of one page*. SQL
stays under a tenth of a second.

Three reasons the gap exists:

1. Every row crosses the network from PostgreSQL to Python.
2. Every row becomes a Python object — hundreds of bytes each, versus the few
   bytes PostgreSQL needs to add a number to a running total.
3. Memory grows with your *history*, not with the size of the *answer*.

---

## 5. Three rules every query obeys

All in `backend/app/services/analytics.py`.

### 5.1 Transfers are excluded from spending

Paying your credit card moves money between your own accounts. Counting it as
spending double-counts every purchase that card already recorded. This is
what the `is_transfer` flag seeded back in Milestone 2 is for, and it is the
single most consequential exclusion in the file — without it every total on
the dashboard is inflated, plausibly and invisibly.

### 5.2 Income is separated, not netted

Income has a *negative* amount under Plaid's convention. Summing everything
together nets salary against groceries and produces a number that means
nothing. Spending and income are computed as two aggregates in one pass using
`FILTER`.

### 5.3 Effective values, always

`COALESCE(user_amount, raw_amount)`, never `raw_amount`. A dashboard that
ignores your own corrections is worse than no dashboard — it is confidently
wrong, and you can see that it is.

### And two subtleties worth knowing

**Multiple snapshots per day.** Syncs do not run on a tidy schedule, so an
account can have three balance snapshots on Monday and none on Tuesday.
Summing them would triple-count Monday. `DISTINCT ON (day, account)` takes
the last snapshot per account per day. This is a PostgreSQL-specific feature
and a genuinely good reason to be on PostgreSQL — the portable equivalent is
a window function plus a subquery and is much harder to read.

**Burn rate excludes the current month.** On the 3rd, this month's spending
is a fraction of a normal month. Including it drags the average down and
*overstates* your runway — precisely when an accurate number matters most.

---

## 6. The charts

Hand-written inline SVG, no charting library. Three reasons: they render
inside Server Components so the SVG is in the initial HTML; every mark spec
is deliberate; and there are no dependencies to keep patched.

### Colour was computed, not chosen

The two-colour categorical palette was run through a validator in both light
and dark mode. It passes every check — lightness band, chroma floor,
colourblind separation (worst adjacent ΔE 24.7 light / 26.8 dark against a
target of 8), normal-vision separation, and 3:1 contrast against the surface.

**Dark mode is a *selected* set of steps, not an inverted light palette.** An
automatic flip fails the contrast check.

### The form fits the job

| Panel | Form | Why |
|---|---|---|
| Net worth headline | Hero figure | One number. A one-bar chart would be more ink for less information |
| Spent / income / burn / runway | Stat tiles | Headline numbers, no plot needed |
| Cash flow | Grouped columns | Two series sharing one unit → **one axis** |
| Net worth over time | Area, single series | Trend; no legend, since the title names the series |
| Category & merchant breakdown | Horizontal bars | Long names; horizontal avoids rotated labels |
| Budget vs actual | Meter | One ratio against one limit |
| Recurring charges | Table | Two unrelated measures over many rows |

### One instinct I had to correct

My first plan was to shade the category bars darker-where-bigger. That is a
**value ramp on nominal categories**, and it is wrong: categories have no
natural order, so the ramp double-encodes bar length as hue, spends the one
free channel on information the chart already shows, and produces a colour
set that fails the contrast and chroma checks by construction. Every category
bar is now the same colour.

### Never a dual-axis chart

Spending and income are both dollars, so they share one axis. Two y-scales
would let the chart invent a relationship that is not in the data — the most
common and most misleading charting mistake there is.

### Accessibility

- Two-series charts always carry a legend; identity is never colour-alone.
- Over-budget rows say "Over by $12" in text — the red is a supplement.
- Increases and decreases are marked with an explicit `+` or `−`.
- Every chart has an `aria-label`; bars and points carry `<title>` tooltips.

**I rendered the page and looked at it** — light and dark, screenshotted at
2× — rather than trusting that the geometry was right. The validator checks
colour, not layout.

---

## 7. Testing

```bash
cd backend && source .venv/bin/activate && pytest
```

Expected: **288 passed**.

Aggregation bugs are quiet — the page renders, the number looks plausible,
and it is wrong. Each test pins one rule a plausible-looking query would
break: transfers excluded, income separated, corrections respected, removed
and hidden rows skipped, month boundaries inclusive on both ends, liabilities
subtracted, snapshots deduplicated per day.

### Try it yourself

```bash
# Set a budget
curl -X PUT localhost:8000/api/analytics/budgets \
  -H "Content-Type: application/json" -H "Authorization: Bearer <token>" \
  -d '{"category_id":"<groceries-id>","period_start":"2026-07-01","amount":"600"}'
```

Then on `/insights`, check that a credit-card payment does **not** appear in
your category breakdown. That is rule 5.1 working, and it is the one most
worth confirming with your own data.

---

## 8. Troubleshooting

| Symptom | Cause | Fix |
|---|---|---|
| Net worth shows but the chart is empty | Fewer than two snapshots | Sync a few times; history accumulates per sync |
| Spending looks far too high | Transfers miscategorized | Check that card payments use a `is_transfer` category |
| Savings rate is "—" | No income recorded this month | Expected: the rate is undefined, not zero |
| Cash runway is "—" | Income exceeds spending | Expected: you are not burning cash |
| Recurring list is empty | Needs 3+ charges from one merchant | A genuine limit — see §9 |
| Budget shows $0 spent | Spending is in a different category | Budgets match on the *effective* category |
| Benchmark fails to start | `PYTHONPATH` not set | `PYTHONPATH=. python scripts/…` |

---

## 9. Honest limits of the recurring detector

It flags a merchant seen 3+ times, at a consistent amount (spread under 15%),
at gaps between 5 and 100 days.

- **Misses**: a subscription paid only twice so far; one whose price changes
  every month.
- **Wrongly flags**: a coffee shop you visit every Tuesday for the same
  amount.

That is acceptable *because of how the result is used* — a list you review,
not an action taken automatically. A heuristic that suggests is fine; the
same heuristic silently cancelling things would not be. The `is_subscription`
flag on Merchant is how this improves over time instead of staying a guess.

---

## 10. Something the earlier milestones caught

While wiring up budgets, `scoped_select(Budget, user)` raised:

```
NotScopeable: Budget is not a user-owned model. Add it to USER_OWNED_MODELS
or SHARED_OR_OWNED_MODELS in app/db/scoping.py after deciding how it should
be scoped.
```

That is the Milestone 3 design working exactly as intended. I added a new
user-owned table and forgot to classify it. Because the helper **fails closed**
rather than falling back to an unfiltered query, the mistake surfaced as a
loud test failure instead of a silent cross-user data leak.

A design that turns "I forgot" into a build error is worth far more than one
that relies on remembering.

---

## 11. Checklist before Milestone 7

- [ ] `alembic upgrade head` applied `ff5166b6ed4c`
- [ ] `pytest` prints **288 passed**
- [ ] `/insights` renders with your data
- [ ] A credit-card payment does **not** appear in the category breakdown
- [ ] You set a budget and saw it in "Budget vs actual"
- [ ] Setting the same budget twice updated it rather than duplicating it
- [ ] You ran the benchmark and saw the SQL/Python gap for yourself
- [ ] The page looks right in both light and dark mode

---

## What's next — Milestone 7: AI Insights

The natural-language layer from your original brief:

> "Why did I spend more this month?" · "What subscriptions can I cancel?"
> "Show restaurants over $100." · "Forecast next month's card balance."

The interesting engineering problem is *not* prompting. It is that a language
model must never invent a number. The approach: the model chooses which
analytics function to call and with what arguments; the SQL built in this
milestone computes the actual figures; the model only phrases the answer.
Every number in a response will be traceable to a query — and we will test
that it cannot fabricate one.
