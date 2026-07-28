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
 * to server components. `auth.protect()` redirects signed-out visitors away
 * from private pages.
 *
 * This is a **user experience** control, not a security boundary. It stops
 * someone seeing a broken empty dashboard; it does not stop them calling the
 * API directly with curl. The real enforcement is the token check on the
 * FastAPI side, which runs on every request no matter who is asking.
 *
 * Next.js documents this explicitly: proxy is for optimistic checks, not for
 * authorization. Never let it be the only thing standing between a request
 * and someone's data.
 */

import { clerkMiddleware, createRouteMatcher } from "@clerk/nextjs/server";
import { NextResponse } from "next/server";

import { clerkEnabled } from "@/lib/clerk";

// Routes that require a signed-in user. Everything else stays public.
const isProtectedRoute = createRouteMatcher(["/dashboard(.*)"]);

const clerkProxy = clerkMiddleware(async (auth, request) => {
  if (isProtectedRoute(request)) {
    await auth.protect();
  }
});

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
