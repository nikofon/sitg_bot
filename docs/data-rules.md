# Data Model and Invariants

This document contains only implemented, ruleset-independent data invariants. Ruleset-owned
behavior belongs in [game-rulesets.md](game-rulesets.md), and unimplemented requirements are
indexed in [future-work.md](future-work.md).

## Storage map

[Technical index](architecture.md) · [Database operations](database-operations.md)

[storage/models.py](../src/sitg_bot/storage/models.py) is the physical schema reference.
[storage/database.py](../src/sitg_bot/storage/database.py) provides async sessions;
`Database.transaction()` commits on success and rolls back on failure. Services own use-case
transactions and acquire row/advisory locks where needed. Do not share one session between
concurrent operations or commit halfway through an atomic assignment/publication.

| Record family | Principal tables and relationships |
| --- | --- |
| Identity | `players`, `platform_administrators`, `player_telegram_navigation`, author-link records |
| Organization | `tournaments` references type/ruleset/policy versions; managers, memberships, registration attempts, pricing and authors are related records |
| Classic competition | `classic_stages` owns seeds/scoring; `classic_rounds` owns packets/deadlines; `classic_matches` stores prescribed seats, advancement, assigned game, and final results |
| Content | `logical_packets` → `packet_versions`; `themes` → `theme_revisions`; `logical_questions` → `question_revisions`; `packet_questions` stores placements |
| Access | `tournament_packet_assignments` joins tournaments to content; `tournament_packet_entitlements` stores player overrides; drafts retain intended tournaments |
| Assembly | `pregame_lobbies` owns member, packet, and event rows and references its assigned game |
| Execution | `games` owns participants, observers, packet versions, themes, question rounds, player-question state, attempts, appeals/votes, results, score ledger and events |
| Exposure | `player_exposure_claims` ties a player and canonical logical identity to reservation/burn/release and source packet provenance |
| Rating/trust | Tournament and ruleset rating ledgers, reputation votes/reports/ledgers, suspicion aggregates/evaluations/evidence |
| Application/presentation | Request audit/idempotency, Telegram update receipts/game views, Mini App sessions, launch references, notifications |
| Work queues | `outbox_events`, `durable_jobs`, and suspicion evaluation schedules |

Use stable IDs for relationships; names/usernames are mutable. Timestamps are timezone-aware.
SI scores use `NUMERIC(24, 8)` and rating ledgers retain four decimal places. Foreign keys,
checks, and uniqueness constraints complement service validation; source facts and immutable
versions remain authoritative over projections.

[0001_initial_schema.py](../migrations/versions/0001_initial_schema.py) freezes the baseline
independently of ORM metadata. [test_initial_schema.py](../tests/integration/test_initial_schema.py)
checks schema parity and migration lifecycle; other integration tests exercise transactional
constraints. Migration procedure belongs in the operations guide.

## Assignment and versioning

- Every game belongs to one tournament and pins its tournament-type, game-ruleset, and
  tournament-policy versions.
- Tournament play requires an actual manual start; planned dates do not open or close play.
  Starting a Classic stage also records the tournament's actual start. Durable reminders
  use the planned start date and persist recipient deduplication in notifications.
- A game stores its effective ruleset parameters, immutable assignment plan, random seed,
  adopted packet versions, and the exact content revisions selected for play.
- Changes to tournament policy, ruleset registration, packet assignments, or published
  content do not alter an assigned game.
- Lobby start repeats validation and creates the game, participants, content provenance,
  and exposure reservations in one transaction.

## Canonical exposure claims

- A ruleset defines the play units it can assign, the canonical claims protected by each
  unit, and the disclosure boundary at which those claims become exposed.
- Assignment reserves every selected claim for every participant. A player cannot hold or
  receive another live reservation for a claim they have already reserved or burnt.
- An observer permitted to see fresh assigned content receives the same canonical claims.
  Joining after disclosure burns them immediately; joining before disclosure reserves them
  until the ruleset boundary. Leaving before disclosure releases only those reservations.
- Disclosure changes reserved claims to `burnt`. A burnt claim remains unavailable to that
  player across tournaments and across content revisions that preserve the same logical
  identity.
- Termination before the ruleset's disclosure boundary releases reserved claims. A later
  termination releases claims that are still reserved but never reverses claims already
  burnt.
- Claim uniqueness is global per player and identified by claim namespace plus logical ID;
  it does not depend on a packet revision or tournament.

The SI mapping and its disclosure boundary are defined in
[game-rulesets.md](game-rulesets.md).

## Lobby and participant lifecycle

- A player may belong to at most one active lobby or active game and never both at once.
- Classic Chair participants have `is_chair=true`, are inactive and automatically joined,
  and receive no gameplay actions or rating changes. They retain zero scores in game results.
- Lobby observers do not count toward player capacity, readiness, content selection, or
  hybrid matchmaking. Game observers are stored separately from participants and never
  enter score, result, rating, appeal electorate, buzz, or answer state.
- Joining or reconnecting to a game as a participant ends every other active observation
  for that player. Observer-created reservations that have not crossed disclosure are
  released; burnt claims remain burnt. A joined active participant cannot start observing
  another game.
- A game uses `lobby`, `active`, `completed`, and `finalized` for its ordinary lifecycle;
  `failed_to_start`, `cancelled`, and `abandoned` are implemented terminal alternatives.
- Game assignment starts a fixed five-minute first-player join deadline. The first join
  replaces it with a fixed five-minute deadline for every remaining assigned player. If the
  applicable deadline expires, the game becomes `failed_to_start`, all participants and
  observers become inactive, all reservations are released, no content is burnt, and no
  rating result is produced.
- Player abandonment before protected content is disclosed cancels the game and releases
  its reservations. No result or rating change is produced.
- Player abandonment after disclosure deactivates only that participant. The game continues,
  and already burnt claims remain burnt.
- An abandoned participant may reconnect while the game remains active unless assignment to
  a later game has forfeited reconnection. This ordering uses the player's global game
  sequence rather than timestamps.
- When a paused active game has no connected participants, a fixed five-minute abandonment
  deadline starts. A participant connection cancels it. Expiry resolves pending appeals as
  rejected, preserves all burnt claims, releases only still-reserved claims, and finalizes
  results and ratings from the scores actually recorded at expiry.
- Platform capacity is at most 12 players per game. Tournament types and rulesets may impose
  narrower limits.

## Tournament policy, appeals, and rating order

- Hybrid matchmaking requires both immutable support in the pinned tournament-type rules
  and explicit enablement in current tournament policy. Policy may narrow a type capability
  but cannot add one. Invitation joining does not depend on hybrid enablement.
- Effective parameters come from a versioned tournament policy plus only those lobby
  overrides that policy permits. Parameter or policy changes clear readiness in affected
  assembling lobbies.
- Assigned games retain their snapshotted policy and parameters when tournament managers
  publish a later policy version.
- The snapshotted `observing` policy is `unlimited`, `burnt-only`, or `forbidden`. Observer
  score requests are private reads: they append no event and do not move a deadline or game
  version. Other observer commands cannot alter game state.
- Appeal electorate, threshold, deadlines, and escalation availability come from the
  snapshotted tournament policy. Accepted appeal corrections append score facts instead of
  rewriting attempts or earlier score entries.
- Escalated appeals are reviewed by an active manager of the game's tournament. Platform
  administrator status does not itself grant tournament appeal authority.
- Pending appeals do not prevent later gameplay. Rating results are settled in durable
  per-player order within each tournament, so a later result waits for an earlier unsettled
  result in the same tournament.
- Ranking is determined by the snapshotted game ruleset. Players equal after every
  non-random criterion split their occupied places and count as pairwise draws for rating.

Ruleset-specific appeal effects, scoring, and tie-breaking are defined with that ruleset.

## Reputation, reports, and suspicion

- Reputation and suspicion are global to a player rather than tournament- or
  ruleset-scoped. Reputation is constrained to 0–100 and starts at 90; suspicion starts at
  zero and has no configured upper cap.
- Reputation votes and reports require both actors to be participants in the referenced
  completed or finalized game. Self-votes and self-reports are forbidden.
- A directed player pair may produce at most one reputation vote in a rolling seven-day
  interval, regardless of how many games they share. A game also accepts at most one vote
  and one report from the reporting player for the target player.
- Every applied or cap-limited reputation change, suspicion increase, and administrator
  clearance appends a before/delta/after ledger fact linked to its source and actor.
- Rulesets own their statistical suspicion signals. A ruleset evaluation does not begin
  before the player's twentieth completed multiplayer game under that ruleset, and
  insufficient comparison data postpones rather than clears the evaluation.
- SI gameplay facts are materialized idempotently into per-game counters, cumulative player
  counters, indexed player-question facts, and question aggregates. One durable evaluation
  job exists per player/ruleset/window; workers claim bounded batches with row locking and
  commit each player independently.
- Every raised statistical signal snapshots its algorithm, baseline, decision statistics,
  and contributing game/round/question actions. Administrator clearance resets the current
  value but never deletes evaluations, evidence, reports, or ledger history.
- Moderation bans are reversible rows keyed by the player with a reason and issuing
  administrator; unban records the lifting administrator and time. Banned players keep only
  their packet library; all other gateway actions are refused, and banned players are
  excluded from the legacy suspicion ledger but included in Management's Players section.
  Platform administrators cannot be banned.
- Bug reports are append-only facts carrying the reporter, commentary, and timestamp, and
  fan out an admin-audience notification per active administrator.

## Published content and access

- Published packet and question revisions are immutable. Historical and active games retain
  their pinned revisions.
- Tournament halt preserves library rights and assigned games. Abolition revokes reads through
  that tournament only, and appends global rating reversals without deleting historical facts.
  Administrator Management packet reads bypass access gates and permanently burn content for
  the administrator, including any live reservations.
- A logical packet may be assigned independently to multiple tournaments. Rights granted by
  one assignment do not apply through another tournament's assignment.
- Tournament membership alone does not grant packet access. Current authorization composes
  active membership, manager role, assignment-wide member rights, and explicit per-player
  entitlements.
- Tournament discovery, registration approval, and active participation are separate. Only
  active participants receive gameplay rights. Private registration requires a personal or
  shared-link invitation. Approval is manual unless automatic registration approval is enabled;
  finite tournaments still require participant-list finalization.
- Active registration requirements compose by intersection. Prior-play checks use disclosed
  game participation, not membership. The unseen-packet check uses the packet-version
  provenance of burnt exposure claims; reservations do not count. Failed attempts persist
  the rejected status, evaluated requirement IDs, and every player-visible reason.
  Requirement changes are not retroactive.
- A packet version has one language and an owner-controlled library-release gate. Tournament
  assignments independently define `no-access`, `play-only`, `read-after-play`, or
  `read-or-play`, with optional per-player overrides. Library reading requires the readable
  entitlement, viewing eligibility, and version release; tournament managers bypass these gates.
  Reading atomically burns canonical claims after confirmation of fresh content. Live game
  reservations block reading until disclosure or release. Downloads enqueue delivery in that transaction.
- Payment type belongs to the tournament. Paid tournaments have at least one named pricing
  plan; every plan has at least one positive amount and cannot repeat a currency. Free
  tournaments have no pricing plans.
- Drafts retain their creation tournament, intended assignments, validation result, source
  checksum, and uploader when one is supplied. Publication always records its timestamp and
  records its actor when the caller supplies one, and permanently burns every theme and
  question of the published version for the uploader when one is supplied and every active
  manager of each destination tournament. Correction and substitution saves burn the new
  version the same way for the editing actor and the editing tournament's managers, packet
  assignment burns the adopted version for the destination tournament's managers, and
  granting a manager role retroactively burns the versions currently assigned to that
  tournament. Burns are permanent; removing a role or an assignment never resets them.

The implemented publication, correction, and substitution workflows are documented in
[packet-administration.md](packet-administration.md).
