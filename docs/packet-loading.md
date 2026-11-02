# Packet catalogue loading

[Technical index](architecture.md) · [Mini App](mini-app.md)

The Mini App fetches lobby and tournament cards through authenticated `/packets`
endpoints in batches of 25. Name, author, packet year, and publication year filters
apply before freshness calculation. An assignment UUID cursor resumes each list.
Legacy gateway calls still return complete lists.

The browser pauses 200 ms between batches, debounces filters by 250 ms, sorts
progressively, and reconciles every 30 seconds. Both services share a backend
limiter with a 200 ms cooldown. Gameplay and individual card refreshes bypass it.
Block controls update the affected card and its selected-lobby summary in place.
Training/Knowledge use the same pacing, with their own
[catalogue filters and version cursors](training-knowledge-integration.md#coloured-tags-durable-links-and-packet-cards).

## Tournament management

Management batches packet versions and author attribution. The Mini App requests
`include_player_access=false` on route resolution and management mutations;
`player_access: null` means deferred, while `[]` means no participants. Legacy
callers default to complete access arrays. Their default idempotency digests stay
compatible.

Opening Packet accessibility fetches only the selected assignment through
`GET /api/miniapp/manager/tournaments/{launch_ref}/packets/{assignment_id}/access`.
The server rechecks the manager role and assignment ownership, then batches player
rights, including Classic round restrictions. Hidden panels render on first use;
access is cached within the view and invalidated when its snapshot changes.

## Administrator packet lists

The admin Packets tab starts with an authorized empty shell and loads versions
through `/api/miniapp/admin/management/packets` in batches of 25. Each nonempty page
uses seven service-level SQL reads, including authorization. Requests share the
backend limiter and the browser's cancellable packet loader. Cursors and browser
card keys use **version IDs**, preserving multiple versions of one logical packet.
Legacy catalogue calls still return complete lists using batched queries.

Search continues to cover all loaded metadata, including collapsed author and
tournament details. Results and sorting update as pages arrive. Cards retain
focus and open details between unchanged updates; collapsed metadata is built
only when opened. Embedded tournament packet names and author version lists also
use bulk queries. Admin view/download authorization and confirmation remain live.

## Freshness caching

The content adapter keeps database-scoped LRU caches for 1,024 layouts/counts and
8,192 player/version results. Every availability request checks current player
blocks, then compares relevant reserved/burnt logical exposure identities with
cached inputs. Player theme sets are intersected; observers are excluded from
group freshness. Permissions and settings-dependent validation remain live.
Blocked packets skip layouts, questions, and exposure reads; a count-only read
is allowed for an uncached total. Cache eviction and restarts require fresh work.

## Local benchmarks

With development dependencies installed, use the existing commands:

- `python scripts/benchmark_packet_lists.py --baseline-ref <old-commit>` measures
  full versus paced cold/warm content-adapter work on disposable SQLite fixtures.
- `python scripts/benchmark_management_loading.py --baseline-ref <old-commit>`
  compares management snapshot query counts and payload sizes; add `--content`
  to include a theme, question and coauthor per packet.
- From `web/`, run `VITE_MANAGEMENT_BENCHMARK=1 npm test -- src/management-loading.test.ts -t 'opens 200 packets'`
  for jsdom construction measurements, not browser layout or paint.

SQLite timings exclude PostgreSQL latency and HTTP/network work. Pacing can reduce
peak CPU while increasing elapsed loading time; warm-cache gains vary. Measure the
deployed services and Mini App before claiming capacity or user-visible improvements.
