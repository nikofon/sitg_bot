"""Persist authorship exposure independently of games and attribution corrections."""

from collections.abc import Iterable
from datetime import UTC, datetime
from uuid import UUID

from sqlalchemy import func, select, text
from sqlalchemy.dialects.postgresql import insert
from sqlalchemy.ext.asyncio import AsyncSession

from sitg_bot.storage.models import (
    PacketQuestionRecord,
    PacketVersionRecord,
    PacketVersionTesterRecord,
    PlayerAuthorLinkRecord,
    PlayerExposureClaimRecord,
    PlayerTesterLinkRecord,
    QuestionRevisionRecord,
    ThemeRevisionRecord,
    TournamentManagerRecord,
)


async def tournament_manager_ids(
    session: AsyncSession, tournament_ids: Iterable[UUID]
) -> set[UUID]:
    """Active (non-revoked) manager player IDs of the given tournaments."""
    return set(
        await session.scalars(
            select(TournamentManagerRecord.player_id).where(
                TournamentManagerRecord.tournament_id.in_(tuple(tournament_ids)),
                TournamentManagerRecord.revoked_at.is_(None),
            )
        )
    )


async def lock_author_exposure(session: AsyncSession) -> None:
    await session.execute(select(func.pg_advisory_xact_lock(734918227163)))


async def burn_author_content(
    session: AsyncSession,
    *,
    version_id: UUID | None = None,
    author_id: UUID | None = None,
    tester_id: UUID | None = None,
    player_ids: Iterable[UUID] | None = None,
) -> None:
    """Burn canonical claims for linked authors and optionally explicit players.

    ``player_ids`` burns every theme and question of the scoped packet version for
    those players regardless of author links; it requires ``version_id`` so an
    explicit player burn can never become global.
    """
    if player_ids and version_id is None:
        raise ValueError("An explicit player burn requires a packet version")
    await session.flush()
    links_query = select(PlayerAuthorLinkRecord)
    if author_id is not None:
        links_query = links_query.where(PlayerAuthorLinkRecord.author_id == author_id)
    links: dict[UUID, set[UUID]] = {}
    if tester_id is None:
        for link in await session.scalars(links_query):
            links.setdefault(link.author_id, set()).add(link.player_id)
    # Historical membership protects the entire logical packet, including future
    # substitutions. Removing a tester never restores freshness.
    tester_players: dict[UUID, set[UUID]] = {}
    if author_id is None:
        tested = (
            select(PacketVersionRecord.packet_id, PlayerTesterLinkRecord.player_id)
            .join(
                PacketVersionTesterRecord,
                PacketVersionTesterRecord.packet_version_id == PacketVersionRecord.id,
            )
            .join(
                PlayerTesterLinkRecord,
                PlayerTesterLinkRecord.tester_id == PacketVersionTesterRecord.tester_id,
            )
        )
        if tester_id is not None:
            tested = tested.where(PlayerTesterLinkRecord.tester_id == tester_id)
        for packet_id, player_id in await session.execute(tested):
            tester_players.setdefault(packet_id, set()).add(player_id)
    if not links and not player_ids and not tester_players:
        return
    query = select(
        ThemeRevisionRecord, PacketVersionRecord.lead_author_id, PacketVersionRecord.packet_id
    ).join(PacketVersionRecord, PacketVersionRecord.id == ThemeRevisionRecord.packet_version_id)
    if version_id is not None:
        query = query.where(
            PacketVersionRecord.packet_id
            == select(PacketVersionRecord.packet_id)
            .where(PacketVersionRecord.id == version_id)
            .scalar_subquery()
        )
    for theme, lead_id, packet_id in (await session.execute(query)).all():
        questions = (
            await session.scalars(
                select(QuestionRevisionRecord)
                .join(
                    PacketQuestionRecord,
                    PacketQuestionRecord.question_revision_id == QuestionRevisionRecord.id,
                )
                .where(PacketQuestionRecord.theme_revision_id == theme.id)
            )
        ).all()
        authors = {lead_id, *theme.author_ids,
                   *(author_id for question in questions for author_id in question.author_ids)}
        players = set()
        if version_id is None or theme.packet_version_id == version_id:
            players.update(player for author in authors for player in links.get(author, ()))
            players.update(player_ids or ())
        players.update(tester_players.get(packet_id, ()))
        claims = {("theme", theme.theme_id), *(("question", q.question_id) for q in questions)}
        for burned_player_id in sorted(players):
            for namespace, claim_id in sorted(claims):
                statement = insert(PlayerExposureClaimRecord).values(
                    player_id=burned_player_id,
                    packet_version_id=theme.packet_version_id,
                    claim_namespace=namespace,
                    claim_id=claim_id,
                    state="burnt",
                    burnt_at=datetime.now(UTC),
                )
                await session.execute(
                    statement.on_conflict_do_update(
                        index_elements=["player_id", "claim_namespace", "claim_id"],
                        index_where=text("state IN ('reserved', 'burnt')"),
                        set_={
                            "state": "burnt",
                            "burnt_at": func.coalesce(
                                PlayerExposureClaimRecord.burnt_at, statement.excluded.burnt_at
                            ),
                        },
                    )
                )
