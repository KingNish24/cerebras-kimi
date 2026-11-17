#!/usr/bin/env node
/**
 * `npm start` — bring up the whole app: frontend + the ACP backend agent.
 *
 * Auto-detects the mode: if `.next/BUILD_ID` exists (a production build is
 * present) it runs `next start`; otherwise it runs the dev server via
 * scripts/dev.mjs. The backend is a stdio child process spawned lazily by
 * lib/acp-bridge.ts on the first /api/rpc call, so we warm it with an
 * `initialize` request once the server is listening.
 */

import { spawn } from "node:child_process";
import { existsSync } from "node:fs";
import path from "node:path";
import { fileURLToPath } from "node:url";

const WEB_DIR = path.join(path.dirname(fileURLToPath(import.meta.url)), "..");
const PORT = process.env.PORT ?? "3000";
const BASE = `http://localhost:${PORT}`;
const IS_PROD = existsSync(path.join(WEB_DIR, ".next", "BUILD_ID"));

const args = IS_PROD
  ? [path.join(WEB_DIR, "node_modules", "next", "dist", "bin", "next"), "start"]
  : ["./scripts/dev.mjs"];

const app = spawn(process.execPath, args, {
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

app.on("exit", (code) => process.exit(code ?? 0));
process.on("SIGINT", () => app.kill("SIGINT"));
process.on("SIGTERM", () => app.kill("SIGTERM"));

waitForServer()
  .then((ok) => {
    if (ok) {
      console.log(
        `[start-all] ${IS_PROD ? "production" : "dev"} frontend + backend agent ready`,
      );
    } else {
      console.warn("[start-all] server did not come up in time");
    }
  })
  .catch(() => {});
