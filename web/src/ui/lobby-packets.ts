import type { LobbyPacket, LobbyResource } from "../api/types";
import type { I18n } from "../i18n";
import type { MessageKey } from "../i18n/en";
import { element } from "./dom";

export type LobbyPacketFilters = Partial<Record<
  "name" | "author" | "year_from" | "year_to" | "publication_from" | "publication_to", string
>>;

export function renderLobbyPackets(
  lobby: LobbyResource,
  i18n: I18n,
  filters: LobbyPacketFilters,
  editable: boolean,
  mutate: (command: string, body: Record<string, unknown>) => Promise<void>,
  toggleBlock?: (packet: LobbyPacket) => Promise<void>,
): HTMLElement {
  const container = element("div", { className: "lobby-packets" });
  const list = element("div", { className: "lobby-packets", "aria-live": "polite" });
  const selected = new Set(lobby.selected_packets.map((packet) => packet.packet_id));
  const packets = [
    ...lobby.selected_packets,
    ...(editable ? lobby.packet_suggestions.filter((packet) => !selected.has(packet.packet_id)) : []),
  ];
  const normalize = (value: string): string => value.trim().toLocaleLowerCase(i18n.locale);
  let sortMode: "default" | "fresh" | "fresh_zeroes_last" = "fresh_zeroes_last";
  const inRange = (year: number | null, from?: string, to?: string): boolean =>
    (!from && !to) || (year !== null && (!from || year >= Number(from)) && (!to || year <= Number(to)));
  const matches = (packet: LobbyPacket): boolean => {
    if (!editable) return true;
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
    list.replaceChildren(...visible.map((packet) => {
      const added = selected.has(packet.packet_id);
      const command = added ? "packet-remove" : "packet-select";
      const allowed = lobby.available_actions.includes(added ? "packet_remove" : "packet_select");
      const buttons = element("div", { className: "settings-actions" });
      if (editable && allowed) {
        buttons.append(element("button", {
          type: "button", className: added ? "secondary-button" : "primary-button",
          onclick: (() => void mutate(command, { packet_id: packet.packet_id })) as EventListener,
        }, i18n.t(added ? "lobby.packet_remove" : "lobby.packet_add")));
      }
      if (toggleBlock) {
        buttons.append(element("button", {
          type: "button", className: packet.blocked ? "secondary-button" : "warning-button",
          onclick: (() => void toggleBlock(packet)) as EventListener,
        }, i18n.t(packet.blocked ? "packet.unblock" : "packet.block")));
      }
      return element("article", {
        className: `resource-card lobby-packet-card${added ? " is-selected" : ""}`,
        "data-packet-id": packet.packet_id,
      },
      element("h3", {}, packet.name),
      added ? element("span", { className: "lobby-packet-selected" }, i18n.t("lobby.packet_selected")) : null,
      packet.blocked ? element("span", { className: "lobby-packet-selected" }, i18n.t("packet.blocked")) : null,
      element("dl", { className: "lobby-packet-details" },
        detail("lobby.packet_year", packet.year?.toString() ?? "—"),
        detail("lobby.packet_publication_year", packet.published_at?.slice(0, 4) || "—"),
        detail("lobby.packet_lead_author", packet.lead_author || "—"),
        detail("lobby.packet_authors", packet.authors?.join(", ") || "—"),
        detail("lobby.packet_fresh", `${packet.fresh_play_unit_count ?? 0} / ${packet.total_play_unit_count ?? 0}`),
      ),
      element("p", { className: `lobby-packet-access ${packet.playable_for_all ? "is-playable" : "is-unplayable"}` },
        `${i18n.t("lobby.packet_playable")}: ${i18n.t(packet.playable_for_all ? "lobby.packet_yes" : "lobby.packet_no")}`),
      buttons.childElementCount ? buttons : null);
    }));
    if (!visible.length) list.append(element("p", {}, i18n.t(editable ? "lobby.packet_no_matches" : "lobby.packet_none")));
  };
  if (editable) {
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
  return container;
}
