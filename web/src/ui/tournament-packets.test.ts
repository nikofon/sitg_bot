import { afterEach, describe, expect, it, vi } from "vitest";
import type { TournamentPacket } from "../api/types";
import { I18n } from "../i18n";
import { renderTournamentPackets } from "./tournament-packets";

const packet: TournamentPacket = {
  packet_id: "alpha", version_id: "version", name: "Alpha", year: 2020,
  published_at: "2024-01-01T00:00:00Z", lead_author: "Anna", authors: ["Anna", "Boris"],
  playable: true, blocked: false, library_viewable: true,
  fresh_play_unit_count: 3, total_play_unit_count: 5,
};

const packets: TournamentPacket[] = [
  packet,
  {
    ...packet, packet_id: "bravo", name: "Bravo", year: 2019,
    published_at: "2025-01-01T00:00:00Z", lead_author: "Boris", authors: ["Boris"],
    fresh_play_unit_count: 0,
  },
  {
    ...packet, packet_id: "charlie", name: "Charlie", year: 2022,
    published_at: "2025-02-01T00:00:00Z", lead_author: "Carol", authors: ["Carol"],
    fresh_play_unit_count: 7,
  },
];

describe("renderTournamentPackets", () => {
  afterEach(() => {
    document.body.replaceChildren();
    vi.restoreAllMocks();
  });

  it("sorts by fresh themes with zero-fresh packets last by default", () => {
    const root = renderTournamentPackets(packets, new I18n("en"), {}, vi.fn(), {
      viewInLibrary: vi.fn(), toggleBlock: vi.fn(),
    });
    const ids = (): string[] => Array.from(
      root.querySelectorAll("[data-packet-id]"),
      (node) => (node as HTMLElement).dataset.packetId ?? "",
    );
    expect(ids()).toEqual(["alpha", "charlie", "bravo"]);

    const sort = root.querySelector<HTMLSelectElement>("select[aria-label='Sort packets']");
    expect(sort).not.toBeNull();
    expect(sort!.value).toBe("fresh_zeroes_last");

    sort!.value = "fresh";
    sort!.dispatchEvent(new Event("change", { bubbles: true }));
    expect(ids()).toEqual(["charlie", "alpha", "bravo"]);

    sort!.value = "default";
    sort!.dispatchEvent(new Event("change", { bubbles: true }));
    expect(ids()).toEqual(["alpha", "bravo", "charlie"]);
  });

  it("filters by title, authors and inclusive years, and resets", () => {
    const save = vi.fn();
    const filters: Record<string, string> = {};
    const root = renderTournamentPackets(packets, new I18n("en"), filters, save, {
      viewInLibrary: vi.fn(), toggleBlock: vi.fn(),
    });
    const input = (name: string, value: string): void => {
      const field = root.querySelector<HTMLInputElement>(`input[name="${name}"]`)!;
      field.value = value;
      field.dispatchEvent(new Event("input", { bubbles: true }));
    };
    const ids = (): string[] => Array.from(
      root.querySelectorAll("[data-packet-id]"),
      (node) => (node as HTMLElement).dataset.packetId ?? "",
    );

    input("name", "bravo");
    expect(ids()).toEqual(["bravo"]);
    input("name", "");

    input("author", "boris");
    expect(ids()).toEqual(["alpha", "bravo"]);
    input("author", "");

    input("year_from", "2020");
    expect(ids()).toEqual(["alpha", "charlie"]);
    input("year_to", "2021");
    expect(ids()).toEqual(["alpha"]);
    input("year_from", "");
    input("year_to", "");

    input("publication_from", "2025");
    expect(ids()).toEqual(["charlie", "bravo"]);
    input("publication_from", "");
    input("publication_to", "2024");
    expect(ids()).toEqual(["alpha"]);
    input("publication_to", "");
    expect(ids()).toEqual(["alpha", "charlie", "bravo"]);
    expect(save).toHaveBeenCalled();

    root.querySelector<HTMLButtonElement>("button")!.click();
    expect(root.querySelector<HTMLInputElement>('input[name="name"]')!.value).toBe("");
    expect(save).toHaveBeenCalledWith({});
  });

  it("shows distinct empty messages for missing and non-matching packets", () => {
    const actions = { viewInLibrary: vi.fn(), toggleBlock: vi.fn() };
    const empty = renderTournamentPackets([], new I18n("en"), {}, vi.fn(), actions);
    expect(empty.textContent).toContain("No packets are assigned to this tournament.");

    const filtered = renderTournamentPackets(packets, new I18n("en"), { name: "nothing" }, vi.fn(), actions);
    expect(filtered.textContent).toContain("No packets match these filters.");
  });

  it("keeps card actions for view-in-library and block toggling", () => {
    const viewInLibrary = vi.fn();
    const toggleBlock = vi.fn();
    const root = renderTournamentPackets(packets, new I18n("en"), {}, vi.fn(), {
      viewInLibrary, toggleBlock,
    });
    const alpha = root.querySelector<HTMLElement>('[data-packet-id="alpha"]')!;
    const buttons = alpha.querySelectorAll<HTMLButtonElement>("button");
    buttons[0]!.click();
    expect(viewInLibrary).toHaveBeenCalledWith(packets[0]);
    buttons[1]!.click();
    expect(toggleBlock).toHaveBeenCalledWith(packets[0]);
  });
});
