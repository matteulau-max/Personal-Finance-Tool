import { auth } from "@clerk/nextjs/server";
import { redirect } from "next/navigation";

import { clerkEnabled } from "@/lib/clerk";

/**
 * Redirect a signed-out visitor to sign-in, from inside the page itself.
 *
 * WHY THIS EXISTS RATHER THAN A ROUTE MATCHER
 * -------------------------------------------
 * `proxy.ts` used to be the only thing marking a page as private, via
 * `createRouteMatcher(["/dashboard(.*)"])`. Clerk now deprecates that pattern,
 * and the reason they give turned out to be true of this app:
 *
 *   "Middleware-based auth checks rely on path matching, which can diverge
 *    from how Next.js routes requests and leave protected resources
 *    reachable."
 *
 * It had already diverged. `/transactions` and `/insights` were added in later
 * milestones and nobody updated the matcher, so neither was protected. A
 * signed-out visitor did not see anyone's data -- the FastAPI token check is
 * the real boundary and returned 401 -- but they got a page rendering the
 * error "No active session." instead of being sent to sign in.
 *
 * A check that lives in the page cannot drift from the page. Adding a new
 * private page means writing this line in it; forgetting means the page is
 * obviously broken for you during development, rather than quietly public.
 *
 * This is still a user-experience control, not a security boundary. The
 * backend verifies the token on every request no matter who is asking, and
 * that is what actually protects the data.
 */
export async function requireSignedIn(): Promise<void> {
  if (!clerkEnabled) return;

  const { userId } = await auth();
  if (!userId) {
    redirect("/sign-in");
  }
}
