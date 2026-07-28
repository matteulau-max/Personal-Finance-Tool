import { ChartFrame, EmptyChart, SERIES_1, formatCurrency } from "./primitives";

export type BudgetRow = {
  category_id: string;
  category_name: string;
  budgeted: string;
  actual: string;
  remaining: string;
  used_fraction: string | null;
};

/**
 * Budget vs actual — a meter per category.
 *
 * A METER, not a chart. Each row is one ratio against one limit, which is
 * exactly the case where a chart adds nothing: a two-slice pie or a one-bar
 * bar chart per category would be more ink for less information.
 *
 * Over-budget rows use the CRITICAL STATUS COLOUR, which is reserved and never
 * reused as a series colour. It also never carries the meaning alone — the
 * row is labelled "over by $X" in text, because colour alone fails for
 * colourblind readers, in print, and in forced-colors mode.
 */
export function BudgetMeters({ rows }: { rows: BudgetRow[] }) {
  if (rows.length === 0) {
    return (
      <ChartFrame title="Budget vs actual" subtitle="This month">
        <EmptyChart message="No budgets set for this month yet." />
      </ChartFrame>
    );
  }

  return (
    <ChartFrame title="Budget vs actual" subtitle="This month">
      <ul className="flex flex-col gap-3">
        {rows.map((row) => {
          const budgeted = Number(row.budgeted);
          const actual = Number(row.actual);
          const remaining = Number(row.remaining);
          const fraction = budgeted > 0 ? actual / budgeted : 0;
          const isOver = remaining < 0;

          return (
            <li key={row.category_id} className="flex flex-col gap-1">
              <div className="flex items-baseline justify-between gap-3 text-xs">
                <span className="truncate text-zinc-700 dark:text-zinc-300">
                  {row.category_name}
                </span>
                <span className="shrink-0 font-mono tabular-nums text-zinc-600 dark:text-zinc-400">
                  {formatCurrency(actual)} / {formatCurrency(budgeted)}
                </span>
              </div>

              <div
                className="h-2 w-full overflow-hidden rounded-sm"
                style={{ background: "var(--grid)" }}
                role="img"
                aria-label={`${row.category_name}: ${formatCurrency(actual)} spent of ${formatCurrency(budgeted)} budgeted`}
              >
                <div
                  className="h-full rounded-sm"
                  style={{
                    // Capped at 100% so an overspend does not overflow the
                    // track; the text below states the overage precisely.
                    width: `${Math.min(Math.max(fraction, 0), 1) * 100}%`,
                    background: isOver ? "var(--critical)" : SERIES_1,
                  }}
                />
              </div>

              <p className="text-[11px] text-zinc-500">
                {isOver
                  ? `Over by ${formatCurrency(Math.abs(remaining))}`
                  : `${formatCurrency(remaining)} left`}
              </p>
            </li>
          );
        })}
      </ul>
    </ChartFrame>
  );
}
