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
  and unable to use hybrid matchmaking. Its prescribed games follow a group, solo quiz,
  play-off, or double-elimination scheme. Invitation lobbies must match the prescribed roster.

### Classic competition

Managers configure an optional first stage (groups or solo quiz) and optional play-off in
Settings. Each stage must be explicitly configured and started. Starting activates the approved
participants, closes registration, and permanently locks that stage's type, scheme, scoring,
and seeding. Managers can change the unstarted play-off while the first stage runs. Swiss is
not implemented. Overall tournament completion remains a manager action.
The first explicit Classic stage start records the tournament's actual start and opens
lobby assembly. Round packet permissions, prescribed rosters, and deadlines
continue to govern game starts.

The bundled scheme library contains nine group schedules, Top-8/16/32/64 play-offs, and
Top-8/16/32 double elimination. It transcribes the supplied CSVs. In Top-32 DE round 4,
references to round 3 games 5–6 places 1–2 are corrected to games 1–2 places 3–4.
Advancement references are validated and copied into persistent match records at stage start.

A group scheme fixes group size and games per player. There is no built-in limit on group
count. Automatic seeding uses ruleset ratings (1000 for unrated players), snake distribution,
then swaps players to reduce differences between group medians. Managers can randomize or
edit every slot before starting. They can change the scheme or revoke approved registrations
before any stage starts. Any unfilled slots become **Chairs**.

Chairs are passive game participants: automatically joined, always at zero, excluded from
actions, ratings, and suspicion calculations. They occupy places, including finishing above
negative-scoring humans. They can advance through brackets. Games containing only Chairs
resolve automatically. Chairs are excluded from first-stage qualification standings.

Each group game awards configurable place points (default 4, 3, 2, 1), plus the player's
score multiplied by the configurable coefficient (default 0.02). Shared places receive the
mean of the occupied place awards. First-stage standings sum these points across all games
and groups. Quiz entrants play one solo game and rank by score. SI stage ties compare summed
score without penalties, then correct-answer counts at descending question values. Remaining
equality is resolved by a persisted random seed for reproducible qualification and bracket slots.

Play-off takes the highest first-stage finishers in order, up to the scheme's capacity, with
Chairs filling vacancies. Without a first stage, managers seed approved players randomly or
manually; excess registrations must be revoked or accommodated by another scheme.
Play-off advancement uses game ranking only; place points and score multipliers apply only
to the first stage and are omitted from play-off settings.

Each round selects one published tournament packet and independent discoverability/playability
switches for its prescribed participants. A packet cannot serve two rounds in the same
tournament. Round discovery/play switches inherit the assigned packet's member defaults
(or tournament defaults before packet selection), unless explicitly overridden. Unstarted
rounds from older setups inherit defaults after migration; existing started-round choices
are preserved. Unstarted
stages show these configured values with a warning and disabled switches; actual access
still requires a started stage. Packets and deadlines can be prepared
before starting. After a human game starts or resolves, the round's packet is locked. General packet
accessibility controls only reading. Reading still requires the existing release/access rules.
Selecting a round packet sends lobby members its roster or solo restriction. Readiness and
game assignment recheck exact human membership, round access, and the deadline. A prescribed
game can be assigned only once; failed or cancelled attempts can retry subject to exposure rules.
Round cards show game details in a separate **Game statuses** window.

Round deadlines are **game start** cutoffs: successful lobby assignment counts as starting.
Assigned games retain normal join deadlines and finish normally. Unstarted games receive
random places, zero scores, and the corresponding place awards. A durable reconciliation job
records results once, resolves bracket dependencies, and completes stages after all results
are final, including appeals. Restarting or retrying a job does not reroll results.

#### Classic match chats

Every prescribed match that has not been played, whose roster is known, and whose round has
an assigned packet with playable access owns a persistent chat shared by its human seats
(Chairs are not members). Messages are stored transactionally and relayed through the
outbox like lobby/game chat; a chat closes permanently once its match is assigned, has
results, or the stage completes. Participants may set one shared, advisory
`planned_at` per chat (overwriting or removing it), which posts a system message to the
chat and creates player-role reminders 24 hours and one hour before that time. See
[Telegram](telegram.md) for the chat interaction flows.

## Tournament settings

Managers choose defaults and per-parameter player mutability. Effective parameters must satisfy
tournament type, ruleset, and technical limits. Assignment snapshots preserve them for existing
games; changes to assembling lobbies clear readiness.

Classic SI games always use every theme in the round packet. Theme count is fixed for
managers and players, including existing tournaments; unavailable themes block the game
instead of reducing its length. Ladder theme count remains configurable.

`minimum_players` and `maximum_players` default to four, including existing tournaments.
Each has its own mutability grant. They must satisfy `1 <= minimum <= maximum <= 12`;
the maximum controls lobby capacity and both limits govern game starts. Classic uses its
prescribed roster, including solo games and Chairs, instead of these limits.

The runtime currently interprets policies for:

- hybrid matchmaking enablement, only when the tournament type supports it;
- rating enablement when the tournament type is rated;
- an optional positive `maximum_participants` limit;
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
editable with other metadata.

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
