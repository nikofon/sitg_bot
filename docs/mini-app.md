# Mini App and HTTP Adapter

[Technical index](architecture.md) · [Setup](database-operations.md) · [Telegram](telegram.md)

## Source map

| Path | Responsibility |
| --- | --- |
| [web/src/app.ts](../web/src/app.ts), [main.ts](../web/src/main.ts) | Screen rendering and startup |
| [web/src/routing](../web/src/routing) | Route parsing and navigation |
| [web/src/api](../web/src/api) | Typed payloads, credentialed requests, stable errors |
| [web/src/platform](../web/src/platform) | Telegram chrome and local development harness |
| [web/src/ui](../web/src/ui), [state](../web/src/state) | DOM helpers, packet cards, filters and filter persistence |
| [web/src/i18n](../web/src/i18n), [styles.css](../web/src/styles.css) | Russian/English catalogs and responsive layout |
| [miniapp_http.py](../src/sitg_bot/miniapp_http.py) | aiohttp routes, session resolution, gateway translation, static files |
| [services/miniapp_auth.py](../src/sitg_bot/services/miniapp_auth.py), [launch_references.py](../src/sitg_bot/services/launch_references.py) | Telegram signature validation, sessions, CSRF, actor-bound launch targets |

One TypeScript/Vite app shares authentication, navigation, localization, and Telegram chrome
across routes. It uses direct DOM rendering, not a component framework. Production assets
are served by the application server from `web/dist`.
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

The adapter resolves the principal from server-side session state on every request and calls
the typed gateway. Requested `role` is a view selector, never a grant of manager/admin rights.
Exact-origin CORS, CSP/security headers, and per-action authorization remain enforced.
Stable error codes are localized; server exception text is neither rendered nor logged.

## Implemented screens

**Tournaments:** public and membership discovery, player/manager selection, full information,
and registration. Queries support phase, relationship, registration status, type, ruleset,
language, text search, ordering, and cursor pagination. Default ordering is `starts_asc`;
alternatives are `starts_desc`, `name_asc`, and `name_desc`.

**Lobbies:** overview, packet selection, and settings. The overview shows current selections
and validation warnings. Packet cards show author/year metadata, shared fresh-theme counts,
and playability for all players; observers do not affect freshness/playability. Selected cards
come first. Text and inclusive packet/publication year filters survive refresh. Mutations
use current capabilities and versions; ordered lobby events trigger refresh.

**Manager settings:** tournament metadata, pre-finalization type/ruleset, named multi-currency
pricing plans, registration/schedule, policies, ruleset defaults, mutability grants, and
management records. Registration has a schedule enable switch and a current-availability
checkbox; changing current availability applies a manual override. Entered dates use the device timezone.
Authors can be searched, selected, removed, or registered. Typed editors
replace raw JSON inputs. Ruleset rating weight is omitted and protected server-side.
Stale saves reload current state; setup finalization requires confirmation.
Settings and the Management General section provide buttons to switch between these views.

**Tournament management:** General, Registrations, Packet accessibility, and Packet management,
with sections derived from the tournament type. Supports setup finalization, manual
registration availability, completion, pending-registration decisions, and per-player or
all-player packet rights.
General includes **Start tournament** for Ladder; Classic stage-start buttons start the
tournament internally. Planned start dates send managers a reminder instead of starting play.
Classic adds stage start buttons, first-stage/play-off round cards with packet switches and
start deadlines, standings, and automatic/manual seeding. Its general packet-access table
contains only read rights; stage types and first-stage scoring are configured in Settings.
Round discovery/play switches display inherited packet defaults or explicit overrides and
stay disabled with a warning until their stage starts. The all-player access row displays
assignment defaults even when there are no participants yet.

**Player profiles:** per-ruleset public profiles at `/players/{player_id}`, opened from the
bot's player-mode "My profile" reply-keyboard button (own profile) or participant links on
game cards. A ruleset dropdown
lists only rulesets with at least one settled result. Each view shows the global ruleset rating
with a recent-history graph, win rate with place distribution (places 1–4, shared places,
worse), SI per-question-value correct/incorrect counts (custom tournament scales are mapped
onto canonical 10–50 values), and recent game cards with tournament name, stage placeholder,
participants, scores, and places. Private tournament names are replaced with a neutral label
for viewers without membership, manager, or admin access. Real names and non-public Telegram
usernames are visible only to the player themself or platform administrators. The
`/players/{player_id}/games/{game_id}` sub-view shows per-theme answer grids (value columns ×
participant rows, green/red/neutral marks) with theme pagination and a back button; it never
exposes theme names, question text, or answers.

**Packets:** draft preview/edit, author association/creation, publish/reject, assignment
retirement, version release, and correction/substitution editing. Published fields stay locked
until an edit classification is selected; save validates actual changes atomically.
See [packet administration](packet-administration.md) for identity and propagation rules.

**Library:** readable packets and all managed tournament packets, grouped by adopted version.
Opened from the player menu only; managed tournament packets remain available in player mode.
Cards link visible tournament profiles and filter by packet/tournament name or slug, author,
and packet/publication years. View opens ruleset-defined pages (SI themes), with a dropdown
and numbered navigation. Download queues a DOCX in Telegram. Both actions recheck access
and request confirmation before burning fresh content.

**Admin suspicion ledger:** `/admin/suspicion`, opened from the admin menu button and
authorized for platform administrators only. Cards list player name, ID, current suspicion,
rating and completed games per ruleset, and reports per category, ordered by suspicion;
banned players are excluded. `Inspect` loads every event that increased the player's
suspicion with linked evidence; `Clear suspicion` requests a review note and resets the
value to zero through the existing administrator clearance ledger.

Other shared routes may return placeholders. Native SI gameplay stays in Telegram.

## HTTP route families

All paths below start with `/api/miniapp`. Exact request/response fields live in
[api/types.ts](../web/src/api/types.ts), [api/client.ts](../web/src/api/client.ts), and
[application/contracts.py](../src/sitg_bot/application/contracts.py).

| Routes | Purpose |
| --- | --- |
| POST `/session`, `/session/refresh` | Authenticate and refresh |
| GET `/players/{player_id}`; GET `/players/{player_id}/games/{game_id}` | Public per-ruleset player profile statistics and per-theme game result grids (also served via route resolution for `/players/...` paths) |
| POST `/library/{version_id}/{view,download}` | Recheck read access, confirm exposure, read or queue DOCX delivery |
| GET `/routes/resolve?path=...` | Reauthorize and project a route |
| GET `/admin/suspicion/ledger`; GET `.../ledger/{player_id}/events`; POST `.../ledger/{player_id}/clear` | Admin suspicion ledger, inspection, and reviewed reset |
| GET `/tournaments/{id}`; POST `/{id}/register`, `/{id}/select` under `/tournaments` | Information, enrollment, navigation |
| GET `/lobbies/{ref}/events`; POST `/lobbies/{ref}/{command}` | Lobby refresh and mutations |
| `/manager/tournaments/{ref}/settings`, `/authors`, `/finalize` | Settings, author lookup/creation, finalization |
| `/manager/tournaments/{ref}/registration-availability`, `/registrations/{player_id}`, `/packet-access`, `/start`, `/complete` | Tournament management mutations |
| `/manager/tournaments/{ref}/classic` | Versioned stage configuration, seeding, round controls, and starts |
| `/manager/tournaments/{ref}/packets/{assignment_id}[/{command}]` | Published packet view and management |
| `/manager/packets/{ref}`, `/authors`, `/{decision}` | Draft view/edit, author lookup/creation, publish/reject |

Use the route registrations in `MiniAppHttpServer.application` as the complete HTTP inventory;
the table groups endpoints rather than duplicating their schemas.

## Development and verification

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
production source maps disabled. Add routes through the shared shell and permission-aware
backend projections rather than separate apps.

Browser tests are colocated `*.test.ts`; HTTP and auth tests are
[test_miniapp_http.py](../tests/unit/test_miniapp_http.py) and
[test_telegram_miniapp_auth.py](../tests/unit/test_telegram_miniapp_auth.py).
