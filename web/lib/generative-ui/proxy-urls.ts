/**
 * Generative-UI widgets are untrusted HTML/JS that load libraries, map tiles
 * and photos from a small set of public CDNs. Browsers with ad blockers or
 * Shields frequently block those third-party requests, which leaves a widget
 * stuck on its "Loading…" overlay forever.
 *
 * The host rewrites those absolute URLs to a same-origin proxy endpoint. Only
 * the hosts below are eligible — everything else is left untouched, and the
 * proxy enforces the same allowlist (see app/api/genui/asset/route.ts).
 */

export const GENUI_ASSET_ROUTE = "/api/genui/asset";

export const GENUI_ALLOWED_HOSTS = [
  "cdn.jsdelivr.net",
  "cdnjs.cloudflare.com",
  "unpkg.com",
  "esm.sh",
  "basemap.nationalmap.gov",
  "commons.wikimedia.org",
  "upload.wikimedia.org",
  "thumb.wikimedia.org",
] as const;

const HOST_PATTERN = GENUI_ALLOWED_HOSTS.map((host) =>
  host.replaceAll(".", "\\."),
).join("|");

// Trailing characters stop at quotes/whitespace/HTML delimiters so a URL
// embedded in JS or markup is rewritten without swallowing surrounding code.
// Note: `{z}/{y}/{x}` tile placeholders are intentionally kept in the match;
// encodeForQuery leaves braces intact so Leaflet can still substitute them.
function absoluteAllowedUrl(): RegExp {
  // `\x60` is a backtick (escaped so this template literal stays valid).
  // Parentheses are allowed inside URLs (Wikimedia filenames contain them);
  // a trailing `)` from a CSS `url(...)` wrapper is stripped in the replacer.
  return new RegExp(
    `https://(?:${HOST_PATTERN})(?:/[^\\s"'<>\\x60]*)?`,
    "gi",
  );
}

function encodeForQuery(url: string): string {
  // encodeURIComponent escapes `{` and `}`; restoring them keeps tile URL
  // templates usable for L.tileLayer after the proxy URL is put in the query.
  return encodeURIComponent(url)
    .replace(/%7B/g, "{")
    .replace(/%7D/g, "}");
}

/** Rewrite allowlisted third-party URLs in generated markup, CSS or JS. */
export function proxyExternalUrls(text: string): string {
  if (!text || typeof text !== "string") return text;
  return text.replace(absoluteAllowedUrl(), (match) => {
    // A CSS `url(...)` wrapper's closing paren would otherwise be swallowed
    // into the encoded URL and produce a 404 at the proxy.
    const suffix = match.endsWith(")") ? ")" : "";
    const url = suffix ? match.slice(0, -1) : match;
    return `${GENUI_ASSET_ROUTE}?url=${encodeForQuery(url)}${suffix}`;
  });
}

export function isAllowedAssetUrl(url: URL): boolean {
  return (
    url.protocol === "https:" &&
    (GENUI_ALLOWED_HOSTS as readonly string[]).includes(url.hostname)
  );
}
