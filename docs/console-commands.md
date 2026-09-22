# Console User Guide

The console is a local play/testing interface. With the project installed and database
initialized, start `sitg-server` in one terminal. See
[project configuration](database-operations.md) for connection settings.

## Connect and find your context

Open a terminal per player:

```bash
sitg-console --player-id 101 --name Alice
sitg-console --player-id 102 --name Bob
```

Or start `sitg-console` and enter `login 101 "Alice"`. Reuse the same numeric login ID
to recover your account and available lobby/game. These are local test identities.

| Command | Use |
| --- | --- |
| `login <numeric-id> "<name>" [--admin [token]]` | Log in, optionally as administrator |
| `help` | Show built-in command help |
| `me` | Show account IDs and recover current context |
| `quit` | Disconnect the console; does not explicitly abandon a game |

Connection options are `--host` (default `127.0.0.1`), `--port` (default `8765`),
`--player-id`, `--name`, `--admin`, and `--admin-token`.

Below, `<...>` means replace with a value; `[...]` means optional. UUIDs are the long IDs
printed by commands. Numeric player IDs are the IDs used at login. Quote text containing
spaces. Wrap JSON in single quotes so its double quotes survive:

```text
tournament policy observing '"unlimited"'
tournament metadata authors '["Alice","Bob"]'
```

Unless a command accepts a tournament ID, it uses the selected tournament. Lobby and game
commands similarly use current context.

## Select a tournament

| Command | Use |
| --- | --- |
| `tournament list public` | Discover public tournaments |
| `tournament list mine` | List your memberships |
| `tournament register [tournament-id]` | Apply to join; private tournaments require an invitation |
| `tournament use <tournament-id>` | Select a tournament you belong to |
| `tournament manage <tournament-id>` | Select a tournament you manage; no player membership required |
| `tournament info` | Show selected tournament, rules, schedule, settings, and ratings |
| `packets` | List discoverable packets, your access, and fresh content |

Discovery does not enroll you. Registration requires manager approval. Ladder approval
activates participation; finite tournaments also require participant-list finalization.
Managers cannot play in the tournament they manage.

New tournaments require **setup finalization** before player access. Run `tournament finalize`
or use the manager Mini App. Participant-list finalization is a separate command:
`tournament participants finalize [approved-player-uuid ...]`.

## Play a game

For an already configured tournament where Alice and Bob are active players:

1. Both run `tournament use <tournament-id>`.
2. Alice runs `packets`, `lobby create 2`, then `lobby packet add <packet-id>`.
3. Bob runs `lobby join <invitation-code>` using Alice's displayed code.
4. Both run `lobby ready`. Alice runs `lobby start`.
5. Both run `game join`. Questions advance automatically.
6. Use `game buzz`, then `game answer <text>` when prompted.

For a solo test, create a lobby with capacity `1`. Selected packets must supply enough
content fresh for every player. Use `lobby info` to inspect a readiness/start rejection.

### Lobby commands

| Command | Use |
| --- | --- |
| `lobby create [max-players]` | Create a lobby; default 4, maximum 12 subject to tournament limits |
| `lobby join <lobby-id-or-code>` | Join by invitation |
| `lobby info [lobby-id]` | Show members, settings, packets, and validation |
| `lobby packet add <packet-id>` / `lobby packet remove <packet-id>` | Creator selects/removes a packet |
| `lobby suggestions` | Show suggested packets |
| `lobby setting <name> <value>` | Creator changes a player-mutable setting |
| `lobby ready [off]` | Set or withdraw your readiness |
| `lobby start` | Creator assigns the game after everyone is ready |
| `lobby leave` | Leave; creator departure cancels the lobby |
| `lobby cancel` | Creator cancels for everyone |
| `lobby find` / `lobby find cancel` | Creator starts/stops finding other players |
| `lobby observe <lobby-id-or-code> [confirm]` | Join as an observer when permitted |
| `lobby role <player-or-observer> [confirm]` | Switch your lobby role |

Settings, packet, and membership changes can clear readiness. `lobby find` requires a
tournament that supports and enables matchmaking. Blacklists affect matchmaking, not
direct invitations:

```text
blacklist
blacklist add <numeric-player-id>
blacklist remove <numeric-player-id>
```

Observers do not occupy player seats or become ready. If fresh content needs consent,
repeat the observer command with `confirm`; seeing it makes it unavailable for later play.
Packet/settings changes can require renewed consent.

### Game commands

| Command | Use |
| --- | --- |
| `game join` | Join the assigned game or reconnect to an eligible abandoned game |
| `game info [game-id]` | Show game state and select that game as console context |
| `game buzz` | Attempt to answer the current question |
| `game answer <answer text>` | Submit your answer |
| `game score` | Request scores |
| `game pause` / `game resume` | Pause/resume when permitted |
| `game abandon` | Leave participation |
| `game next` | Manually advance for testing |
| `game observe list` | List observable ongoing games in the current tournament |
| `game observe <game-id> [confirm]` | Observe, confirming fresh-content exposure if requested |
| `game observe leave [game-id]` | Stop observing |

The first player has five minutes to join; their join starts a new five-minute window for
the remaining players. Failure to join prevents the game from starting. A paused game with
no connected participants ends after five minutes. Abandonment after disclosure preserves
exposure and earned results. Observers can inspect state/scores but cannot play or vote.

### Appeals and feedback

Submit an appeal immediately after a question. Omit the attempt ID only when one answer is
eligible; otherwise use an ID printed beneath the revealed answer.

| Command | Use |
| --- | --- |
| `game appeal [attempt-id]` | Appeal an eligible answer |
| `game appeal vote <approve-or-reject>` | Vote in the current appeal |
| `game appeal escalate <on-or-off>` | Accept/decline escalation after a rejected vote, if offered |
| `game appeal comment <text>` | Send commentary to reviewing managers |
| `game reputation <upvote-or-downvote> <numeric-player-id>` | Rate another participant after a completed game |
| `game report <cheating-or-toxicity> <numeric-player-id> [details]` | Report another participant after a completed game |

Choice placeholders such as `<approve-or-reject>` mean type either `approve` or `reject`.
Follow the displayed appeal tally, deadline, and next action. Escalated review can continue
while gameplay resumes. Reputation changes by one point, with a seven-day cooldown per
directed player pair. One report per reporter/target/game is accepted.

## Manage a tournament

These commands require the applicable manager role. Keep at least one manager.
Select your tournament with `tournament manage <tournament-id>`, then use commands such as
`tournament info`, `tournament setting`, and `tournament finalize`.

### Creation

An administrator issues a token (see below). Its recipient runs:

```text
tournament create <token> <slug> "<name>" [options]
```

| Option | Values/default |
| --- | --- |
| `--type` | `ladder` (default), `classic` |
| `--ruleset` | `si` |
| `--visibility` | `private` (default), `public` |
| `--language` | Language tag, e.g. `en`, `ru`; default unspecified |
| `--payment-type` | `free` (default), `one-time`, `per-stage` |
| `--pricing-plans` | Quoted JSON list of named plans |
| `--registration-open` | `on` or `off` (default) |
| `--registration-starts-at`, `--registration-ends-at` | Registration dates |
| `--starts-at`, `--planned-ends-at` | Tournament dates |

Dates must be ISO-8601 with timezone, e.g. `2026-10-01T10:00:00+03:00`.
Finite tournaments require registration end, start, and planned finish. Creation makes you
a manager, not a player. Run `tournament finalize` after configuring the tournament and before
inviting players to play.

### Membership, metadata, and pricing

| Command | Use |
| --- | --- |
| `tournament invite <player-uuid>` | Invite a player |
| `tournament approve <player-uuid>` | Approve their registration |
| `tournament registrations` | List all registrations with player UUIDs, names, and current statuses |
| `tournament approve all` | Approve all pending registrations atomically; report the approved count |
| `tournament finalize` | Finish tournament setup and enable player access |
| `tournament start` | Start a finalized non-Classic tournament now |
| `tournament stage start <first|playoff>` | Start a configured Classic stage now |
| `tournament participants finalize [approved-player-uuid ...]` | Finalize a finite participant list; reject unselected applicants |
| `tournament member add <player-uuid>` | Directly activate membership |
| `tournament manager add <player-uuid>` / `remove <player-uuid>` | Grant/revoke management |
| `tournament metadata <field> <json-value>` | Update metadata, e.g. `registration-open true` |
| `tournament pricing set <payment-type> <plans-json>` | Replace pricing |
| `tournament complete [ISO-8601]` | Record completion; default now |

For Classic, configure stages in the manager Mini App before starting them. Starting the
first enabled stage also starts the tournament, confirms participants, and closes registration.
If a first stage is enabled, it must finish before the play-off can start. Scheduled start
dates send reminders; use these commands to actually start play.

A pricing plan has a name and positive prices with distinct currencies:

```text
tournament pricing set one-time '[{"name":"Standard","prices":[{"amount":10,"currency":"USD"}]}]'
tournament pricing set free '[]'
```

Registration checks:

```text
tournament requirement list
tournament requirement add has-played-tournament <tournament-id> [failure reason]
tournament requirement add has-not-played-tournament <tournament-id> [failure reason]
tournament requirement add has-not-seen-packet <packet-id> [failure reason]
tournament requirement remove <requirement-id>
```

All checks must pass at registration. “Played” requires exposure during play, not membership.
A rejected player may retry while registration is open.

### Game settings and policies

```text
tournament setting theme_count 8
tournament setting question_values '[100,200,300,400,500]'
tournament setting minus_multiplier 0.5
tournament setting question_token_delay instantaneous
tournament mutable theme_count on
tournament policy hybrid_matchmaking_enabled true
tournament policy observing '"unlimited"'
```

`tournament setting <name> <value>` changes a default. `tournament mutable <name> <on-or-off>`
controls whether lobby creators may change it. `tournament policy <name> <json-value>`
changes a policy. Edits can clear lobby readiness; games already assigned keep their settings.
Classic does not support hybrid matchmaking.

| SI setting | Meaning/value |
| --- | --- |
| `theme_count` | Number of themes, up to 128 |
| `question_values` | Increasing positive integer list matching each theme |
| `minus_multiplier` | Wrong-answer penalty multiplier; default 1 |
| `pausing_allowed` | `on` or `off` |
| `question_token_target_chars` | Target characters per reveal step |
| `question_token_delay` | Seconds between reveal steps; `instantaneous` also accepted |
| `ready_delay`, `message_delay` | Pre-start and message delays, seconds |
| `game_start_to_first_theme_delay`, `theme_to_first_question_delay` | Introductory delays, seconds |
| `theme_author_to_commentary_delay`, `theme_commentary_to_question_delay` | Theme-commentary delays, seconds (used only when a theme has commentary) |
| `question_cost_announcement_delay`, `buzz_timer_countdown_delay` | Question/countdown delays, seconds |
| `buzz_timeout`, `answer_timeout` | Buzz/answer windows, seconds |
| `between_questions_delay`, `last_question_to_theme_complete_delay` | Question/theme transition delays, seconds |
| `theme_complete_to_scoreboard_delay`, `between_themes_delay` | Scoreboard/next-theme delays, seconds |

Use `tournament info` for current policies, defaults, and permitted overrides.

### Packet access and review

```text
tournament packet assign <packet-id> [--version <packet-version-id>] [options]
```

Options: `--library-viewing-rule` accepts `never`, `after-play`, or `anytime`;
flags are `--discoverable`, `--playable`, `--content-visible`, and
`--editable`. Omitting a version follows the newest published version. Reissuing the
command replaces member-wide access flags.

```text
tournament packet entitlement <assignment-id> <player-uuid> <right> <on-or-off>
```

Rights are `discoverable`, `playable`, `content-visible`, and `editable`. Membership alone
does not grant packet rights. Library viewing rules never change these rights;
there is no console library reader.

| Command | Use |
| --- | --- |
| `packet import <path.json-or-path.docx>` | Import a local file into a draft for the current tournament |
| `packet preview <draft-id>` | Inspect interpreted content and validation |
| `packet publish <draft-id>` | Publish an error-free reviewed draft |
| `packet reject <draft-id>` | Reject a draft |
| `packet release <packet-id> <on-or-off> [packet-version-id]` | Owner/admin changes library release |

Import and draft review use the same tournament permissions as Telegram and the Mini App:
managers and platform administrators can upload; active members can upload when the tournament
enables member uploads. Publishing requires a tournament manager or platform administrator.
Imports return a draft summary with validation errors and warnings, including for malformed
files. Inspect the draft before publication; use the Mini App to edit it. A release alone
does not grant reading rights.

### Manager appeal review

```text
appeals
appeals all
appeals decide <appeal-id> <approve-or-reject>
```

The first command lists tickets for the current tournament; `all` spans tournaments you
manage. Tickets show question, official/appealed answers, commentary, and expiry without
identifying the appellant or match.

## Platform administration

Configure `SITG_ADMIN_TOKEN` on the server, then connect:

```bash
sitg-console --player-id 100 --name Admin --admin --admin-token "$SITG_ADMIN_TOKEN"
```

| Command | Use |
| --- | --- |
| `admin token issue [intended-creator-uuid]` | Issue a single-use tournament-creation token |
| `admin token revoke <token>` | Revoke an unused token |
| `admin suspicion review <player-uuid> [limit]` | Inspect suspicion evidence |
| `admin suspicion clear <player-uuid> <note>` | Record a reviewed reset to zero |

Use the stable UUID from `me` when binding a creation token. Clearing suspicion preserves
reports and review history.
