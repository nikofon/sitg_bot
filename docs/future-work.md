# Planned Features

[Technical index](architecture.md) · [Product baseline](../README.md)

This is the inventory of unimplemented features and unresolved product decisions.
Existing behavior belongs in the linked subsystem guides.

| Area | Remaining work and implementation direction |
| --- | --- |
| Classic tournaments | Add a Swiss first-stage algorithm. Groups, quiz, play-offs, double elimination, Chairs, and round deadlines are implemented. See [tournaments](tournaments.md). |
| Historical packet access | Define post-game/post-tournament visibility beyond current library entitlements. See [packets](packet-administration.md). |
| Member uploads and moderation | Extend the existing manager upload and correction/substitution workflow. Decide upload/publication permissions, review requirements, cross-tournament assignment consent, quotas, copyright handling, and source/draft retention. |
| Additional rulesets | Generalize SI-specific execution persistence before registering another ruleset. Define content-schema compatibility and meaningful cross-ruleset statistics; preserve the existing assignment/claim contract. See [game rulesets](game-rulesets.md). |
| Statistics and history | Player profiles with per-ruleset rating history, win-rate/placement distribution, SI per-question-value statistics, and privacy-aware game result grids are implemented. Remaining: leaderboards, packet/author statistics, and broader query endpoints, including metric visibility and projection refresh policy. Author reputation still needs a product definition. See [statistics](data-and-statistics.md). |
| Privacy maintenance | Decide answer-text retention and player-anonymization authorization/behavior before exposing or scheduling existing helpers. Their method defaults are not accepted product policy. |
| Remaining presentation | Replace Telegram/Mini App placeholders as backend projections become available, including history/statistics and remaining author-link/notification workflows. Reuse shared authentication, navigation, localization, and durable delivery. See [Telegram](telegram.md) and [Mini App](mini-app.md). |

Tournament type and ruleset can already change during setup and become immutable at setup
finalization. Changes after finalization would require an explicit new product decision.
Additional appeal electorates/authorities and score-display conventions also need decisions
before expanding the current behavior.

Native Telegram SI gameplay, recovery, participant chat, appeals, token workflows, manager
settings, packet uploads, corrections/substitutions, outbox delivery, durable jobs, player
bans with bug reporting, and the admin suspicion ledger are
implemented. Live Telegram validation remains manual; extend automated coverage alongside
each new feature.
