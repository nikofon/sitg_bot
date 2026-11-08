# Planned Features

[Technical index](architecture.md) · [Product baseline](../README.md)

This is the inventory of unimplemented features and unresolved product decisions.
Existing behavior belongs in the linked subsystem guides.

| Area | Remaining work and implementation direction |
| --- | --- |
| Training | Group training and product selection of normal-game modifiers. Outstanding statistics inputs, legacy dissection identity compatibility and commercial categories are tracked in [Knowledge requirements](training-knowledge-integration.md#outstanding-requirements-and-limitations); real-provider/deployment acceptance is in [Training AI](training-ai.md#enabling-and-live-acceptance-operator-steps-not-automated-test-claims). |
| Historical packet access | Define post-game/post-tournament visibility beyond current library entitlements. See [packets](packet-administration.md). |
| Upload moderation and retention | Optional review requirements, cross-tournament assignment consent, quotas, copyright handling, and source/draft retention. Opt-in community upload/publication is implemented. |
| Additional rulesets | Generalize SI-specific execution persistence before registering another ruleset. Define content-schema compatibility and meaningful cross-ruleset statistics; preserve the existing assignment/claim contract. See [game rulesets](game-rulesets.md). |
| Statistics and history | Full competitive-history pagination/search/sorting beyond recent game cards, packet statistics, author appeal statistics, and broader query endpoints, including metric visibility and projection refresh policy. Author reputation still needs a product definition. See [statistics](data-and-statistics.md). |
| Privacy maintenance | Decide answer-text retention and player-anonymization authorization/behavior before exposing or scheduling existing helpers. Their method defaults are not accepted product policy. |
| Remaining presentation | Replace Telegram/Mini App placeholders as backend projections become available, including history/statistics and remaining notification workflows. Reuse shared authentication, navigation, localization, and durable delivery. See [Telegram](telegram.md) and [Mini App](mini-app.md). |

Tournament type and ruleset can already change during setup and become immutable at setup
finalization. Changes after finalization would require an explicit new product decision.
Additional appeal electorates/authorities and score-display conventions also need decisions
before expanding the current behavior.

Live Telegram validation remains manual. Training still needs deployed concurrent-AI
performance measurements; fixture benchmarks do not establish production capacity.
