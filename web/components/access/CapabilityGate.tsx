"use client";

import { useModules, moduleRouteAvailable } from "@/hooks/useModules";

import { usePathname } from "@/shared/router/navigation";

import { capabilityForPath } from "@/lib/capability-routes";

import { RequireCapability } from "./RequireCapability";

/**
 * Route-level gate: derives the required model capability from the current
 * pathname and locks the page when the user lacks it. Mounted once per authed
 * layout group, so direct-URL access to a gated feature is covered too.
 */
export default function CapabilityGate({
  children,
}: {
  children: React.ReactNode;
}) {
  const pathname = usePathname() ?? "";
  const capability = capabilityForPath(pathname);
  const { modules, error, reload } = useModules();
  
  if (error) return <button onClick={reload}>{"Retry"}</button>;
  if (!modules) return <div aria-busy="true">{"Loading..."}</div>;
  if (!moduleRouteAvailable(pathname, modules)) {
    return <p className="p-6">{"This module is not installed or is disabled."}</p>;
  }
  return (
    <RequireCapability capability={capability}>{children}</RequireCapability>
  );
}
