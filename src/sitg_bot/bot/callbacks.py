import secrets
from collections.abc import Callable
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from uuid import UUID


@dataclass(frozen=True, slots=True)
class CallbackTarget:
    scope: str
    actor_id: UUID
    resource_id: UUID
    action: str
    expires_at: datetime


class CallbackReferenceStore:
    """Short-lived, actor-bound opaque references for sensitive callback actions."""

    def __init__(
        self,
        *,
        clock: Callable[[], datetime] | None = None,
        lifetime: timedelta = timedelta(minutes=5),
    ) -> None:
        if lifetime <= timedelta(0):
            raise ValueError("Callback reference lifetime must be positive")
        self.clock = clock or (lambda: datetime.now(UTC))
        self.lifetime = lifetime
        self._targets: dict[str, CallbackTarget] = {}

    def issue(
        self,
        *,
        scope: str,
        actor_id: UUID,
        resource_id: UUID,
        action: str,
    ) -> str:
        self._prune()
        reference = secrets.token_urlsafe(9)
        while reference in self._targets:
            reference = secrets.token_urlsafe(9)
        self._targets[reference] = CallbackTarget(
            scope=scope,
            actor_id=actor_id,
            resource_id=resource_id,
            action=action,
            expires_at=self.clock() + self.lifetime,
        )
        return reference

    def resolve(
        self,
        reference: str,
        *,
        scope: str,
        actor_id: UUID,
        consume: bool = False,
    ) -> CallbackTarget:
        self._prune()
        target = self._targets.get(reference)
        if target is None or target.scope != scope or target.actor_id != actor_id:
            raise LookupError("Callback reference is unavailable")
        if consume:
            del self._targets[reference]
        return target

    def _prune(self) -> None:
        now = self.clock()
        self._targets = {
            reference: target
            for reference, target in self._targets.items()
            if target.expires_at > now
        }
