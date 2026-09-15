from types import SimpleNamespace
from unittest.mock import AsyncMock
from uuid import UUID

import pytest
from aiogram.types import Message

from sitg_bot.application.contracts import ActionCode, GatewayRequest, GatewayResponse
from sitg_bot.application.gateway import ACTION_POLICIES
from sitg_bot.bot.handlers import common
from sitg_bot.bot.handlers import tournament_registration as handlers
from sitg_bot.bot.i18n import LocalizationService
from sitg_bot.bot.state import GatewayCallError

REFERENCE = "reg_" + "a" * 32
LOBBY_REFERENCE = "join_" + "b" * 32


def invitation(**changes):
    return {
        "tournament_id": str(UUID(int=1)), "name": "Autumn <Cup>",
        "registration_open": True, "membership_status": None, "is_manager": False,
        **changes,
    }


@pytest.mark.parametrize("locale", ["en", "ru"])
@pytest.mark.parametrize("reference", [REFERENCE, LOBBY_REFERENCE])
def test_invitation_confirmation_is_localized_escaped_and_fits_telegram(locale, reference):
    model = handlers.invitation_message(invitation(), reference, LocalizationService(), locale)
    assert "Autumn &lt;Cup&gt;" in model.text
    buttons = model.keyboard.rows[0]
    assert [button.callback_data for button in buttons] == [f"treg:yes:{reference}", "treg:no"]
    assert all(len(button.callback_data.encode()) <= 64 for button in buttons)
    assert len(reference.encode()) <= 64


@pytest.mark.parametrize("changes", [
    {"registration_open": False}, {"is_manager": True},
    {"membership_status": "registered"}, {"membership_status": "approved"},
    {"membership_status": "active"},
])
def test_ineligible_or_existing_registration_has_no_confirmation(changes):
    model = handlers.invitation_message(
        invitation(**changes), REFERENCE, LocalizationService(), "en",
    )
    assert model.keyboard is None


@pytest.mark.parametrize("reference", [REFERENCE, LOBBY_REFERENCE])
async def test_start_prompts_without_registering_or_joining(monkeypatch, reference):
    send = AsyncMock()
    monkeypatch.setattr(handlers, "send_message_model", send)
    backend = SimpleNamespace(
        registration_invitation=AsyncMock(return_value=invitation()),
        register_tournament=AsyncMock(), join_lobby=AsyncMock(),
    )
    await common.handle_start(
        SimpleNamespace(text=f"/start {reference}"), backend, SimpleNamespace(),
        LocalizationService(), "en", SimpleNamespace(context="menu"),
        SimpleNamespace(clear=AsyncMock()),
    )
    assert send.await_args.args[1].keyboard is not None
    backend.register_tournament.assert_not_awaited()
    backend.join_lobby.assert_not_awaited()


async def test_active_lobby_participant_can_join_even_when_registration_closed():
    backend = SimpleNamespace(registration_invitation=AsyncMock(return_value=invitation(
        membership_status="active", registration_open=False,
    )))
    assert not await handlers.show_invitation(
        SimpleNamespace(), backend, SimpleNamespace(), LOBBY_REFERENCE, LocalizationService(), "en",
    )


@pytest.mark.parametrize("private", [True, False])
@pytest.mark.parametrize("role", ["player", "manager"])
async def test_requested_private_links_warn_both_roles(monkeypatch, private, role):
    send = AsyncMock()
    monkeypatch.setattr(handlers, "send_message_model", send)
    backend = SimpleNamespace(registration_link=AsyncMock(return_value={
        "name": "Cup", "reference": REFERENCE,
        "visibility": "private" if private else "public",
    }))
    selected = SimpleNamespace(id=UUID(int=1))
    navigation = SimpleNamespace(
        active_mode=role, allowed_actions=["tournament.registration_link"],
        selected_manager_tournament=selected, selected_player_tournament=selected,
    )
    message = SimpleNamespace(bot=SimpleNamespace(
        get_me=AsyncMock(return_value=SimpleNamespace(username="test_bot")),
    ))
    await handlers.handle_registration_link(
        message, backend, SimpleNamespace(), LocalizationService(), "en", navigation,
    )
    text = send.await_args.args[1].text
    assert f"https://t.me/test_bot?start={REFERENCE}" in text
    assert ("This tournament is private" in text) is private


@pytest.mark.parametrize("choice", ["no", f"yes:{REFERENCE}"])
async def test_no_and_closed_confirmation_never_register(monkeypatch, choice):
    send = AsyncMock()
    monkeypatch.setattr(handlers, "send_message_model", send)
    backend = SimpleNamespace(
        registration_invitation=AsyncMock(return_value=invitation(registration_open=False)),
        register_tournament=AsyncMock(),
    )
    callback = SimpleNamespace(
        data=f"treg:{choice}", answer=AsyncMock(),
        message=AsyncMock(spec=Message, edit_reply_markup=AsyncMock()),
    )
    await handlers.handle_registration_confirmation(
        callback, backend, SimpleNamespace(), LocalizationService(), "en",
        SimpleNamespace(context="menu"),
    )
    callback.message.edit_reply_markup.assert_awaited_once_with(reply_markup=None)
    backend.register_tournament.assert_not_awaited()
    if choice == "no":
        backend.registration_invitation.assert_not_awaited()
    else:
        assert "is closed" in send.await_args.args[1].text


@pytest.mark.parametrize("status", ["registered", "approved", "active", "rejected"])
async def test_yes_submits_invitation_and_renders_authoritative_result(monkeypatch, status):
    send = AsyncMock()
    monkeypatch.setattr(handlers, "send_message_model", send)
    backend = SimpleNamespace(
        registration_invitation=AsyncMock(return_value=invitation()),
        register_tournament=AsyncMock(return_value={
            "accepted": status != "rejected", "status": status, "reasons": ["Requirement failed"],
        }),
    )
    callback = SimpleNamespace(
        data=f"treg:yes:{REFERENCE}", answer=AsyncMock(),
        message=AsyncMock(spec=Message, edit_reply_markup=AsyncMock()),
    )
    claim = SimpleNamespace()
    await handlers.handle_registration_confirmation(
        callback, backend, claim, LocalizationService(), "en", SimpleNamespace(context="menu"),
    )
    backend.register_tournament.assert_awaited_once_with(
        claim, tournament_id=UUID(int=1), invitation_reference=REFERENCE,
    )
    assert "Autumn &lt;Cup&gt;" in send.await_args.args[1].text
    if status == "rejected":
        assert "Requirement failed" in send.await_args.args[1].text


async def test_new_account_keeps_invitation_until_registration_completes(monkeypatch):
    resume = AsyncMock()
    monkeypatch.setattr(common, "resume_registration", resume)
    monkeypatch.setattr(common, "send_message_model", AsyncMock())
    state = SimpleNamespace(
        clear=AsyncMock(), update_data=AsyncMock(),
        get_data=AsyncMock(return_value={"registration_invitation": REFERENCE}),
    )
    backend = SimpleNamespace(registration_invitation=AsyncMock(return_value=invitation()))
    message, claim = SimpleNamespace(text=f"/start {REFERENCE}"), SimpleNamespace()
    await common.handle_start(message, backend, claim, LocalizationService(), "en", None, state)
    state.update_data.assert_awaited_once_with(registration_invitation=REFERENCE)
    assert resume.await_args.kwargs["state"] is state
    send = AsyncMock()
    monkeypatch.setattr(handlers, "send_message_model", send)
    await common.resume_invitation(message, backend, claim, LocalizationService(), "en", state)
    assert send.await_args.args[1].keyboard is not None
    state.update_data.assert_awaited_with(registration_invitation=None)


@pytest.mark.parametrize("status", ["active", "registered", "approved"])
async def test_lobby_confirmation_only_joins_after_activation(monkeypatch, status):
    monkeypatch.setattr(handlers, "send_message_model", AsyncMock())
    monkeypatch.setattr(handlers, "menu_message", lambda *args: SimpleNamespace(text="Menu"))
    backend = SimpleNamespace(
        registration_invitation=AsyncMock(return_value=invitation()),
        register_tournament=AsyncMock(return_value={
            "accepted": True, "status": status, "reasons": [],
        }),
        join_lobby=AsyncMock(), navigation=AsyncMock(),
    )
    callback = SimpleNamespace(
        data=f"treg:yes:{LOBBY_REFERENCE}", answer=AsyncMock(),
        message=AsyncMock(spec=Message, edit_reply_markup=AsyncMock()),
    )
    claim = SimpleNamespace()
    await handlers.handle_registration_confirmation(
        callback, backend, claim, LocalizationService(), "en", SimpleNamespace(context="menu"),
    )
    if status == "active":
        backend.join_lobby.assert_awaited_once_with(claim, invitation_code="b" * 32)
    else:
        backend.join_lobby.assert_not_awaited()


async def test_confirmation_handles_registration_closing_after_preview(monkeypatch):
    send = AsyncMock()
    monkeypatch.setattr(handlers, "send_message_model", send)
    closed = invitation(registration_open=False)
    backend = SimpleNamespace(
        registration_invitation=AsyncMock(side_effect=[invitation(), closed]),
        register_tournament=AsyncMock(side_effect=GatewayCallError(GatewayResponse.model_validate({
            "action": ActionCode.TOURNAMENT_REGISTER,
            "correlation_id": str(UUID(int=10)), "ok": False,
            "error": {"code": "validation_failed", "message_key": "error.validation_failed"},
        }))),
    )
    callback = SimpleNamespace(
        data=f"treg:yes:{REFERENCE}", answer=AsyncMock(),
        message=AsyncMock(spec=Message, edit_reply_markup=AsyncMock()),
    )
    await handlers.handle_registration_confirmation(
        callback, backend, SimpleNamespace(), LocalizationService(), "en",
        SimpleNamespace(context="menu"),
    )
    assert "is closed" in send.await_args.args[1].text
    assert send.await_args.args[1].keyboard is None


@pytest.mark.parametrize("action, fields", [
    (ActionCode.TOURNAMENT_REGISTRATION_LINK, {"tournament_id": str(UUID(int=1))}),
    (ActionCode.TOURNAMENT_REGISTRATION_INVITATION, {"reference": REFERENCE}),
    (ActionCode.TOURNAMENT_REGISTER, {
        "tournament_id": str(UUID(int=1)), "invitation_reference": REFERENCE,
    }),
])
def test_gateway_invitation_operations_require_authentication(action, fields):
    request = GatewayRequest.model_validate({
        "metadata": {
            "correlation_id": str(UUID(int=10)), "channel": "telegram_bot",
            "client_name": "test", "client_version": "1", "idempotency_key": "registration-link",
        },
        "operation": {"action": action, **fields},
    })
    assert request.operation.action == action
    assert ACTION_POLICIES[action].authentication_required
    assert ACTION_POLICIES[action].mutation is (action == ActionCode.TOURNAMENT_REGISTER)
    assert ACTION_POLICIES[action].idempotency_required is (
        action == ActionCode.TOURNAMENT_REGISTER
    )
