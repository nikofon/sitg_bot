import base64
import json
from uuid import UUID

import pytest
from conftest import create_player
from sqlalchemy import func, select
from test_training import open_question

from sitg_bot.application.contracts import ActionCode, ApplicationPrincipal, GatewayRequest
from sitg_bot.application.gateway import ApplicationGateway
from sitg_bot.storage.models import (
    KnowledgeFactRecord,
    PacketDraftRecord,
    TrainingAttemptRecord,
    TrainingSessionRecord,
)

pytestmark = pytest.mark.integration


async def test_gateway_private_mutations_replay_without_duplicate_or_stale_writes(database):
    owner = await create_player(database, 9301, "retry-owner")
    gateway = ApplicationGateway(database)
    principal = ApplicationPrincipal(player_id=owner, telegram_user_id=9301)
    requests = {}

    async def twice(action, values, key):
        request = GatewayRequest.model_validate({
            "metadata": {"channel": "mini_app", "client_name": "retry-test",
                         "client_version": "1", "idempotency_key": f"gateway-retry:{key}"},
            "operation": {"action": action, **values},
        })
        requests[key] = request
        first = await gateway.execute(principal, request)
        assert first.ok, first.error
        retry = await gateway.execute(principal, request)
        assert retry.ok, retry.error
        assert retry.data == first.data
        return first.data

    tag = await twice(ActionCode.KNOWLEDGE_TAG_CREATE,
                      {"name": "History", "colour": "#123456"}, "tag-create")
    await twice(ActionCode.KNOWLEDGE_TAG_RENAME,
                {"tag_id": tag["tag_id"], "name": "Past"}, "tag-rename")
    fact = await twice(ActionCode.KNOWLEDGE_CREATE,
                       {"title": "Fact", "description": "Private fact", "tag_names": ["Past"]},
                       "fact-create")
    await twice(ActionCode.KNOWLEDGE_UPDATE,
                          {"fact_id": fact["id"], "expected_version": fact["content_version"],
                           "description": "Edited private fact"}, "fact-update")
    await twice(ActionCode.KNOWLEDGE_DELETE, {"fact_id": fact["id"]}, "fact-delete")
    await twice(ActionCode.KNOWLEDGE_TAG_DELETE, {"tag_id": tag["tag_id"]}, "tag-delete")

    source_bytes = json.dumps({
        "name": "Private training",
        "themes": [{"name": "Theme", "questions": [
            {"value": value, "text": "Name this capital city", "answer": "Paris"}
            for value in (10, 20, 30, 40, 50)
        ]}],
    }).encode()
    upload_values = {"source_filename": "private.json",
                     "source_base64": base64.b64encode(source_bytes).decode()}
    uploaded = await twice(ActionCode.TRAINING_UPLOAD, upload_values, "upload")
    draft = uploaded["drafts"][0]
    assert draft["errors"] == []
    published = await twice(ActionCode.TRAINING_DRAFT_DECIDE,
                            {"draft_id": draft["draft_id"], "expected_version": draft["version"],
                             "publish": True}, "publish")
    started = await twice(ActionCode.TRAINING_START,
                          {"version_id": published["version_id"], "confirm": True}, "start")
    state, _ = await open_question(gateway.training, owner, started["session"], None)
    buzzed = await twice(ActionCode.TRAINING_ACT,
                         {"session_id": state["id"], "expected_version": state["version"],
                          "question_revision_id": state["question_revision_id"], "command": "buzz"},
                         "buzz")
    state = buzzed["session"]
    answered = await twice(ActionCode.TRAINING_ACT,
                           {"session_id": state["id"], "expected_version": state["version"],
                            "question_revision_id": state["question_revision_id"],
                            "command": "answer", "answer": "Paris"}, "answer")
    state = answered["session"]
    await twice(ActionCode.TRAINING_ACT,
                {"session_id": state["id"], "expected_version": state["version"],
                 "question_revision_id": state["question_revision_id"], "command": "stop"}, "stop")
    rejected_upload = await twice(ActionCode.TRAINING_UPLOAD, upload_values, "other-upload")
    rejected = rejected_upload["drafts"][0]
    await twice(ActionCode.TRAINING_DRAFT_DECIDE,
                {"draft_id": rejected["draft_id"], "expected_version": rejected["version"],
                 "publish": False}, "reject")

    async with database.sessions() as session:
        assert await session.scalar(select(func.count()).select_from(KnowledgeFactRecord)) == 1
        assert await session.scalar(select(func.count()).select_from(PacketDraftRecord)) == 2
        assert await session.scalar(select(func.count()).select_from(TrainingSessionRecord)) == 1
        assert await session.scalar(select(func.count()).select_from(TrainingAttemptRecord)) == 1

    await gateway.training.packets.retire(owner, UUID(published["version_id"]))
    for key in ("start", "buzz", "answer", "upload", "publish"):
        denied = await gateway.execute(principal, requests[key])
        assert not denied.ok and denied.data is None
        assert denied.error.code.value == "not_found"
    assert (await gateway.execute(principal, requests["stop"])).ok
    assert (await gateway.execute(principal, requests["reject"])).ok
