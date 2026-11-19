"use client";

import "@/lib/openui/devtools";
import "@openuidev/react-ui/styles/index.css";

import { Renderer, type ActionEvent } from "@openuidev/react-lang";
import { useEffect } from "react";
import { ThemeProvider } from "@openuidev/intelligent-ui-library";
import { runLibraryAction } from "@/lib/openui/actions";
import { library } from "@/lib/openui/library";
import { webUrl } from "@/lib/openui/components/style";
import { resolveTheme, getStoredThemePreference, subscribeToThemeChanges } from "@/lib/theme";

/**
 * Renders an assistant turn written in OpenUI Lang as live components inside
 * the main chat, instead of Markdown. The model is instructed to emit UI
 * programs (see backend/app/agent/prompt.py), which stream into msg.content.
 */
export default function OpenUIMessage({
  content,
  isStreaming = false,
}: {
  content: string;
  isStreaming?: boolean;
}) {
  useEffect(() => {
    const update = () => {
      const resolved = resolveTheme(getStoredThemePreference());
      document.documentElement.setAttribute(
        "data-openui-theme",
        resolved === "dark" || resolved === "glass" ? "dark" : "light",
      );
    };
    update();
    return subscribeToThemeChanges(update);
  }, []);

  const resolved = resolveTheme(getStoredThemePreference());
  const mode = resolved === "dark" || resolved === "glass" ? "dark" : "light";

  const onAction = (event: ActionEvent) => {
    const handled = runLibraryAction(event);
    if (handled) return;
    // @OpenUrl is a built-in step the host owns. Only http(s) in a new tab.
    const params = event.params as { url?: unknown } | undefined;
    if (event.type === "OpenUrl" && typeof params?.url === "string") {
      const safe = webUrl(params.url);
      if (safe) window.open(safe, "_blank", "noopener,noreferrer");
    }
  };

  return (
    <ThemeProvider mode={mode}>
      <div className="openui-message">
        <Renderer
          response={content}
          library={library}
          isStreaming={isStreaming}
          onAction={onAction}
        />
      </div>
    </ThemeProvider>
  );
}
