# Training integration boundary

Agent A owns shared models, migrations, contracts, dispatch and adapter registration.
B and C should supply additive specifications beside their modules; do not edit
those shared files in parallel.

`TrainingPacketService.authorize(session, player_id, version_id)` checks live
owner/library read rights without disclosing content. `disclose` uses the same
transaction, checks reservation conflicts, confirms exposure and returns immutable
question revisions and a disclosure receipt. Analysis must call this boundary
before cache lookup and recheck `authorize` before processing/delivery/application.
Private packet scope is `training`; owner identity must be part of cache keys.
Retirement denies subsequent reads, including pinned sessions and analysis.
Call `disclose` before taking account locks: it locks public tournaments,
assignments and grants before the account, matching normal-game entry. Permission,
exposure and analysis-request writes must share the caller's transaction.

Best buzz playback accepts a validated per-question dictionary:
`schema_version: 1`, `tokenizer_version: "announcement-v1"`,
`question_revision_id` (UUID string), `text_digest` (SHA-256 of exact UTF-8 text),
and `source_end` (exclusive character offset at the end of a source word).
Agent A maps this boundary to `announcement_tokens` using pinned settings.
Unavailable/pending/failed analyses cannot start Best buzz playback.

Training settings are versioned independently from tournament settings. Training
uses library-confirmation exposure semantics, live access checks, and excludes
simultaneous active training and normal play. Paused training remains active for
this rule. Reversed mode reverses word order. Hidden mode shows the entire question
immediately for a trainee-configured duration, then hides it for the remainder of
the buzz timer; reconnect cannot redisplay expired text. Reversed and hidden may
combine. Best buzz is incompatible with every other modifier for now.

Packet analysis ports must accept caller transactions for atomic request/quota
creation. Results and cancellation are owner-scoped. Shared artifacts grant no
source rights. Retired private sources must be denied immediately; C supplies
bounded artifact/job cleanup without removing pinned history.
