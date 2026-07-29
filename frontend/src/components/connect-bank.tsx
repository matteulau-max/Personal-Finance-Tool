"use client";

/**
 * The "Connect a bank" button.
 *
 * `"use client"` is required: Plaid Link manipulates the DOM and manages a
 * popup, which needs browser APIs and React state. Everything else in this
 * app is a Server Component; this is one of the few places that genuinely
 * has to run in the browser.
 *
 * WHAT HAPPENS WHEN YOU CLICK IT
 * ------------------------------
 * 1. Ask our server for a Link token.
 * 2. Plaid's iframe opens. The user picks their bank and logs in THERE --
 *    their credentials never touch our servers or this JavaScript.
 * 3. Plaid hands back a `public_token`: single-use, expires in minutes,
 *    useless without our Plaid secret.
 * 4. We pass it to a Server Action, which exchanges it server-side for the
 *    real access token.
 *
 * The security property worth naming: this component never handles anything
 * more sensitive than a public token. Even a fully compromised browser learns
 * nothing that grants access to a bank account.
 */

import { useCallback, useEffect, useState } from "react";
import { usePlaidLink } from "react-plaid-link";

import { createLinkToken, exchangePublicToken } from "@/app/dashboard/actions";

export function ConnectBank() {
  const [linkToken, setLinkToken] = useState<string | null>(null);
  const [error, setError] = useState<string | null>(null);
  const [busy, setBusy] = useState(false);

  // Fetch a Link token once on mount. Link tokens are short-lived, so there
  // is no value in requesting one before the page is actually open.
  useEffect(() => {
    let cancelled = false;

    createLinkToken().then((result) => {
      // Guard against setting state after the component unmounts, which
      // happens if the user navigates away while the request is in flight.
      if (cancelled) return;
      if (result.ok) {
        setLinkToken(result.linkToken);
      } else {
        setError(result.error);
      }
    });

    return () => {
      cancelled = true;
    };
  }, []);

  // Plaid types `public_token` as `string | null`, so the null case has to be
  // handled rather than assumed away. It can be null when Link finishes in an
  // unusual state -- exchanging null would produce a confusing 422 from our
  // own API instead of a clear message here.
  const onSuccess = useCallback(async (publicToken: string | null) => {
    if (!publicToken) {
      setError("Plaid did not return a token. Please try again.");
      return;
    }

    setBusy(true);
    setError(null);

    const result = await exchangePublicToken(publicToken);
    if (!result.ok) {
      setError(result.error);
    }

    setBusy(false);
  }, []);

  const { open, ready } = usePlaidLink({
    token: linkToken,
    onSuccess,
    onExit: (plaidError) => {
      // A user closing the dialog is not an error -- only report a real one.
      if (plaidError) {
        setError(plaidError.display_message ?? "Bank connection was cancelled.");
      }
    },
  });

  return (
    <div className="flex flex-col gap-2">
      <button
        type="button"
        onClick={() => open()}
        disabled={!ready || !linkToken || busy}
        className="w-fit rounded bg-zinc-900 px-4 py-2 text-sm font-medium text-white disabled:opacity-50 dark:bg-zinc-100 dark:text-zinc-900"
      >
        {busy ? "Linking…" : "Connect a bank"}
      </button>

      {error && (
        <p className="text-sm text-amber-700 dark:text-amber-300">{error}</p>
      )}
    </div>
  );
}
