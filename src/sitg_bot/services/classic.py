"""Transactional Classic competition lifecycle and prescribed game assignment."""

import random
import secrets
from collections import Counter
from datetime import UTC, datetime
from decimal import Decimal, InvalidOperation
from uuid import UUID, uuid4

from sqlalchemy import and_, delete, select
from sqlalchemy.ext.asyncio import AsyncSession

from sitg_bot.domain.classic import (
    SCHEMES,
    balanced_groups,
    competition_points,
    order_results,
    standings,
)
from sitg_bot.domain.game_rulesets import DEFAULT_RULESETS
from sitg_bot.services.concurrency import StaleWriteError
from sitg_bot.storage.database import Database
from sitg_bot.storage.models import (
    AnswerAttemptRecord,
    ClassicMatchRecord,
    ClassicRoundRecord,
    ClassicStageRecord,
    GameParticipantRecord,
    GameRecord,
    GameThemeRecord,
    PacketQuestionRecord,
    PlayerRecord,
    QuestionRoundRecord,
    RulesetRatingRecord,
    TournamentMembershipRecord,
    TournamentPacketAssignmentRecord,
    TournamentPolicyVersionRecord,
    TournamentRecord,
)


def is_chair(seat: str) -> bool:
    return seat.startswith("chair:")


class ClassicService:
    def __init__(self, database: Database) -> None:
        self.database = database

    @staticmethod
    async def stages(session: AsyncSession, tournament_id: UUID) -> list[ClassicStageRecord]:
        return list(
            await session.scalars(
                select(ClassicStageRecord)
                .where(ClassicStageRecord.tournament_id == tournament_id)
                .order_by(ClassicStageRecord.kind)
            )
        )

    @staticmethod
    async def rounds(session: AsyncSession, stage_id: UUID) -> list[ClassicRoundRecord]:
        return list(
            await session.scalars(
                select(ClassicRoundRecord)
                .where(ClassicRoundRecord.stage_id == stage_id)
                .order_by(ClassicRoundRecord.number)
            )
        )

    @staticmethod
    async def matches(session: AsyncSession, stage_id: UUID) -> list[ClassicMatchRecord]:
        return list(
            await session.scalars(
                select(ClassicMatchRecord)
                .join(ClassicRoundRecord, ClassicRoundRecord.id == ClassicMatchRecord.round_id)
                .where(ClassicRoundRecord.stage_id == stage_id)
                .order_by(
                    ClassicRoundRecord.number,
                    ClassicMatchRecord.group_number,
                    ClassicMatchRecord.number,
                )
            )
        )

    @staticmethod
    async def eligible(session: AsyncSession, tournament_id: UUID) -> list[PlayerRecord]:
        return list(
            await session.scalars(
                select(PlayerRecord)
                .join(
                    TournamentMembershipRecord,
                    TournamentMembershipRecord.player_id == PlayerRecord.id,
                )
                .where(
                    TournamentMembershipRecord.tournament_id == tournament_id,
                    TournamentMembershipRecord.status.in_(("approved", "active")),
                    PlayerRecord.status == "active",
                )
                .order_by(PlayerRecord.id)
            )
        )

    async def mutate(
        self,
        tournament_id: UUID,
        manager_id: UUID,
        *,
        expected_version: int,
        command: str,
        kind: str,
        values: dict,
    ) -> None:
        from sitg_bot.services.tournaments import TournamentService

        async with self.database.transaction() as session:
            tournament = await session.get(TournamentRecord, tournament_id, with_for_update=True)
            if tournament is None:
                raise LookupError("Tournament not found")
            await TournamentService._require_manager(session, tournament_id, manager_id)
            await TournamentService.require_modifiable(session, tournament_id)
            context = await TournamentService(self.database).context(session, tournament_id)
            if context.type_key != "classic":
                raise ValueError("Classic tournament required")
            if tournament.settings_version != expected_version:
                raise StaleWriteError("Tournament settings have changed")
            if tournament.status != "active":
                raise ValueError("Tournament is closed")
            if kind not in {"first", "playoff"}:
                raise ValueError("Unknown stage")
            stages = await self.stages(session, tournament_id)
            stage = next((s for s in stages if s.kind == kind), None)
            if stage is None:
                stage = ClassicStageRecord(
                    tournament_id=tournament_id,
                    kind=kind,
                    stage_type="none",
                    random_seed=secrets.token_hex(16),
                )
                session.add(stage)
                await session.flush()
                stages.append(stage)
            if command == "configure":
                if stage.started_at or (
                    kind == "first" and any(s.kind == "playoff" and s.started_at for s in stages)
                ):
                    raise ValueError("Started stage type and scheme are locked")
                await self._configure(session, stage, values)
            elif command == "seed":
                if stage.started_at or stage.stage_type == "none":
                    raise ValueError("Seeding requires a configured, unstarted stage")
                if kind == "playoff" and any(
                    s.kind == "first" and s.stage_type != "none" for s in stages
                ):
                    raise ValueError("Play-off seeds come from first-stage standings")
                stage.seeds = await self._seed(session, stage, context.ruleset_key, values)
            elif command == "round":
                await self._round(session, stage, values)
            elif command == "start":
                if tournament.finalized_at is None:
                    raise ValueError("Finalize tournament setup before starting a stage")
                limits = [
                    v
                    for v in (
                        context.type_rules.get("maximum_participants"),
                        context.policies.get("maximum_participants"),
                    )
                    if isinstance(v, int) and not isinstance(v, bool)
                ]
                if limits and len(await self.eligible(session, tournament_id)) > min(limits):
                    raise ValueError("Participant count exceeds the tournament limit")
                await self._reconcile(session, tournament_id)
                await self._start(
                    session, tournament, stage, stages, context.ruleset_key, manager_id
                )
            else:
                raise ValueError("Unknown Classic command")
            tournament.settings_version += 1

    @staticmethod
    def _decimal(value: object) -> Decimal:
        try:
            number = Decimal(str(value))
        except InvalidOperation as error:
            raise ValueError("Scoring parameters must be numbers") from error
        if (
            not number.is_finite()
            or abs(number) >= Decimal("1e16")
            or number.as_tuple().exponent < -8
        ):
            raise ValueError("Scoring parameters must be finite with at most eight decimals")
        return number

    async def _configure(
        self, session: AsyncSession, stage: ClassicStageRecord, values: dict
    ) -> None:
        stage_type = values.get("stage_type", "none")
        allowed = {"none", "groups", "quiz"} if stage.kind == "first" else {"none", "playoff"}
        if stage_type not in allowed:
            raise ValueError("Unsupported stage type (Swiss is not implemented)")
        scheme_key = values.get("scheme_key") if stage_type in {"groups", "playoff"} else None
        scheme = SCHEMES.get(scheme_key)
        if stage_type in {"groups", "playoff"} and (scheme is None or scheme["kind"] != stage_type):
            raise ValueError("Choose a scheme from the stage library")
        if stage.kind == "first":
            points = values.get("place_points", ["4", "3", "2", "1"])
            if not isinstance(points, list) or not 1 <= len(points) <= 12:
                raise ValueError("Provide between one and twelve place awards")
            stage.place_points = [str(self._decimal(p)) for p in points]
            stage.score_multiplier = self._decimal(values.get("score_multiplier", "0.02"))
        else:
            stage.place_points = []
            stage.score_multiplier = Decimal(0)
        changed = stage.stage_type != stage_type or stage.scheme_key != scheme_key
        stage.stage_type, stage.scheme_key = stage_type, scheme_key
        if changed:
            stage.seeds = []
            await session.execute(
                delete(ClassicRoundRecord).where(ClassicRoundRecord.stage_id == stage.id)
            )
            count = scheme["round_count"] if scheme else (1 if stage_type == "quiz" else 0)
            session.add_all(
                ClassicRoundRecord(stage_id=stage.id, number=n) for n in range(1, count + 1)
            )

    async def _seed(
        self, session: AsyncSession, stage: ClassicStageRecord, ruleset_key: str, values: dict
    ) -> list:
        players = await self.eligible(session, stage.tournament_id)
        ids = {str(p.id) for p in players}
        if not ids:
            raise ValueError("Approve participants before seeding")
        size = SCHEMES[stage.scheme_key]["size"] if stage.scheme_key else 1
        if stage.kind == "playoff" and len(ids) > size:
            raise ValueError("Too many participants: change the scheme or revoke registrations")
        if values.get("mode") == "manual":
            groups = values.get("seeds")
            if (
                not isinstance(groups, list)
                or not groups
                or any(not isinstance(g, list) or len(g) != size for g in groups)
            ):
                raise ValueError("Manual seeding must fill each group's slots (null means Chair)")
            flattened = [s for g in groups for s in g if s is not None]
            if any(not isinstance(s, str) for s in flattened):
                raise ValueError("Invalid player seed")
            if len(flattened) != len(ids) or set(flattened) != ids:
                raise ValueError("Seed every approved participant exactly once")
            if len(groups) != (len(ids) + size - 1) // size:
                raise ValueError("Use only the groups needed for the participant count")
            return groups
        if values.get("mode", "automatic") not in {"automatic", "random"}:
            raise ValueError("Unknown seeding mode")
        if stage.stage_type == "groups" and values.get("mode") != "random":
            ratings = dict(
                (str(p), Decimal(r))
                for p, r in (
                    await session.execute(
                        select(RulesetRatingRecord.player_id, RulesetRatingRecord.rating).where(
                            RulesetRatingRecord.ruleset_key == ruleset_key,
                            RulesetRatingRecord.player_id.in_([p.id for p in players]),
                        )
                    )
                ).all()
            )
            return balanced_groups({p: ratings.get(p, Decimal(1000)) for p in ids}, size)
        ordered = sorted(ids)
        random.SystemRandom().shuffle(ordered)
        return [
            ordered[i : i + size] + [None] * max(0, size - len(ordered[i : i + size]))
            for i in range(0, len(ordered), size)
        ]

    async def _round(self, session: AsyncSession, stage: ClassicStageRecord, values: dict) -> None:
        if not values.get("round_id"):
            raise ValueError("Round ID is required")
        round_record = await session.get(ClassicRoundRecord, UUID(str(values["round_id"])))
        if round_record is None or round_record.stage_id != stage.id:
            raise LookupError("Stage round not found")
        assignment_id = UUID(str(values["assignment_id"])) if values.get("assignment_id") else None
        if assignment_id:
            assignment = await session.get(TournamentPacketAssignmentRecord, assignment_id)
            if (
                assignment is None
                or assignment.tournament_id != stage.tournament_id
                or assignment.status != "active"
            ):
                raise ValueError("Choose an active tournament packet")
            other = await session.scalar(
                select(ClassicRoundRecord.id)
                .join(ClassicStageRecord)
                .where(
                    ClassicStageRecord.tournament_id == stage.tournament_id,
                    ClassicRoundRecord.assignment_id == assignment_id,
                    ClassicRoundRecord.id != round_record.id,
                )
            )
            if other:
                raise ValueError("A packet may be assigned to only one round in a tournament")
        matches = list(
            await session.scalars(
                select(ClassicMatchRecord).where(ClassicMatchRecord.round_id == round_record.id)
            )
        )
        if round_record.assignment_id != assignment_id and any(
            m.game_id or (m.results and any(not is_chair(s) for s in m.seats)) for m in matches
        ):
            raise ValueError("A round's packet is locked after its first game starts or resolves")
        deadline = values.get("start_deadline")
        if deadline is not None:
            deadline = datetime.fromisoformat(deadline) if isinstance(deadline, str) else deadline
            if not isinstance(deadline, datetime) or deadline.tzinfo is None:
                raise ValueError("Round start deadline must include a timezone")
        for field in ("discoverable", "playable"):
            if field not in values:
                continue
            if values[field] is not None and not isinstance(values[field], bool):
                raise ValueError("Packet switches must be boolean")
            if values[field] and stage.started_at is None:
                raise ValueError(
                    "Start the stage before making its packets discoverable or playable"
                )
            setattr(round_record, field, values[field])
        round_record.assignment_id, round_record.start_deadline = assignment_id, deadline

    async def _start(
        self,
        session: AsyncSession,
        tournament: TournamentRecord,
        stage: ClassicStageRecord,
        stages: list,
        ruleset_key: str,
        manager_id: UUID,
    ) -> None:
        if stage.started_at or stage.stage_type == "none":
            raise ValueError("Stage is disabled or already started")
        first = next((s for s in stages if s.kind == "first" and s.stage_type != "none"), None)
        if stage.kind == "playoff" and first:
            if not first.completed_at:
                raise ValueError("Finish the first stage before starting play-off")
            ranking = standings(
                await self.matches(session, first.id),
                quiz=first.stage_type == "quiz",
                seed=first.random_seed,
            )
            size = SCHEMES[stage.scheme_key]["size"]
            seeds = [r["seat"] for r in ranking[:size]]
            stage.seeds = [seeds + [None] * (size - len(seeds))]
        else:
            if not stage.seeds:
                stage.seeds = await self._seed(session, stage, ruleset_key, {"mode": "automatic"})
            # Registrations can change between previewing seeds and starting.
            await self._seed(session, stage, ruleset_key, {"mode": "manual", "seeds": stage.seeds})
        now = datetime.now(UTC)
        stage.seeds = [[seat or f"chair:{uuid4()}" for seat in group] for group in stage.seeds]
        for seat in (s for group in stage.seeds for s in group):
            if is_chair(seat):
                session.add(
                    PlayerRecord(id=UUID(seat[6:]), status="anonymized", public_nickname="Chair")
                )
            else:
                membership = await session.get(
                    TournamentMembershipRecord, (tournament.id, UUID(seat))
                )
                membership.status = "active"
                membership.participation_confirmed_at = now
                membership.participation_confirmed_by_id = manager_id
        stage.started_at = now
        if tournament.actual_starts_at is None:
            tournament.actual_starts_at = now
        tournament.registration_open_override = False
        tournament.registration_open = False
        tournament.participants_finalized_at = now
        rounds = {r.number: r for r in await self.rounds(session, stage.id)}
        for group_number, group in enumerate(stage.seeds, 1):
            games = (
                SCHEMES[stage.scheme_key]["games"]
                if stage.scheme_key
                else [{"round": 1, "game": 1, "sources": [1]}]
            )
            for game in games:
                sources = game["sources"]
                seats = (
                    [group[s - 1] for s in sources]
                    if all(isinstance(s, int) for s in sources)
                    else []
                )
                session.add(
                    ClassicMatchRecord(
                        round_id=rounds[game["round"]].id,
                        group_number=group_number,
                        number=game["game"],
                        sources=sources,
                        seats=seats,
                    )
                )
        await session.flush()
        await self._reconcile(session, tournament.id)

    async def reconcile(self) -> None:
        async with self.database.sessions() as session:
            ids = list(
                await session.scalars(
                    select(ClassicStageRecord.tournament_id)
                    .where(
                        ClassicStageRecord.started_at.is_not(None),
                        ClassicStageRecord.completed_at.is_(None),
                    )
                    .distinct()
                )
            )
        for tournament_id in ids:
            async with self.database.transaction() as session:
                await session.get(TournamentRecord, tournament_id, with_for_update=True)
                await self._reconcile(session, tournament_id)

    async def _reconcile(self, session: AsyncSession, tournament_id: UUID) -> None:
        now = datetime.now(UTC)
        for stage in await self.stages(session, tournament_id):
            if not stage.started_at or stage.completed_at:
                continue
            rounds = {r.id: r for r in await self.rounds(session, stage.id)}
            matches = await self.matches(session, stage.id)
            by_number = {(rounds[m.round_id].number, m.group_number, m.number): m for m in matches}
            for match in matches:
                if match.results is not None:
                    continue
                if not match.seats:
                    seats = []
                    for r, g, p in match.sources:
                        previous = by_number[(r, match.group_number, g)]
                        if previous.results is None:
                            break
                        seats.append(previous.results[p - 1]["seat"])
                    if len(seats) != len(match.sources):
                        continue
                    match.seats = seats
                game = await session.get(GameRecord, match.game_id) if match.game_id else None
                if game and game.status == "finalized":
                    await self._collect_results(session, stage, match, game)
                    continue
                if game and game.status in {"lobby", "active", "completed"}:
                    continue
                if game:
                    match.game_id = None
                deadline = rounds[match.round_id].start_deadline
                if all(is_chair(s) for s in match.seats) or (deadline and now >= deadline):
                    seats = list(match.seats)
                    random.Random(f"{stage.random_seed}:{match.id}").shuffle(seats)
                    match.results = [
                        {
                            "seat": s,
                            "score": "0",
                            "place": str(i),
                            "points": str(
                                competition_points(Decimal(i), 1, stage.place_points)
                                if stage.kind == "first" else Decimal(0)
                            ),
                            "correct_values": [],
                            "key": [str(-i)],
                        }
                        for i, s in enumerate(seats, 1)
                    ]
                    match.randomized = True
            if matches and all(m.results is not None for m in matches):
                stage.completed_at = now

    @staticmethod
    async def _collect_results(
        session: AsyncSession,
        stage: ClassicStageRecord,
        match: ClassicMatchRecord,
        game: GameRecord,
    ) -> None:
        participants = list(
            await session.scalars(
                select(GameParticipantRecord)
                .where(GameParticipantRecord.game_id == game.id)
                .order_by(GameParticipantRecord.seat)
            )
        )
        ruleset = DEFAULT_RULESETS.get(
            game.assignment_plan["ruleset_key"], game.assignment_plan["ruleset_version"]
        )
        parameters = ruleset.parameters(game.assignment_plan["parameters"])
        occupied = Counter(p.final_place for p in participants)
        results = []
        for participant in participants:
            correct = list(
                await session.scalars(
                    select(PacketQuestionRecord.value)
                    .join(
                        QuestionRoundRecord,
                        QuestionRoundRecord.question_revision_id
                        == PacketQuestionRecord.question_revision_id,
                    )
                    .join(
                        GameThemeRecord,
                        and_(
                            GameThemeRecord.game_id == game.id,
                            GameThemeRecord.theme_revision_id
                            == PacketQuestionRecord.theme_revision_id,
                            GameThemeRecord.source_packet_version_id
                            == PacketQuestionRecord.packet_version_id,
                        ),
                    )
                    .join(
                        AnswerAttemptRecord, AnswerAttemptRecord.round_id == QuestionRoundRecord.id
                    )
                    .where(
                        QuestionRoundRecord.game_id == game.id,
                        AnswerAttemptRecord.participant_id == participant.id,
                        AnswerAttemptRecord.final_correct.is_(True),
                    )
                )
            )
            points = (
                competition_points(
                    Decimal(participant.final_place),
                    occupied[participant.final_place],
                    stage.place_points,
                )
                + Decimal(participant.score) * stage.score_multiplier
            ) if stage.kind == "first" else Decimal(0)
            results.append(
                {
                    "seat": match.seats[participant.seat - 1],
                    "score": str(participant.score),
                    "place": str(participant.final_place),
                    "points": str(points),
                    "correct_values": correct,
                    "key": [
                        str(v)
                        for v in ruleset.ranking_key(
                            score=participant.score, correct_values=correct, parameters=parameters
                        )
                    ],
                }
            )
        match.results = order_results(results, seed=f"{stage.random_seed}:{match.id}")

    @staticmethod
    async def round_access(
        session: AsyncSession, stage: ClassicStageRecord,
        round_record: ClassicRoundRecord, right: str,
    ) -> bool:
        override = getattr(round_record, right)
        if override is not None:
            return override
        if round_record.assignment_id:
            assignment = await session.get(
                TournamentPacketAssignmentRecord, round_record.assignment_id
            )
            if assignment is not None:
                return bool(getattr(assignment, f"{right}_by_members"))
        policy = await session.scalar(
            select(TournamentPolicyVersionRecord)
            .where(TournamentPolicyVersionRecord.tournament_id == stage.tournament_id)
            .order_by(TournamentPolicyVersionRecord.version.desc()).limit(1)
        )
        return bool(policy and policy.policies.get(
            f"packets_{right}_by_default", right == "discoverable"
        ))

    @classmethod
    async def assignment_access(
        cls, session: AsyncSession, assignment_id: UUID, player_id: UUID, right: str
    ) -> bool:
        rows = (
            await session.execute(
                select(ClassicRoundRecord, ClassicStageRecord, ClassicMatchRecord)
                .join(ClassicStageRecord, ClassicStageRecord.id == ClassicRoundRecord.stage_id)
                .join(ClassicMatchRecord, ClassicMatchRecord.round_id == ClassicRoundRecord.id)
                .where(
                    ClassicRoundRecord.assignment_id == assignment_id,
                    ClassicStageRecord.started_at.is_not(None),
                )
            )
        ).all()
        now = datetime.now(UTC)
        for round_record, stage, match in rows:
            if str(player_id) not in match.seats:
                continue
            if right == "discoverable" and await cls.round_access(
                session, stage, round_record, right
            ):
                return True
            if (
                right == "playable"
                and await cls.round_access(session, stage, round_record, "playable")
                and not stage.completed_at
                and match.results is None
                and match.game_id is None
                and (round_record.start_deadline is None or now < round_record.start_deadline)
            ):
                return True
        return False

    @classmethod
    async def prescribed_match(
        cls,
        session: AsyncSession,
        tournament_id: UUID,
        assignment_id: UUID,
        player_ids: list[UUID],
        *,
        exact: bool,
    ) -> ClassicMatchRecord:
        rows = (
            await session.execute(
                select(ClassicMatchRecord, ClassicRoundRecord, ClassicStageRecord)
                .join(ClassicRoundRecord, ClassicRoundRecord.id == ClassicMatchRecord.round_id)
                .join(ClassicStageRecord, ClassicStageRecord.id == ClassicRoundRecord.stage_id)
                .where(
                    ClassicStageRecord.tournament_id == tournament_id,
                    ClassicRoundRecord.assignment_id == assignment_id,
                    ClassicStageRecord.started_at.is_not(None),
                )
            )
        ).all()
        requested = {str(p) for p in player_ids}
        now = datetime.now(UTC)
        for match, round_record, stage in rows:
            humans = {s for s in match.seats if not is_chair(s)}
            if (
                not requested
                or not match.seats
                or match.results is not None
                or match.game_id
                or stage.completed_at
                or not await cls.round_access(session, stage, round_record, "playable")
                or (round_record.start_deadline and now >= round_record.start_deadline)
            ):
                continue
            matches_roster = requested == humans if exact else requested <= humans
            if matches_roster:
                return match
        raise ValueError("classic_participants_required")

    @staticmethod
    async def attach_chairs(
        session: AsyncSession, match: ClassicMatchRecord, game: GameRecord
    ) -> None:
        for seat_number, seat in enumerate(match.seats, 1):
            if not is_chair(seat):
                continue
            player = await session.get(PlayerRecord, UUID(seat[6:]), with_for_update=True)
            player.game_sequence += 1
            session.add(
                GameParticipantRecord(
                    tournament_id=game.tournament_id,
                    game_id=game.id,
                    player_id=player.id,
                    seat=seat_number,
                    rating_sequence=player.game_sequence,
                    global_game_sequence=player.game_sequence,
                    is_chair=True,
                    joined=True,
                    ready=True,
                    active=False,
                    score=0,
                )
            )
        match.game_id = game.id

    async def snapshot(self, session: AsyncSession, tournament_id: UUID) -> dict:
        players = await self.eligible(session, tournament_id)
        names = {str(p.id): p.public_nickname for p in players}
        result = {
            "schemes": [
                {k: s[k] for k in ("id", "kind", "size", "round_count")} for s in SCHEMES.values()
            ],
            "players": [{"id": str(p.id), "name": p.public_nickname} for p in players],
            "stages": [],
        }
        for stage in await self.stages(session, tournament_id):
            matches = await self.matches(session, stage.id)
            ranking = (
                standings(matches, quiz=stage.stage_type == "quiz", seed=stage.random_seed)
                if stage.stage_type in {"groups", "quiz"} else []
            )
            for row in ranking:
                row["name"] = names.get(row["seat"], row["seat"])
            result["stages"].append(
                {
                    "kind": stage.kind,
                    "stage_type": stage.stage_type,
                    "scheme_key": stage.scheme_key,
                    "started_at": stage.started_at,
                    "completed_at": stage.completed_at,
                    "seeds": stage.seeds,
                    "place_points": stage.place_points,
                    "score_multiplier": str(stage.score_multiplier),
                    "standings": ranking,
                    "rounds": [
                        {
                            "id": str(r.id),
                            "number": r.number,
                            "assignment_id": str(r.assignment_id) if r.assignment_id else None,
                            "discoverable": await self.round_access(
                                session, stage, r, "discoverable"
                            ),
                            "playable": await self.round_access(session, stage, r, "playable"),
                            "start_deadline": r.start_deadline,
                            "packet_locked": any(
                                m.game_id or (m.results and any(not is_chair(s) for s in m.seats))
                                for m in matches
                                if m.round_id == r.id
                            ),
                            "matches": [
                                {
                                    "id": str(m.id),
                                    "group": m.group_number,
                                    "number": m.number,
                                    "players": [
                                        "Chair" if is_chair(s) else names.get(s, s) for s in m.seats
                                    ],
                                    "results": m.results,
                                    "randomized": m.randomized,
                                    "game_id": str(m.game_id) if m.game_id else None,
                                }
                                for m in matches
                                if m.round_id == r.id
                            ],
                        }
                        for r in await self.rounds(session, stage.id)
                    ],
                }
            )
        return result
