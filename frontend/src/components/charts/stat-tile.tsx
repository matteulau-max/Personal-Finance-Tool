import type { ReactNode } from "react";

/**
 * A stat tile: one number, optionally with context beneath it.
 *
 * Deliberately NOT a chart. A single current value plotted as a one-bar bar
 * chart is more ink for less information — the number itself is the clearest
 * possible encoding of one number.
 */
export function StatTile({
  label,
  value,
  detail,
  tone = "neutral",
}: {
  label: string;
  value: string;
  detail?: ReactNode;
  tone?: "neutral" | "good" | "critical";
}) {
  // Tone is a hint, never the only signal. Every tile states its meaning in
  // words underneath, so colour is decoration rather than information.
  const toneClass =
    tone === "good"
      ? "text-green-600 dark:text-green-400"
      : tone === "critical"
        ? "text-red-600 dark:text-red-400"
        : "";

  return (
    <div className="flex flex-col gap-1 rounded-lg border border-zinc-200 p-4 dark:border-zinc-800">
      <p className="text-xs font-medium uppercase tracking-wide text-zinc-500">
        {label}
      </p>
      <p className={`text-2xl font-semibold tabular-nums ${toneClass}`}>{value}</p>
      {detail && <p className="text-xs text-zinc-500">{detail}</p>}
    </div>
  );
}

/**
 * The dashboard's headline number.
 *
 * One figure gets to be large. If everything is a hero, nothing is.
 */
export function HeroFigure({
  label,
  value,
  detail,
}: {
  label: string;
  value: string;
  detail?: ReactNode;
}) {
  return (
    <div className="flex flex-col gap-1 rounded-lg border border-zinc-200 p-5 dark:border-zinc-800">
      <p className="text-xs font-medium uppercase tracking-wide text-zinc-500">
        {label}
      </p>
      <p className="text-4xl font-semibold tabular-nums">{value}</p>
      {detail && <p className="text-xs text-zinc-500">{detail}</p>}
    </div>
  );
}
