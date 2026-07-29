import { ChartFrame, EmptyChart, SERIES_1, formatCurrency } from "./primitives";

export type BreakdownRow = {
  label: string;
  total: string;
  count: number;
};

/**
 * Spending broken down by category or merchant — horizontal bars.
 *
 * HORIZONTAL because category names are long words, not short dates. Vertical
 * columns would force the labels to rotate 45 degrees, which is measurably
 * slower to read.
 *
 * EVERY BAR IS THE SAME COLOUR, and that is deliberate. Shading them
 * darker-where-bigger is a common instinct and a mistake: categories have no
 * natural order, so a value ramp double-encodes bar length as hue, spends the
 * one free channel on information the chart already shows, and produces a set
 * of colours that fails the contrast and chroma checks by construction.
 *
 * One series, so no legend. Values ARE labelled here — there are at most ten
 * bars and the number is the point of the panel, which is exactly the case
 * where direct labels earn their space.
 */
export function BreakdownChart({
  title,
  subtitle,
  rows,
  emptyMessage,
}: {
  title: string;
  subtitle?: string;
  rows: BreakdownRow[];
  emptyMessage: string;
}) {
  if (rows.length === 0) {
    return (
      <ChartFrame title={title} subtitle={subtitle}>
        <EmptyChart message={emptyMessage} />
      </ChartFrame>
    );
  }

  const max = Math.max(...rows.map((row) => Number(row.total)), 1);

  return (
    <ChartFrame title={title} subtitle={subtitle}>
      <ul className="flex flex-col gap-2.5">
        {rows.map((row) => {
          const value = Number(row.total);
          const fraction = Math.max(value / max, 0);

          return (
            <li key={row.label} className="flex flex-col gap-1">
              <div className="flex items-baseline justify-between gap-3 text-xs">
                <span className="truncate text-zinc-700 dark:text-zinc-300">
                  {row.label}
                </span>
                {/* Text wears a text token, never the series colour. The bar
                    beside it already carries the identity. */}
                <span className="shrink-0 font-mono tabular-nums text-zinc-600 dark:text-zinc-400">
                  {formatCurrency(value)}
                  <span className="ml-1.5 text-zinc-400">
                    ({row.count})
                  </span>
                </span>
              </div>
              <div
                className="h-2 w-full overflow-hidden rounded-sm"
                style={{ background: "var(--grid)" }}
                role="img"
                aria-label={`${row.label}: ${formatCurrency(value)} across ${row.count} transactions`}
              >
                <div
                  className="h-full rounded-sm"
                  style={{
                    width: `${Math.max(fraction * 100, 1)}%`,
                    background: SERIES_1,
                  }}
                />
              </div>
            </li>
          );
        })}
      </ul>
    </ChartFrame>
  );
}
