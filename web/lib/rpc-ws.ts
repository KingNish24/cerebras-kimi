/**
 * WebSocket-compatible socket over the Python backend's native WS endpoint.
 * Drop-in for `new WebSocket(wsUrl(path))`: send() writes JSON to the
 * socket; inbound frames arrive as parsed JSON events.
 */

import { backendWsUrl } from "@/shared/api/client";

type MessageListener = (event: { data: string }) => void;

const OPEN = 1;

export class RpcSocket {
  readyState = 0;
  onopen: (() => void) | null = null;
  onmessage: MessageListener | null = null;
  onclose: (() => void) | null = null;
  onerror: ((e: unknown) => void) | null = null;

  private readonly ws: WebSocket;
  private closed = false;

  constructor(path: string) {
    this.ws = new WebSocket(backendWsUrl(path));
    this.ws.onopen = () => {
      if (this.closed) return;
      this.readyState = OPEN;
      this.onopen?.();
    };
    this.ws.onmessage = (ev) => {
      const data = typeof ev.data === "string" ? ev.data : "";
      this.onmessage?.({ data });
    };
    this.ws.onclose = () => {
      this.readyState = 3;
      this.onclose?.();
    };
    this.ws.onerror = (e) => this.onerror?.(e);
  }

  send(data: string) {
    if (this.ws.readyState === OPEN) this.ws.send(data);
  }

  close() {
    this.closed = true;
    this.ws.close();
  }
}
