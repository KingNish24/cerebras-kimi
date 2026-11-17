import { NextRequest } from "next/server";
import { subscribeNotifications, subscribeChannel, warmAgent } from "@/lib/acp-bridge";

export const runtime = "nodejs";
export const dynamic = "force-dynamic";

/** GET /api/rpc/stream — SSE feed of agent->client JSON-RPC notifications
 *  (session/update, turn.event). Replaces the old /ws transport. */
export async function GET(req: NextRequest) {
  warmAgent();
  const channel = req.nextUrl.searchParams.get("channel");
  const encoder = new TextEncoder();
  let unsubscribe: (() => void) | null = null;

  const stream = new ReadableStream<Uint8Array>({
    start(controller) {
      // Flush headers immediately — without a first byte some servers/proxies
      // hold the response and EventSource stays CONNECTING forever.
      try {
        controller.enqueue(encoder.encode(`: connected\n\n`));
      } catch {
        /* closed */
      }
      const send = (data: string) => {
        try {
          controller.enqueue(encoder.encode(`data: ${data}\n\n`));
        } catch {
          /* closed */
        }
      };
      const unsubs: Array<() => void> = [];
      unsubscribe = () => unsubs.forEach((u) => u());
      if (channel) {
        // Named stream channel (book/quiz/progress producers publish here).
        unsubs.push(subscribeChannel(channel, (data) => send(JSON.stringify(data))));
      } else {
        unsubs.push(subscribeNotifications((msg) => {
          send(JSON.stringify(msg));
        }));
      }
      // Heartbeat keeps proxies from idling the stream out.
      const hb = setInterval(() => {
        try {
          controller.enqueue(encoder.encode(`: hb\n\n`));
        } catch {
          clearInterval(hb);
        }
      }, 25_000);
      req.signal.addEventListener("abort", () => {
        clearInterval(hb);
        unsubscribe?.();
        try {
          controller.close();
        } catch {
          /* already closed */
        }
      });
    },
    cancel() {
      unsubscribe?.();
    },
  });

  return new Response(stream, {
    headers: {
      "content-type": "text/event-stream",
      "cache-control": "no-cache, no-transform",
      connection: "keep-alive",
    },
  });
}
