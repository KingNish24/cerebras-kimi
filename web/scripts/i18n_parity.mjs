import fs from "node:fs";
import path from "node:path";

const webRoot = path.resolve(process.cwd());
const enRoot = path.join(webRoot, "locales", "en");

if (!fs.existsSync(enRoot)) {
  console.error(`[i18n:parity] Missing locale root: ${enRoot}`);
  process.exit(2);
}

console.log("[i18n:parity] OK");
