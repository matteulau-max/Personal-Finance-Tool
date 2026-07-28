"use server";

/**
 * Server Actions for the Plaid flow.
 *
 * `"use server"` at the top of the file marks every export as a Server
 * Function: it runs on the server, and the client calls it over the network.
 *
 * SECURITY NOTE, straight from the Next.js docs:
 * "Server Functions are reachable via direct POST requests, not just through
 * your application's UI. Always verify authentication and authorization
 * inside every Server Function."
 *
 * In other words a Server Action is a public HTTP endpoint wearing a
 * function's clothing. Anyone can POST to it. The `requireUser()` check below
 * is therefore not belt-and-braces -- it is the actual door lock. The backend
 * verifies the token again independently, which is what makes this defence in
 * depth rather than a single point of failure.
 */

import { auth } from "@clerk/nextjs/server";
import { revalidatePath } from "next/cache";

import { authedFetch } from "@/lib/api";

async function requireUser(): Promise<void> {
  const { userId } = await auth();
  if (!userId) {
    throw new Error("Not authenticated");
  }
}

export type ActionResult = { ok: true } | { ok: false; error: string };

/**
 * Mint a Link token so the browser can open Plaid's bank picker.
 *
 * The token is short-lived and scoped to this user. It is safe to send to the
 * browser -- unlike the access token it eventually leads to, which never
 * leaves the server.
 */
export async function createLinkToken(): Promise<
  { ok: true; linkToken: string } | { ok: false; error: string }
> {
  await requireUser();

  try {
    const response = await authedFetch("/api/plaid/link-token", {
      method: "POST",
    });

    if (!response.ok) {
      return {
        ok: false,
        error:
          response.status === 503
            ? "Plaid is not configured on the server yet."
            : `Could not start the connection (${response.status}).`,
      };
    }

    const body = (await response.json()) as { link_token: string };
    return { ok: true, linkToken: body.link_token };
  } catch {
    return { ok: false, error: "Could not reach the API." };
  }
}

/**
 * Hand the public token to the backend, which exchanges it for an access
 * token, encrypts it, and imports the accounts.
 *
 * Note what this function does NOT do: it never sees an access token. The
 * exchange happens entirely server-side inside our API.
 */
export async function exchangePublicToken(
  publicToken: string,
): Promise<ActionResult> {
  await requireUser();

  try {
    const response = await authedFetch("/api/plaid/exchange", {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify({ public_token: publicToken }),
    });

    if (!response.ok) {
      return { ok: false, error: `Could not link the account (${response.status}).` };
    }

    // Tells Next.js the cached render of /dashboard is stale, so the new
    // connection appears without a manual refresh.
    revalidatePath("/dashboard");
    return { ok: true };
  } catch {
    return { ok: false, error: "Could not reach the API." };
  }
}

export async function syncItem(itemId: string): Promise<ActionResult> {
  await requireUser();

  try {
    const response = await authedFetch(`/api/plaid/items/${itemId}/sync`, {
      method: "POST",
    });

    if (!response.ok) {
      // 404 here means "not yours or not there" -- the backend deliberately
      // does not distinguish the two.
      return { ok: false, error: `Sync failed (${response.status}).` };
    }

    revalidatePath("/dashboard");
    return { ok: true };
  } catch {
    return { ok: false, error: "Could not reach the API." };
  }
}

export async function disconnectItem(itemId: string): Promise<ActionResult> {
  await requireUser();

  try {
    const response = await authedFetch(`/api/plaid/items/${itemId}`, {
      method: "DELETE",
    });

    if (!response.ok) {
      return { ok: false, error: `Could not disconnect (${response.status}).` };
    }

    revalidatePath("/dashboard");
    return { ok: true };
  } catch {
    return { ok: false, error: "Could not reach the API." };
  }
}
