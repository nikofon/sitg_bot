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
