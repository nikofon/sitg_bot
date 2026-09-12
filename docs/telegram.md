# Telegram Adapter

[Technical index](architecture.md) · [Application contract](application.md) ·
[Configuration](database-operations.md)

## Source map

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

`/start`, `/menu`, `/help`, `/cancel`, and `/language` support recovery and registration.
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
  settings, packet selection, hybrid search, native SI gameplay, and player profile links
  (`/profile player_id` or `/profile @username`, plus the "My profile" button).
- Manager token requests, inventory, token-backed tournament creation, anonymous appeal
  review, selected tournament management/settings, and JSON/DOCX packet upload.
- Administrator credential authentication (`/admin`) and token-request decisions with
  optional commentary and receipts.
- Mini App buttons for tournament lists, the player profile ("My profile"), lobby
  packet/settings views, manager settings, management, and packet draft/editing flows.

The manager creation wizard confirms token consumption and supports revising previous inputs.
Upload checks tournament permission before accepting a bounded file; it reports validation,
counts, and authors, then offers authorized preview/edit, publish, or reject actions.
Manager packet modification uses the Mini App. Remaining placeholder destinations are tracked
in [planned features](future-work.md).

## Native SI delivery

Gameplay uses Telegram messages and controls; there is no game Mini App.

1. Assignment sends a Join button. Once players join, the bot announces participants and
   installs a persistent keyboard: `+` for buzzing, `||` for pause/resume, `!` for appeal.
2. Ordered events announce themes and question value, then reveal text by editing a separate
   question message. Long questions use stable adjacent chunks.
3. Buzz hides all question chunks and prompts the answering player with the answer form.
   Other players see the submitted answer; everyone sees the verdict or timeout.
4. A wrong answer restores the previous reveal position. Question completion fully reveals
   the text and sends answer, commentary, and author separately.
5. Results show shared places, score, and points before penalties. Opponent reputation votes
   have independent controls; reports use explicit confirmation and receipts.

`/score`, `/players`, and `/results` provide private projections. `/answer` reopens an answer
prompt. `/pause`, `/resume`, `/appeal`, `/escalate yes|no`, and `/commentary` supplement
buttons. Appeal target selection uses a numbered prompt when necessary. Inline vote messages
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

## Extending and verifying

Add localized copy and presenters alongside handlers; do not put database queries or gameplay
rules in rendering. Preserve input precedence, opaque actor-bound callbacks, and stale target
rejection. Authentication material and message contents must stay out of logs.

Targeted tests are [test_telegram_frontend.py](../tests/unit/test_telegram_frontend.py),
[test_telegram_gameplay.py](../tests/unit/test_telegram_gameplay.py),
[test_participant_chat.py](../tests/unit/test_participant_chat.py), and delivery/logging unit
tests. PostgreSQL recovery and chat tests are in [tests/integration](../tests/integration).
Live Telegram rendering, media, and cleanup still need manual validation after affected changes.
