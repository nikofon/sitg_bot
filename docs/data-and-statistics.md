# Ratings, Trust, and Statistics

Pairwise Elo, rating projections, reputation/report ledgers, and SI suspicion evaluations are
implemented. The metric definitions below also guide planned general statistics views; a
defined metric does not imply an existing endpoint. The Mini App [player profile](mini-app.md)
already serves per-ruleset rating history, win-rate/placement distribution, SI per-question-value
statistics, and privacy-aware game result grids from these facts. See
[planned features](future-work.md).

## Source map

| Module | Responsibility |
| --- | --- |
| [domain/rating.py](../src/sitg_bot/domain/rating.py) | Pairwise Elo and both confidence models |
| [services/persistent_game.py](../src/sitg_bot/services/persistent_game.py) | Appeal-aware results and ordered rating settlement |
| [domain/trust.py](../src/sitg_bot/domain/trust.py), [services/trust.py](../src/sitg_bot/services/trust.py) | Reputation, reports, SI metrics, suspicion evaluation/evidence and review |
| [services/privacy.py](../src/sitg_bot/services/privacy.py) | Unexposed anonymization and answer-text purge helpers |
| [rating_simulation.py](../src/sitg_bot/rating_simulation.py) | Reproducible model comparison and HTML/CSV reports |

Tests: [test_rating.py](../tests/unit/test_rating.py), [test_trust.py](../tests/unit/test_trust.py),
[test_rating_simulation.py](../tests/unit/test_rating_simulation.py), and relevant PostgreSQL
[integration tests](../tests/integration). See [data rules](data-rules.md) for physical records
and [technical index](architecture.md) for their callers.

## Storage principles

- Rated tournament types (including Ladder) settle tournament ratings by default;
  an explicit `rating_enabled: false` policy disables them. Telegram results show
  tournament and global rating deltas after settlement.
- Store stable source facts and relationships; calculate counts, percentages, and
  totals from those facts. Frequently used aggregates may later be cached, but they
  are not the authoritative record.
- Use internal stable identifiers for tournaments, tournament-type versions,
  game-ruleset versions, packets, questions, authors, players, games, and attempts.
  Ruleset-specific containers such as SI themes also receive stable identifiers. Names
  and Telegram usernames are mutable attributes, not identifiers.
- Every packet records one or more tournament assignments, while each draft records
  its creation context and intended assignments. Every lobby, game, result, and rating
  event records one tournament. Manager grants, membership, and packet-access
  entitlements are scoped to a tournament and retain an audit history.
- Associate results with the exact packet and question versions that were played so
  later content edits do not rewrite historical statistics.
- A correction creates a new revision of the same logical question, so its statistics
  continue across revisions. A substitution creates a new logical question whose
  statistics start empty; the retired question's statistics remain available.
- Final statistics use the outcome after all appeals and authorized manager decisions.
  Preserve both the original answer-checking result and the final result for auditing.
- Production records retain timestamps for games, attempts, appeals, ratings, and reputation
  history.
- Archive content instead of deleting records that are referenced by game history.

## Ruleset-independent records and statistics

### Packet performance

For each game, source packet, and participant, retain:

- the tournament type, game ruleset, effective parameters, and policy versions;
- the source packet/version, selected ruleset-specific content identities, game, and player;
- the player's final place and net score;
- whether the result was finalized, cancelled, or abandoned;
- its tournament type and whether that configuration considers the result rated;
- the packet uploader and publication authority.

This history provides participant and packet performance. Eligibility, reservation,
exposure, abandonment, and rating-settlement semantics are defined in
[data-rules.md](data-rules.md); this document only defines the facts and derived
statistics built from them.

Ruleset-independent packet statistics may include completed games, distinct players, and
average or median final score, grouped by tournament type and game ruleset. Attempt,
question, and content-container statistics depend on the ruleset that produced them.

### Common questions and appeals

Packets retain stable logical question identities and immutable revisions. An assigned game
pins the exact revisions selected by its ruleset, so later content edits do not alter its
facts. A ruleset decides how those questions are presented and what interaction records it
needs; universal statistics do not assume themes, buzzing, point values, or a particular
answer model.

When a ruleset supports appeals, each appeal retains the challenged ruleset-owned judgment,
requester, timestamps, snapshotted voting/escalation policy, player votes, provisional
result, escalation, authorized decision-maker, and resulting corrective facts. This supports
auditing a result if tournament policy later changes.

## Current SI records and derived statistics

### SI theme performance

SI themes retain their packet, position, author, and constituent questions. Per-player
canonical claim history retains the assigning game, namespace, logical identity, and
whether the claim is reserved, burnt, or released. An SI game's ordered theme collection
pins each exact theme revision and source packet version. Theme performance statistics are derived from question results
and may include number of players exposed, buzz rate, correct- and incorrect-answer
rates, and average score. Future rulesets define statistics for their own content
containers without requiring them to model those containers as themes.

### SI question performance and attempts

For every presentation of a question, retain which game it belonged to and which
players were eligible to buzz. For each player-question interaction, retain:

- whether the player buzzed and the accepted buzz order;
- the submitted answer text, if any;
- whether the attempt timed out;
- the original automated judgment and the final judgment;
- the question value and other applicable scoring parameters;
- the exact score change added or deducted;
- any appeal linked to the attempt.

These records provide the players who answered each question correctly or incorrectly,
the number and outcomes of appeals, and the submitted incorrect answers. A timeout is
an incorrect attempt with no submitted answer text.

Question-level statistics may include exposure count, buzz count and rate, attempt
count, correct- and incorrect-answer rates, timeout count, appeal count and rate, and
the rate at which appeals overturn the original decision.

### SI author attribution and statistics

The current source records retain:

- an internal author ID and display name;
- the themes directly credited to them;
- the questions credited to them.

A question without explicit authors inherits all theme authors; explicit question authors replace
them. Each coauthor receives full question counts and performance statistics, once per identity.
Theme counts include only directly credited themes.

Administrators can edit author names, name components, and Telegram details in
**Management → Authors** without changing the identity or its credit. **Split authors** appears
for comma-separated names and creates new identities, even when matching authors already exist.
All receive the original theme/question attribution. The administrator chooses which new author
receives existing player links, link requests, lead-author roles, and Telegram details. Splitting
updates draft associations, preserves exposure burns, and removes the combined identity atomically.
Both actions reject stale author data.

Logical packets, themes, and questions retain their current statistical author separately from
immutable revision credits. Authorship corrections transfer that logical attribution; played
revisions and result facts stay unchanged. Author counts deduplicate logical identities across
correction revisions and include retained historical content. Linked authors' exposure burns are
permanent even after their statistical attribution is corrected away.

Derived author statistics include:

- number of active and archived themes and questions in the database;
- number of times their questions were presented and attempted;
- percentage of final attempts judged correct and percentage judged incorrect;
- buzz rate across their questions;
- appeal rate and successful-appeal rate for their questions.

Correct and incorrect percentages use all final attempts as the denominator. Buzz rate
uses eligible player-question exposures as the denominator: the number of eligible
players who buzzed at least once divided by the number who could have buzzed. This
definition prevents games with different player counts from distorting the statistic.

Author reputation is not currently stored. Its inputs, permissions, and scale remain
to be defined; if introduced, changes should use a reasoned adjustment ledger rather
than only an overwritten total.

The player author catalogue and profiles are implemented in `services/author_profiles.py`.
Tournament counts use distinct author registrations; question counts use logical identities,
including retained history. Profiles include completed, presented questions from finalized SI
games with no unresolved appeals. They show presentations, player-question opportunities,
accepted buzzes and their rate, attempts, final accuracy, timeouts, and solved rate (presentations
with at least one final correct answer divided by all presentations). Repeated plays count
separately. Participation rows preserve opportunities even after answering or leaving clears
current eligibility. Per-value groups use actual played values from the assigned packet/theme
revision, not normalized values. Empty denominators display no percentage. Only aggregates
and public author names/counts are exposed.

## Ruleset-independent player records and statistics

Each player retains:

- an internal player ID and Telegram user ID;
- their current display name and relevant profile timestamps;
- their rating and rating-change history separately for each rated tournament;
- their rating and rating-change history for each ruleset key across all of its versions and
  tournaments;
- their current reputation;
- their current suspicion rating;
- their ruleset-specific reservation and exposure history, including the game and
  game ruleset that caused it.

Reputation and suspicion are global and separate from tournament and ruleset ratings.
Reputation starts at 90 and is capped to 0–100. After a completed shared game, one player
may upvote or downvote another by one point. The same directed pair cannot produce another
vote for seven days, so repeated games cannot be used for rapid mutual boosting or
degradation. Every request retains its game and timestamp, and the adjustment ledger records
the requested and cap-limited applied deltas.

Reports are also tied to a shared completed game, with one report per reporter/target/game.
A toxicity report requests a −10 reputation adjustment; a cheating report adds 10 to the
reported player's suspicion. Reports retain their type, optional details, actors, game, and
timestamp. The type vocabulary is validated by the application so it can expand without a
schema change.

SI evaluates suspicion in completed UTC-week windows. Completed games without unresolved
appeals are incrementally compacted into per-player game counters, cumulative player
counters, player-question facts, and question aggregates. Evaluation starts with the
twentieth completed multiplayer SI game and compares the player's window with established
players in a shared 50-point rating-bucket baseline covering approximately ±200 rating
points. The implemented signals are an average accepted-buzz reveal fraction substantially
lower than the cohort average, buzzes with at most 10% of the ordinary timer remaining, and
correct answers on questions in the lowest 20% of reliable question correct rates. A signal
is raised only with at least 10 player
observations, at least 100 cohort observations from 10 cohort players, a difference of at
least 0.15, and a z-score of at least 2.5. Buzz reveal timing uses the cohort variance of the
continuous reveal fraction; the other two signals use binomial rate variance. Each raised
signal adds one suspicion point.
One durable, unique job is enqueued per player and completed week. Deterministic scheduling
spreads those jobs across the following seven days. Workers use `FOR UPDATE SKIP LOCKED`,
claim at most ten jobs at a time, and commit every player separately; stale claims are
recovered after 15 minutes. Shared cohort baselines avoid repeating historical scans for
every player. Evaluations without a reliable cohort or question sample are postponed for
seven days. Timing facts, job attempts, evaluation details, and suspicion deltas are retained
for audit. Every raised statistical signal also receives an immutable evidence header containing
the algorithm version, frozen thresholds, rates, sample sizes, margin, z-score, and baseline
identity. Its child evidence rows snapshot every contributing game/round/question action and its
then-current timing, answer, value, and question-difficulty facts. Platform administrators can
retrieve this case history and may reset global suspicion to zero with a required note; the reset
is retained as an administrator-attributed suspicion-ledger entry and does not delete evidence.

Player history links the player relationally to finalized game results and to any
ruleset-owned interaction facts. Derived views should query those relationships rather than
copy lists into the player record.

Rating history has durable per-player settlement order in two scopes. A later tournament
result waits for an earlier result in that tournament, and a later ruleset result waits for
an earlier game under the same ruleset key. Tournament rating entries are created only when
the pinned type and policy enable them. Every multiplayer game creates ruleset entries even
when its tournament rating is disabled. One-player games create neither kind of entry.

## Rating calculation

Every multiplayer placement is expanded into pairwise results. Finishing above another
player is a win, below is a loss, and a shared place is a draw. Expected results use the
ordinary base-10 Elo logistic curve. Each player's residuals are averaged, so adding more
players does not multiply the maximum match delta. Ratings and ledger deltas retain four
decimal places.

The player-specific K factor is `25 + 15 × (1 - confidence)`: new or uncertain ratings use
K 40 and established ratings approach K 25. The retained comparison-only `log_recent` model
uses:

```text
evidence   = ln(1 + games_total) / ln(21) + sqrt(games_last_30_days / 10)
confidence = 1 - exp(-evidence)
```

The active production `time_weighted` model gives each historical delta this weight, where
`D=5` days and the current game's day is the latest reference day:

```text
weight = (1 + min(latest_day, played_day + D) - earliest_day)
         / (1 + latest_day - earliest_day)

effective_games     = sum(weight)
time_weighted_rating = 1000 + sum(delta × weight)
evidence             = 1 - exp(-effective_games / 10)
stability            = 1 / (1 + ((rating - time_weighted_rating) / 400)^2)
confidence           = evidence × stability
```

Production games use `time_weighted` and pin that model so delayed settlement remains
reproducible. Previously assigned games retain their existing pin. Tournament and ruleset
ratings use the same function but independent histories. The tournament's pinned positive
`ruleset_rating_weight`, default `1`, multiplies only its ruleset rating delta. Tournament
rating always uses weight `1`, and weight never changes either confidence update. The
`log_recent` implementation remains available to the standalone comparison simulator but is
not selected by the application server.

Ruleset-independent derived player statistics include:

- number of tournaments joined and managed, by tournament type;
- number of distinct packets played, overall and per tournament;
- games played, wins, other placements, and win rate;
- total and average final score, grouped by game ruleset.

A win is a finalized first-place result under the game's snapshotted ruleset. Universal
versioning and reproducibility invariants are defined in [data-rules.md](data-rules.md).

### Current SI player statistics

SI-derived player statistics may additionally include:

- number of distinct themes exposed and fresh themes remaining per tournament/packet;
- total net SI score;
- total points from correct answers before deductions (the "no minuses" total);
- correct and incorrect attempt counts and answer accuracy;
- buzz count and buzz-to-correct conversion rate;
- appeal count, successful appeals, and appeal success rate;
- performance by point value, theme, and author.

SI ranking and the meaning of these gameplay facts are defined in
[game-rulesets.md](game-rulesets.md).

## Stored personal data and maintenance helpers

The current schema stores Telegram identifiers, private real names, public nicknames, and
submitted answer text.
`PrivacyService` contains two application-level maintenance operations:

- anonymize a player who has no active game by clearing their Telegram identifier, replacing
  their private real name and public nickname, disabling Telegram exposure, and marking the
  account anonymized;
- clear submitted answer text from terminal games older than a caller-supplied retention
  window, which defaults to 180 days in the method signature.

Neither operation is exposed by a command or run by a scheduler. The default argument is an
implementation detail, not an accepted retention policy. Retention configuration,
authorization, and the intended product use of anonymization remain undecided and are
indexed in [future-work.md](future-work.md).
