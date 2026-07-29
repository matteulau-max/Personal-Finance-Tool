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

export type PlaidItemResponse = {
  id: string;
  status: string;
  institution: {
    name: string;
    logo_url: string | null;
    primary_color: string | null;
  } | null;
  last_successful_sync_at: string | null;
  consent_expires_at: string | null;
  error_code: string | null;
  created_at: string;
};

export function getPlaidItems(): Promise<Result<PlaidItemResponse[]>> {
  return authedJson<PlaidItemResponse[]>("/api/plaid/items");
}

// ---------------------------------------------------------------------------
// Transactions, categories, tags (Milestone 5)
// ---------------------------------------------------------------------------

export type CategorySummary = {
  id: string;
  name: string;
  slug: string | null;
  icon: string | null;
  color: string | null;
  is_income: boolean;
  is_transfer: boolean;
};

export type MerchantSummary = {
  id: string;
  display_name: string;
  logo_url: string | null;
  is_subscription: boolean;
};

export type TagSummary = { id: string; name: string; color: string | null };

export type TransactionItem = {
  id: string;
  account_id: string;
  status: string;
  source: string;
  amount: string;
  date: string;
  description: string;
  currency_code: string;
  category: CategorySummary | null;
  merchant: MerchantSummary | null;
  tags: TagSummary[];
  notes: string | null;
  is_hidden: boolean;
  is_reviewed: boolean;
  category_source: string;
  is_user_modified: boolean;
  raw_name: string;
  raw_amount: string;
  raw_date: string;
  created_at: string;
};

export type TransactionPage = {
  items: TransactionItem[];
  total: number;
  limit: number;
  offset: number;
};

export type CategoryItem = CategorySummary & {
  parent_id: string | null;
  is_system: boolean;
  sort_order: number;
};

export type TagItem = {
  id: string;
  name: string;
  color: string | null;
  description: string | null;
  created_at: string;
};

export type TransactionFilters = {
  search?: string;
  categoryId?: string;
  tagId?: string;
  startDate?: string;
  endDate?: string;
  limit?: number;
  offset?: number;
};

export function getTransactions(
  filters: TransactionFilters = {},
): Promise<Result<TransactionPage>> {
  // URLSearchParams handles escaping. Building a query string by hand is how
  // a merchant name containing "&" silently truncates the search.
  const params = new URLSearchParams();
  if (filters.search) params.set("search", filters.search);
  if (filters.categoryId) params.set("category_id", filters.categoryId);
  if (filters.tagId) params.set("tag_id", filters.tagId);
  if (filters.startDate) params.set("start_date", filters.startDate);
  if (filters.endDate) params.set("end_date", filters.endDate);
  params.set("limit", String(filters.limit ?? 50));
  params.set("offset", String(filters.offset ?? 0));

  return authedJson<TransactionPage>(`/api/transactions?${params.toString()}`);
}

export function getCategories(): Promise<Result<CategoryItem[]>> {
  return authedJson<CategoryItem[]>("/api/categories");
}

export function getTags(): Promise<Result<TagItem[]>> {
  return authedJson<TagItem[]>("/api/tags");
}

// ---------------------------------------------------------------------------
// Analytics (Milestone 6)
// ---------------------------------------------------------------------------

export type MonthlyPoint = {
  period_start: string;
  spending: string;
  income: string;
  net: string;
  savings_rate: string | null;
};

export type CategoryTotal = {
  category_id: string | null;
  category_name: string;
  total: string;
  transaction_count: number;
};

export type CategoryChange = CategoryTotal & { change: string };

export type MerchantTotal = {
  merchant_id: string | null;
  merchant_name: string;
  total: string;
  transaction_count: number;
  average: string;
};

export type NetWorthPoint = {
  as_of: string;
  assets: string;
  liabilities: string;
  net_worth: string;
};

export type BudgetComparison = {
  category_id: string;
  category_name: string;
  budgeted: string;
  actual: string;
  remaining: string;
  used_fraction: string | null;
};

export type RecurringCharge = {
  merchant_id: string | null;
  merchant_name: string;
  typical_amount: string;
  occurrences: number;
  average_gap_days: number;
  last_seen: string;
};

export type LargestTransaction = {
  id: string;
  date: string;
  description: string;
  amount: string;
  category_name: string | null;
};

export type Overview = {
  as_of: string;
  net_worth: string;
  liquid_balance: string;
  credit_utilization: string | null;
  month_to_date_spending: string;
  month_to_date_income: string;
  savings_rate: string | null;
  rolling_30_day_spending: string;
  rolling_90_day_spending: string;
  burn_rate: string;
  cash_runway_months: string | null;
  monthly: MonthlyPoint[];
  top_categories: CategoryTotal[];
  top_merchants: MerchantTotal[];
  largest_transactions: LargestTransaction[];
  biggest_increases: CategoryChange[];
  biggest_decreases: CategoryChange[];
  net_worth_series: NetWorthPoint[];
  recurring: RecurringCharge[];
  budgets: BudgetComparison[];
};

/**
 * One request for the whole dashboard.
 *
 * Not eight parallel requests: that would pay eight round trips, eight token
 * verifications and eight connection checkouts to draw a single screen, and
 * any one of them failing would leave the page half-drawn.
 */
export function getOverview(months = 12): Promise<Result<Overview>> {
  return authedJson<Overview>(`/api/analytics/overview?months=${months}`);
}

export type Suggestion = { question: string };

/**
 * Starter questions for the ask panel.
 *
 * Static on the server, so this works even when insights are switched off --
 * paying for a model round trip to render four buttons would be a strange
 * way to spend an API call.
 */
export function getInsightSuggestions(): Promise<Result<Suggestion[]>> {
  return authedJson<Suggestion[]>("/api/insights/suggestions");
}
