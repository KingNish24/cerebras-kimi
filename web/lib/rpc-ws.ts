/**
 * WebSocket-compatible socket over the backend's REST + SSE transport.
 * Drop-in for `new WebSocket(wsUrl(path))`: send() POSTs JSON to the realtime
 * command endpoint; inbound frames arrive as parsed JSON events over SSE.
 */

import { sseSocketFactory, type SseSocketLike } from "@/lib/sse-socket";

type MessageListener = (event: { data: string }) => void;

const OPEN = 1;

export class RpcSocket {
  readyState = 0;
  onopen: (() => void) | null = null;
  onmessage: MessageListener | null = null;
  onclose: (() => void) | null = null;
  onerror: ((e: unknown) => void) | null = null;

  private readonly socket: SseSocketLike;
  private closed = false;

  constructor(path: string) {
    this.socket = sseSocketFactory(path);
    this.socket.onopen = () => {
      if (this.closed) return;
      this.readyState = OPEN;
      this.onopen?.();
    };
    this.socket.onmessage = (ev) => {
      if (this.closed) return;
      this.onmessage?.({ data: ev.data });
    };
    this.socket.onclose = () => {
      if (this.closed) return;
      this.readyState = 3;
      this.onclose?.();
    };
    this.socket.onerror = (e) => this.onerror?.(e);
  }

  send(data: string) {
    if (this.readyState === OPEN) this.socket.send(data);
  }

  close() {
    this.closed = true;
    this.socket.close();
  }
}
