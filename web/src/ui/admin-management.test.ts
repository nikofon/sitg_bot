import { describe, expect, it, vi } from "vitest";
import type { AdminManagementResource } from "../api/types";
import { I18n } from "../i18n";
import { renderAdminManagement } from "./admin-management";

describe("admin management", () => {
  const tournament = { id: "t1", name: "Alpha", status: "active", moderation_status: "normal",
    actual_starts_at: "2026-01-01", actual_ends_at: null, participants: 5,
    settings: { policies: { rating: true } }, packets: [{ name: "Secret packet" }] };

  it("shows lifecycle actions only for applicable tournament states and collapses settings", () => {
    const action = vi.fn();
    const resource: AdminManagementResource = { kind: "admin_management", state: "ready", section: "tournaments", items: [
      tournament,
      { ...tournament, id: "t2", name: "Beta", moderation_status: "halted" },
      { ...tournament, id: "t3", name: "Gamma", moderation_status: "abolished", status: "completed" },
      { ...tournament, id: "t4", name: "Setup", actual_starts_at: null },
    ] };
    const view = renderAdminManagement(resource, new I18n("en"), {}, vi.fn(), vi.fn(), action);
    const buttons = (id: string) => [...view.querySelectorAll(`[data-resource-id="${id}"] button`)].map(b => b.textContent);
    expect(buttons("t1")).toEqual(["V", "Halt", "Abolish"]);
    expect(buttons("t2")).toEqual(["Resume", "Abolish"]);
    expect(buttons("t3")).toEqual([]);
    expect(buttons("t4")).toEqual(["V", "Abolish"]);
    expect(view.querySelectorAll("details[open]")).toHaveLength(0);
    expect(view.querySelector("a")?.getAttribute("href")).toContain("role=admin");
  });

  it("offers a bounded rating weight slider with an explicit save on tournament cards", () => {
    const saveWeight = vi.fn();
    const resource: AdminManagementResource = { kind: "admin_management", state: "ready", section: "tournaments", items: [
      { ...tournament, settings: { policies: { ruleset_rating_weight: 0.5 } }, settings_version: 3 },
      { ...tournament, id: "t3", moderation_status: "abolished" },
    ] };
    const view = renderAdminManagement(resource, new I18n("en"), {}, vi.fn(), vi.fn(), vi.fn(), saveWeight);
    const cards = [...view.querySelectorAll("article")];
    const slider = cards[0]!.querySelector<HTMLInputElement>("input[type=range]")!;
    expect(cards[1]!.querySelector("input[type=range]")).toBeNull();
    expect(Number(slider.min)).toBe(0.1);
    expect(Number(slider.max)).toBe(1);
    expect(Number(slider.step)).toBe(0.05);
    expect(Number(slider.value)).toBe(0.5);
    const save = [...cards[0]!.querySelectorAll("button")].find(b => b.textContent === "V")!;
    expect(save.className).toContain("weight-save-button");
    expect((save as HTMLButtonElement).disabled).toBe(true);
    slider.value = "0.75";
    slider.dispatchEvent(new Event("input"));
    expect(save.textContent).toBe("V");
    expect((save as HTMLButtonElement).disabled).toBe(false);
    (save as HTMLButtonElement).click();
    expect(saveWeight).toHaveBeenCalledWith(resource.items[0], 0.75, save);
  });

  it("searches nested metadata, sorts numeric counts, and retains filters", () => {
    const save = vi.fn();
    const view = renderAdminManagement({ kind: "admin_management", state: "ready", section: "tournaments", items: [
      tournament, { ...tournament, id: "t2", name: "Beta", participants: 12, packets: [{ name: "Other" }] },
    ] }, new I18n("en"), {}, save, vi.fn(), vi.fn());
    const sort = view.querySelector("select")!;
    sort.value = "participants:desc"; sort.dispatchEvent(new Event("change"));
    expect(view.querySelector("article")?.getAttribute("data-resource-id")).toBe("t2");
    const search = view.querySelector("input")!;
    search.value = "secret packet"; search.dispatchEvent(new Event("input"));
    expect(view.querySelectorAll("article")).toHaveLength(1);
    expect(save).toHaveBeenLastCalledWith({ order: "participants:desc", search: "secret packet" });
  });

  it("includes banned players and offers inspection and clearance for every player", () => {
    const view = renderAdminManagement({ kind: "admin_management", state: "ready", section: "players", items: [
      { id: "p1", public_nickname: "Banned", ban: { reason: "Review" }, suspicion: 42 },
      { id: "p2", public_nickname: "Admin", administrator: true, suspicion: 0 },
    ] }, new I18n("en"), {}, vi.fn(), vi.fn(), vi.fn());
    expect(view.textContent).toContain("Unban");
    expect(view.querySelectorAll("article")).toHaveLength(2);
    expect(view.querySelector('[data-resource-id="p2"]')?.textContent).not.toContain("Unban");
    expect([...view.querySelectorAll("button")].filter(b => b.textContent === "Review suspicion")).toHaveLength(2);
    expect([...view.querySelectorAll("button")].filter(b => b.textContent === "Clear suspicion")).toHaveLength(2);
  });

  it("lists link requests with decisions only while pending and links to the player", () => {
    const view = renderAdminManagement({ kind: "admin_management", state: "ready", section: "link_requests", items: [
      { id: "r1", name: "Ada Lovelace", status: "pending", request_note: "I am this author",
        created_at: "2026-09-01", decided_at: null,
        player: { id: "p1", public_nickname: "Alice" },
        author: { id: "a1", display_name: "Ada Lovelace", questions: 5, themes: 1 } },
      { id: "r2", name: "Grace Hopper", status: "approved", request_note: null,
        created_at: "2026-09-02", decided_at: "2026-09-03",
        player: { id: "p2", public_nickname: "Bob" },
        author: { id: "a2", display_name: "Grace Hopper", questions: 0, themes: 0 } },
    ] }, new I18n("en"), {}, vi.fn(), vi.fn(), vi.fn());
    const buttons = (id: string) => [...view.querySelectorAll(`[data-resource-id="${id}"] button`)].map(b => b.textContent);
    expect(buttons("r1")).toEqual(["Approve", "Reject"]);
    expect(buttons("r2")).toEqual([]);
    expect(view.querySelector('[data-resource-id="r1"] a')?.getAttribute("href")).toBe("/players/p1");
    expect(view.textContent).toContain("I am this author");
    expect(view.textContent).toContain("Link requests");
  });

  it("offers linking and joining for every author", () => {
    const view = renderAdminManagement({ kind: "admin_management", state: "ready", section: "authors", items: [
      { id: "a1", display_name: "Ada Lovelace", questions: 5, themes: 1, packet_count: 1 },
    ] }, new I18n("en"), {}, vi.fn(), vi.fn(), vi.fn());
    expect([...view.querySelectorAll("article button")].map(b => b.textContent)).toEqual(["Link", "Join"]);
    expect(view.querySelector("article button.danger-button")?.textContent).toBe("Join");
  });

  it("lists ongoing games with a count, links, and no actions", () => {
    const view = renderAdminManagement({ kind: "admin_management", state: "ready", section: "ongoing_games", items: [
      { id: "g1", name: "Alpha", status: "active", phase: "question", paused: false,
        tournament: { id: "t1", name: "Alpha" }, host: { id: "p1", public_nickname: "Alice" },
        participant_count: 2, type: "ladder", ruleset: "si",
        created_at: "2026-09-01T10:00:00+00:00", last_activity_at: "2026-09-02T10:00:00+00:00",
        participants: [
          { id: "p1", public_nickname: "Alice", seat: 1, score: 10, ready: true, joined: true, active: true, is_chair: true },
          { id: "p2", public_nickname: "Bob", seat: 2, score: 5, ready: true, joined: true, active: true, is_chair: false },
        ],
        settings: { policies: { rating_enabled: true } } },
      { id: "g2", name: "Beta", status: "lobby", phase: "lobby", paused: false,
        tournament: { id: "t2", name: "Beta" }, host: { id: "p3", public_nickname: "Carol" },
        participant_count: 1, type: "ladder", ruleset: "si",
        created_at: "2026-09-03T10:00:00+00:00", last_activity_at: "2026-09-03T10:00:00+00:00",
        participants: [{ id: "p3", public_nickname: "Carol", seat: 1, score: 0, ready: false, joined: false, active: true, is_chair: false }] },
    ] }, new I18n("en"), {}, vi.fn(), vi.fn(), vi.fn());
    expect(view.textContent).toContain("Ongoing games: 2");
    expect([...view.querySelectorAll("nav button")].map(b => b.textContent)).toContain("Ongoing games");
    expect(view.querySelector("select")?.value).toBe("created_at:desc");
    const card = view.querySelector('[data-resource-id="g2"]')!;
    expect(card.querySelectorAll("button")).toHaveLength(0);
    expect(card.querySelector("a")?.getAttribute("href")).toContain("info=t2");
    const links = [...card.querySelectorAll("a")].map(a => a.getAttribute("href"));
    expect(links).toContain("/players/p3");
    expect(card.textContent).toContain("lobby");
    expect(card.textContent).toContain("ladder");
    const first = view.querySelector('[data-resource-id="g1"]')!;
    const playerLinks = [...first.querySelectorAll("a")]
      .map(a => a.getAttribute("href"))
      .filter((href): href is string => href?.startsWith("/players/") ?? false);
    expect(new Set(playerLinks)).toEqual(new Set(["/players/p1", "/players/p2"]));
    expect(first.textContent).toContain("Alice");
    expect(first.textContent).toContain("Bob");
  });
});
