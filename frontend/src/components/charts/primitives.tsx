/**
 * Chart primitives, as plain inline SVG.
 *
 * No charting library. Three reasons:
 *
 *   1. These render inside Server Components, so the SVG is in the initial
 *      HTML. A client-side charting library would ship ~100KB of JavaScript
 *      and draw nothing until it loaded.
 *   2. Every mark spec below is deliberate (see the comments). A library's
 *      defaults would have to be fought.
 *   3. Zero dependencies to keep patched.
 *
 * Mark specs applied throughout, from the project's data-viz rules:
 *   - bars capped at 24px thick, rounded on the data end only
 *   - 2px lines, markers >= 8px diameter with a 2px surface ring
 *   - area fills at ~10% opacity (a wash, never a saturated block)
 *   - hairline solid gridlines, one step off the surface, recessive
 *   - a 2px surface gap between touching marks
 *   - text wears text tokens, never the series color
 */

import type { ReactNode } from "react";

export const SERIES_1 = "var(--series-1)";
export const SERIES_2 = "var(--series-2)";

export function formatCurrency(value: number, maximumFractionDigits = 0): string {
  return new Intl.NumberFormat("en-US", {
    style: "currency",
    currency: "USD",
    maximumFractionDigits,
  }).format(value);
}

/** Axis ticks land on clean numbers, never on raw data maxima. */
export function niceTicks(max: number, count = 4): number[] {
  if (max <= 0) return [0];

  const rough = max / count;
  const magnitude = 10 ** Math.floor(Math.log10(rough));
  const normalized = rough / magnitude;
  const step =
    (normalized >= 5 ? 10 : normalized >= 2 ? 5 : normalized >= 1 ? 2 : 1) *
    magnitude;

  const ticks: number[] = [];
  for (let value = 0; value <= max + step * 0.001; value += step) {
    ticks.push(value);
  }
  return ticks;
}

export function ChartFrame({
  title,
  subtitle,
  children,
  legend,
}: {
  title: string;
  subtitle?: string;
  children: ReactNode;
  legend?: ReactNode;
}) {
  return (
    <figure className="viz-root m-0 flex flex-col gap-3 rounded-lg border border-zinc-200 p-5 dark:border-zinc-800">
      <figcaption className="flex flex-wrap items-baseline justify-between gap-2">
        <div>
          <h3 className="text-sm font-medium uppercase tracking-wide text-zinc-500">
            {title}
          </h3>
          {subtitle && (
            <p className="text-xs text-zinc-500">{subtitle}</p>
          )}
        </div>
        {legend}
      </figcaption>
      {children}
    </figure>
  );
}

/**
 * A legend is always present for two or more series.
 *
 * Colour-matching alone is not an identity channel: it fails for colourblind
 * readers, in print, and in forced-colors mode. A single-series chart needs no
 * legend -- the title already says what is plotted, and a one-swatch box just
 * restates it.
 */
export function Legend({ items }: { items: { label: string; color: string }[] }) {
  return (
    <ul className="flex flex-wrap gap-3 text-xs text-zinc-600 dark:text-zinc-400">
      {items.map((item) => (
        <li key={item.label} className="flex items-center gap-1.5">
          <span
            aria-hidden="true"
            className="inline-block h-2.5 w-2.5 rounded-sm"
            style={{ background: item.color }}
          />
          {item.label}
        </li>
      ))}
    </ul>
  );
}

export function EmptyChart({ message }: { message: string }) {
  return (
    <p className="py-8 text-center text-sm text-zinc-500">{message}</p>
  );
}
