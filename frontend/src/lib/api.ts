/**
 * Tiny client for talking to our FastAPI backend.
 *
 * IMPORTANT security note about environment variables in Next.js:
 * a variable named `NEXT_PUBLIC_*` is baked into the JavaScript that ships to
 * the browser -- anyone can read it. Any other variable (like the
 * `API_BASE_URL` below) stays on the server only. Since we call the backend
 * from Server Components, we use the private form. Never put a Plaid secret
 * or database password behind a NEXT_PUBLIC_ name.
 */

const API_BASE_URL = process.env.API_BASE_URL ?? "http://localhost:8000";

export type HealthResponse = {
  status: string;
  app_name: string;
  environment: string;
};

export type DatabaseHealthResponse = {
  status: string;
  database: string;
  migration_revision: string | null;
  detail: string | null;
};

export type Result<T> = { ok: true; data: T } | { ok: false; error: string };

export type HealthResult = Result<HealthResponse>;
export type DatabaseHealthResult = Result<DatabaseHealthResponse>;

/**
 * Returns a result object instead of throwing.
 *
 * Why: a backend that is merely offline is a normal, expected state during
 * development. Modelling it as data (rather than an exception) forces the UI
 * to render something useful instead of crashing the whole page.
 */
export async function getBackendHealth(): Promise<HealthResult> {
  try {
    const response = await fetch(`${API_BASE_URL}/health`, {
      // Never serve a stale health check.
      cache: "no-store",
      signal: AbortSignal.timeout(5000),
    });

    if (!response.ok) {
      return { ok: false, error: `Backend responded with ${response.status}` };
    }

    return { ok: true, data: (await response.json()) as HealthResponse };
  } catch {
    return {
      ok: false,
      error: `Could not reach the backend at ${API_BASE_URL}. Is it running?`,
    };
  }
}

/**
 * Readiness check: is the database reachable, and which migration is applied?
 *
 * Note this treats HTTP 503 as a *successful request reporting a problem*
 * rather than a transport failure -- the backend answered, the database is
 * what is down. Distinguishing the two is what lets the UI say "backend up,
 * database down" instead of an unhelpful "something is broken".
 */
export async function getDatabaseHealth(): Promise<DatabaseHealthResult> {
  try {
    const response = await fetch(`${API_BASE_URL}/health/db`, {
      cache: "no-store",
      signal: AbortSignal.timeout(5000),
    });

    const body = (await response.json()) as DatabaseHealthResponse;

    if (!response.ok) {
      return {
        ok: false,
        error:
          response.status === 503
            ? "Backend is running but cannot reach PostgreSQL. Is `docker compose up -d` running?"
            : `Backend responded with ${response.status}`,
      };
    }

    return { ok: true, data: body };
  } catch {
    return {
      ok: false,
      error: `Could not reach the backend at ${API_BASE_URL}. Is it running?`,
    };
  }
}
