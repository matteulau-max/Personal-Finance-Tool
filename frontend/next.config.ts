import type { NextConfig } from "next";

/**
 * Server Actions and reverse proxies.
 *
 * Next.js protects Server Actions against CSRF by requiring the request's
 * `Origin` to match the `Host` it was served under. Behind a proxy those
 * differ: the browser sends `Origin: https://<name>-3000.app.github.dev`
 * while Next.js, listening on localhost, sees a different host. The request
 * is rejected with "Invalid Server Actions request" -- which reads like a
 * bug in the action, not a host mismatch, and sends you looking in the wrong
 * file entirely.
 *
 * `allowedOrigins` names the extra hosts to trust. It takes host names, not
 * full URLs -- no scheme, no trailing slash, but the port included when there
 * is one, because Next.js compares against `new URL(origin).host`.
 *
 * BOTH ends of the mismatch have to be listed, because which one the browser
 * sends depends on how you opened the app, and both are normal:
 *
 *   - `localhost:3000` -- VS Code forwards the port to your machine, so the
 *     browser genuinely believes it is talking to localhost. Codespaces still
 *     stamps `x-forwarded-host` with the public name, so the pair disagrees.
 *   - `<name>-3000.<domain>` -- you opened the public forwarded URL directly.
 *
 * Listing only the public name looks correct and fails in the common case:
 * the error names the forwarded host, which invites you to allow *that*,
 * when the value needing trust is whatever the browser sent as `origin`.
 *
 * Both are derived from the environment rather than hard-coded, because the
 * Codespace name differs per person and per rebuild. Off a Codespace,
 * CODESPACE_NAME is unset, the list stays empty, and the default
 * same-origin-only rule applies untouched -- including in production, which
 * is why `localhost` appearing here is not a hole in a deployed app.
 *
 * Do not add a wildcard such as `*.app.github.dev`: that would trust every
 * Codespace belonging to every GitHub user, which is precisely the
 * cross-origin case the check exists to stop.
 */
const codespaceName = process.env.CODESPACE_NAME;
const codespaceDomain =
  process.env.GITHUB_CODESPACES_PORT_FORWARDING_DOMAIN ?? "app.github.dev";
const port = process.env.PORT ?? "3000";

const allowedOrigins = codespaceName
  ? [
      `${codespaceName}-${port}.${codespaceDomain}`,
      `localhost:${port}`,
      `127.0.0.1:${port}`,
    ]
  : [];

const nextConfig: NextConfig = {
  experimental: {
    serverActions: { allowedOrigins },
  },
};

export default nextConfig;
