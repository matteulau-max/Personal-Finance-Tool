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

export type HealthResult =
  | { ok: true; data: HealthResponse }
  | { ok: false; error: string };

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
