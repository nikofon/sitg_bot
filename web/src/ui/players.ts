import type { PlayersResource } from "../api/types";
import type { I18n } from "../i18n";
import { element } from "./dom";

export function renderPlayers(
  resource: PlayersResource, i18n: I18n, query: URLSearchParams,
  navigate: (path: string) => void, save: (filters: Record<string, string>) => void,
): HTMLElement {
  const update = (name: string, value: string): void => {
    const next = new URLSearchParams(query);
    if (value) next.set(name, value);
    else next.delete(name);
    if (name !== "offset") next.delete("offset");
    save(Object.fromEntries(["search", "order", "ruleset"].map(key => [key, next.get(key) ?? ""])));
    navigate(`/players${next.size ? `?${next}` : ""}`);
  };
  const ruleset = element("select", { id: "players-ruleset", disabled: !resource.rulesets.length },
    ...resource.rulesets.map(item => element("option", { value: item.key }, item.name)));
  ruleset.value = resource.ruleset_key ?? "";
  ruleset.addEventListener("change", () => update("ruleset", ruleset.value));
  const orders = resource.supported_orders ?? ["name_asc", "name_desc"];
  const order = element("select", { id: "players-order" },
    ...orders.map(value => element("option", { value }, i18n.t(`filters.${value}`))));
  order.value = query.get("order") ?? "name_asc";
  order.addEventListener("change", () => update("order", order.value));
  const search = element("input", {
    id: "players-search", type: "search", maxlength: "200", value: query.get("search") ?? "",
    placeholder: i18n.t("players.search"),
  });
  const form = element("form", { className: "search-form" },
    element("label", { for: "players-search" }, i18n.t("filters.search"), search),
    element("button", { type: "submit" }, i18n.t("filters.search_action")));
  form.addEventListener("submit", event => {
    event.preventDefault();
    update("search", search.value.trim());
  });
  const list = element("ul", { className: "resource-list" }, ...resource.items.map(player => {
    const path = `/players/${encodeURIComponent(player.id)}${resource.ruleset_key
      ? `?ruleset=${encodeURIComponent(resource.ruleset_key)}` : ""}`;
    return element("li", {}, element("a", {
      className: "resource-card player-directory-link", href: path,
      onclick: ((event: MouseEvent) => {
        if (event.button || event.ctrlKey || event.metaKey || event.shiftKey || event.altKey) return;
        event.preventDefault();
        navigate(path);
      }) as EventListener,
    }, element("strong", {}, player.label), element("span", {},
      `${i18n.t("profile.rating")}: ${player.rating.toFixed(1)} · ${i18n.t("profile.games")}: ${player.games}`)));
  }));
  const pages = element("nav", { className: "pagination", "aria-label": i18n.t("players.title") });
  if (Number(query.get("offset") ?? 0) > 0) pages.append(element("button", {
    type: "button", className: "secondary-button",
    onclick: (() => update("offset", "0")) as EventListener,
  }, i18n.t("players.first_page")));
  if (resource.next_offset !== null) pages.append(element("button", {
    type: "button", className: "secondary-button",
    onclick: (() => update("offset", String(resource.next_offset))) as EventListener,
  }, i18n.t("common.next")));
  return element("section", { className: "route-content" },
    element("div", { className: "filters" },
      element("label", { for: "players-ruleset" }, i18n.t("profile.ruleset"), ruleset),
      element("label", { for: "players-order" }, i18n.t("filters.order"), order), form),
    resource.items.length ? list : element("p", {}, i18n.t("players.empty")), pages);
}
