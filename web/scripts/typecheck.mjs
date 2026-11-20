#!/usr/bin/env node

import { spawnSync } from "node:child_process";
import path from "node:path";
import { fileURLToPath } from "node:url";

const webRoot = path.resolve(path.dirname(fileURLToPath(import.meta.url)), "..");
const rawArgs = process.argv.slice(2);
const useTsc = rawArgs.includes("--tsc");
const forwardedArgs = rawArgs.filter((arg) => arg !== "--tsc" && arg !== "--tsrs");

const binPath = useTsc
  ? path.join(webRoot, "node_modules", "typescript", "bin", "tsc")
  : path.join(webRoot, "node_modules", "tsrs", "bin", "tsrs");

const result = spawnSync(
  process.execPath,
  [binPath, "--noEmit", ...forwardedArgs],
  { cwd: webRoot, stdio: "inherit" },
);

if (result.error) {
  console.error(result.error);
  process.exit(1);
}
process.exit(result.status ?? 1);
