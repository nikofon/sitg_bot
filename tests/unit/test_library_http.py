import json
from uuid import UUID

import pytest
from aiohttp.test_utils import make_mocked_request
from test_miniapp_http import FakeAuth, FakeGateway

from sitg_bot.application.contracts import ActionCode
from sitg_bot.miniapp_http import MiniAppHttpServer


@pytest.mark.parametrize("path", ["/library", f"/library/{UUID(int=30)}"])
async def test_library_routes_only_return_metadata(path):
    gateway = FakeGateway()
    http = MiniAppHttpServer(FakeAuth(), gateway)
    request = make_mocked_request("GET", f"/api/miniapp/routes/resolve?path={path}", headers={
        "Origin": "https://mini.example.test", "Cookie": "__Host-sitg_session=test-session",
    })
    response = await http._resolve_route(request)
    assert json.loads(response.text)["resource"] == {
        "kind": "library", "state": "ready", "items": []
    }
    assert gateway.requests[0].operation.action == ActionCode.LIBRARY_LIST


@pytest.mark.parametrize("command,action", [
    ("view", ActionCode.LIBRARY_VIEW), ("download", ActionCode.LIBRARY_DOWNLOAD),
])
async def test_library_post_uses_path_version_and_authenticated_mutation(command, action):
    class RecordingAuth(FakeAuth):
        async def authorize_request(self, session_token, **values):
            assert values["csrf_token"] == "csrf"
            assert values["idempotency_key"] == "library-request"
            assert values["method"] == "POST"
            return await super().authorize_request(session_token, **values)

    gateway = FakeGateway()
    http = MiniAppHttpServer(RecordingAuth(), gateway)
    path = f"/api/miniapp/library/{UUID(int=30)}/{command}"
    request = make_mocked_request("POST", path, headers={
        "Origin": "https://mini.example.test", "Cookie": "__Host-sitg_session=test-session",
        "Content-Type": "application/json", "X-CSRF-Token": "csrf",
        "X-Idempotency-Key": "library-request",
    }, match_info={"version_id": str(UUID(int=30)), "command": command})
    request._read_bytes = json.dumps({"version_id": str(UUID(int=99)), "confirm": True}).encode()
    response = await http._library_access(request)
    assert response.status == 200
    operation = gateway.requests[0].operation
    assert operation.action == action
    assert operation.version_id == UUID(int=30)
    assert operation.confirm is True
