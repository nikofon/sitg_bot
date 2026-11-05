# Training AI, analysis jobs and entitlements

[Technical index](architecture.md) · [Training](training.md)

Modules:
[services/training_ai.py](../src/sitg_bot/services/training_ai.py) (provider
boundary), [services/analysis_jobs.py](../src/sitg_bot/services/analysis_jobs.py)
(durable execution and artifact caching),
[services/entitlements.py](../src/sitg_bot/services/entitlements.py)
(categories, policies, metering). OpenAI or Charm Hyper Responses is configured explicitly;
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
Call-scoped metadata records the client request ID before transport, replacing it with
`x-request-id` when response headers arrive. Pre-header deadlines therefore retain the
current call's identity, not an earlier call's ID; a client ID alone does not prove dispatch.
Checkpoint, completion and failure accounting use only the current job's calls: zero-call
cache work records no request IDs or token usage. Uncertain outcomes
(`ProviderOutcomeUncertain`) still require explicit retry because local idempotency
cannot guarantee one provider charge.

## Analysis requests, jobs and caching

`AnalysisService.request(session, owner, source, instructions, idempotency_key)`
authorizes, reserves quota, persists the request and enqueues a durable
`training.analysis` job inside the caller's transaction
(`DurableJobQueue.schedule_in_session`). Requests are owner-scoped and
idempotent; `status`, `result` and `cancel` check ownership. Artifacts are
immutable validated results keyed by kind, source/revision fingerprint,
language, provider/model and execution profile plus instruction digest. The versioned
profile includes all non-secret `ProviderSettings` (versions, bounds, timeout and concurrency).
Key rotation does not invalidate it. All analysis kinds and public-base lookups are isolated
by this profile. Pending/checkpointed work with a missing or changed profile fails and refunds
through the existing failure path; submit a new request to recompute. Historical completed
results remain readable with existing authorization, but new requests and Best-buzz preparation
do not reuse old/unprofiled results. No database migration is needed for this change.
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
(`question_token_target_chars`), not a provider's tokenization. The
[playback port](training.md#best-buzz-playback-port) documents the current
packed-token persistence limitation.

## Categories, policies and metering

`EntitlementService` keeps categories separate from tournament roles and
subscriptions. Each category references an immutable, versioned
`training_feature_policies` payload for the three features (`dissection`,
`common_knowledge`, `best_buzz`) with `enabled`, `limit` (null = unlimited),
`reset_period` (`none`/`hourly`/`daily`/`weekly`/`monthly`), `cache_hit_charging` and
queued/in-flight request caps. `authorize` reserves quota atomically under a
period row lock and is idempotent per request ID; `settle` records provider
usage separately, releases remainders on partial success, and `release`
refunds failures and cancellations. `capabilities` projects remaining quota
and reset time to both adapters; every limit is enforced server-side, and
feature authorization applies even on cache hits. `reconcile_abandoned`
releases reservations orphaned by crashes; schedule it periodically. Billing
and plan purchase flows are deferred.

## Administrator AI functionality

Active platform administrators (not tournament managers) use `/api/miniapp/admin/ai`.
The existing session/origin boundary applies; POSTs require CSRF and idempotency headers.
Responses are ordinary HTTP data objects, not the internal gateway envelope.
The existing `/admin/management?section=ai` route resolves to the administrator-only
`{kind:"admin_management",state:"ready",section:"ai",items:[]}` resource. The frontend
then loads settings/provider separately; navigation performs no provider check.

- GET `settings` returns `{version,dissection_enabled,best_buzz_enabled,free,premium,
  reset_period,reset_at,quota_generation}`. Both quota objects contain `dissection` and
  `best_buzz`: integer 0–2147483647, or explicit JSON null for Unlimited. Zero blocks
  charged work; compatible free cache hits retain existing charging behavior.
- POST `settings` accepts all editable fields plus `expected_version`, without
  response-only fields. `reset_period` is `hour`, `day`, or `week`. Windows are UTC,
  half-open: hour on HH:00, day on midnight, week on Monday midnight. `reset_at` is
  the next boundary after a managed-settings save, or null before that first save.
  Before the first save the form inherits existing default-category budgets/enabled
  flags (premium initially the same) and proposes the daily window without claiming
  a next reset; quota resets alone do not activate that proposal. Historical custom
  default none/monthly policies remain effective until explicitly saved.
- GET `players?search=...&limit=20` accepts a literal case-insensitive nickname prefix
  (2–100 trimmed characters) or exact UUID, with limit 1–25. It uses an indexed bounded
  query and returns `{items:[{id,name,tier}],limit}`, ordered by lowercase nickname/ID.
- POST `tier` accepts `{player_id,tier}`, where tier is `free`, `premium`, or `no_ai`
  (display labels Free, Premium, NO AI), returning `{id,tier}`. Unassigned manual tier is
  free. Manual AI tiers have no billing/subscription/tournament-role meaning. NO AI denies
  every new AI authorization, including cache hits and prepared Best-buzz cache consumption.
- POST `reset` requires `{confirm:true,expected_version}` and returns settings. Confirm
  that it grants fresh usage for **all three AI features**, all players/manual tiers/custom
  categories. It changes one quota generation, never scans players or game history, deletes
  data, changes tiers, or lifts NO AI. Already accepted reservations settle/refund exactly
  once against their original generation; they do not consume the newly granted budget.

Explicit saves append immutable policy versions for the managed free/premium theme budgets.
Existing category assignments and Common knowledge policies/caps/cache charging are retained.
Every player without a manual premium/NO AI assignment uses the configured free theme
allowances, including players in custom categories. Managed allowances override only the
theme features' enabled flags, limits and reset periods; category cache charging, request
caps and unrelated features remain unchanged. Manual assignment does not change the stored
category. Tier changes share the same player/feature/window counters,
not a separate tier budget. Changing window selects that UTC bucket; switching back sees its
previous usage. No startup auto-enablement of previously persisted disabled policies occurs.
Shared/exclusive state locks serialize new admissions with updates/reset/tier assignment;
accepted work is neither cancelled nor double-settled by these controls. Existing gateway
request-audit/idempotency infrastructure records these actions; no separate audit system.
Stale settings/reset versions return `stale_write`; invalid values are rejected server-side.

### Passive provider status

GET `provider` never calls upstream. It returns `{provider,model,configured,state,verified_at}`
for the selected server execution profile. Providers remain exactly `openai`/`charm`;
models/keys are operator configuration. States are `unconfigured`, `unknown` (configured
but unverified), `responding`, or `stale` (success at least five minutes old).
`responding` means a recorded successful analysis of this exact profile, **not** guaranteed
current uptime or future success. Recorded outcomes must contain actual provider call
metadata; cache-only artifacts and other profiles are excluded by an indexed lookup.
No public model-list response or arbitrary job failure establishes provider health.
There is no probe action, polling or paid monitoring. Keys and raw response/error bodies
never enter the admin projection. Fake transport tests do not establish live provider
access or quality.

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

The shared `ResponsesAnalysisProvider` selects only the configured provider's fixed endpoint
and uses strict structured outputs with
bounded response size, timeouts and concurrency. It records upstream IDs and token
usage separately from trainee quota. Uncertain outcomes become failed, refundable
requests requiring an explicit retry; no automatic repeated provider charge is assumed.
Credentials and models are server configuration, with no fake production fallback.

## Enabling and live acceptance (operator steps, not automated-test claims)

1. Apply the [required migrations](database-operations.md#schema-baseline) on the intended
   application database before deploying this source. Do not use test database URLs for it.
2. Set the server-only provider, exact model ID and matching private key as described below.
   The analysis worker starts inside `sitg-server`; no separate MCP process or proxy is needed.
   There is no fake provider substitute or cross-provider retry. Leaving key or model unset
   keeps AI unavailable (ordinary training/manual facts still work).
3. Existing disabled administrator policies are not overwritten by configuration. Use the
   administrator **AI functionality** settings to explicitly enable the desired managed
   features/budgets. Custom categories can still be maintained through
   `EntitlementService.create_policy` and their `policy_version`. Verify
   `training/entitlements` reports the intended feature availability, budgets and reset times.
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

### Server configuration and provider evidence

| Variable | Contract |
| --- | --- |
| `SITG_TRAINING_AI_PROVIDER` | `openai` (default when absent/empty), `charm` |
| `SITG_TRAINING_AI_MODEL` | Exact server-selected model ID; no production default or GLM version pin |
| `SITG_TRAINING_AI_API_KEY` | Private key belonging to the selected provider; never a coding-agent credential |

Unknown provider names and malformed settings fail startup clearly, without echoing values.
A recognized `sk-hyper-` key is rejected for OpenAI, including when the selector is absent.
Use the correct selector even for credentials without a recognizable prefix; key ownership
cannot be inferred generally. Endpoints are fixed, not client input or environment overrides:
OpenAI `https://api.openai.com/v1/responses`, Charm `https://hyper.charm.land/v1/responses`.
Redirects are rejected; HTTP/model/auth/billing/schema errors never fall through to another provider.

Evidence checked 2026-10-31 (public documentation/catalog only, no authenticated model call):

- [Hyper Responses](https://hyper.charm.land/docs/api/openai-responses.md) explicitly documents
  a standard Responses pass-through, accepting all standard parameters and referring to the
  OpenAI request/response specification. This supports reusing the existing `text.format`
  `json_schema`/`strict:true` request and output-message parsing, not inferring compatibility
  from the provider's name. Its example omits `status` and uses `prompt_tokens`/
  `completion_tokens`: the adapter accepts that omission only for Charm, rejects explicit
  incomplete/failed statuses, and normalizes those usage aliases.
- [Hyper authentication](https://hyper.charm.land/docs/api/authentication.md): Bearer API keys
  from the Hyper dashboard; [model catalog](https://hyper.charm.land/docs/api/list-models.md):
  public `GET https://hyper.charm.land/v1/models`, use the exact `data[].id`, not display name
  or an agent's provider-prefixed label. At verification, `glm-5.3` was listed; availability
  changes and catalog presence alone does not prove strict-schema behavior for that model.
- [OpenAI structured outputs](https://developers.openai.com/api/docs/guides/structured-outputs?api-mode=responses)
  defines strict JSON-schema output. Choose a supporting model from the
  [OpenAI model catalog](https://developers.openai.com/api/docs/models), for example
  [`gpt-4.1-mini`](https://developers.openai.com/api/docs/models/gpt-4.1-mini).

Examples for the deployment's **private** `.env` (choose one block; set the matching key
privately in `SITG_TRAINING_AI_API_KEY`, not in Git, client state, shell transcripts or logs):

```dotenv
# OpenAI example; an existing key/model-only deployment still selects OpenAI.
SITG_TRAINING_AI_PROVIDER=openai
SITG_TRAINING_AI_MODEL=gpt-4.1-mini
```

```dotenv
# Charm Hyper example for initial GLM testing, not a production recommendation.
# Recheck /v1/models and choose an available exact ID before enabling.
SITG_TRAINING_AI_PROVIDER=charm
SITG_TRAINING_AI_MODEL=glm-5.3
```

For VPS Compose, the three optional variables are forwarded only to `server`, not the bot or
migration service. After applying the schema baseline and setting private configuration,
recreate the server so changed environment values take effect:

```sh
docker compose --env-file .env -f compose.vps.yaml up -d --build --force-recreate server
```

For a non-Compose install, export the three variables privately before starting/restarting
`sitg-server` using your existing service manager (the server does not load `.env` itself).
Provider configuration does not overwrite existing disabled policies: step 3 above is still
required. No migrations, deployment, policy mutation or paid calls are performed by the unit
tests. Deterministic transport/schema tests do not prove live model quality, schema compliance,
account access or availability. An incompatible model fails explicitly, rather than weakening
the schema or substituting a provider/model.

## Testing

For provider/configuration changes (no database required):

```sh
# In each isolated validation copy, if dependencies are missing:
bash scripts/check.sh prepare
.venv/bin/python -m pytest tests/unit/test_training_provider.py tests/unit/test_training_ai_configuration.py tests/unit/test_training_ai.py tests/unit/test_analysis_reconciliation.py tests/unit/test_analysis_cache_routing.py -q
bash scripts/check.sh lint
```

These suites cover both providers' five operations, fixed routing and auth, errors/no fallback,
configuration defaults, Compose's normalized server-only environment, and profile isolation/fencing.
`test_service_deadline_before_headers_retains_current_request_id` covers call identity;
`test_zero_call_cache_work_persists_no_unrelated_provider_ids` covers zero-call accounting.

For persistence/entitlement changes run the targeted suites: `tests/unit/test_training_ai.py`,
`tests/unit/test_entitlements_policy.py`, then the PostgreSQL integration
tests `tests/integration/test_entitlements.py` and
`tests/integration/test_analysis_jobs.py` through the
[shared database lease](database-operations.md#shared-test-database-lifecycle). Coverage
includes fake-provider timeout/malformed-output handling, restart and
checkpoint resume, expired leases and fencing, cancellation, gameplay-worker
isolation, coalescing, cache reuse and isolation, owner scoping, concurrent
quota, rollover, category changes, refunds and crash reconciliation.
