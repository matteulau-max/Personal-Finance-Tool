import Link from "next/link";

import { CashFlowChart } from "@/components/charts/cash-flow-chart";
import { MainNav } from "@/components/main-nav";
import { getMonthly } from "@/lib/api";
import { clerkEnabled } from "@/lib/clerk";
import { money, monthLabel, percent } from "@/lib/format";
import { requireSignedIn } from "@/lib/require-signed-in";

/**
 * Cash flow, month by month.
 *
 * The insights page already plots this series, so a second chart would be
 * decoration. What is missing there is the arithmetic: exact figures per
 * month, and totals across the window, so the numbers can be reconciled
 * against a bank statement. A chart shows shape; reconciliation needs digits.
 *
 * The current month is included but marked, because it is incomplete and
 * comparing a third of July against all of June is the single easiest way to
 * conclude your spending has collapsed.
 */

const WINDOWS = [6, 12, 24] as const;
const DEFAULT_MONTHS = 12;

function parseMonths(value: string | string[] | undefined): number {
  // A hand-edited URL is untrusted input like any other. Anything that is not
  // one of the offered windows falls back rather than reaching the API, which
  // would reject it with a 422 and show the user a request error for what is
  // really a typo.
  const raw = Array.isArray(value) ? value[0] : value;
  const parsed = Number(raw);
  return WINDOWS.includes(parsed as (typeof WINDOWS)[number])
    ? parsed
    : DEFAULT_MONTHS;
}

function Shell({ children }: { children: React.ReactNode }) {
  return (
    <main className="mx-auto flex min-h-screen w-full max-w-5xl flex-col gap-6 px-6 py-12">
      {children}
    </main>
  );
}

export default async function CashFlowPage({
  searchParams,
}: {
  // A promise in this version of Next.js -- see
  // node_modules/next/dist/docs/01-app/03-api-reference/03-file-conventions/page.md
  searchParams: Promise<{ [key: string]: string | string[] | undefined }>;
}) {
  if (!clerkEnabled) {
    return (
      <Shell>
        <h1 className="text-2xl font-semibold tracking-tight">Cash flow</h1>
        <p className="rounded border border-blue-300 bg-blue-50 p-3 text-sm text-blue-900 dark:border-blue-900 dark:bg-blue-950 dark:text-blue-200">
          Authentication is not configured. See{" "}
          <code className="font-mono">docs/milestone-03-auth.md</code>.
        </p>
      </Shell>
    );
  }

  // As with every other page: the auth check lives here, not in a route
  // matcher. See src/lib/require-signed-in.ts.
  await requireSignedIn();

  const months = parseMonths((await searchParams).months);
  const monthly = await getMonthly(months);

  if (!monthly.ok) {
    return (
      <Shell>
        <h1 className="text-2xl font-semibold tracking-tight">Cash flow</h1>
        <p className="rounded border border-amber-300 bg-amber-50 p-3 text-sm text-amber-900 dark:border-amber-900 dark:bg-amber-950 dark:text-amber-200">
          {monthly.error}
        </p>
      </Shell>
    );
  }

  // Newest first in the table: the month you care about is almost always the
  // most recent one, and it should not require scrolling to reach.
  const rows = [...monthly.data].reverse();

  const totals = monthly.data.reduce(
    (acc, month) => ({
      income: acc.income + Number(month.income),
      spending: acc.spending + Number(month.spending),
      net: acc.net + Number(month.net),
    }),
    { income: 0, spending: 0, net: 0 },
  );

  // Averages exclude the current month for the same reason it is marked in
  // the table: a partial month drags every average down and makes the figure
  // quietly wrong rather than visibly incomplete.
  const complete = monthly.data.slice(0, -1);
  const averageSpending =
    complete.length > 0
      ? complete.reduce((sum, m) => sum + Number(m.spending), 0) / complete.length
      : null;

  const currentMonthStart = monthly.data.at(-1)?.period_start;

  return (
    <Shell>
      <header className="flex flex-wrap items-start justify-between gap-4">
        <div>
          <h1 className="text-2xl font-semibold tracking-tight">Cash flow</h1>
          <p className="text-sm text-zinc-600 dark:text-zinc-400">
            Income against spending, {months} months
          </p>
        </div>
        <MainNav current="/cashflow" />
      </header>

      <div className="flex items-center gap-2 text-sm">
        <span className="text-zinc-500">Window:</span>
        {WINDOWS.map((option) =>
          option === months ? (
            <span
              key={option}
              aria-current="true"
              className="rounded bg-zinc-900 px-2 py-1 text-xs font-medium text-white dark:bg-zinc-100 dark:text-zinc-900"
            >
              {option} months
            </span>
          ) : (
            <Link
              key={option}
              href={`/cashflow?months=${option}`}
              className="rounded border border-zinc-300 px-2 py-1 text-xs hover:bg-zinc-100 dark:border-zinc-700 dark:hover:bg-zinc-900"
            >
              {option} months
            </Link>
          ),
        )}
      </div>

      {monthly.data.length === 0 ? (
        <p className="rounded-lg border border-zinc-200 p-8 text-center text-sm text-zinc-500 dark:border-zinc-800">
          No transactions yet. Connect a bank and sync to see cash flow.
        </p>
      ) : (
        <>
          <CashFlowChart data={monthly.data} />

          <section
            aria-labelledby="totals-heading"
            className="grid grid-cols-2 gap-3 md:grid-cols-4"
          >
            <h2 id="totals-heading" className="sr-only">
              Totals across the window
            </h2>
            {[
              { label: "Total income", value: money(totals.income) },
              { label: "Total spending", value: money(totals.spending) },
              {
                label: "Net",
                value: money(totals.net),
                tone:
                  totals.net < 0
                    ? "text-red-600 dark:text-red-400"
                    : "text-green-600 dark:text-green-400",
              },
              {
                label: "Average month",
                value: averageSpending === null ? "—" : money(averageSpending),
                detail: "Spending, complete months only",
              },
            ].map((tile) => (
              <div
                key={tile.label}
                className="flex flex-col gap-1 rounded-lg border border-zinc-200 p-4 dark:border-zinc-800"
              >
                <p className="text-xs font-medium uppercase tracking-wide text-zinc-500">
                  {tile.label}
                </p>
                <p
                  className={`text-2xl font-semibold tabular-nums ${tile.tone ?? ""}`}
                >
                  {tile.value}
                </p>
                {tile.detail && (
                  <p className="text-xs text-zinc-500">{tile.detail}</p>
                )}
              </div>
            ))}
          </section>

          <section
            aria-labelledby="months-heading"
            className="rounded-lg border border-zinc-200 p-5 dark:border-zinc-800"
          >
            <h2
              id="months-heading"
              className="mb-1 text-sm font-medium uppercase tracking-wide text-zinc-500"
            >
              By month
            </h2>
            <p className="mb-3 text-xs text-zinc-500">
              Transfers between your own accounts are excluded, and income is
              kept separate rather than netted against spending.
            </p>

            <div className="overflow-x-auto">
              <table className="w-full text-left text-sm">
                <thead className="text-xs uppercase tracking-wide text-zinc-500">
                  <tr>
                    <th className="py-1 pr-4 font-medium">Month</th>
                    <th className="py-1 pr-4 text-right font-medium">Income</th>
                    <th className="py-1 pr-4 text-right font-medium">
                      Spending
                    </th>
                    <th className="py-1 pr-4 text-right font-medium">Net</th>
                    <th className="py-1 text-right font-medium">Saved</th>
                  </tr>
                </thead>
                <tbody className="divide-y divide-zinc-200 dark:divide-zinc-800">
                  {rows.map((month) => {
                    const net = Number(month.net);
                    const isCurrent = month.period_start === currentMonthStart;

                    return (
                      <tr key={month.period_start}>
                        <td className="py-2 pr-4">
                          {monthLabel(month.period_start)}
                          {isCurrent && (
                            // Stated in words, not implied by styling: a
                            // partial month is not comparable to the rows
                            // above it, and the reader has to know that to
                            // read the column correctly.
                            <span className="ml-2 text-xs text-zinc-500">
                              so far
                            </span>
                          )}
                        </td>
                        <td className="py-2 pr-4 text-right font-mono tabular-nums">
                          {money(month.income, { cents: true })}
                        </td>
                        <td className="py-2 pr-4 text-right font-mono tabular-nums">
                          {money(month.spending, { cents: true })}
                        </td>
                        <td
                          className={`py-2 pr-4 text-right font-mono tabular-nums ${
                            net < 0
                              ? "text-red-600 dark:text-red-400"
                              : "text-green-600 dark:text-green-400"
                          }`}
                        >
                          {/* An explicit sign, so the meaning does not rest
                              on colour alone. */}
                          {net < 0 ? "−" : "+"}
                          {money(Math.abs(net), { cents: true })}
                        </td>
                        <td className="py-2 text-right tabular-nums text-zinc-500">
                          {percent(month.savings_rate)}
                        </td>
                      </tr>
                    );
                  })}
                </tbody>
              </table>
            </div>
          </section>
        </>
      )}
    </Shell>
  );
}
