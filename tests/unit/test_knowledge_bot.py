from types import SimpleNamespace
from unittest.mock import AsyncMock
from uuid import UUID

import pytest

from sitg_bot.bot.handlers import knowledge as handlers
from sitg_bot.bot.i18n import LocalizationService

FACT_ID = UUID(int=51)

EN = {
    "knowledge.facts.title": "Knowledge library ({total})",
    "knowledge.facts.empty": "No facts yet.",
    "knowledge.facts.entry": "{title}\n{statistics}",
    "knowledge.facts.tags": "Tags: {tags}",
    "knowledge.facts.more": "More ({total})",
    "knowledge.facts.previous": "Previous",
    "knowledge.statistic.none": "No statistics",
    "knowledge.statistic.pending": "Statistics pending",
    "knowledge.statistic.fresh": "Statistics current",
    "knowledge.statistic.stale": "Statistics stale",
    "knowledge.fact.title": "{title}",
    "knowledge.fact.link": "{title}: {url}",
    "knowledge.fact.origin": "Origin: {origin}",
    "knowledge.origin.manual": "manual",
    "knowledge.origin.dissection": "dissection",
    "knowledge.fact.read_only": "Read-only shared fact",
    "knowledge.shared.title": "Shared with you",
    "knowledge.shared.empty": "Nothing shared with you.",
    "knowledge.shared.entry": "Shared by {owner}",
    "knowledge.dissection.title": "Packet dissection",
    "knowledge.dissection.empty": "No dissections yet.",
    "knowledge.dissection.entry": "{packet}: {status}, {applied} facts added",
    "knowledge.dissection.partial": "Some candidates failed; retry available.",
    "knowledge.dissection.status.pending": "pending",
    "knowledge.dissection.status.succeeded": "succeeded",
    "knowledge.dissection.status.partially_succeeded": "partial",
    "knowledge.dissection.status.failed": "failed",
    "knowledge.error.forbidden": "Not shared with you.",
    "knowledge.error.not_found": "Not found.",
    "knowledge.error.invalid": "Invalid request.",
}

RU = {
    "knowledge.facts.title": "Библиотека знаний ({total})",
    "knowledge.facts.empty": "Фактов пока нет.",
    "knowledge.facts.entry": "{title}\n{statistics}",
    "knowledge.facts.tags": "Метки: {tags}",
    "knowledge.facts.more": "Ещё ({total})",
    "knowledge.facts.previous": "Назад",
    "knowledge.statistic.none": "Нет статистики",
    "knowledge.statistic.pending": "Статистика считается",
    "knowledge.statistic.fresh": "Статистика актуальна",
    "knowledge.statistic.stale": "Статистика устарела",
    "knowledge.fact.title": "{title}",
    "knowledge.fact.link": "{title}: {url}",
    "knowledge.fact.origin": "Происхождение: {origin}",
    "knowledge.origin.manual": "вручную",
    "knowledge.origin.dissection": "разбор",
    "knowledge.fact.read_only": "Факт доступен только для чтения",
    "knowledge.shared.title": "Доступные подборки",
    "knowledge.shared.empty": "Ничего не доступно.",
    "knowledge.shared.entry": "Подборка {owner}",
    "knowledge.dissection.title": "Разбор пакетов",
    "knowledge.dissection.empty": "Разборов пока нет.",
    "knowledge.dissection.entry": "{packet}: {status}, добавлено фактов: {applied}",
    "knowledge.dissection.partial": "Часть кандидатов не применена; доступен повтор.",
    "knowledge.dissection.status.pending": "ожидает",
    "knowledge.dissection.status.succeeded": "готово",
    "knowledge.dissection.status.partially_succeeded": "частично",
    "knowledge.dissection.status.failed": "ошибка",
    "knowledge.error.forbidden": "Подборка недоступна.",
    "knowledge.error.not_found": "Не найдено.",
    "knowledge.error.invalid": "Некорректный запрос.",
}


@pytest.fixture()
def localization() -> LocalizationService:
    return LocalizationService(catalogs={"en": EN, "ru": RU})


def page(**changes):
    values = {
        "items": [
            {
                "fact_id": str(FACT_ID),
                "title": "Battle of <Austerlitz>",
                "tag_names": ["napoleonic wars"],
                "origin": "dissection",
                "statistic_status": "stale",
                "updated_at": "2026-10-01T00:00:00+00:00",
            }
        ],
        "total": 1,
        "page": 1,
        "next_page": 2,
    }
    values.update(changes)
    return values


@pytest.mark.parametrize("locale", ["en", "ru"])
def test_facts_message_escapes_titles_and_bounds_callbacks(locale, localization):
    model = handlers.facts_message(page(), localization, locale)
    assert "Battle of &lt;Austerlitz&gt;" in model.text
    buttons = [button for row in model.keyboard.rows for button in row]
    callbacks = [button.callback_data for button in buttons]
    assert callbacks == [f"klib:fact:{FACT_ID}", "klib:page:2"]
    assert all(len(value.encode()) <= 64 for value in callbacks)


def test_facts_message_renders_pagination_and_empty_state(localization):
    empty = handlers.facts_message(page(items=[], next_page=None), localization, "en")
    assert "No facts yet." in empty.text
    assert empty.keyboard is None
    second = handlers.facts_message(page(page=2, next_page=None), localization, "en")
    callbacks = [button.callback_data for row in second.keyboard.rows for button in row]
    assert callbacks[-1] == "klib:page:1"


def test_fact_detail_message_shows_provenance_links_and_read_only(localization):
    detail = {
        "title": "Battle of <Austerlitz>",
        "description": "1805 battle",
        "links": [{"title": "Source", "url": "https://example.test/a?x=1&y=2"}],
        "origin": "dissection",
        "editable": False,
    }
    model = handlers.fact_detail_message(detail, localization, "en")
    assert "Battle of &lt;Austerlitz&gt;" in model.text
    assert "Source: https://example.test/a?x=1&amp;y=2" in model.text
    assert "Origin: dissection" in model.text
    assert "Read-only shared fact" in model.text


def test_dissection_status_message_reports_partial_work(localization):
    model = handlers.dissection_status_message(
        {
            "items": [
                {
                    "packet_id": "packet",
                    "packet_name": "Cup & Packet",
                    "status": "partially_succeeded",
                    "applied_fact_ids": [str(FACT_ID)],
                    "failed_candidate_ids": ["dc_bad"],
                }
            ]
        },
        localization,
        "en",
    )
    assert "Cup &amp; Packet: partial, 1 facts added" in model.text
    assert "Some candidates failed; retry available." in model.text


async def test_handle_facts_sends_localized_list(monkeypatch, localization):
    send = AsyncMock()
    monkeypatch.setattr(handlers, "send_message_model", send)
    backend = SimpleNamespace(facts_page=AsyncMock(return_value=page()))
    await handlers.handle_facts(
        SimpleNamespace(), backend, localization, "en", SimpleNamespace()
    )
    assert "Battle of &lt;Austerlitz&gt;" in send.await_args.args[1].text


async def test_handle_facts_maps_authorization_errors(monkeypatch, localization):
    send = AsyncMock()
    monkeypatch.setattr(handlers, "send_message_model", send)
    backend = SimpleNamespace(facts_page=AsyncMock(side_effect=PermissionError("no")))
    await handlers.handle_facts(
        SimpleNamespace(), backend, localization, "en", SimpleNamespace()
    )
    assert send.await_args.args[1].text == "Not shared with you."


async def test_knowledge_callback_navigates_pages_and_opens_facts(
    monkeypatch, localization
):
    send = AsyncMock()
    monkeypatch.setattr(handlers, "send_message_model", send)
    backend = SimpleNamespace(facts_page=AsyncMock(return_value=page(page=2)))
    callback = SimpleNamespace(
        data="klib:page:2", message=SimpleNamespace(), answer=AsyncMock()
    )
    await handlers.handle_knowledge_callback(
        callback, backend, localization, "en", SimpleNamespace()
    )
    assert backend.facts_page.await_args.kwargs["page"] == 2
    detail = {
        "title": "Fact",
        "description": "D",
        "links": [],
        "origin": "manual",
        "editable": True,
    }
    backend = SimpleNamespace(fact_detail=AsyncMock(return_value=detail))
    callback = SimpleNamespace(
        data=f"klib:fact:{FACT_ID}", message=SimpleNamespace(), answer=AsyncMock()
    )
    await handlers.handle_knowledge_callback(
        callback, backend, localization, "en", SimpleNamespace()
    )
    assert backend.fact_detail.await_args.args[1] == FACT_ID
    assert "Fact" in send.await_args.args[1].text


async def test_knowledge_callback_rejects_stale_or_unknown_references(localization):
    backend = SimpleNamespace(fact_detail=AsyncMock(side_effect=LookupError("gone")))
    callback = SimpleNamespace(
        data=f"klib:fact:{FACT_ID}", message=SimpleNamespace(), answer=AsyncMock()
    )
    await handlers.handle_knowledge_callback(
        callback, backend, localization, "en", SimpleNamespace()
    )
    callback.answer.assert_awaited_once_with("Not found.")
    malformed = SimpleNamespace(
        data="klib:fact:not-a-uuid", message=None, answer=AsyncMock()
    )
    await handlers.handle_knowledge_callback(
        malformed, SimpleNamespace(), localization, "en", SimpleNamespace()
    )
    malformed.answer.assert_awaited_once_with("Not found.")


def test_knowledge_router_does_not_register_slash_commands():
    from aiogram.filters import Command

    commands = [
        wrapped.callback
        for handler in handlers.router.message.handlers
        for wrapped in handler.filters
        if isinstance(wrapped.callback, Command)
    ]
    assert commands == []
    assert handlers.router.name.endswith("knowledge")


