/**
 * WebSocket-compatible socket backed by JSON-RPC + SSE.
 * Drop-in for `new WebSocket(wsUrl(path))`: send() POSTs the payload as a
 * `legacy.send` RPC; inbound frames arrive on `/api/rpc/stream?channel=`.
 */

type MessageListener = (event: { data: string }) => void;

const OPEN = 1;

export class RpcSocket {
  readyState = 0;
  onopen: (() => void) | null = null;
  onmessage: MessageListener | null = null;
  onclose: (() => void) | null = null;
  onerror: ((e: unknown) => void) | null = null;

  private es: EventSource | null = null;
  private readonly listeners = new Set<MessageListener>();
  private closed = false;

  constructor(private readonly path: string) {
    // Derive a stable channel name from the ws path.
    this.channel = "ws" + path.replace(/[^a-zA-Z0-9]+/g, "_");
    this.connect();
  }

  private readonly channel: string;

  private connect() {
    const es = new EventSource(`/api/rpc/stream?channel=${encodeURIComponent(this.channel)}`);
    this.es = es;
    es.onopen = () => {
      if (this.closed) return;
      this.readyState = OPEN;
      this.onopen?.();
    };
    es.onmessage = (ev) => {
      const data = typeof ev.data === "string" ? ev.data : "";
      const evt = { data };
      this.onmessage?.(evt);
      this.listeners.forEach((fn) => fn(evt));
    };
    es.onerror = (e) => {
      if (es.readyState === EventSource.CLOSED) {
        this.readyState = 3;
        this.onclose?.();
      } else {
        this.onerror?.(e);
      }
    };
  }

  send(data: string) {
    void fetch("/api/rpc", {
      method: "POST",
      headers: { "content-type": "application/json" },
      body: JSON.stringify({
        jsonrpc: "2.0",
        id: `ws-${Date.now()}-${Math.random().toString(36).slice(2, 8)}`,
        method: "legacy.send",
        params: { channel: this.channel, message: data },
      }),
    }).catch(() => this.onerror?.(new Error("rpc send failed")));
  }

  addEventListener(type: string, fn: MessageListener) {
    if (type === "message") this.listeners.add(fn);
  }
  removeEventListener(type: string, fn: MessageListener) {
    if (type === "message") this.listeners.delete(fn);
  }

  close() {
    this.closed = true;
    this.readyState = 3;
    this.es?.close();
    this.onclose?.();
  }
}
