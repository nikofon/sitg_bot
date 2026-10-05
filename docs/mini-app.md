# Mini App and Browser HTTP Adapter

[Technical index](architecture.md) · [Setup](database-operations.md) · [Telegram](telegram.md)

## Source map

| Path | Responsibility |
| --- | --- |
| [web/src/app.ts](../web/src/app.ts), [main.ts](../web/src/main.ts) | Screen rendering and startup |
| [web/src/routing](../web/src/routing) | Route parsing and navigation |
| [web/src/api](../web/src/api) | Typed payloads, credentialed requests, stable errors |
| [web/src/platform](../web/src/platform) | Telegram chrome and local development harness |
| [web/src/ui](../web/src/ui), [state](../web/src/state) | DOM helpers, packet cards, tournament profile sections, filters, filter persistence, and message-flow setting previews |
| [web/src/i18n](../web/src/i18n), [styles.css](../web/src/styles.css) | Russian/English catalogs and responsive layout |
| [miniapp_http.py](../src/sitg_bot/miniapp_http.py) | aiohttp routes, session resolution, gateway translation, static files |
| [services/miniapp_auth.py](../src/sitg_bot/services/miniapp_auth.py), [launch_references.py](../src/sitg_bot/services/launch_references.py) | Telegram signature validation, sessions, CSRF, actor-bound launch targets |
| [services/tournament_profiles.py](../src/sitg_bot/services/tournament_profiles.py) | Read-only tournament profile projections for the Mini App window |

One TypeScript/Vite app shares authentication, navigation, localization, and Telegram chrome
across routes. It uses direct DOM rendering, not a component framework. Production assets
are served by the application server from `web/dist`. Descriptor editors for ruleset settings
that pace in-game messages embed a live preview of the message flow (`ui/setting-demo.ts`),
replayed through a `⏵` (U+23F5) button;
the lobby settings section lists changeable options before fixed ones. Ruleset settings are
grouped into categories (number of players, theme count, question values, question
appearance, message timings, other); each category renders in its own bordered card with an
alternating background tint. Multi-parameter categories use a dropdown with one visible input panel,
single-parameter categories show their input directly, and theme count offers a
"all themes from the selected packets" checkbox that submits the `max` sentinel. The author-link
window (`/authors/link`) lets a registered player search authors, submit a link request
with an optional note, and follow their own request statuses; the bot's player menu opens it.

Registered players open `/authors` through **Other... → Authors**. Like the administrator
catalogue, it supports name search and sorting, with filters retained when returning from
`/authors/{author_id}`. Cards expose only name, tournament count, and logical question count.
Profiles add aggregate SI performance and per-value statistics, without contact details,
linked players, tournament names, or question content. The read-only gateway operations are
`authors.catalogue.v1` and `authors.profile.v1`; the existing author-link picker is unchanged.
Metric definitions are in [statistics](data-and-statistics.md).

The tournament profile window (`/tournaments/{tournament_id}`, opened by the catalogue's
info action, the bot's player **Info** and manager **Tournament profile** buttons, and the
**Leaders** button that adds `?section=leaders`) shows five permission-aware sections:
general details and managers, registered
players with approval badges, the finalized participant list (classic only), games, and
leaders. The general section includes the tournament registration link (a
`t.me/<bot>?start=reg_…` URL resolved by the HTTP adapter) whenever registration is open.
Ladder games list the latest finalized games with per-player scores and links to
the same game examination as player profiles; classic games browse first-stage and play-off
stages with group and round selection. Ladder leaders order players by in-tournament rating;
classic leaders switch between first-stage standings and computed play-off final places,
where the last round keeps its in-game order and players eliminated in a multi-game round
share `survivors + position - 1.5`.

The chat schedule window (`/chats/{chat_id}/schedule`, opened by the bot's `/set_game_time`
inside an open tournament chat) shows the match, its participants, and the current planned
game time. Participants set a new time (replacing any previous one) or remove it; the time
is shared for the whole match and is advisory only. Mutations go through
`POST /api/miniapp/chats/{chat_id}/game-time` and post a system message to the chat.

## Independent website

The website is a separate project in `SITGBot-website`, with its own frontend source,
build, assets, and self-contained API/deployment documentation. `web/` contains only
the Telegram Mini App. The website uses the same backend application services over HTTP;
there are no shared frontend imports or build-time copies.

Configure its exact origins with `WEBSITE_ALLOWED_ORIGINS`. Website and Mini App hostnames
must differ; each session is bound to its origin and each cookie to its hostname.
The website domain serves its own assets and proxies `/api/` and `/auth/` to this listener.
Website login endpoints are disabled unless website origins are explicitly configured.
The Mini App session endpoint rejects website origins, and the listener will not serve
Mini App assets for a website Host. See [deployment configuration](database-operations.md).

Proxies must preserve the website Host (including its port): same-origin GET requests may
omit both Origin and Referer. In Vite, use `{ target, changeOrigin: false }` for `/api` and
`/auth`; the string target shorthand rewrites Host. API origin checks run before handlers;
missing, invalid, or unlisted origins return `403 origin_not_allowed`. Missing or expired
sessions still return `401 authentication_required`. Signing in cannot fix an origin rejection.

## Asset delivery

HTML and unversioned static files use `Cache-Control: no-cache`: browsers may store them
but must revalidate before reuse; unchanged files return 304. Content-hashed files under
`/assets/` use `public, max-age=31536000, immutable`. Missing assets return 404 instead of
the HTML shell. Authenticated API responses use `no-store`.
Bot menu links use a stable `_launch=1` transition value to bypass HTML cached before
these policies were introduced. It carries no identity or permissions and permits reuse
across launches. Existing messages keep their original URLs; request a new menu button.

For deployments, build in a staging directory, publish new hashed assets before replacing
`index.html`, and retain previous assets for the supported open-session window. Avoid
rebuilding directly into the live directory: Vite cleans the output directory by default.

## Authentication and request flow

1. Telegram supplies signed `initData`. `POST /api/miniapp/session` validates signature,
   age, bot environment, and exact origin, then creates an opaque short-lived session.
2. The server sets an HTTP-only session cookie and returns `csrf_token`, `expires_at`,
   and `locale`. Browser code never reads the cookie.
3. Route resolution reloads the caller and reauthorizes the requested resource. Opaque
   launch references bind sensitive lobby/manager/draft routes to their actor and expiry;
   possessing a route string is insufficient authorization.
4. Mutations use POST JSON with `X-CSRF-Token`, `X-Idempotency-Key`, and
   `X-Correlation-ID`. Queries include correlation metadata. Session refresh rotates CSRF.

For authenticated requests, the adapter resolves the principal from server-side session state and calls
the typed gateway. Requested `role` is a view selector, never a grant of manager/admin rights.
Exact-origin CORS, CSP/security headers, and per-action authorization remain enforced.
Stable error codes are localized; server exception text is neither rendered nor logged.

## Implemented screens

**Tournaments:** public and membership discovery, player/manager selection, full information,
and registration. Queries support phase, relationship, registration status, type, ruleset,
language, text search, ordering, and cursor pagination. Default ordering is `starts_asc`;
alternatives are `starts_desc`, `name_asc`, and `name_desc`.

**Lobbies:** overview, packet selection, and settings. The overview shows current selections
and validation warnings, with permission-aware Remove buttons for selected packets.
Packet cards show author/year metadata, shared fresh-theme counts,
and playability for all players; observers do not affect freshness/playability. Selected cards
come first. Text and inclusive packet/publication year filters survive refresh, and a sort
control reorders cards by fresh-theme count; the default mode sorts fresh-theme counts
ascending with zero-fresh packets last, and plain descending/default modes remain available.
Mutations use current capabilities and versions. Actions, ordered lobby events, and periodic
reconciliation refresh data in the background. Packet cards update in place, preserving
filters, sorting, focus, and the visible packet's scroll position where possible. Membership,
readiness, permissions, and packet availability still follow the server snapshot; unsaved
settings defer background updates. Navigation and terminal lobby states may replace the screen.

**Manager settings:** tournament metadata, pre-finalization type/ruleset, named multi-currency
pricing plans, registration/schedule, policies, ruleset defaults, mutability grants, and
registration requirements (type, target UUID, optional rejection message).
Registration settings contain the schedule switch and dates, using the
device timezone. Management's availability switch disables scheduling and applies a manual choice.
Authors can be searched, selected, removed, or registered. Typed editors
replace raw JSON inputs. Ruleset rating weight is omitted and protected server-side.
Stale saves reload current state; setup finalization requires confirmation.
Settings and the Management General section provide buttons to switch between these views.

**Tournament management:** General, Registrations, Packet accessibility, and Packet management,
with sections derived from the tournament type. Supports setup finalization, manual
registration availability, completion, pending-registration decisions, and per-player or
all-player packet rights. Packet management provides a **Library viewing rule** dropdown:
No library viewing, After playing, or Before or after playing. These conditions apply only
to viewing readable, released packets and never affect playability. The policy default
applies only to future uploads.
General includes **Start tournament** for Ladder; Classic stage-start buttons start the
tournament internally. Planned start dates send managers a reminder instead of starting play.
Classic adds stage start buttons, first-stage/play-off round cards with packet switches and
start deadlines, standings, and automatic/manual seeding. Its general packet-access table
contains only read rights and fits the screen; stage types and scoring are configured in Settings.
Round cards open game details through **Game statuses**.
Round discovery/play switches display inherited packet defaults or explicit overrides and
stay disabled with a warning until their stage starts. The all-player access row displays
assignment defaults even when there are no participants yet.

**Player profiles:** per-ruleset public profiles at `/players/{player_id}`, opened from the
bot's player-mode "My profile" reply-keyboard button (own profile) or participant links on
game cards. A ruleset dropdown
lists only rulesets with at least one settled result. Each view shows the global ruleset rating
with a recent-history graph, win rate with place distribution (1, 1.5, 2, 2.5, 3, 3.5, 4,
worse), SI per-question-value correct/incorrect counts (custom tournament scales are mapped
onto canonical 10–50 values), optional SI aggregates (`si_statistics`: the average
per-game normalized score, where each question's signed contribution is scaled by its actual
ruleset value onto the canonical scale, and accepted-buzz average delays from question opening
per canonical value, with missing timings excluded and real sample counts), and recent game
cards with tournament name, stage placeholder, participants, scores, places, the packets used
in the game (`packets`; names visible only to administrators, managers of that tournament,
game participants, and viewers of library-released versions — withheld names stay `null`,
`null` lists mean unavailable), and settlement rating snapshots per participant
(`global_rating_after` from the ruleset ledger; `tournament_rating_after` from the tournament
ledger, included only for viewers allowed to see that tournament). Private tournament names
are replaced with a neutral label
for viewers without membership, manager, or admin access. Real names and non-public Telegram
usernames are visible only to the player themself or platform administrators. The
`/players/{player_id}/games/{game_id}` sub-view shows per-theme answer grids (value columns ×
participant rows, green/red/neutral marks) with theme pagination and a back button; it never
exposes theme names, question text, or answers, and carries the same `packets` label list.

The `/players` directory query supports `order` values `name_asc`, `name_desc`, `rating_asc`,
`rating_desc`, `games_asc`, and `games_desc`; rating and game counts are the selected
ruleset's displayed values, ordering is applied to the complete filtered result before
pagination with stable name/ID tie-breakers, and the response advertises the supported names
in `supported_orders`.

**Packets:** draft preview/edit, author association/creation, publish/reject, assignment
retirement, version release, and correction/substitution editing. Packet management cards expose
packet IDs. **Add existing packet** looks up an ID from another managed tournament and shows
metadata before confirmation or cancellation. Published fields stay locked
until an edit classification is selected; save validates actual changes atomically.
See [packet administration](packet-administration.md) for identity and propagation rules.

**Library:** readable packets and all managed tournament packets, grouped by adopted version.
Opened from the player menu only; managed tournament packets remain available in player mode.
Cards link visible tournament profiles, show viewer-specific fresh/total theme counts, and
filter by packet/tournament name or slug, author, and packet/publication years; a sort control
orders cards by fresh-theme count ascending by default or by the default order. View opens
ruleset-defined pages (SI themes), with a dropdown
and numbered navigation. Download queues a DOCX in Telegram. Both actions recheck access
and request confirmation before burning fresh content.

**Ongoing games:** active lobbies and observable games from the viewer's tournaments
(opened from the player menu's "Ongoing games" button). Lobby cards show the tournament,
members with roles/readiness, capacity, search state, expiry, and selected packets with
fresh/total theme counts, plus join-as-player and join-as-observer buttons that reuse the
invitation join operation and switch Telegram to the lobby context. Game cards show the
tournament, status/phase, and participants with a watch-as-observer button; observing
rechecks membership, participation, policy, and fresh-content claims and requests
confirmation before burning fresh content. Joining sends the viewer back to the bot,
where the full prior game history is replayed durably. The scope covers tournaments the
viewer actively participates in **or manages**: managers see their managed tournaments'
lobbies and games (lobby join actions are replaced by a managing note, since managers
cannot participate) and may observe games regardless of the tournament's observing policy,
with fresh-content exposure claims still applying.

**Admin management:** `/admin/management`, opened by **Management** in the admin keyboard.
Every query and action requires an active platform administrator. Tournaments, Authors,
Players, Packets, and Ongoing games have searchable, sortable cards; filters persist per
section. Detailed metadata, settings, and related records are collapsed initially.
Tournament cards link profiles, offer confirmed Halt, Resume, and permanent Abolish actions
(see [tournaments](tournaments.md)), and carry a global rating weight slider (0.1–1 in 0.05
steps) whose green **V** button must be pressed to save the `ruleset_rating_weight` policy.
Authors show contributions and linked player data; Link accepts a player UUID or `@username`
and records an approved author link with permanent authorship exposure.

Players include banned accounts, private profile details, ratings, reports, and suspicion.
Ban/Unban and suspicion review/clearance replace the separate Telegram keyboard buttons.
Review reuses the evidence inspection; clearance requires a note and preserves the ledger.
The legacy `/admin/suspicion` route remains available for existing links.

Packets include every version regardless of discoverability, release, retirement, or tournament
access. View reuses the library reader and Download queues the same DOCX delivery. Fresh-content
confirmation permanently burns the packet for the admin, including existing reserved claims.
These administrative reads bypass normal library restrictions.

Ongoing games lists every game with `lobby` or `active` status across all tournaments, newest
first, with a count line above the cards. Cards are read-only: the tournament (linked to its
admin profile), host, and participants (linked to player profiles, with seat, score, readiness,
and chair role), status, phase, paused flag, ruleset, tournament type, and timing. The
tournament's settings snapshot and remaining execution details stay collapsed under the card's
details summary.

Other shared routes may return placeholders. Native SI gameplay stays in Telegram.

## HTTP route families

All paths below start with `/api/miniapp`. Exact request/response fields live in
[api/types.ts](../web/src/api/types.ts), [api/client.ts](../web/src/api/client.ts), and
[application/contracts.py](../src/sitg_bot/application/contracts.py).

| Routes | Purpose |
| --- | --- |
| POST `/session`, `/session/refresh` | Authenticate and refresh |
| GET `/authors`; POST `/authors/link` | Player author search and author-link request submission |
| GET `/routes/resolve?path=/players` | Public player directory; `search`, `order`, `offset`, `limit` |
| GET `/players/{player_id}`; GET `/players/{player_id}/games/{game_id}` | Public per-ruleset player profile statistics and per-theme game result grids (also served via route resolution for `/players/...` paths) |
| POST `/library/{version_id}/{view,download}` | Recheck read access, confirm exposure, read or queue DOCX delivery |
| GET `/routes/resolve?path=...` | Reauthorize and project a route |
| POST `/ongoing/lobbies/join`; POST `/ongoing/games/{game_id}/observe` | Join an open lobby by invitation code; observe an ongoing game with fresh-content confirmation |
| POST `/admin/management/{section}/{resource_id}/{command}` | Confirmed tournament moderation, tournament rating-weight updates, author links and author joins, link-request rulings, player bans, unrestricted packet reads/downloads |
| GET `/admin/suspicion/ledger`; GET `.../ledger/{player_id}/events`; POST `.../ledger/{player_id}/clear` | Admin suspicion ledger, inspection, and reviewed reset |
| GET `/tournaments/{id}`; POST `/{id}/register`, `/{id}/select` under `/tournaments` | Information, enrollment, navigation |
| GET route resolution of `/tournaments/{id}` | Tournament profile sections: general, registrations, participants, games, leaders |
| GET route resolution of `/chats/{chat_id}/schedule`; POST `/chats/{chat_id}/game-time` | Classic chat scheduling window and shared game-time mutations |
| GET `/lobbies/{ref}/events`; POST `/lobbies/{ref}/{command}` | Lobby refresh and mutations |
| `/manager/tournaments/{ref}/settings`, `/authors`, `/finalize` | Settings, author lookup/creation, finalization |
| `/manager/tournaments/{ref}/registration-availability`, `/registrations/{player_id}`, `/packet-access`, `/start`, `/complete` | Tournament management mutations |
| `/manager/tournaments/{ref}/classic` | Versioned stage configuration, seeding, round controls, and starts |
| `/manager/tournaments/{ref}/packets/{assignment_id}[/{command}]` | Published packet view and management |
| POST `/manager/tournaments/{ref}/existing-packets/{preview,add}` | Preview and confirm an existing packet assignment |
| `/manager/packets/{ref}`, `/authors`, `/{decision}` | Draft view/edit, author lookup/creation, publish/reject |

Use the route registrations in `MiniAppHttpServer.application` as the complete HTTP inventory;
the table groups endpoints rather than duplicating their schemas.

## Development and verification

Buttons generally need external margins so they do not touch adjacent controls or content.
Use consistent vertical and horizontal spacing, including in dialogs and when rows wrap.
An existing flex/grid `gap` may provide equivalent separation. Padding inside a button does
not replace external spacing; avoid horizontal margins that make full-width buttons overflow.

Buttons for important, impactful, or irreversible actions must stand out from ordinary
controls. Use yellow warning styling for cautionary actions such as Halt, and red danger
styling for destructive or restrictive actions such as Abolish and Ban. Keep labels explicit
and text legible; color supplements the label and any required confirmation.

Run from `web/`:

```bash
npm ci
npm run dev
npm test
npm run build
```

Vite proxies `/api` to `http://127.0.0.1:8080`; override with
`SITG_MINIAPP_API_TARGET`. The localhost/127.0.0.1 development harness accepts server-signed
test `initData` in untracked `web/.env.local` as `VITE_DEV_INIT_DATA`. It only works in a
development build and displays a warning. It still requires valid backend authentication;
there is no production fallback identity and no browser bot token.

Use text insertion for user/server content, preserve the security policy and lockfile, and keep
production source maps disabled. Add Mini App routes through its shell and permission-aware backend projections.
Keep website navigation and assets in the independent website project.

Browser tests are colocated `*.test.ts`; HTTP and auth tests are
[test_miniapp_http.py](../tests/unit/test_miniapp_http.py) and
[test_telegram_miniapp_auth.py](../tests/unit/test_telegram_miniapp_auth.py).
