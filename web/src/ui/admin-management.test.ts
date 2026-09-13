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
    expect(buttons("t1")).toEqual(["Halt", "Abolish"]);
    expect(buttons("t2")).toEqual(["Resume", "Abolish"]);
    expect(buttons("t3")).toEqual([]);
    expect(buttons("t4")).toEqual(["Abolish"]);
    expect(view.querySelectorAll("details[open]")).toHaveLength(0);
    expect(view.querySelector("a")?.getAttribute("href")).toContain("role=admin");
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
});
