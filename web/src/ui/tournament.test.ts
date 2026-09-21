import { describe, expect, it, vi } from "vitest";
import type { TournamentProfileResource } from "../api/types";
import { I18n } from "../i18n";
import { renderTournamentProfile } from "./tournament";

const tournamentId = "00000000-0000-0000-0000-000000000001";
const playerOneId = "11111111-1111-1111-1111-111111111111";
const playerTwoId = "22222222-2222-2222-2222-222222222222";
const gameId = "33333333-3333-3333-3333-333333333333";

const general: TournamentProfileResource["general"] = {
  name: "Autumn <b>Open</b>",
  slug: "autumn-open",
  description: "Season opener",
  status: "active",
  moderation_status: "normal",
  visibility: "public",
  language: "en",
  payment_type: "free",
  type: { key: "ladder", name: "Ladder", version: 1 },
  ruleset: { key: "si", name: "SI", version: 1 },
  starts_at: "2026-09-10T12:00:00Z",
  planned_ends_at: null,
  actual_starts_at: null,
  actual_ends_at: null,
  finalized_at: null,
  settings_version: 1,
  registration: { open: true, starts_at: null, ends_at: null },
  managers: [{ player_id: playerOneId, name: "Manager One" }],
  authors: ["Author One"],
  registration_count: 3,
  participant_count: 2,
};

const ladder: TournamentProfileResource = {
  kind: "tournament_profile",
  state: "ready",
  type_key: "ladder",
  tournament: { id: tournamentId, name: "Autumn <b>Open</b>", slug: "autumn-open" },
  general,
  registrations: [
    { player_id: playerOneId, nickname: "Player One", status: "approved", registered_at: null },
    { player_id: playerTwoId, nickname: "Player Two", status: "registered", registered_at: null },
  ],
  participants: null,
  games: {
    kind: "ladder",
    items: [
      {
        game_id: gameId,
        played_at: "2026-09-01T10:00:00Z",
        participants: [
          { player_id: playerOneId, nickname: "Player One", score: 10, place: 1 },
          { player_id: playerTwoId, nickname: "Player Two", score: -5, place: 2 },
        ],
      },
    ],
  },
  leaders: {
    kind: "ladder",
    items: [
      { player_id: playerOneId, nickname: "Player One", rating: 1010.5 },
      { player_id: playerTwoId, nickname: "Player Two", rating: 990 },
    ],
  },
};

function classicProfile(): TournamentProfileResource {
  return {
    kind: "tournament_profile",
    state: "ready",
    type_key: "classic",
    tournament: { id: tournamentId, name: "Autumn <b>Open</b>", slug: "autumn-open" },
    general,
    registrations: ladder.registrations,
    participants: [
      { player_id: playerOneId, nickname: "Player One" },
      { player_id: playerTwoId, nickname: "Player Two" },
    ],
    games: {
      kind: "classic",
      stages: [
        {
          kind: "first",
          stage_type: "groups",
          scheme_key: "groups-9-4",
          started_at: null,
          completed_at: null,
          groups: [1, 2],
          rounds: [
            {
              number: 1,
              matches: [
                {
                  id: "44444444-4444-4444-4444-444444444441",
                  group: 1,
                  number: 1,
                  game_id: gameId,
                  played_at: "2026-09-01T10:00:00Z",
                  participants: [
                    { player_id: playerOneId, nickname: "Player One", score: 10, place: 1 },
                  ],
                  players: [{ player_id: playerOneId, nickname: "Player One" }],
                  manual_results: [],
                },
                {
                  id: "44444444-4444-4444-4444-444444444442",
                  group: 2,
                  number: 1,
                  game_id: null,
                  played_at: null,
                  participants: [],
                  players: [{ player_id: playerTwoId, nickname: "Player Two" }],
                  manual_results: [
                    {
                      player_id: playerTwoId,
                      nickname: "Player Two",
                      place: "1",
                      score: "20",
                      points: "4",
                    },
                  ],
                },
              ],
            },
            {
              number: 2,
              matches: [
                {
                  id: "44444444-4444-4444-4444-444444444443",
                  group: 1,
                  number: 2,
                  game_id: null,
                  played_at: null,
                  participants: [],
                  players: [],
                  manual_results: [],
                },
              ],
            },
          ],
        },
        {
          kind: "playoff",
          stage_type: "playoff",
          scheme_key: "playoff-8",
          started_at: null,
          completed_at: null,
          groups: [1],
          rounds: [{ number: 1, matches: [] }],
        },
      ],
    },
    leaders: {
      kind: "classic",
      stages: [
        {
          kind: "first",
          stage_type: "groups",
          standings: [
            { player_id: playerOneId, nickname: "Player One", points: "8", score: "120" },
            { player_id: playerTwoId, nickname: "Player Two", points: "4", score: "90" },
          ],
        },
        {
          kind: "playoff",
          stage_type: "playoff",
          places: [
            { player_id: playerOneId, nickname: "Player One", place: "1" },
            { player_id: playerTwoId, nickname: "Player Two", place: "5.5" },
          ],
        },
      ],
    },
  };
}

describe("tournament profile", () => {
  it("renders general details and hides classic-only sections for ladder", () => {
    const root = renderTournamentProfile(ladder, new I18n("en"), () => "—", {
      openPlayer: vi.fn(),
      openGame: vi.fn(),
    });

    expect(root.querySelector(".tournament-name")!.textContent).toBe("Autumn <b>Open</b>");
    const labels = Array.from(
      root.querySelectorAll(".tournament-section-nav button"),
      (item) => item.textContent,
    );
    expect(labels).toEqual(["General", "Registrations", "Games", "Leaders"]);
    expect(root.querySelector(".detail-list")!.textContent).toContain("Ladder");
    expect(root.querySelector(".detail-list")!.textContent).toContain("Manager One");
  });

  it("marks registration decisions with status badges", () => {
    const root = renderTournamentProfile(ladder, new I18n("en"), () => "—", {
      openPlayer: vi.fn(),
      openGame: vi.fn(),
    });
    Array.from(root.querySelectorAll<HTMLButtonElement>(".tournament-section-nav button"))
      .find((item) => item.textContent === "Registrations")!
      .click();

    const items = Array.from(root.querySelectorAll(".tournament-registration"));
    expect(items).toHaveLength(2);
    expect(items[0]!.querySelector(".badge")!.className).toContain("badge-approved");
    expect(items[1]!.querySelector(".badge")!.className).toContain("badge-warning");
  });

  it("lists ladder games and opens examination for a placed participant", () => {
    const openGame = vi.fn();
    const root = renderTournamentProfile(ladder, new I18n("en"), () => "—", {
      openPlayer: vi.fn(),
      openGame,
    });
    Array.from(root.querySelectorAll<HTMLButtonElement>(".tournament-section-nav button"))
      .find((item) => item.textContent === "Games")!
      .click();

    expect(root.querySelectorAll(".tournament-game-card")).toHaveLength(1);
    root.querySelector<HTMLButtonElement>(".tournament-game-card .primary-button")!.click();
    expect(openGame).toHaveBeenCalledWith(playerOneId, gameId);
  });

  it("lists ladder leaders by in-tournament rating", () => {
    const root = renderTournamentProfile(ladder, new I18n("en"), () => "—", {
      openPlayer: vi.fn(),
      openGame: vi.fn(),
    });
    Array.from(root.querySelectorAll<HTMLButtonElement>(".tournament-section-nav button"))
      .find((item) => item.textContent === "Leaders")!
      .click();

    const rows = root.querySelectorAll(".tournament-leader");
    expect(rows).toHaveLength(2);
    expect(rows[0]!.querySelector(".tournament-leader-rating")!.textContent).toBe("1010.5");
  });

  it("shows participants for classic tournaments", () => {
    const root = renderTournamentProfile(classicProfile(), new I18n("en"), () => "—", {
      openPlayer: vi.fn(),
      openGame: vi.fn(),
    });
    const labels = Array.from(
      root.querySelectorAll(".tournament-section-nav button"),
      (item) => item.textContent,
    );
    expect(labels).toEqual(["General", "Registrations", "Participants", "Games", "Leaders"]);

    Array.from(root.querySelectorAll<HTMLButtonElement>(".tournament-section-nav button"))
      .find((item) => item.textContent === "Participants")!
      .click();
    expect(root.querySelectorAll(".tournament-registration")).toHaveLength(2);
  });

  it("browses classic group games by group and round", () => {
    const root = renderTournamentProfile(classicProfile(), new I18n("en"), () => "—", {
      openPlayer: vi.fn(),
      openGame: vi.fn(),
    });
    Array.from(root.querySelectorAll<HTMLButtonElement>(".tournament-section-nav button"))
      .find((item) => item.textContent === "Games")!
      .click();

    expect(root.querySelectorAll(".tournament-game-card")).toHaveLength(1);
    expect(root.querySelector(".pagination-status")!.textContent).toContain("Round 1 / 2");

    const groupSelect = root.querySelector<HTMLSelectElement>(
      '.tournament-games-controls select[aria-label="Group"]',
    )!;
    expect(groupSelect.value).toBe("0");
    groupSelect.value = "1";
    groupSelect.dispatchEvent(new Event("change"));

    const cards = root.querySelectorAll(".tournament-game-card");
    expect(cards).toHaveLength(1);
    expect(cards[0]!.textContent).toContain("Player Two");
    expect(root.querySelector(".tournament-game-card .primary-button")).toBeNull();

    const next = Array.from(root.querySelectorAll<HTMLButtonElement>(".pagination button"))
      .find((item) => item.textContent === "Next")!;
    next.click();
    expect(root.querySelector(".pagination-status")!.textContent).toContain("Round 2 / 2");
  });

  it("opens on the requested section for deep links", () => {
    const root = renderTournamentProfile(ladder, new I18n("en"), () => "—", {
      openPlayer: vi.fn(),
      openGame: vi.fn(),
    }, "leaders");

    expect(
      Array.from(root.querySelectorAll<HTMLButtonElement>(".tournament-section-nav button"))
        .find((item) => item.classList.contains("active"))!
        .textContent,
    ).toBe("Leaders");
    expect(root.querySelector(".tournament-leaders")).not.toBeNull();

    const fallback = renderTournamentProfile(ladder, new I18n("en"), () => "—", {
      openPlayer: vi.fn(),
      openGame: vi.fn(),
    }, "participants");
    expect(
      Array.from(fallback.querySelectorAll<HTMLButtonElement>(".tournament-section-nav button"))
        .find((item) => item.classList.contains("active"))!
        .textContent,
    ).toBe("General");
  });

  it("switches classic leader stages and shows shared play-off places", () => {
    const root = renderTournamentProfile(classicProfile(), new I18n("en"), () => "—", {
      openPlayer: vi.fn(),
      openGame: vi.fn(),
    });
    Array.from(root.querySelectorAll<HTMLButtonElement>(".tournament-section-nav button"))
      .find((item) => item.textContent === "Leaders")!
      .click();

    expect(root.querySelector(".leader-table")!.textContent).toContain("8");

    const select = root.querySelector<HTMLSelectElement>(".tournament-profile-section select")!;
    expect(Array.from(select.options).map((option) => option.textContent)).toEqual([
      "First stage",
      "Play-off",
    ]);
    select.value = "1";
    select.dispatchEvent(new Event("change"));

    const places = Array.from(
      root.querySelectorAll(".leader-table th[scope=row]"),
      (item) => item.textContent,
    );
    expect(places).toEqual(["1", "5.5"]);
  });
});
