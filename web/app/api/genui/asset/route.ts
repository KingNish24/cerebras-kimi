import { isAllowedAssetUrl } from "@/lib/generative-ui/proxy-urls";

// Runs in the Node runtime so AbortSignal.timeout and streaming upstream
// bodies work consistently. Node's fetch follows redirects only when asked;
// we follow them manually to re-check the allowlist on every hop (SSRF guard).
export const runtime = "nodejs";

const MAX_REDIRECTS = 3;
const USER_AGENT =
  "Mozilla/5.0 (X11; Linux x86_64) AppleWebKit/537.36 (KHTML, like Gecko) DeepTutor/1.0";

function bad(message: string, status: number) {
  return Response.json({ error: { message } }, { status });
}

export async function GET(req: Request) {
  const raw = new URL(req.url).searchParams.get("url");
  if (!raw) return bad("url is required", 400);

  let target: URL;
  try {
    target = new URL(raw);
  } catch {
    return bad("invalid url", 400);
  }
  if (!isAllowedAssetUrl(target)) return bad("host not allowed", 403);

  for (let hop = 0; hop <= MAX_REDIRECTS; hop += 1) {
    let upstream: Response;
    try {
      upstream = await fetch(target, {
        redirect: "manual",
        signal: AbortSignal.timeout(12000),
        headers: { "User-Agent": USER_AGENT },
        cache: "no-store",
      });
    } catch {
      return bad("upstream unavailable", 502);
    }

    if (upstream.status >= 300 && upstream.status < 400) {
      const location = upstream.headers.get("location");
      if (!location) return bad("redirect without location", 502);
      let next: URL;
      try {
        next = new URL(location, target);
      } catch {
        return bad("invalid redirect", 502);
      }
      if (!isAllowedAssetUrl(next)) return bad("redirect host not allowed", 403);
      target = next;
      continue;
    }

    if (!upstream.ok || !upstream.body) {
      return bad(`upstream ${upstream.status}`, 502);
    }

    const contentType =
      upstream.headers.get("content-type") ?? "application/octet-stream";
    return new Response(upstream.body, {
      status: 200,
      headers: {
        "Content-Type": contentType,
        "Cache-Control": "public, max-age=86400",
        "Access-Control-Allow-Origin": "*",
        "X-Content-Type-Options": "nosniff",
      },
    });
  }

  return bad("too many redirects", 502);
}
