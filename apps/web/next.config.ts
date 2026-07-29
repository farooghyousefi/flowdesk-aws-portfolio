import type { NextConfig } from "next";

const nextConfig: NextConfig = {
  reactStrictMode: true,
  transpilePackages: [
    "@trading-assistant/shared-types",
    "@trading-assistant/risk-engine",
    "@trading-assistant/trading-engine"
  ],
  async rewrites() {
    const marketService = process.env.MARKET_SERVICE_INTERNAL_URL ?? "http://127.0.0.1:8787";
    return [{ source: "/market-api/:path*", destination: `${marketService}/:path*` }];
  }
};

export default nextConfig;
