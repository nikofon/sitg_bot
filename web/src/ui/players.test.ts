import { describe, expect, it, vi } from "vitest";
import type { PlayersResource } from "../api/types";
import { I18n } from "../i18n";
import { renderPlayers } from "./players";

const resource: PlayersResource = {
  kind: "players", state: "ready", rulesets: [{ key: "si", name: "SI" }, { key: "other", name: "Other" }],
  ruleset_key: "si", items: [{ id: "player-id", label: "Ada <b>", rating: 1234.56, games: 12 }],
  total: 45, next_offset: 40,
  supported_orders: ["name_asc", "name_desc", "rating_asc", "rating_desc", "games_asc", "games_desc"],
};

describe("player directory", () => {
  it("opens escaped player links with the selected ruleset and shows ratings and games", () => {
    const navigate = vi.fn();
    const node = renderPlayers(resource, new I18n("en"), new URLSearchParams(), navigate, vi.fn());
    expect(node.querySelector("b")).toBeNull();
    expect(node.textContent).toContain("Ada <b>");
    expect(node.textContent).toContain("1234.6");
    expect(node.textContent).toContain("12");
    expect(node.querySelector<HTMLSelectElement>("#players-ruleset")!.value).toBe("si");
    node.querySelector("a")!.click();
    expect(navigate).toHaveBeenCalledWith("/players/player-id?ruleset=si");
  });

  it("resets pagination on search, sorting, and ruleset changes while preserving other filters", () => {
    const navigate = vi.fn(), save = vi.fn();
    const node = renderPlayers(resource, new I18n("en"),
      new URLSearchParams("search=Ada&order=rating_desc&ruleset=si&offset=20"), navigate, save);
    for (const [selector, value, key] of [
      ["#players-order", "games_desc", "order"], ["#players-ruleset", "other", "ruleset"],
    ]) {
      const control = node.querySelector<HTMLSelectElement>(selector!)!;
      control.value = value!;
      control.dispatchEvent(new Event("change"));
      const url = new URL(navigate.mock.lastCall![0], "https://mini.test");
      expect(url.searchParams.has("offset")).toBe(false);
      expect(url.searchParams.get(key!)).toBe(value);
      expect(url.searchParams.get("search")).toBe("Ada");
    }
    node.querySelector<HTMLInputElement>("#players-search")!.value = " Bob ";
    node.querySelector("form")!.dispatchEvent(new Event("submit", { cancelable: true }));
    expect(save).toHaveBeenLastCalledWith({ search: "Bob", order: "rating_desc", ruleset: "si" });
    expect(navigate).toHaveBeenLastCalledWith("/players?search=Bob&order=rating_desc&ruleset=si");
  });

  it("uses server pagination and handles empty results and unsupported sorts", () => {
    const navigate = vi.fn();
    const query = new URLSearchParams("ruleset=si&offset=20");
    const node = renderPlayers(resource, new I18n("en"), query, navigate, vi.fn());
    const pages = node.querySelectorAll<HTMLButtonElement>("nav button");
    pages[1]!.click();
    expect(navigate).toHaveBeenLastCalledWith("/players?ruleset=si&offset=40");
    pages[0]!.click();
    expect(navigate).toHaveBeenLastCalledWith("/players?ruleset=si&offset=0");
    const empty = renderPlayers({ ...resource, items: [], next_offset: null, supported_orders: ["name_asc"] },
      new I18n("en"), new URLSearchParams(), navigate, vi.fn());
    expect(empty.textContent).toContain("No players found.");
    expect(empty.querySelectorAll("nav button")).toHaveLength(0);
    expect(empty.querySelectorAll("#players-order option")).toHaveLength(1);
  });
});
