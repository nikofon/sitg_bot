# Telegram Adapter

[Technical index](architecture.md) · [Application contract](application.md) ·
[Configuration](database-operations.md)

## Source map

After an appeal decision or escalation submission, delivery waits the game's
`message_delay` before subsequent events. The per-chat deadline persists across
retries and restarts, including when several events arrive in one batch.

| Module | Responsibility |
| --- | --- |
| [bot/app.py](../src/sitg_bot/bot/app.py), [router.py](../src/sitg_bot/bot/router.py) | Polling composition, middleware order, routers, remote clients, consumers |
| [bot/middleware](../src/sitg_bot/bot/middleware) | Correlation, update deduplication, private identity, player context, locale, errors |
| [bot/handlers](../src/sitg_bot/bot/handlers) | Registration, navigation, player, lobby, game, manager, admin, chat inputs |
| [bot/state](../src/sitg_bot/bot/state) | Backend facade, conversation models, settings |
| [bot/presenters](../src/sitg_bot/bot/presenters), [keyboards](../src/sitg_bot/bot/keyboards), [i18n](../src/sitg_bot/bot/i18n) | Projections, rendering, controls, Russian/English text |
| [bot/callbacks.py](../src/sitg_bot/bot/callbacks.py), [miniapps](../src/sitg_bot/bot/miniapps) | Actor-bound callback references and Mini App launch links |
| [bot/game_delivery.py](../src/sitg_bot/bot/game_delivery.py), [lobby_delivery.py](../src/sitg_bot/bot/lobby_delivery.py), [chat_delivery.py](../src/sitg_bot/bot/chat_delivery.py) | Outbox presentation and Telegram message handling |
| [services/telegram_game.py](../src/sitg_bot/services/telegram_game.py), [chat.py](../src/sitg_bot/services/chat.py) | Authorized game views/actions, durable presentation state, participant chat |
| [bot/logging_config.py](../src/sitg_bot/bot/logging_config.py), [log_context.py](../src/sitg_bot/bot/log_context.py) | Redacted rotating logs and update/correlation context |

## Update handling and state

The bot connects to the authenticated application protocol and has no direct PostgreSQL
dependency. Handlers use `BotBackend` and gateway projections, not storage models.

Outer middleware runs correlation logging, durable update deduplication, private-chat identity
validation, player-context loading, locale selection, and error mapping. Identity must match
the update's `from_user`; group-chat identities are not accepted as private sessions.
Per-user event isolation prevents rapid input from bypassing an active conversation.

`/start`, `/menu`, `/help`, `/command_help`, `/cancel`, and `/language` support recovery and
registration. `/help` shows a short tutorial (choosing a tournament, joining or creating a
lobby, starting a game, answering questions) and is also sent once when a player completes registration;
`/command_help` lists every command with a description and the context where it applies.
Tournament menus expose **Registration link** to managers; active participants find the same
link in the tournament profile Mini App's general section while registration is open.
Shared `/start reg_…` links ask for registration confirmation; private links include a sharing warning
when requested. `/start join_…` offers registration to nonparticipants before lobby joining.
Both flows recheck registration availability. The player menu omits rating/history placeholders.
Real name, public nickname, locale, and Telegram visibility consent are separate choices.
Navigation and registration are recovered from backend state. Short-lived callback references
may expire; re-opening a destination must regenerate authorized controls.

Menus derive from capabilities and current context. Player and manager tournament selections
are independent. Back may change navigation without leaving a lobby; Quit tournament also
does not implicitly leave an active lobby. A creator's departure confirms cancellation.
Unknown tournament action descriptors are ignored and logged.

## Implemented interaction surfaces

- Player registration, profile settings (`/set` and buttons), mode switching, tournament
  discovery/registration/selection, information, invitation lobbies, readiness, observers,
  settings, packet selection, hybrid search, native SI gameplay, and the "My profile" Mini App
  button for player profiles.
- **Other... → Authors** opens the player author catalogue and question-performance profiles
  in the Mini App; list cards show only name, tournament count, and question count.
- In player tournament context, **Other... → Upload packet** starts the shared upload flow
  when community uploads are enabled. The prompt warns that published packets can only be
  modified or deleted by tournament managers; unavailable uploads return an error message.
- `/bug` reports: `/bug <description>` submits a bug report to the administrators; a bare
  `/bug` prompts for the description. Reports store the reporter, commentary, and timestamp,
  and notify every active administrator through the admin notification audience and the
  throttled Telegram alert.
- Manager token requests, inventory, token-backed tournament creation, anonymous appeal
  review, selected tournament management and profiles, and JSON/DOCX packet upload.
- Administrator credential authentication (`/admin`) and token-request decisions with
  optional commentary and receipts.
- Administrator moderation is opened through the **Management** keyboard button. Player
  cards provide Ban/Unban and suspicion review/clearance. Banned players receive
  "You have been banned! Reason: … You can still use your library" on every interaction
  except opening their packet library, which stays available in both the bot and the Mini App.
- Mini App buttons for tournament lists, ongoing games/lobbies ("Ongoing games"), the player profile ("My profile"), lobby
  packet/settings views, management, packet draft/editing flows,
  and administrator management (Tournaments, Authors, Players, Packets).

The player tournament context opens the tournament profile Mini App window through the
**Info** button (**Leaders** deep-links the same window on its leaders section), and the
manager tournament context opens it through the **Tournament profile** button. The lobby
menu offers readiness, start/search, leave, and a **Lobby info** button. Lobby info lists
the tournament, every player and observer with the lobby owner marked, global and
tournament ratings (when present), readiness, and the selected packets, followed by an
actor-bound **Lobby settings** Mini App link: any participant may view settings and
packets there, while changes remain owner-only. Starting a
lobby without selected packets proposes an
automatic assignment instead of refusing: packets playable for every member are picked,
preferring the least (but non-zero) fresh themes and adding packets until the theme count
is satisfied, then a Yes/No inline confirmation applies the selection and starts the game.

Nicknames in lobby rosters/readiness lists and game participant lists/scores link to player
profiles. Register a named Mini App through BotFather's `/newapp` with short name
`profiles` and URL matching `MINI_APP_BASE_URL`. The Main Mini App can remain disabled.
Links use `https://t.me/<bot>/profiles?startapp=player_<UUID hex>`, which opens `/players/{UUID}`.
Authentication and profile authorization still run normally. Single-action notices such as
joining or buzzing retain plain nicknames; Chairs have no profile link.
Without a valid packet the bot reports that the tournament does not contain one.
Readiness notices list every player's status with ✅/⏳ indicators instead of reporting a
single player.

The manager creation wizard confirms token consumption and supports revising previous inputs.
Upload checks tournament permission before accepting a bounded file; it reports validation,
counts, and authors, then offers authorized preview/edit, publish, or reject actions.
Manager packet modification uses the Mini App. Remaining placeholder destinations are tracked
in [planned features](future-work.md).

## Native SI delivery

Gameplay uses Telegram messages and controls; there is no game Mini App.

1. Assignment sends a Join button with the tournament, packets, and participants. Once
   players join, the bot announces participants and
   installs a persistent keyboard: `+` for buzzing, `||` for pause/resume, `!` for appeal.
2. Ordered events announce numbered themes and question value, then reveal text by editing a separate
   question message. Long questions use stable adjacent chunks.
3. Buzz hides all question chunks and prompts the answering player with the answer form.
   Other players receive a named buzz notice and see the submitted answer; everyone sees the verdict or timeout.
   Rejected actions explain the current restriction, including another player's answer,
   a spent attempt, paused play, or an expired question/appeal. Server rejections cover races.
4. A wrong answer restores the previous reveal position. Question completion fully reveals
   the text and sends answer, commentary, and author separately.
5. Results show shared places, score, and points before penalties. Opponent reputation votes
   have independent controls; reports use explicit confirmation and receipts.
   Each player's settled global and, when applicable, tournament rating appears with
   before/after values and a signed change. Pending settlement is indicated; finalization
   updates the same results message with the recorded ratings.

Scores are displayed from highest to lowest. `/themes` lists the game's themes during active
play, only after the initial theme reveal and only for rulesets that announce themes.
`/score`, `/players`, and `/results` provide private projections. `/answer` reopens an answer
prompt. `/pause`, `/resume`, `/appeal`, `/escalate yes|no`, and `/commentary` supplement
buttons. With multiple eligible answers, appeal selection lists each answer and offers
buttons to credit the player's rejected answer or reject another credited answer. A single
eligible target is submitted automatically. Choice buttons retain their original question
and answer identities across restarts; stale choices are refused. Inline vote messages
update totals against a fixed electorate. Manager Appeals and `/appeals` show anonymous tickets.

The service rechecks membership, pinned SI capabilities, and question/appeal identity under
the game lock. Persisted prompts keep their original target across restarts, so an old reply
cannot become an answer to a later question. Public views use platform player IDs; username
links require consent and a shared completed game.

## Recovery, exit, and cleanup

`telegram_game_views` stores the ordered event cursor, message/prompt ledger, and dismissal
state. The game-event consumer coalesces adjacent reveal events only, preserving lifecycle
boundaries. Manual and outbox sends share per-chat locking and durable message identities.

`/abandon` requires confirmation. Completed-game `/quit` restores menu context and stops
delivery. Ordinary menu commands do not reopen historical games. Explicit `/reconnect`
restores an abandoned running game until a later game assignment forfeits that right.

Exit queues durable best-effort deletion of tracked inputs and gameplay messages. Cleanup
retains the old presentation session's message IDs, so retries cannot delete a reconnected
session. Telegram deletion limits still apply.

Connected adapters maintain presence for their joined players during pause handling; blocked
chat delivery clears presence. Game lifecycle and termination rules remain in
[game execution](game-rulesets.md) and [data rules](data-rules.md).

## Participant chat

Unhandled text and supported media relay to other current lobby/game participants, independent
of menu or mode. Commands, menu actions, and active conversations take precedence; token
commentary is never broadcast. Answer/commentary prompts also accept plain text.

`/whisper` lists public nicknames and platform IDs. `/whisper "Nickname" message` or an ID
targets one participant; duplicate nicknames require an ID. Omitting the message targets the
next text/media; captions may contain the command. `/cancel` clears that prompt.

Media relay uses Telegram `copyMessage`; file bytes do not pass through the game server.
Supported categories include photos, video, animation, audio, voice/video notes, documents,
stickers, contacts, locations, venues, dice, and copyable polls. Albums relay individually
while retaining the whisper target. Protected/service messages, paid media, invoices,
giveaways, and quiz polls lacking a known correct answer are unsupported.

Chat uses the outbox and rechecks both memberships at delivery. Abandoned players cannot send
or receive before reconnection; queued messages from an old game session are discarded.
Game chat and whisper receipts participate in the cleanup ledger, including late send results.

## Tournament chats

Classic tournaments with a preset composition offer per-match **Chats** from the player
tournament menu. Pressing it lists every round that has not been played, whose match
composition is already known, and whose round packet is assigned and playable; each room
message carries an actor-bound inline button that opens the chat. Opening a chat stores it
as the navigation context, removes all reply-keyboard buttons, and replays up to the last
50 stored messages through the outbox, so regular typing relays to the other players of
that match exactly like lobby and game chat (whispers included). Chats whose match has
been played or lost are closed automatically by the navigation snapshot.

While a chat is open, `/set_game_time` opens a Mini App scheduling window
(`/chats/{chat_id}/schedule`) where any participant sets or removes the shared, advisory
game time; the time applies to all participants, and setting or removing it posts a system
message to the chat and schedules player-role reminders 24 hours and one hour before the
time. `/quit` clears all chat messages for the player (cleanup ledger and best-effort
deletion) and restores the tournament context, like quitting a game. New messages for a
closed chat produce a single "new messages in chat" notification until the chat is opened
again. See [tournaments](tournaments.md) for the underlying match-chat rules.

## Extending and verifying

Add localized copy and presenters alongside handlers; do not put database queries or gameplay
rules in rendering. Preserve input precedence, opaque actor-bound callbacks, and stale target
rejection. Authentication material and message contents must stay out of logs.

Targeted tests are [test_telegram_frontend.py](../tests/unit/test_telegram_frontend.py),
[test_telegram_gameplay.py](../tests/unit/test_telegram_gameplay.py),
[test_participant_chat.py](../tests/unit/test_participant_chat.py), and delivery/logging unit
tests. PostgreSQL recovery and chat tests are in [tests/integration](../tests/integration).
Live Telegram rendering, media, and cleanup still need manual validation after affected changes.
