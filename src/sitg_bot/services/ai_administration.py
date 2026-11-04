"""Administrator-only manual AI policy, quota resets and passive provider status."""

from datetime import UTC, datetime, timedelta
from uuid import UUID

from sqlalchemy import func, select, text
from sqlalchemy.dialects.postgresql import insert as pg_insert

from sitg_bot.application.contracts import (
    ActionCode,
    AdminAIPlayersOperation,
    AdminAITierOperation,
    AdminAIUpdateOperation,
)
from sitg_bot.services.concurrency import StaleWriteError
from sitg_bot.services.entitlements import EntitlementService, period_bounds
from sitg_bot.services.moderation import _require_administrator
from sitg_bot.storage.models import (
    PlayerRecord,
    TrainingAITierRecord,
    TrainingAnalysisArtifactRecord,
    TrainingCategoryRecord,
    TrainingPolicyStateRecord,
)

PERIODS = {"hour": "hourly", "day": "daily", "week": "weekly"}
THEME_FEATURES = ("dissection", "best_buzz")


class AIAdministrationService:
    def __init__(self, database, *, settings=None):
        self.database = database
        self.entitlements = EntitlementService(database)
        self.settings = settings

    async def _state(self, session, actor, *, lock=False):
        await _require_administrator(session, actor)
        state = await session.get(TrainingPolicyStateRecord, 1, with_for_update=lock)
        if state is None:
            raise ValueError("AI policies have not been initialized")
        return state

    async def _policy_pair(self, session, state):
        category = await session.get(TrainingCategoryRecord, state.default_category_id)
        default_version = category.policy_version if category else None
        free = await self.entitlements._policies_version(
            session, state.free_policy_version or default_version
        )
        premium = await self.entitlements._policies_version(
            session, state.premium_policy_version or state.free_policy_version or default_version
        )
        return free, premium

    async def _projection(self, session, state):
        free, premium = await self._policy_pair(session, state)
        period = (state.admin_features or {}).get("reset_period", "day")
        # Existing custom monthly/none policies are untouched. Before the first
        # explicit save, the admin form proposes the supported daily window.
        reset_at = period_bounds(PERIODS[period], datetime.now(UTC))[1]
        return {
            "version": state.admin_version,
            "dissection_enabled": free["dissection"].enabled,
            "best_buzz_enabled": free["best_buzz"].enabled,
            "free": {key: free[key].limit for key in THEME_FEATURES},
            "premium": {key: premium[key].limit for key in THEME_FEATURES},
            "reset_period": period,
            "reset_at": reset_at,
            "quota_generation": state.quota_generation,
        }

    async def read(self, actor):
        async with self.database.transaction() as session:
            state = await self._state(session, actor)
            return await self._projection(session, state)

    async def update(self, actor, **values):
        operation = AdminAIUpdateOperation.model_validate(
            {
                **values,
                "action": ActionCode.ADMIN_AI_UPDATE,
            }
        )
        async with self.database.transaction() as session:
            state = await self._state(session, actor, lock=True)
            if state.admin_version != operation.expected_version:
                raise StaleWriteError("AI settings have changed")
            free, premium = await self._policy_pair(session, state)
            for tier, policies in (("free", free), ("premium", premium)):
                payload = {key: policy.to_mapping() for key, policy in policies.items()}
                quotas = getattr(operation, tier)
                for key in THEME_FEATURES:
                    payload[key].update(
                        enabled=getattr(operation, key + "_enabled"),
                        limit=getattr(quotas, key),
                        reset_period=PERIODS[operation.reset_period],
                    )
                version = await self.entitlements.create_policy(session, payload)
                # Persist each appended version before allocating the next one.
                await session.flush()
                setattr(state, tier + "_policy_version", version)
            state.admin_features = {"reset_period": operation.reset_period}
            state.admin_version += 1
            state.updated_at = datetime.now(UTC)
            await session.flush()
            return await self._projection(session, state)

    async def reset(self, actor, *, expected_version, confirm):
        if confirm is not True or type(expected_version) is not int:
            raise ValueError("Explicit reset confirmation and version required")
        async with self.database.transaction() as session:
            state = await self._state(session, actor, lock=True)
            if state.admin_version != expected_version:
                raise StaleWriteError("AI settings have changed")
            state.quota_generation += 1
            state.admin_version += 1
            state.updated_at = datetime.now(UTC)
            return await self._projection(session, state)

    async def players(self, actor, *, search, limit=20):
        operation = AdminAIPlayersOperation.model_validate(
            {
                "action": ActionCode.ADMIN_AI_PLAYERS,
                "search": search,
                "limit": limit,
            }
        )
        async with self.database.transaction() as session:
            await _require_administrator(session, actor)
            query = select(
                PlayerRecord.id, PlayerRecord.public_nickname, TrainingAITierRecord.tier
            ).outerjoin(TrainingAITierRecord, TrainingAITierRecord.player_id == PlayerRecord.id)
            try:
                player_id = UUID(operation.search)
            except ValueError:
                # Literal, indexed prefix search; no wildcard interpretation.
                escaped = (
                    operation.search.lower()
                    .replace("\\", "\\\\")
                    .replace("%", "\\%")
                    .replace("_", "\\_")
                )
                query = query.where(
                    func.lower(PlayerRecord.public_nickname).like(escaped + "%", escape="\\")
                )
            else:
                query = query.where(PlayerRecord.id == player_id)
            rows = await session.execute(
                query.order_by(func.lower(PlayerRecord.public_nickname), PlayerRecord.id).limit(
                    operation.limit
                )
            )
            return {
                "items": [
                    {"id": row.id, "name": row.public_nickname or "", "tier": row.tier or "free"}
                    for row in rows
                ],
                "limit": operation.limit,
            }

    async def tier(self, actor, *, player_id, tier):
        operation = AdminAITierOperation.model_validate(
            {
                "action": ActionCode.ADMIN_AI_TIER,
                "player_id": player_id,
                "tier": tier,
            }
        )
        async with self.database.transaction() as session:
            await _require_administrator(session, actor)
            if await session.get(
                PlayerRecord, operation.player_id, with_for_update={"read": True, "key_share": True}
            ) is None:
                raise LookupError("Player not found")
            await self._state(session, actor, lock=True)
            await session.execute(
                pg_insert(TrainingAITierRecord)
                .values(
                    player_id=operation.player_id,
                    tier=operation.tier,
                )
                .on_conflict_do_update(
                    index_elements=["player_id"],
                    set_={"tier": operation.tier},
                )
            )
            return {"id": operation.player_id, "tier": operation.tier}

    def _health_projection(self, now, observed_at=None):
        configured = bool(self.settings.api_key and self.settings.model != "unconfigured")
        if not configured:
            state = "unconfigured"
        elif observed_at is None:
            state = "unknown"
        elif now >= observed_at + timedelta(minutes=5):
            state = "stale"
        else:
            state = "responding"
        return {
            "provider": self.settings.provider,
            "model": self.settings.model,
            "configured": configured,
            "state": state,
            "verified_at": observed_at,
        }

    async def health(self, actor):
        async with self.database.transaction() as session:
            await _require_administrator(session, actor)
            # Partial expression index bounds this lookup; empty/cache-only outcomes
            # cannot establish that the selected model executed successfully.
            observed = await session.scalar(
                select(TrainingAnalysisArtifactRecord.completed_at)
                .where(
                    text(
                        "status = 'succeeded' AND "
                        "(provider_usage -> 'calls') @> '[{}]'::jsonb AND "
                        "provider_usage ->> 'execution_profile' = :profile"
                    )
                )
                .params(profile=self.settings.execution_profile)
                .order_by(TrainingAnalysisArtifactRecord.completed_at.desc())
                .limit(1)
            )
            return self._health_projection(datetime.now(UTC), observed)
