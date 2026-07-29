import {
  ChartFrame,
  EmptyChart,
  Legend,
  SERIES_1,
  SERIES_2,
  formatCurrency,
  niceTicks,
} from "./primitives";

export type MonthlyPoint = {
  period_start: string;
  spending: string;
  income: string;
};

/**
 * Spending vs income per month — grouped columns.
 *
 * WHY GROUPED COLUMNS and not a dual-axis line chart: the two series share a
 * unit (dollars), so they belong on ONE axis. A second y-scale would let the
 * chart invent a relationship between them that is not in the data — the most
 * common and most misleading charting mistake there is.
 *
 * Two series, so a legend is mandatory. Values are NOT labelled on every
 * column: twenty-four numbers would be unreadable and would go unread. The
 * axis carries the magnitudes; the per-column tooltip carries the exact
 * figures.
 */
export function CashFlowChart({ data }: { data: MonthlyPoint[] }) {
  if (data.length === 0) {
    return (
      <ChartFrame title="Cash flow" subtitle="Spending vs income by month">
        <EmptyChart message="No transactions yet." />
      </ChartFrame>
    );
  }

  const width = 720;
  const height = 240;
  const padding = { top: 12, right: 12, bottom: 28, left: 56 };
  const plotWidth = width - padding.left - padding.right;
  const plotHeight = height - padding.top - padding.bottom;

  const values = data.flatMap((d) => [Number(d.spending), Number(d.income)]);
  const max = Math.max(...values, 1);
  const ticks = niceTicks(max);
  const scaleMax = ticks[ticks.length - 1] || max;

  const bandWidth = plotWidth / data.length;
  // Bars are capped at 24px and never fill their slot -- the leftover band is
  // air, which is what stops a chart looking like a solid block of ink.
  const barWidth = Math.min(18, (bandWidth - 8) / 2);

  const y = (value: number) => padding.top + plotHeight - (value / scaleMax) * plotHeight;

  return (
    <ChartFrame
      title="Cash flow"
      subtitle="Spending vs income by month"
      legend={
        <Legend
          items={[
            { label: "Spending", color: SERIES_1 },
            { label: "Income", color: SERIES_2 },
          ]}
        />
      }
    >
      <svg
        viewBox={`0 0 ${width} ${height}`}
        className="h-auto w-full"
        role="img"
        aria-label="Monthly spending and income"
      >
        {ticks.map((tick) => (
          <g key={tick}>
            {/* Hairline, solid, one step off the surface. Recessive by design:
                gridlines orient the eye, they are not data. */}
            <line
              x1={padding.left}
              x2={width - padding.right}
              y1={y(tick)}
              y2={y(tick)}
              stroke="var(--grid)"
              strokeWidth={1}
            />
            <text
              x={padding.left - 8}
              y={y(tick) + 4}
              textAnchor="end"
              className="fill-zinc-500 text-[10px]"
            >
              {formatCurrency(tick)}
            </text>
          </g>
        ))}

        {data.map((point, index) => {
          const spending = Number(point.spending);
          const income = Number(point.income);
          const bandStart = padding.left + index * bandWidth;
          // 2px surface gap between the two touching bars.
          const groupStart = bandStart + (bandWidth - barWidth * 2 - 2) / 2;
          const month = new Date(point.period_start + "T00:00:00");

          return (
            <g key={point.period_start}>
              <rect
                x={groupStart}
                y={y(spending)}
                width={barWidth}
                height={Math.max(padding.top + plotHeight - y(spending), 0)}
                fill={SERIES_1}
                // Rounded on the data end, square at the baseline.
                rx={3}
              >
                <title>
                  {month.toLocaleDateString("en-US", { month: "long", year: "numeric" })}
                  {": "}
                  {formatCurrency(spending)} spent
                </title>
              </rect>
              <rect
                x={groupStart + barWidth + 2}
                y={y(income)}
                width={barWidth}
                height={Math.max(padding.top + plotHeight - y(income), 0)}
                fill={SERIES_2}
                rx={3}
              >
                <title>
                  {month.toLocaleDateString("en-US", { month: "long", year: "numeric" })}
                  {": "}
                  {formatCurrency(income)} in
                </title>
              </rect>
              <text
                x={bandStart + bandWidth / 2}
                y={height - 10}
                textAnchor="middle"
                className="fill-zinc-500 text-[10px]"
              >
                {month.toLocaleDateString("en-US", { month: "short" })}
              </text>
            </g>
          );
        })}
      </svg>
    </ChartFrame>
  );
}
