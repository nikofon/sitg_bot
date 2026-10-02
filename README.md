# SITG Bot

English | [Русский](README.ru.md)

SITG is a tournament platform for Jeopardy-style multiplayer games, played through private
Telegram conversations. A console interface supports local play and development, and a
Telegram Mini App handles tournament, lobby, and packet management.

## How to start playing

1. Open the bot in Telegram, send `/start`, and follow the registration prompts.
2. Open **Tournaments**, register for an ongoing tournament, and wait for approval.
   Press **Select** in the tournament list.
3. Press **Create lobby** and share the invitation link with friends registered in that tournament.
4. Open **Lobby info → Lobby settings**, select a packet, and gather the required number of players.
5. Everyone presses **Ready**; the lobby creator presses **Start game**.
   Each player then presses **Join** in the bot's game invitation.
6. Press `+` to buzz and reply to the bot's answer prompt before time runs out.
   Use `/score` to check scores and `/quit` after the game ends.

## How to set up your own tournament

1. Complete `/start` registration, then choose **Switch mode → Manager**.
2. Press **Request tournament token**, enter the tournament name and a short message,
   and wait for administrator approval. Check **My tokens** for the result.
3. Press **Create tournament**. Choose **Ladder** for an ongoing tournament, select the
   SI ruleset, fill in the requested details, and confirm creation.
4. Open **Tournament management → Settings**. Set the number of players and themes per game,
   enable **Automatically approve registrations** and **Packets playable by default**, and save.
5. Return to the bot and press **Upload packets**. Send a question packet in JSON or DOCX
   format, review the upload, and publish it.
6. Open **Tournament management → General**, press **Finalize tournament**, enable
   **Registration available**, and press **Start tournament**.
7. Return to the bot, press **Registration link**, and share it with players.
   They can now register and create lobbies. You manage this tournament and cannot play in it.

## Product baseline

- **Tournaments** organize participation, managers, game settings, packet access, and ratings.
  Ladder supports ongoing competition; Classic supports groups, solo quizzes, play-offs,
  and double elimination with prescribed participants and round deadlines.
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
| Choose work that is still planned | [Planned features](docs/future-work.md) |

Implementation guides for each subsystem are indexed in the architecture document.
