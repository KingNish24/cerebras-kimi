export interface SocketMessageEvent {
  data: unknown;
}

export interface TurnSocket {
  readonly readyState: number;
  send(data: string): void;
  close(code?: number, reason?: string): void;
  addEventListener(
    type: "open" | "message" | "close" | "error",
    listener: (event: SocketMessageEvent) => void,
  ): void;
}

export type TurnSocketFactory = (url: string) => TurnSocket;

export const SOCKET_CONNECTING = 0;
export const SOCKET_OPEN = 1;

export function browserSocketFactory(url: string): TurnSocket {
  return new WebSocket(url) as unknown as TurnSocket;
}

/**
 * JSON-RPC transport wrapper for the ACP endpoint (/ws/rpc).
 *
 * Outbound legacy {"type": ...} commands pass through unchanged — the
 * backend accepts them on the RPC socket. Inbound `turn.event`
 * notifications are unwrapped back into raw event payloads so the
 * turn-protocol client sees the same shapes as before; `session/update`
 * notifications surface as `{type:"session_update", ...}`; JSON-RPC
 * responses surface as `{type:"rpc_response", ...}`.
 */
export function rpcSocketFactory(url: string): TurnSocket {
  const inner = new WebSocket(url);
  const messageListeners = new Set<(e: SocketMessageEvent) => void>();

  inner.addEventListener("message", (ev) => {
    let msg: unknown;
    try {
      msg = JSON.parse(String(ev.data));
    } catch {
      return;
    }
    let data: string | null = null;
    if (msg && typeof msg === "object") {
      const m = msg as Record<string, unknown>;
      if (m.method === "turn.event" && m.params) {
        data = JSON.stringify(m.params);
      } else if (m.method === "session/update" && m.params) {
        data = JSON.stringify({ type: "session_update", ...(m.params as object) });
      } else if ("id" in m && ("result" in m || "error" in m)) {
        data = JSON.stringify({ type: "rpc_response", ...m });
      }
    }
    if (data !== null) {
      const event = { data } as SocketMessageEvent;
      messageListeners.forEach((fn) => fn(event));
    }
  });

  return {
    get readyState() {
      return inner.readyState;
    },
    send: (data: string) => inner.send(data),
    close: (code?: number, reason?: string) => inner.close(code, reason),
    addEventListener: (type, listener) => {
      if (type === "message") {
        messageListeners.add(listener);
      } else {
        inner.addEventListener(type, listener as unknown as EventListener);
      }
    },
  } as TurnSocket;
}
