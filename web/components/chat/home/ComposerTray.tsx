"use client";

import Link from "next/link";
import { m, type MotionStyle } from "framer-motion";
import { createElement, useEffect, useId, useMemo, useState, type ReactNode } from "react";
import { ArrowUpRight, PencilRuler, Plug, Plus, Shapes, UserRound, Wand2 } from "lucide-react";
import { useTranslation } from "react-i18next";
import { Tooltip } from "@/shared/ui/Tooltip";
import { SPACE_MCP_SURFACE, loadMcpSurface } from "@/components/mcp/surface";
import { mcpRowStatus, type McpServerStatus, type McpStoreState } from "@/lib/mcp-api";
import type { ComposerResourceCatalog } from "@/hooks/useComposerResources";
import type { ResourceSelection } from "@/features/chat/ChatStateAdapter";
import type { WorkspaceResourceOption } from "@/lib/workspaces-api";
import type { AnswerModeProps, PartnerLinkProps } from "./AnswerModeMenu";
import type { CapabilityDef } from "@/features/capabilities/presentation";
import { useCapabilityLabel } from "@/features/ui-layout/tokens";
import {
  TrayFoot,
  TrayList,
  TrayMenu,
  TrayOption,
  TrayReuse,
  TraySearch,
  type TrayPlacement,
} from "./ComposerTrayMenu";
import { ConnectorHubIcon, modeIcon } from "./ComposerIcons";
import type { ComposerControls } from "@/lib/page-customization";
import { useTrayFold } from "./ComposerTrayFold";
import { CREATE_KINDS, CREATION_KIND_ORDER, type CreationKind } from "@/lib/creations";

const STATUS_DOT: Record<McpServerStatus, string> = {
  connected: "bg-emerald-500",
  connecting: "bg-amber-400",
  error: "bg-red-500",
  needs_auth: "bg-amber-400",
  disabled: "bg-[var(--border)]",
};

const STATUS_LABEL: Record<McpServerStatus, string> = {
  connected: "Connected",
  connecting: "Connecting",
  error: "Error",
  needs_auth: "Needs authorization",
  disabled: "Disabled",
};

/** Long lists get a search box; short ones are faster to scan. */
const SEARCH_FROM = 7;

interface Connector {
  option: WorkspaceResourceOption;
  status: McpServerStatus;
  tools: number;
  error: string;
}

/** Each catalog server's live state, read from the account's MCP store. */
function useConnectors(options: WorkspaceResourceOption[]): Connector[] {
  const [state, setState] = useState<McpStoreState | null>(null);
  useEffect(() => {
    let alive = true;
    void loadMcpSurface(SPACE_MCP_SURFACE)
      .then((next) => {
        if (alive) setState(next);
      })
      // Without the store the tray still lists the servers, just without state.
      .catch(() => undefined);
    return () => {
      alive = false;
    };
  }, []);
  return useMemo(
    () =>
      options.map((option) => {
        const [origin, ...rest] = option.id.split(":");
        const name = rest.join(":") || option.name;
        const rows = origin === "deployment" ? state?.user?.deployment.status : state?.status;
        const row = rows?.find((entry) => entry.name === name);
        const config = origin === "account" ? state?.servers[name] : undefined;
        const status: McpServerStatus =
          option.available === false
            ? "disabled"
            : config
              ? mcpRowStatus(config, row)
              : (row?.status ?? "connecting");
        return { option, status, tools: row?.tools.length ?? 0, error: row?.error ?? "" };
      }),
    [options, state],
  );
}

export interface TrayResources {
  catalog: ComposerResourceCatalog;
  selection: ResourceSelection;
  onChange: (selection: ResourceSelection) => void;
}

/** The turn's mode; `""` is a plain conversation, which is no mode at all. */
export interface TrayMode {
  capabilities: CapabilityDef[];
  value: string;
  onChange: (value: string) => void;
}

/** Create mode: which artifact kind this conversation is authoring, if any. */
export interface TrayCreate {
  value: string | null;
  onChange: (value: CreationKind | null) => void;
}

/**
 * The layer under the composer: how Dito works this turn, what it can reach
 * in this conversation, and who answers.
 *
 * A mode comes first. Picking none is plain conversation, so the control
 * names itself until one is chosen and then folds to two marks — its own and
 * the mode's — while the mode also opens the message. Skills and connectors
 * rest as marks from the start; each unfolds while pointed at.
 *
 * Skills and connectors are both narrowing, never granting: the first row of
 * each list is "everything this workspace allows", and picking some limits
 * the conversation to those. What is picked shows at the start of the
 * message itself, so the tray only has to say how many. The far side is
 * about people, not tools: who DeepTutor works with (partners) and who
 * answers at all.
 *
 * A grip at the middle of its lower edge folds it up behind the box, by a
 * drag or a click, until only that edge shows.
 */
export function ComposerTray({
  controls,
  placement,
  mode,
  resources,
  create,
  partners,
  answer,
  end,
}: {
  /** Lists open toward the page's free space: below on the home screen, above in a conversation. */
  placement: TrayPlacement;
  controls?: ComposerControls;
  mode?: TrayMode;
  resources?: TrayResources;
  create?: TrayCreate;
  partners?: PartnerLinkProps;
  answer?: AnswerModeProps;
  /** A surface's own control for the far end, where who answers sits. */
  end?: ReactNode;
}) {
  const { t } = useTranslation();
  const rowId = useId();
  const fold = useTrayFold(Boolean(answer?.open));
  const gripLabel = fold.away ? t("Expand") : t("Collapse");
  return (
    <m.div
      className="dt-composer-tray"
      data-placement={placement}
      data-folded={fold.away || undefined}
      data-dragging={fold.dragging || undefined}
      style={{ "--dt-tray-reveal": fold.reveal } as MotionStyle}
      {...fold.sheet}
    >
      <Tooltip
        label={gripLabel}
        side={placement === "up" ? "top" : "bottom"}
        suppressed={fold.dragging}
        className="dt-tray-grip-anchor"
      >
        <button
          type="button"
          className="dt-tray-grip"
          aria-label={gripLabel}
          aria-expanded={!fold.away}
          aria-controls={rowId}
          onKeyDown={fold.onGripKeyDown}
        />
      </Tooltip>
      <div id={rowId} className="dt-tray-bar" inert={fold.away}>
        {mode && <ModeMenu {...mode} placement={placement} />}
        {end}
      </div>
    </m.div>
  );
}

function ModeMenu({ capabilities, value, onChange, placement }: TrayMode & { placement: TrayPlacement }) {
  const { t } = useTranslation();
  const capabilityLabel = useCapabilityLabel();
  const modes = capabilities.filter((cap) => cap.value !== "" && !cap.legacy);
  const chosen = modes.find((cap) => cap.value === value);
  const ChosenIcon = chosen && modeIcon(chosen);
  const row = (cap: CapabilityDef, close: () => void) => {
    const selected = cap.value === chosen?.value;
    const Icon = modeIcon(cap);
    return (
      <TrayOption
        key={cap.value}
        selected={selected}
        // The chosen mode again is the way back to plain conversation.
        onSelect={() => {
          onChange(selected ? "" : cap.value);
          close();
        }}
        leading={<Icon size={22} strokeWidth={1.5} />}
        title={capabilityLabel(cap)}
        meta={t(cap.description)}
      />
    );
  };
  return (
    <TrayMenu
      icon={<Shapes size={14} strokeWidth={1.7} className="shrink-0" />}
      label={t("Mode")}
      value={chosen ? capabilityLabel(chosen) : undefined}
      valueIcon={ChosenIcon && createElement(ChosenIcon, { size: 15, strokeWidth: 1.7, className: "shrink-0" })}
      active={Boolean(chosen)}
      fold={Boolean(chosen)}
      placement={placement}
    >
      {(close) => (
        <TrayList label={t("Mode")} bare>{modes.map((cap) => row(cap, close))}</TrayList>
      )}
    </TrayMenu>
  );
}

