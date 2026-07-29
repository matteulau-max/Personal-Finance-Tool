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
 * full URLs -- no scheme, no trailing slash.
 *
 * This is derived from the environment rather than hard-coded, because the
 * Codespace name differs per person and per rebuild. On a normal machine
 * neither variable is set, the list stays empty, and the default
 * same-origin-only rule applies untouched.
 *
 * Do not add a wildcard such as `*.app.github.dev` here: that would trust
 * every Codespace belonging to every GitHub user, which is precisely the
 * cross-origin case the check exists to stop.
 */
const codespaceName = process.env.CODESPACE_NAME;
const codespaceDomain =
  process.env.GITHUB_CODESPACES_PORT_FORWARDING_DOMAIN ?? "app.github.dev";

const allowedOrigins = codespaceName
  ? [`${codespaceName}-3000.${codespaceDomain}`]
  : [];

const nextConfig: NextConfig = {
  experimental: {
    serverActions: { allowedOrigins },
  },
};

export default nextConfig;
