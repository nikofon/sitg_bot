import type { MessageKey } from "../i18n/en";

export type RouteId =
  | "tournaments"
  | "ongoing"
  | "history"
  | "library"
  | "authors_link"
  | "lobby"
  | "report"
  | "manager_appeals"
  | "manager_settings"
  | "manager_management"
  | "packet_editor"
  | "not_found";

export interface RouteMatch {
  id: RouteId;
  path: string;
  titleKey: MessageKey;
  emptyKey: MessageKey;
  params: Readonly<Record<string, string>>;
  query: URLSearchParams;
  isRoot: boolean;
}

interface RouteDefinition {
  id: Exclude<RouteId, "not_found">;
  pattern: RegExp;
  parameterNames?: string[];
  titleKey: MessageKey;
  emptyKey: MessageKey;
  isRoot?: boolean;
}

const routes: RouteDefinition[] = [
  {
    id: "tournaments",
    pattern: /^\/tournaments\/?$/,
    titleKey: "route.tournaments.title",
    emptyKey: "route.tournaments.empty",
    isRoot: true,
  },
  {
    id: "ongoing",
    pattern: /^\/ongoing\/?$/,
    titleKey: "route.ongoing.title",
    emptyKey: "route.ongoing.empty",
    isRoot: true,
  },
  {
    id: "history",
    pattern: /^\/history\/?$/,
    titleKey: "route.history.title",
    emptyKey: "route.history.empty",
    isRoot: true,
  },
  {
    id: "library",
    pattern: /^\/library\/?$/,
    titleKey: "route.library.title",
    emptyKey: "route.library.empty",
    isRoot: true,
  },
  {
    id: "authors_link",
    pattern: /^\/authors\/link\/?$/,
    titleKey: "route.authors_link.title",
    emptyKey: "route.authors_link.empty",
    isRoot: true,
  },
  {
    id: "lobby",
    pattern: /^\/lobbies\/([^/]+)\/?$/,
    parameterNames: ["launch_ref"],
    titleKey: "route.lobby.title",
    emptyKey: "route.lobby.empty",
  },
  {
    id: "report",
    pattern: /^\/reports\/([^/]+)\/?$/,
    parameterNames: ["launch_ref"],
    titleKey: "route.report.title",
    emptyKey: "route.report.empty",
  },
  {
    id: "manager_appeals",
    pattern: /^\/manager\/appeals\/?$/,
    titleKey: "route.manager_appeals.title",
    emptyKey: "route.manager_appeals.empty",
    isRoot: true,
  },
  {
    id: "manager_settings",
    pattern: /^\/manager\/tournaments\/([^/]+)\/settings\/?$/,
    parameterNames: ["launch_ref"],
    titleKey: "route.manager_settings.title",
    emptyKey: "route.manager_settings.empty",
  },
  {
    id: "manager_management",
    pattern: /^\/manager\/tournaments\/([^/]+)\/management\/?$/,
    parameterNames: ["launch_ref"],
    titleKey: "route.manager_management.title",
    emptyKey: "route.manager_management.empty",
  },
  {
    id: "packet_editor",
    pattern: /^\/manager\/packets\/([^/]+)\/edit\/?$/,
    parameterNames: ["launch_ref"],
    titleKey: "route.packet_editor.title",
    emptyKey: "route.packet_editor.empty",
  },
];

export function matchRoute(location: Pick<Location, "pathname" | "search">): RouteMatch {
  for (const route of routes) {
    const result = route.pattern.exec(location.pathname);
    if (!result) continue;
    const params: Record<string, string> = {};
    route.parameterNames?.forEach((name, index) => {
      const raw = result[index + 1];
      if (raw) params[name] = safeDecode(raw);
    });
    return {
      id: route.id,
      path: location.pathname,
      titleKey: route.titleKey,
      emptyKey: route.emptyKey,
      params,
      query: new URLSearchParams(location.search),
      isRoot: route.isRoot === true,
    };
  }
  return {
    id: "not_found",
    path: location.pathname,
    titleKey: "route.not_found.title",
    emptyKey: "route.not_found.empty",
    params: {},
    query: new URLSearchParams(location.search),
    isRoot: false,
  };
}

function safeDecode(value: string): string {
  try {
    return decodeURIComponent(value);
  } catch {
    return value;
  }
}

export function routeRequestPath(route: RouteMatch): string {
  const query = route.query.toString();
  return `${route.path}${query ? `?${query}` : ""}`;
}
