import { afterEach, describe, expect, it, vi } from "vitest";
import type { ClassicStage, ClassicTournament } from "../api/types";
import { I18n } from "../i18n";
import { renderClassicSeeding } from "./classic-seeding";
import { exportSeedingCsv } from "./seeding-csv";

function stage(values: Partial<ClassicStage> = {}): ClassicStage {
  return { kind: "first", stage_type: "groups", scheme_key: "groups", seeds: [], started_at: null,
    completed_at: null, place_points: [], score_multiplier: "0", standings: [], rounds: [], ...values };
}
function render(stages = [stage()], active = true) {
  const classic: ClassicTournament = { stages,
    schemes: [{ id: "groups", kind: "groups", size: 2, round_count: 1 },
      { id: "playoff", kind: "playoff", size: 4, round_count: 2, opening_games: [[1, 4], [2, 3]] }],
    players: [{ id: "z", name: "Zoe" }, { id: "a", name: "Ada" }, { id: "b", name: "Bob" }],
  };
  const save = vi.fn();
  const root = renderClassicSeeding(classic, active, new I18n("en"), save);
  document.body.append(root);
  const button = (text: string) => [...root.querySelectorAll("button")].find((item) => item.textContent === text)!;
  const seat = (id: string) => root.querySelector<HTMLButtonElement>(`[data-seat='${id}']`)!;
  return { root, save, button, seat, classic };
}
afterEach(() => { document.body.replaceChildren(); vi.restoreAllMocks(); });

describe("Classic seeding", () => {
  it("creates groups before automatic seeding, searches names, lists free players first, and swaps assigned players", () => {
    const { root, seat, save, button } = render([stage({ seeds: [["a", "z"], ["b", null]] })]);
    const group = root.querySelector<HTMLSelectElement>("select[aria-label=Group]")!;
    expect(group.options).toHaveLength(2);
    expect(root.querySelectorAll("[data-seat]")).toHaveLength(2);
    seat("0:0").click();
    const choices = () => [...document.querySelectorAll<HTMLButtonElement>("[data-player-id]")];
    expect(choices().map((item) => item.textContent)).toEqual(["Ada · Group 1 Seat 1", "Bob · Group 2 Seat 1", "Zoe · Group 1 Seat 2"]);
    const search = document.querySelector<HTMLInputElement>(".seeding-picker input")!;
    search.value = "zo"; search.dispatchEvent(new Event("input"));
    expect(choices()).toHaveLength(1); choices()[0]!.click();
    expect(seat("0:0").textContent).toContain("Zoe");
    expect(seat("0:1").textContent).toContain("Ada");
    group.value = "1"; group.dispatchEvent(new Event("change"));
    seat("1:0").click();
    [...document.querySelectorAll<HTMLButtonElement>(".seeding-picker button")].find((item) => item.textContent === "Clear seat")!.click();
    seat("1:1").click();
    expect(choices()[0]!.textContent).toBe("Bob"); choices()[0]!.click();
    button("Save manual seeding").click();
    expect(save).toHaveBeenCalledWith(expect.objectContaining({ kind: "first" }), { mode: "manual", seeds: [["z", "a"], [null, "b"]] });
  });

  it("renders blank seats from group capacity and refuses incomplete saves", () => {
    const { root, button, save } = render();
    expect(root.querySelectorAll("[data-seat]")).toHaveLength(2);
    button("Save manual seeding").click();
    expect(save).not.toHaveBeenCalled();
    expect(root.querySelector("[role=status]")?.textContent).toContain("exactly one");
    button("Automatic seeding").click();
    expect(save).toHaveBeenLastCalledWith(expect.anything(), { mode: "automatic", strategy: "best" });
    root.querySelector<HTMLSelectElement>("select[aria-label='Automatic seeding']")!.value = "average";
    button("Automatic seeding").click();
    expect(save).toHaveBeenLastCalledWith(expect.anything(), { mode: "automatic", strategy: "average" });
    button("Random seeding").click();
    expect(save).toHaveBeenLastCalledWith(expect.anything(), { mode: "random" });
  });

  it("renders play-off games using scheme source positions and serializes the bracket correctly", () => {
    const { root, seat, button, save } = render([stage({ kind: "playoff", stage_type: "playoff", scheme_key: "playoff", seeds: [["a", "b", null, "z"]] })]);
    expect([...root.querySelectorAll("legend")].map((item) => item.textContent)).toEqual(["Game 1", "Game 2"]);
    expect(seat("0:1").textContent).toContain("Zoe");
    seat("1:1").click();
    document.querySelector<HTMLButtonElement>("[data-player-id=z]")!.click();
    button("Save manual seeding").click();
    expect(save).toHaveBeenCalledWith(expect.anything(), { mode: "manual", seeds: [["a", "b", "z", null]] });
  });

  it("renders Swiss human seats, solo explanation and fixed qualification seeding", () => {
    const { root } = render([stage({ stage_type: "swiss", scheme_key: null }), stage({ kind: "playoff", stage_type: "playoff" })]);
    expect(root.querySelectorAll("[data-seat]")).toHaveLength(3);
    const select = root.querySelector<HTMLSelectElement>("select[aria-label=Stage]")!;
    select.value = "playoff"; select.dispatchEvent(new Event("change"));
    expect(root.textContent).toContain("fixed by first-stage standings");
    expect(root.querySelectorAll("[data-seat]")).toHaveLength(0);
    const solo = render([stage({ stage_type: "quiz" })]);
    expect(solo.root.textContent).toContain("not required");
    expect(solo.root.querySelectorAll("button")).toHaveLength(0);
  });

  it.each([true, false])("locks editing after start or tournament closure", (started) => {
    const { root, button } = render([stage({ started_at: started ? "2026-09-30" : null })], started);
    expect(button("Save manual seeding").disabled).toBe(true);
    expect(button("Automatic seeding").disabled).toBe(true);
    expect(root.querySelector<HTMLInputElement>("input[type=file]")!.disabled).toBe(true);
    expect(root.querySelector<HTMLButtonElement>("[data-seat]")!.disabled).toBe(true);
  });

  it("previews a CSV import and only persists it on save", async () => {
    const { root, button, save, seat, classic } = render();
    const file = root.querySelector<HTMLInputElement>("input[type=file]")!;
    const csv = exportSeedingCsv([{ title: "Group 1", seats: ["b", "z"] }, { title: "Group 2", seats: ["a", null] }], classic.players);
    Object.defineProperty(file, "files", { value: [{ size: csv.length, text: async () => csv }] });
    file.dispatchEvent(new Event("change"));
    await vi.waitFor(() => expect(seat("0:0").textContent).toContain("Bob"));
    expect(save).not.toHaveBeenCalled();
    button("Save manual seeding").click();
    expect(save).toHaveBeenCalledWith(expect.anything(), { mode: "manual", seeds: [["b", "z"], ["a", null]] });
  });
});
