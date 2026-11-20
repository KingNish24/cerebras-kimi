import fs from "node:fs";
import path from "node:path";
import { fileURLToPath } from "node:url";
import { defineConfig, type Plugin } from "vite";
import react from "@vitejs/plugin-react";

const __dirname = path.dirname(fileURLToPath(import.meta.url));

function readJsonFile(filePath: string): Record<string, unknown> {
  try {
    return JSON.parse(fs.readFileSync(filePath, "utf8")) as Record<
      string,
      unknown
    >;
  } catch {
    return {};
  }
}

function firstNonEmpty(...values: unknown[]): string {
  for (const value of values) {
    if (value !== undefined && value !== null && String(value).trim() !== "") {
      return String(value).trim();
    }
  }
  return "";
}

function normalizeBoolean(value: string): string {
  if (value === "__NEXT_PUBLIC_AUTH_ENABLED_PLACEHOLDER__") {
    return value;
  }
  return ["1", "true", "yes", "on"].includes(value.trim().toLowerCase())
    ? "true"
    : "false";
}

const SETTINGS_DIR = path.resolve(__dirname, "..", "data", "user", "settings");
const SYSTEM_SETTINGS = readJsonFile(path.join(SETTINGS_DIR, "system.json"));
const AUTH_SETTINGS = readJsonFile(path.join(SETTINGS_DIR, "auth.json"));

const BACKEND_PORT = firstNonEmpty(
  process.env.BACKEND_PORT,
  SYSTEM_SETTINGS.backend_port,
  "8001",
);

const NEXT_PUBLIC_API_BASE = firstNonEmpty(
  process.env.NEXT_PUBLIC_API_BASE_EXTERNAL,
  SYSTEM_SETTINGS.next_public_api_base_external,
  process.env.NEXT_PUBLIC_API_BASE,
  SYSTEM_SETTINGS.next_public_api_base,
  `http://localhost:${BACKEND_PORT}`,
);

const NEXT_PUBLIC_AUTH_ENABLED = normalizeBoolean(
  firstNonEmpty(
    process.env.NEXT_PUBLIC_AUTH_ENABLED,
    process.env.AUTH_ENABLED,
    AUTH_SETTINGS.enabled,
    "false",
  ),
);

const APP_VERSION = (() => {
  try {
    const text = fs.readFileSync(
      path.resolve(__dirname, "..", "deeptutor", "__version__.py"),
      "utf8",
    );
    const match = text.match(/__version__\s*=\s*["']([^"']+)["']/);
    if (match) return match[1];
  } catch {
    // ignore missing version file
  }
  return "";
})();

/**
 * Mirrors scripts/compact-locale-loader.cjs for Vite builds: stores identity
 * English locale keys once in the bundle instead of repeating key+value pairs.
 */
function compactLocalePlugin(): Plugin {
  const targetPattern = /locales[/\\]en[/\\]app\.json$/;
  const virtualPrefix = "\0deeptutor-compact-locale:";
  return {
    name: "deeptutor-compact-locale",
    enforce: "pre",
    async resolveId(source, importer, options) {
      const resolved = await this.resolve(source, importer, {
        ...options,
        skipSelf: true,
      });
      if (!resolved) return null;
      const cleanId = resolved.id.split("?")[0];
      if (!targetPattern.test(cleanId)) return null;
      return `${virtualPrefix}${cleanId}.js`;
    },
    load(id) {
      if (!id.startsWith(virtualPrefix)) return null;
      const filePath = id.slice(virtualPrefix.length).replace(/\.js$/, "");
      const source = fs.readFileSync(filePath, "utf8");
      const identities: string[] = [];
      const translations: [string, unknown][] = [];
      for (const [key, value] of Object.entries(
        JSON.parse(source) as Record<string, unknown>,
      )) {
        if (key === value) identities.push(key);
        else translations.push([key, value]);
      }
      return `export default Object.fromEntries([
  ...${JSON.stringify(identities)}.map((key) => [key, key]),
  ...${JSON.stringify(translations)},
]);`;
    },
  };
}

const BACKEND_ORIGIN = `http://127.0.0.1:${BACKEND_PORT}`;

export default defineConfig({
  plugins: [
    compactLocalePlugin(),
    react(),
  ],
  resolve: {
    alias: {
      "@": path.resolve(__dirname, "."),
      cytoscape: path.resolve(
        __dirname,
        "node_modules/cytoscape/dist/cytoscape.cjs.js",
      ),
    },
  },
  define: {
    "process.env.NEXT_PUBLIC_APP_VERSION": JSON.stringify(APP_VERSION),
    "process.env.NEXT_PUBLIC_API_BASE": JSON.stringify(NEXT_PUBLIC_API_BASE),
    "process.env.NEXT_PUBLIC_AUTH_ENABLED": JSON.stringify(
      NEXT_PUBLIC_AUTH_ENABLED,
    ),
  },
  server: {
    host: true,
    proxy: {
      "/api": {
        target: BACKEND_ORIGIN,
        changeOrigin: true,
        ws: true,
      },
      "/files": {
        target: BACKEND_ORIGIN,
        changeOrigin: true,
      },
    },
  },
});
