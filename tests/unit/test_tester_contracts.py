import io
import json
from dataclasses import asdict
from types import SimpleNamespace
from unittest.mock import AsyncMock
from uuid import uuid4
from zipfile import ZipFile

import pytest
from aiohttp.test_utils import TestClient, TestServer
from test_miniapp_http import FakeAuth, FakeGateway
from test_packet import docx_bytes

from sitg_bot.application.contracts import ActionCode, GatewayRequest
from sitg_bot.application.gateway import ACTION_POLICIES
from sitg_bot.domain.packet import Packet, Question, Theme
from sitg_bot.miniapp_http import MiniAppHttpServer
from sitg_bot.packet_export import library_docx
from sitg_bot.packet_import import packet_from_data, packet_from_docx_bytes, packet_to_json


def test_tester_json_and_document_metadata():
    packet = Packet(
        "Packet",
        (Theme("Theme", (Question("Q", "A", "", 10),)),),
        testers=("First Tester", "Second Tester"),
    )
    assert packet_from_data(json.loads(packet_to_json(packet))) == packet
    legacy = asdict(packet)
    legacy.pop("testers")
    assert packet_from_data(legacy).testers == ()
    source = docx_bytes(
        [
            ("Heading1", "Packet"),
            ("Normal", "Тестировщики: First Tester; Second Tester"),
            ("Normal", "Testers: Third Tester"),
            ("Heading2", "Theme"),
            ("Normal", "10. Q"),
            ("Normal", "Ответ: A"),
        ]
    )
    assert packet_from_docx_bytes(source).testers == (
        "First Tester",
        "Second Tester",
        "Third Tester",
    )
    content = library_docx("Packet", [{"title": "Theme", "questions": []}], packet.testers)
    with ZipFile(io.BytesIO(content)) as document:
        xml = document.read("word/document.xml").decode()
        assert "Testers: First Tester; Second Tester" in xml
        assert xml.index("Testers:") < xml.index(">Theme<")


@pytest.mark.parametrize("testers", ["Person", [""], [123], ["a" * 301]])
def test_invalid_testers_rejected(testers):
    with pytest.raises(ValueError):
        packet_from_data(
            {
                "name": "P",
                "testers": testers,
                "themes": [
                    {"name": "T", "questions": [{"value": 10, "text": "Q", "answer": "A"}]},
                ],
            }
        )


@pytest.mark.parametrize(
    "action,fields",
    [
        (ActionCode.TESTERS_SEARCH, {"query": "Person"}),
        (ActionCode.TESTERS_MINE, {}),
        (ActionCode.TESTER_LINK_CREATE, {"tester_id": str(uuid4())}),
        (ActionCode.TESTER_LINK_CANCEL, {"request_id": str(uuid4())}),
        (ActionCode.TESTER_LINK_MINE, {}),
        (ActionCode.TESTER_LINK_ADMIN_PENDING, {}),
        (ActionCode.TESTER_LINK_ADMIN_DECIDE, {"request_id": str(uuid4()), "approve": True}),
        (ActionCode.PACKET_DRAFT_TESTER_SEARCH, {"draft_id": str(uuid4())}),
        (
            ActionCode.PACKET_DRAFT_TESTER_CREATE,
            {"draft_id": str(uuid4()), "first_name": "First", "surname": "Tester"},
        ),
    ],
)
def test_tester_operations_are_authenticated(action, fields):
    request = GatewayRequest.model_validate(
        {
            "metadata": {"channel": "mini_app", "client_name": "test", "client_version": "1"},
            "operation": {"action": action, **fields},
        }
    )
    assert request.operation.action == action
    assert ACTION_POLICIES[action].authentication_required
    if ACTION_POLICIES[action].mutation:
        assert ACTION_POLICIES[action].idempotency_required


@pytest.mark.parametrize(
    "method,path,body,action",
    [
        ("GET", "/testers?query=Person", None, ActionCode.TESTERS_SEARCH),
        ("GET", "/testers/mine", None, ActionCode.TESTERS_MINE),
        ("GET", "/tester-links", None, ActionCode.TESTER_LINK_MINE),
        ("GET", "/admin/tester-links", None, ActionCode.TESTER_LINK_ADMIN_PENDING),
        ("POST", "/tester-links", {"tester_id": str(uuid4())}, ActionCode.TESTER_LINK_CREATE),
        ("POST", f"/tester-links/{uuid4()}/cancel", {}, ActionCode.TESTER_LINK_CANCEL),
        (
            "POST",
            f"/admin/tester-links/{uuid4()}/decide",
            {"approve": True},
            ActionCode.TESTER_LINK_ADMIN_DECIDE,
        ),
        ("GET", f"/manager/packets/{uuid4()}/testers", None, ActionCode.PACKET_DRAFT_TESTER_SEARCH),
        (
            "POST",
            f"/manager/packets/{uuid4()}/testers",
            {"first_name": "First", "surname": "Tester"},
            ActionCode.PACKET_DRAFT_TESTER_CREATE,
        ),
    ],
)
async def test_tester_http_routes_use_gateway_security(method, path, body, action):
    gateway = FakeGateway()
    references = SimpleNamespace(
        resolve=AsyncMock(
            return_value=SimpleNamespace(
                route="packet_draft",
                target_id=uuid4(),
            )
        )
    )
    http = MiniAppHttpServer(FakeAuth(), gateway, launch_references=references)
    async with TestClient(TestServer(http.application)) as client:
        response = await client.request(
            method,
            f"/api/miniapp{path}",
            json=body,
            headers={
                "Origin": "https://mini.example.test",
                "Cookie": "__Host-sitg_session=test-session",
                "Content-Type": "application/json",
                "X-CSRF-Token": "csrf",
                "X-Idempotency-Key": "tester-http-test",
            },
        )
        assert response.status == 200, await response.text()
    request = gateway.requests[-1]
    assert request.operation.action == action
    if method == "POST":
        assert request.metadata.idempotency_key == "tester-http-test"
