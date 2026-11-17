#!/usr/bin/env node
/**
 * `npm start` — bring up the whole app: Next.js dev server + the ACP
 * backend agent. The backend is a stdio child process spawned lazily by
 * lib/acp-bridge.ts on the first /api/rpc call, so we warm it with an
 * `initialize` request once the server is listening.
 */

import { spawn } from "node:child_process";
import path from "node:path";
import { fileURLToPath } from "node:url";

const WEB_DIR = path.join(path.dirname(fileURLToPath(import.meta.url)), "..");
const PORT = process.env.PORT ?? "3000";
const BASE = `http://localhost:${PORT}`;

const dev = spawn(process.execPath, ["./scripts/dev.mjs"], {
  cwd: WEB_DIR,
  stdio: "inherit",
  env: process.env,
});

async function waitForServer(timeoutMs = 120_000) {
  const deadline = Date.now() + timeoutMs;
  while (Date.now() < deadline) {
    try {
      const res = await fetch(`${BASE}/api/rpc`, {
        method: "POST",
        headers: { "content-type": "application/json" },
        body: JSON.stringify({
          jsonrpc: "2.0",
          id: 0,
          method: "initialize",
          params: {},
        }),
      });
      if (res.ok) return true;
    } catch {
      // not up yet
    }
    await new Promise((r) => setTimeout(r, 1000));
  }
  return false;
}

// Wait for the server, then warm the ACP backend child.
dev.on("exit", (code) => process.exit(code ?? 0));
process.on("SIGINT", () => dev.kill("SIGINT"));
process.on("SIGTERM", () => dev.kill("SIGTERM"));

waitForServer()
  .then((ok) => {
    if (ok) console.log("[start-all] frontend + backend agent ready");
    else console.warn("[start-all] server did not come up in time");
  })
  .catch(() => {});
