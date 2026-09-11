# Tournament Model

[Technical index](architecture.md) · [Console usage](console-commands.md)

## Source map

- [services/tournaments.py](../src/sitg_bot/services/tournaments.py): type descriptors,
  creation, settings, registration, roles, policy, assignments, and entitlements.
- [services/token_requests.py](../src/sitg_bot/services/token_requests.py): creation-token
  requests, rulings, and inventory; shared identity contracts are in [application.md](application.md).
- [storage/models.py](../src/sitg_bot/storage/models.py): tournament, version, membership,
  management, requirement, token, and packet-access records.
- [test_tournaments.py](../tests/unit/test_tournaments.py) and
  [test_lobby_architecture.py](../tests/integration/test_lobby_architecture.py): behavior and
  transactional integration coverage.

The following describes implemented behavior. Remaining competition algorithms and product
decisions are tracked in [planned features](future-work.md).

## Ownership and roles

A packet must be assigned to at least one tournament and may be assigned to several.
The logical packet and its immutable versions are shared content; an explicit
tournament-packet assignment controls how each tournament may use and expose them.
Drafts record the tournament context in which they are created and any intended packet
assignments. Lobbies, games, results, and rating events each belong to one tournament.

Roles are scoped as follows:

- **Administrators** direct the bot as a whole. They authorize tournament creation and
  perform platform-wide moderation and operations.
- **Managers** run one concrete tournament. A tournament may have multiple managers.
- **Players** enroll in and play a concrete tournament.

Administrator roles are independent from tournament roles. An administrator may manage a
tournament they organize and be only a player in another tournament. Within one tournament,
active manager and player roles are mutually exclusive: managers have access to all assigned
packets and therefore cannot discover, select, register for, or play that tournament. Every
packet version a manager can read through their role — published into, assigned to, or edited
in their tournament — is permanently burnt for them, and granting a manager role retroactively
burns the versions currently assigned to that tournament. Removing a role or an assignment
never resets burns.

Tournament discovery is independent from membership. A finalized public tournament may be listed
by every authenticated player, while a private tournament appears only in the membership
listing of its participants. Public visibility does not grant a right to enroll or play;
enrollment is governed independently by tournament policy and manager actions.

Registration and participation are separate membership states. A player may be invited,
registered, approved, active, rejected, suspended, or left. Public tournaments accept
self-registration while the effective registration window is open. A private tournament
requires a direct invitation before registration. Managers approve registrations. Approval
immediately activates a player in an open-ended Ladder; in a finite tournament it moves the
player to `approved` until managers close registration and finalize an explicit participant
list. Finalization activates selected approved players and rejects the remaining applicants.
Only active participants can enter lobbies or use tournament packet play rights.
An optional positive `maximum_participants` policy narrows any limit supplied by the
tournament type and is enforced when the finite participant list is finalized.

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

Tournaments carry a BCP 47-style language tag (`und` means unspecified), explicit authors,
and packet-derived lead/theme/question authors. Payment is `free`, `one-time`, or
`per-stage`. A paid tournament has one or more named pricing plans, and every plan has one
or more positive two-decimal prices in distinct three-letter ISO currencies. For example,
`Students: 10 USD / 9 EUR` and `Adults: 15 USD / 13 EUR`. Free tournaments have no pricing
plans.

Registration start/end, tournament start, planned finish, and actual finish are separate
timezone-aware facts. Without a manual override, the enabled registration flag is bounded
by its dates (the end is enforced when late registrations are disabled). The manager's
explicit open/closed override takes precedence over that window. Finite
tournaments require registration end, start, and planned finish; open-ended types may omit
registration end and planned finish. Completion records actual finish and closes
registration. Player-facing server listings
separate tournaments into future, ongoing, and past groups. Completed and archived
tournaments are always past; otherwise schedule boundaries determine the group. An
unscheduled active tournament is ongoing.

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
  and unable to use hybrid matchmaking by design. Classic matches are intended to be preset
  or derived from prior results; until that type-specific assembly is implemented, the
  generic lobby service permits invitation assembly only.

The descriptors already constrain lobby assembly and tournament-rating eligibility. Classic
seeding, advancement, bracket, stage, and packet-count algorithms are not implemented; they
are indexed in [future-work.md](future-work.md).

## Tournament settings

Managers configure their tournament within its type's rules and platform technical
limits. A tournament may be very restrictive or permissive.

Each configurable gameplay setting has both:

1. a tournament default chosen by managers; and
2. a manager-selected mutability policy defining whether lobby players may override
   it.

The policy may lock every parameter, allow every parameter, or grant override
permission per parameter. Effective parameters are validated against the tournament
type, selected game ruleset, and technical limits, then copied into the assigned game
so later changes cannot alter an existing game. Changes that affect an assembling
lobby clear player readiness.

The runtime currently interprets policies for:

- hybrid matchmaking enablement, only when the tournament type supports it;
- rating enablement when the tournament type is rated;
- an optional positive `maximum_participants` limit;
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
  is already burnt for that player, and `forbidden` disables observing;
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

`hybrid_matchmaking_enabled` is a boolean and defaults to `false`. Managers may set it to
`true` for Ladder tournaments. Enabling it for Classic or any other type without the
`supports_hybrid_matchmaking` capability is rejected. Disabling it also stops searches in
assembling lobbies; direct invitation remains available.

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

The current access decision composes active membership, manager role, assignment-wide
member flags, and explicit per-player grants. Stage- and advancement-aware entitlement
automation belongs to the unimplemented Classic tournament algorithm.

Authorization is checked whenever content is listed, previewed, downloaded, selected,
assigned, or administered; hiding a command in the interface is not sufficient.
Historical games retain their pinned packet revisions even if later access policy
changes, while post-game access to their full content follows a separately defined
tournament policy.

Gameplay/library access has an assignment-wide default and optional per-player override:

- `no-access`: neither play nor library eligibility;
- `play-only`: may play, but play does not grant library eligibility;
- `read-after-play`: may play and becomes library-eligible only after actually playing;
- `read-or-play`: may play or read; reading first must burn the version's canonical claims
  and thereby prevent later play of that content.

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
