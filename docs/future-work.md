# Planned Features

[Technical index](architecture.md) · [Product baseline](../README.md)

This is the inventory of unimplemented features and unresolved product decisions.
Existing behavior belongs in the linked subsystem guides.

| Area | Remaining work and implementation direction |
| --- | --- |
| Historical packet access | Define post-game/post-tournament visibility beyond current library entitlements. See [packets](packet-administration.md). |
| Upload moderation and retention | Optional review requirements, cross-tournament assignment consent, quotas, copyright handling, and source/draft retention. Opt-in community upload/publication is implemented. |
| Additional rulesets | Generalize SI-specific execution persistence before registering another ruleset. Define content-schema compatibility and meaningful cross-ruleset statistics; preserve the existing assignment/claim contract. See [game rulesets](game-rulesets.md). |
| Statistics and history | Player profiles with rating history, placement and SI statistics, game packet labels, and rating snapshots are implemented, as are tournament leaders and player-facing author profiles with aggregate SI performance. Player profiles still expose only recent game cards. Remaining: full-history pagination/search/sorting, packet statistics, author appeal statistics, and broader query endpoints, including metric visibility and projection refresh policy. Author reputation still needs a product definition. See [statistics](data-and-statistics.md). |
| Privacy maintenance | Decide answer-text retention and player-anonymization authorization/behavior before exposing or scheduling existing helpers. Their method defaults are not accepted product policy. |
| Remaining presentation | Replace Telegram/Mini App placeholders as backend projections become available, including history/statistics and remaining notification workflows. Reuse shared authentication, navigation, localization, and durable delivery. See [Telegram](telegram.md) and [Mini App](mini-app.md). |

Tournament type and ruleset can already change during setup and become immutable at setup
finalization. Changes after finalization would require an explicit new product decision.
Additional appeal electorates/authorities and score-display conventions also need decisions
before expanding the current behavior.

Native Telegram SI gameplay, recovery, participant chat, appeals, token workflows, manager
settings, packet uploads, corrections/substitutions, outbox delivery, durable jobs, player
bans with bug reporting, and administrator management with suspicion review are
implemented. Live Telegram validation remains manual; extend automated coverage alongside
each new feature.
