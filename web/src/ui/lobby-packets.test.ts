import { afterEach, describe, expect, it, vi } from "vitest";
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
      packet_id: "delta", name: "Delta", year: 2019, published_at: "2023-01-01T00:00:00Z",
      lead_author: "Diana", authors: ["Diana"], fresh_play_unit_count: 0,
      total_play_unit_count: 4, playable_for_all: true,
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
  afterEach(() => {
    document.body.replaceChildren();
    vi.restoreAllMocks();
  });

  it("sorts by fresh themes with zero-fresh packets last by default", () => {
    const root = renderLobbyPackets(lobby, new I18n("en"), {}, true, vi.fn()).element;
    const ids = (): string[] => Array.from(
      root.querySelectorAll("[data-packet-id]"),
      (node) => (node as HTMLElement).dataset.packetId ?? "",
    );
    expect(ids()).toEqual(["alpha", "bravo", "charlie", "delta"]);

    const sort = root.querySelector<HTMLSelectElement>("select[aria-label='Sort packets']");
    expect(sort).not.toBeNull();
    expect(sort!.value).toBe("fresh_zeroes_last");

    sort!.value = "fresh";
    sort!.dispatchEvent(new Event("change", { bubbles: true }));
    expect(ids()).toEqual(["charlie", "bravo", "alpha", "delta"]);

    sort!.value = "default";
    sort!.dispatchEvent(new Event("change", { bubbles: true }));
    expect(ids()).toEqual(["alpha", "delta", "bravo", "charlie"]);
  });

  it("updates and reorders existing cards while retaining focus and the visible packet's position", () => {
    const mutate = vi.fn();
    const view = renderLobbyPackets(lobby, new I18n("en"), {}, true, mutate);
    document.body.append(view.element);
    const alpha = view.element.querySelector<HTMLElement>('[data-packet-id="alpha"]')!;
    const button = alpha.querySelector<HTMLButtonElement>("button")!;
    button.focus();
    let scrollY = 0;
    vi.spyOn(window, "scrollY", "get").mockImplementation(() => scrollY);
    const scroll = vi.spyOn(window, "scrollTo").mockImplementation((options) => {
      scrollY = (options as ScrollToOptions).top ?? scrollY;
    });
    for (const card of view.element.querySelectorAll<HTMLElement>("article")) {
      vi.spyOn(card, "getBoundingClientRect").mockImplementation(() => {
        const top = Array.from(card.parentElement!.children).indexOf(card) * 200 - scrollY;
        return { top, bottom: top + 200 } as DOMRect;
      });
    }
    const next = structuredClone(lobby);
    const selected = next.packet_suggestions[0]!;
    selected.fresh_play_unit_count = 10;
    next.selected_packets = [selected];
    next.available_actions.push("packet_remove");
    view.update(next);
    expect(view.element.querySelector('[data-packet-id="alpha"]')).toBe(alpha);
    expect(alpha.querySelector("button")).toBe(button);
    expect(button.textContent).toBe("Remove");
    expect(alpha.textContent).toContain("10 / 5");
    expect(document.activeElement).toBe(button);
    expect(scroll).toHaveBeenCalledWith({ top: 400, behavior: "instant" });
    expect(alpha.getBoundingClientRect().top).toBe(0);
    button.click();
    expect(mutate).toHaveBeenCalledWith("packet-remove", { packet_id: "alpha" });
  });

  it("adds and removes packets without resetting the active filter or sort", () => {
    const view = renderLobbyPackets(lobby, new I18n("en"), {}, true, vi.fn());
    const input = view.element.querySelector<HTMLInputElement>('input[name="name"]')!;
    input.value = "a";
    input.dispatchEvent(new Event("input"));
    const sort = view.element.querySelector<HTMLSelectElement>("select")!;
    sort.value = "fresh";
    sort.dispatchEvent(new Event("change"));
    const bravo = view.element.querySelector('[data-packet-id="bravo"]');
    const next = structuredClone(lobby);
    next.packet_suggestions.shift();
    next.packet_suggestions.push({ ...next.packet_suggestions[0]!, packet_id: "echo", name: "Echo" });
    view.update(next);
    expect(view.element.querySelector('[data-packet-id="alpha"]')).toBeNull();
    expect(view.element.querySelector('[data-packet-id="echo"]')).toBeNull();
    expect(view.element.querySelector('[data-packet-id="bravo"]')).toBe(bravo);
    expect(Array.from(view.element.querySelectorAll("article"), (card) => card.dataset.packetId)).toEqual(["charlie", "bravo", "delta"]);
    expect(view.element.querySelector("select")).toBe(sort);
    expect(sort.value).toBe("fresh");
    input.value = "";
    input.dispatchEvent(new Event("input"));
    expect(view.element.querySelector('[data-packet-id="echo"]')).not.toBeNull();
  });
});
