import { describe, expect, it, vi } from "vitest";
import type { LobbyResource } from "../api/types";
import { I18n } from "../i18n";
import { renderLobbyPackets } from "./lobby-packets";

const lobby = {
  selected_packets: [],
  packet_suggestions: [
    {
      packet_id: "alpha", name: "Alpha", year: 2020, published_at: "2024-12-31T23:00:00Z",
      lead_author: "Anna", authors: ["Anna"], fresh_play_unit_count: 2,
      total_play_unit_count: 5, playable_for_all: true,
    },
    {
      packet_id: "bravo", name: "Bravo", year: 2021, published_at: "2025-01-01T00:00:00Z",
      lead_author: "Boris", authors: ["Boris"], fresh_play_unit_count: 4,
      total_play_unit_count: 6, playable_for_all: true,
    },
    {
      packet_id: "charlie", name: "Charlie", year: 2022, published_at: "2025-02-01T00:00:00Z",
      lead_author: "Carol", authors: ["Carol"], fresh_play_unit_count: 7,
      total_play_unit_count: 7, playable_for_all: true,
    },
  ],
  available_actions: ["packet_select"],
} as unknown as LobbyResource;

describe("renderLobbyPackets sorting", () => {
  it("keeps the default order and sorts by fresh themes on demand", () => {
    const root = renderLobbyPackets(lobby, new I18n("en"), {}, true, vi.fn());
    const ids = (): string[] => Array.from(
      root.querySelectorAll("[data-packet-id]"),
      (node) => (node as HTMLElement).dataset.packetId ?? "",
    );
    expect(ids()).toEqual(["alpha", "bravo", "charlie"]);

    const sort = root.querySelector<HTMLSelectElement>("select[aria-label='Sort packets']");
    expect(sort).not.toBeNull();
    sort!.value = "fresh";
    sort!.dispatchEvent(new Event("change", { bubbles: true }));

    expect(ids()).toEqual(["charlie", "bravo", "alpha"]);
  });
});
