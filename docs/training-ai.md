# Training AI, analysis jobs and entitlements

[Technical index](architecture.md) · [Training mode plan](training-mode-plan.md)

Agent C subsystem of the training mode plan. Modules:
[services/training_ai.py](../src/sitg_bot/services/training_ai.py) (provider
boundary), [services/analysis_jobs.py](../src/sitg_bot/services/analysis_jobs.py)
(durable execution and artifact caching),
[services/entitlements.py](../src/sitg_bot/services/entitlements.py)
(categories, policies, metering). The production Responses provider is configured explicitly;
`FakeAnalysisProvider` serves tests and recorded fixtures. See the
[integration guide](training-knowledge-integration.md) for defaults and placeholders.

## Provider boundary

`AnalysisProvider` exposes three structured operations: `extract_facts`
(dissection), `best_buzz_boundaries` and `estimate_common_knowledge`.
`ProviderSettings` bounds provider, model, timeout, chunk size, output fact
count, concurrency and prompt/schema/tokenizer versions; credentials stay
server-side and out of `redacted()` logging. Packet content and instructions
are data and never grant tools or authority. Packets are scanned in bounded
batches (`packet_chunks`, never splitting a question; oversized questions are
rejected with `SourceTooLarge`). All provider output is validated:
deterministic candidate IDs, question provenance, absolute http(s) links,
full boundary coverage with digests and token ranges, 0–100 estimates.
Uncertain outcomes (`ProviderOutcomeUncertain`) record upstream request IDs
because local idempotency cannot guarantee one provider charge.

## Analysis requests, jobs and caching

`AnalysisService.request(session, owner, source, instructions, idempotency_key)`
authorizes, reserves quota, persists the request and enqueues a durable
`training.analysis` job inside the caller's transaction
(`DurableJobQueue.schedule_in_session`). Requests are owner-scoped and
idempotent; `status`, `result` and `cancel` check ownership. Artifacts are
immutable validated results keyed by kind, source/revision fingerprint,
language, model and prompt/schema/tokenizer versions plus instruction digest.
Only empty instructions share the base artifact; custom instructions and
private sources stay owner-scoped even when content hashes match. Concurrent
requests coalesce onto one computation guarded by a lease; each waiter keeps
its own request, authorization and refund. Cancelling one waiter never cancels
work others need; cancelling the last waiter abandons the computation.

Workers claim jobs with the kind filter (`DurableJobWorker(kinds=...)`), so
slow analysis never occupies gameplay workers. Chunk checkpoints persist
progress and renew leases; retries resume from the checkpoint, provider calls
run outside transactions, and stale completions are fenced by lease tokens.
Failures are never cached as successful empty data: terminal job failure
(lazily reconciled from `status`) fails requests, refunds reservations and
remains retryable via a new generation. `cleanup(before)` prunes terminal
artifacts and requests.

Best buzz boundaries are validated against the application
`announcement_tokens` stream with pinned tokenizer settings
(`question_token_target_chars`), never against provider token indexes.

## Categories, policies and metering

`EntitlementService` keeps categories separate from tournament roles and
subscriptions. Each category references an immutable, versioned
`training_feature_policies` payload for the three features (`dissection`,
`common_knowledge`, `best_buzz`) with `enabled`, `limit` (null = unlimited),
`reset_period` (`none`/`daily`/`monthly`), `cache_hit_charging` and
queued/in-flight request caps. `authorize` reserves quota atomically under a
period row lock and is idempotent per request ID; `settle` records provider
usage separately, releases remainders on partial success, and `release`
refunds failures and cancellations. `capabilities` projects remaining quota
and reset time to both adapters; every limit is enforced server-side, and
feature authorization applies even on cache hits. `reconcile_abandoned`
releases reservations orphaned by crashes; schedule it periodically. Billing
and plan purchase flows are deferred.

## Application integration

All category, ledger, request and artifact records live in shared
`storage/models.py`; migration `0022_training_knowledge_ai` creates them.
`KnowledgeApplication` supplies live packet/fact authorization and automatic fact
application. Separate analysis and gameplay workers use explicit kind filters.
Private retirement emits durable cleanup events; hourly maintenance expires
terminal artifacts after 30 days and reconciles orphaned quota reservations.

The configured `ResponsesAnalysisProvider` uses strict structured outputs with
bounded response size, timeouts and concurrency. It records upstream IDs and token
usage separately from trainee quota. Uncertain outcomes become failed, refundable
requests requiring an explicit retry; no automatic repeated provider charge is assumed.
Credentials and models are server configuration, with no fake production fallback.

## Testing

Run the targeted suites: `tests/unit/test_training_ai.py`,
`tests/unit/test_entitlements_policy.py`, then the PostgreSQL integration
tests `tests/integration/test_entitlements.py` and
`tests/integration/test_analysis_jobs.py` with `TEST_DATABASE_URL`. Coverage
includes fake-provider timeout/malformed-output handling, restart and
checkpoint resume, expired leases and fencing, cancellation, gameplay-worker
isolation, coalescing, cache reuse and isolation, owner scoping, concurrent
quota, rollover, category changes, refunds and crash reconciliation.
