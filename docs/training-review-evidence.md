# Backend review handoff

## Selected fixes

- r06: the existing gateway idempotency record retains original successful fact,
  tag, training-session and upload/draft mutation responses. Replays check the
  active Telegram/account identity and ban state, plus live session, packet or
  draft authorization where applicable. A denied replay does not replace the
  successful record. Legacy completion-only records are indeterminate rather
  than executed again. Stop retains its source-revocation exception.
- r07: both artifact lookup paths lock the shared artifact through waiter
  registration. Terminal writes, stale-job reconciliation and cancellation use
  the same serialization boundary. Cancellation refreshes its request after
  acquiring that lock, before changing status or settling quota.
- r08: training text answers no longer match slash commands; ordinary answer
  submission remains routed through training.
- r09: both menu launch paths consume polling's `launch_links` dependency and
  preserve the unconfigured upload/document fallback and private upload scope.
- r10: head assertions target `0025_knowledge_catalogue`; historical migration
  setup uses revision-0022 SQL for players, facts and tags, with a historical SQL
  read after downgrade. Unchanged policy/grant models remain revision-compatible.

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

The focused command was attempted with `PYTHONPATH=src:tests/unit:tests/integration
python3 -m pytest -q`, selecting the two retry regression files, training adapters,
application gateway, analysis jobs, copy-sharing migration and the three affected
initial-schema migration cases. It exited before collection with
`/usr/bin/python3: No module named pytest`. The `pytest` executable is also absent
from PATH. No tests executed, and no passing or skipped-test evidence is claimed.
`TEST_DATABASE_URL` is unset, independently blocking PostgreSQL verification.
No tool installation or filesystem search was attempted. The outer executor must
run these checks in a configured environment. No full-suite, lint, live
Telegram/provider, deployment, PR or CI checks were performed in this phase.

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
