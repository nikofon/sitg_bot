# SITG Bot

SITG is a tournament platform for Jeopardy-style multiplayer games, played through private
Telegram conversations. A console interface supports local play and development, and a
Telegram Mini App handles tournament, lobby, and packet management.

## Product baseline

- **Tournaments** organize participation, managers, game settings, packet access, and ratings.
  Ladder supports ongoing competition; Classic has a finite schedule, with stages and
  brackets still planned.
- **Lobbies** let players assemble by invitation or, where enabled, find other players.
  Observers can join when tournament policy permits.
- **Games** currently use the SI ruleset: questions revealed progressively, buzzing,
  timed answers, scoring, and appeals. Additional game formats are planned.
- **Packets** are reusable collections of questions. Managers can import, review, publish,
  correct, and substitute content while preserving past games.
- **Fair play** includes global protection against repeating exposed content, tournament and
  ruleset ratings, player reputation, reports, and reviewable suspicion signals.
- **Continuity and privacy** are core requirements: games and conversations recover after
  interruption; public nicknames are separate from private registration details.

Telegram registration, gameplay, participant chat, appeals, and manager workflows are
implemented. Some menu destinations remain placeholders; the project is still in development.

## Documentation

| If you need to… | Read |
| --- | --- |
| Navigate the code or understand the architecture | [Architecture and developer guide](docs/architecture.md) — technical index and contribution guidelines |
| Understand project configuration and database constraints | [Project configuration and database operations](docs/database-operations.md) |
| Use the console interface | [Console user guide](docs/console-commands.md) |
| Choose work that is still planned | [Planned features](docs/future-work.md) |

Implementation guides for each subsystem are indexed in the architecture document.
