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
