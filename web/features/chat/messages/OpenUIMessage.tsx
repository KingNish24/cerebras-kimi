"use client";

import "@/lib/openui/devtools";
import "@openuidev/react-ui/styles/index.css";

import { Renderer, type ActionEvent } from "@openuidev/react-lang";
import { useEffect } from "react";
import { ThemeProvider } from "@openuidev/intelligent-ui-library";
import { handleOpenUIAction } from "@/lib/openui/actions";
import { library } from "@/lib/openui/library";
import { resolveTheme, getStoredThemePreference, subscribeToThemeChanges } from "@/lib/theme";

/**
 * Renders an assistant turn written in OpenUI Lang as live components inside
 * the main chat, instead of Markdown. The model is instructed to emit UI
 * programs (see backend/app/agent/prompt.py), which stream into msg.content.
 */
export default function OpenUIMessage({
  content,
  isStreaming = false,
  onSendFollowUp,
}: {
  content: string;
  isStreaming?: boolean;
  /** Sends the `continue_conversation` action as a normal user turn. */
  onSendFollowUp?: (text: string) => void | Promise<void>;
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
    handleOpenUIAction(event, onSendFollowUp);
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
