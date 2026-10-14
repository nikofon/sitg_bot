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
  {
    ...packet, packet_id: "unknown", name: "Unknown", year: null,
    published_at: "2026-01-01T00:00:00Z", lead_author: "Uma", authors: ["Uma"],
    fresh_play_unit_count: 1,
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
    expect(ids()).toEqual(["unknown", "alpha", "charlie", "bravo"]);

    const sort = root.querySelector<HTMLSelectElement>("select[aria-label='Sort packets']");
    expect(sort).not.toBeNull();
    expect(sort!.value).toBe("fresh_zeroes_last");

    sort!.value = "fresh";
    sort!.dispatchEvent(new Event("change", { bubbles: true }));
    expect(ids()).toEqual(["charlie", "alpha", "unknown", "bravo"]);

    sort!.value = "fresh_asc";
    sort!.dispatchEvent(new Event("change", { bubbles: true }));
    expect(ids()).toEqual(["bravo", "unknown", "alpha", "charlie"]);

    sort!.value = "name_asc";
    sort!.dispatchEvent(new Event("change", { bubbles: true }));
    expect(ids()).toEqual(["alpha", "bravo", "charlie", "unknown"]);

    sort!.value = "name_desc";
    sort!.dispatchEvent(new Event("change", { bubbles: true }));
    expect(ids()).toEqual(["unknown", "charlie", "bravo", "alpha"]);

    sort!.value = "year_asc";
    sort!.dispatchEvent(new Event("change", { bubbles: true }));
    expect(ids()).toEqual(["bravo", "alpha", "charlie", "unknown"]);

    sort!.value = "year_desc";
    sort!.dispatchEvent(new Event("change", { bubbles: true }));
    expect(ids()).toEqual(["charlie", "alpha", "bravo", "unknown"]);

    sort!.value = "default";
    sort!.dispatchEvent(new Event("change", { bubbles: true }));
    expect(ids()).toEqual(["alpha", "bravo", "charlie", "unknown"]);
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
    expect(ids()).toEqual(["unknown", "charlie", "bravo"]);
    input("publication_from", "");
    input("publication_to", "2024");
    expect(ids()).toEqual(["alpha"]);
    input("publication_to", "");
    expect(ids()).toEqual(["unknown", "alpha", "charlie", "bravo"]);
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

  it("sorts progressive updates without requesting on sort and retains focus when blocked", () => {
    const changeFilters = vi.fn();
    const toggleBlock = vi.fn();
    const root = renderTournamentPackets([packets[0]!], new I18n("en"), {}, vi.fn(), {
      viewInLibrary: vi.fn(), toggleBlock, changeFilters,
    });
    document.body.append(root);
    const alpha = root.querySelector<HTMLElement>('[data-packet-id="alpha"]')!;
    const button = alpha.querySelectorAll<HTMLButtonElement>("button")[1]!;
    button.focus();
    root.update(packets, true);
    expect(Array.from(root.querySelectorAll("article"), (node) => node.dataset.packetId))
      .toEqual(["unknown", "alpha", "charlie", "bravo"]);
    expect(root.querySelector('[role="status"]')?.hasAttribute("hidden")).toBe(false);
    const sort = root.querySelector<HTMLSelectElement>("select")!;
    sort.value = "name_desc";
    sort.dispatchEvent(new Event("change"));
    expect(changeFilters).not.toHaveBeenCalled();
    const blocked = { ...packet, blocked: true, fresh_play_unit_count: 0 };
    root.update([blocked, ...packets.slice(1)]);
    expect(root.querySelector('[data-packet-id="alpha"]')).toBe(alpha);
    expect(alpha.querySelectorAll("button")[1]).toBe(button);
    expect(document.activeElement).toBe(button);
    expect(button.textContent).toBe("Unblock");
    button.click();
    expect(toggleBlock).toHaveBeenCalledWith(blocked);
  });

  it("settles locale sorting, name ties, unknown years and zero freshness across batches", () => {
    const entries = [
      ["a", "Бета", 5, 2021], ["b", "Альфа", 0, null], ["c", "Ёлка", 2, 2020],
      ["d", "Альфа", 2, null], ["e", "Альфа", 2, 2019],
    ] as const;
    const received = entries.map(([id, name, fresh, year]) => ({
      ...packet, packet_id: id, name, fresh_play_unit_count: fresh, year,
    }));
    const root = renderTournamentPackets([], new I18n("ru"), {}, vi.fn(), {
      viewInLibrary: vi.fn(), toggleBlock: vi.fn(),
    });
    root.update(received.slice(0, 2), true);
    root.update(received);
    const ids = (): string[] => Array.from(root.querySelectorAll("article"),
      (node) => node.dataset.packetId!);
    expect(ids()).toEqual(["d", "e", "c", "a", "b"]);
    const sort = root.querySelector<HTMLSelectElement>("select")!;
    for (const [mode, expected] of [
      ["name_asc", ["b", "d", "e", "a", "c"]],
      ["year_asc", ["e", "c", "a", "b", "d"]],
      ["year_desc", ["a", "c", "e", "b", "d"]],
    ] as const) {
      sort.value = mode;
      sort.dispatchEvent(new Event("change"));
      expect(ids()).toEqual(expected);
    }
  });
});
