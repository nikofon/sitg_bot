import base64
import struct
from contextlib import asynccontextmanager
from types import SimpleNamespace
from unittest.mock import AsyncMock
from uuid import UUID

import pytest
from aiohttp.test_utils import TestClient, TestServer
from pydantic import ValidationError
from test_knowledge_catalogue_http import HEADERS, BoundaryAuth
from test_miniapp_http import FakeGateway

from sitg_bot.application.contracts import ActionCode, GatewayRequest
from sitg_bot.domain.knowledge import FactContent
from sitg_bot.miniapp_http import MiniAppHttpServer
from sitg_bot.services.concurrency import StaleWriteError
from sitg_bot.services.knowledge_images import MAX_IMAGE_BYTES, raster_content_type
from sitg_bot.services.training import TrainingService

PNG = base64.b64decode(
    "iVBORw0KGgoAAAANSUhEUgAAAAEAAAABCAQAAAC1HAwCAAAAC0lEQVR42mP8/x8AAwMCAO+aG2kAAAAASUVORK5CYII="
)


def test_uploaded_rasters_have_bounded_size_dimensions_and_explicit_mime():
    assert raster_content_type(PNG) == "image/png"
    jpeg = b"\xff\xd8\xff\xc0\x00\x08\x08\x00\x01\x00\x02\x01\xff\xd9"
    assert raster_content_type(jpeg) == "image/jpeg"
    webp = b"RIFF" + struct.pack("<I", 22) + b"WEBPVP8X\x0a\x00\x00\x00" + bytes(10)
    assert raster_content_type(webp) == "image/webp"
    for data in (b"", b"<svg xmlns='http://www.w3.org/2000/svg'/>", b"<html/>",
                 PNG[:30], b"x" * (MAX_IMAGE_BYTES + 1),
                 PNG[:16] + struct.pack(">II", 10000, 10000) + PNG[24:]):
        with pytest.raises(ValueError):
            raster_content_type(data)


def test_legacy_fingerprints_stay_stable_and_images_are_validated_separately():
    import hashlib
    import json

    old = {"title": "Title", "description": "Description", "links": []}
    expected = hashlib.sha256(
        json.dumps(old, ensure_ascii=False, sort_keys=True).encode()
    ).hexdigest()
    assert FactContent("Title", "Description").fingerprint() == expected
    image = UUID(int=1)
    assert FactContent("Title", "Description", image_ids=(image,)).fingerprint() != expected
    for images in ((image, image), tuple(UUID(int=i) for i in range(9)), ("https://image.test/a",)):
        with pytest.raises(ValueError):
            FactContent("Title", "Description", image_ids=images)


async def test_additive_tags_and_upload_only_filter_cross_real_http_contracts():
    gateway = FakeGateway()
    http = MiniAppHttpServer(BoundaryAuth(), gateway)
    async with TestClient(TestServer(http.application)) as client:
        result = await client.get(
            "/api/miniapp/knowledge/facts?tags=History&tags=Science&tag=Other", headers=HEADERS
        )
        assert result.status == 200
        operation = gateway.requests[-1].operation
        assert operation.tags == ["History", "Science"] and operation.tag == "Other"
        result = await client.get(
            "/api/miniapp/training/packets?owner_uploads_only=true", headers=HEADERS
        )
        assert result.status == 200 and gateway.requests[-1].operation.owner_uploads_only is True
        result = await client.post("/api/miniapp/knowledge/merge", json={}, headers=HEADERS)
        assert result.status == 404
        assert len(gateway.requests) == 2
    with pytest.raises(ValidationError):
        GatewayRequest.model_validate({
            "metadata": {"channel": "mini_app", "client_name": "test", "client_version": "1"},
            "operation": {"action": "knowledge.facts.merge.v1"},
        })


async def test_image_routes_require_auth_csrf_and_return_nosniff_raster_bytes():
    class ImagesGateway(FakeGateway):
        async def execute(self, principal, request):
            result = await super().execute(principal, request)
            return result.model_copy(update={"data": {
                "id": str(UUID(int=4)), "content_type": "image/png",
                "source_base64": base64.b64encode(PNG).decode(),
            }})

    gateway = ImagesGateway()
    http = MiniAppHttpServer(BoundaryAuth(), gateway)
    async with TestClient(TestServer(http.application)) as client:
        path = f"/api/miniapp/knowledge/images/{UUID(int=4)}"
        response = await client.get(path, headers=HEADERS)
        assert response.status == 200 and await response.read() == PNG
        assert response.content_type == "image/png"
        assert response.headers["Cache-Control"] == "no-store"
        assert response.headers["X-Content-Type-Options"] == "nosniff"
        assert (await client.get(path, headers={"Origin": HEADERS["Origin"]})).status == 401
        payload = {"source_base64": base64.b64encode(PNG).decode()}
        response = await client.post(
            "/api/miniapp/knowledge/image_upload", json=payload, headers=HEADERS
        )
        assert response.status == 200
        assert gateway.requests[-1].operation.action == ActionCode.KNOWLEDGE_IMAGE_UPLOAD
        response = await client.post(
            "/api/miniapp/knowledge/image_upload", json=payload,
            headers={key: value for key, value in HEADERS.items() if key != "X-CSRF-Token"},
        )
        assert response.status == 403


async def test_packet_autosave_and_settings_save_are_independent_and_versioned(monkeypatch):
    from sitg_bot.services import training

    owner, packet = UUID(int=1), UUID(int=2)
    player = SimpleNamespace(training_lobby={
        "version": 1, "version_id": None, "packet_name": None,
        "settings": {"reversed": False},
    }, training_lobby_version=1)
    session = SimpleNamespace(get=AsyncMock(return_value=player))

    @asynccontextmanager
    async def transaction():
        yield session

    monkeypatch.setattr(training, "require_trainee", AsyncMock())
    service = TrainingService(SimpleNamespace(transaction=transaction))
    service.packets.authorize = AsyncMock(return_value=(SimpleNamespace(name="Packet"), None))
    saved = (await service.lobby(
        owner, "save_settings", expected_version=1, settings={"reversed": True}
    ))["lobby"]
    assert saved["version_id"] is None and saved["settings"]["reversed"] is True
    service.packets.authorize.assert_not_awaited()
    selected = (await service.lobby(
        owner, "select_packet", expected_version=2, version_id=packet,
        settings={"reversed": False},  # Even a stale UI draft cannot overwrite saved settings.
    ))["lobby"]
    assert selected["version"] == 3 and selected["settings"]["reversed"] is True
    assert selected["version_id"] == str(packet)
    with pytest.raises(StaleWriteError):
        await service.lobby(owner, "save_settings", expected_version=2, settings={})
    with pytest.raises(StaleWriteError):
        await service.lobby(owner, "select_packet", expected_version=1, version_id=packet)
    latest = (await service.lobby(owner, "save_settings", expected_version=3, settings={}))["lobby"]
    assert latest["version_id"] == str(packet) and latest["settings"]["reversed"] is False
