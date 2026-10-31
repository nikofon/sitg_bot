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

`AnalysisProvider` exposes structured `extract_facts` (dissection),
`decide_cached_facts`, `transform_cached_facts`, `best_buzz_boundaries` and
`estimate_common_knowledge` operations. Extraction/transformation assign only explicitly
requested tag names; authorship is resolved from pinned source records, never model output.
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
Only bare public results share reusable artifacts. Public dissection cache v2 is scoped
to provider identity, exact immutable source/revision/question/theme coverage, language,
model and prompt/schema/tokenizer profile, with explicit tag coverage. Partial theme
selection cannot hit a whole-packet artifact or vice versa. A partial index introduced
by `0030_public_analysis_cache` bounds eligible public source/profile lookups.

Routing is server-side: compatible bare same/subset-tag hits make zero provider calls;
new tags transform only cached facts. Commentary first invokes a strict structured LLM
`requires_source` decision using cached facts and verbatim commentary. A false decision
transforms cached facts; true triggers a full scan of only the selected source. There is
no keyword heuristic. Cached transformations have no packet/question text in their payload;
question provenance, tag names, output bounds and references are revalidated. Existing
public bare artifacts are immutable; commented results are owner-private and never eligible
as public bases. The decision and transformed output checkpoint under the normal compute
lease, avoiding repeated completed steps after restart.

Private uploads never reuse a cross-request analysis artifact, even for the same owner;
the idempotency key salts the compute identity. Same-request recovery checkpoints and
prepared Best-buzz records remain durable, but do not count as reusable cache hits.
Provider schema/prompt defaults are v2; Best-buzz playback payload shape is unchanged
and the adapter explicitly accepts compatible v1/v2 producing artifacts. Concurrent
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
`storage/models.py`; see [schema setup](database-operations.md#schema-baseline).
`KnowledgeApplication` supplies live packet/fact authorization and persisted review
candidates. Only explicit selected-candidate approval adds facts. Separate analysis and gameplay workers use explicit kind filters.
Private retirement emits durable cleanup events; hourly maintenance expires
cached results/request records after 30 days and reconciles orphaned reservations.
Saved facts/copies, packet uploads and pinned training sessions remain intact.

Default budgets, reset times and request caps are recorded in the
[integration decisions](training-knowledge-integration.md#integration-decisions).

The configured `ResponsesAnalysisProvider` uses strict structured outputs with
bounded response size, timeouts and concurrency. It records upstream IDs and token
usage separately from trainee quota. Uncertain outcomes become failed, refundable
requests requiring an explicit retry; no automatic repeated provider charge is assumed.
Credentials and models are server configuration, with no fake production fallback.

## Enabling and live acceptance (operator steps, not automated-test claims)

1. Apply the [required migrations](database-operations.md#schema-baseline) on the intended
   application database before deploying this source. Do not use test database URLs for it.
2. Configure the existing server-only `SITG_TRAINING_AI_API_KEY` secret and
   `SITG_TRAINING_AI_MODEL` structured-output-capable model. The analysis worker starts with
   the application server; no fake provider substitutes for missing configuration.
   For `compose.vps.yaml`, set both values in the deployment's private `.env` before
   creating or recreating the `server` service. Compose forwards these optional variables
   only to `server`, not to the Telegram bot or migration service. Leaving either value
   unset keeps AI unconfigured; never print or commit the API key.
3. Existing disabled administrator policies are not overwritten by configuration. Enable the
   desired category using `EntitlementService.create_policy` and its `policy_version` explicitly.
   Verify `training/entitlements` reports enabled features and the intended daily budgets.
4. With authorized test accounts, preview/confirm one theme, select tags and start dissection.
   Close/reopen the window, verify the durable pending review, then approve only selected facts.
   Confirm author attribution, source privacy and that unselected themes remain undisclosed.
5. For the same authorized public source/coverage, repeat with compatible/subset tags, new tags,
   cache-sufficient commentary and source-required commentary. Inspect recorded provider calls,
   usage and supplied payloads; the LLM, not a keyword list, chooses commentary routing.
   Repeat a private upload with a new request identity and verify it recomputes rather than hits
   reusable cache. Do not infer real-provider quality/cost from fake-provider test counts.

Credentials, operator policy changes, deployed browser/Telegram delivery and real-provider
smoke acceptance remain explicit deployment prerequisites; implementation tests make no live calls.

## Testing

Run the targeted suites: `tests/unit/test_training_ai.py`,
`tests/unit/test_entitlements_policy.py`, then the PostgreSQL integration
tests `tests/integration/test_entitlements.py` and
`tests/integration/test_analysis_jobs.py` through the
[shared database lease](database-operations.md#shared-test-database-lifecycle). Coverage
includes fake-provider timeout/malformed-output handling, restart and
checkpoint resume, expired leases and fencing, cancellation, gameplay-worker
isolation, coalescing, cache reuse and isolation, owner scoping, concurrent
quota, rollover, category changes, refunds and crash reconciliation.
