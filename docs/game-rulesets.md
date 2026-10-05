# Game Rulesets and Execution

This document defines the implemented ruleset boundary and the initial **SI** game
ruleset. Tournament structure and game rules are separate concerns: a tournament type
defines organization such as Ladder or Classic, while its selected game ruleset
defines how individual games are played.

Universal assignment, versioning, claim-lifecycle, and tournament-ordering invariants are
defined in [data-rules.md](data-rules.md). This document owns ruleset-specific behavior.

## Source map

| Module | Responsibility |
| --- | --- |
| [domain/game_rulesets.py](../src/sitg_bot/domain/game_rulesets.py) | Registry, common planning/validation contract, SI implementation |
| [domain/game_settings.py](../src/sitg_bot/domain/game_settings.py) | SI settings, limits and defaults |
| [domain/game.py](../src/sitg_bot/domain/game.py), [game_deadlines.py](../src/sitg_bot/domain/game_deadlines.py) | Domain gameplay state and deadline rules |
| [domain/answers.py](../src/sitg_bot/domain/answers.py), [appeals.py](../src/sitg_bot/domain/appeals.py) | Answer judgment and appeal decisions |
| [services/persistent_game.py](../src/sitg_bot/services/persistent_game.py) | Transactional execution, locks, snapshots, events, appeals and settlement |
| [services/ruleset_content.py](../src/sitg_bot/services/ruleset_content.py) | Database content adapter used during planning |

See the [technical index](architecture.md) for adapters and
[ratings/statistics](data-and-statistics.md) for rating calculations. Start validation with
[test_game_rulesets.py](../tests/unit/test_game_rulesets.py),
[test_answers.py](../tests/unit/test_answers.py), [test_appeals.py](../tests/unit/test_appeals.py),
and PostgreSQL [gameplay integration](../tests/integration/test_telegram_gameplay_integration.py).

## Persistent execution

`PersistentGameService` materializes the stored plan into game themes, rounds, and participant
state. Commands lock the game row, validate the actor/current phase, apply domain decisions,
and commit state, score facts, and ordered events together. `Transition` returns the resulting
snapshot and events; adapters render them rather than advance their own game state.

`recover` reconstructs the current snapshot from persisted execution rows. `progress_due`
advances stored deadlines; the server's durable scheduler resumes work after restart. Keep
join, answer, reveal, pause, appeal, and settlement timing persisted. Delivery delay must not
silently choose a different question or appeal target.

Ordinary game status progresses from `lobby` through `active`, `completed`, and `finalized`.
Join failure and cancellation before disclosure release reservations without rating. Abandonment
after disclosure preserves earned facts and permanent exposure. A game also terminates when
no active or reconnectable players and no active observers remain; temporary abandonment alone
does not meet that condition. See [data rules](data-rules.md) for exact join/pause deadlines.

## Common game-host contract

The implemented boundary combines a ruleset contract with a same-version content adapter. It
covers parameter parsing, stored-content projection, content and lobby validation, play-unit
and claim planning, seeded immutable assignment, answer judgment, scoring, and ranking.
The stored plan is ruleset-neutral; progression persistence and event execution are still
SI-specific and must be generalized before registering another ruleset.

All supported rulesets share these properties:

- games use one or more packets assigned to their tournament;
- packets contain questions, but need not organize them into themes;
- a game has one or more players, subject to platform and ruleset limits;
- the bot presents some form of question and players submit some form of answer;
- the game maintains scores and has a competitive element, including for a solo game
  where the ruleset may define a target or another basis for competition;
- games pin the exact game-ruleset version, effective parameters, packet versions, and
  question revisions used for play;
- gameplay decisions and results remain reconstructible and auditable after restart.

A ruleset supplies its own content schema, play units and canonical exposure claims,
progression state, answer and judgment model, scoring formula, ranking method, events,
and settings schema. Future rulesets may therefore omit themes, buzzing, fixed question
values, or other SI concepts.

Managers select a game ruleset when creating a tournament and configure the parameters
that ruleset exposes. The tournament type may restrict compatible game rulesets or
parameter ranges. Managers also decide which configurable parameters lobby players
may override. An assigned game stores the effective parameter snapshot so subsequent
manager changes cannot alter it.

Tournament type and ruleset can change during setup and become immutable at setup
finalization. Assigned games always keep their original version and parameters.

## SI ruleset

**SI** is the name of the currently implemented style of game flow. The existing
implementation is only the first game ruleset, not the permanent shape of the game
host.

### Content structure and progression

An SI packet contains ordered themes. Every theme contains one ordered question for
each configured entry in `question_values`. All themes in the same game use the same
ordered positive values. A theme may additionally start with one question whose `value`
is `0`. It follows normal gameplay, including timeouts and appeals, but always changes
the score by zero, regardless of tournament/lobby scoring settings or `minus_multiplier`.
It contributes to no game or tournament tiebreaker.

SI play follows this flow:

1. Assigned players join and a configurable pre-start countdown runs.
2. The selected theme list is announced. Disclosing a logical theme permanently marks
   it exposed for every participant.
3. The host announces each theme and its author, when present.
4. Before each question, the host announces its configured value and reveals the text
   in readable, character-budgeted tokens. Players may buzz during reveal; the normal
   buzz countdown starts after full reveal.
5. The accepted buzzer sees the answer form and receives an answer deadline. An
   incorrect answer or timeout applies the configured negative score and reopens
   buzzing to eligible players. A player cannot attempt the same question twice.
6. The answer and commentary are revealed when the question closes. The host announces
   theme completion and scores after the theme's final question.
7. After the final theme, the host calculates places and results. Appeals may delay
   final settlement under the tournament's snapshotted policies.

SI uses a logical theme as its play unit and claims both that theme and every logical
question it contains. A player cannot play a unit that overlaps a reserved or burnt
claim across their complete bot history. Corrections, packet revisions, and assignment
through a different tournament do not make preserved logical identities fresh again.

Announcing the initial theme list is SI's disclosure boundary. It burns the selected
logical-theme and logical-question claims for every participant. Cancellation before that
announcement releases them. A packet can be reused only when enough themes have no claim
conflict for any participant.

### SI abandonment, appeals, and ranking

Automated answer judgment compares normalized text against the primary and accepted answers:
Unicode NFKC normalization, case folding, accent removal (preserving Russian `й` and `ё`),
punctuation-to-space replacement, and collapsed whitespace. Accepted answers allow one
insertion, deletion, or substitution per four normalized expected characters, capped at
three edits. Both texts must have at least five characters for fuzzy matching, and digit
sequences must match exactly. Thus `центл Памптду` matches `Центр Помпиду`. When an accepted
answer's letters are all Latin, Russian letters in the submission are transliterated before
comparison; reverse transliteration is not applied. Appeals handle disputed judgments.
A question's unaccepted answers (`rejected_answers`, labeled `Незачёт`) are checked first
using exact normalized matching, without fuzziness or transliteration: a matching submission
is incorrect even when it also matches an accepted answer.

If a participant abandons while their answer is pending, SI records an answer timeout. A
participant who leaves during another player's question becomes ineligible for the remaining
attempts on that question until reconnection.

SI finalization waits until all questions and their appeal windows are complete and every
escalated appeal has a decision. Appeal corrections use the snapshotted question value and
`minus_multiplier`:

```text
incorrect → correct correction = value × (1 + minus_multiplier)
correct → incorrect correction = -(value × (1 + minus_multiplier))
```

Overturning a correct answer does not award hypothetical points or replay the question for
other players. When an incorrect answer is accepted, earlier incorrect attempts keep their
penalties, the appealed attempt receives the correct-answer score, and later attempts on that
question are neutralized.

After a rejected appeal, another eligible answer on the same question may be appealed
while its appeal window remains open, including during a pause. Each answer can be
appealed only once; unresolved or accepted appeals still block further appeals on that question.

SI ranks participants by score, then points earned from correct answers before deductions,
then correct-answer counts for every configured value except the lowest in descending-value
order. Players still equal split the occupied places; the tied pairwise rating result is a
draw.

### Scoring parameters

`question_values` is the ordered list of question point values used by every theme in
an SI game. Its default is `[10, 20, 30, 40, 50]`; a tournament may configure another
list such as `[100, 200, 300, 400, 500]`. Each theme must contain exactly one question
at every configured value, optionally preceded by a zero-point question. Zero is not
included in `question_values`.

`minus_multiplier` controls the penalty for an incorrect answer or answer timeout:

```text
correct score change   = question_value
incorrect score change = -(question_value × minus_multiplier)
```

Its default is `1`. For example, with a value of `50` and a
`minus_multiplier` of `0.5`, an incorrect answer changes the score by `-25`.
The multiplier is snapshotted on the game with the other effective SI parameters.

Configured question values are positive, strictly increasing integers up to `1,000,000,000`.
`minus_multiplier` is between `0` and `1,000,000` with at most eight decimal places.
Scores and ledger deltas use `NUMERIC(24, 8)`, so every accepted configuration is
stored without integer truncation.

### Other SI parameters

SI also exposes the pre-start delay, question token size and speed, buzz and answer
timeouts, pause permission, inter-message delays, and other presentation timings.
Defaults are defined by `GameSettings`; each tournament policy snapshots effective values
and player-mutability grants.

A theme may carry optional commentary. When a started theme has commentary, the game
announces the theme name and author, waits `theme_author_to_commentary_delay`, announces
the commentary, then waits `theme_commentary_to_question_delay` before the first question.
A theme without commentary sends no commentary message and uses
`theme_to_first_question_delay` between the theme announcement and its first question.

The SI technical limits currently include at most 12 players and at most 128 themes in
one game. A tournament type may impose narrower limits.

### SI suspicion signals

SI records the fraction of question tokens revealed and, after the normal buzz timer has
started, the fraction of that timer remaining when a buzz is accepted. Its queued suspicion
evaluator processes completed weekly windows and owns three SI-specific signals:

- a player's average revealed fraction at accepted buzzes being substantially lower than the
  average for their established rating cohort;
- buzzes accepted with no more than 10% of the normal buzz timer remaining;
- correct answers on reliably measured questions in the lowest 20% of overall correct rates.

The first signal compares mean reveal fractions; the second compares late-buzz rates among
accepted buzzes. The last compares correct answers among eligible question exposures.
All comparisons use established players within
200 points of the subject's current cross-tournament SI rating. The minimum sample, margin,
and z-score thresholds are documented in
[data-and-statistics.md](data-and-statistics.md). Rulesets added later define their own
signals without changing the global suspicion ledger.

Raised signals snapshot their statistical decision and contributing question actions. These
snapshots retain the algorithm version and values used at evaluation time so later question
statistics do not change the evidence presented to an administrator.

Additional rulesets and compatibility decisions are tracked in [planned features](future-work.md).
