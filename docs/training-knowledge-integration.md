# Training knowledge and analysis integration

B's knowledge and C's analysis/entitlement services now use shared SQLAlchemy
metadata and migrations through `0026_knowledge_images` (following
`0025_knowledge_catalogue` and `0024_training_lobby`). `PostgresKnowledgeStore`
implements B's repository; `KnowledgeApplication` connects persistence, source
access, automatic fact application, statistics and Best buzz.

## Integration decisions

- Sharing sends permanent, independently editable copies of the current collection
  to the recipient's own library. Later edits/deletions never propagate. Repeating
  a send adds only uncopied facts; it never overwrites or resurrects prior copies.
  Durable recipient/content-fingerprint claims also deduplicate equivalent facts
  arriving through different sources or sharing links, including after editing or
  deleting the imported copy. Existing copies are preserved by migration.
  Tags and available current statistics are copied without new AI calls. Copies
  grant no packet rights and expose no private source metadata.
- The user's confirmed policies apply: library-confirmation exposure, live access
  checks, and no simultaneous normal play and active/paused training. Reverse
  means word order; hidden text appears immediately for a configurable duration,
  then disappears until reveal. Hidden/reverse combine; Best buzz excludes both.
- Private analysis caches are owner-scoped. Instructions/comments remain verbatim,
  including whitespace and ambiguous dates such as “before 1500 A.C.”.
- Production uses configurable OpenAI Responses with strict structured outputs.
  No model or credential is assumed. Without configuration, ordinary training,
  manual facts, sharing and uploads remain usable; AI is unavailable.
- The basic free plan allows 24 themes for dissection and a separate 24 themes for
  Best buzz per trainee per day; Common knowledge remains 50 estimates per day.
  Packet requests reserve their distinct theme count, settle that count on success,
  and refund it on failure/cancellation. Over-budget packets are rejected atomically.
  Reset: midnight UTC. Cache hits are free, with feature authorization still
  enforced. Each feature allows 3 queued requests and 2 concurrent computations;
  coalesced waiters count as one computation. Custom categories are preserved;
  former free defaults migrate to 24 themes. Recorded usage is not repriced.
- Common knowledge is an LLM estimate for adults in the relevant language
  community, rather than quiz specialists. Packet language takes precedence;
  otherwise use trainee locale. Manual facts use their owner's locale.
- Analysis accepts at most 16 chunks of 12,000 characters, never splitting
  questions, and at most 200 extracted facts. Calls have a 120-second deadline.
  A separate worker uses an hour-long job lease. Gameplay workers claim only
  their registered kinds.
- Completed request/status records are pruned after 30 days; old cached AI results
  are removed once no surviving request references them.
  Saved facts/copies, uploads, training history and pinned session boundaries remain.
  Expired preparations require recomputation and can use quota again. This storage
  management default remains unchanged. Hourly reconciliation refunds orphan
  reservations older than a day, excluding active requests. Private retirement
  denies reads immediately and emits a durable event for payload/checkpoint cleanup.
- Mentions is unavailable until supported GotQuestions access is established.
  Bingo is `pending_calibration`; missing values never become zero. Extracted
  source URLs must already occur in the pinned source.

## Configuration and persistence

Run `alembic upgrade head`. Set these on the application server:

```text
SITG_TRAINING_AI_API_KEY=<provider credential>
SITG_TRAINING_AI_MODEL=<chosen structured-output-capable model>
```

Credentials stay server-side. Production never substitutes the fake provider.
The implementation follows the official
[structured outputs documentation](https://developers.openai.com/api/docs/guides/structured-outputs?api-mode=responses).
Tests make no live provider calls.

Default policies are installed once. For an existing disabled installation, create
a policy with `EntitlementService.create_policy` and update the selected category's
`policy_version`; configuration does not replace administrator choices. Policies
support disabled, finite/unlimited, daily/monthly/no reset and cache-hit charging.

Facts keep content and provenance in separate fields. Ordered links, provenance,
application outcomes and immutable statistic snapshots use JSONB; tags and
ownership-enforced associations are relational. SQL filters, keyset cursors and
batched tags/statistics bound fact pages to 25 cards without per-card reads.

Dissection applies automatically after another source-access check. Deterministic
candidate mappings prevent duplicates, manual overwrites and resurrection of
deleted/merged imports. Follow-up comments create another analysis revision.
Statistics run separately with quotas; unavailable metrics never prevent saving facts.

## Coloured tags, durable links and packet cards

Tags persist a normalized six-digit RGB `colour` (`#64748b` for existing tags).
`GET /api/miniapp/knowledge/tags` returns `{items:[{tag_id,name,colour}]}`.
The existing `tag_create`/`tag_rename` POSTs accept `colour`; omitting colour
on rename preserves it. Fact detail tags add `colour` while fact lists keep
`tag_names`. Fact create/update still accept selected `tag_names` for compatibility.
The Mini App should select from this palette, with explicit new-tag creation.
Fact search accepts repeated `tags` query parameters and matches **all** selected
normalized tags (at most 32); legacy `tag` intersects them. Fact merging is removed
from HTTP, gateway and services. Historical merge tombstones remain intact.

Facts retain internal content versions for stale-write protection. List cards now
include description, links, images and the version without separate detail reads.
Original dissection facts expose `packet_name` only while their source remains
readable; copied facts do not inherit packet metadata or source access.

Images are immutable persisted raster attachments, not URLs embedded in fact text
or source links. POST `/api/miniapp/knowledge/image_upload` with `{source_base64}`
uses normal session, CSRF and idempotency protection and returns `{id,url,content_type}`.
PNG/JPEG/WebP headers and dimensions are checked; uploads are limited to 2 MiB and
20 million pixels. SVG and remote fetching are unsupported. Fact create/update
accepts up to eight distinct `image_ids`; omission preserves existing images and
an empty array removes them. GET `/api/miniapp/knowledge/images/{image_id}` checks
live uploader or owned-fact reference access, and serves raster bytes with
`no-store`/`nosniff`. List/detail `images` contains `{id,url,content_type}` metadata,
never image bytes. Permanent copies retain immutable attachments independently of
source edits/deletion and grant no packet metadata. Apply migration
`0026_knowledge_images` before deploying this API.

`GET /api/miniapp/knowledge/sharing` retains historical `grants` and `shared`
copy summaries and adds the creator's `links`. POST `knowledge/link_create`
with `{}` creates a reusable multi-recipient link. POST `knowledge/link_deactivate`
with `{link_id}` deactivates only a link owned by the actor. There is no expiry,
recipient binding or single-use consumption. Repeating a creation idempotency key
returns its original link even after deactivation. Every link exposes
`{id,token,url,created_at,deactivated_at,active}` only to its creator.

The HTTP response's share `url` is
`https://t.me/<actual_bot_username>?start=kl_TOKEN`. The existing cached Telegram
notifier resolves the actual bot identity; no Mini App short name is assumed.
The bot's `/start kl_TOKEN` handler sends an inline WebApp launch button to
`<MINI_APP_BASE_URL>/knowledge?_launch=1#import=TOKEN`, preserving a deployment
prefix. Opening this button in Telegram supplies signed initData to the existing
session bootstrap. Registration-required recipients retain the import launch URL
in FSM state and receive the button when registration completes. The bot never
imports facts itself. After authentication the frontend presents confirmation and
POSTs `{token}` to `knowledge/import` using the existing CSRF/idempotency headers.
No GET imports data. The token stays in the WebApp URL fragment, outside server
HTTP access logs/referrers. The HTTP response also includes `app_url` (the relative
in-session fragment route) and `launch_available`. If bot identity is unavailable,
`url` is null and `launch_available` is false: the UI must not offer the plain
HTTPS fallback as an externally working launch link. This uses the existing bot
notifier/token and HTTPS `MINI_APP_BASE_URL` configuration, not new BotFather setup.
The import response is `{owner_id,recipient_id,imported_count,skipped_count}`. Unknown/deactivated links
fail without copying; self-import is rejected. Creator account locks and a locked
link recheck serialize revocation against import. Sorted two-account locks and the
unique `(recipient_id,content_fingerprint)` claim make concurrent imports safe.
Copies retain separate source/owner/recipient provenance rows; sharing grants no
packet entitlement. Deactivation never removes or revokes permanent copies.
The legacy `revoke` operation still rejects revoking permanent copies.

Both Mini Apps use `GET /api/miniapp/training/packets`, which keeps the existing
readable-packet predicate (own unretired uploads and authorized published tournament
versions). It adds regular catalogue-style literal case-insensitive `name`/`author`
filters and inclusive `year_from`, `year_to`, `publication_from`, `publication_to`.
Orders are `id_asc` (legacy default), `name_asc`, `name_desc`, `year_asc`, `year_desc`;
unknown years sort as 0 and UUID breaks ties. `owner_uploads_only=true` intersects
readability with private training scope and uploader=caller before pagination;
this filter is shared by Knowledge and the training lobby. Preserve opaque `next_cursor` values
and reset them on filter/order changes. Legacy UUID cursors remain valid with
`id_asc`. Pages remain at most 25 and use the shared background batch pacer.

Each card retains version `id`, logical `packet_id`, name/year/scope and adds:

- `authors:[{id,name}]`: batched lead/content author attribution. Own private
  uploads include lead, theme and question names from the owner-authorized draft,
  with `id:null`; these names also participate in author filtering but never enter
  public author catalogues.
- `tournaments:[{id,name}]`: only currently readable associations for this user.
- `total_theme_count`, `fresh_theme_count`: existing SI content-adapter freshness,
  including reserved/burnt question/theme identities and user packet blocks.
- `dissected`: this user has a successful/partial applied dissection for this version.
- `dissected_theme_count`: distinct known fully dissected themes; **null**, not zero,
  if partial/legacy data prevents exact coverage (unless another complete request
  covers all themes). No dissection is known zero.
- `best_buzz_calculated`: durable successful calculation history for this user/version,
  not a buzz score or a guarantee that the prepared cache still exists.

Calculation history and known dissection coverage survive analysis-request/cache
cleanup. Migration backfills surviving successful requests; information purged
before upgrade cannot be reconstructed. Per-user analysis state is never inferred
from another user's shared cache. All metadata loads are page-batched, never
per-card HTTP or SQL fetches. Listing does not disclose/consume exposures or run
analysis. Existing dissection instructions/comments, explicit exposure confirmation,
quotas and cancellation remain unchanged. Dissection identity lookup is exact even
when the request is older than the bounded 100-row history listing.

Queued or expired/checkpointed analysis is rejected and its reservations released
if the recorded model, prompt, schema or tokenizer version differs from the worker
configuration. A compatible completed artifact remains readable through the existing
access checks. New statistic snapshots use the producing artifact's model and
versions, not the current application configuration. This does not migrate old
metadata, promise compatibility across arbitrary restarts, or alter Best-buzz
position representation. Statistics batches acquire accounts in UUID order, matching
collection imports and direct copying.

## Adapters

The Mini App links `/training` and `/knowledge`. Knowledge supports fact create/edit,
source URLs, tags, delete, tag rename/delete, search, additive tag filtering, sorting,
pagination, permanent copy sharing and dissection with comments. Job polling preserves
form input. Each training card offers Best buzz preparation.
HTTP follows existing GET-query and CSRF/idempotency-protected POST-mutation
conventions, including edits and deletion.

Telegram exposes Knowledge library through the Training keyboard, launching the
knowledge Mini App. When the Mini App is not configured, the keyboard falls back
to the read-only Telegram library with lazy detail callbacks. Training and
knowledge slash commands are no longer registered; editing, tags, sharing,
analysis preparation/status, and entitlements use Mini App controls instead.

Deleting a dissection import requires confirmation.
Exposure prompts use explicit confirmation controls. See [training.md](training.md)
for the keyboard-driven training lobby and settings flow.

## Placeholders awaiting input

| Item | Placeholder |
| --- | --- |
| Provider | Deployment model and credential above |
| Bingo examples | `<high-bingo fact, Mentions, Common knowledge estimate, desired score>` |
| Bingo formula | `<calibrated versioned formula>` |
| Ambiguous dates | `<meaning of A.C.>`; passed through verbatim |
| GotQuestions | `<supported programmatic access and count semantics>` |
| Commercial categories | Introductory defaults above; billing deferred |

The upgrade converts existing active shares into recipient-owned copies and disables
legacy source access. Downgrading retains the copied facts. The deprecated revoke
operation is rejected; legacy `/sharedfacts` lists the recipient's own library.

Tests cover migration upgrade/downgrade, PostgreSQL CRUD/copy independence, bounded
query counts, private isolation/cleanup, automatic fact application, Best buzz
conversion, quotas/refunds/recovery, authenticated contracts and browser views.
Live Telegram delivery and a configured-provider smoke test remain deployment checks.
