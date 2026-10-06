import { describe, expect, it, vi } from "vitest";
import type { PlayerGameResource, PlayerProfileResource } from "../api/types";
import { I18n } from "../i18n";
import { renderPlayerGame, renderPlayerProfile } from "./profile";

const playerOneId = "11111111-1111-1111-1111-111111111111";
const gameId = "22222222-2222-2222-2222-222222222222";
const participantOne = "33333333-3333-3333-3333-333333333333";
const participantTwoId = "44444444-4444-4444-4444-444444444444";
const playerTwoId = "55555555-5555-5555-5555-555555555555";

const profile: PlayerProfileResource = {
  kind: "player_profile",
  state: "ready",
  player: {
    id: playerOneId,
    nickname: "Player <b>One</b>",
    real_name: "Ivan Ivanov",
    telegram_username: "ivan",
    telegram_public: true,
    viewer_privileged: true,
  },
  rulesets: [
    { key: "si", name: "Своя игра" },
    { key: "other", name: "Other rules" },
  ],
  ruleset_key: "si",
  rating: {
    value: 1010.5,
    history: [
      { played_at: "2026-08-01T10:00:00Z", rating: 1000 },
      { played_at: "2026-09-01T10:00:00Z", rating: 1010.5 },
    ],
  },
  stats: {
    games: 2,
    wins: 1,
    win_rate: 50,
    placements: [
      { kind: "place_1", count: 1, percent: 50 },
      { kind: "place_1_5", count: 0, percent: 0 },
      { kind: "place_2", count: 0, percent: 0 },
      { kind: "place_2_5", count: 1, percent: 50 },
      { kind: "place_3", count: 0, percent: 0 },
      { kind: "place_3_5", count: 0, percent: 0 },
      { kind: "place_4", count: 0, percent: 0 },
      { kind: "below_4", count: 0, percent: 0 },
    ],
  },
  si_question_stats: [
    { value: 10, correct: 3, incorrect: 1 },
    { value: 20, correct: 0, incorrect: 2 },
  ],
  games: [
    {
      game_id: gameId,
      tournament_id: "66666666-6666-6666-6666-666666666666",
      tournament_name: null,
      stage: null,
      played_at: "2026-09-01T10:00:00Z",
      participants: [
        {
          participant_id: participantOne,
          player_id: playerOneId,
          nickname: "Player <b>One</b>",
          score: 10,
          place: 1,
        },
        {
          participant_id: participantTwoId,
          player_id: playerTwoId,
          nickname: "Player Two",
          score: -5,
          place: 2.5,
        },
      ],
    },
  ],
};

const game: PlayerGameResource = {
  kind: "player_game",
  state: "ready",
  game_id: gameId,
  player_id: playerOneId,
  tournament_name: null,
  tournament_visible: false,
  stage: null,
  played_at: "2026-09-01T10:00:00Z",
  participants: profile.games[0]!.participants,
  themes: [
    {
      index: 1,
      questions: [
        {
          value: 10,
          answers: { [participantOne]: "correct", [participantTwoId]: "incorrect" },
        },
        { value: 20, answers: { [participantOne]: "incorrect" } },
      ],
    },
    {
      index: 2,
      questions: [{ value: 10, answers: { [participantTwoId]: "correct" } }],
    },
  ],
};

describe("player profile", () => {
  it("renders identity, ruleset selection, rating graph, and placement distribution", () => {
    const root = renderPlayerProfile(profile, new I18n("en"), () => "—", {
      selectRuleset: vi.fn(),
      openPlayer: vi.fn(),
      openGame: vi.fn(),
    });
    expect(root.querySelector(".profile-name")!.textContent).toContain("Player <b>One</b>");
    expect(root.querySelector(".profile-identity")!.textContent).toContain("Ivan Ivanov");
    expect(root.querySelector(".profile-identity")!.textContent).toContain("@ivan");
    const select = root.querySelector<HTMLSelectElement>(".profile-ruleset-select")!;
    expect(select.value).toBe("si");
    expect(root.querySelector(".rating-graph polyline")).not.toBeNull();
    const rows = [...root.querySelectorAll(".profile-distribution-row")].map(
      (row) => row.textContent,
    );
    expect(rows[0]).toContain("1st place");
    expect(rows[1]).toContain("1.5 place");
    expect(rows[3]).toContain("2.5 place");
    expect(rows[5]).toContain("3.5 place");
    expect(root.querySelector(".profile-question-stats")!.textContent).toContain("3");
  });

  it("hides private tournament names and links participants and details", () => {
    const openPlayer = vi.fn();
    const openGame = vi.fn();
    const selectRuleset = vi.fn();
    const root = renderPlayerProfile(profile, new I18n("en"), () => "—", {
      selectRuleset,
      openPlayer,
      openGame,
    });
    const card = root.querySelector<HTMLElement>(".profile-game-card")!;
    expect(card.textContent).toContain("Private tournament");
    expect(card.textContent).not.toContain("Ivan Ivanov");
    const anchors = [...card.querySelectorAll<HTMLAnchorElement>(".game-participant a")];
    expect(anchors[1]!.getAttribute("href")).toBe(`/players/${playerTwoId}`);
    anchors[1]!.click();
    expect(openPlayer).toHaveBeenCalledWith(playerTwoId);
    const select = root.querySelector<HTMLSelectElement>(".profile-ruleset-select")!;
    select.value = "other";
    select.dispatchEvent(new Event("change"));
    expect(selectRuleset).toHaveBeenCalledWith("other");
    card.querySelector<HTMLButtonElement>(".settings-actions button")!.click();
    expect(openGame).toHaveBeenCalledWith(gameId);
  });
});

describe("player game results", () => {
  it("shows one theme grid at a time with answer marks and navigation", () => {
    const back = vi.fn();
    const openPlayer = vi.fn();
    const root = renderPlayerGame(game, new I18n("en"), () => "—", { back, openPlayer });
    expect(root.querySelector(".profile-name")!.textContent).toBe("Private tournament");
    expect(root.querySelector(".player-game-themes h3")!.textContent).toContain("Theme 1 / 2");
    const marks = [...root.querySelectorAll(".player-game-grid tbody tr")].map((row) =>
      [...row.querySelectorAll(".answer-mark")].map((cell) => cell.className),
    );
    expect(marks[0]).toEqual(["answer-mark answer-correct", "answer-mark answer-incorrect"]);
    expect(marks[1]).toEqual(["answer-mark answer-incorrect", "answer-mark answer-none"]);
    const navigation = root.querySelectorAll<HTMLButtonElement>(".pagination button");
    expect(navigation[0]!.disabled).toBe(true);
    expect(navigation[1]!.disabled).toBe(false);
    navigation[1]!.click();
    expect(root.querySelector(".player-game-themes h3")!.textContent).toContain("Theme 2 / 2");
    expect(root.querySelectorAll(".player-game-grid tbody .answer-none").length).toBe(1);
    root.querySelector<HTMLButtonElement>(".settings-actions button")!.click();
    expect(back).toHaveBeenCalled();
  });

  it("links participants to their profiles", () => {
    const openPlayer = vi.fn();
    const root = renderPlayerGame(game, new I18n("en"), () => "—", {
      back: vi.fn(),
      openPlayer,
    });
    const anchor = root.querySelector<HTMLAnchorElement>(".player-game-grid a")!;
    expect(anchor.getAttribute("href")).toBe(`/players/${playerOneId}`);
    anchor.click();
    expect(openPlayer).toHaveBeenCalledWith(playerOneId);
  });
});


describe("additional profile statistics", () => {
  it("localizes SI statistics and distinguishes zero scores from missing data", () => {
    const render = (average: number | null) => renderPlayerProfile({
      ...profile,
      si_statistics: { average_normalized_score: average, buzz_times: [
        { value: 10, average_seconds: 1.25, samples: 3 },
        { value: 20, average_seconds: null, samples: 0 },
      ] },
    }, new I18n("ru"), () => "—", {
      selectRuleset: vi.fn(), openPlayer: vi.fn(), openGame: vi.fn(),
    });
    const root = render(0);
    expect(root.querySelector(".profile-si-statistics h3")!.textContent).toBe("Статистика СИ");
    expect(root.querySelector(".profile-si-statistics dd")!.textContent).toBe("0");
    expect(root.querySelector(".profile-si-statistics tbody td")!.textContent).toBe("1,25");
    expect(render(null).querySelector(".profile-si-statistics dd")!.textContent).toBe("—");
  });

  it("labels both rating axes and spaces chronologically ordered points by elapsed time", () => {
    const history = [
      { played_at: "2026-09-11T12:00:00Z", rating: 1100 },
      { played_at: "2026-09-01T12:00:00Z", rating: 1000 },
      { played_at: "2026-09-02T12:00:00Z", rating: 1050 },
    ];
    const root = renderPlayerProfile({ ...profile, rating: { value: 1100, history } }, new I18n("en"), () => "—", {
      selectRuleset: vi.fn(), openPlayer: vi.fn(), openGame: vi.fn(),
    });
    const graph = root.querySelector(".rating-graph")!;
    expect([...graph.querySelectorAll(".rating-graph-y-label")].map(label => label.textContent))
      .toEqual(["999", "1,050", "1,101"]);
    const dates = [...graph.querySelectorAll(".rating-graph-x-label")].map(label => label.textContent);
    const format = new Intl.DateTimeFormat("en", { day: "numeric", month: "short", year: "2-digit" });
    expect(dates).toEqual([1, 6, 11].map(day => format.format(new Date(`2026-09-${String(day).padStart(2, "0")}T12:00:00Z`))));
    const xs = graph.querySelector("polyline")!.getAttribute("points")!.split(" ").map(point => Number(point.split(",")[0]));
    expect((xs[1]! - xs[0]!) / (xs[2]! - xs[0]!)).toBeCloseTo(0.1);
    expect(graph.getAttribute("aria-label")).toContain("1,050");
    expect(history[0]!.rating).toBe(1100);
  });

  it("keeps constant ratings and identical timestamps finite and adds intraday time labels", () => {
    const history = [
      { played_at: "2026-09-01T12:00:00Z", rating: 1000 },
      { played_at: "2026-09-01T12:00:00Z", rating: 1000 },
    ];
    const root = renderPlayerProfile({ ...profile, rating: { value: 1000, history } }, new I18n("ru"), () => "—", {
      selectRuleset: vi.fn(), openPlayer: vi.fn(), openGame: vi.fn(),
    });
    const graph = root.querySelector(".rating-graph")!;
    expect(graph.querySelectorAll(".rating-graph-x-label")).toHaveLength(1);
    expect(graph.querySelectorAll(".rating-graph-time-label")).toHaveLength(1);
    const points = graph.querySelector("polyline")!.getAttribute("points")!.split(/[ ,]/).map(Number);
    expect(points.every(Number.isFinite)).toBe(true);
    expect(points[1]).toBe(points[3]);
    expect(graph.querySelectorAll(".rating-graph-y-label")).toHaveLength(3);
  });

  it("omits graphs without enough valid dated ratings", () => {
    const root = renderPlayerProfile({ ...profile, rating: { value: 1000, history: [
      { played_at: "invalid", rating: 1000 },
      { played_at: "2026-09-01T12:00:00Z", rating: NaN },
      { played_at: "2026-09-02T12:00:00Z", rating: 1000 },
    ] } }, new I18n("en"), () => "—", {
      selectRuleset: vi.fn(), openPlayer: vi.fn(), openGame: vi.fn(),
    });
    expect(root.querySelector(".rating-graph")).toBeNull();
  });

  it("shows packet names and post-game ratings safely, without substituting current ratings", () => {
    const resource = structuredClone(profile);
    resource.games[0]!.packets = [{ name: "Packet <script>" }, { name: "Second packet" }];
    resource.games[0]!.participants[0]!.global_rating_after = 1200.5;
    resource.games[0]!.participants[0]!.tournament_rating_after = 0;
    const root = renderPlayerProfile(resource, new I18n("en"), () => "—", {
      selectRuleset: vi.fn(), openPlayer: vi.fn(), openGame: vi.fn(),
    });
    expect(root.querySelector(".profile-game-packets")!.textContent).toBe("Packets used: Packet <script>, Second packet");
    expect(root.querySelector("script")).toBeNull();
    const ratings = root.querySelectorAll(".game-participant-ratings");
    expect(ratings[0]!.textContent).toContain("Global rating: 1,200.5");
    expect(ratings[0]!.textContent).toContain("Tournament rating: 0");
    expect(ratings[0]!.getAttribute("title")).toBe("Ratings when this game ended");
    expect(ratings[1]!.textContent).toBe("Global rating: —");
  });

  it("renders SI aggregates only when supplied, preserving zero and missing samples", () => {
    const render = (resource: PlayerProfileResource) => renderPlayerProfile(resource, new I18n("en"), () => "—", {
      selectRuleset: vi.fn(), openPlayer: vi.fn(), openGame: vi.fn(),
    });
    expect(render(profile).querySelector(".profile-si-statistics")!.textContent).toContain("not available yet");
    const root = render({ ...profile, si_statistics: {
      average_normalized_score: -12.25,
      buzz_times: [{ value: 10, average_seconds: 0, samples: 2 }, { value: 20, average_seconds: null, samples: 0 }],
    } });
    expect(root.querySelector(".profile-si-statistics dd")!.textContent).toBe("-12.25");
    const rows = root.querySelectorAll(".profile-si-statistics tbody tr");
    expect(rows).toHaveLength(5);
    expect([...rows[0]!.children].map(cell => cell.textContent)).toEqual(["10", "0", "2"]);
    expect([...rows[1]!.children].map(cell => cell.textContent)).toEqual(["20", "—", "0"]);
    expect([...rows[2]!.children].map(cell => cell.textContent)).toEqual(["30", "—", "—"]);
    expect(render({ ...profile, ruleset_key: "other" }).querySelector(".profile-si-statistics")).toBeNull();
  });

  it("shows packets in game details and distinguishes unavailable data from no packets", () => {
    for (const [packets, expected] of [
      [undefined, "Not available"], [null, "Not available"], [[], "None"],
      [[{ name: "First <b>packet</b>" }, { name: null }], "First <b>packet</b>, Not available"],
    ] as const) {
      const root = renderPlayerGame({ ...game, packets: packets ? [...packets] : packets }, new I18n("en"), () => "—", {
        back: vi.fn(), openPlayer: vi.fn(),
      });
      expect(root.querySelector(".profile-game-packets")!.textContent).toBe(`Packets used: ${expected}`);
      expect(root.querySelector(".profile-game-packets b")).toBeNull();
    }
  });
});
