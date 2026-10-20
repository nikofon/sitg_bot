# Backend review handoff

## Selected fixes

- r06: the existing gateway idempotency record retains original successful fact,
  tag, training-session and upload/draft mutation responses. Replays check the
  active Telegram/account identity and ban state, plus live session, packet or
  draft authorization where applicable. A denied replay does not replace the
  successful record. Legacy completion-only records are indeterminate rather
  than executed again. Stop retains its source-revocation exception.
- r07/r13: both artifact lookup paths serialize waiter registration with terminal
  writes. The shared reconciliation boundary now locks and reloads the artifact
  before inspecting its generation/status, including background callers. Account
  locks precede artifact locks in admission and cancellation. Training account
  guards and artifact writers use `FOR NO KEY UPDATE`, preserving serialization
  while permitting foreign-key `KEY SHARE` checks. Dissection reconciliation
  locks packet access, account, then dissection/artifact; background batches use
  separate transactions per item rather than retaining inversely ordered locks.
  Retired cleanup locks the account then current artifacts; retention skips locked
  artifact candidates. Worker error handling cannot fail or expire a later generation.
  Cancellation refreshes its request under the artifact lock before quota changes.
- r08: training text answers no longer match slash commands; ordinary answer
  submission remains routed through training.
- r09: both menu launch paths consume polling's `launch_links` dependency and
  preserve the unconfigured upload/document fallback and private upload scope.
- r10: head assertions target `0025_knowledge_catalogue`; historical migration
  setup uses revision-0022 SQL for players, facts and tags, with a historical SQL
  read after downgrade. Unchanged policy/grant models remain revision-compatible.

- r12: prior analysis is reconciled before deciding whether to reuse a dissection.
  An explicitly retried failed/cancelled dissection becomes pending when its
  replacement is linked; existing applications and saved facts are not reset.
- r14: the gateway integration helper prefixes every metadata key with
  `gateway-retry:`, satisfying the existing eight-character minimum.

## Focused verification scope

Executable unit regressions exercise gateway retries through dispatch and the
existing idempotency boundary, including identity/ban/source revocation, restored
access, legacy markers and changed payloads. Registered-router tests cover command
fallthrough, ordinary answers, and configured/unconfigured launches in both locales.

Disposable-database integration regressions cover real CRUD/tag/upload/session
mutations and both analysis races: an existing artifact and a concurrent insertion
between an absent lookup and insert conflict. They assert persisted terminal waiter
state and settled, non-reserved quota, not merely a successful status projection.
Migration coverage retains the upgrade/copy/policy/downgrade behavior assertions.
These checks require `TEST_DATABASE_URL`; no running/restored database is authorized.

The initial-round command used system `python3` and exited before collection
with `/usr/bin/python3: No module named pytest`; no tests executed in that attempt.
The follow-up below supersedes that interpreter limitation, not the missing
disposable PostgreSQL evidence. No tools were installed or searched for.

## Follow-up verification and baseline scope

This round uses the explicitly supplied task interpreter
`/home/nikofon/.treehouse/SITGBot-b1b67b/5/SITGBot/.venv/bin/python`, with
`PYTHONPATH` rooted at this gate worktree's `src`. Focused behavioral checks cover
stale-generation reconciliation, failed/cancelled dissection retries before and
after reconciliation, preservation of applied facts, obsolete-worker errors and
existing gateway retry behavior. PostgreSQL cases additionally cover the real
private-disclosure/account/artifact/Best-buzz-completion FK path and persisted
quota settlement. No isolated database is configured; skipped database cases
must not be presented as executed concurrency evidence.

The r15 provider implementation was introduced in earlier training integration
`08cd5a9`, already present at pre-redesign `5a9c99b`, and unchanged by redesign
`e8b0a54` or review fix `ce66503`. The provider file is absent at the entire branch
base `15073a0`, so this is pre-existing relative to the selected redesign/review
fixes, not relative to all training-branch history. Its persisted representation
is explicitly deferred and remains unchanged. A no-network behavioral comparison
of those historical/current provider and preparation interfaces is included in
the focused verification, rather than inferring behavior from matching source.

### Follow-up results

One focused verification command used the supplied interpreter with
`PYTHONPATH=src:tests/integration:tests/unit`; the imported project path was confirmed
as this exact gate worktree. It ran only the selected backend files:

- Unit: `test_analysis_reconciliation.py`, `test_training_mutation_retries.py`,
  `test_training_knowledge_adapters.py`, `test_training_adapters.py`,
  `test_knowledge_catalogue.py`.
- Integration: `test_analysis_jobs.py`, `test_training_knowledge.py`,
  `test_training_gateway_retries.py`.

Ten behavioral regressions failed as expected with the relevant historical
`ce66503` methods substituted, demonstrating stale-generation settlement,
unreconciled/reconciled retry failures and obsolete-worker effects. With current
methods restored, **137 passed, 32 skipped**. All PostgreSQL cases remain unexecuted
because `TEST_DATABASE_URL` is unset. In particular, real lock/FK concurrency,
retirement cleanup, persisted automatic retry application and the repaired
end-to-end gateway regression are not claimed as passing persistence evidence.

The no-network r15 probe executed provider conversion, preparation reconstruction
and playback conversion at `5a9c99b`, `e8b0a54`, `ce66503` and the current worktree.
Every version produced the same observable result for `one two three four five`:
provider offset 3, persisted token index 1, reconstructed offset 18 and playback
cursor 4 at target size 3. This confirms the behavior predates the selected
redesign/review fixes; its persisted representation was not changed.

No full-suite, lint, frontend, live Telegram/provider, deployment, PR or CI phase
was run. Final combined delivery and remaining database validation belong to the
outer executor and training task.

## Catalogue and data follow-up: r16–r20

Baseline comparison used the submitted base `15073a0`, pre-redesign `5a9c99b`,
original data commit `e8b0a54`, and accepted fix head `f40c983`. The training
knowledge/analysis modules are absent at the submitted base. Within the training
branch, r17's unchecked execution profile/current-configuration attribution,
r18's unordered statistics batch, r19's bounded identity lookup and r20's unchecked
merge union already existed before the data redesign. They are fixed here only
at the boundaries required by this deliverable, not as unrelated baseline cleanup:

- r16 was introduced by the new card projection/author filter. One owner-scoped,
  draft-backed query supplies lead/theme/question attribution to both consumers.
  Projection remains page-batched; private names have null IDs and are never
  published to global author tables. Both logical-packet and draft ownership and
  training scope are required. The referenced integration-owner commits `18fdd1a`
  and `aa0e612` are unavailable in this worktree's history; no other clone was read.
- r17 uses only the existing four recorded profile fields to reject incompatible
  queued/expired work before computation, with the existing failure/refund path.
  This prevents new model/version misattribution; new statistics use the producing
  artifact's fields. No persisted representation, historical metadata or Best-buzz
  position changes, replay framework or cross-restart compatibility promise is added.
- r18 closes the concrete omitted statistics caller with a deterministic UUID
  ordering before account acquisition, identical to sorted import/grant locking.
  No scheduler, lock abstraction or new reconciliation subsystem is introduced.
- r19 separates exact four-field persisted identity lookup from the bounded history
  list in both stores. The accepted retry path can reach old requests without
  losing application state; history remains limited to 100 rows in PostgreSQL.
- r20 preserves the existing 32-tag limit at the palette/edit round-trip boundary:
  validate the merged union before any mutation, rather than truncating tags or
  retiring a source that cannot be represented by ordinary fact editing.

New executable regressions cover matching/incompatible profiles for all three
analysis kinds, producing-artifact snapshot metadata, old applied identities beyond
100 revisions, and atomic oversized-merge rejection/32-tag edit round trips.
Disposable PostgreSQL cases cover private author projection/search and isolation,
profile refunds, exact persisted identity, and statistics/import/grant account order.
Actual database and concurrency evidence remains unavailable without an isolated
`TEST_DATABASE_URL`. r21 delivery is intentionally untouched; the final integration
owner already owns that solution.

### r16–r20 focused results

The supplied task interpreter imported this gate's `src`. With the historical
`f40c983` method implementations rebound to the same dependency globals, the new
profile, snapshot, old-identity and merge checks produced **27 expected failures,
7 passing controls**. This is behavioral method-level reproduction, not a claim
that an entire historical checkout or database was tested.

The first current-code collection was blocked by the repository's duplicate
unit/integration `test_knowledge_catalogue.py` module names. Reissuing the same
focused selection with `--import-mode=importlib` succeeded: **136 passed,
50 skipped**. Files selected were the unit reconciliation, knowledge service,
catalogue, knowledge adapter and mutation retry files, plus integration analysis,
catalogue, training knowledge and gateway retry files. No source changes were made
between those two current-code collection attempts.

All PostgreSQL cases remain skipped because no isolated `TEST_DATABASE_URL` is
configured. In particular, private-author SQL execution/owner isolation, persisted
profile refunds, exact historical identity and actual statistics/import lock order
are not claimed as executed PostgreSQL evidence. No full-suite, lint, frontend,
live provider/Telegram, deployment, PR or CI phase was performed.

## Frontend delivery dependencies for the outer executor's PR

This backend review does not complete or modify the separately owned frontend.
The combined redesign still depends on that owner delivering and validating:

- r01: Share library popup, reusable/active links, deactivation and authenticated,
  explicitly confirmed recipient import from `#import=TOKEN`.
- r02: existing coloured-tag selection and the nearby New tag popup, rather than
  free-text tag entry.
- r03: grouped training settings, modifier-specific controls and selected-modifier
  summaries, integrated with the approved training-lobby persistence prerequisite.
- r04: searchable/sortable batched packet cards, tournament/author/freshness and
  analysis metadata, and per-packet Dissect options.
- r05: removal of Download from training packet cards, retaining required View APIs.
- r11: preservation of an in-progress answer across hidden-mode presentation polling.

These are explicit delivery dependencies, not backend acceptance claims. The outer
executor owns PR publication and should carry this scope distinction and the actual
verification limitations into the PR evidence.

## Admission regression and deferred identity limitation

The redundant function-local `PlayerRecord` import shadowed the existing module
import before the first account lookup in production `AnalysisService.request`.
Removing that duplicate leaves the locking protocol unchanged. Isolated admission
regressions execute that method for dissection, Best buzz and common knowledge,
including idempotent replay, with fake persistence, entitlement and queue seams.
They require no database, Telegram or live provider. The stranded-waiter integration
fixture now supplies `failed_at` when marking its artifact failed, leaving the
active waiter and quota-settlement assertions intact; PostgreSQL execution remains
the outer executor's responsibility.

The supplied `scope-base-result.txt` and `scope-current-result.txt` under
`/home/nikofon/firstmate/data/sitg-knowledge-data/` both report r23 digests
`67d88afe537b31cf` for the two field arrangements, `r23_same_request: true`, and
`r23_request_count: 1`. MAIN identifies these as original `5a9c99b` and backend
`0a5fff57`, with zero domain diff. This is evidence of the same pre-existing
instructions/comment boundary collision, not a new redesign regression. It remains
an explicitly deferred follow-up: distinct arrangements can reuse a request.
No persisted key format, compatibility handling or data migration is changed here.

## Combined direct-delivery evidence

Both requested windows and their assigned backend corrections are integrated. On
2026-10-20 the user explicitly authorized direct PR delivery after repeated
model-service transport failures. The unavailable no-mistakes model-dependent
validation is **waived for this delivery, not passed**. No attestation or green CI
claim substitutes for that missing evidence.

Deterministic checks executed on the combined source:

- Frontend: 264 tests passed in 28 files; TypeScript typecheck and Vite build passed.
- Focused backend/HTTP/admission/provider/retry/Knowledge checks: 241 passed,
  54 PostgreSQL cases skipped because no isolated test database was configured.
- Full existing unit suite: 1468 passed, eight failures. These are the same eight
  independently reproduced on the untouched base by the backend owner: four admin
  packet-loading fixture cases, two maintenance configuration fixture cases, and
  two Russian question-indicator wording assertions. They remain failures, not
  green checks; unrelated code was not changed to hide them.
- Ruff passes for every changed Python file; whitespace check passes. Alembic has
  one head, `0025_knowledge_catalogue`, following the approved lobby migration 0024.

An older Knowledge HTTP test expected eager packet loading at window bootstrap;
its assertions now exercise the redesigned lazy catalogue endpoint explicitly,
while retaining authentication, filter and mutation checks. Final lint adjustments
only wrap changed lines and order an import; production behavior is unchanged.

Real PostgreSQL migration/concurrency/persistence, browser visual review, signed
Telegram-client acceptance and live provider analysis remain **unverified**. No
running application or database was changed, restarted, deployed or reset.
