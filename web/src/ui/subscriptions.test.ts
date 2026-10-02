import { afterEach, describe, expect, it, vi } from "vitest";
import type { TournamentSubscriptions } from "../api/types";
import { I18n } from "../i18n";
import { renderSubscriptions } from "./subscriptions";

const data: TournamentSubscriptions = {
  cards: [{ id: "card", name: "Yearly", packet_count: 52, discoverable: true, readable: null, playable: true }],
  players: [{ id: "ada", name: "Ada", active: true }, { id: "bob", name: "Bob", active: false }],
  instances: [
    { id: "live", card_id: "card", player_id: "ada", remaining_packets: 51, revoked_at: null, assigned_at: "2026-01-01" },
    { id: "old", card_id: "card", player_id: "ada", remaining_packets: 0, revoked_at: null, assigned_at: "2025-01-01" },
  ],
};
function render(active = true, locale: "en" | "ru" = "en", selectedPlayer = "") {
  const save = vi.fn().mockResolvedValue(undefined);
  const select = vi.fn();
  const root = renderSubscriptions(data, active, new I18n(locale), save, selectedPlayer, select);
  document.body.append(root);
  const button = (text: string) => [...root.querySelectorAll("button")].find((item) => item.textContent === text)!;
  const field = <T extends HTMLElement>(text: string) => root.querySelector<T>(`[aria-label='${text}']`)!;
  return { root, save, select, button, field };
}
afterEach(() => { document.body.replaceChildren(); });

describe("subscription cards", () => {
  it("creates unlimited cards with tri-state rights", () => {
    const { root, save, field } = render();
    field<HTMLInputElement>("Subscription card name").value = "Unlimited play";
    field<HTMLInputElement>("Unlimited").click();
    const selects = root.querySelectorAll<HTMLSelectElement>("form select");
    selects[0]!.value = "no";
    selects[2]!.value = "yes";
    root.querySelector("form")!.dispatchEvent(new Event("submit", { cancelable: true }));
    expect(save).toHaveBeenCalledWith("create", { name: "Unlimited play", packet_count: null,
      discoverable: false, readable: null, playable: true });
  });

  it("searches participants, shows only their cards, assigns and revokes instances", async () => {
    const { root, save, select, button, field } = render();
    const search = field<HTMLInputElement>("Search participants");
    search.value = "ad"; search.dispatchEvent(new Event("input"));
    const picker = field<HTMLSelectElement>("Participant");
    expect([...picker.options].map((option) => option.text)).toEqual(["Select a participant", "Ada"]);
    picker.value = "ada"; picker.dispatchEvent(new Event("change"));
    expect(select).toHaveBeenLastCalledWith("ada");
    expect(root.textContent).toContain("Packets remaining: 51");
    expect(root.textContent).toContain("Exhausted");
    expect([...root.querySelectorAll("button")].filter((item) => item.textContent === "Revoke")).toHaveLength(1);
    button("Revoke").click();
    expect(save).toHaveBeenCalledWith("revoke", { subscription_id: "live" });
    await Promise.resolve();
    button("Assign subscription card").click();
    expect(save).toHaveBeenLastCalledWith("assign", { card_id: "card", player_id: "ada" });
  });

  it("preserves selected participant after refresh and disables unavailable mutations", () => {
    const { field, button, save } = render(false, "en", "ada");
    expect(field<HTMLSelectElement>("Participant").value).toBe("ada");
    expect(button("Revoke").disabled).toBe(true);
    button("Revoke").click();
    expect(save).not.toHaveBeenCalled();
  });

  it("uses Абонемент in Russian", () => {
    const { root } = render(true, "ru", "ada");
    expect(root.textContent).toContain("Создать абонемент");
    expect(root.textContent).toContain("Назначить абонемент");
  });
});
