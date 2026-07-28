import Link from "next/link";
import { UserButton } from "@clerk/nextjs";

import { ConnectBank } from "@/components/connect-bank";
import { ConnectionList } from "@/components/connection-list";
import { getAccounts, getMe, getPlaidItems } from "@/lib/api";
import { clerkEnabled } from "@/lib/clerk";

function ClerkSetupNotice() {
  return (
    <main className="mx-auto flex min-h-screen w-full max-w-2xl flex-col justify-center gap-4 px-6">
      <h1 className="text-2xl font-semibold tracking-tight">Dashboard</h1>
      <p className="rounded border border-blue-300 bg-blue-50 p-3 text-sm text-blue-900 dark:border-blue-900 dark:bg-blue-950 dark:text-blue-200">
        Authentication is not configured, so there is no signed-in user to show
        a dashboard for. Add your Clerk keys to{" "}
        <code className="font-mono">frontend/.env.local</code> and restart the
        dev server &mdash; see{" "}
        <code className="font-mono">docs/milestone-03-auth.md</code>.
      </p>
    </main>
  );
}

/**
 * Money is formatted here, at the edge, and never earlier.
 *
 * The API sends amounts as STRINGS, deliberately: JSON numbers are IEEE
 * doubles, so parsing "1234.56" into a JavaScript number reintroduces exactly
 * the float imprecision we went to the trouble of avoiding in PostgreSQL.
 * Keeping the string intact until the moment of display means arithmetic
 * never happens in a lossy type.
 */
function formatMoney(amount: string | null, currency: string): string {
  if (amount === null) return "—";

  const value = Number(amount);
  if (Number.isNaN(value)) return amount;

  return new Intl.NumberFormat("en-US", {
    style: "currency",
    currency,
  }).format(value);
}

export default async function DashboardPage() {
  // Without a provider, Clerk's components throw during prerendering and the
  // production build fails outright. Bail out before rendering any of them.
  if (!clerkEnabled) {
    return <ClerkSetupNotice />;
  }

  const [me, accounts, items] = await Promise.all([
    getMe(),
    getAccounts(),
    getPlaidItems(),
  ]);

  const netWorth = accounts.ok
    ? accounts.data
        .filter((account) => account.include_in_net_worth)
        .reduce((total, account) => {
          const balance = Number(account.current_balance ?? "0");
          if (Number.isNaN(balance)) return total;
          // Credit balances are money OWED, so they subtract. Getting this
          // backwards is one of the easiest ways to show someone a net worth
          // that is wrong by twice their card balance.
          return account.type === "credit" ? total - balance : total + balance;
        }, 0)
    : 0;

  return (
    <main className="mx-auto flex min-h-screen w-full max-w-3xl flex-col gap-8 px-6 py-12">
      <header className="flex items-start justify-between gap-4">
        <div>
          <h1 className="text-2xl font-semibold tracking-tight">Dashboard</h1>
          <p className="text-sm text-zinc-600 dark:text-zinc-400">
            {me.ok ? me.data.email : "Loading your profile…"}
          </p>
        </div>
        <div className="flex items-center gap-4">
          <Link href="/transactions" className="text-sm underline">
            Transactions
          </Link>
          <UserButton />
        </div>
      </header>

      {!me.ok && (
        <p className="rounded border border-amber-300 bg-amber-50 p-3 text-sm text-amber-900 dark:border-amber-900 dark:bg-amber-950 dark:text-amber-200">
          {me.error}
        </p>
      )}

      <section className="rounded-lg border border-zinc-200 p-5 dark:border-zinc-800">
        <h2 className="mb-1 text-sm font-medium uppercase tracking-wide text-zinc-500">
          Net worth
        </h2>
        <p className="text-3xl font-semibold tabular-nums">
          {new Intl.NumberFormat("en-US", {
            style: "currency",
            currency: "USD",
          }).format(netWorth)}
        </p>
        <p className="mt-1 text-xs text-zinc-500">
          Cash and investments minus credit balances.
        </p>
      </section>

      <section
        aria-labelledby="connections-heading"
        className="flex flex-col gap-4 rounded-lg border border-zinc-200 p-5 dark:border-zinc-800"
      >
        <h2
          id="connections-heading"
          className="text-sm font-medium uppercase tracking-wide text-zinc-500"
        >
          Connected banks
        </h2>

        {items.ok ? (
          <ConnectionList connections={items.data} />
        ) : (
          <p className="text-sm text-amber-700 dark:text-amber-300">
            {items.error}
          </p>
        )}

        <ConnectBank />
      </section>

      <section
        aria-labelledby="accounts-heading"
        className="rounded-lg border border-zinc-200 p-5 dark:border-zinc-800"
      >
        <h2
          id="accounts-heading"
          className="mb-4 text-sm font-medium uppercase tracking-wide text-zinc-500"
        >
          Accounts
        </h2>

        {accounts.ok && accounts.data.length === 0 && (
          <p className="text-sm text-zinc-600 dark:text-zinc-400">
            No accounts yet. Connect a bank above to import them.
          </p>
        )}

        {accounts.ok && accounts.data.length > 0 && (
          <ul className="flex flex-col divide-y divide-zinc-200 dark:divide-zinc-800">
            {accounts.data.map((account) => (
              <li
                key={account.id}
                className="flex items-center justify-between gap-4 py-3"
              >
                <div>
                  <p className="font-medium">{account.display_name}</p>
                  <p className="text-xs text-zinc-500">
                    {account.subtype ?? account.type}
                    {account.mask ? ` ••${account.mask}` : ""}
                    {account.utilization !== null
                      ? ` · ${(Number(account.utilization) * 100).toFixed(0)}% utilized`
                      : ""}
                  </p>
                </div>
                <p className="font-mono text-sm tabular-nums">
                  {formatMoney(account.current_balance, account.currency_code)}
                </p>
              </li>
            ))}
          </ul>
        )}

        {!accounts.ok && (
          <p className="text-sm text-amber-700 dark:text-amber-300">
            {accounts.error}
          </p>
        )}
      </section>
    </main>
  );
}
