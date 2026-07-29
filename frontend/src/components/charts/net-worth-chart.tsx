import {
  ChartFrame,
  EmptyChart,
  SERIES_1,
  formatCurrency,
  niceTicks,
} from "./primitives";

export type NetWorthPoint = {
  as_of: string;
  assets: string;
  liabilities: string;
  net_worth: string;
};

/**
 * Net worth over time — a single-series area chart.
 *
 * ONE series, so there is no legend: the title already says what is plotted,
 * and a one-swatch box would just restate it.
 *
 * The area is a 10% wash of the series hue rather than a saturated block. A
 * solid fill would dominate the panel and make the line — the part carrying
 * the actual shape — harder to follow.
 *
 * Only the final value is directly labelled. Labelling every point is the
 * fastest way to make a chart unreadable; the axis and tooltips carry the rest.
 */
export function NetWorthChart({ data }: { data: NetWorthPoint[] }) {
  if (data.length < 2) {
    return (
      <ChartFrame title="Net worth" subtitle="Assets minus liabilities">
        <EmptyChart message="Net worth history appears after a few syncs." />
      </ChartFrame>
    );
  }

  const width = 720;
  const height = 220;
  const padding = { top: 16, right: 64, bottom: 28, left: 60 };
  const plotWidth = width - padding.left - padding.right;
  const plotHeight = height - padding.top - padding.bottom;

  const values = data.map((d) => Number(d.net_worth));
  const max = Math.max(...values, 1);
  const ticks = niceTicks(max);
  const scaleMax = ticks[ticks.length - 1] || max;

  const x = (index: number) =>
    padding.left + (data.length === 1 ? 0 : (index / (data.length - 1)) * plotWidth);
  const y = (value: number) =>
    padding.top + plotHeight - (Math.max(value, 0) / scaleMax) * plotHeight;

  const line = values.map((value, i) => `${i === 0 ? "M" : "L"} ${x(i)} ${y(value)}`).join(" ");
  const area = `${line} L ${x(values.length - 1)} ${padding.top + plotHeight} L ${x(0)} ${
    padding.top + plotHeight
  } Z`;

  const last = values[values.length - 1];

  return (
    <ChartFrame title="Net worth" subtitle="Assets minus liabilities">
      <svg
        viewBox={`0 0 ${width} ${height}`}
        className="h-auto w-full"
        role="img"
        aria-label={`Net worth over time, currently ${formatCurrency(last)}`}
      >
        {ticks.map((tick) => (
          <g key={tick}>
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

        <path d={area} fill={SERIES_1} fillOpacity={0.1} />
        <path
          d={line}
          fill="none"
          stroke={SERIES_1}
          strokeWidth={2}
          strokeLinejoin="round"
          strokeLinecap="round"
        />

        {/* End marker: >= 8px across, with a 2px surface ring so it stays
            legible where it meets the line and the gridlines. */}
        <circle
          cx={x(values.length - 1)}
          cy={y(last)}
          r={4}
          fill={SERIES_1}
          stroke="var(--surface-1)"
          strokeWidth={2}
        />
        <text
          x={x(values.length - 1) + 10}
          y={y(last) + 4}
          className="fill-zinc-700 text-[11px] font-medium dark:fill-zinc-300"
        >
          {formatCurrency(last)}
        </text>

        {data.map((point, index) => (
          // Invisible hit targets, comfortably larger than the marks they
          // stand for. A 4px dot is far too small to hover reliably.
          <circle
            key={point.as_of}
            cx={x(index)}
            cy={y(Number(point.net_worth))}
            r={10}
            fill="transparent"
          >
            <title>
              {new Date(point.as_of + "T00:00:00").toLocaleDateString()}:{" "}
              {formatCurrency(Number(point.net_worth))}
              {" ("}
              {formatCurrency(Number(point.assets))} assets,{" "}
              {formatCurrency(Number(point.liabilities))} owed)
            </title>
          </circle>
        ))}

        <text
          x={padding.left}
          y={height - 10}
          className="fill-zinc-500 text-[10px]"
        >
          {new Date(data[0].as_of + "T00:00:00").toLocaleDateString()}
        </text>
        <text
          x={width - padding.right}
          y={height - 10}
          textAnchor="end"
          className="fill-zinc-500 text-[10px]"
        >
          {new Date(data[data.length - 1].as_of + "T00:00:00").toLocaleDateString()}
        </text>
      </svg>
    </ChartFrame>
  );
}
