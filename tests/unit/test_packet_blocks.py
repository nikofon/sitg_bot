"""SI content adapter unit tests for player packet blocks."""

from uuid import uuid4

from sitg_bot.services.ruleset_content import PacketSelection, SIContentAdapter
from sitg_bot.storage.models import PacketQuestionRecord, ThemeRevisionRecord


class FakeResult:
    def __init__(self, rows):
        self._rows = list(rows)

    def all(self):
        return list(self._rows)

    def tuples(self):
        return list(self._rows)

    def scalars(self):
        return list(self._rows)


class FakeSession:
    def __init__(self, results):
        self._results = list(results)
        self.queries = []

    async def execute(self, query):
        self.queries.append(query)
        return self._results.pop(0)


def _theme(version_id, position=1):
    return ThemeRevisionRecord(
        theme_id=uuid4(),
        packet_version_id=version_id,
        revision_number=1,
        position=position,
        name="Theme",
    )


async def test_available_play_units_skips_themes_of_blocked_packets() -> None:
    version_id = uuid4()
    packet_id = uuid4()
    theme = _theme(version_id)
    session = FakeSession([
        FakeResult([(theme, packet_id)]),  # theme rows joined with packet ids
        FakeResult([]),  # exposure claims
        FakeResult([packet_id]),  # blocked packet ids
    ])
    units = await SIContentAdapter().available_play_units(
        session, [PacketSelection(version_id, 1)], [uuid4()]
    )
    assert units == []
    # No per-theme question query runs: blocked themes are treated as burnt.
    assert len(session.queries) == 3


async def test_available_play_units_keeps_unblocked_themes() -> None:
    version_id = uuid4()
    packet_id = uuid4()
    other_packet_id = uuid4()
    theme = _theme(version_id)
    question_revision_id = uuid4()
    placement = PacketQuestionRecord(
        packet_version_id=version_id,
        theme_revision_id=theme.id,
        question_revision_id=question_revision_id,
        position=1,
        value=10,
    )
    session = FakeSession([
        FakeResult([(theme, packet_id)]),
        FakeResult([]),
        FakeResult([other_packet_id]),
        FakeResult([(placement, uuid4())]),
    ])
    units = await SIContentAdapter().available_play_units(
        session, [PacketSelection(version_id, 1)], [uuid4()]
    )
    assert len(units) == 1
    assert units[0].question_revision_ids == (question_revision_id,)
