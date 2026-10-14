# Packet catalogue loading

The Mini App fetches lobby and tournament cards through authenticated `/packets`
endpoints in batches of 25. Name, author, packet year, and publication year filters
apply before freshness calculation. An assignment UUID cursor resumes each list.
Legacy gateway calls still return complete lists.

The browser pauses 200 ms between batches, debounces filters by 250 ms, sorts
progressively, and reconciles every 30 seconds. Both services share a backend
limiter with a 200 ms cooldown. Gameplay and individual card refreshes bypass it.
Block controls update the affected card and its selected-lobby summary in place.

The content adapter keeps database-scoped LRU caches for 1,024 layouts/counts and
8,192 player/version results. Every availability request checks current player
blocks, then compares relevant reserved/burnt logical exposure identities with
cached inputs. Player theme sets are intersected; observers are excluded from
group freshness. Permissions and settings-dependent validation remain live.
Blocked packets skip layouts, questions, and exposure reads; a count-only read
is allowed for an uncached total. Cache eviction and restarts require fresh work.

## Local measurements

Run with development dependencies:

```bash
python scripts/benchmark_packet_lists.py --baseline-ref 43e47e9
```

Measured on 2026-10-14 using disposable SQLite data: eight themes and five questions
per theme, two players per lobby, disjoint player groups for two lobbies. The
baseline loads full catalogues; paced runs use the shared limiter. Warm runs keep
the cold run's cache. CPU is process time; peak CPU samples use 100 ms windows and
percent of one core. These measure adapter work, excluding HTTP, permission
queries, browser rendering, and PostgreSQL latency. They are not production
capacity estimates.

| Packets | Lobbies | Mode | Load seconds | CPU seconds | Peak CPU % |
| ---: | ---: | --- | ---: | ---: | ---: |
| 50 | 1 | Full | 0.059 | 0.059 | 59.4 |
| 50 | 1 | Paced cold | 0.221 | 0.021 | 10.9 |
| 50 | 1 | Paced warm | 0.205 | 0.005 | 2.4 |
| 50 | 2 | Full | 0.028 | 0.028 | 27.7 |
| 50 | 2 | Paced cold | 0.656 | 0.055 | 30.9 |
| 50 | 2 | Paced warm | 0.611 | 0.011 | 2.9 |
| 100 | 1 | Full | 0.032 | 0.032 | 32.0 |
| 100 | 1 | Paced cold | 0.724 | 0.123 | 46.6 |
| 100 | 1 | Paced warm | 0.635 | 0.034 | 11.8 |
| 100 | 2 | Full | 0.082 | 0.082 | 82.1 |
| 100 | 2 | Paced cold | 1.561 | 0.160 | 44.9 |
| 100 | 2 | Paced warm | 1.464 | 0.064 | 10.3 |
| 200 | 1 | Full | 0.092 | 0.092 | 92.0 |
| 200 | 1 | Paced cold | 1.605 | 0.204 | 40.0 |
| 200 | 1 | Paced warm | 1.480 | 0.079 | 11.2 |
| 200 | 2 | Full | 0.160 | 0.160 | 97.5 |
| 200 | 2 | Paced cold | 3.301 | 0.300 | 41.0 |
| 200 | 2 | Paced warm | 3.162 | 0.161 | 38.9 |

Pacing generally reduced peak CPU and increased elapsed loading time. Cold total
CPU often increased; warm total CPU gains varied. Query compilation and scheduling
affect these small samples. Measure the deployed services before claiming an
overall CPU reduction.
