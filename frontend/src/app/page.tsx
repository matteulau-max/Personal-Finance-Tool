import Link from "next/link";
import { Show, SignInButton, UserButton } from "@clerk/nextjs";

import { getBackendHealth, getDatabaseHealth } from "@/lib/api";
import { clerkEnabled } from "@/lib/clerk";

/**
 * This is a Server Component (the default in the Next.js App Router).
 * It runs on the server, so it can call our backend directly and can safely
 * read private environment variables. Nothing here ships to the browser
 * except the finished HTML.
 */
export default async function Home() {
  // Promise.all runs both requests concurrently rather than one after the
  // other. With two 30ms calls that is the difference between 30ms and 60ms --
  // trivial here, but the habit matters once a page needs six of them.
  const [health, database] = await Promise.all([
    getBackendHealth(),
    getDatabaseHealth(),
  ]);

  return (
    <main className="mx-auto flex min-h-screen max-w-2xl flex-col justify-center gap-8 px-6 py-16">
      <header className="flex flex-col gap-2">
        <h1 className="text-3xl font-semibold tracking-tight">
          Personal Finance Dashboard
        </h1>
        <p className="text-zinc-600 dark:text-zinc-400">
          Your accounts, transactions and spending in one place &mdash; with a
          complete history that never duplicates and never loses a correction.
        </p>
      </header>

      {clerkEnabled ? (
        <div className="flex items-center gap-4">
          {/*
            `<Show>` renders its children only when the condition holds. In
            Clerk v7 it replaces the older `<SignedIn>` / `<SignedOut>`
            components you will see in most tutorials -- those no longer
            exist, and importing them is a build error.

            Because this resolves on the server, the correct branch is in the
            initial HTML. There is no flash of a "Sign in" button for someone
            who is already signed in.
          */}
          <Show when="signed-out">
            <SignInButton mode="modal">
              <button className="rounded bg-zinc-900 px-4 py-2 text-sm font-medium text-white dark:bg-zinc-100 dark:text-zinc-900">
                Sign in
              </button>
            </SignInButton>
          </Show>

          <Show when="signed-in">
            <Link
              href="/dashboard"
              className="rounded bg-zinc-900 px-4 py-2 text-sm font-medium text-white dark:bg-zinc-100 dark:text-zinc-900"
            >
              Go to dashboard
            </Link>
            <UserButton />
          </Show>
        </div>
      ) : (
        <p className="rounded border border-blue-300 bg-blue-50 p-3 text-sm text-blue-900 dark:border-blue-900 dark:bg-blue-950 dark:text-blue-200">
          Clerk is not configured yet. Add your keys to{" "}
          <code className="font-mono">frontend/.env.local</code> to enable
          sign-in &mdash; see{" "}
          <code className="font-mono">docs/milestone-03-auth.md</code>.
        </p>
      )}

      <section
        className="rounded-lg border border-zinc-200 p-5 dark:border-zinc-800"
        aria-labelledby="system-status"
      >
        <h2
          id="system-status"
          className="mb-4 text-sm font-medium uppercase tracking-wide text-zinc-500"
        >
          System status
        </h2>

        <dl className="flex flex-col gap-3 text-sm">
          <div className="flex items-center justify-between gap-4">
            <dt className="text-zinc-600 dark:text-zinc-400">Frontend</dt>
            <dd className="font-medium text-green-600 dark:text-green-400">
              Running
            </dd>
          </div>

          <div className="flex items-center justify-between gap-4">
            <dt className="text-zinc-600 dark:text-zinc-400">Backend API</dt>
            <dd
              className={
                health.ok
                  ? "font-medium text-green-600 dark:text-green-400"
                  : "font-medium text-amber-600 dark:text-amber-400"
              }
            >
              {health.ok ? "Connected" : "Not reachable"}
            </dd>
          </div>

          <div className="flex items-center justify-between gap-4">
            <dt className="text-zinc-600 dark:text-zinc-400">Database</dt>
            <dd
              className={
                database.ok
                  ? "font-medium text-green-600 dark:text-green-400"
                  : "font-medium text-amber-600 dark:text-amber-400"
              }
            >
              {database.ok ? "Connected" : "Not reachable"}
            </dd>
          </div>

          {database.ok && (
            <div className="flex items-center justify-between gap-4">
              <dt className="text-zinc-600 dark:text-zinc-400">
                Schema version
              </dt>
              <dd className="font-mono text-xs">
                {database.data.migration_revision ?? "none"}
              </dd>
            </div>
          )}

          <div className="flex items-center justify-between gap-4">
            <dt className="text-zinc-600 dark:text-zinc-400">
              Authentication
            </dt>
            <dd
              className={
                clerkEnabled
                  ? "font-medium text-green-600 dark:text-green-400"
                  : "font-medium text-zinc-500"
              }
            >
              {clerkEnabled ? "Clerk configured" : "Not configured"}
            </dd>
          </div>

          {health.ok && (
            <div className="flex items-center justify-between gap-4">
              <dt className="text-zinc-600 dark:text-zinc-400">Environment</dt>
              <dd className="font-medium">{health.data.environment}</dd>
            </div>
          )}
        </dl>

        {!health.ok && (
          <p className="mt-4 rounded border border-amber-300 bg-amber-50 p-3 text-sm text-amber-900 dark:border-amber-900 dark:bg-amber-950 dark:text-amber-200">
            {health.error}
          </p>
        )}

        {health.ok && !database.ok && (
          <p className="mt-4 rounded border border-amber-300 bg-amber-50 p-3 text-sm text-amber-900 dark:border-amber-900 dark:bg-amber-950 dark:text-amber-200">
            {database.error}
          </p>
        )}
      </section>
    </main>
  );
}
