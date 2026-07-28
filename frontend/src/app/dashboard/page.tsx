import { UserButton } from "@clerk/nextjs";

import { getAccounts, getMe } from "@/lib/api";
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
 * The first authenticated page.
 *
 * It proves the full chain works end to end:
 *
 *   browser cookie -> Clerk session -> short-lived JWT -> Authorization header
 *   -> FastAPI verifies the signature -> local user row -> scoped query
 *
 * `proxy.ts` already redirects signed-out visitors away from `/dashboard`, but
 * that is only a convenience. The data below is protected because the *API*
 * refuses to return it without a valid token -- which is true whether the
 * request came from this page or from curl.
 */
export default async function DashboardPage() {
  // Without a provider, Clerk's components throw during prerendering and the
  // production build fails outright. Bail out before rendering any of them.
  if (!clerkEnabled) {
    return <ClerkSetupNotice />;
  }

  const [me, accounts] = await Promise.all([getMe(), getAccounts()]);

  return (
    <main className="mx-auto flex min-h-screen w-full max-w-3xl flex-col gap-8 px-6 py-12">
      <header className="flex items-start justify-between gap-4">
        <div>
          <h1 className="text-2xl font-semibold tracking-tight">Dashboard</h1>
          <p className="text-sm text-zinc-600 dark:text-zinc-400">
            {me.ok ? me.data.email : "Loading your profile…"}
          </p>
        </div>
        {/* Clerk's prebuilt account menu: profile, security, sign out. */}
        <UserButton />
      </header>

      {!me.ok && (
        <p className="rounded border border-amber-300 bg-amber-50 p-3 text-sm text-amber-900 dark:border-amber-900 dark:bg-amber-950 dark:text-amber-200">
          {me.error}
        </p>
      )}

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
            No accounts yet. Connecting a bank via Plaid arrives in Milestone 4.
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
                  </p>
                </div>
                <p className="font-mono text-sm">
                  {account.current_balance ?? "—"} {account.currency_code}
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
