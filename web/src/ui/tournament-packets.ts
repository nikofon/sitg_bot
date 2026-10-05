import type { TournamentPacket } from "../api/types";
import type { I18n } from "../i18n";
import type { MessageKey } from "../i18n/en";
import { element } from "./dom";

export function renderTournamentPackets(
  packets: TournamentPacket[],
  i18n: I18n,
  actions: {
    viewInLibrary: (packet: TournamentPacket) => void;
    toggleBlock: (packet: TournamentPacket) => void;
  },
): HTMLElement {
  const container = element("div", { className: "lobby-packets" });
  const detail = (key: MessageKey, value: string): HTMLElement => element(
    "div", { className: "lobby-packet-detail" },
    element("dt", {}, i18n.t(key)), element("dd", {}, value),
  );
  container.append(...packets.map((packet) => {
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
  }));
  if (!packets.length) {
    container.append(element("p", {}, i18n.t("tournament_packets.empty")));
  }
  return container;
}
