"use client";

/**
 * Host bridge exposed to sandboxed generative-UI code as
 * `Websandbox.connection.remote.*`. Ported from OpenIntelligentUI's
 * `sandbox-functions.ts`, minus the CopilotKit plumbing: `sendPrompt` is
 * injected by the chat surface, `openLink` is a plain window.open.
 */

export interface SandboxBridgeHandlers {
  /** Send a user message into the chat, as if the learner typed it. */
  onSendPrompt?: (text: string) => void | Promise<void>;
}

function argText(args: unknown): string | null {
  if (!args || typeof args !== "object") return null;
  const text = (args as Record<string, unknown>).text;
  return typeof text === "string" ? text : null;
}

function argUrl(args: unknown): string | null {
  if (!args || typeof args !== "object") return null;
  const url = (args as Record<string, unknown>).url;
  return typeof url === "string" ? url : null;
}

export function isAllowedLinkUrl(url: string): boolean {
  let parsed: URL;
  try {
    parsed = new URL(url);
  } catch {
    return false;
  }
  return parsed.protocol === "https:";
}

export function createSandboxApi(
  handlers: SandboxBridgeHandlers,
): Record<string, (args: unknown) => unknown> {
  return {
    sendPrompt: async (args: unknown) => {
      const text = argText(args)?.trim();
      if (!text || text.length > 4000) {
        throw new Error(
          "sendPrompt: enter a non-empty message of at most 4000 characters.",
        );
      }
      if (!handlers.onSendPrompt) {
        throw new Error(
          "sendPrompt: chat bridge unavailable. Please reload the page and try again.",
        );
      }
      await handlers.onSendPrompt(text);
      return { ok: true };
    },
    openLink: async (args: unknown) => {
      const url = argUrl(args);
      if (!url || !isAllowedLinkUrl(url)) {
        throw new Error(`openLink: url not allowed — ${url ?? "(missing)"}`);
      }
      window.open(url, "_blank", "noopener,noreferrer");
      return { ok: true };
    },
  };
}
