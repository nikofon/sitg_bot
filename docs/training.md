# Training

Solo training uses separate session, membership, question-pin and attempt tables.
It never creates competitive games, rating entries or tournament results.
Run `alembic upgrade head` (through `0027_training_history_flow`) before deployment.

## Access and uploads

Training mode is available to registered, unbanned accounts. Changing modes grants
no packet rights. Private packets use `scope=training`, require an uploader and
have no creation tournament or assignments. Database triggers reject assigning
private packets/drafts to tournaments. Publication preserves immutable revisions;
private attribution remains in the owner's original draft, outside public author
catalogues. Other trainees and platform administrators cannot read private uploads.

Uploads reuse the DOCX/PDF/JSON parser, checksum, size limit, validation and preview
confirmation. Parsing runs in a thread. Question values are explicit training
upload settings, defaulting to `10,20,30,40,50`. Private duplicate warnings never
search another trainee's content.

`TrainingPacketService` is the permission boundary for gameplay and analysis.
Library packets retain existing adopted-version, readability, release and
after-play rules. Already burnt packets remain trainable. Fresh disclosure needs
confirmation and permanently burns canonical theme/question claims; active normal
game reservations prevent disclosure. Rights are checked again on protected reads.
Private deletion retires access while retaining pinned history. The retirement
event drives durable analysis payload/checkpoint cleanup.

Packet pages contain at most 25 cards, use UUID cursors and apply permissions and
name filters in SQL. Each page uses three SELECTs, independent of card count;
freshness is not calculated. Browser loading reuses the paced, cancellable loader.
Content and downloads load only on request. Queued downloads recheck rights before
constructing the document.

## Execution and presentation

Telegram training is keyboard-driven, without training-specific slash commands. The home
keyboard contains Start training, Knowledge library, Upload packet for training,
and Change mode. Start training creates a persistent solo lobby, replacing the
keyboard with Packets and settings, Start, and Quit. Packets and settings launches
the `/training` Mini App, using the regular lobby's Overview / Packets / Options
sections. Packet selection autosaves independently, while settings require an
explicit save. Selecting a packet never starts questions.
Return to Telegram and press Start after choosing a packet and saving any settings; exposure confirmation and Best
buzz preparation checks still apply. Quit discards the lobby or stops a running
session. A durable lobby status message shows the selected packet and localized enabled
modifiers; packet/settings saves update that message through the transactional outbox.
Reopening the menu recovers the same status instead of posting another copy.

During a session, reply-keyboard controls use `+` to buzz, `||` to pause/resume,
and `!` to appeal, like regular games. Next question, reveal, skip, score and quit
are also keyboard actions. `/score` reads the current or latest finished training
score. Ordinary question messages have no inline controls. Only the Best buzz pause
has inline Continue reading; it resumes the configured token cadence rather than
instantly revealing the remainder. Completion restores the home keyboard.

Lobby selections and settings persist across restarts and use an owner-locked,
monotonically increasing version to reject stale saves, quits, and confirmations.
Only a successful start consumes the lobby. The lobby API's `select_packet` command
changes only the packet and `save_settings` changes only settings (even without a
selected packet); both require the current `expected_version` and return the new
`lobby` projection. The compatibility `save` command still updates both atomically.
Clients must retain unsaved settings drafts when updating the version after a
packet autosave, serialize mutations, and surface stale writes rather than silently
overwriting newer state. Apply Alembic migration
`0024_training_lobby` before deploying this flow.

Upload packet for training opens `/training?view=upload`; native document upload
uses default question values while the Mini App permits custom values. Both
adapters call authenticated, versioned gateway operations.

Sessions support incremental reading, buzz, deterministic answer matching, reveal,
skip, next, pause/resume, stop and replay. Paused sessions still exclude normal play.
Deadline jobs and authoritative snapshots recover progress after restarts. Account
locks serialize training start with normal-game entry. Commands carry expected
versions and question revision IDs. Reading cursors have a separate presentation
version so a newly displayed token does not invalidate a buzz on the same question.
Outbox events contain identifiers; Telegram fetches authorized text at delivery and
stores message identities for subsequent edits. Buzzing is bound to the question
last delivered in the chat, not an unseen later server question. Pausing freezes
reading, buzz, answer and hidden-text timers; resuming restores their remaining time.

Training-only question rows record opening time and accepted buzz time, active elapsed
seconds (excluding explicit pauses), and revealed-token cursor at the actual buzz,
even if no answer follows. Legacy unknown timing remains null. Attempts preserve the
original and effective verdicts. Correct answers add the question value; incorrect
answers and accepted-buzz answer timeouts subtract it. No-buzz timeouts, reveal and
skip are neutral. An answer in progress must be submitted or time out before reveal/skip.
A submitted answer on the current revealed question can be appealed once with `!`.
The solo trainee explicitly votes Yes/No within 60 seconds, as a one-player electorate.
Acceptance flips the verdict and corrects the training score; rejection/expiry leaves
it unchanged. The prior pause state is restored. Neither attempts nor appeals write
competitive scores, ratings, suspicion, player statistics or author totals. These
additive persistence fields require migration `0027_training_history_flow`.

Reversed mode reverses word order. Hidden mode immediately displays the whole
question for a configurable positive duration shorter than the buzz timer, then
hides it until the answer is revealed. Reconnection cannot redisplay expired text.
Hidden and reversed may combine. Best buzz excludes both; validated source character
boundaries map to the pinned announcement-token stream. Playback pauses once and
offers inline Continue reading while buzzing remains available. Missing preparation
is reported explicitly. Normal-game modifier hooks default to disabled.

## Training history

The home keyboard's **Training history** text button supplies an authenticated
inline Mini App launch at `/training/history` (a reply-keyboard WebApp would not
supply signed `initData`). Cards show the played date, pinned packet, effective
score, status and modifiers. **More info** opens `/training/history/{session_id}`.
Both screens use explicit **Load more** requests, at most 25 runs/questions each.
The run list uses descending `(created_at, id)` keysets; detail uses ascending
question positions. Cursors are bound to the owner and list/detail identity.

`TrainingHistoryService` is read-only and owner-scoped, including for administrators.
Scores join only training attempts/questions: correct `+value`, incorrect `-value`,
otherwise zero; appeals use the effective verdict and preserve the original one.
Anonymous revision-level question statistics aggregate training records only, never
competitive games/statistics; answers and performance are always from the owner's
selected run. Known buzz-delay samples are the latest 25 per question, SQL-bounded.
Counts do not imply missing timings were known. Unknown legacy timings display as
unavailable, never inferred from an attempt's generic creation timestamp.

History rechecks `TrainingPacketService.authorize` on each detail request. Source
retirement, deletion, archiving or library revocation removes protected question
content; packet/run metadata and the owner's submitted answers remain. Pinning and
whole-packet exposure claims do not prove playback: unvisited questions remain
redacted even after stopping the run. Recorded opening/buzz or a finished legacy
attempt proves play; unanswered questions of active/paused runs stay redacted so
history cannot bypass hidden/incremental reading. These forward-recorded timing
and appeal fields depend on integration-owned migration `0027_training_history_flow`;
this component creates no separate migration.

Targeted checks: `tests/unit/test_training_history*.py`,
`tests/integration/test_training_history.py`, `web/src/ui/training-history.test.ts`
and the real shell navigation regression in `web/src/app.test.ts`. Base-schema
checks are not acceptance of the forthcoming timing writer/migration; the timing
storage regression explicitly requires 0027.

## Knowledge and AI integration

[Contracts](training-contracts.md) describe source access and Best buzz ports.
Knowledge library is enabled in Telegram and at `/knowledge` in the Mini App.
Facts, tags, permanent copy sharing, automatic dissection application and metered analysis
use persistent storage. See the [integration guide](training-knowledge-integration.md)
for configuration, commands, limits, decisions and remaining placeholders.

Targeted tests: `tests/unit/test_training*.py`, `tests/integration/test_training.py`
and `web/src/ui/training.test.ts`. PostgreSQL tests require a disposable
`TEST_DATABASE_URL`. Live Telegram delivery and deployed concurrent-AI performance
still require deployment validation. Provider tests use fixtures without live calls.
