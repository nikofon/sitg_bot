"""Authored regression tests; run explicitly (see docs/telegram-gameplay-validation.md)."""

import asyncio
import copy
import html
from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock
from uuid import UUID

import pytest
from aiogram.dispatcher.event.bases import SkipHandler
from aiogram.exceptions import TelegramBadRequest, TelegramRetryAfter
from aiogram.methods import EditMessageText, SendMessage
from aiogram.types import ForceReply, Message, ReplyKeyboardMarkup, ReplyKeyboardRemove

from sitg_bot.application.contracts import ActionCode, GameActOperation
from sitg_bot.application.gateway import ACTION_POLICIES
from sitg_bot.application.protocol import RetryableDeliveryError
from sitg_bot.bot.app import configure_bot_commands
from sitg_bot.bot.game_delivery import GameDelivery
from sitg_bot.bot.handlers.game import (
    handle_game_appeal_pick,
    handle_game_command,
    handle_game_input,
    handle_game_keyboard,
    perform,
    report_command,
    request_appeal,
)
from sitg_bot.bot.i18n import LocalizationService
from sitg_bot.bot.presenters.game import (
    game_keyboard,
    join_message,
    plain_chunks,
    rating_message,
    score_text,
)
from sitg_bot.bot.presenters.render import telegram_keyboard

GAME = str(UUID(int=1))
ROUND = str(UUID(int=5))


async def test_exit_waits_for_inflight_delivery_and_stops_following_messages():
    bot, protocol, delivery = fixture()
    entered = asyncio.Event()
    release = asyncio.Event()

    async def delayed_send(*args, **kwargs):
        entered.set()
        await release.wait()
        return SimpleNamespace(message_id=101)

    bot.send_message.side_effect = delayed_send
    protocol.events = [event(1, "game_paused")]
    sending = asyncio.create_task(delivery.sync(42, GAME))
    await entered.wait()

    async def dismiss(*args, **kwargs):
        protocol.dismissed = True
        return {"accepted": True}

    backend = SimpleNamespace(game_delivery=delivery, game_action=AsyncMock(side_effect=dismiss))
    message = SimpleNamespace(chat=SimpleNamespace(id=42), message_id=123)
    leaving = asyncio.create_task(
        perform(message, backend, object(), view(), LocalizationService(), "en", "quit")
    )
    await asyncio.sleep(0)
    backend.game_action.assert_not_awaited()
    release.set()
    await sending
    assert await leaving
    protocol.events.append(event(2, "game_resumed"))
    await delivery.sync(42, GAME)
    assert bot.send_message.await_count == 1


async def test_report_requires_reviewable_confirmation_and_returns_receipt():
    snapshot = view(status="finalized", actions=["report"])
    backend, message, _ = handler_fixture(snapshot)
    backend.game_action.return_value = {"accepted": True, "receipt": {"report_id": "receipt-1"}}
    data = {}

    async def save(values):
        data.update(values)

    state = SimpleNamespace(
        set_state=AsyncMock(),
        set_data=AsyncMock(side_effect=save),
        get_data=AsyncMock(side_effect=lambda: dict(data)),
        clear=AsyncMock(),
    )
    await report_command(
        message,
        backend,
        object(),
        snapshot,
        LocalizationService(),
        "ru",
        state,
        "2 toxicity Details",
    )
    backend.game_action.assert_not_awaited()
    confirmation = backend.game_delivery.send.await_args.args[3]
    assert "Other &lt;b&gt;" in confirmation.text
    assert "Details" in confirmation.text and "/report confirm" in confirmation.text
    await report_command(
        message, backend, object(), snapshot, LocalizationService(), "ru", state, "confirm"
    )
    assert backend.game_action.await_args.kwargs["target_id"] == str(UUID(int=3))
    assert backend.game_action.await_args.kwargs["text"] == "Details"
    assert "receipt-1" in backend.game_delivery.send.await_args.args[3].text
    state.clear.assert_awaited_once()


async def test_cleanup_continues_past_messages_telegram_cannot_delete():
    bot, protocol, delivery = fixture()
    protocol.messages = {"old": {"ids": [1]}, "new": {"ids": [2]}}
    bot.delete_message.side_effect = [
        TelegramBadRequest(
            method=SendMessage(chat_id=42, text="x"), message="message can't be deleted"
        ),
        True,
    ]
    await delivery.cleanup(
        {"recipient_telegram_user_id": 42, "game_id": GAME, "message_ids": [1, 2]}
    )
    assert protocol.messages["_deleted"]["ids"] == [1, 2]


async def test_cleanup_uses_exit_snapshot_after_reconnection():
    bot, protocol, delivery = fixture()
    protocol.dismissed = False
    protocol.messages = {"question": {"ids": [200]}}
    await delivery.cleanup(
        {"recipient_telegram_user_id": 42, "game_id": GAME, "message_ids": [100, 101]}
    )
    assert [call.args[1] for call in bot.delete_message.await_args_list] == [100, 101]
    assert protocol.messages["question"]["ids"] == [200]


def view(**changes):
    return {
        "id": GAME,
        "viewer_id": str(UUID(int=2)),
        "status": "active",
        "phase": "question",
        "locale": "ru",
        "paused": False,
        "message_delay": 3.0,
        "actions": ["buzz", "pause", "appeal"],
        "dismissed": False,
        "messages": {},
        "participants": [
            {
                "id": str(UUID(int=2)),
                "name": "Me",
                "self": True,
                "score": 10,
                "place": None,
                "correct_points": 20,
            },
            {
                "id": str(UUID(int=3)),
                "name": "Other <b>",
                "self": False,
                "score": 10,
                "place": None,
                "correct_points": 10,
                "vote_reason": None,
            },
            {
                "id": str(UUID(int=4)),
                "name": "Third",
                "self": False,
                "score": 0,
                "place": None,
                "correct_points": 0,
                "vote_reason": "cooldown",
            },
        ],
        "question": {"round_id": ROUND, "text": "Visible <text>", "form": None},
        "appeal": None,
    } | changes


def event(sequence, kind, **parameters):
    return {"sequence": sequence, "kind": kind, "parameters": parameters}


class PresentationProtocol:
    def __init__(self, snapshot=None):
        self.view = snapshot or view()
        self.messages = {}
        self.sequence = 0
        self.events = []
        self.dismissed = False

    async def request(self, method, **values):
        if method == "telegram.game.delivery":
            return {
                "skip": self.dismissed,
                "view": copy.deepcopy(self.view),
                "messages": copy.deepcopy(self.messages),
                "events": [e for e in self.events if e["sequence"] > self.sequence][:100],
                "sequence": self.sequence,
            }
        if method == "telegram.game.presentation":
            return {"messages": copy.deepcopy(self.messages), "dismissed": self.dismissed}
        if method == "telegram.game.record":
            if "key" in values:
                self.messages[values["key"]] = copy.deepcopy(values["value"])
            if "sequence" in values:
                self.sequence = max(self.sequence, values["sequence"])
        return {}


def fixture(snapshot=None):
    protocol = PresentationProtocol(snapshot)
    counter = 100

    async def send(*args, **kwargs):
        nonlocal counter
        counter += 1
        return SimpleNamespace(message_id=counter)

    bot = SimpleNamespace(
        send_message=AsyncMock(side_effect=send),
        edit_message_text=AsyncMock(),
        delete_message=AsyncMock(),
    )
    delivery = GameDelivery(bot, LocalizationService(), protocol, None)
    return bot, protocol, delivery


@pytest.mark.parametrize("kind", ["appeal_resolved", "appeal_escalated"])
@pytest.mark.parametrize(
    "next_kind", ["question_cost_announced", "theme_completed", "game_finalized"]
)
async def test_appeal_delivery_delay_survives_restart(monkeypatch, kind, next_kind):
    now = 1000.0
    monkeypatch.setattr("sitg_bot.bot.game_delivery.time.time", lambda: now)
    bot, protocol, delivery = fixture()
    protocol.events = [
        event(1, kind, appeal_id=str(UUID(int=9)), approved=True),
        event(2, next_kind, theme="Theme", value=10, name="Theme"),
    ]
    with pytest.raises(RetryableDeliveryError):
        await delivery.sync(42, GAME)
    assert protocol.sequence == 1
    assert bot.send_message.await_count == 1
    restarted = GameDelivery(bot, LocalizationService(), protocol, None)
    now += 2
    with pytest.raises(RetryableDeliveryError):
        await restarted.sync(42, GAME)
    assert bot.send_message.await_count == 1
    now += 1
    await restarted.sync(42, GAME)
    assert protocol.sequence == 2
    assert bot.send_message.await_count > 1


def test_final_scores_include_both_rating_deltas():
    snapshot = view(
        rating_changes=[
            {
                "player_id": str(UUID(int=2)), "scope": scope,
                "before": 1500, "after": 1516, "delta": 16,
            }
            for scope in ("ruleset", "tournament")
        ]
    )
    text = score_text(snapshot, LocalizationService(), "ru", final=True)
    assert "Глобальный рейтинг: 1500.00 → 1516.00 (+16.00)" in text
    assert "Рейтинг турнира: 1500.00 → 1516.00 (+16.00)" in text


@pytest.mark.parametrize("locale", ["ru", "en"])
def test_reply_keyboard_layout_and_stable_join_callback(locale):
    keyboard = telegram_keyboard(game_keyboard())
    assert isinstance(keyboard, ReplyKeyboardMarkup)
    assert [[b.text for b in row] for row in keyboard.keyboard] == [["+"], ["||", "!"]]
    assert keyboard.is_persistent
    join = join_message(view(status="lobby"), LocalizationService(), locale)
    assert len(join.keyboard.rows) == 1
    assert join.keyboard.rows[0][0].callback_data == f"gamejoin:{UUID(GAME).hex}"
    if locale == "ru":
        assert html.unescape(join.text).startswith('Ваша игра создана, нажмите "Присоединиться"')
        assert "Игроки: Me, Other <b>, Third" in html.unescape(join.text)


def test_join_message_includes_tournament_packets_and_players():
    localization = LocalizationService()
    snapshot = view(status="lobby")
    snapshot["tournament_name"] = "<Cup>"
    snapshot["packet_names"] = ["Packet A", "Packet B"]
    join = join_message(snapshot, localization, "en")
    text = html.unescape(join.text)
    assert text.startswith('Your game has been created. Press "Join".')
    assert "Tournament: <Cup>" in text
    assert "Packets: Packet A, Packet B" in text
    assert "Players: Me, Other <b>, Third" in text


def test_final_scores_and_ratings_are_separate_and_preserve_shared_places():
    snapshot = view(status="finalized")
    for p in snapshot["participants"][:2]:
        p["place"] = 1.5
    text = score_text(snapshot, LocalizationService(), "ru", final=True)
    assert text.startswith("Финальный счёт:")
    assert text.count("1.5)") == 2
    assert "Other &lt;b&gt; Счёт: 10 Без минусов: 10" in text
    assert "/quit" in text
    vote = rating_message(snapshot, 1, LocalizationService(), "ru")
    assert [b.text for b in vote.keyboard.rows[0]] == ["👍", "👎"]
    assert all(len(b.callback_data.encode()) <= 64 for b in vote.keyboard.rows[0])
    assert rating_message(snapshot, 2, LocalizationService(), "ru").keyboard is None
    snapshot["participants"][1]["vote_reason"] = "voted"
    assert rating_message(snapshot, 1, LocalizationService(), "ru").keyboard is None


def test_long_questions_are_escaped_without_losing_text():
    text = "<&😀" * 4000
    chunks = plain_chunks(text)
    assert "".join(html.unescape(c) for c in chunks) == text
    assert all(len(html.unescape(c).encode("utf-16-le")) <= 8192 for c in chunks)


async def test_commands_remove_game_and_question_and_expose_results_and_report():
    bot = SimpleNamespace(set_my_commands=AsyncMock())
    await configure_bot_commands(bot, LocalizationService())
    for call in bot.set_my_commands.await_args_list:
        names = {c.command for c in call.args[0]}
        assert {"results", "report", "escalate", "quit"} <= names
        assert not {"game", "question", "vote"} & names


def test_quit_is_a_typed_idempotent_game_action():
    operation = GameActOperation(action=ActionCode.GAME_ACT, game_id=UUID(GAME), command="quit")
    assert operation.command == "quit"
    assert ACTION_POLICIES[ActionCode.GAME_ACT].idempotency_required


async def test_join_is_sent_once_across_direct_sync_and_outbox_then_start_precedes_themes():
    bot, protocol, delivery = fixture(view(status="lobby"))
    protocol.events = [event(1, "game_created"), event(2, "players_assigned")]
    await delivery.sync(42, GAME)
    await delivery({"recipient_telegram_user_id": 42, "game_id": GAME})
    assert bot.send_message.await_count == 1
    protocol.view["status"] = "active"
    protocol.events += [
        event(3, "ready_countdown"),
        event(4, "game_started"),
        event(5, "themes_announced", themes=[{"name": "Theme"}]),
        event(6, "theme_started", position=1, name="Theme", author="Author"),
        event(7, "theme_commentary_announced", name="Theme", commentary="About the theme"),
        event(8, "question_cost_announced", round_id=ROUND, theme="Theme", value=10),
        event(9, "question_token_revealed", round_id=ROUND, text="First token"),
    ]
    await delivery.sync(42, GAME)
    texts = [c.args[1] for c in bot.send_message.await_args_list]
    assert len(texts) == 7
    assert texts[1].startswith("Все игроки присоединились.")
    assert isinstance(
        bot.send_message.await_args_list[1].kwargs["reply_markup"], ReplyKeyboardMarkup
    )
    assert texts[2] == "Темы игры:\n1) Theme"
    assert texts[3].startswith("Тема 1: <b>Theme</b>\nАвтор: Author")
    assert texts[4] == "Комментарий к теме: About the theme"
    assert texts[5] == "Тема: Theme\nВопрос за 10"
    assert texts[6] == "First token"
    assert all("Между вопросами" not in text for text in texts)


async def test_join_updates_existing_prompt_with_waiting_confirmation():
    snapshot = view(status="lobby")
    bot, protocol, delivery = fixture(snapshot)
    protocol.events = [event(1, "game_created"), event(2, "players_assigned")]
    await delivery.sync(42, GAME)
    message_id = protocol.messages["join"]["ids"][0]
    protocol.view["participants"][0]["joined"] = True
    protocol.events.append(event(3, "player_joined"))
    await delivery.sync(42, GAME)
    assert bot.send_message.await_count == 1
    assert "Вы присоединились" in bot.edit_message_text.await_args.args[0]
    assert "1/3" in bot.edit_message_text.await_args.args[0]
    assert bot.edit_message_text.await_args.kwargs["message_id"] == message_id
    assert bot.edit_message_text.await_args.kwargs["reply_markup"] is None


async def test_buzz_hides_question_wrong_answer_resumes_same_message_and_completion_reveals_it():
    bot, protocol, delivery = fixture()
    protocol.events = [event(1, "question_token_revealed", round_id=ROUND, text="First")]
    await delivery.sync(42, GAME)
    question_id = protocol.messages[f"question:{ROUND}:0"]["ids"][0]
    protocol.events += [event(2, "player_buzzed", round_id=ROUND, mine=True, form="Who?")]
    await delivery.sync(42, GAME)
    assert bot.edit_message_text.await_args.args[0] == "Вопрос скрыт"
    assert bot.edit_message_text.await_args.kwargs["message_id"] == question_id
    assert "<b>Who?</b>" in bot.send_message.await_args.args[1]
    assert isinstance(bot.send_message.await_args.kwargs["reply_markup"], ForceReply)
    assert protocol.messages["event:2"]["round_id"] == ROUND
    assert protocol.messages["event:2"]["kind"] == "answer"
    protocol.events += [
        event(
            3,
            "answer_judged",
            round_id=ROUND,
            mine=False,
            name="Me",
            answer="Wrong",
            timed_out=False,
            original_correct=False,
            revealed_text="First",
        )
    ]
    await delivery.sync(42, GAME)
    assert bot.edit_message_text.await_args.args[0] == "First"
    assert bot.edit_message_text.await_args.kwargs["message_id"] == question_id
    assert [c.args[1] for c in bot.send_message.await_args_list][-2:] == [
        "Me: Wrong",
        "Неправильный ответ!",
    ]
    protocol.events += [
        event(
            4,
            "question_completed",
            round_id=ROUND,
            text="First and remaining",
            answer="Official",
            commentary="Explanation",
            author="Author",
        )
    ]
    await delivery.sync(42, GAME)
    assert bot.edit_message_text.await_args.args[0] == "First and remaining"
    assert bot.edit_message_text.await_args.kwargs["message_id"] == question_id
    assert bot.send_message.await_args.args[1] == (
        "Ответ: Official\nКомментарий: Explanation\nАвтор: Author"
    )
    assert sum("First" in c.args[1] for c in bot.send_message.await_args_list) == 1


@pytest.mark.parametrize(
    "timed_out,correct,expected", [(True, False, "Нет ответа!"), (False, True, "Ответ верный!")]
)
async def test_answer_verdicts(timed_out, correct, expected):
    bot, protocol, delivery = fixture()
    protocol.events = [
        event(
            1,
            "answer_judged",
            round_id=ROUND,
            mine=True,
            answer=None,
            timed_out=timed_out,
            original_correct=correct,
        )
    ]
    await delivery.sync(42, GAME)
    assert bot.send_message.await_args.args[1] == expected


async def test_reveal_coalesces_only_before_boundaries_and_restart_reuses_question():
    bot, protocol, delivery = fixture()
    protocol.events = [
        event(1, "question_token_revealed", round_id=ROUND, text="One"),
        event(2, "question_token_revealed", round_id=ROUND, text="One two"),
    ]
    await delivery.sync(42, GAME)
    assert bot.send_message.await_count == 1
    assert bot.send_message.await_args.args[1] == "One two"
    protocol.events += [event(3, "player_buzzed", round_id=ROUND, mine=False, name="Other <b>")]
    restarted = GameDelivery(bot, LocalizationService(), protocol, None, "test_bot")
    await restarted.sync(42, GAME)
    assert bot.send_message.await_count == 2
    assert bot.send_message.await_args.args[1] == "Other &lt;b&gt; отбился!"
    assert bot.edit_message_text.await_args.args[0] == "Вопрос скрыт"
    await restarted.sync(42, GAME)
    assert bot.edit_message_text.await_count == 1
    assert bot.send_message.await_count == 2


async def test_final_results_have_no_inline_controls_and_each_opponent_gets_a_rating_message():
    bot, protocol, delivery = fixture(view(status="finalized"))
    protocol.events = [event(1, "game_completed"), event(2, "game_finalized")]
    await delivery.sync(42, GAME)
    calls = bot.send_message.await_args_list
    assert len(calls) == 4
    assert calls[0].args[1].startswith("Финальный счёт:")
    assert isinstance(calls[0].kwargs["reply_markup"], ReplyKeyboardRemove)
    assert calls[1].args[1] == "Оцените своих оппонентов"
    assert calls[2].args[1] == "Other &lt;b&gt;"
    assert len(calls[2].kwargs["reply_markup"].inline_keyboard[0]) == 2


async def test_deleted_question_message_is_recreated_and_flood_wait_does_not_acknowledge():
    bot, protocol, delivery = fixture()
    protocol.messages[f"question:{ROUND}:0"] = {"ids": [100], "digest": "old"}
    protocol.events = [event(1, "question_token_revealed", round_id=ROUND, text="Updated")]
    bot.edit_message_text.side_effect = TelegramBadRequest(
        method=EditMessageText(text="x", chat_id=42, message_id=100),
        message="message to edit not found",
    )
    bot.send_message.side_effect = TelegramRetryAfter(
        method=SendMessage(chat_id=42, text="x"), message="flood", retry_after=3
    )
    with pytest.raises(RetryableDeliveryError) as error:
        await delivery({"recipient_telegram_user_id": 42, "game_id": GAME})
    assert error.value.retry_after_seconds == 3
    assert protocol.sequence == 0
    bot.send_message.side_effect = None
    bot.send_message.return_value = SimpleNamespace(message_id=200)
    await delivery.sync(42, GAME)
    assert protocol.messages[f"question:{ROUND}:0"]["ids"] == [200]
    assert protocol.sequence == 1


async def test_cleanup_is_durable_and_best_effort_and_dismissed_delivery_stops():
    bot, protocol, delivery = fixture()
    protocol.messages = {
        "question": {"ids": [100]},
        "input:101": {"ids": [101]},
        "answer": {"ids": [102]},
    }
    protocol.dismissed = True
    bot.delete_message.side_effect = [
        True,
        TelegramRetryAfter(
            method=SendMessage(chat_id=42, text="x"), message="flood", retry_after=1
        ),
    ]
    payload = {
        "recipient_telegram_user_id": 42, "game_id": GAME, "message_ids": [100, 101, 102]
    }
    with pytest.raises(RetryableDeliveryError):
        await delivery.cleanup(payload)
    assert protocol.messages["_deleted"]["ids"] == [100]
    bot.delete_message.side_effect = None
    await GameDelivery(bot, LocalizationService(), protocol, None).cleanup(payload)
    assert protocol.messages["_deleted"]["ids"] == [100, 101, 102]
    assert [c.args[1] for c in bot.delete_message.await_args_list] == [100, 101, 101, 102]
    protocol.events = [event(1, "game_completed")]
    await delivery(payload)
    bot.send_message.assert_not_awaited()


def handler_fixture(snapshot=None):
    snapshot = snapshot or view()
    backend = SimpleNamespace(
        game_view=AsyncMock(return_value=snapshot),
        game_action=AsyncMock(return_value={"accepted": True}),
        game_delivery=SimpleNamespace(track=AsyncMock(), send=AsyncMock(), sync=AsyncMock()),
    )
    message = SimpleNamespace(
        chat=SimpleNamespace(id=42),
        message_id=123,
        reply_to_message=None,
        text="answer",
        answer=AsyncMock(),
    )
    navigation = SimpleNamespace(context="game", active_game=SimpleNamespace(id=UUID(GAME)))
    return backend, message, navigation


def appeal_choice_view():
    return view(appeal_targets=[
        {"id": str(UUID(int=10)), "player_id": str(UUID(int=2)),
         "submitted_answer": "My wrong answer", "original_correct": False},
        {"id": str(UUID(int=11)), "player_id": str(UUID(int=3)),
         "submitted_answer": "Credited answer", "original_correct": True},
    ])


async def test_appeal_offers_explicit_choices_for_own_wrong_and_other_correct_answer():
    snapshot = appeal_choice_view()
    backend, message, _ = handler_fixture(snapshot)
    assert not await request_appeal(
        message, backend, object(), snapshot, LocalizationService(), "ru"
    )
    backend.game_action.assert_not_awaited()
    call = backend.game_delivery.send.await_args
    model = call.args[3]
    assert "My wrong answer" in model.text and "Credited answer" in model.text
    buttons = [row[0] for row in model.keyboard.rows]
    assert [b.text for b in buttons] == ["1) зачесть отклонённый ответ", "2) снять зачтённый ответ"]
    assert [b.callback_data for b in buttons] == [
        f"appealpick:{UUID(a['id']).hex}" for a in snapshot["appeal_targets"]
    ]
    assert call.kwargs["round_id"] == ROUND


@pytest.mark.parametrize("target_index", [0, 1])
async def test_single_appeal_target_is_submitted_without_prompt(target_index):
    snapshot = appeal_choice_view()
    snapshot["appeal_targets"] = [snapshot["appeal_targets"][target_index]]
    backend, message, _ = handler_fixture(snapshot)
    assert await request_appeal(message, backend, object(), snapshot, LocalizationService(), "en")
    backend.game_delivery.send.assert_not_awaited()
    assert backend.game_action.await_args.kwargs["target_id"] == snapshot["appeal_targets"][0]["id"]


@pytest.mark.parametrize("target_index,stale", [(0, False), (1, False), (1, True)])
async def test_appeal_choice_submits_selected_target_and_rejects_old_round(target_index, stale):
    snapshot = appeal_choice_view()
    snapshot["messages"] = {"choice": {
        "ids": [123], "kind": "appeal", "round_id": ROUND,
        "targets": [a["id"] for a in snapshot["appeal_targets"]],
    }}
    if stale:
        snapshot["question"]["round_id"] = str(UUID(int=99))
    backend, _, navigation = handler_fixture(snapshot)
    message = MagicMock(spec=Message)
    message.message_id, message.chat = 123, SimpleNamespace(id=42)
    message.edit_reply_markup = AsyncMock()
    target = snapshot["appeal_targets"][target_index]
    callback = SimpleNamespace(
        message=message, data=f"appealpick:{UUID(target['id']).hex}", answer=AsyncMock()
    )
    await handle_game_appeal_pick(
        callback, backend, object(), LocalizationService(), "en", navigation
    )
    if stale:
        backend.game_action.assert_not_awaited()
        assert callback.answer.await_args.kwargs["show_alert"]
    else:
        assert backend.game_action.await_args.kwargs["target_id"] == target["id"]
        assert backend.game_action.await_args.kwargs["round_id"] == ROUND
        message.edit_reply_markup.assert_awaited_once_with(reply_markup=None)


async def test_reply_to_unrelated_message_is_not_an_answer():
    backend, message, navigation = handler_fixture()
    message.reply_to_message = SimpleNamespace(message_id=999)
    with pytest.raises(SkipHandler):
        await handle_game_input(message, backend, object(), LocalizationService(), "en", navigation)
    backend.game_action.assert_not_awaited()


async def test_prompt_round_survives_restart_and_is_never_replaced_by_current_round():
    snapshot = view()
    snapshot["messages"] = {"prompt": {"ids": [42], "kind": "answer", "round_id": ROUND}}
    snapshot["question"]["round_id"] = str(UUID(int=6))
    backend, message, navigation = handler_fixture(snapshot)
    message.reply_to_message = SimpleNamespace(message_id=42)
    await handle_game_input(message, backend, object(), LocalizationService(), "en", navigation)
    assert backend.game_action.await_args.kwargs["round_id"] == ROUND


async def test_keyboard_buzz_is_bound_to_delivered_question_not_unseen_next_question():
    snapshot = view(messages={"screen": {"round_id": ROUND}})
    snapshot["question"]["round_id"] = str(UUID(int=6))
    backend, message, navigation = handler_fixture(snapshot)
    message.text = "+"
    await handle_game_keyboard(message, backend, object(), LocalizationService(), "en", navigation)
    assert backend.game_action.await_args.kwargs["command"] == "buzz"
    assert backend.game_action.await_args.kwargs["round_id"] == ROUND


@pytest.mark.parametrize("raced", [False, True])
async def test_rejected_buzz_explains_other_player_is_answering(raced):
    snapshot = view(actions=["buzz"] if raced else [])
    snapshot["participants"][0].update(active=True, joined=True)
    snapshot["question"]["accepted_buzzer_id"] = str(UUID(int=3))
    backend, message, navigation = handler_fixture(snapshot)
    message.text = "+"
    backend.game_action.return_value = {"accepted": False, "reason": "another_answering"}
    await handle_game_keyboard(message, backend, object(), LocalizationService(), "en", navigation)
    assert "Another player has already buzzed" in backend.game_delivery.send.await_args.args[3].text
    if not raced:
        backend.game_action.assert_not_awaited()


async def test_commands_after_exit_do_not_resolve_a_historical_game():
    backend, message, navigation = handler_fixture()
    navigation.context, navigation.active_game = "menu", None
    message.text = "/results"
    await handle_game_command(
        message, backend, object(), LocalizationService(), "en", navigation, SimpleNamespace(), None
    )
    backend.game_view.assert_not_awaited()
    backend.game_action.assert_not_awaited()


@pytest.mark.parametrize("available", [False, True])
async def test_themes_command_requires_revealed_themes(available):
    snapshot = view(actions=["themes"] if available else [])
    snapshot["themes"] = [{"name": "First <theme>"}, {"name": "Second"}]
    backend, message, navigation = handler_fixture(snapshot)
    message.text = "/themes"
    await handle_game_command(
        message, backend, object(), LocalizationService(), "en", navigation, SimpleNamespace(), None
    )
    text = backend.game_delivery.send.await_args.args[3].text
    if available:
        assert "1) First &lt;theme&gt;\n2) Second" in text
    else:
        assert "First" not in text
    backend.game_action.assert_not_awaited()


async def test_score_command_and_scoreboard_sort_descending():
    snapshot = view()
    for player, score in zip(snapshot["participants"], [-20, 50, 10], strict=True):
        player["score"] = score
    backend, message, navigation = handler_fixture(snapshot)
    message.text = "/score"
    await handle_game_command(
        message, backend, object(), LocalizationService(), "en", navigation, SimpleNamespace(), None
    )
    text = backend.game_delivery.send.await_args.args[3].text
    names = [html.escape(p["name"]) for p in snapshot["participants"]]
    assert text.index(names[1]) < text.index(names[2]) < text.index(names[0])
    bot, protocol, delivery = fixture(snapshot)
    delivery.bot_username = "test_bot"
    protocol.events = [event(1, "scoreboard", players=[
        {"name": p["name"], "score": p["score"], "player_id": p["id"]}
        for p in snapshot["participants"]
    ])]
    await delivery.sync(42, GAME)
    text = bot.send_message.await_args.args[1]
    assert text.index(names[1]) < text.index(names[2]) < text.index(names[0])
    assert text.count('<a href="https://t.me/test_bot/profiles?startapp=player_') == 3
