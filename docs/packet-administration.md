# Tournament Packet Administration

[Technical index](architecture.md) · [Mini App editor](mini-app.md)

## Source map

| Module | Responsibility |
| --- | --- |
| [packet_import.py](../src/sitg_bot/packet_import.py) | Bounded DOCX/PDF parsing, multi-packet splitting, JSON serialization/parsing |
| [domain/packet.py](../src/sitg_bot/domain/packet.py) | Packet/theme/question values and validation |
| [services/packets.py](../src/sitg_bot/services/packets.py) | Drafts, preview/edit, publication, management and authorization |
| [storage/packets.py](../src/sitg_bot/storage/packets.py) | Logical content, immutable revisions and publication persistence |
| [services/author_exposure.py](../src/sitg_bot/services/author_exposure.py) | Permanent exposure burns for linked authors, packet uploaders, and tournament managers |
| [services/library.py](../src/sitg_bot/services/library.py), [packet_export.py](../src/sitg_bot/packet_export.py) | Library access, exposure, and DOCX export |
| [packet_admin.py](../src/sitg_bot/packet_admin.py) | Trusted local administration CLI |

Start tests with [test_packet.py](../tests/unit/test_packet.py),
[test_packet_changes.py](../tests/unit/test_packet_changes.py), and
[test_packet_management.py](../tests/integration/test_packet_management.py).

## Scope

This document defines how authorized tournament users import, verify, publish, and
modify question packets. A packet must be assigned to at least one tournament but may
be assigned to several. Its logical identity and immutable revisions can therefore be
shared, while use and access are controlled independently by each tournament-packet
assignment. Server packet commands require permission in the relevant tournament. The
standalone local import tool requires an explicit tournament ID.

Initial draft import, Telegram upload, Mini App preview/editing, rejection, publication,
tournament scoping, packet management, corrections, and substitutions are implemented.
Member upload and original-file retention remain [planned work](future-work.md).

## Input formats

JSON has packet fields `name`, `themes`, optional `year`, `lead_author`, and `language`
(default `und`). Each theme has `name`, optional `author` and `commentary`, and `questions`.
Each question requires integer `value`, `text`, and `answer`; optional fields are `accepted_answers`
(string list), `form`, `commentary`, `source`, and `author`. Use
`packet_to_json`/`packet_from_data` in the importer as the serialization contract.
Uploads also accept an array of packet objects. JSON remains supported internally but is
not advertised in Telegram's upload prompt.

The DOCX converter uses Heading 1 for each packet name and Heading 2 for theme names
(including Russian style names), question lines such as `10. [answer form] Question text`,
and fields labeled `Ответ:`, `Зачёт:`, `Комментарий:`, `Источник:`, and `Автор:`/`Author:`.
A theme-level commentary line starts with `Комментарий к теме:` (or `Theme commentary:`)
after the theme heading or author line; following plain lines continue it.
Accepted alternatives in `Зачёт:` are comma-separated; `Источники:` is also supported.

PDF uploads use `pypdf` layout extraction and require selectable text; scanned images need
OCR before upload. Both PDF and DOCX recognize standalone `Бой I`/`Бой 1`, numbered or
spelled-out stage headings (`ПЕРВЫЙ ЭТАП`, `2-й ЭТАП`, `Этап 2`), `ГРАНД-ФИНАЛ`, `ФИНАЛ`,
and `ЗАПАС`. Section names are appended to the source filename without its extension;
Roman fight numbers become decimal, e.g. `Чемпионат Воронежа по СИ 2017. Бой 1`.
Text before the first section and theme indexes before the first theme are ignored.
Plain theme headings include `1. ТЕМА: Название`, `Тема 1. Название`, and `Тема: Название`.
Before a theme's first question, `Комментарий:` also denotes theme commentary.
Wrapped question fields continue across lines and pages. These are structural heuristics;
review the extracted text and correct interpretation errors in the editor.

Convert with `sitg-import-packet packet.pdf packet.json` (DOCX also works). Output is one
JSON object for a single packet or an array for several. The shared byte API is
`packets_from_document_bytes(source, source_filename)`; existing singular DOCX helpers
reject multi-packet input instead of silently discarding packets. Limits include 128 packets
per document, 512 PDF pages, and 8 MiB of extracted text. Uploads retain the existing 4 MiB
file and combined interpreted-content limits. These are separate from per-game SI limits.

## Initial import and verification

1. An authorized administrator or tournament manager imports a supported DOCX, PDF, or JSON
   file in a specific tournament context. Publishing creates tournament assignments using
   each destination's configured access defaults. Automatic library release is disabled by
   default and can be enabled with `packets_released_by_default`.
2. The application parses all packets and saves their drafts in one transaction without
   making them available for games. Each draft has the source filename and checksum.
   Telegram sends a separate preview/edit link and publish/reject controls for each packet.
   A parse failure creates a validation-failed draft; it does not import a partial batch.
   The upload response retains the existing summary for one draft; multiple drafts return
   an ordered `drafts` array of those summaries.
3. The application validates the draft and presents its complete interpreted content
   back to the uploader and authorized reviewers. The preview includes packet
   metadata, ruleset-specific content structure and ordering, resolved authors, and
   every question's interpreted fields. For SI this includes themes, question values,
   text, accepted answers, form, commentary, and source.
4. Structural errors prevent confirmation. Warnings are displayed but may be accepted
   by an authorized reviewer.
5. If the interpretation is wrong, the uploader or reviewer edits the interpreted draft in the
   ruleset-provided Mini App editor and revalidates it, or rejects it and imports a corrected file.
6. If the interpretation is correct, an authorized reviewer confirms it. The bot
   publishes the complete packet to PostgreSQL in one transaction. Publication does
   not itself make the packet visible or playable; tournament stage and access policy
   determine those rights. It permanently burns every theme and question of the
   published version for the uploader (when one is recorded) and every active
   manager of each destination tournament, so none of them can ever play it.

The preview must represent exactly the content that confirmation would publish. A
confirmation identifies a specific draft, preventing an older command from publishing
a later import.

JSON imports accept optional top-level `year` and `lead_author` fields. The current
DOCX/PDF converter does not infer either field, so converted drafts leave them empty.

The Mini App's packet details list distinct author names found in packet, theme, and question
author fields. Each name can be associated with a registered author or a newly registered author
using the same name, surname, optional second name, and Telegram-link form as tournament settings.
The lead author can also be selected or registered there. Associations preserve the source spelling
in the draft while publication uses the chosen author IDs. Saving adds these authors to the
tournament's author list; names without an explicit association create separate author records,
never an automatic merge by name. Repeated saves reuse the persisted associations. Publication
also adds authors when the draft is published directly from Telegram without an editor save.

A draft has an explicit lifecycle:

```text
valid import → awaiting confirmation ┬→ published
                                     └→ rejected
invalid import → validation failed
```

Rejected and failed drafts are never used in games and currently remain in PostgreSQL.
The runtime stores their interpreted content, source filename, and checksum, but not
the original source file. Draft and source-file retention remain future decisions.

## Published content and revisions

Published content is never changed in place. Every confirmed modification creates a
new immutable packet version for future games. A game retains the exact packet and
question revisions with which it started, so an active or historical game cannot
change underneath its players.

The database distinguishes a logical content item from its revisions. At minimum:

```text
logical packet   → packet versions
logical question → question revisions
```

Game rulesets may add versioned logical containers, such as SI themes, without making
those containers mandatory for every packet.

The logical ID determines statistical continuity. The revision ID identifies the exact
text and metadata that were played.

## Corrections

A correction fixes a negligible mistake without materially changing what the question
tests, which answers are substantively correct, or its difficulty. A
typo, punctuation error, or formatting problem is normally a correction.

When an authorized tournament editor confirms a correction:

- the bot creates a new packet version and question revision;
- the logical packet and logical question IDs remain unchanged;
- existing games continue using their pinned revisions;
- future games use the corrected revision;
- all past statistics remain attached to the same logical question, and future results
  continue accumulating with them.

Statistics may still be inspected per revision for auditing, but ordinary question
statistics aggregate all correction revisions under the logical question ID.

Authorship corrections update the logical packet/theme/question's statistical author, transferring
attribution without rewriting played revisions or result facts. Existing authorship counts use
logical identities, including retained historical content. Theme-author corrections also transfer
questions attributed to that theme author unless their own author fields were separately changed.
All themes containing a linked author's work (and every question in those themes) are permanently
burnt for that player. Changing authorship never removes or resets the former author's burns.

Correction-only saves replace the edited version in every active tournament assignment using it.
The previous version is marked deleted and archived; its underlying rows remain as immutable game
snapshots and statistical evidence. Independently substituted versions are not overwritten.

## Substitutions

A substitution materially changes a question's meaning, expected knowledge, difficulty,
or set of substantively correct answers, or replaces it completely.

When an authorized tournament editor confirms a substitution:

- the old logical question is replaced in this tournament but retained for history and other tournaments;
- the replacement receives a new logical question ID and its first revision;
- a new packet version places the replacement in the old question's position;
- existing games continue using the old question revision;
- future games use the replacement;
- the old question keeps its accumulated statistics, while the replacement starts with
  no question statistics.

In SI, the packet and its unchanged themes retain their logical identities across
versions. Theme and question claims therefore continue across corrections: substituting
one question does not make an already burnt theme fresh again. Packet statistics can be
reported both across the logical packet and for an exact packet version so material
substitutions remain visible. Other game rulesets define their own optional content
containers, play units, and canonical claims.

Substituting a theme name replaces the entire theme and every question with new logical identities,
fresh statistical histories, and fresh exposure identities. A question-only substitution preserves
the theme identity, so an already burnt theme stays burnt.

Every confirmed correction or substitution save also permanently burns the new version's themes
and questions for the editing actor and every active manager of the editing tournament. Already
burnt identities stay burnt; freshly substituted theme identities are burnt with the save.

A save containing any substitution changes only the current tournament assignment. The old version
is inaccessible through that assignment, but other tournaments keep their existing version and
rights; managers of those tournaments receive a notification naming the manager who substituted it.
The old version is archived when no active assignment still adopts it. Assignments that previously
followed the latest version are pinned before branching so substitutions cannot spread implicitly.

## Modification workflow

1. A tournament manager selects Modify on a packet card in Tournament management.
2. The upload-style editor displays the assignment's adopted version with every field locked.
3. A green pencil enables a field as a correction. Theme names and question text/answer fields
   also offer a red substitution arrow; theme commentary is correction-only. Author fields
   resolve registered identities independently.
4. Save changes verifies that every enabled field changed and every change was classified,
   validates the result in each affected tournament, and publishes in one transaction.
5. A concurrent edit or deleted assignment rejects the stale save. Managers reload and review
   the current version before trying again.

Each save retains a confirmed draft, the previous-version link, actor, timestamp, overall change
type, and the per-field change classification in the immutable version's change-reason record.
Assignment-wide and per-player rights stay on the same assignment; library release transfers to
the new version. Assembling lobbies lose readiness and revalidate their selections; assigned games
continue with their snapshots.

Delete retires only the current tournament assignment, preventing discovery, play, and library
access there without changing results or statistics. Release opens the adopted version's global
library gate. The read decision requires both this gate and tournament-scoped read eligibility;
the library reader itself remains unimplemented.

## Validation rules

Every packet has at least one question and declares or can be validated against a
compatible game-ruleset content schema. Assignment to a tournament is rejected when
the packet is incompatible with that tournament's selected game ruleset or configured
parameters.

### Current SI validation

The current importer implements the SI content schema. SI domain and ruleset validation
require:

- a non-empty packet name and at least one theme;
- non-empty theme names;
- one ordered question at each value in the tournament's effective `question_values`;
- non-empty question text and primary answer;
- a four-digit creation year when a year is supplied.

The non-blocking warnings cover a
missing lead author, missing theme or resolved question author, theme counts outside
the recommended 8–12 range, duplicate normalized question text, and missing question
form or source. All author fields are optional and a draft with warnings may be
published.

SI values are stored on packet question placements. Import validates those values against
the creation tournament's current SI parameters; selection and assignment validate the
pinned packet version again in the applicable tournament context.

## Persistence implications

The implemented initial-publication model stores:

- packet-import drafts and their status;
- the creation tournament and intended assignments on every packet draft;
- explicit tournament-packet assignments and their lifecycle;
- logical packets and immutable packet versions;
- optional packet-version creation year and lead-author reference, plus a BCP 47-style
  language tag (`und` when omitted);
- owner-controlled packet-version library-release time;
- logical questions and immutable question revisions;
- the packet content schema and compatible game-ruleset versions;
- the source file name, checksum, uploader, and confirmation timestamps;
- the packet version pinned to each game and question revision pinned to each round;
- assignment-wide member rights and per-player packet entitlement grants/revocations;
- assignment-wide library viewing rules, independent of playability.

Correction and substitution saves also store field classifications, publishing actor, previous
version, and logical statistical attribution. They enforce assignment-version concurrency checks.

The parsed packet content is stored in PostgreSQL. The original DOCX is an import
artifact, not the runtime source of truth; whether it is archived separately and for
how long is a retention decision.

## Member uploads

Member-facing upload commands and moderation are not implemented. They will use the same
validation, explicit tournament context, and immutable versioning principles while retaining
the uploader. Upload permission will not automatically grant publication, game-selection,
editorial, or content access.
Requirements and open moderation questions are in
[future-work.md](future-work.md).

## Visibility and authorization

Packet assignment, discoverability, selection eligibility, question-content access,
and editorial access are separate states. Every list, preview, download, assignment,
and modification operation enforces the relevant tournament entitlement. Access
through one tournament does not carry into another tournament using the same packet.
Enrollment alone grants none of these rights implicitly. A game may pin an authorized
packet revision without granting every tournament member access to that revision's
questions.

Tournament-scoped packet rights and their current composition are defined in
[tournaments.md](tournaments.md).

Assigning an already-published packet to a tournament burns the adopted version (or the
packet's latest published version) for that tournament's active managers. Granting a
tournament manager role retroactively burns every version currently assigned to that
tournament for the new manager, including on re-grant after revocation. Burns are
permanent; removing the role or the assignment never resets them.
