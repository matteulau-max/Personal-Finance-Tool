/**
 * Display formatting for money, percentages and dates.
 *
 * Centralised because the alternative is a private `money()` in every page,
 * and the subtle parts below are exactly the ones that get retyped slightly
 * differently each time.
 */

/**
 * Format an amount that arrived from the API as a string.
 *
 * The API sends amounts as STRINGS deliberately: JSON numbers are IEEE
 * doubles, so parsing "1234.56" into a JavaScript number reintroduces the
 * float imprecision the backend went to some trouble to avoid in PostgreSQL.
 * Converting here, at the point of display, means no arithmetic ever happens
 * in a lossy type.
 *
 * A value that will not parse is returned unchanged rather than shown as NaN
 * -- if the backend ever sends something unexpected, showing it is more
 * diagnosable than hiding it behind a dash.
 */
export function money(
  amount: string | number | null | undefined,
  { currency = "USD", cents = false, fallback = "—" } = {},
): string {
  if (amount === null || amount === undefined || amount === "") return fallback;

  const value = Number(amount);
  if (Number.isNaN(value)) return String(amount);

  return new Intl.NumberFormat("en-US", {
    style: "currency",
    currency,
    maximumFractionDigits: cents ? 2 : 0,
    minimumFractionDigits: cents ? 2 : 0,
  }).format(value);
}

/** A fraction (0.42) as a percentage ("42%"). */
export function percent(
  value: string | number | null | undefined,
  { fallback = "—", digits = 0 } = {},
): string {
  if (value === null || value === undefined || value === "") return fallback;

  const parsed = Number(value);
  if (Number.isNaN(parsed)) return fallback;

  return `${(parsed * 100).toFixed(digits)}%`;
}

/**
 * Parse a plain `YYYY-MM-DD` from the API as a LOCAL date.
 *
 * `new Date("2026-07-31")` is parsed as UTC midnight, which in any negative
 * UTC offset displays as the 30th. Appending a time makes it local midnight
 * instead. This is why a transaction dated the 1st can otherwise appear in
 * the previous month for anyone in the Americas.
 */
export function parseApiDate(value: string): Date {
  return new Date(`${value}T00:00:00`);
}

/** A `YYYY-MM-DD` as a short local date, e.g. "31 Jul 2026". */
export function shortDate(value: string): string {
  return parseApiDate(value).toLocaleDateString(undefined, {
    day: "numeric",
    month: "short",
    year: "numeric",
  });
}

/** The month a `YYYY-MM-DD` falls in, e.g. "July 2026". */
export function monthLabel(value: string): string {
  return parseApiDate(value).toLocaleDateString(undefined, {
    month: "long",
    year: "numeric",
  });
}
