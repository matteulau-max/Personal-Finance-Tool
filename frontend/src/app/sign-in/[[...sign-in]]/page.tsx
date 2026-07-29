import { SignIn } from "@clerk/nextjs";

import { clerkEnabled } from "@/lib/clerk";

/**
 * The folder name `[[...sign-in]]` is a Next.js **optional catch-all route**.
 *
 * Reading it: `[...x]` catches `/sign-in/anything/here`; the extra brackets in
 * `[[...x]]` also make it match the bare `/sign-in`. Clerk needs this because
 * it appends its own path segments during multi-step flows -- verifying an
 * email, entering an MFA code, resetting a password. A plain `page.tsx` would
 * 404 the moment sign-in required a second step.
 */
export default function SignInPage() {
  // Clerk's components require <ClerkProvider>, which is absent until keys
  // are configured. Rendering them anyway would break `next build`.
  if (!clerkEnabled) {
    return (
      <main className="flex min-h-screen items-center justify-center p-6 text-sm text-zinc-600 dark:text-zinc-400">
        Authentication is not configured yet.
      </main>
    );
  }

  return (
    <main className="flex min-h-screen items-center justify-center p-6">
      <SignIn />
    </main>
  );
}
