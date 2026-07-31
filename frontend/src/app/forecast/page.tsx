import { MainNav } from "@/components/main-nav";
import { getForecast } from "@/lib/api";
import { clerkEnabled } from "@/lib/clerk";
import { money, monthLabel, shortDate } from "@/lib/format";
import { requireSignedIn } from "@/lib/require-signed-in";

/**
 * Next month's projected spending.
 *
 * The projection is the mean of recent complete months. It is not a model: no
 * trend, no seasonality, no confidence interval, and no awareness that
 * December exists. The backend chose that deliberately -- on a personal
 * finance dashboard, unearned authority is the more expensive error -- and
 * this page is where that choice either survives or is quietly undone.
 *
 * So the page shows its work. The months being averaged are listed next to
 * the figure, the count is stated in words, and the arithmetic is checkable
 * by hand. A single large number with no context would look like a
 * prediction, and would be believed like one.
 */

function Shell({ children }: { children: React.ReactNode }) {
  return (
    <main className="mx-auto flex min-h-screen w-full max-w-4xl flex-col gap-6 px-6 py-12">
      {children}
    </main>
  );
}

function nextMonthLabel(): string {
  const now = new Date();
  return new Date(now.getFullYear(), now.getMonth() + 1, 1).toLocaleDateString(
    undefined,
    { month: "long", year: "numeric" },
  );
}

export default async function ForecastPage() {
  if (!clerkEnabled) {
    return (
      <Shell>
        <h1 className="text-2xl font-semibold tracking-tight">Forecast</h1>
        <p className="rounded border border-blue-300 bg-blue-50 p-3 text-sm text-blue-900 dark:border-blue-900 dark:bg-blue-950 dark:text-blue-200">
          Authentication is not configured. See{" "}
          <code className="font-mono">docs/milestone-03-auth.md</code>.
        </p>
      </Shell>
    );
  }

  await requireSignedIn();

  const forecast = await getForecast(3);

  if (!forecast.ok) {
    return (
      <Shell>
        <h1 className="text-2xl font-semibold tracking-tight">Forecast</h1>
        <p className="rounded border border-amber-300 bg-amber-50 p-3 text-sm text-amber-900 dark:border-amber-900 dark:bg-amber-950 dark:text-amber-200">
          {forecast.error}
        </p>
      </Shell>
    );
  }

  const data = forecast.data;
  const projected = Number(data.projected_spending);
  const committed = Number(data.recurring_committed);

  // What fraction of the projection is already spoken for. Only meaningful
  // when there is a projection to divide into; a brand-new account has none.
  const committedShare = projected > 0 ? committed / projected : null;

  return (
    <Shell>
      <header className="flex flex-wrap items-start justify-between gap-4">
        <div>
          <h1 className="text-2xl font-semibold tracking-tight">Forecast</h1>
          <p className="text-sm text-zinc-600 dark:text-zinc-400">
            Projected spending for {nextMonthLabel()}
          </p>
        </div>
        <MainNav current="/forecast" />
      </header>

      {data.months_used === 0 ? (
        <section className="rounded-lg border border-zinc-200 p-8 text-center dark:border-zinc-800">
          <p className="text-sm text-zinc-600 dark:text-zinc-400">
            Nothing to project from yet.
          </p>
          <p className="mt-2 text-xs text-zinc-500">
            A forecast needs at least one complete calendar month of
            transactions. Connect a bank and sync, then check back after the
            month turns over.
          </p>
        </section>
      ) : (
        <>
          <section className="flex flex-col gap-1 rounded-lg border border-zinc-200 p-5 dark:border-zinc-800">
            <p className="text-xs font-medium uppercase tracking-wide text-zinc-500">
              Projected spending
            </p>
            <p className="text-4xl font-semibold tabular-nums">
              {money(projected)}
            </p>
            <p className="text-xs text-zinc-500">
              The average of your last {data.months_used}{" "}
              {data.months_used === 1 ? "complete month" : "complete months"}.
            </p>
          </section>

          {/* Saying plainly what the number is NOT. This paragraph is the
              honest half of showing a projection at all: without it, an
              average of three months reads as a forecast produced by
              something cleverer than division. */}
          <p className="rounded border border-zinc-200 bg-zinc-50 p-3 text-xs leading-relaxed text-zinc-600 dark:border-zinc-800 dark:bg-zinc-900 dark:text-zinc-400">
            This is an average, not a model. It carries no trend and no
            seasonality — it does not know that December is expensive, and a
            holiday last month is projected straight into next month. Read it
            as &ldquo;a month like the recent ones&rdquo;, and check it against
            the months below.
          </p>

          <section
            aria-labelledby="basis-heading"
            className="rounded-lg border border-zinc-200 p-5 dark:border-zinc-800"
          >
            <h2
              id="basis-heading"
              className="mb-1 text-sm font-medium uppercase tracking-wide text-zinc-500"
            >
              What it is averaging
            </h2>
            <p className="mb-3 text-xs text-zinc-500">
              The current month is excluded — on the 3rd it is a fraction of a
              month and would drag the average down.
            </p>

            <ul className="flex flex-col divide-y divide-zinc-200 text-sm dark:divide-zinc-800">
              {data.basis.map((month) => (
                <li
                  key={month.period_start}
                  className="flex items-baseline justify-between gap-4 py-2"
                >
                  <span>{monthLabel(month.period_start)}</span>
                  <span className="font-mono tabular-nums">
                    {money(month.spending, { cents: true })}
                  </span>
                </li>
              ))}
              <li className="flex items-baseline justify-between gap-4 py-2 font-medium">
                <span>Average</span>
                <span className="font-mono tabular-nums">
                  {money(projected, { cents: true })}
                </span>
              </li>
            </ul>
          </section>

          <section
            aria-labelledby="committed-heading"
            className="rounded-lg border border-zinc-200 p-5 dark:border-zinc-800"
          >
            <h2
              id="committed-heading"
              className="mb-1 text-sm font-medium uppercase tracking-wide text-zinc-500"
            >
              Already committed
            </h2>
            <p className="mb-3 text-xs text-zinc-500">
              Detected recurring charges. These are the part of next month you
              have effectively already decided — the difference between what
              you will probably spend and what you will spend unless you
              cancel something.
            </p>

            {data.recurring.length === 0 ? (
              <p className="py-4 text-sm text-zinc-500">
                No recurring charges detected yet. This needs at least three
                charges from the same merchant.
              </p>
            ) : (
              <>
                <p className="mb-3 text-2xl font-semibold tabular-nums">
                  {money(committed)}
                  {committedShare !== null && (
                    <span className="ml-2 text-sm font-normal text-zinc-500">
                      {(committedShare * 100).toFixed(0)}% of the projection
                    </span>
                  )}
                </p>

                <div className="overflow-x-auto">
                  <table className="w-full text-left text-sm">
                    <thead className="text-xs uppercase tracking-wide text-zinc-500">
                      <tr>
                        <th className="py-1 pr-4 font-medium">Merchant</th>
                        <th className="py-1 pr-4 text-right font-medium">
                          Typical
                        </th>
                        <th className="py-1 pr-4 font-medium">Every</th>
                        <th className="py-1 font-medium">Last seen</th>
                      </tr>
                    </thead>
                    <tbody className="divide-y divide-zinc-200 dark:divide-zinc-800">
                      {data.recurring.map((charge) => (
                        <tr key={`${charge.merchant_id}-${charge.last_seen}`}>
                          <td className="py-2 pr-4">{charge.merchant_name}</td>
                          <td className="py-2 pr-4 text-right font-mono tabular-nums">
                            {money(charge.typical_amount, { cents: true })}
                          </td>
                          <td className="py-2 pr-4 text-zinc-500">
                            ~{Math.round(charge.average_gap_days)} days
                          </td>
                          <td className="py-2 text-zinc-500">
                            {shortDate(charge.last_seen)}
                          </td>
                        </tr>
                      ))}
                    </tbody>
                  </table>
                </div>

                <p className="mt-3 text-xs text-zinc-500">
                  Detected from consistent amounts at regular intervals.
                  Suggestions to review, not conclusions — an irregular charge
                  that happens to land twice at the same price will show up
                  here too.
                </p>
              </>
            )}
          </section>
        </>
      )}
    </Shell>
  );
}
