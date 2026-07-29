"use server";

/**
 * Server Actions for editing transactions.
 *
 * As in Milestone 4: a Server Function is a public HTTP endpoint wearing a
 * function's clothing. Anyone can POST to it, so each one verifies auth
 * itself. The backend verifies the token independently -- that is what makes
 * this defence in depth rather than a single point of failure.
 */

import { auth } from "@clerk/nextjs/server";
import { revalidatePath } from "next/cache";

import { authedFetch } from "@/lib/api";

// EVERY export in a "use server" file must be an `async function`.
// Returning a Promise from a sync function is not enough -- Next.js rejects
// the build with "Server Actions must be async functions". The rule exists
// because these become network endpoints, and the framework needs to wrap
// them uniformly.

async function requireUser(): Promise<void> {
  const { userId } = await auth();
  if (!userId) throw new Error("Not authenticated");
}

export type ActionResult = { ok: true } | { ok: false; error: string };

async function patchTransaction(
  transactionId: string,
  body: Record<string, unknown>,
): Promise<ActionResult> {
  await requireUser();

  try {
    const response = await authedFetch(`/api/transactions/${transactionId}`, {
      method: "PATCH",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify(body),
    });

    if (!response.ok) {
      return { ok: false, error: `Update failed (${response.status}).` };
    }

    revalidatePath("/transactions");
    return { ok: true };
  } catch {
    return { ok: false, error: "Could not reach the API." };
  }
}

/**
 * `applyToSimilar` is passed straight through from a checkbox in the UI.
 *
 * Deliberately the user's decision, not ours: fixing ONE transaction ("this
 * particular Amazon order was a gift") does not mean every Amazon order is a
 * gift. Only the user knows whether a correction is a one-off or a pattern.
 */
export async function setCategory(
  transactionId: string,
  categoryId: string | null,
  applyToSimilar = false,
): Promise<ActionResult> {
  return await patchTransaction(transactionId, {
    category_id: categoryId,
    apply_to_similar: applyToSimilar,
  });
}

export async function setTags(
  transactionId: string,
  tagIds: string[],
): Promise<ActionResult> {
  return await patchTransaction(transactionId, { tag_ids: tagIds });
}

export async function setReviewed(
  transactionId: string,
  reviewed: boolean,
): Promise<ActionResult> {
  return await patchTransaction(transactionId, { is_reviewed: reviewed });
}

/** Clear every override, restoring exactly what the bank sent. */
export async function resetToOriginal(transactionId: string): Promise<ActionResult> {
  return await patchTransaction(transactionId, {
    category_id: null,
    merchant_id: null,
    description: null,
    amount: null,
    date: null,
  });
}
