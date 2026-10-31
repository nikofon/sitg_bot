# Training mode implementation plan

Scope: Telegram and the Mini App in this repository. Start with solo training;
retain a path to group training. The independent website is outside this plan.
Use **trainee** for training interactions. Existing account identifiers such as
`player_id` remain unchanged. Agent A's current backend and adapters are described
in [training](training.md); shared ports are in [contracts](training-contracts.md).

## 1. Shared foundation and decisions

### Stage 0: agree contracts before implementation

Three agents own the following workstreams. Agent A coordinates integration;
this does not require a fourth agent.

| Agent | Responsibility | Primary new modules, proposed |
| --- | --- | --- |
| A | Training execution, modifiers, private packets, navigation, shared integration | `domain/training.py`, `services/training.py`, `services/training_packets.py`, `bot/handlers/training.py`, `web/src/ui/training.ts` |
| B | Facts, tags, sharing, statistics, dissection application | `domain/knowledge.py`, `services/knowledge.py`, `services/knowledge_statistics.py`, `bot/handlers/knowledge.py`, `web/src/ui/knowledge.ts` |
| C | LLM integration, analysis jobs/caches, category entitlements and metering | `services/training_ai.py`, `services/analysis_jobs.py`, `services/entitlements.py`, corresponding tests |

Keep existing domain/service/adapter boundaries. Reuse SQLAlchemy/PostgreSQL,
aiohttp, typed gateway operations, authentication, durable jobs, and transactional
outbox delivery; no new infrastructure dependency is initially needed.

The important existing constraints are:

- `GameRecord` requires tournament/type/policy references. Introduce narrow
  training session records rather than fake tournaments or a redesign of games.
  Reuse `announcement_tokens`, answer matching, packet models and small pure
  progression helpers where practical; avoid duplicating tournament execution.
- Library access is distinct from tournament play permission. Use existing
  readable/release gates and disclosure confirmation for training and dissection.
  Already burnt content remains replayable in training; normal freshness,
  reservation conflicts, and permanent exposure rules remain authoritative.
- Import drafts require a creation tournament. Private uploads need explicit
  training scope rather than an invented tournament assignment.
- Navigation and database checks currently enumerate `player`, `manager`, and
  `admin`. Add `training` consistently, preserving administrator access and bans.

Resolve these product questions before their dependent stages, recording choices
here instead of letting agents independently choose behavior:

1. Whether fresh content becomes burnt at library confirmation or first training
   disclosure; recommend existing library confirmation semantics. Define handling
   of access revoked during a session and active training versus normal play.
2. Reversed text granularity; hidden-text delay, trigger and redisplay controls;
   allowed modifier combinations, especially reversed text with Best buzz.
3. Sharing and reusable public-link behavior are defined in the
   [knowledge integration guide](training-knowledge-integration.md#coloured-tags-durable-links-and-packet-cards).
4. LLM provider/model, population/language assumptions for Common knowledge%,
   category limits/reset periods, cache-hit charging, and private upload limits.
5. Bingo examples/calibration and the intended era in “before 1500 A.C.”; preserve
   custom instructions verbatim until ambiguous dates are clarified.

These decisions can remain pending while independent implementation proceeds.
For Agent A, the agreed policies are library-confirmation exposure, live permission
rechecks, and no simultaneous active training and normal play. Reverse word order;
show hidden-mode text immediately for a trainee-configured duration, then hide it
for the remaining buzz timer. Hidden and reversed may combine; Best buzz excludes
all other modifiers for now.
Group training, exact commercial tiers and normal-game modifier availability are
deferred. The interfaces must support later decisions without implementing them now.

Current sharing behavior, quota defaults and unresolved product inputs belong in the
[integration guide](training-knowledge-integration.md); provider configuration belongs in
[Training AI](training-ai.md).

### Stage 1: freeze the parallel-development boundary

Agent A owns edits to shared files: `storage/models.py`, migrations,
`application/contracts.py`, gateway dispatch, `server.py`, `config.py`,
`miniapp_http.py`, navigation/router registration, browser routes/API types/client/
`app.ts`, and EN/RU catalogs. B and C provide schema, operation and localization
specifications; A integrates these centrally. C owns narrowly required changes to
`services/reliable_delivery.py`. Each agent otherwise edits its own new modules
and tests in an isolated branch/worktree.

Freeze these interfaces together, including authorization and stable errors:

| Interface | Producer → consumer | Required contract |
| --- | --- | --- |
| Packet access | A → B/C | Authorized immutable packet/question revisions, content fingerprint, private/shared scope, disclosure receipt |
| Analysis | C → A/B | `request(kind, source, instructions, idempotency_key)`, owner-scoped status/result, cancellation; kinds: dissection, best-buzz, common-knowledge |
| Dissection application | B ← C | Versioned fact candidates, authoritative source links, question provenance, deterministic candidate IDs |
| Best buzz | A ← C | Question revision/text digest, validated source boundary, tokenizer/schema version, unavailable/error state |
| Entitlements | C → A/B | Allowed features, remaining/reset information, atomic reserve/settle/release; server-authoritative decisions |

Requests return job references promptly. Common job projections include queued,
running, succeeded, partially succeeded, failed and cancelled states, progress,
retryability and permission/quota errors. Versioned gateway mutations require
idempotency and expected versions where appropriate. Module dependencies use
these interfaces and fixtures, avoiding circular service imports.

## 2. Agent A: training and private packets

### A1. Persistence, access and private import

Add training sessions, pinned packet/question revisions, trainee membership,
progress, attempts, settings snapshots, state versions, deadlines and presentation
cursors. Enforce one trainee initially; a membership relation leaves room for
group training. Keep training results outside competitive ratings, suspicion,
normal-game statistics, tournament results and matchmaking.

Extend drafts/logical packets with explicit scope and ownership constraints:
tournament drafts retain their required creation tournament; training drafts
require an uploader and no tournament. Training publication creates immutable
versions without tournament assignments. Reject attempts to assign private
packets through existing management paths. Reuse DOCX/PDF/JSON parsing,
validation, preview/confirmation, checksums, size limits and `asyncio.to_thread`.
Validate against training ruleset settings without tournament defaults.

Only the uploader may list/read/download/train/dissect their private packets.
Apply scope checks to existing direct-ID, author, administrator and export paths
that could expose content. Duplicate warnings must not reveal another trainee's
private packet metadata. Deletion retires access while preserving pinned history;
define cache/job cleanup alongside retirement. Sharing facts grants no packet rights.

**Exit:** targeted PostgreSQL tests prove owner isolation, no assignments, immutable
revision pinning and unchanged tournament upload behavior.

### A2. Solo session execution

Add Training navigation with **Train**, **Knowledge library**, and **Upload
training packets**. Enforce permissions in services, including existing ban rules;
changing modes grants no authority. Preserve mode/recovery state across restarts.

List authorized library packets and private uploads, including previously played
packets. Do not apply the normal fresh-only selector. Starting training repeats
access/disclosure checks and pins revisions/settings atomically. Recheck access
at later protected reads according to Stage 0 policy. Dissection uses the same
access boundary, including exposure confirmation even on cache hits.

Implement question selection/progression, incremental reading, buzz, answer
submission/judgment, answer reveal, skip, pause/resume, stop and replay. Reuse
existing deterministic answer evaluation; LLM calls are unnecessary for judging.
Persist progress/deadlines and deliver through the outbox. Reconnection restores
the authoritative question state. Apply the agreed cross-mode active-session rule
in both training and normal-game entry paths.

**Exit:** a trainee can finish and replay a packet; restart and stale-command tests
pass; competitive ledgers/statistics are unchanged.

### A3. Display modifiers and Best buzz playback

Create a small modifier registry with versioned parameters, supported interaction
modes, compatibility checks and a deterministic display pipeline. Separate text
transforms from reveal/pause/hide behavior. Published question text stays immutable.
Add policy-gated hooks in normal-game settings and display projections using the
same registry/pipeline. Default them to disabled until product design selects
which modifiers tournaments may enable.

Implement reversed and hidden modes after Stage 0 specifies semantics. Persist
hidden deadlines; recovery must not redisplay expired text. Escape transformed
text before Telegram/HTML formatting.

Best buzz preparation comes from C. Map its validated source boundary to the
actual `announcement_tokens` stream using pinned tokenizer settings, rather than
assuming provider token indexes equal application tokens. Pause once at that
boundary; keep buzzing available. Attach an inline **Continue reading** button
to the question message. Resume continues from the next token without repeatedly
pausing. Authenticate callbacks and reject stale session/question versions.
Define missing-analysis and incompatible-modifier behavior explicitly; show
preparation/failure status without silently substituting an invented boundary.

**Exit:** deterministic boundary, hidden deadline, modifier combination, duplicate
callback and reconnect tests pass in backend and Telegram presentation coverage.

### A4. Adapters and integration ownership

Wire B's knowledge views and C's job/entitlement projections into Telegram and
Mini App routes. Training gameplay must expose the same service behavior in both
adapters. Centralize shared registrations, localized labels, errors and capability
discovery. Ship backend capabilities before frontend entry points.

## 3. Agent B: knowledge library

### B1. Facts, tags and sharing

Model an owned knowledge collection, facts, ordered source links, trainee-created
tags, fact/tag associations, copy lineage and statistic snapshots. Facts contain
title, description, links, statistics and tags. Store provenance separately from
editable text. Index ownership, normalized tag names and pagination keys; enforce
tag ownership and association uniqueness.

Implement manual create/read/update/delete, tag management, search/filter/sort and
paginated lists. Trainees fill all content fields; statistics are server-managed
and computed asynchronously. Content changes version facts and invalidate relevant
statistics without deleting historical snapshots. Editing only tags need not
repeat statistical analysis.

Sharing creates permanent recipient-owned snapshots with independent tags and
available statistics. Repeated sends add new facts without overwriting prior copies.
Never share private annotations or packet question/answer text through source metadata.
Copies grant no source access. Use optimistic concurrency for owned edits.

**Exit:** CRUD, tag isolation, stale edits, permanent copy ownership and independent edits pass
targeted service/HTTP and PostgreSQL tests.

### B2. Automatic statistics

Provide a GotQuestions connector behind a replaceable interface. The
[official FAQ](https://gotquestions.online/faq) describes phrase and morphological
search; first verify a supported programmatic access method and response/count
semantics. Do not assume an undocumented public API exists or bypass throttling.
Use recorded fixtures until this integration is established.

Define Mentions as distinct matching question IDs, deduplicated across aliases
and pages, with query/matching-policy version and retrieval time. Distinguish a
fact/entity match from an ambiguous title occurrence; retain query provenance and
sample matches for inspection. Never present a capped/partial count as complete.
Record corpus size/date where available for subsequent frequency normalization.

Request Common knowledge% through C using fact content, language and agreed
population assumptions. Validate 0–100; label it an LLM estimate and retain
model/prompt/version/time metadata. A limited category may receive pending or
unavailable statistics; manual fact creation must still succeed.

Define a versioned Bingo scoring interface consuming Mentions/frequency and Common
knowledge%. Do not invent the final percentage formula. Await the supplied bingo
examples, calibrate against them, and test increasing mention frequency and
decreasing common knowledge produce the intended ordering. Handle zero knowledge,
missing corpus counts and small samples explicitly. Until calibrated, display
“pending calibration”; unknown, denied and failed values are never zero.

**Exit:** connector fixtures, pagination/deduplication, ambiguous matches, nullable
states and statistic invalidation tests pass without live provider calls.

### B3. Packet dissection application

Let trainees select an authorized packet and optionally provide prompt-style
instructions. Preserve the instruction text, source revision and request identity.
Extraction now stages private review candidates rather than automatically saving facts;
see [review and approval](training-knowledge-integration.md#configuration-and-persistence)
for the authoritative application contract.

Comments create a new dissection revision/job; they never mutate a shared base
artifact. Support exclusions and requests for additional historical detail.
Fact edit/delete compatibility is specified in the
[integration guide](training-knowledge-integration.md#coloured-tags-durable-links-and-packet-cards).
Do not silently remove manually maintained facts. Track partial application and
allow retry of failed work without reapplying successes. Schedule statistics as
separate bounded jobs so extraction need not wait for every metric.

**Exit:** base/custom instructions, selected-candidate approval, duplicate suppression,
manual-edit preservation and partial-retry tests pass.

### B4. Presentation

Deliver isolated Mini App views and Telegram handlers for facts/tags, sharing,
dissection instructions/progress and statistics. Show loading, quota, unavailable,
estimated and error states. Supply route, operation and EN/RU catalog additions to
A for shared-file integration. Paginate facts and load details only when opened.

## 4. Agent C: AI, caching and user categories

### C1. Provider boundary and durable execution

Implement provider-neutral structured operations for extraction, Best buzz and
Common knowledge estimation. Best buzz scans the packet in bounded batches and
returns a boundary for each question. Keep credentials server-side; configure provider,
model, timeout, output limits and concurrency. Bound packet chunks and output
fact counts. Validate schemas, revision IDs, source boundaries and URLs; packet
content/instructions are data and cannot grant tools or change authorization.

Extend existing jobs narrowly: enqueue within the caller's transaction, and filter
claims by job kind for separate gameplay and analysis workers. The current worker
claims general batches and executes sequentially; slow analysis must not occupy
gameplay deadlines or be claimed by workers without its handlers. Share existing
queue infrastructure, not a second broker. Use async network I/O and bounded
offloading for CPU work; hold no transaction/row lock during provider calls.

Persist chunk checkpoints, bounded retries/backoff, cancellation and progress.
Provider deadlines must fit leases, or renew leases safely. Fence stale completions.
Record upstream request IDs and uncertain outcomes: an external call can be billed
before a crash, so local idempotency alone cannot guarantee one provider charge.

**Exit:** fake-provider timeout, malformed output, restart, expired lease,
cancellation and gameplay-worker isolation tests pass.

### C2. Persistent analysis caching

The implemented cache contract, including public tag-coverage reuse, LLM-decided
commentary routing, private-request isolation, leases and cleanup, belongs in
[Training AI](training-ai.md#analysis-requests-jobs-and-caching).

**Exit:** simultaneous base requests produce one computation; custom/private
requests remain isolated; version changes, revocation and retries behave correctly.

### C3. Configurable categories and usage accounting

Add category assignments, versioned feature/limit policies, usage periods and an
append-only reservation/settlement ledger. Support feature disablement, finite or
unlimited allowances and separate budgets for dissection, Common knowledge and
Best buzz. Account categories are separate from tournament roles/subscriptions.
Keep numerical limits/reset rules configurable; defer billing and plan
purchase flows.

Atomically authorize, reserve quota and persist the request/job. Associate ledger
entries with idempotent request IDs, preventing concurrent overspend and duplicate
charges. Settle successful work and release failed/cancelled unused reservations;
record actual provider usage/cost separately. Specify partial-success charging,
ambiguous provider outcomes and reset/category-change behavior. Reconcile abandoned
reservations after crashes. Cap per-account queued/in-flight work as well as usage.

Apply feature authorization even on cache hits; cache-hit charges follow policy.
One packet dissection must not fan out unlimited unmetered statistic calls. Return
capabilities/remaining quota to both adapters; enforce every limit server-side.

**Exit:** concurrent quota, retries, cache hits, period rollover, category changes,
failure/refund and crash-reconciliation PostgreSQL tests pass.

## 5. Parallel delivery and release gates

1. **Foundation:** A integrates agreed schemas/contracts and ordered migrations;
   B builds fact logic/UI against fixtures; C builds provider/job fixtures and
   category policy. Assign one migration chain; never edit historical migrations.
2. **Independent slices:** A completes uploads/solo training/modifiers; B completes
   manual knowledge/sharing; C completes jobs/caches/quotas. Merge tested additive
   slices while preserving disabled capabilities for unfinished features.
3. **Integration:** connect C's Best buzz to A and C's extraction/estimates to B.
   A wires adapters; B verifies fact application; C verifies metering and recovery.
4. **Release:** run targeted regression and migration checks, then enable features
   incrementally. Keep non-AI training/manual facts available during provider outages.

For packet lists, follow [existing loading practices](packet-loading.md): 25-card
pages, stable cursors, filtering before expensive work, batched metadata/permissions,
the shared background limiter, cancellable/debounced browser loading and lazy details.
Training and Knowledge packet-card metadata and batching are specified in the
[integration guide](training-knowledge-integration.md#coloured-tags-durable-links-and-packet-cards).
Keep permission checks live; avoid caching grants.

Each agent starts with targeted unit tests, then relevant disposable-PostgreSQL
integration tests and browser/adapter tests. Add query-count regression checks proving
bounded reads per page without per-card queries. Benchmark cold/warm listing,
payload size, CPU and event-loop latency under simultaneous AI work; verify cache
reuse eliminates repeated calls without exposing content. No full suite is needed
unless integration failures justify it.

Final acceptance covers owner-only uploads, authorized replay, restart recovery,
modifier behavior/inline resume, automatic fact statistics, library sharing,
base/custom dissection, cache reuse/isolation, and concurrent quota enforcement.
Check normal packet access, exposure, gameplay and rating regressions using the
existing targeted tests. Update owning subsystem guides and link this plan from
`future-work.md` during implementation. Keep unresolved calibration/limits explicit.
