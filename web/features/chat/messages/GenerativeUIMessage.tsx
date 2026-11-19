"use client";

import { useMemo } from "react";

import AssistantResponse from "@/components/common/AssistantResponse";
import { GenerativeUIWidget } from "@/components/generative-ui/GenerativeUIWidget";
import type { OpenGenUIContent } from "@/components/generative-ui/schema";
import type { StreamEvent } from "@/features/chat/model/protocol";
import { parseGenerativeUi } from "@/lib/generative-ui/parse-ogui";
import { createSandboxApi } from "@/lib/generative-ui/sandbox-api";

interface GenerativeUIMessageProps {
  content: string;
  isStreaming?: boolean;
  language?: string;
  readingMaterialId?: string;
  readingMaterialRevision?: number;
  events?: StreamEvent[];
  /** Sends a learner message, e.g. from a widget's sendPrompt bridge. */
  onWidgetPrompt?: (text: string) => void | Promise<void>;
}

/**
 * Renders an assistant turn that mixes Markdown prose with sandboxed
 * generative-UI widgets (the ```ogui-* fenced blocks). Ported from
 * OpenIntelligentUI's open-generative-ui activity renderer; the wire format
 * is the text-streaming equivalent of its generateSandboxedUi channels.
 */
export default function GenerativeUIMessage({
  content,
  isStreaming = false,
  language,
  readingMaterialId,
  readingMaterialRevision,
  events,
  onWidgetPrompt,
}: GenerativeUIMessageProps) {
  const segments = useMemo(() => parseGenerativeUi(content), [content]);
  // onWidgetPrompt is a stable useCallback at the call site; localApi feeds
  // the sandbox effect, which recreates the iframe if it ever changes.
  const localApi = useMemo(
    () => createSandboxApi({ onSendPrompt: onWidgetPrompt }),
    [onWidgetPrompt],
  );

  return (
    <>
      {segments.map((seg, i) => {
        if (seg.kind === "prose") {
          return (
            <AssistantResponse
              key={`p${i}`}
              content={seg.text}
              language={language}
              isStreaming={isStreaming && i === segments.length - 1}
              readingMaterialId={readingMaterialId}
              readingMaterialRevision={readingMaterialRevision}
              events={events}
            />
          );
        }
        const w = seg.widget;
        const ui: OpenGenUIContent = {
          initialHeight: w.initialHeight,
          // The message stream owns "generating": once the turn settles the
          // widget is final even if a fence never closed.
          generating: isStreaming,
          css: w.css || undefined,
          cssComplete: w.cssComplete,
          html: w.html ? [w.html] : undefined,
          htmlComplete: w.htmlComplete,
          jsFunctions: w.jsFunctions || undefined,
          jsFunctionsComplete: w.jsFunctionsComplete,
          jsExpressions: w.jsExpressions.length ? w.jsExpressions : undefined,
          jsExpressionsComplete: w.htmlComplete,
        };
        return (
          <GenerativeUIWidget
            key={`w${i}`}
            content={ui}
            localApi={localApi}
          />
        );
      })}
    </>
  );
}
