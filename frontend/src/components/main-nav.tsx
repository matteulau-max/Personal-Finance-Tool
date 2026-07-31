import Link from "next/link";

/**
 * The links between the app's pages.
 *
 * Extracted rather than repeated: with six pages, an inline nav is the same
 * list written six times, and the failure mode is silent -- adding a page
 * means editing five other files, and forgetting one leaves a page that
 * cannot be reached from wherever you happen to be standing. That had already
 * started: `/dashboard` linked to Insights and Transactions, while
 * `/insights` linked back to Dashboard and Transactions, and neither knew
 * about anything else.
 *
 * `current` marks the page you are on. It is rendered as plain text rather
 * than a link to itself, and carries `aria-current="page"` so the distinction
 * is available to a screen reader and not only to someone who can see that
 * one item is not underlined.
 */

const LINKS = [
  { href: "/dashboard", label: "Dashboard" },
  { href: "/accounts", label: "Accounts" },
  { href: "/transactions", label: "Transactions" },
  { href: "/cashflow", label: "Cash flow" },
  { href: "/forecast", label: "Forecast" },
  { href: "/insights", label: "Insights" },
] as const;

export function MainNav({ current }: { current?: string }) {
  return (
    <nav
      aria-label="Main"
      className="flex flex-wrap items-center gap-x-4 gap-y-1 text-sm"
    >
      {LINKS.map((link) =>
        link.href === current ? (
          <span
            key={link.href}
            aria-current="page"
            className="font-medium text-zinc-900 dark:text-zinc-100"
          >
            {link.label}
          </span>
        ) : (
          <Link
            key={link.href}
            href={link.href}
            className="text-zinc-600 underline hover:text-zinc-900 dark:text-zinc-400 dark:hover:text-zinc-100"
          >
            {link.label}
          </Link>
        ),
      )}
    </nav>
  );
}
