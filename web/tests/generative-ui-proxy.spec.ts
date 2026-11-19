import { describe, expect, it } from "vitest";
import {
  GENUI_ASSET_ROUTE,
  isAllowedAssetUrl,
  proxyExternalUrls,
} from "@/lib/generative-ui/proxy-urls";
import { buildFinalFrameContent } from "@/components/generative-ui/frame-content";

describe("proxyExternalUrls", () => {
  it("rewrites allowlisted CDN, tile and Wikimedia URLs", () => {
    const text = [
      'fetch("https://cdn.jsdelivr.net/npm/leaflet@1.9.4/dist/leaflet.js")',
      '<img src="https://commons.wikimedia.org/wiki/Special:FilePath/Bridge.jpg?width=900">',
      'L.tileLayer("https://basemap.nationalmap.gov/arcgis/rest/services/USGSTopo/MapServer/tile/{z}/{y}/{x}")',
    ].join("\n");
    const out = proxyExternalUrls(text);
    expect(out).toContain(`${GENUI_ASSET_ROUTE}?url=`);
    expect(out).toContain("cdn.jsdelivr.net%2Fnpm%2Fleaflet");
    expect(out).toContain("commons.wikimedia.org%2Fwiki%2FSpecial%3AFilePath");
    // Tile placeholders must survive so L.tileLayer can substitute z/y/x.
    expect(out).toContain("%2F{z}%2F{y}%2F{x}");
  });

  it("keeps parentheses in filenames and strips CSS url() wrappers", () => {
    const out = proxyExternalUrls(
      '<img src="https://upload.wikimedia.org/wikipedia/commons/Boardwalk_2023_(2).jpg?width=900">' +
        '<style>.x{background:url(https://esm.sh/x.png)}</style>',
    );
    expect(out).toContain("Boardwalk_2023_(2).jpg%3Fwidth%3D900");
    // The CSS closing paren must stay outside the query string.
    expect(out).toContain("esm.sh%2Fx.png)");
    expect(out).not.toContain("esm.sh%2Fx.png%29");
  });

  it("leaves unknown hosts and relative URLs untouched", () => {
    const text =
      '<img src="https://evil.example.com/x.png"><img src="/local.png">';
    expect(proxyExternalUrls(text)).toBe(text);
  });
});

describe("isAllowedAssetUrl", () => {
  it("allows only https on the allowlisted hosts", () => {
    expect(isAllowedAssetUrl(new URL("https://cdn.jsdelivr.net/a.js"))).toBe(true);
    expect(isAllowedAssetUrl(new URL("https://basemap.nationalmap.gov/tile/1"))).toBe(true);
    expect(isAllowedAssetUrl(new URL("http://cdn.jsdelivr.net/a.js"))).toBe(false);
    expect(isAllowedAssetUrl(new URL("https://evil.example.com/a.js"))).toBe(false);
  });
});

describe("buildFinalFrameContent", () => {
  it("routes external widget resources through the asset proxy", () => {
    const html =
      '<head></head><body><img src="https://upload.wikimedia.org/a.jpg"></body>';
    const frame = buildFinalFrameContent(html, ".x{background:url(https://esm.sh/x.png)}");
    expect(frame).toContain(`${GENUI_ASSET_ROUTE}?url=`);
    expect(frame).toContain("upload.wikimedia.org%2Fa.jpg");
    expect(frame).toContain("esm.sh%2Fx.png");
    // The app origin must be allowlisted in the sandbox CSP: a srcdoc iframe
    // has an opaque origin, so `self` does not cover the same-origin proxy.
    expect(frame).toContain(window.location.origin);
  });
});
