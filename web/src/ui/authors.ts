import type { AuthorProfileResource, AuthorsResource, PublicAuthor } from "../api/types";
import type { I18n } from "../i18n";
import type { MessageKey } from "../i18n/en";
import { element } from "./dom";

function authorCounts(author: PublicAuthor, i18n: I18n): HTMLElement {
  return element("dl", { className: "admin-metadata" },
    ...(["tournament_count", "question_count"] as const).map((key) => element("div", {},
      element("dt", {}, i18n.t(`authors.${key}`)), element("dd", {}, String(author[key])))));
}

export function renderAuthors(
  resource: AuthorsResource, i18n: I18n, filters: Record<string, string>,
  save: (filters: Record<string, string>) => void, navigate: (path: string) => void,
): HTMLElement {
  const list = element("div", { className: "lobby-packets", "aria-live": "polite" });
  const search = element("input", {
    type: "search", value: filters.search ?? "", "aria-label": i18n.t("admin_management.search"),
  });
  const sort = element("select", { "aria-label": i18n.t("admin_management.sort") },
    ...(["display_name", "tournament_count", "question_count"] as const).flatMap((key) =>
      ["asc", "desc"].map((direction) => element("option", { value: `${key}:${direction}` },
        `${i18n.t(`authors.${key}`)} ${direction === "asc" ? "↑" : "↓"}`))));
  sort.value = filters.order ?? "display_name:asc";
  const render = (): void => {
    const query = search.value.trim().toLocaleLowerCase(i18n.locale);
    const [key, direction] = sort.value.split(":");
    const items = resource.items.filter((author) =>
      author.display_name.toLocaleLowerCase(i18n.locale).includes(query));
    items.sort((a, b) => {
      const comparison = key === "tournament_count" || key === "question_count"
        ? a[key] - b[key] : a.display_name.localeCompare(b.display_name, i18n.locale);
      return comparison * (direction === "desc" ? -1 : 1) || a.id.localeCompare(b.id);
    });
    list.replaceChildren(...items.map((author) => {
      const path = `/authors/${encodeURIComponent(author.id)}`;
      return element("article", { className: "resource-card", "data-author-id": author.id },
        element("h2", {}, element("a", {
          href: path,
          onclick: ((event: Event) => { event.preventDefault(); navigate(path); }) as EventListener,
        }, author.display_name)), authorCounts(author, i18n));
    }));
    if (!items.length) list.append(element("p", {}, i18n.t("authors.empty")));
  };
  const update = (): void => {
    save({ search: search.value, order: sort.value });
    render();
  };
  search.addEventListener("input", update);
  sort.addEventListener("change", update);
  render();
  return element("section", { className: "route-content" },
    element("div", { className: "lobby-packet-filters", role: "search" },
      element("label", {}, i18n.t("admin_management.search"), search),
      element("label", {}, i18n.t("admin_management.sort"), sort)), list);
}

export function renderAuthorProfile(
  resource: AuthorProfileResource, i18n: I18n, navigate: (path: string) => void,
): HTMLElement {
  const label = (key: string): string => i18n.t(`authors.${key}` as MessageKey);
  const percent = (value: number | null): string => value === null ? "—" : `${value}%`;
  const stats = resource.statistics;
  const summary = element("dl", { className: "admin-metadata" },
    ...(["presentations", "exposures", "buzzes", "buzz_rate", "attempts", "accuracy",
      "solved_rate", "timeouts"] as const).map((key) => element("div", {},
      element("dt", {}, label(key)), element("dd", {},
        key === "buzz_rate" || key === "accuracy" || key === "solved_rate"
          ? percent(stats[key]) : String(stats[key])))));
  const columns = ["value", "presentations", "buzz_rate", "attempts", "correct", "accuracy",
    "solved_rate", "timeouts"] as const;
  const table = element("table", {},
    element("caption", {}, label("by_value")),
    element("thead", {}, element("tr", {}, ...columns.map((key) =>
      element("th", { scope: "col" }, label(key))))),
    element("tbody", {}, ...resource.by_value.map((row) => element("tr", {},
      ...columns.map((key) => element("td", {},
        key === "buzz_rate" || key === "accuracy" || key === "solved_rate"
          ? percent(row[key]) : String(row[key])))))));
  const back = element("button", { type: "button", className: "secondary-button" }, label("title"));
  back.addEventListener("click", () => navigate("/authors"));
  return element("section", { className: "route-content" }, back,
    element("article", { className: "resource-card" },
      element("h2", {}, resource.author.display_name), authorCounts(resource.author, i18n)),
    element("article", { className: "resource-card" },
      element("h2", {}, label("statistics")), element("p", {}, label("help")),
      stats.presentations ? summary : element("p", {}, label("no_statistics")),
      stats.presentations ? element("div", { className: "author-statistics-table" }, table) : null));
}
