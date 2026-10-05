import type { LibraryPacket, LibraryPage, LibraryQuestionStatistics } from "../api/types";
import type { I18n } from "../i18n";
import type { MessageKey } from "../i18n/en";
import { element } from "./dom";

export function renderLibrary(
  packets: LibraryPacket[], i18n: I18n, filters: Record<string, string>,
  saveFilters: (filters: Record<string, string>) => void,
  access: (packet: LibraryPacket, command: "view" | "download", button: HTMLButtonElement) => void,
  profile: (tournament: LibraryPacket["tournaments"][number]) => void,
): HTMLElement {
  const list = element("div", { className: "lobby-packets", "aria-live": "polite" });
  const controls = element("div", { className: "lobby-packet-filters", role: "search" });
  const normalize = (value: string): string => value.trim().toLocaleLowerCase(i18n.locale);
  let sortMode: "default" | "fresh" = "fresh";
  const inRange = (year: number | null, from: string, to: string): boolean =>
    (!filters[from] && !filters[to]) || (year !== null
      && (!filters[from] || year >= Number(filters[from]))
      && (!filters[to] || year <= Number(filters[to])));
  const render = (): void => {
    const visible = packets.filter((packet) =>
      normalize([packet.name, ...packet.tournaments.flatMap((t) => [t.name, t.slug])].join(" "))
        .includes(normalize(filters.search ?? ""))
      && normalize([packet.lead_author, ...packet.authors].join(" ")).includes(normalize(filters.author ?? ""))
      && inRange(packet.year, "year_from", "year_to")
      && inRange(Number(packet.published_at.slice(0, 4)), "publication_from", "publication_to"));
    if (sortMode === "fresh") {
      visible.sort((a, b) =>
        (a.fresh_play_unit_count ?? 0) - (b.fresh_play_unit_count ?? 0)
        || a.name.localeCompare(b.name, i18n.locale));
    }
    list.replaceChildren(...visible.map((packet) => {
      const button = (command: "view" | "download"): HTMLButtonElement => element("button", {
        type: "button", className: command === "view" ? "primary-button" : "secondary-button",
        onclick: ((event: Event) => access(packet, command, event.currentTarget as HTMLButtonElement)) as EventListener,
      }, i18n.t(`library.${command}`));
      const detail = (key: MessageKey, value: string): HTMLElement => element("div", { className: "lobby-packet-detail" },
        element("dt", {}, i18n.t(key)), element("dd", {}, value || "—"));
      return element("article", { className: "resource-card lobby-packet-card", "data-packet-id": packet.version_id },
        element("h2", {}, packet.name),
        element("dl", { className: "lobby-packet-details" },
          detail("lobby.packet_year", packet.year?.toString() ?? ""),
          detail("lobby.packet_publication_year", packet.published_at.slice(0, 4)),
          detail("lobby.packet_lead_author", packet.lead_author),
          detail("lobby.packet_authors", packet.authors.join(", ")),
          detail("library.packet_fresh", `${packet.fresh_play_unit_count ?? 0} / ${packet.total_play_unit_count ?? 0}`)),
        element("ul", {}, ...packet.tournaments.map((tournament) => element("li", {},
          element("a", {
            href: `/tournaments?role=${tournament.role}&info=${encodeURIComponent(tournament.id)}`,
            onclick: ((event: Event) => { event.preventDefault(); profile(tournament); }) as EventListener,
          }, tournament.name)))),
        element("div", { className: "settings-actions" }, button("view"), button("download")));
    }));
    if (!visible.length) list.append(element("p", {}, i18n.t(packets.length ? "lobby.packet_no_matches" : "route.library.empty")));
  };
  const input = (name: string, label: MessageKey, numeric = false): HTMLElement => element("label", {},
    i18n.t(label), element("input", {
      name, type: numeric ? "number" : "search", value: filters[name] ?? "",
      min: numeric ? "1000" : undefined, max: numeric ? "9999" : undefined,
      oninput: ((event: Event) => {
        filters[name] = (event.currentTarget as HTMLInputElement).value;
        saveFilters(filters);
        render();
      }) as EventListener,
    }));
  controls.append(input("search", "library.search"), input("author", "lobby.packet_authors"));
  const sort = element("select", {
    "aria-label": i18n.t("lobby.packet_sort"),
    onchange: (() => {
      sortMode = (sort.value as typeof sortMode) || "fresh";
      render();
    }) as EventListener,
  });
  sort.append(
    element("option", { value: "fresh", selected: true }, i18n.t("lobby.packet_sort_fresh_asc")),
    element("option", { value: "default" }, i18n.t("lobby.packet_sort_default")),
  );
  controls.append(sort);
  for (const [label, from, to] of [
    ["lobby.packet_year", "year_from", "year_to"],
    ["lobby.packet_publication_year", "publication_from", "publication_to"],
  ] as const) {
    controls.append(element("fieldset", { className: "lobby-packet-year-range" },
      element("legend", {}, i18n.t(label)), input(from, "lobby.packet_from", true), input(to, "lobby.packet_to", true)));
  }
  controls.append(element("button", {
    type: "button", className: "secondary-button",
    onclick: (() => {
      for (const key of Object.keys(filters)) delete filters[key];
      for (const field of controls.querySelectorAll("input")) field.value = "";
      saveFilters(filters);
      render();
    }) as EventListener,
  }, i18n.t("lobby.packet_reset")));
  render();
  return element("section", { className: "route-content" }, controls, list);
}

export function renderLibraryReader(
  name: string, pages: LibraryPage[], i18n: I18n,
  statistics?: Record<string, LibraryQuestionStatistics>,
  navigate: (path: string) => void = () => {},
): HTMLElement {
  const content = element("section", { className: "library-page" });
  const navigation = element("nav", { className: "pagination", "aria-label": i18n.t("library.pages") });
  const select = element("select", { className: "library-theme-select", "aria-label": i18n.t("library.jump") }, ...pages.map((page, index) =>
    element("option", { value: String(index) }, `${i18n.t("library.theme")}: ${page.title}`)));
  const authorField = (label: MessageKey, value: string, authorId?: string | null): HTMLElement =>
    element("p", {}, `${i18n.t(label)}: `, authorId
      ? element("a", {
        href: `/authors/${encodeURIComponent(authorId)}`,
        onclick: ((event: Event) => {
          event.preventDefault();
          navigate(`/authors/${encodeURIComponent(authorId)}`);
        }) as EventListener,
      }, value)
      : value);
  const percent = (value: number | null): string => value === null ? "—" : `${value}%`;
  const questionStatistics = (stats: LibraryQuestionStatistics): HTMLElement =>
    element("p", { className: "library-question-statistics" },
      `${i18n.t("library.statistics")}: ${i18n.t("library.stats_views")} ${stats.views}, `,
      `${i18n.t("library.stats_buzzes")} ${stats.buzzes} (`,
      `${i18n.t("library.stats_correct")} ${percent(stats.correct_rate)}, `,
      `${i18n.t("library.stats_incorrect")} ${percent(stats.incorrect_rate)})`);
  const render = (index: number): void => {
    const page = pages[index];
    if (!page) return;
    select.value = String(index);
    content.replaceChildren(element("h2", { className: "library-theme-title", tabindex: "-1" }, `${i18n.t("library.theme")}: ${page.title}`));
    if (page.author) content.append(authorField("library.author", page.author, page.author_id));
    if (page.commentary) content.append(element("p", {}, `${i18n.t("library.commentary")}: ${page.commentary}`));
    for (const question of page.questions) {
      const block = element("article", { className: "library-question" },
        element("h3", {}, question.form
          ? `${question.value}) [${i18n.t("library.form")}: ${question.form}]\n${question.text}`
          : `${question.value}) ${question.text}`),
        element("p", {}, `${i18n.t("library.answer")}: ${question.answer}`));
      for (const [field, label] of [
        ["accepted_answers", "library.additional_answers"], ["rejected_answers", "library.rejected_answers"],
        ["commentary", "library.commentary"],
        ["source", "library.source"],
      ] as const) {
        const value = question[field];
        if (value?.length) block.append(element("p", {}, `${i18n.t(label)}: ${Array.isArray(value) ? value.join(", ") : value}`));
      }
      if (question.author) block.append(authorField("library.author", question.author, question.author_id));
      if (statistics !== undefined && question.id) {
        const stats = statistics[question.id];
        if (stats) block.append(questionStatistics(stats));
        else block.append(element("p", { className: "library-question-statistics" },
          `${i18n.t("library.statistics")}: ${i18n.t("library.stats_empty")}`));
      }
      content.append(block);
    }
    navigation.replaceChildren(...pages.map((_, pageIndex) => element("button", {
      type: "button", className: "pagination-page",
      "aria-current": index === pageIndex ? "page" : undefined,
      onclick: (() => { render(pageIndex); content.querySelector<HTMLElement>("h2")?.focus(); }) as EventListener,
    }, String(pageIndex + 1))));
  };
  select.addEventListener("change", () => { render(Number(select.value)); content.querySelector<HTMLElement>("h2")?.focus(); });
  render(0);
  return element("section", { className: "route-content" },
    element("h2", { className: "library-packet-title" }, name), select, content, navigation);
}
