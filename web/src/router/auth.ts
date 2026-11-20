import { redirect } from "@tanstack/react-router";

function isAuthEnabled(): boolean {
  const raw = process.env.NEXT_PUBLIC_AUTH_ENABLED ?? "";
  return ["1", "true", "yes", "on"].includes(raw.trim().toLowerCase());
}

function isAuthExemptPath(pathname: string): boolean {
  return (
    pathname === "/login" ||
    pathname.startsWith("/login/") ||
    pathname === "/register" ||
    pathname.startsWith("/register/") ||
    pathname === "/handoff" ||
    pathname.startsWith("/handoff/")
  );
}

export async function requireAuth({
  location,
}: {
  location: { pathname: string; searchStr: string };
}): Promise<void> {
  if (!isAuthEnabled() || isAuthExemptPath(location.pathname)) {
    return;
  }

  let authenticated = true;
  try {
    const res = await fetch("/api/auth/status", { credentials: "include" });
    if (res.ok) {
      const data = (await res.json()) as { authenticated?: boolean };
      if (data.authenticated === false) {
        authenticated = false;
      }
    } else if (res.status === 401) {
      authenticated = false;
    }
  } catch {
    // Ignore transient network failures; API calls still enforce auth.
  }

  if (!authenticated) {
    throw redirect({
      to: "/login",
      search: { next: `${location.pathname}${location.searchStr}` },
    });
  }
}
