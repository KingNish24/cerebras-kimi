/**
 * Parser for the generative-UI wire format DeepTutor's assistant emits inside
 * chat text. It is the text-streaming equivalent of OpenIntelligentUI's
 * `generateSandboxedUi` tool arguments: instead of structured tool-call
 * channels, the model writes fenced blocks in the order the UI needs them.
 *
 * Per widget, in order:
 *   ```ogui-meta     {"initialHeight": 320, "placeholderMessages": [...]}
 *   ```ogui-css      all styles up front (preview stays hidden until it closes)
 *   ```ogui-html     body markup; the open fence streams a live preview
 *   ```ogui-js       named function declarations (classic script, no top-level await)
 *   ```ogui-expr     one small synchronous statement per fence, run in order
 *
 * Prose outside the fences renders as normal Markdown. An `ogui-meta` fence
 * opens a new widget; non-whitespace prose between ogui fences also closes the
 * current widget, so several widgets can share one answer.
 */

export interface OguiWidget {
  initialHeight?: number;
  placeholderMessages?: string[];
  css: string;
  cssComplete: boolean;
  htmlComplete: boolean;
  jsFunctions?: string;
  jsFunctionsComplete: boolean;
  /** Completed `ogui-expr` fences, in arrival order. */
  jsExpressions: string[];
  /** Raw html channel; kept as chunks so partial content can stream. */
  html: string;
  generating: boolean;
}

export type OguiSegment =
  | { kind: "prose"; text: string }
  | { kind: "widget"; widget: OguiWidget };

const FENCE_OPEN = /^[ \t]*```ogui-(meta|css|html|js|expr)[ \t]*\r?$/;
const FENCE_CLOSE = /^[ \t]*```[ \t]*\r?$/;
const ANY_OGUI = /```ogui-(?:meta|css|html|js|expr)/;

export function hasGenerativeUi(content: string | null | undefined): boolean {
  return !!content && ANY_OGUI.test(content);
}

function emptyWidget(): OguiWidget {
  return {
    css: "",
    cssComplete: false,
    html: "",
    htmlComplete: false,
    jsFunctionsComplete: false,
    jsExpressions: [],
    generating: true,
  };
}

function parseMeta(text: string): Partial<OguiWidget> {
  try {
    const raw = JSON.parse(text.trim()) as Record<string, unknown>;
    const out: Partial<OguiWidget> = {};
    if (typeof raw.initialHeight === "number" && Number.isFinite(raw.initialHeight)) {
      out.initialHeight = raw.initialHeight;
    }
    if (Array.isArray(raw.placeholderMessages)) {
      const msgs = raw.placeholderMessages.filter(
        (m): m is string => typeof m === "string" && m.trim().length > 0,
      );
      if (msgs.length) out.placeholderMessages = msgs.slice(0, 6);
    }
    return out;
  } catch {
    return {};
  }
}

type FenceKind = "meta" | "css" | "html" | "js" | "expr";

interface Fence {
  kind: FenceKind;
  body: string;
  closed: boolean;
}

/** Extract every ogui fence in order, tracking prose gaps between them. */
function scanFences(content: string): { fences: Fence[]; gaps: string[] } {
  const fences: Fence[] = [];
  const gaps: string[] = [];
  const lines = content.split("\n");
  let i = 0;
  let gap: string[] = [];
  const flushGap = () => {
    gaps.push(gap.join("\n"));
    gap = [];
  };

  while (i < lines.length) {
    const open = FENCE_OPEN.exec(lines[i]);
    if (!open) {
      gap.push(lines[i]);
      i++;
      continue;
    }
    flushGap();
    const kind = open[1] as FenceKind;
    i++;
    const body: string[] = [];
    let closed = false;
    while (i < lines.length) {
      if (FENCE_CLOSE.test(lines[i])) {
        closed = true;
        i++;
        break;
      }
      body.push(lines[i]);
      i++;
    }
    fences.push({ kind, body: body.join("\n"), closed });
    // A fence that never closed consumed the rest of the message.
    if (!closed) break;
  }
  flushGap();
  return { fences, gaps };
}

/**
 * Split assistant content into ordered prose/widget segments.
 * Streaming-safe: an unclosed fence contributes its partial body and the
 * widget stays `generating` until every open channel has closed.
 */
export function parseGenerativeUi(content: string): OguiSegment[] {
  if (!hasGenerativeUi(content)) return [{ kind: "prose", text: content }];

  const { fences, gaps } = scanFences(content);
  const segments: OguiSegment[] = [];
  let widget: OguiWidget | null = null;
  let fenceIndex = 0;

  const ensureWidget = () => {
    if (!widget) {
      widget = emptyWidget();
      segments.push({ kind: "widget", widget });
    }
    return widget;
  };
  const closeWidget = () => {
    widget = null;
  };

  // gaps[0] precedes fences[0]; gaps[i] sits between fences[i-1] and fences[i].
  const emitGap = (text: string, isTrailing: boolean) => {
    if (!text.trim()) return;
    // Non-whitespace prose finalizes the current widget.
    closeWidget();
    segments.push({ kind: "prose", text: isTrailing ? text : text });
  };

  emitGap(gaps[0] ?? "", false);

  for (const fence of fences) {
    // Prose gap before this fence closes any open widget.
    const gapText = gaps[fenceIndex + 1] ?? "";
    const nextIsFence = fenceIndex + 1 < fences.length;
    void nextIsFence;

    if (fence.kind === "meta") {
      closeWidget();
      widget = emptyWidget();
      segments.push({ kind: "widget", widget });
      const meta = parseMeta(fence.body);
      if (meta.initialHeight !== undefined) widget.initialHeight = meta.initialHeight;
      if (meta.placeholderMessages) widget.placeholderMessages = meta.placeholderMessages;
    } else {
      const w = ensureWidget();
      switch (fence.kind) {
        case "css":
          w.css = fence.body;
          w.cssComplete = fence.closed;
          break;
        case "html":
          w.html = fence.body;
          w.htmlComplete = fence.closed;
          break;
        case "js":
          w.jsFunctions = fence.body;
          w.jsFunctionsComplete = fence.closed;
          break;
        case "expr":
          if (fence.closed && fence.body.trim()) w.jsExpressions.push(fence.body);
          break;
      }
    }

    // Whitespace-only gaps keep the widget open; real prose closes it. The
    // gap after the LAST fence is the trailing segment, emitted below.
    if (fenceIndex + 1 < fences.length && gapText.trim()) {
      emitGap(gapText, false);
      closeWidget();
    }
    fenceIndex++;
  }

  // Trailing gap after the last fence.
  const trailing = gaps[gaps.length - 1] ?? "";
  if (trailing.trim()) {
    closeWidget();
    segments.push({ kind: "prose", text: trailing });
  }

  // Mark widgets still receiving stream content as generating.
  for (const seg of segments) {
    if (seg.kind !== "widget") continue;
    const w = seg.widget;
    w.generating =
      !w.htmlComplete || !w.cssComplete || !w.jsFunctionsComplete;
  }

  return segments;
}
