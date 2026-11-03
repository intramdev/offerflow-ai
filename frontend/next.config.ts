import type { NextConfig } from "next";

// Same-origin API: browser calls stay on this host and the server
// forwards them to Django, so session cookies never cross origins.
const backend = process.env.OFFERFLOW_BACKEND_URL ?? "http://backend:8000";

const nextConfig: NextConfig = {
  // Dev resources (client bundles, HMR) load from the page origin; without
  // this, Next blocks them on 127.0.0.1 and pages render dead (SSR HTML
  // with no interactivity, no errors). Localhost is allowed by default.
  allowedDevOrigins: ["127.0.0.1", "localhost"],
  // Ask Next to keep trailing slashes on proxied API calls: Django cannot
  // slash-redirect a POST, so a stripped slash turns every API write into
  // a 500. The backend shim (PreserveApiSlashMiddleware) covers proxies
  // that strip regardless of this setting.
  skipTrailingSlashRedirect: true,
  // Proxy body cap deliberately sits above Django's MAX_UPLOAD_BYTES
  // (200_000_000): Next truncates bodies past its cap, so an exactly
  // matching cap would corrupt a 200_000_001-byte upload into a
  // proxy 500. The 5 MB headroom lets Django — the single decider —
  // see the full body and answer 413 with the exact limit message.
  // Next 16 renamed middleware to proxy; only this spelling may be set.
  experimental: {
    proxyClientMaxBodySize: 205000000,
  },
  async rewrites() {
    return [{ source: "/api/:path*", destination: `${backend}/api/:path*` }];
  },
};

export default nextConfig;
