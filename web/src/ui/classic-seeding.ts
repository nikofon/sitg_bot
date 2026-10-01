import type { ClassicStage, ClassicTournament } from "../api/types";
import type { I18n } from "../i18n";
import { element, replaceChildren } from "./dom";
import { exportSeedingCsv, importSeedingCsv, validateSeeding, type SeedSection } from "./seeding-csv";

export function renderClassicSeeding(
  classic: ClassicTournament, active: boolean, i18n: I18n,
  save: (stage: ClassicStage, values: Record<string, unknown>) => void,
): HTMLElement {
  const container = element("div", { className: "classic-seeding" });
  const stages = classic.stages.filter((stage) => stage.stage_type !== "none");
  if (!stages.length) return element("p", {}, i18n.t("classic.configure_first"));
  const stageSelect = element("select", { "aria-label": i18n.t("classic.stage") }, ...stages.map((stage) =>
    element("option", { value: stage.kind }, i18n.t(stage.kind === "first" ? "manager_management.first_stage" : "manager_management.playoff_stage"))));
  const content = element("div");
  container.append(element("label", {}, i18n.t("classic.stage"), stageSelect), content);
  const drafts = new Map<string, SeedSection[]>();
  const render = (): void => {
    const stage = stages.find((item) => item.kind === stageSelect.value)!;
    replaceChildren(content);
    if (stage.stage_type === "quiz") { content.append(element("p", {}, i18n.t("classic.no_seeding"))); return; }
    if (stage.kind === "playoff" && stages.some((item) => item.kind === "first")) {
      content.append(element("p", {}, i18n.t("classic.standings_seeding"))); return;
    }
    const scheme = classic.schemes.find((item) => item.id === stage.scheme_key);
    if (stage.stage_type !== "swiss" && !scheme) {
      content.append(element("p", {}, i18n.t("classic.configure_first"))); return;
    }
    const players = classic.players;
    const names = new Map(players.map((player) => [player.id, player.name]));
    const locked = !!stage.started_at || !active || !players.length;
    const human = (id: string | null | undefined): string | null => id && !id.startsWith("chair:") ? id : null;
    const opening = scheme?.opening_games ?? [];
    let sections = drafts.get(stage.kind);
    if (!sections) {
      if (stage.stage_type === "swiss") sections = [{ title: "Seats", seats: Array.from({ length: players.length }, (_, index) => human(stage.seeds.flat()[index])) }];
      else if (stage.stage_type === "playoff") sections = opening.map((sources, index) => ({
        title: `Game ${index + 1}`, seats: sources.map((source) => human(stage.seeds[0]?.[source - 1])),
      }));
      else sections = Array.from({ length: Math.ceil(players.length / scheme!.size) }, (_, index) => ({
        title: `Group ${index + 1}`, seats: Array.from({ length: scheme!.size }, (_, seat) => human(stage.seeds[index]?.[seat])),
      }));
      drafts.set(stage.kind, sections);
    }
    const seats = sections;
    const label = (section: number): string => stage.stage_type === "swiss" ? "" :
      `${i18n.t(stage.stage_type === "groups" ? "classic.group" : "classic.game")} ${section + 1}`;
    const location = (section: number, seat: number): string =>
      `${label(section)} ${i18n.t("classic.seat")} ${seat + 1}`.trim();
    const status = element("p", { role: "status" });
    content.append(element("p", { className: "field-help" }, i18n.t("classic.seeding_help")));
    if (stage.stage_type === "swiss") content.append(element("p", { className: "field-help" }, i18n.t("classic.swiss_seeding_help")));
    if (!players.length) content.append(element("p", {}, i18n.t("classic.finalize_players")));
    if (stage.started_at) content.append(element("p", {}, i18n.t("classic.locked")));
    const strategy = element("select", { disabled: locked, "aria-label": i18n.t("classic.automatic") },
      element("option", { value: "best" }, i18n.t("classic.best")), element("option", { value: "average" }, i18n.t("classic.average")));
    content.append(strategy,
      element("button", { type: "button", className: "secondary-button", disabled: locked,
        onclick: (() => save(stage, { mode: "automatic", strategy: strategy.value })) as EventListener }, i18n.t("classic.automatic")),
      element("button", { type: "button", className: "secondary-button", disabled: locked,
        onclick: (() => save(stage, { mode: "random" })) as EventListener }, i18n.t("classic.random")));
    const groupSelect = element("select", { "aria-label": i18n.t("classic.group") }, ...seats.map((_, index) =>
      element("option", { value: String(index) }, label(index))));
    const seatList = element("div", { className: "seeding-seats" });
    const picker = (section: number, seat: number): void => {
      const dialog = element("dialog", { className: "tournament-dialog seeding-picker", "aria-label": location(section, seat) });
      const close = (): void => { if (typeof dialog.close === "function") dialog.close(); dialog.remove(); };
      const search = element("input", { type: "search", "aria-label": i18n.t("classic.search_player"), placeholder: i18n.t("classic.search_player") });
      const results = element("ul", { className: "resource-list" });
      const assign = (id: string | null): void => {
        if (id) for (const group of seats) {
          const previous = group.seats.indexOf(id);
          if (previous !== -1) group.seats[previous] = seats[section]!.seats[seat]!;
        }
        seats[section]!.seats[seat] = id;
        close(); drawSeats(); status.textContent = "";
      };
      const locations = new Map<string, string>();
      seats.forEach((group, number) => group.seats.forEach((id, index) => { if (id) locations.set(id, location(number, index)); }));
      const drawPlayers = (): void => {
        const matches = [...players].filter((player) => player.name.toLocaleLowerCase().includes(search.value.toLocaleLowerCase()))
          .sort((a, b) => Number(locations.has(a.id)) - Number(locations.has(b.id)) || a.name.localeCompare(b.name) || a.id.localeCompare(b.id));
        replaceChildren(results, ...matches.map((player) => element("li", {}, element("button", {
          type: "button", "data-player-id": player.id, onclick: (() => assign(player.id)) as EventListener,
        }, player.name, locations.has(player.id) ? ` · ${locations.get(player.id)}` : ""))));
      };
      search.addEventListener("input", drawPlayers);
      dialog.append(element("h2", {}, location(section, seat)),
        element("button", { type: "button", onclick: close }, i18n.t("common.close")), search,
        element("button", { type: "button", onclick: (() => assign(null)) as EventListener }, i18n.t("classic.clear_seat")), results);
      dialog.addEventListener("close", () => dialog.remove(), { once: true });
      document.body.append(dialog); drawPlayers();
      if (typeof dialog.showModal === "function") dialog.showModal(); else dialog.setAttribute("open", "");
      search.focus();
    };
    const drawSeats = (): void => {
      replaceChildren(seatList);
      seats.forEach((group, section) => {
        if (stage.stage_type === "groups" && section !== Number(groupSelect.value)) return;
        const list = element("ol", {}, ...group.seats.map((id, seat) => element("li", {}, element("button", {
          type: "button", disabled: locked, "data-seat": `${section}:${seat}`,
          onclick: (() => picker(section, seat)) as EventListener,
        }, `${i18n.t("classic.seat")} ${seat + 1}: ${id ? names.get(id) ?? id : i18n.t("classic.chair")}`))));
        seatList.append(element("fieldset", {}, label(section) ? element("legend", {}, label(section)) : null, list));
      });
    };
    if (stage.stage_type === "groups") content.append(element("label", {}, i18n.t("classic.group"), groupSelect));
    groupSelect.addEventListener("change", drawSeats);
    content.append(seatList); drawSeats();
    const saveManual = (): void => {
      try { validateSeeding(seats, players); } catch { status.textContent = i18n.t("classic.invalid_seeding"); return; }
      let seeds = seats.map((section) => section.seats);
      if (stage.stage_type === "playoff") {
        const flat: Array<string | null> = Array(scheme!.size).fill(null);
        opening.forEach((sources, game) => sources.forEach((source, seat) => { flat[source - 1] = seats[game]!.seats[seat]!; }));
        seeds = [flat];
      }
      save(stage, { mode: "manual", seeds });
    };
    const file = element("input", { type: "file", accept: ".csv,text/csv", disabled: locked, "aria-label": i18n.t("classic.import_csv") });
    file.addEventListener("change", () => { void (async () => {
      try {
        const selected = file.files?.[0];
        if (!selected) return;
        if (selected.size > 5_000_000) throw new Error("CSV too large");
        const imported = importSeedingCsv(await selected.text(), seats, players);
        imported.forEach((group, index) => { seats[index]!.seats = group.seats; });
        drawSeats(); status.textContent = i18n.t("classic.csv_loaded");
      } catch { status.textContent = i18n.t("classic.csv_invalid"); }
      finally { file.value = ""; }
    })(); });
    content.append(element("button", { type: "button", className: "primary-button", disabled: locked,
      onclick: saveManual }, i18n.t("classic.save_seeding")),
    element("button", { type: "button", className: "secondary-button", disabled: !players.length,
      onclick: (() => {
        const url = URL.createObjectURL(new Blob([exportSeedingCsv(seats, players)], { type: "text/csv;charset=utf-8" }));
        const link = element("a", { href: url, download: `seeding-${stage.kind}.csv` });
        document.body.append(link); link.click(); link.remove();
        window.setTimeout(() => URL.revokeObjectURL(url), 1000);
      }) as EventListener }, i18n.t("classic.export_csv")),
    element("label", {}, i18n.t("classic.import_csv"), file),
    element("p", { className: "field-help" }, i18n.t("classic.csv_help")), status);
  };
  stageSelect.addEventListener("change", render); render();
  return container;
}
