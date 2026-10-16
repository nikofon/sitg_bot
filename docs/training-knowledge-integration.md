# Training mode: Agent B knowledge integration spec

What Agent A needs to integrate the knowledge library slice into shared files.
Product context and agent boundaries are in [the training mode plan](training-mode-plan.md).
Agent B owns the modules below; A owns every shared file this document touches.

## Implemented modules (owner: B)

| Module | Contents |
| --- | --- |
| `src/sitg_bot/domain/knowledge.py` | Pure rules: fact content validation and fingerprints, tag normalization, source links, revocable read-only sharing grants, Mentions semantics (dedup across aliases/pages, capped never complete), Common knowledge% validation, versioned Bingo scoring (`pending_calibration` until calibrated), deterministic dissection candidate IDs and idempotent application planning. |
| `src/sitg_bot/services/knowledge.py` | `KnowledgeService` over a narrow repository port; `InMemoryKnowledgeStore` is the fixture implementation. CRUD, tags, search/filter/sort with stable keyset cursors (25/page), optimistic concurrency via `expected_version`, sharing grants, dissection request/apply/retry with duplicate suppression and manual-edit preservation, statistic snapshots. |
| `src/sitg_bot/services/knowledge_statistics.py` | `GotQuestionsConnector` and `CommonKnowledgePort` protocols, recorded fixture implementations (no network), `KnowledgeStatisticsService` with per-query page caps, mention TTL cache, estimate validation. |
| `src/sitg_bot/bot/handlers/knowledge.py` | Isolated aiogram router (`/facts`, `klib:*` callbacks) behind a `KnowledgeBackend` port; presenters build `MessageModel`s with escaped parameters and ≤64-byte callback data. |
| `web/src/ui/knowledge.ts` | Mini App view with its own types and `KnowledgeI18n` key contract; paginated list, lazy detail loading, sharing and dissection sections, pending/quota/unavailable/error states. |

Tests: `tests/unit/test_knowledge_{domain,service,statistics,bot}.py` and
`web/src/ui/knowledge.test.ts` (43 Python + 7 browser tests, all green).
PostgreSQL integration tests are added by A together with the adapter below.

## Storage schema (A: `storage/models.py` + one ordered migration)

All tables reference `players.id` for ownership. Authorization stays in the
service; storage enforces integrity only.

- `knowledge_facts`: `id` UUID PK, `owner_id` FK, `title` varchar(200),
  `description` text, `content_fingerprint` char(64), `content_version` int ≥ 1,
  `origin` IN (`manual`, `dissection`), `packet_id`/`question_id`/`request_id`
  UUID NULL, `question_revision` varchar NULL, `applied_candidate_id`
  varchar(19) NULL, `applied_content_version` int NULL, `statistic_status` IN
  (`none`, `pending`, `fresh`, `stale`), `deleted_at`/`merged_into_id` NULL,
  `created_at`/`updated_at`. CHECK: dissection origin requires packet,
  question, revision and request; content limits from `domain/knowledge.py`.
  Indexes: `(owner_id, deleted_at)`, `(owner_id, applied_candidate_id)`,
  `(owner_id, title)` for title ordering.
- `knowledge_fact_links`: `fact_id` FK, `position` int, `url` varchar(2000),
  `title` varchar(200), `origin` IN (`manual`, `dissection`); unique
  `(fact_id, url)`. URL scheme validation is domain-level.
- `knowledge_tags`: `id` UUID PK, `owner_id` FK, `name` varchar(64); unique
  `(owner_id, name)` on the normalized (casefolded) form.
- `knowledge_fact_tags`: PK (`fact_id`, `tag_id`), both FKs.
- `knowledge_sharing_grants`: `id` UUID PK, `owner_id`/`recipient_id` FKs,
  `granted_at`, `revoked_at` NULL; unique `(owner_id, recipient_id)`; CHECK
  recipient ≠ owner. Revocation updates the same row.
- `knowledge_dissection_requests`: `id` UUID PK, `owner_id` FK, `packet_id`,
  `packet_revision` varchar, `instructions`/`comment` text, 
  `instruction_digest` char(16), `base` boolean, `status` IN (`pending`,
  `succeeded`, `partially_succeeded`, `failed`), `created_at`; unique
  `(owner_id, packet_id, packet_revision, instruction_digest)` — this is the
  service's request idempotency key.
- `knowledge_dissection_applications`: `request_id` FK PK, `packet_id`,
  `instruction_digest`, `base`, `outcomes` JSONB (candidate ID, outcome,
  fact ID), `applied_at`. Immutable once written.
- `knowledge_statistic_snapshots`: `id` UUID PK, `fact_id`/`owner_id` FKs,
  `content_version`, `mentions`/`common`/`bingo` JSONB NULL, `created_at`.
  Append-only; never rewritten by refreshes.

## Repository port (A: SQLAlchemy adapter)

Implement the exact async method surface of `InMemoryKnowledgeStore` in
`services/knowledge.py` against PostgreSQL: `add_fact`, `get_fact`,
`save_fact`, `facts_for_owner`, `add_tag`, `get_tag`, `save_tag`,
`tags_for_owner`, `add_grant`, `get_grant`, `save_grant`,
`grants_for_recipient`, `grants_for_owner`, `add_request`, `get_request`,
`save_request`, `requests_for_owner`, `add_snapshot`, `snapshots_for_fact`.
Keep grants uncached and live. `list_facts` filtering, ordering and keyset
cursors move into SQL (cursor format stays the service's opaque base64 of
`order_key\0fact_id`); each page must remain bounded with no per-card queries.
Query-count regression checks follow the existing loading practices
([packet loading](packet-loading.md)).

## Gateway operations (A: `application/contracts.py` + dispatch)

Typed operations with stable errors; service exceptions map as
`PermissionError → forbidden`, `LookupError → not_found`,
`ValueError → validation_failed` / version mismatch → `stale_write`.

| Operation | Kind | Idempotency | Notes |
| --- | --- | --- | --- |
| `knowledge.facts.view` | query | — | Page parameters: `owner_id?`, `cursor?`, `search?`, `tag?`, `order`, `limit`. |
| `knowledge.fact.view` | query | — | Owner gets full detail; recipients get `editable: false`. |
| `knowledge.fact.create` | mutation | required | Returns the created projection. |
| `knowledge.fact.update` | mutation | required | Requires `expected_version`; content changes bump the version and mark statistics stale. |
| `knowledge.fact.delete` | mutation | required | Requires `confirm_dissection_origin` for dissection facts. |
| `knowledge.facts.merge` | mutation | required | Requires `expected_target_version` and `confirm_source_delete` for manual sources. |
| `knowledge.tag.create` / `rename` / `delete` | mutation | required | Owner-scoped normalized names. |
| `knowledge.grant.create` / `revoke` | mutation | required | Idempotent active grant per `(owner, recipient)`. |
| `knowledge.dissection.request` | mutation | required | Body: packet access reference, `packet_revision`, `instructions`, `comment`. Enforces packet access/disclosure boundary before recording. |
| `knowledge.dissection.apply` | mutation | required | Consumes C's validated candidates; retries are idempotent. |
| `knowledge.dissection.status` | query | — | Per request or per packet list. |

Projections use the JSON shapes already consumed by
`bot/handlers/knowledge.py` and `web/src/ui/knowledge.ts` (see their tests);
`LocalizationService.text` escapes every string parameter.

## HTTP routes (A: `miniapp_http.py`, browser client, routing)

- `GET /api/miniapp/knowledge/facts` — cursor pagination (25/page),
  `search`, `tag`, `order`; embeds statistic states.
- `GET /api/miniapp/knowledge/facts/{fact_id}` — lazy detail.
- `POST /api/miniapp/knowledge/facts`, `PATCH|DELETE .../facts/{fact_id}`,
  `POST .../facts/{fact_id}/merge` — CSRF-protected.
- `GET|POST /api/miniapp/knowledge/sharing/grants`,
  `DELETE .../sharing/grants/{grant_id}`, `GET .../sharing/shared-with-me`.
- `GET|POST /api/miniapp/knowledge/dissection/requests`.
Browser wiring passes the view's `loadFacts`/`loadFact`/`grant`/`revoke`/
`dissect` actions to the authenticated client and re-renders on fresh data.

## Localization (A: catalogs)

Bot `locales/ru.json` + `locales/en.json`: the `knowledge.*` keys used in
`tests/unit/test_knowledge_bot.py` (EN/RU pairs are ready there; placeholders
must match across locales). Web `i18n/en.ts` + `ru.ts`: the keys exported by
`knowledgeMessageKeys` in `web/src/ui/knowledge.ts`; once merged, the shared
`I18n` satisfies `KnowledgeI18n` structurally. Proposed EN/RU values are in
`web/src/ui/knowledge.test.ts`.

## Telegram wiring (A: `bot/app.py`, `bot/router.py`)

1. Build a `KnowledgeBackend` implementation over the gateway and register it
   as the `knowledge_backend` workflow dependency.
2. `root_router.include_router(knowledge.router)` after the player router.
3. `/facts` lists the owner's collection; `klib:page:<n>` page numbers must be
   resolved server-side to the service's keyset cursors (callback data stays
   ≤ 64 bytes); `klib:fact:<uuid>` opens the detail. Every read re-authorizes
   in the service; revoked access degrades to `not_found`/`forbidden` messages.

## Deferred decisions

- Real GotQuestions connector: verify a supported programmatic access method
  and response/count semantics against the official FAQ before shipping; the
  fixture connector stays the test boundary. Respect throttling.
- Bingo formula: calibrate against the supplied examples, then inject the
  formula and bump `BINGO_SCORING_VERSION`; until then all values are
  `pending_calibration`, never zero.
- Common knowledge population assumptions, provider/model and cache-hit
  charging (Agent C), plus sharing edit/copy/public-link semantics remain open
  in the plan's Stage 0 list.

