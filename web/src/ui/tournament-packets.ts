import type { TournamentPacket } from "../api/types";
import type { I18n } from "../i18n";
import type { MessageKey } from "../i18n/en";
import { element } from "./dom";
import { preserveListPosition } from "./scroll";

export function renderTournamentPackets(
  packets: TournamentPacket[],
  i18n: I18n,
  filters: Record<string, string>,
  saveFilters: (filters: Record<string, string>) => void,
  actions: {
    viewInLibrary: (packet: TournamentPacket) => void;
    toggleBlock: (packet: TournamentPacket) => void;
    changeFilters?: (filters: Record<string, string>) => void;
  },
): HTMLElement & { update: (packets: TournamentPacket[], loading?: boolean) => void } {
  const controls = element("div", { className: "lobby-packet-filters", role: "search", "aria-label": i18n.t("lobby.packet_filters") });
  const list = element("div", { className: "lobby-packets", "aria-live": "polite" });
  const loading = element("p", { role: "status", hidden: true }, i18n.t("common.loading"));
  const cards = new Map<string, { node: HTMLElement; signature: string; packet: TournamentPacket }>();
  const normalize = (value: string): string => value.trim().toLocaleLowerCase(i18n.locale);
  let sortMode: "default" | "fresh_zeroes_last" | "fresh" | "fresh_asc" | "name_asc" | "name_desc" | "year_asc" | "year_desc" = "fresh_zeroes_last";
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
    let cached = cards.get(packet.packet_id);
    const signature = JSON.stringify(packet);
    if (cached?.signature === signature) return cached.node;
    const current = (): TournamentPacket => cards.get(packet.packet_id)!.packet;
    const buttons = element("div", { className: "settings-actions" });
    buttons.append(element("button", {
      type: "button", className: packet.library_viewable ? "primary-button" : "secondary-button",
      disabled: !packet.library_viewable,
      title: packet.library_viewable ? "" : i18n.t("tournament_packets.library_unavailable"),
      onclick: (() => {
        if (current().library_viewable) actions.viewInLibrary(current());
      }) as EventListener,
    }, i18n.t("tournament_packets.view_in_library")));
    buttons.append(element("button", {
      type: "button", className: packet.blocked ? "secondary-button" : "warning-button",
      onclick: (() => actions.toggleBlock(current())) as EventListener,
    }, i18n.t(packet.blocked ? "packet.unblock" : "packet.block")));
    const node = element("article", {
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
    if (cached) {
      // Retain action buttons so a mutation keeps keyboard focus and event handlers.
      const oldButtons = cached.node.querySelectorAll("button");
      node.querySelectorAll("button").forEach((button, index) => {
        const old = oldButtons[index]!;
        old.className = button.className;
        old.textContent = button.textContent;
        old.disabled = button.disabled;
        old.title = button.title;
        button.replaceWith(old);
      });
      cached.node.replaceChildren(...Array.from(node.childNodes));
      cached.packet = packet;
      cached.signature = signature;
    } else {
      cached = { node, packet, signature };
      cards.set(packet.packet_id, cached);
    }
    return cached.node;
  };
  const render = (): void => {
    const visible = packets.filter(matches);
    const byName = (a: TournamentPacket, b: TournamentPacket): number => a.name.localeCompare(b.name, i18n.locale);
    const byYear = (a: TournamentPacket, b: TournamentPacket, direction: 1 | -1): number => {
      if (a.year == null && b.year == null) return 0;
      if (a.year == null) return 1;
      if (b.year == null) return -1;
      return (a.year - b.year) * direction;
    };
    if (sortMode === "fresh") {
      visible.sort((a, b) =>
        (b.fresh_play_unit_count ?? 0) - (a.fresh_play_unit_count ?? 0)
        || byName(a, b));
    } else if (sortMode === "fresh_asc") {
      visible.sort((a, b) =>
        (a.fresh_play_unit_count ?? 0) - (b.fresh_play_unit_count ?? 0)
        || byName(a, b));
    } else if (sortMode === "fresh_zeroes_last") {
      const zeroFreshLast = (packet: TournamentPacket): number =>
        (packet.fresh_play_unit_count ?? 0) === 0 ? 1 : 0;
      visible.sort((a, b) =>
        zeroFreshLast(a) - zeroFreshLast(b)
        || (a.fresh_play_unit_count ?? 0) - (b.fresh_play_unit_count ?? 0)
        || byName(a, b));
    } else if (sortMode === "name_asc") {
      visible.sort(byName);
    } else if (sortMode === "name_desc") {
      visible.sort((a, b) => byName(b, a));
    } else if (sortMode === "year_asc") {
      visible.sort((a, b) => byYear(a, b, 1) || byName(a, b));
    } else if (sortMode === "year_desc") {
      visible.sort((a, b) => byYear(a, b, -1) || byName(a, b));
    }
    const nodes = visible.map(card);
    const retained = new Set(nodes);
    for (const child of Array.from(list.children)) if (!retained.has(child as HTMLElement)) child.remove();
    nodes.forEach((node, index) => {
      if (list.children[index] !== node) list.insertBefore(node, list.children[index] ?? null);
    });
    const packetIds = new Set(packets.map((packet) => packet.packet_id));
    for (const id of cards.keys()) if (!packetIds.has(id)) cards.delete(id);
    if (!visible.length) {
      list.append(element("p", {}, i18n.t(packets.length ? "lobby.packet_no_matches" : "tournament_packets.empty")));
    }
  };
  const input = (name: string, label: MessageKey, numeric = false): HTMLElement => element("label", {},
    i18n.t(label), element("input", {
      name, type: numeric ? "number" : "search", value: filters[name] ?? "",
      maxlength: numeric ? undefined : "200",
      min: numeric ? "1000" : undefined, max: numeric ? "9999" : undefined,
      step: numeric ? "1" : undefined,
      oninput: ((event: Event) => {
        filters[name] = (event.currentTarget as HTMLInputElement).value;
        saveFilters(filters);
        render();
        actions.changeFilters?.(filters);
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
    element("option", { value: "fresh_asc" }, i18n.t("lobby.packet_sort_fresh_asc")),
    element("option", { value: "name_asc" }, i18n.t("filters.name_asc")),
    element("option", { value: "name_desc" }, i18n.t("filters.name_desc")),
    element("option", { value: "year_asc" }, i18n.t("lobby.packet_sort_year_asc")),
    element("option", { value: "year_desc" }, i18n.t("lobby.packet_sort_year_desc")),
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
      actions.changeFilters?.(filters);
    }) as EventListener,
  }, i18n.t("lobby.packet_reset")));
  render();
  return Object.assign(element("section", { className: "route-content" }, controls, list, loading), {
    update: (next: TournamentPacket[], isLoading = false): void => {
      preserveListPosition(list, () => {
        packets = next;
        render();
        loading.hidden = !isLoading;
        list.setAttribute("aria-busy", String(isLoading));
      });
    },
  });
}
