# Classic Swiss stage

[Tournament lifecycle and access](tournaments.md#classic-competition)

Managers select Swiss for the first stage, a positive round count, and 2–12 players per game.
At least two approved players are required. Stage start locks configuration and participants.
SI games use every theme in their assigned round packet.

## Seeding and pairings

Seeds follow descending global rating in the tournament's ruleset (1000 for unrated players;
player ID breaks rating ties). Automatic previews are refreshed at stage start; manual/random
seeding is unavailable. Chairs fill incomplete games.

The opening round draws one seed from each rating band into each game. Later rounds order
players by accumulated stage points, then initial seed. Greedy pairing prefers fewer previous
encounters, then smaller point differences; up to eight pair-swap passes improve that objective.
The algorithm is deterministic. Repeats are permitted when this search cannot avoid them;
Chairs do not count as opponents.

Only round one is paired at start. Each subsequent round is persisted after every previous
game is final, including appeals and deadline results. Tournament locking and match uniqueness
prevent duplicate pairings on retries. Completion requires all configured rounds.

## Scoring and qualification

Each game awards configurable place points plus in-game score × coefficient, as in groups.
Shared places receive the mean occupied awards. Stage points sum across all rounds.

Ties compare the sum of encountered opponents' final standings positions, lower being better.
These positions use stage points alone, averaging tied positions before applying the tiebreak,
so calculation is non-circular. Every encounter counts, including repeats; Chairs are excluded.
Remaining equality uses the persisted random seed.

The displayed opponent sum is provisional while games remain. Final standings and play-off
qualification use the same calculation over all finalized results.
