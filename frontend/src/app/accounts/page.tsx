import Link from "next/link";

import { MainNav } from "@/components/main-nav";
import { getAccounts, type AccountResponse } from "@/lib/api";
import { clerkEnabled } from "@/lib/clerk";
import { money, percent, shortDate } from "@/lib/format";
import { requireSignedIn } from "@/lib/require-signed-in";

/**
 * Every account, grouped into what you own and what you owe.
 *
 * The dashboard lists accounts flat, which answers "what is my balance"
 * but not "how does that add up to my net worth". Grouping with subtotals
 * makes the headline figure checkable: assets minus liabilities, both shown,
 * and the arithmetic visible rather than asserted.
 *
 * Liabilities are the part that goes wrong. Plaid reports a credit card
 * balance as a positive number -- it is the size of the debt, not a negative
 * asset -- so a naive sum of every balance overstates net worth by twice the
 * card balance. That is why the two groups are computed separately here and
 * combined only at the end.
 */

// Which side of the balance sheet each account type falls on. `other` covers
// Venmo and anything Plaid did not classify; treating it as an asset matches
// the backend's net-worth calculation, which subtracts only credit and loan.
const LIABILITY_TYPES = new Set(["credit", "loan"]);

const TYPE_LABELS: Record<string, string> = {
  depository: "Cash",
  investment: "Investments",
  credit: "Credit cards",
  loan: "Loans",
  other: "Other",
};

function Shell({ children }: { children: React.ReactNode }) {
  return (
    <main className="mx-auto flex min-h-screen w-full max-w-4xl flex-col gap-6 px-6 py-12">
      {children}
    </main>
  );
}

function balanceOf(account: AccountResponse): number {
  const value = Number(account.current_balance ?? "0");
  return Number.isNaN(value) ? 0 : value;
}

function AccountRow({ account }: { account: AccountResponse }) {
  return (
    <li className="flex flex-wrap items-baseline justify-between gap-x-4 gap-y-1 py-3">
      <div className="min-w-0">
        <p className="font-medium">
          {account.display_name}
          {!account.include_in_net_worth && (
            // Worth stating on the row rather than only in a footnote: an
            // account that is visible but not counted is otherwise a
            // discrepancy the reader has to hunt for.
            <span className="ml-2 rounded bg-zinc-100 px-1.5 py-0.5 text-xs font-normal text-zinc-600 dark:bg-zinc-800 dark:text-zinc-400">
              not in net worth
            </span>
          )}
        </p>
        <p className="text-xs text-zinc-500">
          {account.subtype ?? account.type}
          {account.mask ? ` ••${account.mask}` : ""}
          {account.utilization !== null
            ? ` · ${percent(account.utilization)} utilized`
            : ""}
          {account.balance_updated_at
            ? ` · updated ${shortDate(account.balance_updated_at.slice(0, 10))}`
            : ""}
        </p>
      </div>

      <div className="text-right">
        <p className="font-mono text-sm tabular-nums">
          {money(account.current_balance, {
            currency: account.currency_code,
            cents: true,
          })}
        </p>
        {account.available_balance !== null &&
          account.available_balance !== account.current_balance && (
            <p className="text-xs text-zinc-500">
              {money(account.available_balance, {
                currency: account.currency_code,
                cents: true,
              })}{" "}
              available
            </p>
          )}
      </div>
    </li>
  );
}

function Group({
  title,
  accounts,
  total,
}: {
  title: string;
  accounts: AccountResponse[];
  total: number;
}) {
  if (accounts.length === 0) return null;

  // Within a group, order by size. The largest balances are the ones that
  // move net worth, and they should not be buried under a long tail.
  const byType = new Map<string, AccountResponse[]>();
  for (const account of accounts) {
    const list = byType.get(account.type) ?? [];
    list.push(account);
    byType.set(account.type, list);
  }

  return (
    <section className="rounded-lg border border-zinc-200 p-5 dark:border-zinc-800">
      <div className="mb-3 flex items-baseline justify-between gap-4">
        <h2 className="text-sm font-medium uppercase tracking-wide text-zinc-500">
          {title}
        </h2>
        <p className="font-mono text-lg font-semibold tabular-nums">
          {money(total)}
        </p>
      </div>

      {[...byType.entries()].map(([type, group]) => (
        <div key={type} className="mt-4 first:mt-0">
          <h3 className="text-xs font-medium uppercase tracking-wide text-zinc-400">
            {TYPE_LABELS[type] ?? type}
          </h3>
          <ul className="flex flex-col divide-y divide-zinc-200 dark:divide-zinc-800">
            {[...group]
              .sort((a, b) => balanceOf(b) - balanceOf(a))
              .map((account) => (
                <AccountRow key={account.id} account={account} />
              ))}
          </ul>
        </div>
      ))}
    </section>
  );
}

export default async function AccountsPage() {
  if (!clerkEnabled) {
    return (
      <Shell>
        <h1 className="text-2xl font-semibold tracking-tight">Accounts</h1>
        <p className="rounded border border-blue-300 bg-blue-50 p-3 text-sm text-blue-900 dark:border-blue-900 dark:bg-blue-950 dark:text-blue-200">
          Authentication is not configured. See{" "}
          <code className="font-mono">docs/milestone-03-auth.md</code>.
        </p>
      </Shell>
    );
  }

  await requireSignedIn();

  const accounts = await getAccounts();

  if (!accounts.ok) {
    return (
      <Shell>
        <h1 className="text-2xl font-semibold tracking-tight">Accounts</h1>
        <p className="rounded border border-amber-300 bg-amber-50 p-3 text-sm text-amber-900 dark:border-amber-900 dark:bg-amber-950 dark:text-amber-200">
          {accounts.error}
        </p>
      </Shell>
    );
  }

  const visible = accounts.data.filter((account) => !account.is_hidden);
  const assets = visible.filter((a) => !LIABILITY_TYPES.has(a.type));
  const liabilities = visible.filter((a) => LIABILITY_TYPES.has(a.type));

  // Only accounts flagged for inclusion count toward net worth, which is why
  // the group subtotals below can legitimately differ from these figures.
  const counted = (list: AccountResponse[]) =>
    list
      .filter((account) => account.include_in_net_worth)
      .reduce((sum, account) => sum + balanceOf(account), 0);

  const assetTotal = counted(assets);
  const liabilityTotal = counted(liabilities);
  const netWorth = assetTotal - liabilityTotal;

  const hiddenCount = accounts.data.length - visible.length;

  return (
    <Shell>
      <header className="flex flex-wrap items-start justify-between gap-4">
        <div>
          <h1 className="text-2xl font-semibold tracking-tight">Accounts</h1>
          <p className="text-sm text-zinc-600 dark:text-zinc-400">
            {visible.length} {visible.length === 1 ? "account" : "accounts"}
            {hiddenCount > 0 ? ` · ${hiddenCount} hidden` : ""}
          </p>
        </div>
        <MainNav current="/accounts" />
      </header>

      {visible.length === 0 ? (
        <section className="rounded-lg border border-zinc-200 p-8 text-center dark:border-zinc-800">
          <p className="text-sm text-zinc-600 dark:text-zinc-400">
            No accounts yet.
          </p>
          <p className="mt-2 text-xs text-zinc-500">
            <Link href="/dashboard" className="underline">
              Connect a bank
            </Link>{" "}
            to import them.
          </p>
        </section>
      ) : (
        <>
          <section className="rounded-lg border border-zinc-200 p-5 dark:border-zinc-800">
            <p className="text-xs font-medium uppercase tracking-wide text-zinc-500">
              Net worth
            </p>
            <p className="text-4xl font-semibold tabular-nums">
              {money(netWorth)}
            </p>
            {/* The subtraction written out, so the headline can be checked
                rather than taken on trust. */}
            <p className="mt-1 font-mono text-xs text-zinc-500">
              {money(assetTotal)} owned − {money(liabilityTotal)} owed
            </p>
          </section>

          <Group title="Assets" accounts={assets} total={assetTotal} />
          <Group
            title="Liabilities"
            accounts={liabilities}
            total={liabilityTotal}
          />

          <p className="text-xs text-zinc-500">
            Balances come from the last sync of each account, not live from
            the bank — the date on each row is when it was last refreshed.{" "}
            <Link href="/dashboard" className="underline">
              Sync from the dashboard
            </Link>{" "}
            to update them.
          </p>
        </>
      )}
    </Shell>
  );
}
