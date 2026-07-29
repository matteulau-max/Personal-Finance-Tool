import { SignUp } from "@clerk/nextjs";

import { clerkEnabled } from "@/lib/clerk";

// Optional catch-all, for the same reason as the sign-in route.
export default function SignUpPage() {
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
      <SignUp />
    </main>
  );
}
