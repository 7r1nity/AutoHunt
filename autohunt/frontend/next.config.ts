import type { NextConfig } from "next";
import path from "node:path";

const nextConfig: NextConfig = {
  reactStrictMode: true,
  devIndicators: false,
  output: process.env.AUTOHUNT_DOCKER_BUILD === "1" ? "standalone" : undefined,
  outputFileTracingRoot: path.join(__dirname, ".."),
};

export default nextConfig;
