# Project Configuration and Database Operations

[Technical index](architecture.md) · [Console guide](console-commands.md) ·
[Mini App](mini-app.md)

## Runtime configuration

The default database is `postgresql+asyncpg://sitg:sitg@localhost:5432/sitg`.
[compose.yaml](../compose.yaml) provides PostgreSQL 17 with the `sitg-postgres` named volume.
The application TCP listener defaults to `127.0.0.1:8765`.
For the complete Docker stack with HTTPS, see [VPS deployment](deployment.md).

The Telegram process loads `.env` through [Settings](../src/sitg_bot/config.py).
`sitg-server`, Alembic, and database CLIs read exported environment variables instead.
[.env.example](../.env.example) lists the available configuration.

| Variable | SITG-specific behavior |
| --- | --- |
| `DATABASE_URL` | Database override for server, Alembic, and packet administration |
| `SITG_DB_MAINTENANCE_ENABLED` | Opt-in hourly technical-record maintenance; default `false` |
| `SITG_DB_SUCCESS_RETENTION_DAYS`, `SITG_DB_FAILURE_RETENTION_DAYS` | Technical success/failure retention; defaults 7/30 days |
| `SITG_DB_EXPIRY_GRACE_HOURS` | Grace after session/reference expiry; default 24 hours |
| `BOT_TOKEN` | Used by the Telegram process and optional Mini App server |
| `SITG_APPLICATION_CLIENT_TOKEN` | Shared 32+ character secret authenticating bot-to-server connections |
| `APPLICATION_SERVER_HOST`, `APPLICATION_SERVER_PORT` | Bot destination only; server listener uses `--host`/`--port` |
| `SITG_ADMIN_TOKEN` | Optional 32+ character server credential enabling administrator authentication |
| `SITG_TOKEN_DELIVERY_KEY` | Separate optional 32+ character key enabling one-time plaintext token delivery; requests, inventory, and rulings work without it |
| `TELEGRAM_ENVIRONMENT` | Must match between server and bot: `production` or `test` |
| `APPLICATION_SECURITY_KEY` | 32+ character signing key for Mini App sessions and launch references |
| `MINI_APP_BASE_URL` | Public HTTPS launch URL used by the bot |
| `MINI_APP_ALLOWED_ORIGINS` | Exact origins accepted for Mini App authentication/API access |
| `WEBSITE_ALLOWED_ORIGINS` | Exact independent website origins; hostname set must differ from Mini App origins |
| `MINI_APP_HOST`, `MINI_APP_PORT` | Optional HTTP listener; setting the port enables it |
| `MINI_APP_WEB_DIST` | Built browser assets; default `web/dist` |
| `BOT_LOG_LEVEL`, `BOT_LOG_PATH` | Defaults: INFO and `logs/sitg-bot.log` |
| `BOT_LOG_MAX_BYTES`, `BOT_LOG_BACKUP_COUNT` | Defaults: 10 MiB and five backups |

The Mini App HTTP listener runs inside `sitg-server` and requires `BOT_TOKEN`,
`APPLICATION_SECURITY_KEY`, and allowed origins. Its assets and API share the HTTPS origin
used by the launch URL. The TCP listener exposes trusted adapter access and numeric-ID console
login; it is intended for a private boundary. See the [Mini App guide](mini-app.md) for its
browser contract.

The independent website serves its own build on a different domain and proxies `/api/`
and `/auth/` to the HTTP listener, preserving public Host and browser Origin. Configure
`WEBSITE_ALLOWED_ORIGINS` (or `--website-allowed-origins`) explicitly; website login is otherwise
disabled. The server accepts business API calls from both configured origin sets, but keeps
login flows separate and sessions bound to their original origin. Cookies are host-only.
The website project contains its own deployment and API documentation; `MINI_APP_WEB_DIST`
continues to point only at the Mini App build.

## Schema baseline

[0001_initial_schema.py](../migrations/versions/0001_initial_schema.py) is the sole
baseline. It freezes tables, indexes, constraints, and Ladder/Classic/SI seeds
without importing runtime models.

The baseline requires an empty database. Databases from the removed development migration
chain require an intentional reset; stamping them with this revision is unsupported.
Subsequent schema changes require new ordered migrations.

Run `alembic upgrade head` against the server database before restarting after an update.
Revision `0028_training_lifecycle` adds training-only ordered presentation events and
SI announcement/intermission stages. It schedules existing active revealed sessions
for automatic progression; their original attempts and history remain intact. Apply
it before deploying the shared training Telegram lifecycle/cleanup adapter.
Revision `0027_training_history_flow` adds per-question playback/buzz evidence,
pause accounting, solo appeal verdict fields and durable training-lobby presentation.
Legacy playback/buzz timestamps remain null; only the existing answer verdict is
copied into `original_correct`. Apply it before deploying the training flow/history
writers and readers. There is one ordered chain after `0026_knowledge_images`.
Revision `0026_knowledge_images` adds immutable raster attachments and an empty image
list to existing facts; it preserves existing facts, import fingerprints and merge
history. Apply it before deploying Knowledge image upload/read APIs.
Revision `0020_database_maintenance` adds compaction timestamps and maintenance indexes;
it does not prune existing data. Apply it before running the maintenance CLI or enabling
automatic maintenance.
Revision `0018_appeal_selection_timer` persists appeal selection deadlines and the prior pause
state. Apply it before deploying the shared selection/voting timer.
Revision `0012_appeals_per_answer` allows distinct answers on one question to have separate
appeals, preserving existing appeal records. Apply it before deploying the appeal fix.
Revision `0005_manual_tournament_start` adds manual starts and scheduled reminders, preserving
existing tournaments with games or started Classic stages. Others require a manual start.
Revision `0009_tournament_player_limits` initializes minimum/maximum players to four and
clears assembling-lobby readiness. Classic retains prescribed rosters. Existing manual
registration overrides disable scheduling. Packet assignments whose former `no-access` value
represented disabled playability receive `read-after-play`; their access switches remain unchanged.
Revision `0010_library_viewing_rules` separates viewing from playing, renaming the assignment
rule and policy default. The choices become `never`, `after-play`, and `anytime`; former
`no-access` and `play-only` values both become `never` for library viewing only. Explicit
per-player play permissions are preserved independently. Update the server and Mini App together.
Revision `0002_token_delivery_pgcrypto` enables PostgreSQL `pgcrypto`, required for encrypted
one-time token delivery when `SITG_TOKEN_DELIVERY_KEY` is configured. The migration role needs
permission to create this extension. Downgrading preserves it because it may be shared.

[storage/models.py](../src/sitg_bot/storage/models.py) defines ORM records;
[data rules](data-rules.md) maps their relationships, transaction boundaries, and invariants.

## Database test constraints

PostgreSQL tests use `TEST_DATABASE_URL` and the `integration` pytest marker.
The local test database convention is `sitg_test`. An exported `DATABASE_URL` must be unset
or target that same disposable database so migration commands cannot reach another database.

Integration tests apply migrations and mutate data. Baseline tests create disposable databases
and verify schema/ORM parity, seeds, repeated upgrade, rollback/recreation, and queue/audit
behavior. They do not downgrade the database supplied through `TEST_DATABASE_URL`.

## Project tools

Entry points are declared in [pyproject.toml](../pyproject.toml).

| Tool | Responsibility |
| --- | --- |
| `sitg-server` | Application services, TCP listener, background workers, optional Mini App HTTP listener |
| `sitg-bot` / `python -m sitg_bot` | Telegram presentation process |
| `sitg-console` | Local interactive client; [command guide](console-commands.md) |
| `sitg-import-packet` | DOCX/PDF-to-JSON conversion, including multiple packets |
| `sitg-admin-packet` | Trusted direct-database draft import, preview, publication, and rejection |
| `sitg-db-maintenance` | Bounded preview or application of technical-record retention |
| `sitg-rating-simulation` | Comparison of `time_weighted` and `log_recent`; produces `report.html` and CSV files |

Packet administration requires explicit tournament context on import; its global
`--database-url` option precedes the subcommand. These local operations bypass the
authenticated frontend boundary. Format and publication rules belong in
[packet administration](packet-administration.md).

The rating simulator defaults to four players per game; a daily remainder forms an additional
game. Its models are described in [ratings and statistics](data-and-statistics.md).

## Diagnostic records

| Record | Purpose |
| --- | --- |
| `game_events`, ordered by game/sequence | Gameplay progression |
| `player_exposure_claims` | Content reservation and permanent exposure |
| `score_ledger`, `rating_ledger`, `ruleset_rating_ledger` | Score and rating history |
| `outbox_events`, `durable_jobs` | Delivery/work status, attempts, leases, schedule, and `last_error` |

Bot diagnostics include update/correlation IDs and go to stderr plus the rotating log file.
Message content, names, tokens, and configured secrets are excluded.

## Technical-record maintenance

`sitg-db-maintenance` previews eligible records by default. `--apply` deletes or compacts them.
Both modes use the same bounded scan; `eligible` counts describe the inspected candidates,
not necessarily the entire backlog. `deleted` and `compacted` count committed changes only.

| Records | Default action |
| --- | --- |
| Known scheduler periodic jobs with matching `periodic:<kind>:<bucket>` keys | Delete successes after 7 days and failures after 30 days |
| Other terminal jobs and outbox events | Clear payload and diagnostic text after 7/30 days; preserve IDs, keys, state, timestamps, and attempts |
| Completed request audits | Delete successes after 7 days; rejected/failed requests after 30 days |
| Sessions and launch references | Delete more than 24 hours after expiry, including revoked/redeemed records |

Retention starts at completion, delivery, or failure, rather than creation. Pending/running
work, cancelled jobs, and missing terminal timestamps do not qualify. Lease expiry alone
never qualifies a record. Application idempotency rows and cached responses, Telegram
receipts, and all `telegram.chat.message` outbox records remain intact. Chat payloads preserve
private album routing. Game history, notifications, and moderation records are unaffected.
Permanent replay records and excluded chat payloads will continue growing.

Each run cycles through categories in batches of 1,000, committing each batch separately.
Locked rows are skipped. Runs stop after 100 batches or 10 seconds; later runs resume the
remaining work. A session advisory lock serializes automatic and CLI runs across processes.
An unavailable lock produces `lock_acquired: false`. Reports include old unfinished work,
grouped by status and kind/topic. The bot has no `lobby.event` consumer; such pending records
are reported and retained. Reports share the runtime limit and may be incomplete when
`work_limit_reached` is true.

Roll out with the database environment exported:

```bash
alembic upgrade head
sitg-db-maintenance
sitg-db-maintenance --apply
```

Then set `SITG_DB_MAINTENANCE_ENABLED=true` and restart the server. It runs maintenance on
startup and hourly in a separate task. The equivalent server flag is
`--db-maintenance-enabled`; `--no-db-maintenance-enabled` disables it. VPS Compose forwards
the maintenance variables from `.env`. Automatic maintenance remains disabled by default.

Both tools accept `--success-retention-days`, `--failure-retention-days`, and
`--expiry-grace-hours`, overriding the corresponding environment variables. The CLI also
accepts `--database-url`, `--batch-size` (at most 1,000), `--max-batches`, and `--max-runtime`.
Monitor summaries, unfinished backlog, and autovacuum as old history drains. Deletion makes
space reusable; it does not promise an immediate reduction in database file size.
