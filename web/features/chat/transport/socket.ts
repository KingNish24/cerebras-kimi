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
 * ACP transport over HTTP: commands go out as JSON-RPC requests via
 * POST /api/rpc; agent->client traffic (session/update, turn.event)
 * arrives on the SSE stream at /api/rpc/stream.
 *
 * Legacy {"type": ...} command dicts are translated to their turn.*
 * JSON-RPC method equivalents; inbound turn.event notifications are
 * unwrapped back into raw protocol events.
 */
const LEGACY_TO_METHOD: Record<string, string> = {
  start_turn: "turn.start",
  subscribe_turn: "turn.subscribe",
  subscribe_session: "turn.subscribe_session",
  resume_from: "turn.resume_from",
  unsubscribe: "turn.unsubscribe",
  cancel_turn: "turn.cancel",
  regenerate: "turn.regenerate",
  check_active_turn: "turn.check_active",
  ping: "turn.check_active", // ping handled server-side as pong via turn.event
};

let rpcSocketSeq = 0;

export function rpcSocketFactory(_url: string): TurnSocket {
  const messageListeners = new Set<(e: SocketMessageEvent) => void>();
  const stateListeners = new Map<string, Set<(e: Event) => void>>();
  let closed = false;

  const es = new EventSource("/api/rpc/stream");
  es.onmessage = (ev) => {
    let msg: any;
    try {
      msg = JSON.parse(ev.data);
    } catch {
      return;
    }
    let data: string | null = null;
    if (msg && msg.method === "turn.event" && msg.params) {
      data = JSON.stringify(msg.params);
    } else if (msg && msg.method === "session/update" && msg.params) {
      data = JSON.stringify({ type: "session_update", ...(msg.params as object) });
    } else if (msg && ("id" in msg) && ("result" in msg || "error" in msg)) {
      data = JSON.stringify({ type: "rpc_response", ...msg });
    }
    if (data !== null) {
      const event = { data } as SocketMessageEvent;
      messageListeners.forEach((fn) => fn(event));
    }
  };
  const fire = (type: string) =>
    stateListeners.get(type)?.forEach((fn) => fn(new Event(type)));
  es.onopen = () => fire("open");
  es.onerror = () => { if (es.readyState === EventSource.CLOSED) fire("close"); };

  const socket: TurnSocket = {
    get readyState() {
      return closed ? 3 : es.readyState === EventSource.OPEN ? 1 : 0;
    },
    send(data: string) {
      let payload: any;
      try {
        payload = JSON.parse(data);
      } catch {
        return;
      }
      const type = payload?.type as string | undefined;
      const method = type ? LEGACY_TO_METHOD[type] ?? null : null;
      if (!method) return;
      const { type: _t, ...params } = payload;
      const body = {
        jsonrpc: "2.0",
        id: `c${++rpcSocketSeq}`,
        method,
        params,
      };
      void fetch("/api/rpc", {
        method: "POST",
        headers: { "content-type": "application/json" },
        body: JSON.stringify(body),
      }).catch(() => {});
    },
    close() {
      closed = true;
      es.close();
      fire("close");
    },
    addEventListener(type: string, listener: (e: never) => void) {
      if (type === "message") {
        messageListeners.add(listener as (e: SocketMessageEvent) => void);
      } else {
        if (!stateListeners.has(type)) stateListeners.set(type, new Set());
        stateListeners.get(type)!.add(listener as (e: Event) => void);
      }
    },
  } as TurnSocket;
  return socket;
}
