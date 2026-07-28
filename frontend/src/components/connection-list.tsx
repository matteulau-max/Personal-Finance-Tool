"use client";

/**
 * Connected institutions, with per-connection Sync and Disconnect.
 *
 * A client component because the buttons need pending state. `useTransition`
 * is React's way of tracking an in-flight Server Action without inventing a
 * loading flag of your own -- `isPending` is true for exactly as long as the
 * action runs, including the re-render it triggers.
 */

import { useState, useTransition } from "react";

import { disconnectItem, syncItem } from "@/app/dashboard/actions";

export type Connection = {
  id: string;
  status: string;
  institution: { name: string; logo_url: string | null } | null;
  last_successful_sync_at: string | null;
  error_code: string | null;
};

function statusLabel(connection: Connection): {
  text: string;
  className: string;
} {
  if (connection.status === "login_required") {
    return {
      text: "Reconnect needed",
      className: "text-amber-600 dark:text-amber-400",
    };
  }
  if (connection.status === "error") {
    return { text: "Error", className: "text-red-600 dark:text-red-400" };
  }
  return { text: "Connected", className: "text-green-600 dark:text-green-400" };
}

function formatSyncTime(value: string | null): string {
  if (!value) return "never synced";
  return `synced ${new Date(value).toLocaleString()}`;
}

export function ConnectionList({ connections }: { connections: Connection[] }) {
  const [isPending, startTransition] = useTransition();
  const [message, setMessage] = useState<string | null>(null);

  if (connections.length === 0) {
    return (
      <p className="text-sm text-zinc-600 dark:text-zinc-400">
        No banks connected yet.
      </p>
    );
  }

  const run = (action: () => Promise<{ ok: boolean; error?: string }>) => {
    setMessage(null);
    startTransition(async () => {
      const result = await action();
      if (!result.ok) setMessage(result.error ?? "Something went wrong.");
    });
  };

  return (
    <div className="flex flex-col gap-3">
      <ul className="flex flex-col divide-y divide-zinc-200 dark:divide-zinc-800">
        {connections.map((connection) => {
          const status = statusLabel(connection);
          return (
            <li
              key={connection.id}
              className="flex flex-wrap items-center justify-between gap-3 py-3"
            >
              <div>
                <p className="font-medium">
                  {connection.institution?.name ?? "Bank"}
                </p>
                <p className="text-xs text-zinc-500">
                  <span className={status.className}>{status.text}</span>
                  {" · "}
                  {formatSyncTime(connection.last_successful_sync_at)}
                </p>
              </div>

              <div className="flex gap-2">
                <button
                  type="button"
                  disabled={isPending}
                  onClick={() => run(() => syncItem(connection.id))}
                  className="rounded border border-zinc-300 px-3 py-1 text-xs disabled:opacity-50 dark:border-zinc-700"
                >
                  {isPending ? "Working…" : "Sync now"}
                </button>
                <button
                  type="button"
                  disabled={isPending}
                  onClick={() => {
                    // Disconnecting stops future syncing. It does NOT delete
                    // history -- worth saying plainly, because users assume
                    // the opposite and would otherwise avoid the button.
                    if (
                      window.confirm(
                        "Stop syncing this bank? Your existing transaction history is kept.",
                      )
                    ) {
                      run(() => disconnectItem(connection.id));
                    }
                  }}
                  className="rounded border border-zinc-300 px-3 py-1 text-xs text-red-600 disabled:opacity-50 dark:border-zinc-700 dark:text-red-400"
                >
                  Disconnect
                </button>
              </div>
            </li>
          );
        })}
      </ul>

      {message && (
        <p className="text-sm text-amber-700 dark:text-amber-300">{message}</p>
      )}
    </div>
  );
}
