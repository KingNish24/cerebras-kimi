import { NextRequest, NextResponse } from "next/server";
import { acpRequest, type JsonRpcMessage } from "@/lib/acp-bridge";

export const runtime = "nodejs";
export const dynamic = "force-dynamic";

/** POST /api/rpc — JSON-RPC 2.0 over HTTP, bridged to the stdio ACP agent. */
export async function POST(req: NextRequest) {
  let payload: unknown;
  try {
    payload = await req.json();
  } catch {
    return NextResponse.json(
      { jsonrpc: "2.0", id: null, error: { code: -32700, message: "Parse error" } },
      { status: 200 },
    );
  }

  const messages = Array.isArray(payload) ? payload : [payload];
  const results: JsonRpcMessage[] = [];

  for (const msg of messages) {
    const m = msg as JsonRpcMessage;
    if (!m || m.jsonrpc !== "2.0" || typeof m.method !== "string") {
      results.push({
        jsonrpc: "2.0",
        id: ((m as { id?: unknown })?.id ?? null) as string | number | null,
        error: { code: -32600, message: "Invalid JSON-RPC request" },
      } satisfies JsonRpcMessage);
      continue;
    }
    if ("id" in m) {
      results.push(await acpRequest(m.method, m.params ?? {}));
    } else {
      // Notification: forward without awaiting a response.
      const { acpNotify } = await import("@/lib/acp-bridge");
      acpNotify(m.method, m.params ?? {});
    }
  }

  return NextResponse.json(Array.isArray(payload) ? results : results[0] ?? null);
}
