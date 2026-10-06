import type { TournamentPacket } from "../api/types";
import type { I18n } from "../i18n";
import type { MessageKey } from "../i18n/en";
import { element } from "./dom";

export function renderTournamentPackets(
  packets: TournamentPacket[],
  i18n: I18n,
  filters: Record<string, string>,
  saveFilters: (filters: Record<string, string>) => void,
  actions: {
    viewInLibrary: (packet: TournamentPacket) => void;
    toggleBlock: (packet: TournamentPacket) => void;
  },
): HTMLElement {
  const controls = element("div", { className: "lobby-packet-filters", role: "search", "aria-label": i18n.t("lobby.packet_filters") });
  const list = element("div", { className: "lobby-packets", "aria-live": "polite" });
  const normalize = (value: string): string => value.trim().toLocaleLowerCase(i18n.locale);
  let sortMode: "default" | "fresh" | "fresh_zeroes_last" = "fresh_zeroes_last";
  const inRange = (year: number | null, from: string, to: string): boolean =>
    (!filters[from] && !filters[to]) || (year !== null
      && (!filters[from] || year >= Number(filters[from]))
      && (!filters[to] || year <= Number(filters[to])));
  const matches = (packet: TournamentPacket): boolean =>
    normalize(packet.name).includes(normalize(filters.name ?? ""))
    && normalize([packet.lead_author, ...(packet.authors ?? [])].filter(Boolean).join(" "))
      .includes(normalize(filters.author ?? ""))
    && inRange(packet.year, "year_from", "year_to")
    && inRange(packet.published_at ? Number(packet.published_at.slice(0, 4)) : null, "publication_from", "publication_to");
  const detail = (key: MessageKey, value: string): HTMLElement => element(
    "div", { className: "lobby-packet-detail" },
    element("dt", {}, i18n.t(key)), element("dd", {}, value),
  );
  const card = (packet: TournamentPacket): HTMLElement => {
    const buttons = element("div", { className: "settings-actions" });
    buttons.append(element("button", {
      type: "button", className: packet.library_viewable ? "primary-button" : "secondary-button",
      disabled: !packet.library_viewable,
      title: packet.library_viewable ? "" : i18n.t("tournament_packets.library_unavailable"),
      onclick: (() => {
        if (packet.library_viewable) actions.viewInLibrary(packet);
      }) as EventListener,
    }, i18n.t("tournament_packets.view_in_library")));
    buttons.append(element("button", {
      type: "button", className: packet.blocked ? "secondary-button" : "warning-button",
      onclick: (() => actions.toggleBlock(packet)) as EventListener,
    }, i18n.t(packet.blocked ? "packet.unblock" : "packet.block")));
    return element("article", {
      className: "resource-card lobby-packet-card",
      "data-packet-id": packet.packet_id,
    },
    element("h3", {}, packet.name),
    packet.blocked ? element("span", { className: "lobby-packet-selected" }, i18n.t("packet.blocked")) : null,
    element("dl", { className: "lobby-packet-details" },
      detail("lobby.packet_year", packet.year?.toString() ?? "—"),
      detail("lobby.packet_publication_year", packet.published_at?.slice(0, 4) || "—"),
      detail("lobby.packet_lead_author", packet.lead_author || "—"),
      detail("lobby.packet_authors", packet.authors?.join(", ") || "—"),
      detail("lobby.packet_fresh", `${packet.fresh_play_unit_count} / ${packet.total_play_unit_count}`),
    ),
    element("p", { className: `lobby-packet-access ${packet.playable ? "is-playable" : "is-unplayable"}` },
      `${i18n.t("lobby.packet_playable")}: ${i18n.t(packet.playable ? "lobby.packet_yes" : "lobby.packet_no")}`),
    buttons);
  };
  const render = (): void => {
    const visible = packets.filter(matches);
    if (sortMode === "fresh") {
      visible.sort((a, b) =>
        (b.fresh_play_unit_count ?? 0) - (a.fresh_play_unit_count ?? 0)
        || a.name.localeCompare(b.name, i18n.locale));
    } else if (sortMode === "fresh_zeroes_last") {
      const zeroFreshLast = (packet: TournamentPacket): number =>
        (packet.fresh_play_unit_count ?? 0) === 0 ? 1 : 0;
      visible.sort((a, b) =>
        zeroFreshLast(a) - zeroFreshLast(b)
        || (a.fresh_play_unit_count ?? 0) - (b.fresh_play_unit_count ?? 0)
        || a.name.localeCompare(b.name, i18n.locale));
    }
    list.replaceChildren(...visible.map(card));
    if (!visible.length) {
      list.append(element("p", {}, i18n.t(packets.length ? "lobby.packet_no_matches" : "tournament_packets.empty")));
    }
  };
  const input = (name: string, label: MessageKey, numeric = false): HTMLElement => element("label", {},
    i18n.t(label), element("input", {
      name, type: numeric ? "number" : "search", value: filters[name] ?? "",
      min: numeric ? "1000" : undefined, max: numeric ? "9999" : undefined,
      step: numeric ? "1" : undefined,
      oninput: ((event: Event) => {
        filters[name] = (event.currentTarget as HTMLInputElement).value;
        saveFilters(filters);
        render();
      }) as EventListener,
    }));
  controls.append(input("name", "lobby.packet_search"), input("author", "lobby.packet_authors"));
  const sort = element("select", {
    "aria-label": i18n.t("lobby.packet_sort"),
    onchange: (() => {
      sortMode = (sort.value as typeof sortMode) || "default";
      render();
    }) as EventListener,
  });
  sort.append(
    element("option", { value: "default" }, i18n.t("lobby.packet_sort_default")),
    element("option", { value: "fresh_zeroes_last", selected: true }, i18n.t("lobby.packet_sort_fresh_zeroes_last")),
    element("option", { value: "fresh" }, i18n.t("lobby.packet_sort_fresh")),
  );
  controls.append(sort);
  for (const [label, from, to] of [
    ["lobby.packet_year", "year_from", "year_to"],
    ["lobby.packet_publication_year", "publication_from", "publication_to"],
  ] as const) {
    controls.append(element("fieldset", { className: "lobby-packet-year-range" },
      element("legend", {}, i18n.t(label)), input(from, "lobby.packet_from", true), input(to, "lobby.packet_to", true)));
  }
  controls.append(element("button", {
    type: "button", className: "secondary-button",
    onclick: (() => {
      for (const key of Object.keys(filters)) delete filters[key];
      for (const field of controls.querySelectorAll("input")) field.value = "";
      saveFilters(filters);
      render();
    }) as EventListener,
  }, i18n.t("lobby.packet_reset")));
  render();
  return element("section", { className: "route-content" }, controls, list);
}
