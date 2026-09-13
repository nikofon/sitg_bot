# Project Configuration and Database Operations

[Technical index](architecture.md) · [Console guide](console-commands.md) ·
[Mini App](mini-app.md)

## Runtime configuration

The default database is `postgresql+asyncpg://sitg:sitg@localhost:5432/sitg`.
[compose.yaml](../compose.yaml) provides PostgreSQL 17 with the `sitg-postgres` named volume.
The application TCP listener defaults to `127.0.0.1:8765`.

The Telegram process loads `.env` through [Settings](../src/sitg_bot/config.py).
`sitg-server`, Alembic, and database CLIs read exported environment variables instead.
[.env.example](../.env.example) lists the available configuration.

| Variable | SITG-specific behavior |
| --- | --- |
| `DATABASE_URL` | Database override for server, Alembic, and packet administration |
| `BOT_TOKEN` | Used by the Telegram process and optional Mini App server |
| `SITG_APPLICATION_CLIENT_TOKEN` | Shared 32+ character secret authenticating bot-to-server connections |
| `APPLICATION_SERVER_HOST`, `APPLICATION_SERVER_PORT` | Bot destination only; server listener uses `--host`/`--port` |
| `SITG_ADMIN_TOKEN` | Optional 32+ character server credential enabling administrator authentication |
| `SITG_TOKEN_DELIVERY_KEY` | Separate optional 32+ character key enabling one-time plaintext token delivery; requests, inventory, and rulings work without it |
| `TELEGRAM_ENVIRONMENT` | Must match between server and bot: `production` or `test` |
| `APPLICATION_SECURITY_KEY` | 32+ character signing key for Mini App sessions and launch references |
| `MINI_APP_BASE_URL` | Public HTTPS launch URL used by the bot |
| `MINI_APP_ALLOWED_ORIGINS` | Exact origins accepted for Mini App authentication/API access |
| `MINI_APP_HOST`, `MINI_APP_PORT` | Optional HTTP listener; setting the port enables it |
| `MINI_APP_WEB_DIST` | Built browser assets; default `web/dist` |
| `BOT_LOG_LEVEL`, `BOT_LOG_PATH` | Defaults: INFO and `logs/sitg-bot.log` |
| `BOT_LOG_MAX_BYTES`, `BOT_LOG_BACKUP_COUNT` | Defaults: 10 MiB and five backups |

The Mini App HTTP listener runs inside `sitg-server` and requires `BOT_TOKEN`,
`APPLICATION_SECURITY_KEY`, and allowed origins. Its assets and API share the HTTPS origin
used by the launch URL. The TCP listener exposes trusted adapter access and numeric-ID console
login; it is intended for a private boundary. See the [Mini App guide](mini-app.md) for its
browser contract.

## Schema baseline

[0001_initial_schema.py](../migrations/versions/0001_initial_schema.py) is the sole
baseline. It freezes tables, indexes, constraints, and Ladder/Classic/SI seeds
without importing runtime models.

The baseline requires an empty database. Databases from the removed development migration
chain require an intentional reset; stamping them with this revision is unsupported.
Subsequent schema changes require new ordered migrations.

Run `alembic upgrade head` against the server database before restarting after an update.
Revision `0005_manual_tournament_start` adds manual starts and scheduled reminders, preserving
existing tournaments with games or started Classic stages. Others require a manual start.
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
| `sitg-import-packet` | Marked-up DOCX-to-JSON conversion |
| `sitg-admin-packet` | Trusted direct-database draft import, preview, publication, and rejection |
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
