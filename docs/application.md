# Application Services and Protocol

[Technical index](architecture.md) · [Telegram](telegram.md) · [Mini App](mini-app.md)

## Source map

| File or module | Responsibility |
| --- | --- |
| [server.py](../src/sitg_bot/server.py) | Composition, TCP sessions, console dispatch, background workers, optional HTTP startup |
| [application/contracts.py](../src/sitg_bot/application/contracts.py) | Versioned action names, typed operations, metadata, responses, stable errors |
| [application/gateway.py](../src/sitg_bot/application/gateway.py) | Authorization, dispatch, audit, durable request deduplication |
| [application/adapters.py](../src/sitg_bot/application/adapters.py) | Adapter request construction |
| [application/protocol.py](../src/sitg_bot/application/protocol.py) | Remote gateway, update authentication, outbox consumer |
| [services/players.py](../src/sitg_bot/services/players.py), [navigation.py](../src/sitg_bot/services/navigation.py) | Accounts, registration, profile settings, saved navigation and capabilities |
| [services/telegram_auth.py](../src/sitg_bot/services/telegram_auth.py), [admin_auth.py](../src/sitg_bot/services/admin_auth.py) | Update claims and administrator authentication |
| [services/author_links.py](../src/sitg_bot/services/author_links.py), [token_requests.py](../src/sitg_bot/services/token_requests.py) | Audited author-link and tournament-token workflows |
| [services/reliable_delivery.py](../src/sitg_bot/services/reliable_delivery.py), [reliable_scheduling.py](../src/sitg_bot/services/reliable_scheduling.py) | Outbox leases, retries, jobs, deadline reconciliation |
| [services/notifications.py](../src/sitg_bot/services/notifications.py) | Notification inbox, read state, alert throttling |

## Sessions and contracts

The TCP transport is newline-delimited JSON with request/response correlation and asynchronous
events. Authenticated adapters bind a channel and client identity using
`SITG_APPLICATION_CLIENT_TOKEN` before making gateway, Telegram-update, or outbox calls.
The adapter is a trusted backend client: never give this credential to browser code.
Console numeric-ID login is a local development interface, not public account authentication.
Keep the TCP listener on a trusted local/private boundary.

`ApplicationPrincipal` carries internal player and/or Telegram identity. Identity is
established by the adapter's authentication path, then checked against the requested resource.
The Mini App resolves its principal from a server-side session; Telegram derives it from a
claimed private update.

`ActionCode` is the executable action inventory, with names such as
`lobbies.start.v1`. Operations are frozen Pydantic models rejecting unknown fields.
`RequestMetadata` includes channel, client name/version, correlation ID, and an optional
idempotency key. Capability negotiation lets adapters discover available operations rather
than assuming every menu destination is implemented.

Mutations requiring idempotency persist request identity and results. A reused key with
different content conflicts; in-progress and indeterminate requests have distinct outcomes.
Do not blindly retry an indeterminate operation under a new key. Versioned edits supply the
expected resource version and return `stale_write` if another actor has changed it.
Queries use permission-aware projections and cursor pagination where supported.

Errors are stable codes, including `forbidden`, `not_found`, `validation_failed`,
`stale_write`, and `capability_unavailable`. Localize these in the adapter rather than
displaying exception text. Multiple calls from one frontend update share a correlation ID
but retain separate audit records.

## Accounts and navigation

An account separates public nickname from private real name, Telegram identity, optional
username visibility, and `ru`/`en` locale. Registration is resumable and records completion;
profile edits use optimistic versions. Only dedicated authorized operations expose private
registration details.

Navigation stores interaction mode, independent player/manager tournament selections, and
lobby/game context. Capabilities are rebuilt from current registration, roles, membership,
and game state. Player and manager modes are available to registered users; administrator
mode requires an active platform role. Entering manager mode alone grants no tournament rights.

Administrator authentication validates the configured credential on the server and atomically
activates the platform role and administrator navigation mode. Telegram deletes the submitted
credential message after processing.

Player-to-author linking retains pending, approved, rejected, and cancelled requests.
Administrator decisions do not merge authors by display name. Approved links feed permanent
authorship exposure burns; see [packet administration](packet-administration.md).

Tournament-token requests retain requester, proposed name, commentary, decision, and audit
history. Approval issues an expiring, creator-bound, single-use token. Creation consumes it
atomically. Inventory and rulings work without a token-delivery key;
`SITG_TOKEN_DELIVERY_KEY` additionally enables protected, one-time plaintext delivery.

## Durable delivery and jobs

Services enqueue outbox records in the transaction that changes domain state. Messages use
locale-independent keys and parameters. Consumers subscribe to topics, lease eligible
records, deliver, and acknowledge. Deduplication and per-partition ordering prevent repeated
or reordered processing; topic subscriptions keep unrelated domain events from blocking
presentation notices.

Failures retry with capped exponential backoff. Expired leases can be reclaimed; exhausted
attempts retain terminal status and diagnostic details. Consumers must still account for the
send/ack gap: presentation message identities and idempotent handlers are required, not an
assumption of exactly-once external delivery.

The scheduler reconciles persisted deadlines into durable jobs for game progression,
join/pause expiry, appeals, lobby expiry, rating settlement, matchmaking, scheduled tournament
start reminders, and suspicion work.
Workers lease jobs and invoke idempotent services. Stored deadlines and state govern recovery,
not the lifetime of a worker task. Privacy maintenance helpers are not currently exposed or
scheduled.

Console socket broadcasts are convenient live views; durable presentation delivery uses the
outbox. Reconnect by loading authoritative context instead of relying on missed socket events.

## Changing and testing the boundary

Add operations in contracts, implement the service and gateway dispatch, then wire the adapter.
Preserve actor checks, stale-write behavior, metadata, and error localization. Place durable
notifications beside the mutation, not after a frontend response.

Start with [test_application_gateway.py](../tests/unit/test_application_gateway.py),
[test_server.py](../tests/unit/test_server.py), and
[test_reliable_delivery.py](../tests/unit/test_reliable_delivery.py). Persistence coverage lives
in the relevant [integration tests](../tests/integration), including administrator
authentication and baseline queue/audit checks.
