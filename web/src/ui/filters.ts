import type { I18n } from "../i18n";
import type { MessageKey } from "../i18n/en";
import type { RouteId } from "../routing/routes";
import { element } from "./dom";

interface FilterOption {
  value: string;
  label: MessageKey;
}

interface FilterDefinition {
  name: string;
  label: MessageKey;
  options: FilterOption[];
}

const routeFilters: Partial<Record<RouteId, FilterDefinition[]>> = {
  tournaments: [
    {
      name: "phase",
      label: "filters.status",
      options: [
        { value: "", label: "common.all" },
        { value: "upcoming", label: "filters.upcoming" },
        { value: "ongoing", label: "filters.ongoing" },
        { value: "past", label: "filters.past" },
      ],
    },
    {
      name: "relationship",
      label: "filters.relationship",
      options: [
        { value: "", label: "common.all" },
        { value: "discoverable", label: "filters.discoverable" },
        { value: "registered", label: "filters.registered" },
        { value: "participating", label: "filters.participating" },
        { value: "managed", label: "filters.managed" },
      ],
    },
    {
      name: "registration",
      label: "filters.registration",
      options: [
        { value: "", label: "common.all" },
        { value: "open", label: "filters.registration_open" },
        { value: "closed", label: "filters.registration_closed" },
      ],
    },
    {
      name: "order",
      label: "filters.order",
      options: [
        { value: "starts_asc", label: "filters.starts_asc" },
        { value: "starts_desc", label: "filters.starts_desc" },
        { value: "name_asc", label: "filters.name_asc" },
        { value: "name_desc", label: "filters.name_desc" },
      ],
    },
  ],
  ongoing: [
    {
      name: "kind",
      label: "filters.kind",
      options: [
        { value: "", label: "common.all" },
        { value: "lobby", label: "filters.lobbies" },
        { value: "game", label: "filters.games" },
      ],
    },
  ],
};

export function filterNames(routeId: RouteId): string[] {
  if (routeId === "players") return ["search", "order", "ruleset"];
  const names = routeFilters[routeId]?.map((filter) => filter.name) ?? [];
  return routeId === "tournaments" ? [...names, "search"] : names;
}

export function renderFilters(
  routeId: RouteId,
  query: URLSearchParams,
  i18n: I18n,
  onChange: (name: string, value: string) => void,
): HTMLElement | null {
  const filters = routeFilters[routeId];
  if (!filters) return null;
  const group = element("div", { className: "filters", "aria-label": i18n.t("filters.status") });
  for (const filter of filters) {
    const id = `filter-${filter.name}`;
    const select = element("select", {
      id,
      name: filter.name,
      onchange: ((event: Event) => {
        const target = event.currentTarget as HTMLSelectElement;
        onChange(filter.name, target.value);
      }) as EventListener,
    });
    for (const option of filter.options) {
      const item = element("option", { value: option.value }, i18n.t(option.label));
      item.selected = query.get(filter.name) === option.value;
      select.append(item);
    }
    group.append(element("label", { for: id }, i18n.t(filter.label), select));
  }
  if (routeId === "tournaments") {
    const form = element("form", {
      className: "search-form",
      onsubmit: ((event: Event) => {
        event.preventDefault();
        const input = (event.currentTarget as HTMLFormElement).elements.namedItem(
          "search",
        ) as HTMLInputElement;
        onChange("search", input.value.trim());
      }) as EventListener,
    });
    form.append(
      element(
        "label",
        { for: "filter-search" },
        i18n.t("filters.search"),
        element("input", {
          id: "filter-search",
          name: "search",
          type: "search",
          value: query.get("search") ?? "",
          maxlength: "200",
          placeholder: i18n.t("filters.search_placeholder"),
        }),
      ),
      element("button", { type: "submit" }, i18n.t("filters.search_action")),
    );
    group.append(form);
  }
  return group;
}
