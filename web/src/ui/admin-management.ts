import type { AdminCard, AdminManagementResource, AdminValue } from "../api/types";
import type { I18n } from "../i18n";
import type { MessageKey } from "../i18n/en";
import { element } from "./dom";
import { saveReminder } from "./save-reminder";

export function renderAdminManagement(
  resource: AdminManagementResource, i18n: I18n, filters: Record<string, string>,
  save: (filters: Record<string, string>) => void,
  navigate: (path: string) => void,
  action: (card: AdminCard, command: string, button: HTMLButtonElement) => void,
  saveWeight: (card: AdminCard, weight: number, button: HTMLButtonElement) => void = () => undefined,
): HTMLElement {
  const label = (key: string): string => {
    const translated = `admin_management.${key}` as MessageKey;
    const value = i18n.t(translated);
    return value ?? key.replaceAll("_", " ");
  };
  const name = (card: Record<string, AdminValue>): string =>
    String(card.name ?? card.display_name ?? card.public_nickname ?? card.telegram_username ?? card.id ?? "—");
  const link = (path: string, text: string): HTMLAnchorElement => element("a", {
    href: path, onclick: ((event: Event) => { event.preventDefault(); navigate(path); }) as EventListener,
  }, text);
  const metadata = (data: Record<string, AdminValue>): HTMLElement => element("dl", {
    className: "admin-metadata",
  }, ...Object.entries(data).flatMap(([key, value]) => {
    if (value !== null && typeof value === "object") {
      const entries = Array.isArray(value) ? value : [value];
      return [element("div", {}, element("details", {},
        element("summary", {}, `${label(key)}${Array.isArray(value) ? ` (${value.length})` : ""}`),
        ...entries.map((entry) => {
          if (entry === null || typeof entry !== "object") return element("p", {}, String(entry ?? "—"));
          if (Array.isArray(entry)) return element("p", {}, entry.join(", "));
          const profile = (key === "players" || key === "managers" || key === "player"
              || key === "participants" || key === "host")
            && typeof entry.id === "string"
            ? link(`/players/${entry.id}`, name(entry))
            : (key === "tournaments" || key === "tournament") && typeof entry.id === "string"
              ? link(`/tournaments?role=admin&info=${entry.id}`, name(entry)) : null;
          return element("section", { className: "admin-related" }, profile, metadata(entry));
        })))];
    }
    return [element("div", {}, element("dt", {}, label(key)),
      element("dd", {}, typeof value === "boolean" ? label(value ? "yes" : "no") : String(value ?? "—")))];
  }));
  const tabs = element("nav", { className: "settings-actions", "aria-label": label("title") },
    ...(["tournaments", "authors", "players", "packets", "link_requests", "ongoing_games"] as const).map((section) => element("button", {
      type: "button", className: resource.section === section ? "primary-button" : "secondary-button",
      "aria-current": resource.section === section ? "page" : undefined,
      onclick: (() => navigate(`/admin/management?section=${section}`)) as EventListener,
    }, label(section))));
  const list = element("div", { className: "lobby-packets", "aria-live": "polite" });
  const card = (item: AdminCard): HTMLElement => {
    const buttons = element("div", { className: "settings-actions" });
    const add = (command: string): void => {
      const className = command === "abolish" || command === "ban" || command === "reject"
        || command === "merge" ? "danger-button"
        : command === "approve" ? "primary-button"
        : command === "halt" ? "secondary-button warning-button" : "secondary-button";
      const button = element("button", { type: "button", className }, label(command));
      button.addEventListener("click", () => action(item, command, button));
      buttons.append(button);
    };
    let profile: HTMLElement | null = null;
    if (resource.section === "tournaments") {
      profile = link(`/tournaments?role=admin&info=${item.id}`, label("profile"));
      if (item.moderation_status === "halted") add("resume");
      else if (item.moderation_status === "normal" && item.status === "active"
        && item.actual_starts_at && !item.actual_ends_at) add("halt");
      if (item.moderation_status !== "abolished") add("abolish");
    } else if (resource.section === "link_requests") {
      profile = typeof item.player === "object" && item.player !== null
        && typeof (item.player as { id?: unknown }).id === "string"
        ? link(`/players/${(item.player as { id: string }).id}`, label("profile")) : null;
      if (item.status === "pending") { add("approve"); add("reject"); }
    } else if (resource.section === "authors") {
      add("edit_author"); add("link"); add("merge");
      if (Array.isArray(item.split_names) && item.split_names.length > 1) add("split");
    }
    else if (resource.section === "ongoing_games") {
      profile = typeof item.tournament === "object" && item.tournament !== null
        && !Array.isArray(item.tournament)
        && typeof (item.tournament as { id?: unknown }).id === "string"
        ? link(`/tournaments?role=admin&info=${(item.tournament as { id: string }).id}`,
          label("profile")) : null;
    }
    else if (resource.section === "players") {
      profile = link(`/players/${item.id}`, label("profile"));
      if (!item.administrator) add(item.ban ? "unban" : "ban");
      add("clear_suspicion"); add("review_suspicion");
    } else { add("view"); add("download"); }
    let weightControl: HTMLElement | null = null;
    if (resource.section === "tournaments" && item.moderation_status === "normal") {
      const settings = typeof item.settings === "object" && item.settings !== null
        && !Array.isArray(item.settings)
        ? item.settings as { policies?: Record<string, AdminValue> } : null;
      const current = Number(settings?.policies?.ruleset_rating_weight ?? 1);
      const initial = Math.min(Math.max(Number.isFinite(current) ? current : 1, 0.1), 1);
      const slider = element("input", {
        type: "range", min: "0.1", max: "1", step: "0.05", value: String(initial),
        "aria-label": label("rating_weight"),
      });
      const readout = element("output", {}, String(initial));
      const confirm = element("button", { type: "button", className: "weight-save-button" }, "V");
      confirm.disabled = Number(slider.value) === current;
      slider.addEventListener("input", () => {
        readout.textContent = slider.value;
        confirm.disabled = Number(slider.value) === current;
      });
      confirm.addEventListener("click", () => saveWeight(item, Number(slider.value), confirm));
      weightControl = element("div", { className: "admin-weight" },
        element("label", {}, label("rating_weight"), slider), readout, confirm);
    }
    const essentialKeys = {
      tournaments: ["id", "status", "moderation_status", "type", "ruleset", "language", "starts_at", "actual_starts_at", "actual_ends_at", "participants", "managers", "packets"],
      authors: ["id", "questions", "themes", "packet_count", "players", "tournaments", "packets"],
      players: ["id", "real_name", "telegram_username", "suspicion", "reputation", "ban", "games_played", "rulesets", "reports"],
      packets: ["id", "packet_id", "version_number", "year", "language", "state", "library_released_at", "themes", "questions", "authors", "tournaments"],
      link_requests: ["player", "author", "status", "request_note", "created_at", "decided_at"],
      ongoing_games: ["id", "tournament", "host", "status", "phase", "paused", "participant_count", "participants", "type", "ruleset", "created_at", "last_activity_at"],
    }[resource.section];
    const essential = Object.fromEntries(Object.entries(item).filter(([key]) => essentialKeys.includes(key)));
    const remaining = Object.fromEntries(Object.entries(item).filter(([key]) => !essentialKeys.includes(key)
      && !(resource.section === "authors" && ["version", "split_names"].includes(key))));
    return element("article", { className: "resource-card admin-card", "data-resource-id": item.id },
      element("h2", {}, name(item)), profile, metadata(essential), weightControl,
      element("details", {}, element("summary", {}, label("details")), metadata(remaining)), buttons);
  };
  const render = (): void => {
    const search = (filters.search ?? "").trim().toLocaleLowerCase(i18n.locale);
    const order = filters.order ?? (resource.section === "tournaments" ? "starts_at:asc"
      : resource.section === "link_requests" ? "created_at:desc"
      : resource.section === "ongoing_games" ? "created_at:desc" : "name:asc");
    const [key, direction] = order.split(":");
    const items = resource.items.filter((item) => JSON.stringify(item).toLocaleLowerCase(i18n.locale).includes(search));
    const value = (item: AdminCard): string | number => key === "name" ? name(item) :
      typeof item[key!] === "number" ? item[key!] as number : String(item[key!] ?? "");
    items.sort((a, b) => {
      const first = value(a), second = value(b);
      const comparison = typeof first === "number" && typeof second === "number"
        ? first - second : String(first).localeCompare(String(second), i18n.locale, { numeric: true });
      return comparison * (direction === "desc" ? -1 : 1) || a.id.localeCompare(b.id);
    });
    const summary = resource.section === "ongoing_games"
      ? [element("p", { className: "resource-summary" },
        `${label("ongoing_games")}: ${resource.items.length}`)]
      : [];
    list.replaceChildren(...summary, ...items.map(card));
    if (!items.length) list.append(element("p", {}, label("empty")));
  };
  const search = element("input", { type: "search", value: filters.search ?? "", "aria-label": label("search") });
  search.addEventListener("input", () => { filters.search = search.value; save(filters); render(); });
  const sortKeys = ["name", "created_at", ...{
    tournaments: ["starts_at", "participants"], authors: ["questions", "themes", "packet_count"],
    players: ["suspicion", "reputation", "games_played"], packets: ["year", "published_at", "questions", "themes"],
    link_requests: ["status", "decided_at"],
    ongoing_games: ["status", "phase", "participant_count", "last_activity_at"],
  }[resource.section]];
  const sort = element("select", { "aria-label": label("sort") }, ...sortKeys.flatMap((key) =>
    ["asc", "desc"].map((direction) => element("option", { value: `${key}:${direction}` },
      `${label(key)} ${direction === "asc" ? "↑" : "↓"}`))));
  sort.value = filters.order ?? (resource.section === "tournaments" ? "starts_at:asc"
    : resource.section === "link_requests" || resource.section === "ongoing_games"
      ? "created_at:desc" : "name:asc");
  sort.addEventListener("change", () => { filters.order = sort.value; save(filters); render(); });
  render();
  return element("section", { className: "route-content" }, tabs,
    resource.section === "tournaments" ? saveReminder(i18n) : null,
    element("div", { className: "lobby-packet-filters", role: "search" },
      element("label", {}, label("search"), search), element("label", {}, label("sort"), sort)), list);
}
