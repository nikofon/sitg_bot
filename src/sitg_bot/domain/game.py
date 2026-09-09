from dataclasses import dataclass, field
from enum import StrEnum
from random import Random
from uuid import UUID, uuid4


class GameStatus(StrEnum):
    LOBBY = "lobby"
    ACTIVE = "active"
    FINISHED = "finished"


@dataclass(slots=True)
class Player:
    id: int
    display_name: str
    score: int = 0
    correct_points: int = 0
    correct_by_value: dict[int, int] = field(default_factory=dict)

    def apply_answer(self, value: int, *, correct: bool) -> None:
        self.score += value if correct else -value
        if correct:
            self.correct_points += value
            self.correct_by_value[value] = self.correct_by_value.get(value, 0) + 1


@dataclass(slots=True)
class Game:
    host_id: int
    id: UUID = field(default_factory=uuid4)
    status: GameStatus = GameStatus.LOBBY
    players: dict[int, Player] = field(default_factory=dict)

    def add_player(self, player: Player) -> None:
        if self.status is not GameStatus.LOBBY:
            raise ValueError("Players can only join a game in the lobby")
        if player.id in self.players:
            raise ValueError("Player has already joined this game")
        self.players[player.id] = player

    def start(self, requested_by: int) -> None:
        if requested_by != self.host_id:
            raise PermissionError("Only the host can start the game")
        if self.status is not GameStatus.LOBBY:
            raise ValueError("Game has already started")
        if len(self.players) < 2:
            raise ValueError("At least two players are required")
        self.status = GameStatus.ACTIVE

    def finish(self) -> None:
        if self.status is not GameStatus.ACTIVE:
            raise ValueError("Only an active game can finish")
        self.status = GameStatus.FINISHED

    def ranking(self, random: Random | None = None) -> list[Player]:
        """Rank players, randomly ordering only players tied on every defined metric."""
        random = random or Random()
        tie_broken = list(self.players.values())
        random.shuffle(tie_broken)
        return sorted(
            tie_broken,
            key=lambda player: (
                player.score,
                player.correct_points,
                *(player.correct_by_value.get(value, 0) for value in (50, 40, 30, 20)),
            ),
            reverse=True,
        )
