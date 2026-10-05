import type { LobbyPacket, LobbyResource } from "../api/types";
import type { I18n } from "../i18n";
import type { MessageKey } from "../i18n/en";
import { element } from "./dom";
import { preserveListPosition } from "./scroll";

export type LobbyPacketFilters = Partial<Record<
  "name" | "author" | "year_from" | "year_to" | "publication_from" | "publication_to", string
>>;

export function renderLobbyPackets(
  lobby: LobbyResource,
  i18n: I18n,
  filters: LobbyPacketFilters,
  showPicker: boolean,
  mutate: (command: string, body: Record<string, unknown>) => Promise<void>,
  toggleBlock?: (packet: LobbyPacket) => Promise<void>,
): { element: HTMLElement; update: (next: LobbyResource) => void } {
  const container = element("div", { className: "lobby-packets" });
  const list = element("div", { className: "lobby-packets", "aria-live": "polite" });
  const cards = new Map<string, { node: HTMLElement; update: (packet: LobbyPacket, added: boolean, allowed: boolean) => void }>();
  const normalize = (value: string): string => value.trim().toLocaleLowerCase(i18n.locale);
  let sortMode: "default" | "fresh" | "fresh_zeroes_last" = "fresh_zeroes_last";
  const inRange = (year: number | null, from?: string, to?: string): boolean =>
    (!from && !to) || (year !== null && (!from || year >= Number(from)) && (!to || year <= Number(to)));
  const matches = (packet: LobbyPacket): boolean => {
    if (!showPicker) return true;
    const publicationYear = packet.published_at ? Number(packet.published_at.slice(0, 4)) : null;
    return normalize(packet.name).includes(normalize(filters.name ?? ""))
      && normalize([packet.lead_author, ...(packet.authors ?? [])].filter(Boolean).join(" "))
        .includes(normalize(filters.author ?? ""))
      && inRange(packet.year ?? null, filters.year_from, filters.year_to)
      && inRange(publicationYear, filters.publication_from, filters.publication_to);
  };
  const detail = (key: MessageKey, value: string): HTMLElement => element(
    "div", { className: "lobby-packet-detail" },
    element("dt", {}, i18n.t(key)), element("dd", {}, value),
  );
  const render = (): void => {
    const selected = new Set(lobby.selected_packets.map((packet) => packet.packet_id));
    const packets = [
      ...lobby.selected_packets,
      ...(showPicker ? lobby.packet_suggestions.filter((packet) => !selected.has(packet.packet_id)) : []),
    ];
    const visible = packets.filter(matches);
    if (sortMode === "fresh") {
      visible.sort((a, b) =>
        (b.fresh_play_unit_count ?? 0) - (a.fresh_play_unit_count ?? 0)
        || a.name.localeCompare(b.name, i18n.locale));
    } else if (sortMode === "fresh_zeroes_last") {
      const zeroFreshLast = (packet: LobbyPacket): number =>
        (packet.fresh_play_unit_count ?? 0) === 0 ? 1 : 0;
      visible.sort((a, b) =>
        zeroFreshLast(a) - zeroFreshLast(b)
        || (a.fresh_play_unit_count ?? 0) - (b.fresh_play_unit_count ?? 0)
        || a.name.localeCompare(b.name, i18n.locale));
    }
    const nodes = visible.map((packet) => {
      const added = selected.has(packet.packet_id);
      const allowed = (added || showPicker) && lobby.available_actions.includes(added ? "packet_remove" : "packet_select");
      let card = cards.get(packet.packet_id);
      if (!card) {
        const title = element("h3");
        const badge = element("span", { className: "lobby-packet-selected" }, i18n.t("lobby.packet_selected"));
        const blockedBadge = element("span", { className: "lobby-packet-selected" }, i18n.t("packet.blocked"));
        const details = element("dl", { className: "lobby-packet-details" },
          ...(["lobby.packet_year", "lobby.packet_publication_year", "lobby.packet_lead_author", "lobby.packet_authors", "lobby.packet_fresh"] as const)
            .map((key) => detail(key, "")));
        const access = element("p");
        const button = element("button", {
          type: "button",
          onclick: (() => {
            const isSelected = lobby.selected_packets.some((item) => item.packet_id === packet.packet_id);
            if ((isSelected || showPicker) && lobby.available_actions.includes(isSelected ? "packet_remove" : "packet_select")) {
              void mutate(isSelected ? "packet-remove" : "packet-select", { packet_id: packet.packet_id });
            }
          }) as EventListener,
        });
        const currentPacket = (): LobbyPacket | undefined =>
          lobby.selected_packets.find((item) => item.packet_id === packet.packet_id)
          ?? lobby.packet_suggestions.find((item) => item.packet_id === packet.packet_id);
        const blockButton = toggleBlock ? element("button", {
          type: "button",
          onclick: (() => {
            const current = currentPacket();
            if (current) void toggleBlock(current);
          }) as EventListener,
        }) : null;
        const node = element("article", { "data-packet-id": packet.packet_id }, title, details, access);
        let previous = "";
        card = { node, update: (current, isSelected, canChange) => {
          const signature = JSON.stringify([current, isSelected, canChange]);
          if (signature === previous) return;
          previous = signature;
          node.className = `resource-card lobby-packet-card${isSelected ? " is-selected" : ""}`;
          title.textContent = current.name;
          if (isSelected) node.insertBefore(badge, details);
          else badge.remove();
          if (current.blocked) node.insertBefore(blockedBadge, details);
          else blockedBadge.remove();
          const values = [current.year?.toString() ?? "—", current.published_at?.slice(0, 4) || "—",
            current.lead_author || "—", current.authors?.join(", ") || "—",
            `${current.fresh_play_unit_count ?? 0} / ${current.total_play_unit_count ?? 0}`];
          details.querySelectorAll("dd").forEach((value, index) => { value.textContent = values[index]!; });
          access.className = `lobby-packet-access ${current.playable_for_all ? "is-playable" : "is-unplayable"}`;
          access.textContent = `${i18n.t("lobby.packet_playable")}: ${i18n.t(current.playable_for_all ? "lobby.packet_yes" : "lobby.packet_no")}`;
          button.className = isSelected ? "secondary-button" : "primary-button";
          button.textContent = i18n.t(isSelected ? "lobby.packet_remove" : "lobby.packet_add");
          if (canChange && button.parentNode !== node) {
            if (blockButton?.parentNode === node) node.insertBefore(button, blockButton);
            else node.append(button);
          } else if (!canChange) button.remove();
          if (blockButton) {
            blockButton.className = current.blocked ? "secondary-button" : "warning-button";
            blockButton.textContent = i18n.t(current.blocked ? "packet.unblock" : "packet.block");
            if (blockButton.parentNode !== node) node.append(blockButton);
          }
        } };
        cards.set(packet.packet_id, card);
      }
      card.update(packet, added, allowed);
      return card.node;
    });
    const retained = new Set(nodes);
    for (const child of Array.from(list.children)) {
      if (!retained.has(child as HTMLElement)) child.remove();
    }
    nodes.forEach((node, index) => {
      if (list.children[index] !== node) list.insertBefore(node, list.children[index] ?? null);
    });
    const packetIds = new Set(packets.map((packet) => packet.packet_id));
    for (const id of cards.keys()) if (!packetIds.has(id)) cards.delete(id);
    if (!visible.length) list.append(element("p", {}, i18n.t(showPicker ? "lobby.packet_no_matches" : "lobby.packet_none")));
  };
  if (showPicker) {
    const controls = element("div", { className: "lobby-packet-filters", role: "search", "aria-label": i18n.t("lobby.packet_filters") });
    const input = (name: keyof LobbyPacketFilters, label: MessageKey, numeric = false): HTMLElement => element(
      "label", {}, i18n.t(label), element("input", {
        name, type: numeric ? "number" : "search", value: filters[name] ?? "",
        min: numeric ? "1000" : undefined, max: numeric ? "9999" : undefined,
        step: numeric ? "1" : undefined,
        oninput: ((event: Event) => {
          filters[name] = (event.currentTarget as HTMLInputElement).value;
          render();
        }) as EventListener,
      }),
    );
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
        for (const field of controls.querySelectorAll("input")) {
          field.value = "";
          delete filters[field.name as keyof LobbyPacketFilters];
        }
        render();
      }) as EventListener,
    }, i18n.t("lobby.packet_reset")));
    container.append(controls);
  }
  render();
  container.append(list);
  return { element: container, update: (next) => {
    preserveListPosition(list, () => {
      lobby = next;
      render();
    });
  } };
}
