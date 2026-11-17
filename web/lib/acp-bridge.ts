/**
 * ACP stdio bridge — spawns the Python agent (`python -m app.acp_stdio`)
 * once per Next.js server process and forwards JSON-RPC 2.0 messages.
 *
 * Canonical ACP transport is stdio ndJSON (same as vtx's acp.py); this
 * module is the only place that knows about the subprocess. Notifications
 * (session/update, turn.event) are fanned out to SSE subscribers.
 */
import { spawn, type ChildProcessByStdio } from "node:child_process";
import type { Writable, Readable } from "node:stream";
import path from "node:path";

export interface JsonRpcMessage {
  jsonrpc?: string;
  id?: string | number | null;
  method?: string;
  params?: unknown;
  result?: unknown;
  error?: { code: number; message: string; data?: unknown };
}

type Subscriber = (msg: JsonRpcMessage) => void;

const BACKEND_DIR = process.env.DEEPTUTOR_BACKEND_DIR ?? path.join(process.cwd(), "..", "backend");
const PYTHON = process.env.DEEPTUTOR_PYTHON ?? path.join(BACKEND_DIR, ".venv", "bin", "python");

type AgentProc = ChildProcessByStdio<Writable, Readable, Readable>;
let child: AgentProc | null = null;
let buffer = "";
let writeChain: Promise<void> = Promise.resolve();
const pending = new Map<string | number, { resolve: (m: JsonRpcMessage) => void; timer: NodeJS.Timeout }>();
const subscribers = new Set<Subscriber>();
let nextId = 1;

function ensureChild(): AgentProc {
  if (child && !child.killed) return child;
  child = spawn(PYTHON, ["-m", "app.acp_stdio"], {
    cwd: BACKEND_DIR,
    env: { ...process.env, PYTHONPATH: BACKEND_DIR },
    stdio: ["pipe", "pipe", "pipe"],
  });
  child.stderr.resume(); // drain agent logs
  child.stdout.setEncoding("utf8");
  child.stdout.on("data", (chunk: string) => {
    buffer += chunk;
    let idx: number;
    while ((idx = buffer.indexOf("\n")) >= 0) {
      const line = buffer.slice(0, idx).trim();
      buffer = buffer.slice(idx + 1);
      if (!line) continue;
      let msg: JsonRpcMessage;
      try {
        msg = JSON.parse(line);
      } catch {
        continue; // non-JSON stdout noise — ignore
      }
      dispatch(msg);
    }
  });
  child.on("exit", () => {
    child = null;
    for (const [, p] of pending) {
      clearTimeout(p.timer);
      p.resolve({ jsonrpc: "2.0", id: null, error: { code: -32603, message: "agent exited" } });
    }
    pending.clear();
  });
  return child;
}

function dispatch(msg: JsonRpcMessage): void {
  // Response to a pending request?
  if (msg.id !== undefined && msg.id !== null && ("result" in msg || "error" in msg)) {
    const p = pending.get(msg.id);
    if (p) {
      pending.delete(msg.id);
      clearTimeout(p.timer);
      p.resolve(msg);
      return;
    }
  }
  // Notification / server-initiated message -> SSE subscribers.
  for (const sub of subscribers) sub(msg);
}

/** Send a JSON-RPC message; returns the response for requests (id set). */
export function acpRequest(method: string, params: unknown, timeoutMs = 10 * 60 * 1000): Promise<JsonRpcMessage> {
  const proc = ensureChild();
  const id = nextId++;
  const msg: JsonRpcMessage = { jsonrpc: "2.0", id, method, params };
  return new Promise((resolve) => {
    const timer = setTimeout(() => {
      pending.delete(id);
      resolve({ jsonrpc: "2.0", id, error: { code: -32603, message: "request timed out" } });
    }, timeoutMs);
    pending.set(id, { resolve, timer });
    writeChain = writeChain.then(
      () => new Promise<void>((res, rej) => {
        try {
          proc.stdin.write(JSON.stringify(msg) + "\n", () => res());
        } catch (e) {
          rej(e);
        }
      }),
    );
    writeChain.catch(() => {});
  });
}

/** Fire-and-forget notification (no id). */
export function acpNotify(method: string, params: unknown): void {
  const proc = ensureChild();
  writeChain = writeChain.then(
    () => new Promise<void>((res) => {
      try {
        proc.stdin.write(JSON.stringify({ jsonrpc: "2.0", method, params }) + "\n", () => res());
      } catch {
        res();
      }
    }),
  );
  writeChain.catch(() => {});
}

/** Subscribe to agent->client notifications (session/update, turn.event). */
export function subscribeNotifications(fn: Subscriber): () => void {
  subscribers.add(fn);
  return () => subscribers.delete(fn);
}

/** Warm the child process (used by the stream route). */
export function warmAgent(): void {
  ensureChild();
}
