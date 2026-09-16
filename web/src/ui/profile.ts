import type {
  PlayerGameResource,
  PlayerGameTheme,
  PlayerPlacementKind,
  PlayerProfileGame,
  PlayerProfileGameParticipant,
  PlayerProfileResource,
} from "../api/types";
import type { I18n } from "../i18n";
import type { MessageKey } from "../i18n/en";
import { element } from "./dom";

export interface ProfileHandlers {
  selectRuleset: (key: string) => void;
  openPlayer: (playerId: string) => void;
  openGame: (gameId: string) => void;
}

export interface GameHandlers {
  back: () => void;
  openPlayer: (playerId: string) => void;
}

const PLACEMENT_LABELS: Record<PlayerPlacementKind, MessageKey> = {
  place_1: "profile.place_1",
  place_2: "profile.place_2",
  place_3: "profile.place_3",
  place_4: "profile.place_4",
  place_1_5: "profile.place_1_5",
  place_2_5: "profile.place_2_5",
  place_3_5: "profile.place_3_5",
  below_4: "profile.below_4",
};

function displayName(
  participant: Pick<PlayerProfileGameParticipant, "nickname" | "player_id">,
  i18n: I18n,
): string {
  return participant.nickname ?? `${i18n.t("profile.player")} ${participant.player_id.slice(0, 8)}`;
}

function playerAnchor(
  participant: Pick<PlayerProfileGameParticipant, "nickname" | "player_id">,
  i18n: I18n,
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
    displayName(participant, i18n),
  );
}

function formatPlace(place: number | null): string {
  if (place === null) return "—";
  return Number.isInteger(place) ? String(place) : String(place);
}

function ratingGraph(
  history: Array<{ rating: number }>,
  i18n: I18n,
): SVGSVGElement | null {
  if (history.length < 2) return null;
  const width = 300;
  const height = 90;
  const padding = 6;
  const values = history.map((point) => point.rating);
  const minimum = Math.min(...values);
  const span = Math.max(...values) - minimum || 1;
  const svg = document.createElementNS("http://www.w3.org/2000/svg", "svg");
  svg.setAttribute("viewBox", `0 0 ${width} ${height}`);
  svg.setAttribute("class", "rating-graph");
  svg.setAttribute("role", "img");
  svg.setAttribute(
    "aria-label",
    `${i18n.t("profile.rating_history")}: ${values.map((value) => Math.round(value)).join(" → ")}`,
  );
  const points = history
    .map((point, index) => {
      const x =
        padding + (index * (width - 2 * padding)) / Math.max(1, history.length - 1);
      const y = height - padding - ((point.rating - minimum) / span) * (height - 2 * padding);
      return `${x.toFixed(1)},${y.toFixed(1)}`;
    })
    .join(" ");
  const polyline = document.createElementNS("http://www.w3.org/2000/svg", "polyline");
  polyline.setAttribute("points", points);
  polyline.setAttribute("class", "rating-graph-line");
  svg.append(polyline);
  return svg;
}

function participantRows(
  game: Pick<PlayerProfileGame, "participants">,
  i18n: I18n,
  openPlayer: (playerId: string) => void,
): HTMLElement {
  return element(
    "ul",
    { className: "game-participants" },
    ...game.participants.map((participant) =>
      element(
        "li",
        { className: "game-participant" },
        element("span", { className: "game-participant-place" }, formatPlace(participant.place)),
        playerAnchor(participant, i18n, openPlayer),
        element("span", { className: "game-participant-score" }, String(participant.score)),
      ),
    ),
  );
}

export function renderPlayerProfile(
  resource: PlayerProfileResource,
  i18n: I18n,
  formatDate: (value?: string | null) => string,
  handlers: ProfileHandlers,
): HTMLElement {
  const player = resource.player;
  const heading = element(
    "h2",
    { className: "profile-name" },
    player.nickname ?? `${i18n.t("profile.player")} ${player.id.slice(0, 8)}`,
  );
  const identity = element("p", { className: "profile-identity" });
  if (player.real_name) identity.append(element("span", {}, `${i18n.t("profile.real_name")}: ${player.real_name}`));
  if (player.telegram_username) {
    identity.append(
      element("span", {}, `${i18n.t("profile.telegram")}: @${player.telegram_username}`),
    );
  }
  const controls = element("div", { className: "profile-controls" });
  if (resource.rulesets.length) {
    const select = element(
      "select",
      {
        className: "profile-ruleset-select",
        "aria-label": i18n.t("profile.ruleset"),
        onchange: ((event: Event) => {
          const target = event.currentTarget as HTMLSelectElement;
          handlers.selectRuleset(target.value);
        }) as EventListener,
      },
      ...resource.rulesets.map((ruleset) =>
        element("option", { value: ruleset.key }, ruleset.name),
      ),
    );
    if (resource.ruleset_key) select.value = resource.ruleset_key;
    controls.append(element("label", {}, `${i18n.t("profile.ruleset")}: `, select));
  }

  const rating = element(
    "section",
    { className: "profile-section profile-rating" },
    element("h3", {}, i18n.t("profile.rating")),
    element("p", { className: "profile-rating-value" }, String(Math.round(resource.rating.value))),
  );
  const graph = ratingGraph(resource.rating.history, i18n);
  if (graph) rating.append(graph);

  const stats = element("section", { className: "profile-section profile-stats" }, element("h3", {}, i18n.t("profile.win_rate")));
  const summary = element("dl", { className: "profile-stats-summary" });
  for (const [label, value] of [
    [i18n.t("profile.games"), String(resource.stats.games)],
    [i18n.t("profile.wins"), String(resource.stats.wins)],
    [i18n.t("profile.win_rate"), `${resource.stats.win_rate}%`],
  ] as const) {
    summary.append(
      element("div", { className: "profile-stat" }, element("dt", {}, label), element("dd", {}, value)),
    );
  }
  stats.append(summary);
  if (resource.stats.games) {
    stats.append(
      element(
        "ul",
        { className: "profile-distribution" },
        ...resource.stats.placements.map((placement) =>
          element(
            "li",
            { className: "profile-distribution-row" },
            element("span", { className: "profile-distribution-label" }, i18n.t(PLACEMENT_LABELS[placement.kind])),
            element(
              "span",
              {
                className: "profile-distribution-bar",
                "aria-hidden": "true",
              },
              element("span", {
                className: "profile-distribution-fill",
                style: `width:${Math.min(100, placement.percent)}%`,
              }),
            ),
            element(
              "span",
              { className: "profile-distribution-value" },
              `${placement.percent}% · ${placement.count}`,
            ),
          ),
        ),
      ),
    );
  }

  const content = element(
    "section",
    { className: "route-content profile" },
    heading,
    identity.childElementCount ? identity : null,
    controls,
    rating,
    stats,
  );
  if (resource.si_question_stats?.length) {
    const table = element(
      "table",
      { className: "profile-question-stats" },
      element(
        "thead",
        {},
        element(
          "tr",
          {},
          element("th", { scope: "col" }, i18n.t("profile.question_value")),
          element("th", { scope: "col" }, i18n.t("profile.correct")),
          element("th", { scope: "col" }, i18n.t("profile.incorrect")),
        ),
      ),
      element(
        "tbody",
        {},
        ...resource.si_question_stats.map((stat) =>
          element(
            "tr",
            {},
            element("th", { scope: "row" }, String(stat.value)),
            element("td", { className: "answer-correct-text" }, String(stat.correct)),
            element("td", { className: "answer-incorrect-text" }, String(stat.incorrect)),
          ),
        ),
      ),
    );
    content.append(
      element("section", { className: "profile-section" }, element("h3", {}, i18n.t("profile.question_stats")), table),
    );
  }

  const gamesHeading = element("h3", {}, i18n.t("profile.game_history"));
  const gamesList = element(
    "ul",
    { className: "resource-list profile-games" },
    ...resource.games.map((game) =>
      element(
        "li",
        {},
        element(
          "article",
          { className: "resource-card profile-game-card", "data-game-id": game.game_id },
          element(
            "h3",
            {},
            game.tournament_name ?? i18n.t("profile.private_tournament"),
          ),
          game.stage
            ? element("p", { className: "profile-game-stage" }, `${i18n.t("profile.stage")}: ${game.stage}`)
            : null,
          element("p", { className: "profile-game-date" }, formatDate(game.played_at)),
          participantRows(game, i18n, handlers.openPlayer),
          element(
            "div",
            { className: "settings-actions" },
            element(
              "button",
              {
                type: "button",
                className: "primary-button",
                onclick: (() => handlers.openGame(game.game_id)) as EventListener,
              },
              i18n.t("profile.details"),
            ),
          ),
        ),
      ),
    ),
  );
  content.append(
    element(
      "section",
      { className: "profile-section" },
      gamesHeading,
      resource.games.length
        ? gamesList
        : element("p", { className: "field-help" }, i18n.t("profile.no_games")),
    ),
  );
  return content;
}

function themeGrid(
  theme: PlayerGameTheme,
  participants: PlayerProfileGameParticipant[],
  i18n: I18n,
  openPlayer: (playerId: string) => void,
): HTMLElement {
  const head = element("tr", {}, element("th", { scope: "col" }, i18n.t("profile.participants")));
  for (const question of theme.questions) {
    head.append(element("th", { scope: "col" }, String(question.value)));
  }
  const body = element("tbody");
  for (const participant of participants) {
    const row = element("tr", {}, element("th", { scope: "row" }, playerAnchor(participant, i18n, openPlayer)));
    for (const question of theme.questions) {
      const status = question.answers[participant.participant_id];
      row.append(
        element(
          "td",
          { className: "answer-cell" },
          element("span", {
            className:
              status === "correct"
                ? "answer-mark answer-correct"
                : status === "incorrect"
                  ? "answer-mark answer-incorrect"
                  : "answer-mark answer-none",
            title:
              status === "correct"
                ? i18n.t("profile.correct")
                : status === "incorrect"
                  ? i18n.t("profile.incorrect")
                  : i18n.t("profile.no_answer"),
            "aria-label":
              status === "correct"
                ? i18n.t("profile.correct")
                : status === "incorrect"
                  ? i18n.t("profile.incorrect")
                  : i18n.t("profile.no_answer"),
          }),
        ),
      );
    }
    body.append(row);
  }
  return element(
    "table",
    { className: "player-game-grid" },
    element("thead", {}, head),
    body,
  );
}

export function renderPlayerGame(
  resource: PlayerGameResource,
  i18n: I18n,
  formatDate: (value?: string | null) => string,
  handlers: GameHandlers,
): HTMLElement {
  const header = element(
    "section",
    { className: "profile-section" },
    element(
      "div",
      { className: "settings-actions" },
      element(
        "button",
        {
          type: "button",
          className: "secondary-button",
          onclick: (() => handlers.back()) as EventListener,
        },
        i18n.t("common.back"),
      ),
    ),
    element("h2", { className: "profile-name" }, resource.tournament_name ?? i18n.t("profile.private_tournament")),
    resource.stage
      ? element("p", { className: "profile-game-stage" }, `${i18n.t("profile.stage")}: ${resource.stage}`)
      : null,
    element("p", { className: "profile-game-date" }, formatDate(resource.played_at)),
    participantRows(resource, i18n, handlers.openPlayer),
  );
  const themeView = element("section", { className: "profile-section player-game-themes" });
  const renderTheme = (index: number): void => {
    const theme = resource.themes[index];
    if (!theme) return;
    themeView.replaceChildren(
      element(
        "h3",
        {},
        `${i18n.t("profile.theme")} ${index + 1} / ${resource.themes.length}`,
      ),
      themeGrid(theme, resource.participants, i18n, handlers.openPlayer),
    );
    navigation.replaceChildren(
      element("button", {
        type: "button",
        disabled: index === 0,
        onclick: (() => {
          current -= 1;
          renderTheme(current);
        }) as EventListener,
      }, i18n.t("common.previous")),
      element("button", {
        type: "button",
        disabled: index === resource.themes.length - 1,
        onclick: (() => {
          current += 1;
          renderTheme(current);
        }) as EventListener,
      }, i18n.t("common.next")),
    );
  };
  const navigation = element("nav", { className: "pagination", "aria-label": i18n.t("profile.theme") });
  let current = 0;
  renderTheme(0);
  return element(
    "section",
    { className: "route-content profile player-game" },
    header,
    themeView,
    navigation,
  );
}
