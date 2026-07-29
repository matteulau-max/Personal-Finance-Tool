/**
 * Next.js Proxy — runs before every matched request.
 *
 * Note the filename. In Next.js 16 this file was renamed from `middleware.ts`
 * to `proxy.ts`; the functionality is identical. Almost every tutorial you
 * will find online still says `middleware.ts`, and a file by that name is
 * simply ignored, which produces the baffling symptom of authentication
 * silently not running at all.
 *
 * WHAT THIS DOES AND DOES NOT DO
 * ------------------------------
 * `clerkMiddleware()` reads the session cookie and makes auth state available
 * to server components. That is now ALL it does.
 *
 * It used to also decide which pages were private, with
 * `createRouteMatcher(["/dashboard(.*)"])`. Clerk deprecated that, and their
 * reason turned out to describe this codebase exactly:
 *
 *   "Middleware-based auth checks rely on path matching, which can diverge
 *    from how Next.js routes requests and leave protected resources
 *    reachable."
 *
 * It had diverged. `/transactions` and `/insights` arrived in later
 * milestones and nobody updated the matcher, so neither was ever protected --
 * a signed-out visitor got a page rendering "No active session." instead of
 * being redirected to sign in. The list of private pages was in a different
 * file from the pages.
 *
 * Each page now calls `requireSignedIn()` itself. See
 * src/lib/require-signed-in.ts.
 *
 * Either way this is a **user experience** control, not a security boundary.
 * It stops someone seeing a broken empty dashboard; it does not stop them
 * calling the API directly with curl. The real enforcement is the token check
 * on the FastAPI side, which runs on every request no matter who is asking.
 */

import { clerkMiddleware } from "@clerk/nextjs/server";
import { NextResponse } from "next/server";

import { clerkEnabled } from "@/lib/clerk";

const clerkProxy = clerkMiddleware();

// When Clerk is not configured, pass every request straight through so the
// app still runs. See src/lib/clerk.ts for why.
export const proxy = clerkEnabled ? clerkProxy : () => NextResponse.next();

export const config = {
  matcher: [
    // Everything except Next.js internals and static files...
    "/((?!_next|[^?]*\\.(?:html?|css|js(?!on)|jpe?g|webp|png|gif|svg|ttf|woff2?|ico|csv|docx?|xlsx?|zip|webmanifest)).*)",
    // ...but always run for API and tRPC routes.
    "/(api|trpc)(.*)",
  ],
};
