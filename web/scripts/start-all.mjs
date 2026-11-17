#!/usr/bin/env node
/**
 * `npm start` — build, then serve production + warm the ACP backend agent.
 *
 * Always runs a fresh `next build` first (no dev mode), then `next start`,
 * then sends `initialize` to /api/rpc so the stdio backend child is spawned
 * and ready before the first page load.
 */

import { spawn } from "node:child_process";
import path from "node:path";
import { fileURLToPath } from "node:url";

const WEB_DIR = path.join(path.dirname(fileURLToPath(import.meta.url)), "..");
const PORT = process.env.PORT ?? "3000";
const BASE = `http://localhost:${PORT}`;
const NEXT_BIN = path.join(WEB_DIR, "node_modules", "next", "dist", "bin", "next");

function run(cmd, args) {
  return new Promise((resolve) => {
    const child = spawn(cmd, args, { cwd: WEB_DIR, stdio: "inherit", env: process.env });
    child.on("exit", (code) => resolve(code ?? 1));
  });
}

async function waitForServer(timeoutMs = 120_000) {
  const deadline = Date.now() + timeoutMs;
  while (Date.now() < deadline) {
    try {
      const res = await fetch(`${BASE}/api/rpc`, {
        method: "POST",
        headers: { "content-type": "application/json" },
        body: JSON.stringify({ jsonrpc: "2.0", id: 0, method: "initialize", params: {} }),
      });
      if (res.ok) return true;
    } catch {
      // not up yet
    }
    await new Promise((r) => setTimeout(r, 1000));
  }
  return false;
}

const buildCode = await run(process.execPath, ["./scripts/build.mjs"]);
if (buildCode !== 0) {
  console.error("[start-all] build failed");
  process.exit(buildCode);
}

const server = spawn(process.execPath, [NEXT_BIN, "start"], {
  cwd: WEB_DIR,
  stdio: "inherit",
  env: process.env,
});

server.on("exit", (code) => process.exit(code ?? 0));
process.on("SIGINT", () => server.kill("SIGINT"));
process.on("SIGTERM", () => server.kill("SIGTERM"));

waitForServer()
  .then((ok) => {
    if (ok) console.log("[start-all] production frontend + backend agent ready");
    else console.warn("[start-all] server did not come up in time");
  })
  .catch(() => {});
