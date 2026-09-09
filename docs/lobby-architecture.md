# Lobbies, Matchmaking, and Assignment

[Technical index](architecture.md) · [Tournament policy](tournaments.md) ·
[Game execution](game-rulesets.md)

## Source map

- [services/matchmaking.py](../src/sitg_bot/services/matchmaking.py): `LobbyService`,
  invitations, readiness, validation, hybrid matching, and atomic start.
- [services/ruleset_content.py](../src/sitg_bot/services/ruleset_content.py): versioned
  content adapters, stored-content projections and claim filtering.
- [domain/game_rulesets.py](../src/sitg_bot/domain/game_rulesets.py): ruleset contract,
  play units, validation and immutable plans.
- [services/concurrency.py](../src/sitg_bot/services/concurrency.py): shared stale-write error.
- [test_matchmaking.py](../tests/unit/test_matchmaking.py) and
  [test_lobby_architecture.py](../tests/integration/test_lobby_architecture.py): matching,
  plan, freshness, and transaction regressions.

## Responsibility boundaries

| Level | Responsibilities |
| --- | --- |
| Platform and lobby core | Persistent lifecycle, creator authority, invitations, membership, readiness, expiry, one active lobby/game per player, concurrency, events, hybrid-search mechanics, structured validation, and atomic assignment |
| Tournament type | Permitted assembly methods, stage and participant eligibility, scheduling, capacity restrictions, packet-count restrictions, and rating model |
| Tournament policy | Manager enablement of type-supported hybrid search, enrollment and packet access, effective defaults, player-mutability grants, appeals, rating enablement, and other manager-controlled restrictions |
| Game ruleset | Parameter schema and validation, a versioned content adapter, play units, exposure claims, start restrictions, and immutable assignment-plan construction |
| Game host | Execution and recovery of the stored plan, including progression, input, judgment, scoring, ranking, events, disclosure, and finalization |

Constraints compose by intersection. Ruleset approval cannot bypass a tournament restriction,
and a tournament cannot expand platform technical limits.

## Universal lobby model

A lobby stores its pinned tournament, tournament-type version, game-ruleset version, and
tournament-policy version. It also stores the creator, ordered active members, invitation and
search state, target capacity, selected packet versions, ruleset-owned parameter document,
structured validation results, readiness, expiry, lifecycle status, version, events, and the
assigned game reference.

The core enforces only rules it can express without interpreting a ruleset:

- the lobby is assembling and unexpired;
- the actor has lobby authority;
- members have active tournament membership and no other active lobby or game;
- selected packets are assigned and accessible;
- all members are ready for the current lobby version;
- validation, content reservation, game creation, and lobby closure share one transaction.

Every lobby and packet workflow requires an explicit tournament. There is no implicit
tournament, automatic enrollment, or unscoped packet access.

## Ruleset planning contract

The shared `GameRuleset` contract parses and validates parameters, validates packet content,
validates a lobby against candidate play units, and prepares an immutable assignment plan.
A separately registered adapter for the same ruleset version projects stored packet content
into play units and filters claims already reserved or burnt for any member. The lobby service
depends on these two contracts and contains no SI content queries.
Plans contain the ruleset identity, complete parameters, pinned packet versions, ordered play
units, canonical exposure claims, a stored random seed, and ruleset-owned initial state.

Validation returns stable codes with structured details. Current codes include
`packet_not_playable`, `insufficient_fresh_content`, `ruleset_player_limit_exceeded`,
`tournament_capacity_restriction`, and `tournament_packet_limit_exceeded`. Presentation
adapters translate these results without reproducing ruleset logic.

## Canonical exposure claims

An exposure claim is a namespaced logical identity that cannot be concurrently reserved or
previously burnt for the same player. Claims are stable across content corrections because
they use logical identities instead of revision IDs.

Claims move through this lifecycle:

1. `reserved` during the assignment transaction;
2. `burnt` at the ruleset's disclosure boundary;
3. `released` if the game terminates before that boundary.

The partial unique index on player, claim namespace, and claim identity makes reservation
universal and race-safe. Packet-version provenance remains separately stored on the game.

## Settings and mutability

The ruleset owns parameter names, types, defaults, and validation. A tournament type can
narrow valid values or combinations. Tournament-policy versions store effective defaults and
the fields lobby creators may override. Any effective parameter, packet, membership,
entitlement, or policy change clears readiness. Assigned games retain their captured policy,
parameters, and plan.

Rating is derived only from the snapshotted tournament-type rules, tournament policy, and
tournament ordering.
Appeal escalation is derived only from the snapshotted appeal policy.

## Start and recovery

Authoritative start repeats validation under locks, asks the ruleset to prepare a plan,
reserves all claims for all participants, persists the game and its packet provenance,
asks the game host to materialize ruleset execution state, closes lobby memberships, and
marks the lobby started in one transaction. The stored plan and seed make selection
auditable. Recovery executes the stored plan through the game host; it does not reconstruct
selection from mutable packet state.

## Current SI adapter

SI version 1 interprets a logical theme revision as a play unit. It selects the configured
number of fresh themes with a stored seed and orders selected units by packet selection and
theme position. Each unit claims both its logical theme and every logical question it
contains; announcing the initial theme list is the disclosure boundary that burns those
claims. The SI game host materializes theme and question execution rows from the stored plan.

These are SI choices, not lobby-core assumptions. The lobby core does not inspect question
values, themes, buzzing, or SI progression. Full SI behavior is defined in
[game-rulesets.md](game-rulesets.md).

## Hybrid matchmaking

The platform provides ruleset-neutral hybrid-search orchestration, but availability is not
universal. It requires both a tournament type whose immutable rules declare
`supports_hybrid_matchmaking: true` and a current tournament policy with
`hybrid_matchmaking_enabled: true`. Managers control the latter but cannot override the type.

Ladder supports this capability and defaults to disabled per tournament. Classic does not
support it because its opponents are designed to come from preset matches or earlier
results. It rejects a policy that tries to enable hybrid search; invitation assembly is its
only currently implemented lobby path while type-specific scheduling remains future work.

When available, candidates share a tournament and compatible pinned type, policy, and
ruleset context; fit the surviving lobby capacity; pass directional blacklists and widening
rating/reputation tolerances; and admit a valid combined assignment plan. Merging clears
readiness and recomputes validation. Disabling the policy stops searches in assembling
lobbies without affecting invitation joining.
