/**
 * Whether Clerk is configured on this deployment.
 *
 * Why this exists: `<ClerkProvider>` and `clerkMiddleware()` both throw at
 * startup if no publishable key is present. Without a guard, the entire app --
 * including the public landing page -- would be unrunnable until you finish
 * signing up for Clerk. That makes the project impossible to clone and start,
 * and it makes CI impossible without secrets.
 *
 * So we degrade: no keys means no auth UI, and a visible banner explaining
 * what to do. The moment the keys appear, everything switches on.
 *
 * The environment variable is read via the full `process.env.X` expression
 * rather than destructured, because Next.js replaces that exact text at build
 * time. `const { X } = process.env` is NOT replaced and would be undefined in
 * the browser.
 */
export const clerkEnabled = Boolean(
  process.env.NEXT_PUBLIC_CLERK_PUBLISHABLE_KEY,
);
