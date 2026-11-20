import React, { Suspense } from "react";
import {
  Outlet,
  createRootRoute,
  createRoute,
  createRouter,
  lazyRouteComponent,
  notFound,
  redirect,
} from "@tanstack/react-router";
import SettingsReturnTracker from "@/components/settings/SettingsReturnTracker";
import { WorkspaceNavigation } from "@/components/workspaces/WorkspaceNavigation";
import MotionProvider from "@/components/common/MotionProvider";
import ToastViewport from "@/components/common/ToastViewport";
import { AppShellProvider } from "@/context/AppShellContext";
import { I18nClientBridge } from "@/i18n/I18nClientBridge";
import { READING_HOME } from "@/lib/learning-routes";

import AdminLayout from "@/app/(admin)/layout";
import AuthLayout from "@/app/(auth)/layout";
import SettingsGroupLayout from "@/app/(settings)/layout";
import SettingsLayout from "@/app/(settings)/settings/layout";
import UtilityLayout from "@/app/(utility)/layout";
import AgentsLayout from "@/app/(utility)/agents/layout";
import MemoryLayout from "@/app/(utility)/memory/layout";
import SpaceLayout from "@/app/(utility)/space/layout";
import WorkspaceLayout from "@/app/(workspace)/layout";
import CoWriterLayout from "@/app/(workspace)/co-writer/layout";
import LearningLayout from "@/app/(workspace)/learning/layout";
import MasteryLayout from "@/app/(workspace)/learning/mastery/layout";
import MasterySessionsLayout from "@/app/(workspace)/learning/mastery/[pathId]/sessions/layout";
import ReadingLayout from "@/app/(workspace)/learning/reading/layout";

const LazySettingsPageContent = React.lazy(
  () => import("@/components/settings/SettingsPageContent"),
);
const LazyReadingFolderPage = React.lazy(() =>
  import("@/components/reading/library/ReadingFolder").then((m) => ({
    default: m.ReadingFolderPage,
  })),
);
const LazyLegacyWatchingSession = React.lazy(
  () => import("@/components/reading/workspace/LegacyWatchingSession"),
);

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

function readCookie(name: string): string | null {
  if (typeof document === "undefined") return null;
  const prefix = `${name}=`;
  for (const part of document.cookie.split(";")) {
    const trimmed = part.trim();
    if (trimmed.startsWith(prefix)) {
      return decodeURIComponent(trimmed.slice(prefix.length));
    }
  }
  return null;
}

function isValidJwt(token: string | null): boolean {
  if (!token) return false;
  const parts = token.split(".");
  if (parts.length !== 3) return false;
  try {
    const base64 = parts[1].replace(/-/g, "+").replace(/_/g, "/");
    const payload = JSON.parse(atob(base64)) as { exp?: number };
    if (typeof payload.exp === "number" && payload.exp * 1000 <= Date.now()) {
      return false;
    }
    return true;
  } catch {
    return false;
  }
}

function RootShell() {
  return (
    <AppShellProvider>
      <SettingsReturnTracker />
      <WorkspaceNavigation />
      <MotionProvider>
        <I18nClientBridge>
          <Outlet />
        </I18nClientBridge>
      </MotionProvider>
      <ToastViewport />
    </AppShellProvider>
  );
}

const rootRoute = createRootRoute({
  beforeLoad: ({ location }) => {
    if (!isAuthEnabled() || isAuthExemptPath(location.pathname)) return;
    const token = readCookie("dt_token");
    if (!isValidJwt(token)) {
      if (typeof document !== "undefined") {
        document.cookie = "dt_token=; Max-Age=0; path=/";
      }
      throw redirect({
        to: "/login",
        search: { next: `${location.pathname}${location.searchStr}` },
      });
    }
  },
  component: RootShell,
  notFoundComponent: () => (
    <div className="flex min-h-screen items-center justify-center bg-[var(--background)] text-[var(--foreground)]">
      <div className="text-center">
        <h1 className="text-2xl font-semibold">404</h1>
        <p className="mt-1 text-sm text-[var(--muted-foreground)]">
          Page not found
        </p>
      </div>
    </div>
  ),
});

// Root-level routes & retired paths
const indexRedirectRoute = createRoute({
  getParentRoute: () => rootRoute,
  path: "/",
  beforeLoad: () => {
    throw redirect({ to: "/chat" });
  },
});

const handoffRoute = createRoute({
  getParentRoute: () => rootRoute,
  path: "/handoff",
  component: lazyRouteComponent(() => import("@/app/handoff/page")),
});

const retiredPartnersGroupsRoute = createRoute({
  getParentRoute: () => rootRoute,
  path: "/partners/groups",
  beforeLoad: () => {
    throw notFound();
  },
});

// Legacy redirects
const notebookLegacyIndexRoute = createRoute({
  getParentRoute: () => rootRoute,
  path: "/notebook",
  beforeLoad: () => {
    throw redirect({ to: "/notebooks" });
  },
});

const notebookLegacySplatRoute = createRoute({
  getParentRoute: () => rootRoute,
  path: "/notebook/$",
  beforeLoad: ({ params, location }) => {
    throw redirect({
      href: `/notebooks/${params._splat}${location.searchStr}`,
    });
  },
});

const masteryStudyLegacyRoute = createRoute({
  getParentRoute: () => rootRoute,
  path: "/mastery/$pathId/study",
  beforeLoad: ({ params, location }) => {
    throw redirect({
      href: `/learning/mastery/${params.pathId}/sessions${location.searchStr}`,
    });
  },
});

const masteryStudySessionLegacyRoute = createRoute({
  getParentRoute: () => rootRoute,
  path: "/mastery/$pathId/study/$sessionId",
  beforeLoad: ({ params, location }) => {
    throw redirect({
      href: `/learning/mastery/${params.pathId}/sessions/${params.sessionId}${location.searchStr}`,
    });
  },
});

const legacyLearningSurfaceRoutes = (
  ["books", "mastery", "reading", "watching"] as const
).flatMap((surface) => [
  createRoute({
    getParentRoute: () => rootRoute,
    path: `/${surface}`,
    beforeLoad: ({ location }) => {
      throw redirect({ href: `/learning/${surface}${location.searchStr}` });
    },
  }),
  createRoute({
    getParentRoute: () => rootRoute,
    path: `/${surface}/$`,
    beforeLoad: ({ params, location }) => {
      throw redirect({
        href: `/learning/${surface}/${params._splat}${location.searchStr}`,
      });
    },
  }),
]);

// (admin) group
const adminLayoutRoute = createRoute({
  getParentRoute: () => rootRoute,
  id: "admin",
  component: () => (
    <AdminLayout>
      <Outlet />
    </AdminLayout>
  ),
});

const adminUsersRoute = createRoute({
  getParentRoute: () => adminLayoutRoute,
  path: "/admin/users",
  component: lazyRouteComponent(() => import("@/app/(admin)/admin/users/page")),
});

// (auth) group
const authLayoutRoute = createRoute({
  getParentRoute: () => rootRoute,
  id: "auth",
  component: () => (
    <AuthLayout>
      <Outlet />
    </AuthLayout>
  ),
});

const loginRoute = createRoute({
  getParentRoute: () => authLayoutRoute,
  path: "/login",
  component: lazyRouteComponent(() => import("@/app/(auth)/login/page")),
});

const registerRoute = createRoute({
  getParentRoute: () => authLayoutRoute,
  path: "/register",
  component: lazyRouteComponent(() => import("@/app/(auth)/register/page")),
});

// (settings) group
const settingsGroupLayoutRoute = createRoute({
  getParentRoute: () => rootRoute,
  id: "settings-group",
  component: () => (
    <SettingsGroupLayout>
      <Outlet />
    </SettingsGroupLayout>
  ),
});

const settingsLayoutRoute = createRoute({
  getParentRoute: () => settingsGroupLayoutRoute,
  path: "/settings",
  component: () => (
    <SettingsLayout>
      <Outlet />
    </SettingsLayout>
  ),
});

const settingsIndexRoute = createRoute({
  getParentRoute: () => settingsLayoutRoute,
  path: "/",
  component: lazyRouteComponent(
    () => import("@/app/(settings)/settings/page"),
  ),
});

const settingsSectionRoute = createRoute({
  getParentRoute: () => settingsLayoutRoute,
  path: "/$section",
  component: function SettingsSectionRouteComponent() {
    const { section } = settingsSectionRoute.useParams();
    return (
      <Suspense fallback={null}>
        <LazySettingsPageContent section={section} />
      </Suspense>
    );
  },
});

// (utility) group
const utilityLayoutRoute = createRoute({
  getParentRoute: () => rootRoute,
  id: "utility",
  component: () => (
    <UtilityLayout>
      <Outlet />
    </UtilityLayout>
  ),
});

const agentsLayoutRoute = createRoute({
  getParentRoute: () => utilityLayoutRoute,
  path: "/agents",
  component: () => (
    <AgentsLayout>
      <Outlet />
    </AgentsLayout>
  ),
});

const agentsIndexRoute = createRoute({
  getParentRoute: () => agentsLayoutRoute,
  path: "/",
  component: lazyRouteComponent(() => import("@/app/(utility)/agents/page")),
});

const avatarPreviewRoute = createRoute({
  getParentRoute: () => utilityLayoutRoute,
  path: "/avatar-preview",
  component: lazyRouteComponent(
    () => import("@/app/(utility)/avatar-preview/page"),
  ),
});

const knowledgeBasesIndexRoute = createRoute({
  getParentRoute: () => utilityLayoutRoute,
  path: "/knowledge-bases",
  component: lazyRouteComponent(
    () => import("@/app/(utility)/knowledge-bases/page"),
  ),
});

const knowledgeBasesDetailRoute = createRoute({
  getParentRoute: () => utilityLayoutRoute,
  path: "/knowledge-bases/$kbName",
  component: lazyRouteComponent(
    () => import("@/app/(utility)/knowledge-bases/[kbName]/page"),
  ),
});

const memoryLayoutRoute = createRoute({
  getParentRoute: () => utilityLayoutRoute,
  path: "/memory",
  component: () => (
    <MemoryLayout>
      <Outlet />
    </MemoryLayout>
  ),
});

const memoryIndexRoute = createRoute({
  getParentRoute: () => memoryLayoutRoute,
  path: "/",
  component: lazyRouteComponent(() => import("@/app/(utility)/memory/page")),
});

const memoryGraphRoute = createRoute({
  getParentRoute: () => memoryLayoutRoute,
  path: "/graph",
  component: lazyRouteComponent(
    () => import("@/app/(utility)/memory/graph/page"),
  ),
});

const memoryL1Route = createRoute({
  getParentRoute: () => memoryLayoutRoute,
  path: "/l1",
  component: lazyRouteComponent(() => import("@/app/(utility)/memory/l1/page")),
});

const memoryL2IndexRoute = createRoute({
  getParentRoute: () => memoryLayoutRoute,
  path: "/l2",
  component: lazyRouteComponent(() => import("@/app/(utility)/memory/l2/page")),
});

const memoryL2SurfaceRoute = createRoute({
  getParentRoute: () => memoryLayoutRoute,
  path: "/l2/$surface",
  component: lazyRouteComponent(
    () => import("@/app/(utility)/memory/l2/[surface]/page"),
  ),
});

const memoryL3IndexRoute = createRoute({
  getParentRoute: () => memoryLayoutRoute,
  path: "/l3",
  component: lazyRouteComponent(() => import("@/app/(utility)/memory/l3/page")),
});

const memoryL3SlotRoute = createRoute({
  getParentRoute: () => memoryLayoutRoute,
  path: "/l3/$slot",
  component: lazyRouteComponent(
    () => import("@/app/(utility)/memory/l3/[slot]/page"),
  ),
});

const memoryResolveRoute = createRoute({
  getParentRoute: () => memoryLayoutRoute,
  path: "/resolve",
  component: lazyRouteComponent(
    () => import("@/app/(utility)/memory/resolve/page"),
  ),
});

const notebooksIndexRoute = createRoute({
  getParentRoute: () => utilityLayoutRoute,
  path: "/notebooks",
  component: lazyRouteComponent(
    () => import("@/app/(utility)/notebooks/page"),
  ),
});

const notebooksDetailRoute = createRoute({
  getParentRoute: () => utilityLayoutRoute,
  path: "/notebooks/$notebookId",
  component: lazyRouteComponent(
    () => import("@/app/(utility)/notebooks/[notebookId]/page"),
  ),
});

const personalizationRoute = createRoute({
  getParentRoute: () => utilityLayoutRoute,
  path: "/personalization",
  component: lazyRouteComponent(
    () => import("@/app/(utility)/personalization/page"),
  ),
});

const profileRoute = createRoute({
  getParentRoute: () => utilityLayoutRoute,
  path: "/profile",
  component: lazyRouteComponent(() => import("@/app/(utility)/profile/page")),
});

const sessionHandoffRoute = createRoute({
  getParentRoute: () => utilityLayoutRoute,
  path: "/session-handoff",
  component: lazyRouteComponent(
    () => import("@/app/(utility)/session-handoff/page"),
  ),
});

const settingsProgressRoute = createRoute({
  getParentRoute: () => utilityLayoutRoute,
  path: "/settings/progress",
  component: lazyRouteComponent(
    () => import("@/app/(utility)/settings/progress/page"),
  ),
});

const spaceLayoutRoute = createRoute({
  getParentRoute: () => utilityLayoutRoute,
  path: "/space",
  component: () => (
    <SpaceLayout>
      <Outlet />
    </SpaceLayout>
  ),
});

const spaceIndexRoute = createRoute({
  getParentRoute: () => spaceLayoutRoute,
  path: "/",
  component: lazyRouteComponent(() => import("@/app/(utility)/space/page")),
});

const spaceChatHistoryRoute = createRoute({
  getParentRoute: () => spaceLayoutRoute,
  path: "/chat-history",
  component: lazyRouteComponent(
    () => import("@/app/(utility)/space/chat-history/page"),
  ),
});

const spaceCliAppsRoute = createRoute({
  getParentRoute: () => spaceLayoutRoute,
  path: "/cli-apps",
  component: lazyRouteComponent(
    () => import("@/app/(utility)/space/cli-apps/page"),
  ),
});

const spaceJournalRoute = createRoute({
  getParentRoute: () => spaceLayoutRoute,
  path: "/journal",
  component: lazyRouteComponent(
    () => import("@/app/(utility)/space/journal/page"),
  ),
});

const spaceQuestionsRoute = createRoute({
  getParentRoute: () => spaceLayoutRoute,
  path: "/questions",
  component: lazyRouteComponent(
    () => import("@/app/(utility)/space/questions/page"),
  ),
});

// (workspace) group
const workspaceLayoutRoute = createRoute({
  getParentRoute: () => rootRoute,
  id: "workspace",
  component: () => (
    <WorkspaceLayout>
      <Outlet />
    </WorkspaceLayout>
  ),
});

const calendarRoute = createRoute({
  getParentRoute: () => workspaceLayoutRoute,
  path: "/calendar",
  component: lazyRouteComponent(
    () => import("@/app/(workspace)/calendar/page"),
  ),
});

const chatIndexRoute = createRoute({
  getParentRoute: () => workspaceLayoutRoute,
  path: "/chat",
  component: lazyRouteComponent(() => import("@/app/(workspace)/chat/page")),
});

const chatSessionRoute = createRoute({
  getParentRoute: () => workspaceLayoutRoute,
  path: "/chat/$sessionId",
  component: lazyRouteComponent(
    () => import("@/app/(workspace)/chat/[sessionId]/page"),
  ),
});

const coWriterLayoutRoute = createRoute({
  getParentRoute: () => workspaceLayoutRoute,
  path: "/co-writer",
  component: () => (
    <CoWriterLayout>
      <Outlet />
    </CoWriterLayout>
  ),
});

const coWriterIndexRoute = createRoute({
  getParentRoute: () => coWriterLayoutRoute,
  path: "/",
  component: lazyRouteComponent(
    () => import("@/app/(workspace)/co-writer/page"),
  ),
});

const coWriterDocRoute = createRoute({
  getParentRoute: () => coWriterLayoutRoute,
  path: "/$docId",
  component: lazyRouteComponent(
    () => import("@/app/(workspace)/co-writer/[docId]/page"),
  ),
});

const whisperRoute = createRoute({
  getParentRoute: () => workspaceLayoutRoute,
  path: "/whisper",
  component: lazyRouteComponent(() => import("@/app/(workspace)/whisper/page")),
});

// learning subtree
const learningLayoutRoute = createRoute({
  getParentRoute: () => workspaceLayoutRoute,
  path: "/learning",
  component: () => (
    <LearningLayout>
      <Outlet />
    </LearningLayout>
  ),
});

const learningIndexRoute = createRoute({
  getParentRoute: () => learningLayoutRoute,
  path: "/",
  component: lazyRouteComponent(
    () => import("@/app/(workspace)/learning/page"),
  ),
});

const learningNewRoute = createRoute({
  getParentRoute: () => learningLayoutRoute,
  path: "/new",
  component: lazyRouteComponent(
    () => import("@/app/(workspace)/learning/new/page"),
  ),
});

const learningPracticeRoute = createRoute({
  getParentRoute: () => learningLayoutRoute,
  path: "/practice",
  component: lazyRouteComponent(
    () => import("@/app/(workspace)/learning/practice/page"),
  ),
});

const learningBooksIndexRoute = createRoute({
  getParentRoute: () => learningLayoutRoute,
  path: "/books",
  component: lazyRouteComponent(
    () => import("@/app/(workspace)/learning/books/page"),
  ),
});

const learningBooksDetailRoute = createRoute({
  getParentRoute: () => learningLayoutRoute,
  path: "/books/$bookId",
  component: lazyRouteComponent(
    () => import("@/app/(workspace)/learning/books/[bookId]/page"),
  ),
});

const learningBooksPageRoute = createRoute({
  getParentRoute: () => learningLayoutRoute,
  path: "/books/$bookId/pages/$pageId",
  component: lazyRouteComponent(
    () => import("@/app/(workspace)/learning/books/[bookId]/pages/[pageId]/page"),
  ),
});

const masteryLayoutRoute = createRoute({
  getParentRoute: () => learningLayoutRoute,
  path: "/mastery",
  component: () => (
    <MasteryLayout>
      <Outlet />
    </MasteryLayout>
  ),
});

const masteryIndexRoute = createRoute({
  getParentRoute: () => masteryLayoutRoute,
  path: "/",
  component: lazyRouteComponent(
    () => import("@/app/(workspace)/learning/mastery/page"),
  ),
});

const masteryPathRoute = createRoute({
  getParentRoute: () => masteryLayoutRoute,
  path: "/$pathId",
  component: lazyRouteComponent(
    () => import("@/app/(workspace)/learning/mastery/[pathId]/page"),
  ),
});

const masterySessionsLayoutRoute = createRoute({
  getParentRoute: () => masteryLayoutRoute,
  path: "/$pathId/sessions",
  component: () => (
    <MasterySessionsLayout>
      <Outlet />
    </MasterySessionsLayout>
  ),
});

const masterySessionsIndexRoute = createRoute({
  getParentRoute: () => masterySessionsLayoutRoute,
  path: "/",
  component: lazyRouteComponent(
    () => import("@/app/(workspace)/learning/mastery/[pathId]/sessions/page"),
  ),
});

const masterySessionsDetailRoute = createRoute({
  getParentRoute: () => masterySessionsLayoutRoute,
  path: "/$sessionId",
  component: lazyRouteComponent(
    () =>
      import(
        "@/app/(workspace)/learning/mastery/[pathId]/sessions/[sessionId]/page"
      ),
  ),
});

const readingLayoutRoute = createRoute({
  getParentRoute: () => learningLayoutRoute,
  path: "/reading",
  component: () => (
    <ReadingLayout>
      <Outlet />
    </ReadingLayout>
  ),
});

const readingIndexRoute = createRoute({
  getParentRoute: () => readingLayoutRoute,
  path: "/",
  component: lazyRouteComponent(
    () => import("@/app/(workspace)/learning/reading/page"),
  ),
});

const readingMaterialsRoute = createRoute({
  getParentRoute: () => readingLayoutRoute,
  path: "/materials",
  component: lazyRouteComponent(
    () => import("@/app/(workspace)/learning/reading/materials/page"),
  ),
});

const readingFolderRoute = createRoute({
  getParentRoute: () => readingLayoutRoute,
  path: "/folders/$folderId",
  component: function ReadingFolderRouteComponent() {
    const { folderId } = readingFolderRoute.useParams();
    return (
      <Suspense fallback={null}>
        <LazyReadingFolderPage folderId={decodeURIComponent(folderId)} />
      </Suspense>
    );
  },
});

const readingWorkspaceRoute = createRoute({
  getParentRoute: () => readingLayoutRoute,
  path: "/$workspaceId",
  component: lazyRouteComponent(
    () => import("@/app/(workspace)/learning/reading/[workspaceId]/page"),
  ),
});

const readingWorkspaceSessionsRoute = createRoute({
  getParentRoute: () => readingLayoutRoute,
  path: "/$workspaceId/sessions",
  component: lazyRouteComponent(
    () =>
      import("@/app/(workspace)/learning/reading/[workspaceId]/sessions/page"),
  ),
});

const readingWorkspaceSessionDetailRoute = createRoute({
  getParentRoute: () => readingLayoutRoute,
  path: "/$workspaceId/sessions/$sessionId",
  component: lazyRouteComponent(
    () =>
      import(
        "@/app/(workspace)/learning/reading/[workspaceId]/sessions/[sessionId]/page"
      ),
  ),
});

const watchingIndexRedirectRoute = createRoute({
  getParentRoute: () => learningLayoutRoute,
  path: "/watching",
  beforeLoad: ({ location }) => {
    throw redirect({ href: `${READING_HOME}${location.searchStr}` });
  },
});

const watchingSessionRoute = createRoute({
  getParentRoute: () => learningLayoutRoute,
  path: "/watching/$sessionId",
  component: function WatchingSessionRouteComponent() {
    const { sessionId } = watchingSessionRoute.useParams();
    return (
      <Suspense fallback={null}>
        <LazyLegacyWatchingSession sessionId={sessionId} />
      </Suspense>
    );
  },
});

const routeTree = rootRoute.addChildren([
  indexRedirectRoute,
  handoffRoute,
  retiredPartnersGroupsRoute,
  notebookLegacyIndexRoute,
  notebookLegacySplatRoute,
  masteryStudyLegacyRoute,
  masteryStudySessionLegacyRoute,
  ...legacyLearningSurfaceRoutes,
  adminLayoutRoute.addChildren([adminUsersRoute]),
  authLayoutRoute.addChildren([loginRoute, registerRoute]),
  settingsGroupLayoutRoute.addChildren([
    settingsLayoutRoute.addChildren([settingsIndexRoute, settingsSectionRoute]),
  ]),
  utilityLayoutRoute.addChildren([
    agentsLayoutRoute.addChildren([agentsIndexRoute]),
    avatarPreviewRoute,
    knowledgeBasesIndexRoute,
    knowledgeBasesDetailRoute,
    memoryLayoutRoute.addChildren([
      memoryIndexRoute,
      memoryGraphRoute,
      memoryL1Route,
      memoryL2IndexRoute,
      memoryL2SurfaceRoute,
      memoryL3IndexRoute,
      memoryL3SlotRoute,
      memoryResolveRoute,
    ]),
    notebooksIndexRoute,
    notebooksDetailRoute,
    personalizationRoute,
    profileRoute,
    sessionHandoffRoute,
    settingsProgressRoute,
    spaceLayoutRoute.addChildren([
      spaceIndexRoute,
      spaceChatHistoryRoute,
      spaceCliAppsRoute,
      spaceJournalRoute,
      spaceQuestionsRoute,
    ]),
  ]),
  workspaceLayoutRoute.addChildren([
    calendarRoute,
    chatIndexRoute,
    chatSessionRoute,
    coWriterLayoutRoute.addChildren([coWriterIndexRoute, coWriterDocRoute]),
    whisperRoute,
    learningLayoutRoute.addChildren([
      learningIndexRoute,
      learningNewRoute,
      learningPracticeRoute,
      learningBooksIndexRoute,
      learningBooksDetailRoute,
      learningBooksPageRoute,
      masteryLayoutRoute.addChildren([
        masteryIndexRoute,
        masteryPathRoute,
        masterySessionsLayoutRoute.addChildren([
          masterySessionsIndexRoute,
          masterySessionsDetailRoute,
        ]),
      ]),
      readingLayoutRoute.addChildren([
        readingIndexRoute,
        readingMaterialsRoute,
        readingFolderRoute,
        readingWorkspaceRoute,
        readingWorkspaceSessionsRoute,
        readingWorkspaceSessionDetailRoute,
      ]),
      watchingIndexRedirectRoute,
      watchingSessionRoute,
    ]),
  ]),
]);

export const router = createRouter({
  routeTree,
  defaultPreload: "intent",
});

declare module "@tanstack/react-router" {
  interface Register {
    router: typeof router;
  }
}
