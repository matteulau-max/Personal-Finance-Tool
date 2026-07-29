"use server";

/**
 * Server Action for the "ask a question" panel.
 *
 * Same rule as every other action file in this project: a Server Function is
 * a public HTTP endpoint wearing a function's clothing, so `requireUser()`
 * is the door lock, not a formality. The backend verifies the token again.
 */

import { auth } from "@clerk/nextjs/server";

import { authedFetch } from "@/lib/api";

async function requireUser(): Promise<void> {
  const { userId } = await auth();
  if (!userId) {
    throw new Error("Not authenticated");
  }
}

export type Source = { tool: string; arguments: Record<string, unknown> };

export type AskResult =
  | { ok: true; answer: string; sources: Source[] }
  | { ok: false; error: string };

/**
 * The status codes are the interesting part of this function.
 *
 * A 422 means the backend computed an answer and then *threw it away*,
 * because it contained a figure no query produced. The message says exactly
 * that. It is tempting to soften it into "something went wrong" -- but the
 * honest version is more useful and, more importantly, it is the behaviour
 * the user should know the product has.
 */
export async function askQuestion(question: string): Promise<AskResult> {
  await requireUser();

  const trimmed = question.trim();
  if (!trimmed) {
    return { ok: false, error: "Type a question first." };
  }

  try {
    const response = await authedFetch("/api/insights", {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify({ question: trimmed.slice(0, 500) }),
    });

    if (!response.ok) {
      if (response.status === 503) {
        return {
          ok: false,
          error:
            "Insights are not configured on this server. Set ANTHROPIC_API_KEY in backend/.env.",
        };
      }
      if (response.status === 422) {
        return {
          ok: false,
          error:
            "That answer could not be verified against your data, so it was discarded. Try rephrasing the question.",
        };
      }
      return { ok: false, error: `Could not answer that (${response.status}).` };
    }

    const body = (await response.json()) as { answer: string; sources: Source[] };
    return { ok: true, answer: body.answer, sources: body.sources ?? [] };
  } catch {
    return { ok: false, error: "Could not reach the API." };
  }
}
