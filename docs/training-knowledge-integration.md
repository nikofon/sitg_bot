# Training knowledge and analysis integration

B's knowledge and C's analysis/entitlement services now use shared SQLAlchemy
metadata and migrations through `0023_copy_sharing_theme_quotas`. `PostgresKnowledgeStore`
implements B's repository; `KnowledgeApplication` connects persistence, source
access, automatic fact application, statistics and Best buzz.

## Integration decisions

- Sharing sends permanent, independently editable copies of the current collection
  to the recipient's own library. Later edits/deletions never propagate. Repeating
  a send adds only uncopied facts; it never overwrites or resurrects prior copies.
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

## Adapters

The Mini App links `/training` and `/knowledge`. Knowledge supports fact create/edit,
source URLs, tags, delete/merge, tag rename/delete, search, tag filtering, sorting,
pagination, permanent copy sharing and dissection with comments. Job polling preserves
form input. Each training card offers Best buzz preparation.
HTTP follows existing GET-query and CSRF/idempotency-protected POST-mutation
conventions, including edits and deletion.

Telegram exposes Knowledge library in the Training menu and `/facts` with lazy
detail callbacks. Native commands:

```text
/facts [search text] | [tag] | [title_asc, title_desc, updated_desc]
/factadd Title | Description | tag1, tag2 | https://source.example
/factedit FACT_ID CONTENT_VERSION Title | Description | tags | URLs
/factdelete FACT_ID [confirm]
/factmerge SOURCE_ID TARGET_ID TARGET_VERSION [confirm]
/factshare NICKNAME_OR_PLAYER_ID
/factgrants
/tagadd NAME
/tagrename TAG_ID NAME
/tagdelete TAG_ID
/dissect VERSION_ID [confirm] [instructions]
/bestbuzz VERSION_ID [confirm]
/analysis REQUEST_ID
/analysiscancel REQUEST_ID
/traininglimits
```

Deleting a dissection import or merging away a manual fact requires confirmation.
Exposure prompts require repeating the analysis command with `confirm`.
Command results retain IDs needed for subsequent edits/status reads.

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
