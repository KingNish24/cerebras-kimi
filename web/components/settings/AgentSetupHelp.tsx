"use client";

import { useState } from "react";
import { Bot, Check, Copy } from "lucide-react";

import Modal from "@/components/common/Modal";
import { copyText } from "@/lib/clipboard";

export default function AgentSetupHelp() {
  
  const [open, setOpen] = useState(false);
  const [copyState, setCopyState] = useState<"idle" | "copying" | "copied" | "failed">("idle");
  const prompt = "Help me configure my existing DeepTutor installation: https://github.com/HKUDS/DeepTutor.\nFirst read SKILL.md and docs-for-dev/AGENT_SETUP.md in the matching checkout (or in that repository if no local source is available).\nIdentify the installation, version, running instance, account, and runtime home. Reuse my environment and preserve existing files, settings, and pending drafts.\nHelp me connect a provider and select a chat model; configure embedding, search, and other services only as needed. Ask for missing model choices or credential sources; do not ask me to paste API keys into chat or print them.\nFollow the guide and the installed CLI's --help to configure and verify this same instance. Finish with what changed, actual check results, and any remaining steps.";

  const copy = async () => {
    setCopyState("copying");
    try {
      await copyText(prompt);
      setCopyState("copied");
    } catch {
      setCopyState("failed");
    }
  };

  return (
    <>
      <div className="flex flex-wrap items-center justify-between gap-3 border-b border-[var(--border)]/50 py-4">
        <p className="text-[13px] leading-relaxed text-[var(--muted-foreground)]">
          {"Follow the steps below, or let your own Agent help you configure DeepTutor."}
        </p>
        <button
          type="button"
          onClick={() => {
            setCopyState("idle");
            setOpen(true);
          }}
          className="inline-flex items-center gap-2 rounded-lg border border-[var(--border)] px-3 py-2 text-[13px] font-medium hover:bg-[var(--muted)] focus-visible:outline-none focus-visible:ring-2 focus-visible:ring-[var(--ring)]"
        >
          <Bot size={16} aria-hidden="true" />
          {"Let your Agent help you configure"}
        </button>
      </div>
      <Modal
        isOpen={open}
        onClose={() => setOpen(false)}
        title={"Let your Agent help you configure"}
        width="lg"
        footer={
          <div className="flex flex-wrap items-center justify-end gap-3">
            <p role="status" className="text-xs text-[var(--muted-foreground)]">
              {copyState === "failed" ? "Could not copy. Select the text above and copy it manually." : copyState === "copied" ? "Copied" : ""}
            </p>
            <button
              type="button"
              onClick={() => void copy()}
              disabled={copyState === "copying"}
              className="inline-flex items-center gap-2 rounded-lg bg-[var(--foreground)] px-4 py-2 text-sm font-medium text-[var(--background)] disabled:opacity-50 focus-visible:outline-none focus-visible:ring-2 focus-visible:ring-[var(--ring)] focus-visible:ring-offset-2"
            >
              {copyState === "copied" ? <Check size={16} aria-hidden="true" /> : <Copy size={16} aria-hidden="true" />}
              {"Copy prompt"}
            </button>
          </div>
        }
      >
        <div className="space-y-4 p-5">
          <p className="text-sm leading-relaxed text-[var(--muted-foreground)]">
            {"Copy this prompt into an Agent with terminal access to your DeepTutor installation, such as Codex or Claude Code."}
          </p>
          <textarea
            readOnly
            data-autofocus
            aria-label={"Let your Agent help you configure"}
            value={prompt}
            onFocus={(event) => event.currentTarget.select()}
            className="h-72 w-full resize-y rounded-lg border border-[var(--border)] bg-[var(--background)] p-3 text-sm leading-relaxed text-[var(--foreground)] focus:outline-none focus:ring-2 focus:ring-[var(--ring)]"
          />
        </div>
      </Modal>
    </>
  );
}
