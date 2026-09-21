import type {
  TournamentProfileGameParticipant,
  TournamentProfileMatch,
  TournamentProfileRegistration,
  TournamentProfileResource,
} from "../api/types";
import type { I18n } from "../i18n";
import type { MessageKey } from "../i18n/en";
import { element, replaceChildren } from "./dom";

export interface TournamentProfileHandlers {
  openPlayer: (playerId: string) => void;
  openGame: (playerId: string, gameId: string) => void;
}

type SectionId = "general" | "registrations" | "participants" | "games" | "leaders";

const STATUS_BADGES: Record<string, string> = {
  approved: "badge-approved",
  active: "badge-approved",
  rejected: "badge-rejected",
  registered: "badge-warning",
};

const STATUS_KEYS: Record<string, MessageKey> = {
  invited: "tournament_profile.status_invited",
  registered: "tournament_profile.status_registered",
  approved: "tournament_profile.status_approved",
  active: "tournament_profile.status_active",
  rejected: "tournament_profile.status_rejected",
};

function statusBadge(status: string, i18n: I18n): HTMLElement {
  return element(
    "span",
    { className: `badge ${STATUS_BADGES[status] ?? ""}`.trim() },
    i18n.t(STATUS_KEYS[status] ?? "tournament_profile.status_other"),
  );
}

function detailRow(label: string, value: string | null | undefined): HTMLElement {
  if (value === null || value === undefined || value === "") return element("li", {});
  return element("li", {}, `${label}: ${value}`);
}

function generalPanel(
  resource: TournamentProfileResource,
  i18n: I18n,
  formatDate: (value?: string | null) => string,
): HTMLElement {
  const general = resource.general;
  const registration = general.registration;
  const managers = general.managers.map((manager) => manager.name).join(", ");
  const authors = general.authors.join(", ");
  const registrationWindow = [registration.starts_at, registration.ends_at]
    .filter((part) => part !== null && part !== undefined)
    .map((part) => formatDate(part))
    .join(" – ");
  return element(
    "section",
    { className: "tournament-profile-section", "data-section": "general" },
    element("h3", {}, i18n.t("tournament_profile.section_general")),
    general.description
      ? element("p", { className: "tournament-description" }, general.description)
      : null,
    element(
      "ul",
      { className: "detail-list" },
      detailRow(i18n.t("tournament_profile.status"), general.status),
      detailRow(i18n.t("tournament_profile.type"), general.type?.name ?? resource.type_key),
      detailRow(i18n.t("tournament_profile.ruleset"), general.ruleset?.name ?? null),
      detailRow(i18n.t("tournament_profile.managers"), managers),
      detailRow(i18n.t("tournament_profile.authors"), authors),
      detailRow(i18n.t("tournament_profile.language"), general.language),
      detailRow(i18n.t("tournament_profile.payment"), general.payment_type),
      detailRow(i18n.t("tournament_profile.visibility"), general.visibility),
      detailRow(i18n.t("tournament_profile.starts_at"), formatDate(general.starts_at)),
      detailRow(i18n.t("tournament_profile.ends_at"), formatDate(general.planned_ends_at ?? general.actual_ends_at)),
      detailRow(
        i18n.t("tournament_profile.registration_window"),
        [
          registrationWindow,
          registration.open
            ? i18n.t("tournament_profile.registration_open")
            : i18n.t("tournament_profile.registration_closed"),
        ]
          .filter(Boolean)
          .join(" · "),
      ),
      detailRow(
        i18n.t("tournament_profile.counts"),
        `${general.registration_count} / ${general.participant_count}`,
      ),
    ),
  );
}

function registrationsPanel(
  registrations: TournamentProfileRegistration[],
  i18n: I18n,
  formatDate: (value?: string | null) => string,
  openPlayer: (playerId: string) => void,
): HTMLElement {
  return element(
    "section",
    { className: "tournament-profile-section", "data-section": "registrations" },
    element("h3", {}, i18n.t("tournament_profile.section_registrations")),
    registrations.length
      ? element(
          "ul",
          { className: "resource-list" },
          ...registrations.map((registration) =>
            element(
              "li",
              { className: "tournament-registration" },
              playerAnchor(registration, openPlayer),
              registration.registered_at
                ? element("span", { className: "tournament-registration-date" }, formatDate(registration.registered_at))
                : null,
              statusBadge(registration.status, i18n),
            ),
          ),
        )
      : element("p", { className: "field-help" }, i18n.t("tournament_profile.no_registrations")),
  );
}

function participantRow(
  participant: { player_id: string; nickname: string; score?: number | string; place?: number | string | null },
  openPlayer: (playerId: string) => void,
): HTMLElement {
  return element(
    "li",
    { className: "game-participant" },
    element(
      "span",
      { className: "game-participant-place" },
      participant.place !== null && participant.place !== undefined
        ? formatPlace(participant.place)
        : "",
    ),
    playerAnchor(participant, openPlayer),
    element(
      "span",
      { className: "game-participant-score" },
      participant.score !== undefined ? String(participant.score) : "",
    ),
  );
}

function formatPlace(place: number | string): string {
  return Number.isInteger(Number(place)) ? String(place) : String(place);
}

function gameCard(
  match: {
    game_id: string | null;
    played_at?: string | null;
    participants: TournamentProfileGameParticipant[];
    players: Array<{ player_id: string; nickname: string }>;
    manual_results: TournamentProfileMatch["manual_results"];
    number: number;
  },
  i18n: I18n,
  formatDate: (value?: string | null) => string,
  handlers: TournamentProfileHandlers,
  title: string,
): HTMLElement {
  const participants = match.participants.length
    ? element(
        "ul",
        { className: "game-participants" },
        ...match.participants.map((participant) =>
          participantRow(participant, handlers.openPlayer),
        ),
      )
    : match.manual_results.length
      ? element(
          "ul",
          { className: "game-participants" },
          ...match.manual_results.map((result) =>
            participantRow(result, handlers.openPlayer),
          ),
        )
      : match.players.length
        ? element(
            "ul",
            { className: "game-participants" },
            ...match.players.map((player) => participantRow(player, handlers.openPlayer)),
          )
        : element("p", { className: "field-help" }, i18n.t("tournament_profile.match_pending"));
  const examined = match.participants.find(
    (participant) => participant.place !== null && participant.place !== undefined,
  );
  return element(
    "article",
    { className: "resource-card tournament-game-card", "data-game-id": match.game_id ?? "" },
    element("h4", {}, title),
    match.played_at
      ? element("p", { className: "profile-game-date" }, formatDate(match.played_at))
      : null,
    participants,
    examined && match.game_id
      ? element(
          "div",
          { className: "settings-actions" },
          element(
            "button",
            {
              type: "button",
              className: "primary-button",
              onclick: (() =>
                handlers.openGame(examined.player_id, match.game_id as string)) as EventListener,
            },
            i18n.t("tournament_profile.open_game"),
          ),
        )
      : null,
  );
}

function stageTitle(stage: { kind: string }, i18n: I18n): string {
  return i18n.t(
    stage.kind === "playoff"
      ? "tournament_profile.stage_playoff"
      : "tournament_profile.stage_first",
  );
}

function participantsPanel(
  participants: Array<{ player_id: string; nickname: string }>,
  i18n: I18n,
  openPlayer: (playerId: string) => void,
): HTMLElement {
  return element(
    "section",
    { className: "tournament-profile-section", "data-section": "participants" },
    element("h3", {}, i18n.t("tournament_profile.section_participants")),
    participants?.length
      ? element(
          "ul",
          { className: "resource-list" },
          ...participants.map((participant) =>
            element("li", { className: "tournament-registration" }, playerAnchor(participant, openPlayer)),
          ),
        )
      : element("p", { className: "field-help" }, i18n.t("tournament_profile.no_participants")),
  );
}

function gamesPanel(
  resource: TournamentProfileResource,
  i18n: I18n,
  formatDate: (value?: string | null) => string,
  handlers: TournamentProfileHandlers,
  state: { stageIndex: number; groupIndex: number; roundIndex: number },
  rerender: () => void,
): HTMLElement {
  const panel = element(
    "section",
    { className: "tournament-profile-section", "data-section": "games" },
    element("h3", {}, i18n.t("tournament_profile.section_games")),
  );
  if (resource.games.kind === "ladder") {
    const items = resource.games.items;
    panel.append(
      items.length
        ? element(
            "ul",
            { className: "resource-list" },
            ...items.map((game) =>
              element(
                "li",
                {},
                gameCard(
                  {
                    game_id: game.game_id,
                    played_at: game.played_at,
                    participants: game.participants,
                    players: [],
                    manual_results: [],
                    number: 0,
                  },
                  i18n,
                  formatDate,
                  handlers,
                  formatDate(game.played_at),
                ),
              ),
            ),
          )
        : element("p", { className: "field-help" }, i18n.t("tournament_profile.no_games")),
    );
    return panel;
  }
  const stages = resource.games.stages.filter((stage) => stage.rounds.length);
  if (!stages.length) {
    panel.append(element("p", { className: "field-help" }, i18n.t("tournament_profile.no_games")));
    return panel;
  }
  if (state.stageIndex >= stages.length) state.stageIndex = 0;
  const controls = element("div", { className: "tournament-games-controls" });
  if (stages.length > 1) {
    const select = element(
      "select",
      {
        "aria-label": i18n.t("tournament_profile.stage"),
        onchange: ((event: Event) => {
          state.stageIndex = Number((event.currentTarget as HTMLSelectElement).value);
          state.groupIndex = 0;
          state.roundIndex = 0;
          rerender();
        }) as EventListener,
      },
      ...stages.map((stage, index) =>
        element("option", { value: String(index) }, stageTitle(stage, i18n)),
      ),
    );
    select.value = String(state.stageIndex);
    controls.append(element("label", {}, `${i18n.t("tournament_profile.stage")}: `, select));
  }
  const stage = stages[state.stageIndex]!;
  const grouped = stage.stage_type === "groups" && stage.kind === "first";
  const list = element("ul", { className: "resource-list" });
  if (grouped) {
    if (state.groupIndex >= stage.groups.length) state.groupIndex = 0;
    const groupSelect = element(
      "select",
      {
        "aria-label": i18n.t("tournament_profile.group"),
        onchange: ((event: Event) => {
          state.groupIndex = Number((event.currentTarget as HTMLSelectElement).value);
          rerender();
        }) as EventListener,
      },
      ...stage.groups.map((group, index) =>
        element("option", { value: String(index) }, String(group)),
      ),
    );
    groupSelect.value = String(state.groupIndex);
    controls.append(element("label", {}, `${i18n.t("tournament_profile.group")}: `, groupSelect));
  }
  if (stage.kind === "playoff" || grouped) {
    if (state.roundIndex >= stage.rounds.length) state.roundIndex = 0;
    controls.append(
      element(
        "nav",
        { className: "pagination", "aria-label": i18n.t("tournament_profile.round") },
        element(
          "button",
          {
            type: "button",
            disabled: state.roundIndex === 0,
            onclick: (() => {
              state.roundIndex -= 1;
              rerender();
            }) as EventListener,
          },
          i18n.t("common.previous"),
        ),
        element(
          "span",
          { className: "pagination-status" },
          `${i18n.t("tournament_profile.round")} ${state.roundIndex + 1} / ${stage.rounds.length}`,
        ),
        element(
          "button",
          {
            type: "button",
            disabled: state.roundIndex >= stage.rounds.length - 1,
            onclick: (() => {
              state.roundIndex += 1;
              rerender();
            }) as EventListener,
          },
          i18n.t("common.next"),
        ),
      ),
    );
    const round = stage.rounds[state.roundIndex]!;
    const matches = grouped
      ? round.matches.filter((match) => match.group === stage.groups[state.groupIndex])
      : round.matches;
    list.append(
      ...matches.map((match) =>
        element(
          "li",
          {},
          gameCard(
            match, i18n, formatDate, handlers,
            `${i18n.t("tournament_profile.game")} ${match.number}`,
          ),
        ),
      ),
    );
  } else {
    list.append(
      ...stage.rounds.flatMap((round) =>
        round.matches.map((match) =>
          element(
            "li",
            {},
            gameCard(
              match, i18n, formatDate, handlers,
              `${i18n.t("tournament_profile.round")} ${round.number} · ${i18n.t("tournament_profile.game")} ${match.number}`,
            ),
          ),
        ),
      ),
    );
  }
  panel.append(
    controls,
    list.childElementCount
      ? list
      : element("p", { className: "field-help" }, i18n.t("tournament_profile.no_games")),
  );
  return panel;
}

function leadersPanel(
  resource: TournamentProfileResource,
  i18n: I18n,
  handlers: TournamentProfileHandlers,
  state: { stageIndex: number },
  rerender: () => void,
): HTMLElement {
  const panel = element(
    "section",
    { className: "tournament-profile-section", "data-section": "leaders" },
    element("h3", {}, i18n.t("tournament_profile.section_leaders")),
  );
  if (resource.leaders.kind === "ladder") {
    const items = resource.leaders.items;
    panel.append(
      items.length
        ? element(
            "ol",
            { className: "resource-list tournament-leaders" },
            ...items.map((leader) =>
              element(
                "li",
                { className: "tournament-leader" },
                playerAnchor(leader, handlers.openPlayer),
                element("span", { className: "tournament-leader-rating" }, String(leader.rating)),
              ),
            ),
          )
        : element("p", { className: "field-help" }, i18n.t("tournament_profile.no_leaders")),
    );
    return panel;
  }
  const stages = resource.leaders.stages;
  if (!stages.length) {
    panel.append(element("p", { className: "field-help" }, i18n.t("tournament_profile.no_leaders")));
    return panel;
  }
  if (state.stageIndex >= stages.length) state.stageIndex = 0;
  if (stages.length > 1) {
    const select = element(
      "select",
      {
        "aria-label": i18n.t("tournament_profile.stage"),
        onchange: ((event: Event) => {
          state.stageIndex = Number((event.currentTarget as HTMLSelectElement).value);
          rerender();
        }) as EventListener,
      },
      ...stages.map((stage, index) =>
        element("option", { value: String(index) }, stageTitle(stage, i18n)),
      ),
    );
    select.value = String(state.stageIndex);
    panel.append(element("label", {}, `${i18n.t("tournament_profile.stage")}: `, select));
  }
  const stage = stages[state.stageIndex]!;
  if (stage.standings?.length) {
    panel.append(
      element(
        "table",
        { className: "leader-table" },
        element(
          "thead",
          {},
          element(
            "tr",
            {},
            element("th", { scope: "col" }, i18n.t("tournament_profile.place")),
            element("th", { scope: "col" }, i18n.t("tournament_profile.participant")),
            element("th", { scope: "col" }, i18n.t("tournament_profile.points")),
            element("th", { scope: "col" }, i18n.t("tournament_profile.score")),
          ),
        ),
        element(
          "tbody",
          {},
          ...stage.standings.map((row, index) =>
            element(
              "tr",
              {},
              element("th", { scope: "row" }, String(index + 1)),
              element("td", {}, playerAnchor(row, handlers.openPlayer)),
              element("td", {}, row.points),
              element("td", {}, row.score),
            ),
          ),
        ),
      ),
    );
  } else if (stage.places?.length) {
    panel.append(
      element(
        "table",
        { className: "leader-table" },
        element(
          "thead",
          {},
          element(
            "tr",
            {},
            element("th", { scope: "col" }, i18n.t("tournament_profile.place")),
            element("th", { scope: "col" }, i18n.t("tournament_profile.participant")),
          ),
        ),
        element(
          "tbody",
          {},
          ...stage.places.map((row) =>
            element(
              "tr",
              {},
              element("th", { scope: "row" }, row.place),
              element("td", {}, playerAnchor(row, handlers.openPlayer)),
            ),
          ),
        ),
      ),
    );
  } else {
    panel.append(
      element("p", { className: "field-help" }, i18n.t("tournament_profile.no_leaders")),
    );
  }
  return panel;
}

const SECTION_KEYS: Record<SectionId, MessageKey> = {
  general: "tournament_profile.section_general",
  registrations: "tournament_profile.section_registrations",
  participants: "tournament_profile.section_participants",
  games: "tournament_profile.section_games",
  leaders: "tournament_profile.section_leaders",
};

export function renderTournamentProfile(
  resource: TournamentProfileResource,
  i18n: I18n,
  formatDate: (value?: string | null) => string,
  handlers: TournamentProfileHandlers,
  initialSection?: string,
): HTMLElement {
  const sections: SectionId[] =
    resource.type_key === "classic"
      ? ["general", "registrations", "participants", "games", "leaders"]
      : ["general", "registrations", "games", "leaders"];
  const initial = sections.find((section) => section === initialSection);
  const state = { active: initial ?? ("general" as SectionId) };
  const gamesState = { stageIndex: 0, groupIndex: 0, roundIndex: 0 };
  const leaderState = { stageIndex: 0 };
  const navigation = element(
    "nav",
    { className: "tournament-section-nav", "aria-label": i18n.t("route.tournament_profile.title") },
  );
  const buttons = new Map<SectionId, HTMLButtonElement>();
  for (const section of sections) {
    const button = element(
      "button",
      {
        type: "button",
        className: section === state.active ? "active" : "",
        onclick: (() => {
          state.active = section;
          for (const [name, node] of buttons) {
            node.classList.toggle("active", name === section);
          }
          render();
        }) as EventListener,
      },
      i18n.t(SECTION_KEYS[section]),
    );
    buttons.set(section, button);
    navigation.append(button);
  }
  const container = element("div", { className: "tournament-profile-sections" });
  const render = (): void => {
    const panel =
      state.active === "general"
        ? generalPanel(resource, i18n, formatDate)
        : state.active === "registrations"
          ? registrationsPanel(resource.registrations, i18n, formatDate, handlers.openPlayer)
          : state.active === "participants"
            ? participantsPanel(resource.participants ?? [], i18n, handlers.openPlayer)
            : state.active === "games"
              ? gamesPanel(resource, i18n, formatDate, handlers, gamesState, render)
              : leadersPanel(resource, i18n, handlers, leaderState, render);
    replaceChildren(container, panel);
  };
  render();
  return element(
    "section",
    { className: "route-content tournament-profile" },
    element(
      "header",
      { className: "tournament-heading" },
      element("h2", { className: "tournament-name" }, resource.general.name),
      element(
        "div",
        { className: "tournament-badges" },
        element(
          "span",
          { className: "badge badge-accent" },
          resource.general.type?.name ?? resource.type_key,
        ),
      ),
    ),
    navigation,
    container,
  );
}


function playerAnchor(
  participant: { player_id: string; nickname: string },
  openPlayer: (playerId: string) => void,
): HTMLElement {
  return element(
    "a",
    {
      href: `/players/${encodeURIComponent(participant.player_id)}`,
      onclick: ((event: Event) => {
        event.preventDefault();
        openPlayer(participant.player_id);
      }) as EventListener,
    },
    participant.nickname,
  );
}

