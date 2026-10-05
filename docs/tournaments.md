# Tournament Model

[Technical index](architecture.md) · [Console usage](console-commands.md)

## Source map

- [services/tournaments.py](../src/sitg_bot/services/tournaments.py): settings, registration,
  roles, policy, and access.
- [services/classic.py](../src/sitg_bot/services/classic.py): Classic stages, prescribed games,
  standings, deadlines, and round entitlements. [domain/classic.py](../src/sitg_bot/domain/classic.py)
  owns scheme validation, balanced seeding, point aggregation, and play-off final places.
- [services/tournament_chats.py](../src/sitg_bot/services/tournament_chats.py): per-match
  Classic chats with replayed history, relay delivery, closed-chat notifications, and the
  shared advisory game time with reminders.
- [services/tournament_profiles.py](../src/sitg_bot/services/tournament_profiles.py): read-only
  tournament profile projections (general details, registrations, participants, games, and
  leaders) served to the Mini App through `tournaments.profile.get.v1`.
- [services/token_requests.py](../src/sitg_bot/services/token_requests.py): creation-token
  requests, rulings, and inventory; shared identity contracts are in [application.md](application.md).
- [storage/models.py](../src/sitg_bot/storage/models.py): tournament, version, membership,
  management, requirement, token, and packet-access records.
- [test_tournaments.py](../tests/unit/test_tournaments.py) and
  [test_lobby_architecture.py](../tests/integration/test_lobby_architecture.py): behavior and
  transactional integration coverage.

## Ownership and roles

A packet belongs to one or more tournaments, with independent assignments controlling access
to shared immutable versions. Drafts retain their creation context and intended assignments.
Lobbies, games, results, and rating events each belong to one tournament.

- **Administrators** authorize tournament creation and perform platform-wide moderation.
- **Managers** run a tournament, potentially together.
- **Players** enroll and play.

Administrator roles are independent from tournament roles. Within one tournament,
active manager and player roles are mutually exclusive: managers have access to all assigned
packets and therefore cannot discover, select, register for, or play that tournament. Every
packet version a manager can read through their role — published into, assigned to, or edited
in their tournament — is permanently burnt for them, and granting a manager role retroactively
burns the versions currently assigned to that tournament. Removing a role or an assignment
never resets burns.

Platform administrators moderate tournaments through the Management Mini App. Halt applies
only after an actual start and before completion. It prevents new game assignments, uploads
and publication, tournament edits, and new managers. Existing games may finish, and existing
library entitlements remain readable. Resume removes the halt restrictions.

Abolish is permanent: the tournament finishes, registration closes, and library access through
its assignments is revoked even for tournament managers. Shared packets remain accessible
through other tournaments. Existing games may finish with local results but produce no further
global ruleset rating changes. Prior global deltas are reversed with append-only correction
entries; original results and ledgers remain intact. Abolished games no longer contribute to
global rating confidence. All three actions require explicit warning confirmation and the
current tournament settings version. Settlement and moderation serialize on the tournament row.

Tournament discovery is independent from membership. A finalized public tournament may be listed
by every authenticated player, while a private tournament appears only in the membership
listing of its participants. Public visibility does not grant a right to enroll or play;
enrollment is governed independently by tournament policy and manager actions.

Registration and participation are separate membership states. A player may be invited,
registered, approved, active, rejected, suspended, or left. Public tournaments accept
self-registration while the effective registration window is open. A private tournament
requires an invitation. Managers approve registrations unless `auto_approve_registrations`
is enabled (default `false`); requirements still apply. Approval
immediately activates a player in an open-ended Ladder; in a finite tournament it moves the
player to `approved` until managers close registration and finalize an explicit participant
list. Finalization activates selected approved players and rejects the remaining applicants.
Only active participants can enter lobbies or use tournament packet play rights.
An optional positive `maximum_participants` policy narrows any limit supplied by the
tournament type and is enforced when the finite participant list is finalized.

Managers and participants can share **Registration links**, granting private-tournament
invitations with a sharing warning. The bot's manager menu shows the link; the tournament
profile Mini App general section shows it to everyone while registration is open.
Yes/No confirmation rechecks registration availability.
Lobby links offer registration to nonparticipants; automatically activated players then join.
See [Telegram](telegram.md) for bot flows.

### Registration requirements

Managers may add any number of active requirements. They compose with AND semantics: every
requirement must pass when the player submits registration.
Settings provides add/remove rows with a requirement type, target tournament or logical packet
UUID, and optional rejection message. Saving applies all rows atomically with the settings
version check; unchanged requirements retain their audit identities.

- `has-played-tournament` requires the player to have participated in a disclosed game in
  the referenced other tournament.
- `has-not-played-tournament` requires the opposite.
- `has-not-seen-packet` fails when any exposure claim sourced from a version of the referenced
  logical packet is burnt for that player. Reservations do not count as seen, and every
  version of the logical packet is covered.

Gameplay participation is proven by a game-participant fact plus burnt exposure in that
game, rather than by membership alone. On failure, the membership becomes `rejected`, all
failed reasons are returned to the player, and the evaluated requirement IDs and reasons are
stored in an append-only registration-attempt record. Managers may supply a custom failure
message or use the generated reason. A rejected invitee retains the ability to retry while
registration remains open. Adding or removing a requirement affects later attempts only; it
does not retroactively remove registered, approved, or active players.

Exposure claims record their source packet version independently of ruleset-specific claim
namespaces. Source packet provenance is required for every claim, including permanent
authorship and uploader burns outside games.

Tournaments carry a BCP 47 language tag (`und` means unspecified), explicit authors, and
packet-derived authors. Payment is `free`, `one-time`, or `per-stage`. Paid tournaments have
named pricing plans with positive two-decimal prices in distinct three-letter ISO currencies.
Free tournaments have no pricing plans.

Registration start/end, planned start/finish, and actual start/finish are separate
timezone-aware facts. Without a manual override, the enabled registration flag is bounded
by its dates (the end is enforced when late registrations are disabled). The manager's
explicit open/closed override takes precedence over that window. Settings exposes only the
date-based registration switch. Management shows current availability; changing it disables
scheduling and sets availability immediately. Re-enabling scheduling clears the manual override.
Dates use the device timezone in the Mini App.
The creation wizard omits dates for both types; managers set them later in Settings. Finite
tournaments require registration end at setup finalization; unfinalized drafts may omit it.
Tournament start and planned finish dates are optional guidance. Ladder managers use
**Start tournament** in Management → General; starting either Classic stage also starts its
tournament. Until then, lobby assembly is closed regardless of dates. Starting Ladder does
not change registration. Completion records actual finish and closes registration.
Listings classify unstarted tournaments as future, manually started ones as ongoing, and
completed/archived ones as past.

When a planned start arrives, a durable job sends each current manager a notification to
start manually. Started or closed tournaments are skipped. Reminders survive restarts and
are deduplicated per tournament, scheduled date, and recipient; rescheduling can trigger a
new reminder. The migration preserves tournaments with existing stages or games as started.

Creation requires a confirmation token issued by an administrator. Tokens are hashed,
expiring, single-use, optionally bound to a creator, and revocable before use. Successful
confirmation creates the tournament and makes its creator its first manager, without enrolling
them as a player. A newly created tournament remains unfinalized regardless of its public/private
visibility. During that setup phase, it is visible only to its managers and players cannot
discover, select, register for, or start play in it. A manager explicitly finalizes setup when
it is ready. Finalization is permanent: it makes the tournament player-accessible according to
its visibility and membership rules and locks the tournament type and game ruleset. Metadata,
pricing, registration, schedule, policy, defaults, packet assignments, requirements, memberships,
and manager roles remain manageable afterward. Settings carry an optimistic concurrency version
so one manager cannot silently overwrite another manager's newer edit.

## Tournament types and game rulesets

A tournament type and a game ruleset are separate choices. The tournament type defines
organization, progression, scheduling, and rating behavior. The game ruleset defines
packet structure and how an individual game is played. Both are versioned and
extensible.

Managers select the game ruleset when creating a tournament and may change it, together with the
tournament type, while setup is unfinalized. Both choices become immutable at finalization.
Managers configure the ruleset's exposed parameters. Tournament types may restrict compatible game rulesets or parameter
ranges. The game-host contract and the initial SI ruleset are defined in
[game-rulesets.md](game-rulesets.md).

The database registers two type descriptors:

- **Ladder**: open-ended, rated, SI-compatible, limited to 1–12 players per game, and
  capable of hybrid matchmaking. Hybrid matchmaking remains disabled until that
  tournament's managers enable it in policy.
- **Classic**: scheduled, finite-packet, SI-compatible, limited to 1–12 players per game,
  and unable to use hybrid matchmaking. Its prescribed games follow a group, Swiss, solo quiz,
  play-off, or double-elimination scheme. Invitation lobbies must match the prescribed roster.

### Classic competition

Settings provides optional first (groups, Swiss, solo quiz) and play-off stages. Explicitly
starting a configured stage uses the finalized participant list, closes registration, locks its type,
scheme, scoring and seeds, and opens lobby assembly. Unstarted play-offs remain editable.
Managers complete tournaments manually. Round permissions, rosters and deadlines govern play.

The CSV-derived library contains nine group schedules, Top-8/16/32/64 play-offs and Top-8/16/32
double elimination. Top-32 DE round 4 references to round 3 games 5–6 places 1–2 are corrected
to games 1–2 places 3–4. Validated advancement references are persisted at stage start.

Group schemes fix group size and games per player; group count follows the finalized participant
count. Management → First round seeding selects the stage, then a group, opening play-off game,
or Swiss seat. The searchable picker sorts names alphabetically, unassigned players first, and
labels occupied seats. Assigning an already seated player swaps seats. Solo quiz needs no seeding.

Automatic **Best vs best** orders players by descending global ruleset rating (unrated: 1000;
player ID breaks ties), putting adjacent players together. **Average** uses snake distribution
and improving swaps to balance group/game mean ratings. Random seeding shuffles the roster.
Manual, automatic and random choices persist until changed; stage start revalidates eligibility.
Vacancies become **Chairs**. Seeds and stage configuration lock at start.

CSV export lists eligible names and IDs in columns A–B, leaves C blank, then uses a name/ID
column pair per group or opening game from D onward; Swiss has one seat-list pair. Row one holds
headers; following rows are seats. Keep headers and move names with IDs; IDs determine assignments.
Import validates the layout and requires every eligible player exactly once, rejecting unknown IDs,
duplicates and extra seats. Import previews changes; **Save manual seeding** persists them using
the tournament settings version. Blank name/ID pairs are vacancies. UTF-8, quoted fields and
multiline names are supported; exported names that resemble spreadsheet formulas are escaped.

Swiss managers choose a positive round count and 2–12 players per game; at least two confirmed
players are required. The editable list has one seat per participant; stage start appends Chairs
to fill the last game. The opening round uses consecutive seats. Later rounds order players by
accumulated points, then initial seed.
Deterministic greedy pairing prefers fewer previous encounters, then smaller point differences;
up to eight pair-swap passes improve that objective. Repeats are allowed when this search cannot
avoid them. Only round one is paired at start; subsequent pairings are persisted after all
previous games finalize, including appeals/deadline results. Tournament locking and match
uniqueness prevent duplicate pairings. Completion requires every configured round.

Chairs automatically join, stay at zero, occupy places and can advance, but cannot act and
do not affect ratings, suspicion or qualification standings. They beat negative-scoring humans.
All-Chair games resolve automatically.

Group/Swiss games award configurable place points (default 4, 3, 2, 1) plus score × coefficient
(default 0.02); shared places average occupied awards. Standings sum these across games/groups.
Quiz entrants play once solo and rank by score. Group/quiz ties compare summed unpenalized score,
then correct-answer counts at descending values. Remaining ties use a persisted random seed.

Swiss ties compare the sum of encountered opponents' final positions by stage points, lower
being better. Equal points share the mean occupied position before applying this tiebreak,
avoiding circular ranking. Every encounter counts, including repeats; Chairs are excluded.
Remaining ties use the persisted random seed. The displayed sum is provisional until all
results finalize; final standings and play-off qualification use the same calculation.

Play-offs take top first-stage finishers up to capacity, padding with Chairs. Without a first
stage, managers seed automatically, randomly or manually and must reduce the participant list or enlarge the
scheme. Advancement uses game ranking; play-off settings omit first-stage scoring parameters.

Each round uses one published packet, never reused within the tournament. Discovery/play switches
inherit packet member defaults (tournament defaults before selection) unless overridden. Migration
preserves started-round choices; unstarted rounds inherit defaults. Unstarted stages show disabled
switches and a warning; access requires stage start. Packets/deadlines can be prepared beforehand;
packets lock after a human game starts or resolves. General packet access governs reading only,
subject to release rules. Packet selection announces the roster/solo restriction. Readiness and
assignment recheck exact human membership, access and deadline. Each match permits one assignment;
failed/cancelled attempts may retry subject to exposure rules. **Game statuses** shows game details.

Deadlines cut off **game starts** (successful lobby assignment). Assigned games retain normal
join deadlines and finish normally. Unstarted games receive random places, zero scores and place
awards. Durable reconciliation records results once, resolves dependencies and completes stages
after appeals/results finalize. Retries/restarts never reroll results.

#### Classic match chats

Unplayed matches with known rosters and playable assigned packets own persistent chats for human
seats. Transactional messages use the outbox. Chats close permanently on assignment, results or
stage completion. Players may set/replace/remove a shared advisory `planned_at`, posting a system
message and scheduling player reminders 24 hours and one hour beforehand. See [Telegram](telegram.md).

## Tournament settings

The `packet_notifications_enabled` policy defaults to `true` and controls player notices
when assigned packets become playable. Managers can toggle it in Settings. Disabling it
stops new notices without cancelling queued ones. Re-enabling announces currently eligible
packets that have not yet been announced; existing per-player deduplication still applies.

Managers choose defaults and per-parameter player mutability. Effective parameters must satisfy
tournament type, ruleset, and technical limits. Assignment snapshots preserve them for existing
games; changes to assembling lobbies clear readiness.

Classic SI games always use every theme in the round packet. Theme count is fixed for
managers and players, including existing tournaments; unavailable themes block the game
instead of reducing its length. Ladder theme count remains configurable. Ladder defaults
and lobby settings may also store the `max` sentinel: the lobby then resolves it to every
theme of its selected packets before validation and assignment, and unresolved themes
block the game like Classic.

The manager and lobby settings screens group parameters into categories (number of
players, theme count, question values, question appearance, message timings, and other
parameters), each rendered in its own bordered card. The question values category holds
`question_values` and `minus_multiplier`; question appearance holds the token size and
token delay. The theme-count category offers a checkbox that selects the `max` sentinel.

`minimum_players` and `maximum_players` default to four, including existing tournaments.
Each has its own mutability grant. They must satisfy `1 <= minimum <= maximum <= 12`;
the maximum controls lobby capacity and both limits govern game starts. Classic uses its
prescribed roster, including solo games and Chairs, instead of these limits.

The runtime currently interprets policies for:

- hybrid matchmaking enablement, only when the tournament type supports it;
- rating enablement when the tournament type is rated;
- an optional positive `maximum_participants` limit;
- `packets_per_lobby` (default `one`): `one` restricts every lobby to a single selected
  packet per game, `any` permits the tournament type's full packet range. The restriction
  composes with the type limits by intersection;
- `auto_approve_registrations` (default `false`), which approves new qualifying registrations;
- `library_viewing_rule_default` (default `after-play`): copied to new packet assignments.
  Managers choose `never`, `after-play`, or `anytime` in Packet management. Rules govern
  viewing readable packets in the library, never playing. They are tournament-scoped;
  changing the default affects future uploads only;
- `packets_discoverable_by_default` (default `true`), `packets_playable_by_default`
  (default `false`), and `packets_readable_by_default` (default `false`): independent boolean
  access defaults for newly uploaded packets. Publication copies each destination tournament's
  current values to the packet assignment for all participating players, including later
  participants. Existing tournaments with no explicit values use these defaults. Policy edits
  do not change existing assignments or per-player grants; readable does not enable playable,
  and content release rules still apply;
- a positive `ruleset_rating_weight`, defaulting to `1`, which scales this tournament's
  contribution to ruleset-wide rating without changing tournament rating or confidence;
- `observing`, defaulting to `forbidden`: `unlimited` permits observing fresh or burnt
  assigned content, `burnt-only` permits a player to observe only when every assigned claim
  is already burnt for that player, and `forbidden` disables observing. Active tournament
  managers bypass this policy (and the participant-membership requirement) for games in
  tournaments they manage; fresh-content exposure claims still apply to them;
- appeal voting rule, such as unanimity or majority;
- whether appeal escalation is available and its deadlines.

The appeal backend recognizes `appeal_voting_rule` (`majority` by default or
`unanimous`), `appeal_vote_timeout_seconds` (default `60`),
`appeal_escalation_enabled` (default `false`),
`appeal_escalation_decision_timeout_seconds` (default `30`),
`appeal_commentary_timeout_seconds` (default `300`), and
`appeal_ticket_expiry_seconds` (default `86400`, or 24 hours). Durations must be finite
and positive. Escalation depends only on the snapshotted tournament appeal policy.

Policy changes are versioned. A game records the policy version under which it was
assigned, and results record the policy relevant to their interpretation.

A durable job checks packet availability every 30 seconds, notifying active participants once
per tournament packet with discoverable, playable, fresh content. Classic adds opponents,
Chairs, and start deadlines, excluding unstarted stages and expired/resolved matches.
Transactional notifications and Telegram alerts survive restarts without duplicates.

`hybrid_matchmaking_enabled` is a boolean and defaults to `false`. Managers may set it to
`true` for Ladder tournaments. Enabling it for Classic or any other type without the
`supports_hybrid_matchmaking` capability is rejected. Disabling it also stops searches in
assembling lobbies; direct invitation remains available.

Tournament cards show the optional plain-text description (at most 2000 characters),
editable with other metadata. Manager settings also carry optional organizer contacts
(plain text, at most 2000 characters) and a tournament channel (a link, at most 500
characters); both may be empty, are editable in tournament settings, and the profile
general section shows them when set.

## Universal and technical constraints

The universal gameplay content rule is: **a player's reserved or burnt canonical claim
must never be assigned again**. It applies across tournament and ruleset assignments and
across the player's history. Tournament settings cannot disable it. Corrections preserve
their logical claims; substitutions can introduce new logical identities.

Platform technical limits also apply to every tournament. At most 12 players may take
part in one game. Rulesets may narrow that limit or impose content-specific limits. These
limits are implementation constraints, not tournament policy, and may be revised as the
platform evolves.

### Current SI application

SI assignment reserves each selected logical theme and its logical questions for every
participant. Theme-list disclosure permanently burns those claims; cancellation before any
disclosure releases them. SI games may only be assigned when all claims are fresh, and SI
currently supports at most 128 themes per game. The full SI contract is in
[game-rulesets.md](game-rulesets.md).

## Packet availability and visibility

### Ladder subscription cards

Management → **Subscription cards** (**Абонементы**) creates named templates with a positive
packet count or unlimited allowance and Yes/No/Default discovery, reading, and play rights.
The searchable participant picker lists assigned instances, remaining allowances, and revocation
controls. Managers can assign multiple instances, including repeated copies of one template.
Each assignment sends the player a notification naming the card, tournament, and packet allowance.

Every newly published or newly assigned logical packet consumes one unit from each active
participant's oldest unrevoked, unexhausted instance. Unlimited instances remain first until
revoked. Previewing, correcting, substituting, or reactivating an existing assignment consumes
nothing. Assignment, revocation, and consumption serialize on the tournament row.

Explicit card choices become ordinary per-packet overrides, including denials. Default preserves
existing individual rights, otherwise inheriting assignment defaults. Managers can subsequently
change packet access normally. Revocation affects future packets only. Subscription reading
rights do not bypass library viewing rules, release gates, or exposure restrictions.

Templates and instances are managed through `tournaments.manager.subscriptions.update.v1`
with manager authorization, an idempotency key, and the current tournament settings version.

### Access evaluation

Active participation does not grant access to every packet assigned to a tournament. Packet
access is a separate, explicit entitlement evaluated for a player or tournament role
within that tournament-packet assignment. At minimum, the model distinguishes:

- metadata discoverability, such as knowing that a round or packet exists;
- eligibility to select or be assigned the packet for a game;
- access to the packet's question content; and
- manager/editor access to drafts, validation reports, and revisions.

The access decision composes active membership, manager role, assignment-wide member flags,
and explicit per-player grants. Classic play/discovery rights instead come from the round
and its prescribed participants; general grants cannot bypass the schedule.

Authorization is checked whenever content is listed, previewed, downloaded, selected,
assigned, or administered; hiding a command in the interface is not sufficient.
Historical games retain their pinned packet revisions even if later access policy
changes, while post-game access to their full content follows a separately defined
tournament policy.

Each assignment has a **Library viewing rule**:

- `never` — **No library viewing**;
- `after-play` — **After playing**, requiring actual play in this tournament;
- `anytime` — **Before or after playing**.

These rules never grant or revoke playability or discoverability. Player-specific play
permission overrides the member default; an absent override inherits it. Reading still burns
canonical claims, preventing replay of seen content under the universal exposure rule.

Library eligibility is not sufficient for reading. The owner must also release the adopted
packet version for library viewing. Release is version-specific and global, while access is
tournament-assignment-specific. The readable flag controls library listing; it does not bypass
these rules. Managers can list and read every active assignment in their tournaments,
regardless of readable flags, viewing rules, or release status.
Tournament managers can release adopted packets through Packet management. The
`packets_released_by_default` policy releases confirmed uploads automatically and defaults to false;
it does not grant read eligibility. Removing a packet retires only its tournament assignment.

One logical packet may be assigned to many tournaments. Each assignment independently pins
or follows a version and owns its access defaults and player overrides. Exposure remains
global per player, so consuming content through one tournament prevents reassignment through
another.

Corrections update every assignment sharing the edited version. Substitutions branch only the
current tournament, preserve its rights, and notify the other tournaments' managers. Later
corrections do not overwrite independently substituted versions.

The exact post-game visibility timeline remains an open decision in
[future-work.md](future-work.md); ongoing-game observing is implemented.
