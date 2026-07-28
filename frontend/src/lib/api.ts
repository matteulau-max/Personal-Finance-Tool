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

import { auth } from "@clerk/nextjs/server";

import { clerkEnabled } from "@/lib/clerk";

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

// ---------------------------------------------------------------------------
// Authenticated requests
// ---------------------------------------------------------------------------

export type MeResponse = {
  id: string;
  email: string;
  full_name: string | null;
  default_currency: string;
  timezone: string;
  created_at: string;
};

export type AccountResponse = {
  id: string;
  name: string;
  display_name: string;
  mask: string | null;
  type: string;
  subtype: string | null;
  currency_code: string;
  current_balance: string | null;
  available_balance: string | null;
  credit_limit: string | null;
  balance_updated_at: string | null;
  utilization: string | null;
  is_active: boolean;
  is_hidden: boolean;
  include_in_net_worth: boolean;
};

/**
 * Call the backend as the signed-in user.
 *
 * WHERE THE TOKEN COMES FROM
 * --------------------------
 * `auth()` reads Clerk's session cookie, and `getToken()` exchanges it for a
 * short-lived JWT. This runs on the SERVER, inside a Server Component or
 * Server Action -- never in the browser.
 *
 * That matters. Clerk's session cookie is HttpOnly, meaning JavaScript in the
 * page cannot read it. If a dependency is ever compromised and injects a
 * script, it still cannot steal the session, because there is nothing readable
 * to steal. Fetching the token server-side preserves that property; copying it
 * into browser-visible state would throw it away.
 *
 * The token is deliberately short-lived (about a minute). `getToken()` returns
 * a fresh one per request, so there is nothing worth caching or persisting.
 */
export async function authedFetch(
  path: string,
  init: RequestInit = {},
): Promise<Response> {
  if (!clerkEnabled) {
    throw new Error("Clerk is not configured; cannot make an authenticated request.");
  }

  const { getToken } = await auth();
  const token = await getToken();

  if (!token) {
    throw new Error("No active session.");
  }

  return fetch(`${API_BASE_URL}${path}`, {
    ...init,
    cache: "no-store",
    headers: {
      ...init.headers,
      Authorization: `Bearer ${token}`,
    },
  });
}

async function authedJson<T>(path: string): Promise<Result<T>> {
  try {
    const response = await authedFetch(path);

    if (response.status === 401) {
      return { ok: false, error: "Your session has expired. Please sign in again." };
    }
    if (!response.ok) {
      return { ok: false, error: `Request failed with ${response.status}` };
    }

    return { ok: true, data: (await response.json()) as T };
  } catch (error) {
    return {
      ok: false,
      error: error instanceof Error ? error.message : "Unknown error",
    };
  }
}

export function getMe(): Promise<Result<MeResponse>> {
  return authedJson<MeResponse>("/api/me");
}

export function getAccounts(): Promise<Result<AccountResponse[]>> {
  return authedJson<AccountResponse[]>("/api/accounts");
}
