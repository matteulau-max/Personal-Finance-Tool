import { getBackendHealth } from "@/lib/api";

/**
 * This is a Server Component (the default in the Next.js App Router).
 * It runs on the server, so it can call our backend directly and can safely
 * read private environment variables. Nothing here ships to the browser
 * except the finished HTML.
 *
 * Making the function `async` and `await`-ing inside it is all it takes to
 * fetch data -- no useEffect, no loading spinner boilerplate.
 */
export default async function Home() {
  const health = await getBackendHealth();

  return (
    <main className="mx-auto flex min-h-screen max-w-2xl flex-col justify-center gap-8 px-6 py-16">
      <header className="flex flex-col gap-2">
        <h1 className="text-3xl font-semibold tracking-tight">
          Personal Finance Dashboard
        </h1>
        <p className="text-zinc-600 dark:text-zinc-400">
          Milestone 1 &mdash; development environment and project skeleton.
        </p>
      </header>

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
      </section>
    </main>
  );
}
