import { ApiError, type AppError, type AppErrorScope } from "./errors";
import { browserReturnPath, loginHref } from "../auth/return-url";
import { scopedUrl } from "@/lib/workspace-scope";

export interface RequestOptions extends RequestInit {
  scope?: AppErrorScope;
  skipAuthRedirect?: boolean;
}

let runtimeAuthEnabled = false;

export function apiUrl(path: string): string {
  return scopedUrl(path);
}

export function wsUrl(path: string): string {
  return scopedUrl(path);
}

export function parseAuthEnabled(raw: string | undefined): boolean {
  return /^(1|true|yes|on)$/i.test((raw ?? "").trim());
}

export function setRuntimeAuthEnabled(enabled: boolean): void {
  runtimeAuthEnabled = enabled;
}

let rpcId = 0;

/** JSON-RPC 2.0 call to POST /api/rpc (method: http.request). */
async function rpcCall(
  method: string,
  path: string,
  query: Record<string, string>,
  body: unknown,
  headers: Record<string, string>,
): Promise<{ status: number; body: unknown }> {
  const response = await fetch(scopedUrl("/api/rpc"), {
    method: "POST",
    credentials: "include",
    headers: { "content-type": "application/json" },
    body: JSON.stringify({
      jsonrpc: "2.0",
      id: ++rpcId,
      method: "http.request",
      params: { method, path, query, body, headers },
    }),
  });
  const payload = await response.json().catch(() => null);
  const result = payload?.result;
  if (result && typeof result === "object" && "status" in result) {
    return result as { status: number; body: unknown };
  }
  // RPC-level failure: surface as a 502 with the error message.
  return {
    status: 502,
    body: { detail: payload?.error?.message ?? "RPC call failed" },
  };
}

function requestToRpcParts(
  input: RequestInfo | URL,
  init?: RequestInit,
): { method: string; path: string; query: Record<string, string>; body: unknown; headers: Record<string, string> } | null {
  let urlStr: string;
  if (typeof input === "string") urlStr = input;
  else if (input instanceof URL) urlStr = input.toString();
  else if (input instanceof Request) urlStr = input.url;
  else return null;
  const url = new URL(scopedUrl(urlStr), typeof window !== "undefined" ? window.location.origin : "http://local");
  const method = (init?.method ?? (input instanceof Request ? input.method : "GET")).toUpperCase();
  const query: Record<string, string> = {};
  url.searchParams.forEach((v, k) => { query[k] = v; });
  const headers: Record<string, string> = {};
  const rawHeaders = init?.headers ?? (input instanceof Request ? input.headers : undefined);
  if (rawHeaders) {
    new Headers(rawHeaders).forEach((v, k) => { headers[k] = v; });
  }
  let body: unknown = null;
  const rawBody = init?.body ?? (input instanceof Request ? null : null);
  if (rawBody != null) {
    if (typeof rawBody === "string") {
      try { body = JSON.parse(rawBody); } catch { body = rawBody; }
    } else if (rawBody instanceof FormData) {
      // Multipart -> __multipart__ spec (files base64) for the RPC layer.
      const fields: Record<string, string> = {};
      const files: Array<Record<string, string>> = [];
      rawBody.forEach((value, key) => {
        if (value instanceof File) {
          // File -> base64 happens async; handled by caller via rpcCallForm.
          files.push({ name: key, filename: value.name, content_type: value.type || "application/octet-stream", _file: "" });
        } else {
          fields[key] = String(value);
        }
      });
      body = { __multipart__: { fields, files } };
    } else if (rawBody instanceof Blob) {
      body = { __blob__: { content_type: rawBody.type || "application/octet-stream" } };
    } else if (rawBody instanceof ArrayBuffer) {
      body = { __blob__: { content_type: "application/octet-stream" } };
    }
  }
  return { method, path: url.pathname, query, body, headers };
}

export async function apiFetch(
  input: RequestInfo | URL,
  init?: RequestInit & { skipAuthRedirect?: boolean },
): Promise<Response> {
  const { skipAuthRedirect, ...fetchInit } = init ?? {};

  // JSON-RPC transport: every REST-shaped call becomes an http.request
  // method call against POST /api/rpc. Falls back to plain fetch for
  // inputs we can't serialize (e.g. streaming bodies).
  const parts = requestToRpcParts(input, fetchInit);
  let response: Response;
  if (parts !== null && !(fetchInit.body instanceof ReadableStream)) {
    // Async FormData file encoding (base64) — rare path (kb upload).
    if (parts.body && typeof parts.body === "object" && "__multipart__" in (parts.body as object)) {
      const spec = (parts.body as { __multipart__: { fields: Record<string, string>; files: Array<Record<string, string> & { _file?: File }> } }).__multipart__;
      const rawForm = fetchInit.body;
      if (rawForm instanceof FormData) {
        const files: Array<Record<string, string>> = [];
        let i = 0;
        rawForm.forEach((value, key) => {
          if (value instanceof File) {
            const meta = spec.files[i++];
            // encode below via File.arrayBuffer in rpcCallMultipart
            files.push({ ...meta, _pending: key } as unknown as Record<string, string>);
          }
        });
        // Simpler: encode whole FormData client-side into __multipart__.
        const fields: Record<string, string> = {};
        const fileSpecs: Array<Record<string, string>> = [];
        const entries = Array.from(rawForm.entries());
        for (const [key, value] of entries) {
          if (value instanceof File) {
            const buf = await value.arrayBuffer();
            let bin = "";
            const bytes = new Uint8Array(buf);
            for (let j = 0; j < bytes.length; j += 0x8000) {
              bin += String.fromCharCode(...bytes.subarray(j, j + 0x8000));
            }
            fileSpecs.push({
              name: key, filename: value.name,
              content_type: value.type || "application/octet-stream",
              data_b64: btoa(bin),
            });
          } else {
            fields[key] = String(value);
          }
        }
        parts.body = { __multipart__: { fields, files: fileSpecs } };
      }
    }
    const rpc = await rpcCall(parts.method, parts.path, parts.query, parts.body, parts.headers);
    const isJson = rpc.body !== null && typeof rpc.body === "object";
    response = new Response(
      isJson ? JSON.stringify(rpc.body) : String(rpc.body ?? ""),
      {
        status: rpc.status,
        headers: { "content-type": isJson ? "application/json" : "text/plain; charset=utf-8" },
      },
    );
  } else {
    const scopedInput = typeof input === "string" ? scopedUrl(input)
      : input instanceof URL ? new URL(scopedUrl(input.toString()))
      : new Request(scopedUrl(input.url), input);
    response = await fetch(scopedInput, { credentials: "include", ...fetchInit });
  }

  if (
    response.status === 401 &&
    runtimeAuthEnabled &&
    !skipAuthRedirect &&
    typeof window !== "undefined"
  ) {
    window.location.href = loginHref(browserReturnPath(window.location));
    return new Promise(() => {});
  }

  return response;
}

function correlationId(response: Response): string | undefined {
  return (
    response.headers.get("x-correlation-id") ??
    response.headers.get("x-request-id") ??
    undefined
  );
}

function messageFromBody(body: unknown, fallback: string): string {
  if (!body || typeof body !== "object") return fallback;
  const value = body as Record<string, unknown>;
  if (typeof value.message === "string" && value.message.trim())
    return value.message;
  if (typeof value.detail === "string" && value.detail.trim())
    return value.detail;
  if (value.detail && typeof value.detail === "object") {
    const detail = value.detail as Record<string, unknown>;
    if (typeof detail.message === "string" && detail.message.trim())
      return detail.message;
  }
  return fallback;
}

function normalizedHttpError(
  response: Response,
  body: unknown,
  scope: AppErrorScope,
): AppError {
  const value =
    body && typeof body === "object" ? (body as Record<string, unknown>) : {};
  const detail =
    value.detail && typeof value.detail === "object"
      ? (value.detail as Record<string, unknown>)
      : {};
  let retryable: boolean;
  if (typeof value.retryable === "boolean") {
    retryable = value.retryable;
  } else if (typeof detail.retryable === "boolean") {
    retryable = detail.retryable;
  } else {
    retryable =
      response.status === 408 ||
      response.status === 429 ||
      response.status >= 500;
  }
  return {
    code:
      (typeof value.error_code === "string" && value.error_code) ||
      (typeof detail.error_code === "string" && detail.error_code) ||
      `http_${response.status}`,
    message: messageFromBody(body, response.statusText || "Request failed"),
    retryable,
    scope,
    correlationId:
      (typeof value.correlation_id === "string" && value.correlation_id) ||
      correlationId(response),
    status: response.status,
  };
}

async function responseBody(response: Response): Promise<unknown> {
  const text = await response.text();
  if (!text.trim()) return undefined;
  try {
    return JSON.parse(text) as unknown;
  } catch {
    return text;
  }
}

async function performRequest(
  input: RequestInfo | URL,
  options: RequestOptions = {},
): Promise<{ response: Response; body: unknown }> {
  const { scope = "network", ...init } = options;
  try {
    const response = await apiFetch(input, init);
    const body = await responseBody(response);
    if (!response.ok)
      throw new ApiError(normalizedHttpError(response, body, scope));
    return { response, body };
  } catch (error) {
    if (error instanceof ApiError) throw error;
    const aborted =
      error instanceof DOMException && error.name === "AbortError";
    throw new ApiError(
      {
        code: aborted ? "request_aborted" : "network_error",
        message: aborted
          ? "Request was cancelled"
          : "Unable to reach the server",
        retryable: !aborted,
        scope,
      },
      { cause: error },
    );
  }
}

export async function requestJson<T>(
  input: RequestInfo | URL,
  options: RequestOptions = {},
): Promise<T> {
  const { response, body } = await performRequest(input, options);
  if (body === undefined || typeof body === "string") {
    throw new ApiError({
      code: "invalid_response",
      message: "The server returned an invalid JSON response",
      retryable: true,
      scope: options.scope ?? "network",
      correlationId: correlationId(response),
      status: response.status,
    });
  }
  return body as T;
}

export async function requestVoid(
  input: RequestInfo | URL,
  options: RequestOptions = {},
): Promise<void> {
  await performRequest(input, options);
}

export async function requestBlob(
  input: RequestInfo | URL,
  options: RequestOptions = {},
): Promise<Blob> {
  const { scope = "network", ...init } = options;
  try {
    const response = await apiFetch(input, init);
    if (!response.ok) {
      const body = await responseBody(response);
      throw new ApiError(normalizedHttpError(response, body, scope));
    }
    return await response.blob();
  } catch (error) {
    if (error instanceof ApiError) throw error;
    const aborted =
      error instanceof DOMException && error.name === "AbortError";
    throw new ApiError(
      {
        code: aborted ? "request_aborted" : "network_error",
        message: aborted
          ? "Request was cancelled"
          : "Unable to reach the server",
        retryable: !aborted,
        scope,
      },
      { cause: error },
    );
  }
}

/**
 * Parse a response, throwing FastAPI's ``detail`` as the message on failure.
 *
 * The status line alone ("500 Internal Server Error") tells a reader nothing;
 * the backend's own detail is what names the actual problem. Callers wanting
 * structured refusals (an error code, a log tail) parse the body themselves.
 */
export async function asJsonOrThrow(response: Response): Promise<any> {
  if (!response.ok) {
    let detail = `${response.status} ${response.statusText}`;
    try {
      const body = await response.json();
      if (body?.detail) detail = String(body.detail);
    } catch {
      /* the body was not JSON; the status line is all we have */
    }
    throw new Error(detail);
  }
  return response.json();
}
