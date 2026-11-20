#!/usr/bin/env node

import { spawnSync } from "node:child_process";
import path from "node:path";
import { fileURLToPath } from "node:url";

const webRoot = path.resolve(path.dirname(fileURLToPath(import.meta.url)), "..");
const tscBin = path.join(
  webRoot,
  "node_modules",
  "typescript",
  "bin",
  "tsc",
);

const result = spawnSync(
  process.execPath,
  [tscBin, "--noEmit", ...process.argv.slice(2)],
  { cwd: webRoot, stdio: "inherit" },
);

if (result.error) {
  console.error(result.error);
  process.exit(1);
}
process.exit(result.status ?? 1);
