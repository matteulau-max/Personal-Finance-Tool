import Link from "next/link";

import { MainNav } from "@/components/main-nav";

import { TransactionRow } from "@/components/transaction-row";
import { getCategories, getTags, getTransactions } from "@/lib/api";
import { clerkEnabled } from "@/lib/clerk";
import { requireSignedIn } from "@/lib/require-signed-in";

/**
 * The transactions page.
 *
 * Filters live in the URL as search params rather than in React state. That
 * is a deliberate choice: it makes a filtered view shareable, bookmarkable,
 * and survivable across a refresh, and it means the server can render the
 * correct list on the first request instead of flashing an unfiltered one.
 *
 * In Next.js 16, `searchParams` is a Promise and must be awaited.
 */
export default async function TransactionsPage({
  searchParams,
}: {
  searchParams: Promise<{ [key: string]: string | string[] | undefined }>;
}) {
  if (!clerkEnabled) {
    return (
      <main className="mx-auto flex min-h-screen max-w-2xl flex-col justify-center gap-4 px-6">
        <h1 className="text-2xl font-semibold tracking-tight">Transactions</h1>
        <p className="rounded border border-blue-300 bg-blue-50 p-3 text-sm text-blue-900 dark:border-blue-900 dark:bg-blue-950 dark:text-blue-200">
          Authentication is not configured. See{" "}
          <code className="font-mono">docs/milestone-03-auth.md</code>.
        </p>
      </main>
    );
  }

  // This page was never covered by the route matcher in proxy.ts, so a
  // signed-out visitor used to get "No active session." rendered as an
  // error. See src/lib/require-signed-in.ts.
  await requireSignedIn();

  const params = await searchParams;
  const asString = (value: string | string[] | undefined): string | undefined =>
    typeof value === "string" && value ? value : undefined;

  const page = Number(asString(params.page) ?? "1");
  const limit = 50;
  const offset = (Math.max(page, 1) - 1) * limit;

  const [transactions, categories, tags] = await Promise.all([
    getTransactions({
      search: asString(params.search),
      categoryId: asString(params.category),
      tagId: asString(params.tag),
      startDate: asString(params.start),
      endDate: asString(params.end),
      limit,
      offset,
    }),
    getCategories(),
    getTags(),
  ]);

  const categoryList = categories.ok ? categories.data : [];
  const tagList = tags.ok ? tags.data : [];

  const total = transactions.ok ? transactions.data.total : 0;
  const showing = transactions.ok ? transactions.data.items.length : 0;

  return (
    <main className="mx-auto flex min-h-screen w-full max-w-4xl flex-col gap-6 px-6 py-12">
      <header className="flex flex-wrap items-center justify-between gap-4">
        <h1 className="text-2xl font-semibold tracking-tight">Transactions</h1>
        <MainNav current="/transactions" />
      </header>

      {/*
        A plain GET form. No JavaScript required, the URL updates naturally,
        and the back button behaves the way users expect -- all of which you
        would have to rebuild by hand with a client-side filter.
      */}
      <form
        method="get"
        className="flex flex-wrap items-end gap-3 rounded-lg border border-zinc-200 p-4 dark:border-zinc-800"
      >
        <label className="flex flex-col gap-1 text-xs">
          Search
          <input
            type="search"
            name="search"
            defaultValue={asString(params.search) ?? ""}
            placeholder="coffee, rent, amazon…"
            className="rounded border border-zinc-300 bg-transparent px-2 py-1 text-sm dark:border-zinc-700"
          />
        </label>

        <label className="flex flex-col gap-1 text-xs">
          Category
          <select
            name="category"
            defaultValue={asString(params.category) ?? ""}
            className="rounded border border-zinc-300 bg-transparent px-2 py-1 text-sm dark:border-zinc-700"
          >
            <option value="">All</option>
            {categoryList.map((category) => (
              <option key={category.id} value={category.id}>
                {category.name}
              </option>
            ))}
          </select>
        </label>

        <label className="flex flex-col gap-1 text-xs">
          Tag
          <select
            name="tag"
            defaultValue={asString(params.tag) ?? ""}
            className="rounded border border-zinc-300 bg-transparent px-2 py-1 text-sm dark:border-zinc-700"
          >
            <option value="">All</option>
            {tagList.map((tag) => (
              <option key={tag.id} value={tag.id}>
                {tag.name}
              </option>
            ))}
          </select>
        </label>

        <label className="flex flex-col gap-1 text-xs">
          From
          <input
            type="date"
            name="start"
            defaultValue={asString(params.start) ?? ""}
            className="rounded border border-zinc-300 bg-transparent px-2 py-1 text-sm dark:border-zinc-700"
          />
        </label>

        <label className="flex flex-col gap-1 text-xs">
          To
          <input
            type="date"
            name="end"
            defaultValue={asString(params.end) ?? ""}
            className="rounded border border-zinc-300 bg-transparent px-2 py-1 text-sm dark:border-zinc-700"
          />
        </label>

        <button
          type="submit"
          className="rounded bg-zinc-900 px-3 py-1.5 text-sm font-medium text-white dark:bg-zinc-100 dark:text-zinc-900"
        >
          Filter
        </button>

        <Link href="/transactions" className="text-sm underline">
          Clear
        </Link>
      </form>

      {!transactions.ok && (
        <p className="rounded border border-amber-300 bg-amber-50 p-3 text-sm text-amber-900 dark:border-amber-900 dark:bg-amber-950 dark:text-amber-200">
          {transactions.error}
        </p>
      )}

      {transactions.ok && (
        <>
          <p className="text-xs text-zinc-500">
            {total === 0
              ? "No transactions match."
              : `Showing ${offset + 1}–${offset + showing} of ${total}`}
          </p>

          <ul className="flex flex-col divide-y divide-zinc-200 dark:divide-zinc-800">
            {transactions.data.items.map((transaction) => (
              <TransactionRow
                key={transaction.id}
                transaction={transaction}
                categories={categoryList}
              />
            ))}
          </ul>

          <nav className="flex justify-between text-sm">
            {page > 1 ? (
              <Link
                href={{ pathname: "/transactions", query: { ...params, page: page - 1 } }}
                className="underline"
              >
                ← Previous
              </Link>
            ) : (
              <span />
            )}
            {offset + showing < total ? (
              <Link
                href={{ pathname: "/transactions", query: { ...params, page: page + 1 } }}
                className="underline"
              >
                Next →
              </Link>
            ) : (
              <span />
            )}
          </nav>
        </>
      )}
    </main>
  );
}
