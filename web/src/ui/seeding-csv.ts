export interface SeedPlayer { id: string; name: string }
export interface SeedSection { title: string; seats: Array<string | null> }

export function validateSeeding(sections: SeedSection[], players: SeedPlayer[]): void {
  const ids = sections.flatMap((section) => section.seats).filter((id) => id !== null);
  const eligible = new Set(players.map((player) => player.id));
  if (ids.length !== eligible.size || new Set(ids).size !== ids.length || ids.some((id) => !eligible.has(id))) {
    throw new Error("Seed every confirmed participant exactly once.");
  }
}

export function exportSeedingCsv(sections: SeedSection[], players: SeedPlayer[]): string {
  const names = new Map(players.map((player) => [player.id, player.name]));
  // Keep names literal when opening the CSV in spreadsheet applications.
  const nameCell = (name: string): string => /^[\s]*[=+@\-\t\r\n]/u.test(name) ? `'${name}` : name;
  const rows = [["Player name", "ID", "", ...sections.flatMap((section) => [section.title, "ID"])]];
  const roster = [...players].sort((a, b) => a.name.localeCompare(b.name) || a.id.localeCompare(b.id));
  for (let row = 0; row < Math.max(players.length, ...sections.map((section) => section.seats.length)); row++) {
    const player = roster[row];
    rows.push([player ? nameCell(player.name) : "", player?.id ?? "", "", ...sections.flatMap((section) => {
      const id = section.seats[row];
      return id ? [nameCell(names.get(id) ?? id), id] : ["", ""];
    })]);
  }
  return "\uFEFF" + rows.map((row) => row.map((cell) => `"${cell.replaceAll('"', '""')}"`).join(",")).join("\r\n") + "\r\n";
}

function parseCsv(text: string): string[][] {
  text = text.replace(/^\uFEFF/u, "");
  const rows: string[][] = [];
  let row: string[] = [], cell = "", quoted = false, closed = false;
  for (let index = 0; index < text.length; index++) {
    const char = text[index]!;
    if (quoted) {
      if (char === '"') {
        if (text[index + 1] === '"') { cell += '"'; index++; }
        else { quoted = false; closed = true; }
      } else cell += char;
    } else if (char === "," || char === "\r" || char === "\n") {
      row.push(cell); cell = ""; closed = false;
      if (char !== ",") {
        rows.push(row); row = [];
        if (char === "\r" && text[index + 1] === "\n") index++;
      }
    } else if (char === '"' && !cell && !closed) quoted = true;
    else {
      if (closed || char === '"') throw new Error("Invalid CSV quoting");
      cell += char;
    }
  }
  if (quoted) throw new Error("Unclosed CSV field");
  if (cell || row.length || closed) rows.push([...row, cell]);
  return rows;
}

export function importSeedingCsv(text: string, layout: SeedSection[], players: SeedPlayer[]): SeedSection[] {
  const [header, ...rows] = parseCsv(text);
  const expected = ["Player name", "ID", "", ...layout.flatMap((section) => [section.title, "ID"])];
  if (!header || expected.some((value, index) => header[index] !== value) ||
      header.slice(expected.length).some((value) => value.trim())) throw new Error("Invalid CSV layout");
  const sections = layout.map((section) => ({ title: section.title, seats: section.seats.map(() => null as string | null) }));
  rows.forEach((row, index) => {
    if (row[2]?.trim() || row.slice(expected.length).some((value) => value.trim())) throw new Error("Unexpected CSV column");
    sections.forEach((section, number) => {
      const name = row[3 + number * 2]?.trim() ?? "";
      const id = row[4 + number * 2]?.trim() ?? "";
      if (index >= section.seats.length) {
        if (name || id) throw new Error("Too many seats");
      } else {
        if (name && !id) throw new Error("Player ID is required");
        section.seats[index] = id || null;
      }
    });
  });
  validateSeeding(sections, players);
  return sections;
}
