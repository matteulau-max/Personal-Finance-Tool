import Link from "next/link";

import { BreakdownChart } from "@/components/charts/breakdown-chart";
import { BudgetMeters } from "@/components/charts/budget-meters";
import { CashFlowChart } from "@/components/charts/cash-flow-chart";
import { NetWorthChart } from "@/components/charts/net-worth-chart";
import { HeroFigure, StatTile } from "@/components/charts/stat-tile";
import { AskPanel } from "@/components/ask-panel";
import { getInsightSuggestions, getOverview } from "@/lib/api";
import { clerkEnabled } from "@/lib/clerk";
import { requireSignedIn } from "@/lib/require-signed-in";

function money(value: string | null, fallback = "—"): string {
  if (value === null) return fallback;
  const parsed = Number(value);
  if (Number.isNaN(parsed)) return fallback;

  return new Intl.NumberFormat("en-US", {
    style: "currency",
    currency: "USD",
    maximumFractionDigits: 0,
  }).format(parsed);
}

function percent(value: string | null): string {
  if (value === null) return "—";
  const parsed = Number(value);
  if (Number.isNaN(parsed)) return "—";
  return `${(parsed * 100).toFixed(0)}%`;
}

export default async function InsightsPage() {
  if (!clerkEnabled) {
    return (
      <main className="mx-auto flex min-h-screen max-w-2xl flex-col justify-center gap-4 px-6">
        <h1 className="text-2xl font-semibold tracking-tight">Insights</h1>
        <p className="rounded border border-blue-300 bg-blue-50 p-3 text-sm text-blue-900 dark:border-blue-900 dark:bg-blue-950 dark:text-blue-200">
          Authentication is not configured. See{" "}
          <code className="font-mono">docs/milestone-03-auth.md</code>.
        </p>
      </main>
    );
  }

  // As with /transactions: never covered by the route matcher. See
  // src/lib/require-signed-in.ts.
  await requireSignedIn();

  const [overview, suggestions] = await Promise.all([
    getOverview(12),
    getInsightSuggestions(),
  ]);

  if (!overview.ok) {
    return (
      <main className="mx-auto flex min-h-screen max-w-2xl flex-col justify-center gap-4 px-6">
        <h1 className="text-2xl font-semibold tracking-tight">Insights</h1>
        <p className="rounded border border-amber-300 bg-amber-50 p-3 text-sm text-amber-900 dark:border-amber-900 dark:bg-amber-950 dark:text-amber-200">
          {overview.error}
        </p>
      </main>
    );
  }

  const data = overview.data;

  return (
    <main className="mx-auto flex min-h-screen w-full max-w-5xl flex-col gap-6 px-6 py-12">
      <header className="flex flex-wrap items-center justify-between gap-4">
        <div>
          <h1 className="text-2xl font-semibold tracking-tight">Insights</h1>
          <p className="text-sm text-zinc-600 dark:text-zinc-400">
            As of {new Date(data.as_of + "T00:00:00").toLocaleDateString()}
          </p>
        </div>
        <nav className="flex gap-4 text-sm">
          <Link href="/dashboard" className="underline">
            Dashboard
          </Link>
          <Link href="/transactions" className="underline">
            Transactions
          </Link>
        </nav>
      </header>

      <AskPanel
        suggestions={
          suggestions.ok ? suggestions.data.map((item) => item.question) : []
        }
      />

      {/* One hero figure. If everything is a hero, nothing is. */}
      <HeroFigure
        label="Net worth"
        value={money(data.net_worth)}
        detail={`${money(data.liquid_balance)} liquid`}
      />

      <section
        aria-label="Key figures"
        className="grid grid-cols-2 gap-3 md:grid-cols-4"
      >
        <StatTile
          label="Spent this month"
          value={money(data.month_to_date_spending)}
          detail={`${money(data.rolling_30_day_spending)} in the last 30 days`}
        />
        <StatTile
          label="Income this month"
          value={money(data.month_to_date_income)}
          detail={`Savings rate ${percent(data.savings_rate)}`}
        />
        <StatTile
          label="Burn rate"
          value={money(data.burn_rate)}
          detail="Average net outflow, last 3 complete months"
        />
        <StatTile
          label="Cash runway"
          value={
            data.cash_runway_months === null
              ? "—"
              : `${Number(data.cash_runway_months).toFixed(1)} mo`
          }
          // "Not burning" rather than a huge number: if income exceeds
          // spending there is no runway to run out of.
          detail={
            data.cash_runway_months === null
              ? "Not currently burning cash"
              : "At the current burn rate"
          }
          tone={
            data.cash_runway_months !== null &&
            Number(data.cash_runway_months) < 3
              ? "critical"
              : "neutral"
          }
        />
      </section>

      <CashFlowChart data={data.monthly} />

      <NetWorthChart data={data.net_worth_series} />

      <div className="grid gap-6 md:grid-cols-2">
        <BreakdownChart
          title="Spending by category"
          subtitle="This month"
          emptyMessage="Nothing spent yet this month."
          rows={data.top_categories.map((c) => ({
            label: c.category_name,
            total: c.total,
            count: c.transaction_count,
          }))}
        />
        <BreakdownChart
          title="Top merchants"
          subtitle="This month"
          emptyMessage="No merchant activity yet this month."
          rows={data.top_merchants.map((m) => ({
            label: m.merchant_name,
            total: m.total,
            count: m.transaction_count,
          }))}
        />
      </div>

      <div className="grid gap-6 md:grid-cols-2">
        <BudgetMeters rows={data.budgets} />

        <figure className="viz-root m-0 flex flex-col gap-3 rounded-lg border border-zinc-200 p-5 dark:border-zinc-800">
          <figcaption>
            <h3 className="text-sm font-medium uppercase tracking-wide text-zinc-500">
              Biggest changes
            </h3>
            <p className="text-xs text-zinc-500">
              This month so far vs all of last month
            </p>
          </figcaption>

          {data.biggest_increases.length === 0 &&
          data.biggest_decreases.length === 0 ? (
            <p className="py-6 text-center text-sm text-zinc-500">
              Not enough history to compare yet.
            </p>
          ) : (
            <ul className="flex flex-col gap-2 text-xs">
              {[...data.biggest_increases, ...data.biggest_decreases].map(
                (change) => {
                  const delta = Number(change.change);
                  return (
                    <li
                      key={`${change.category_id ?? "none"}-${change.change}`}
                      className="flex items-baseline justify-between gap-3"
                    >
                      <span className="truncate text-zinc-700 dark:text-zinc-300">
                        {change.category_name}
                      </span>
                      {/* The sign is stated with an explicit + or -, not
                          carried by colour alone. */}
                      <span
                        className={`shrink-0 font-mono tabular-nums ${
                          delta > 0
                            ? "text-red-600 dark:text-red-400"
                            : "text-green-600 dark:text-green-400"
                        }`}
                      >
                        {delta > 0 ? "+" : "−"}
                        {money(String(Math.abs(delta)))}
                      </span>
                    </li>
                  );
                },
              )}
            </ul>
          )}
        </figure>
      </div>

      <section
        aria-labelledby="recurring-heading"
        className="rounded-lg border border-zinc-200 p-5 dark:border-zinc-800"
      >
        <h2
          id="recurring-heading"
          className="mb-1 text-sm font-medium uppercase tracking-wide text-zinc-500"
        >
          Recurring charges
        </h2>
        <p className="mb-3 text-xs text-zinc-500">
          Detected from consistent amounts at regular intervals. Suggestions to
          review, not conclusions.
        </p>

        {data.recurring.length === 0 ? (
          <p className="py-4 text-sm text-zinc-500">
            No recurring charges detected yet. This needs at least three charges
            from the same merchant.
          </p>
        ) : (
          // A table, not a chart: seven-plus rows of two unrelated measures
          // (amount and interval) is exactly what a table is for.
          <div className="overflow-x-auto">
            <table className="w-full text-left text-sm">
              <thead className="text-xs uppercase tracking-wide text-zinc-500">
                <tr>
                  <th className="py-1 pr-4 font-medium">Merchant</th>
                  <th className="py-1 pr-4 font-medium">Typical</th>
                  <th className="py-1 pr-4 font-medium">Every</th>
                  <th className="py-1 font-medium">Last seen</th>
                </tr>
              </thead>
              <tbody className="divide-y divide-zinc-200 dark:divide-zinc-800">
                {data.recurring.map((charge) => (
                  <tr key={`${charge.merchant_id}-${charge.last_seen}`}>
                    <td className="py-2 pr-4">{charge.merchant_name}</td>
                    <td className="py-2 pr-4 font-mono tabular-nums">
                      {money(charge.typical_amount, "—")}
                    </td>
                    <td className="py-2 pr-4 text-zinc-500">
                      ~{Math.round(charge.average_gap_days)} days
                    </td>
                    <td className="py-2 text-zinc-500">
                      {new Date(
                        charge.last_seen + "T00:00:00",
                      ).toLocaleDateString()}
                    </td>
                  </tr>
                ))}
              </tbody>
            </table>
          </div>
        )}
      </section>

      <section
        aria-labelledby="largest-heading"
        className="rounded-lg border border-zinc-200 p-5 dark:border-zinc-800"
      >
        <h2
          id="largest-heading"
          className="mb-3 text-sm font-medium uppercase tracking-wide text-zinc-500"
        >
          Largest purchases this month
        </h2>

        {data.largest_transactions.length === 0 ? (
          <p className="text-sm text-zinc-500">Nothing yet this month.</p>
        ) : (
          <ul className="flex flex-col divide-y divide-zinc-200 text-sm dark:divide-zinc-800">
            {data.largest_transactions.map((transaction) => (
              <li
                key={transaction.id}
                className="flex items-baseline justify-between gap-4 py-2"
              >
                <div className="min-w-0">
                  <p className="truncate">{transaction.description}</p>
                  <p className="text-xs text-zinc-500">
                    {new Date(
                      transaction.date + "T00:00:00",
                    ).toLocaleDateString()}
                    {transaction.category_name
                      ? ` · ${transaction.category_name}`
                      : ""}
                  </p>
                </div>
                <p className="shrink-0 font-mono tabular-nums">
                  {money(transaction.amount, "—")}
                </p>
              </li>
            ))}
          </ul>
        )}
      </section>
    </main>
  );
}
