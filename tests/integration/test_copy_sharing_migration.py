"""Upgrade legacy live shares without losing recipient facts or custom policies."""

import asyncio
from datetime import UTC, datetime
from uuid import uuid4

from alembic import command
from alembic.config import Config
from conftest import create_player
from sqlalchemy import select

from sitg_bot.domain.knowledge import CommonKnowledgeEstimate
from sitg_bot.services.training_knowledge import KnowledgeApplication
from sitg_bot.storage.database import Database
from sitg_bot.storage.models import (
    KnowledgeFactCopyRecord,
    KnowledgeGrantRecord,
    TrainingCategoryRecord,
    TrainingFeaturePolicyRecord,
    TrainingPolicyStateRecord,
)


def test_upgrade_converts_legacy_shares_and_only_updates_the_old_free_policy(
    training_database, monkeypatch
):
    monkeypatch.delenv("SITG_TRAINING_AI_API_KEY", raising=False)
    config = Config("alembic.ini")
    command.downgrade(config, "0022_training_knowledge_ai")

    async def seed():
        database = Database(training_database)
        try:
            owner = await create_player(database, 8701, "legacy-owner")
            recipient = await create_player(database, 8702, "legacy-recipient")
            app = KnowledgeApplication(database)
            created = await app.mutate(
                owner,
                "create",
                {
                    "title": "Original",
                    "description": "Original description",
                    "tag_names": ["history"],
                    "links": [{"url": "https://example.org/source", "title": "Reference"}],
                },
            )
            async with database.transaction() as session:
                await app.service(session).record_statistics(
                    owner,
                    created["id"],
                    common=CommonKnowledgeEstimate(
                        status="available",
                        value=25,
                        model="fixture",
                        estimated_at=datetime.now(UTC),
                    ),
                )
                session.add(
                    KnowledgeGrantRecord(
                        id=uuid4(),
                        owner_id=owner,
                        recipient_id=recipient,
                        granted_at=datetime.now(UTC),
                    )
                )
                features = {
                    feature: {
                        "enabled": True,
                        "limit": 50 if feature == "common_knowledge" else 10,
                        "reset_period": "daily",
                        "cache_hit_charging": False,
                        "queued_request_cap": 3,
                        "in_flight_request_cap": 2,
                    }
                    for feature in ("dissection", "best_buzz", "common_knowledge")
                }
                version = await app.entitlements.create_policy(session, features)
                await session.flush()
                category = await app.entitlements.create_category(
                    session,
                    code="default",
                    display_name="Default",
                    policy_version=version,
                    is_default=True,
                )
                session.add(TrainingPolicyStateRecord(singleton=1, default_category_id=category))
                custom_version = await app.entitlements.create_policy(
                    session, {key: {**value, "limit": 7} for key, value in features.items()}
                )
                await session.flush()
                await app.entitlements.create_category(
                    session, code="custom", display_name="Custom", policy_version=custom_version
                )
            return owner, recipient, created["id"], custom_version
        finally:
            await database.close()

    owner, recipient, original_id, custom_version = asyncio.run(seed())
    command.upgrade(config, "head")

    async def verify():
        database = Database(training_database)
        try:
            app = KnowledgeApplication(database)
            copied_id = (await app.facts(recipient))["items"][0]["id"]
            assert copied_id != original_id
            detail = await app.detail(recipient, copied_id)
            assert detail["editable"] and detail["statistics"]["common_knowledge"] == 25
            assert detail["tags"][0]["name"] == "history"
            assert detail["links"][0]["title"] == "Reference"
            async with database.transaction() as session:
                mapping = await session.scalar(select(KnowledgeFactCopyRecord))
                assert mapping.source_fact_id == original_id
                grant = await session.scalar(select(KnowledgeGrantRecord))
                assert grant.revoked_at is not None
                default = await session.scalar(
                    select(TrainingCategoryRecord).where(TrainingCategoryRecord.code == "default")
                )
                policy = await session.scalar(
                    select(TrainingFeaturePolicyRecord).where(
                        TrainingFeaturePolicyRecord.version == default.policy_version
                    )
                )
                assert (
                    policy.features["dissection"]["limit"]
                    == policy.features["best_buzz"]["limit"]
                    == 24
                )
                assert policy.features["dissection"]["enabled"] is True
                custom = await session.scalar(
                    select(TrainingCategoryRecord).where(TrainingCategoryRecord.code == "custom")
                )
                assert custom.policy_version == custom_version
            assert (
                await app.mutate(owner, "grant", {"recipient": "legacy-recipient"})
            ).copied_count == 0
            await app.mutate(
                owner, "delete", {"fact_id": original_id, "confirm_dissection_origin": False}
            )
            assert (await app.detail(recipient, copied_id))["title"] == "Original"
            return copied_id
        finally:
            await database.close()

    copied_id = asyncio.run(verify())
    command.downgrade(config, "0022_training_knowledge_ai")

    async def retained():
        from sitg_bot.storage.models import KnowledgeFactRecord

        database = Database(training_database)
        try:
            async with database.transaction() as session:
                assert (await session.get(KnowledgeFactRecord, copied_id)).owner_id == recipient
        finally:
            await database.close()

    asyncio.run(retained())
