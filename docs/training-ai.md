# Training AI, analysis jobs and entitlements

[Technical index](architecture.md) · [Training mode plan](training-mode-plan.md)

Agent C subsystem of the training mode plan. Modules:
[services/training_ai.py](../src/sitg_bot/services/training_ai.py) (provider
boundary), [services/analysis_jobs.py](../src/sitg_bot/services/analysis_jobs.py)
(durable execution and artifact caching),
[services/entitlements.py](../src/sitg_bot/services/entitlements.py)
(categories, policies, metering). The concrete provider/model is a pending
Stage 0 decision; `FakeAnalysisProvider` serves tests and recorded fixtures.

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

## Integration specification for agent A

Port the records declared on `ENTITLEMENT_METADATA` (6 tables:
`training_categories`, `training_feature_policies`, `training_policy_state`,
`training_category_assignments`, `training_usage_periods`,
`training_usage_ledger`) and `ANALYSIS_METADATA` (2 tables:
`training_analysis_artifacts`, `training_analysis_requests`) into
`storage/models.py` with their named unique constraints, check constraints
and indexes, and add them through one new ordered migration. Foreign keys
reference `players.id`. Configuration should add provider settings (see
`ProviderSettings`) to `config.py`; keep the API key server-side. Register a
second worker in `server.py`:

```python
analysis_worker = DurableJobWorker(
    jobs, {TRAINING_ANALYSIS_JOB_KIND: AnalysisJobHandler(analysis_service)},
    kinds=(TRAINING_ANALYSIS_JOB_KIND,),
)
```

Localization needs stable keys for the error codes
`feature_disabled`, `quota_exceeded`, `queued_cap_exceeded`,
`idempotency_conflict` and the request statuses `queued`, `running`,
`succeeded`, `partially_succeeded`, `failed`, `cancelled`. Tests create these
tables directly in [tests/integration/conftest.py](../tests/integration/conftest.py)
until the migration lands; switch them to Alembic afterwards.

Dissection application, fact statistics and packet access loaders are agent
B's and A's sides of the `AnalysisSourceLoader` protocol; one packet
dissection must not fan out unmetered statistic calls, and every statistic
request routes through the same entitlement metering.

## Testing

Run the targeted suites: `tests/unit/test_training_ai.py`,
`tests/unit/test_entitlements_policy.py`, then the PostgreSQL integration
tests `tests/integration/test_entitlements.py` and
`tests/integration/test_analysis_jobs.py` with `TEST_DATABASE_URL`. Coverage
includes fake-provider timeout/malformed-output handling, restart and
checkpoint resume, expired leases and fencing, cancellation, gameplay-worker
isolation, coalescing, cache reuse and isolation, owner scoping, concurrent
quota, rollover, category changes, refunds and crash reconciliation.

