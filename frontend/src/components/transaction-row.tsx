"use client";

import { useState, useTransition } from "react";

import { resetToOriginal, setCategory, setReviewed } from "@/app/transactions/actions";
import type { CategoryItem, TransactionItem } from "@/lib/api";

/**
 * Money is formatted from a STRING, never parsed into a number first.
 *
 * The API sends "1234.5600" deliberately: a JSON number is an IEEE double, so
 * parsing it reintroduces exactly the float imprecision we avoided in
 * PostgreSQL. `Number()` here is safe only because the result is immediately
 * formatted for display and never used in arithmetic.
 */
function formatMoney(amount: string, currency: string): string {
  const value = Number(amount);
  if (Number.isNaN(value)) return amount;

  return new Intl.NumberFormat("en-US", {
    style: "currency",
    currency,
    signDisplay: "never",
  }).format(value);
}

/** Explains WHERE a category came from, so the UI can justify itself. */
function sourceLabel(source: string): string | null {
  switch (source) {
    case "rule":
      return "set by a rule";
    case "heuristic":
      return "from this merchant";
    case "plaid":
      return "suggested by Plaid";
    default:
      return null;
  }
}

export function TransactionRow({
  transaction,
  categories,
}: {
  transaction: TransactionItem;
  categories: CategoryItem[];
}) {
  const [isPending, startTransition] = useTransition();
  const [applyToSimilar, setApplyToSimilar] = useState(false);
  const [error, setError] = useState<string | null>(null);

  // Positive means money LEFT the account (Plaid's convention, kept end to
  // end so no sign flip can be applied twice or zero times).
  const isOutflow = Number(transaction.amount) > 0;

  const run = (action: () => Promise<{ ok: boolean; error?: string }>) => {
    setError(null);
    startTransition(async () => {
      const result = await action();
      if (!result.ok) setError(result.error ?? "Something went wrong.");
    });
  };

  return (
    <li className="flex flex-col gap-2 py-3">
      <div className="flex flex-wrap items-start justify-between gap-3">
        <div className="min-w-0">
          <p className="truncate font-medium">
            {transaction.description}
            {transaction.is_user_modified && (
              <span
                className="ml-2 rounded bg-zinc-100 px-1.5 py-0.5 text-[10px] uppercase tracking-wide text-zinc-600 dark:bg-zinc-800 dark:text-zinc-400"
                title="You have edited this transaction. The bank's original is kept."
              >
                edited
              </span>
            )}
          </p>
          <p className="text-xs text-zinc-500">
            {transaction.date}
            {transaction.merchant ? ` · ${transaction.merchant.display_name}` : ""}
            {transaction.status === "pending" ? " · pending" : ""}
          </p>
        </div>

        <p
          className={`shrink-0 font-mono text-sm tabular-nums ${
            isOutflow ? "" : "text-green-600 dark:text-green-400"
          }`}
        >
          {isOutflow ? "" : "+"}
          {formatMoney(transaction.amount, transaction.currency_code)}
        </p>
      </div>

      <div className="flex flex-wrap items-center gap-2">
        <select
          aria-label="Category"
          disabled={isPending}
          value={transaction.category?.id ?? ""}
          onChange={(event) =>
            run(() =>
              setCategory(
                transaction.id,
                event.target.value || null,
                applyToSimilar,
              ),
            )
          }
          className="rounded border border-zinc-300 bg-transparent px-2 py-1 text-xs dark:border-zinc-700"
        >
          <option value="">Uncategorized</option>
          {categories.map((category) => (
            <option key={category.id} value={category.id}>
              {category.name}
            </option>
          ))}
        </select>

        <label className="flex items-center gap-1 text-xs text-zinc-500">
          <input
            type="checkbox"
            checked={applyToSimilar}
            onChange={(event) => setApplyToSimilar(event.target.checked)}
          />
          {/* The explicit-learning decision, surfaced. Only the user knows
              whether this correction is a one-off or a pattern. */}
          apply to all from this merchant
        </label>

        {transaction.category && sourceLabel(transaction.category_source) && (
          <span className="text-xs text-zinc-400">
            {sourceLabel(transaction.category_source)}
          </span>
        )}

        {transaction.tags.map((tag) => (
          <span
            key={tag.id}
            className="rounded-full border border-zinc-300 px-2 py-0.5 text-[11px] dark:border-zinc-700"
          >
            {tag.name}
          </span>
        ))}

        <button
          type="button"
          disabled={isPending}
          onClick={() => run(() => setReviewed(transaction.id, !transaction.is_reviewed))}
          className="rounded border border-zinc-300 px-2 py-1 text-xs disabled:opacity-50 dark:border-zinc-700"
        >
          {transaction.is_reviewed ? "Reviewed ✓" : "Mark reviewed"}
        </button>

        {transaction.is_user_modified && (
          <button
            type="button"
            disabled={isPending}
            onClick={() => run(() => resetToOriginal(transaction.id))}
            title={`Bank's original: ${transaction.raw_name} · ${transaction.raw_amount}`}
            className="rounded border border-zinc-300 px-2 py-1 text-xs text-zinc-600 disabled:opacity-50 dark:border-zinc-700 dark:text-zinc-400"
          >
            Reset to original
          </button>
        )}
      </div>

      {error && (
        <p className="text-xs text-amber-700 dark:text-amber-300">{error}</p>
      )}
    </li>
  );
}
