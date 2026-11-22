import { readFileSync } from "node:fs";
import { spawn } from "node:child_process";
import { pathToFileURL } from "node:url";

/** Read only the service credential. Never export the data encryption key. */
export function backendToken(configFile) {
  const lines = readFileSync(configFile, "utf8").split(/\r?\n/);
  const matches = lines.filter((line) => line.startsWith("AUTOHUNT_API_KEY="));
  if (matches.length !== 1) {
    throw new Error("Shared configuration must contain one service token.");
  }
  const token = matches[0].slice("AUTOHUNT_API_KEY=".length).trim();
  if (!token || token.includes("\0")) {
    throw new Error("Shared service token is invalid.");
  }
  return token;
}

export function startStandaloneServer(configFile, serverFile) {
  let token;
  try {
    token = backendToken(configFile);
  } catch {
    // Filesystem errors and malformed files must not dump credential contents.
    console.error("AutoHunt frontend could not read its shared service credential.");
    process.exitCode = 1;
    return;
  }
  const env = { ...process.env, AUTOHUNT_BACKEND_TOKEN: token };
  delete env.AUTOHUNT_API_KEY;
  delete env.AUTOHUNT_DATA_KEY;
  const server = spawn(process.execPath, [serverFile], { env, stdio: "inherit" });
  for (const signal of ["SIGTERM", "SIGINT"]) {
    process.on(signal, () => server.kill(signal));
  }
  server.on("error", () => {
    console.error("AutoHunt frontend server could not start.");
    process.exitCode = 1;
  });
  server.on("exit", (code, signal) => {
    process.exitCode = code ?? (signal === "SIGINT" ? 130 : 143);
  });
  return server;
}

if (process.argv[1] && import.meta.url === pathToFileURL(process.argv[1]).href) {
  startStandaloneServer(
    process.env.AUTOHUNT_CONFIG_FILE ?? "/run/autohunt-config/runtime.env",
    "/app/frontend/server.js",
  );
}
