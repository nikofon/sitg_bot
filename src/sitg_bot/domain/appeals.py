from __future__ import annotations

import math
from collections.abc import Mapping
from dataclasses import dataclass
from datetime import timedelta


@dataclass(frozen=True, slots=True)
class AppealPolicy:
    voting_rule: str = "majority"
    vote_timeout_seconds: float = 60
    escalation_enabled: bool = False
    escalation_decision_timeout_seconds: float = 30
    commentary_timeout_seconds: float = 300
    ticket_expiry_seconds: float = 86_400

    def __post_init__(self) -> None:
        if self.voting_rule not in {"majority", "unanimous"}:
            raise ValueError("appeal_voting_rule must be majority or unanimous")
        for name in (
            "vote_timeout_seconds",
            "escalation_decision_timeout_seconds",
            "commentary_timeout_seconds",
            "ticket_expiry_seconds",
        ):
            value = getattr(self, name)
            if (
                isinstance(value, bool)
                or not isinstance(value, (int, float))
                or not math.isfinite(value)
                or value <= 0
            ):
                raise ValueError(f"appeal_{name} must be a finite positive number")
        if not isinstance(self.escalation_enabled, bool):
            raise ValueError("appeal_escalation_enabled must be true or false")

    @classmethod
    def from_mapping(cls, policies: Mapping[str, object] | None) -> AppealPolicy:
        values = policies or {}
        return cls(
            voting_rule=str(values.get("appeal_voting_rule", "majority")),
            vote_timeout_seconds=values.get("appeal_vote_timeout_seconds", 60),  # type: ignore[arg-type]
            escalation_enabled=values.get("appeal_escalation_enabled", False),  # type: ignore[arg-type]
            escalation_decision_timeout_seconds=values.get(
                "appeal_escalation_decision_timeout_seconds", 30
            ),  # type: ignore[arg-type]
            commentary_timeout_seconds=values.get("appeal_commentary_timeout_seconds", 300),  # type: ignore[arg-type]
            ticket_expiry_seconds=values.get("appeal_ticket_expiry_seconds", 86_400),  # type: ignore[arg-type]
        )

    @property
    def vote_timeout(self) -> timedelta:
        return timedelta(seconds=self.vote_timeout_seconds)

    @property
    def escalation_decision_timeout(self) -> timedelta:
        return timedelta(seconds=self.escalation_decision_timeout_seconds)

    @property
    def commentary_timeout(self) -> timedelta:
        return timedelta(seconds=self.commentary_timeout_seconds)

    @property
    def ticket_expiry(self) -> timedelta:
        return timedelta(seconds=self.ticket_expiry_seconds)

    def vote_approved(self, approvals: int, electorate_size: int) -> bool:
        if electorate_size < 1 or not 0 <= approvals <= electorate_size:
            raise ValueError("Invalid appeal vote tally")
        if self.voting_rule == "unanimous":
            return approvals == electorate_size
        return approvals > electorate_size / 2

    def vote_decided(self, approvals: int, rejections: int, electorate_size: int) -> bool | None:
        if min(approvals, rejections) < 0 or approvals + rejections > electorate_size:
            raise ValueError("Invalid appeal vote tally")
        if self.voting_rule == "unanimous":
            if rejections:
                return False
            return True if approvals == electorate_size else None
        if approvals > electorate_size / 2:
            return True
        if rejections >= (electorate_size + 1) // 2:
            return False
        return None
