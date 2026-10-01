import { describe, expect, it } from "vitest";
import { exportSeedingCsv, importSeedingCsv } from "./seeding-csv";

const players = [{ id: "a", name: 'Ada, "A"\nSmith' }, { id: "b", name: "Борис" }, { id: "c", name: "=1+1" }];
const sections = [{ title: "Group 1", seats: ["a", null] }, { title: "Group 2", seats: ["b", "c"] }];

describe("seeding CSV", () => {
  it("round trips Unicode, commas, quotes, multiline names, vacancies and formula-like names", () => {
    const csv = exportSeedingCsv(sections, players);
    expect(csv).toContain('"Player name","ID","","Group 1","ID","Group 2","ID"');
    expect(csv).toContain("'=1+1");
    expect(importSeedingCsv(csv, sections, players)).toEqual(sections);
    expect(importSeedingCsv(csv.slice(1), sections, players)).toEqual(sections);
  });

  it("uses IDs despite duplicate or changed names and ignores reference roster edits", () => {
    const edited = exportSeedingCsv(sections, players).replaceAll("Борис", "Renamed");
    expect(importSeedingCsv(edited, sections, players)).toEqual(sections);
    const moved = [{ title: "Group 1", seats: ["b", "a"] }, { title: "Group 2", seats: [null, "c"] }];
    expect(importSeedingCsv(exportSeedingCsv(moved, players), sections, players)).toEqual(moved);
  });

  it.each([
    [{ title: "Group 1", seats: ["a", "a"] }, { title: "Group 2", seats: ["b", "c"] }],
    [{ title: "Group 1", seats: ["outsider", null] }, { title: "Group 2", seats: ["b", "c"] }],
    [{ title: "Group 1", seats: [null, null] }, { title: "Group 2", seats: ["b", "c"] }],
    [{ title: "Group 1", seats: ["a", null, "b"] }, { title: "Group 2", seats: [null, "c"] }],
  ])("rejects duplicate, unknown, missing or extra assignments", (...invalid) => {
    expect(() => importSeedingCsv(exportSeedingCsv(invalid, players), sections, players)).toThrow();
  });

  it("rejects mismatched headers, missing IDs and broken quotes", () => {
    const csv = exportSeedingCsv(sections, players);
    for (const invalid of [csv.replace("Group 1", "Game 1"), csv.replaceAll('"b"', '""'), csv + '"unfinished']) {
      expect(() => importSeedingCsv(invalid, sections, players)).toThrow();
    }
  });
});
