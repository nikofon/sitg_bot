"""Telegram presentation for the trainee knowledge library (Agent B).

The router stays isolated from gateway wiring: Agent A registers a
``knowledge_backend`` workflow dependency implementing ``KnowledgeBackend``
and includes ``router`` in the root router. All authorization and data live in
the service layer; handlers only translate projections into localized
messages with authenticated inline navigation.
"""

from __future__ import annotations

from typing import Protocol
from uuid import UUID

from aiogram import Router
from aiogram.filters import Command
from aiogram.types import CallbackQuery, Message

from sitg_bot.bot.i18n import LocalizationService
from sitg_bot.bot.presenters.models import (
    InlineButtonModel,
    InlineKeyboardModel,
    MessageModel,
)
from sitg_bot.bot.presenters.render import send_message_model
from sitg_bot.domain.knowledge import FACT_PAGE_SIZE

router = Router(name=__name__)

FACTS_CALLBACK_PREFIX = "klib"
STATISTIC_LABELS = {
    "none": "knowledge.statistic.none",
    "pending": "knowledge.statistic.pending",
    "fresh": "knowledge.statistic.fresh",
    "stale": "knowledge.statistic.stale",
}


class KnowledgeBackend(Protocol):
    """Port Agent A implements against the typed gateway and services.

    Telegram navigates by bounded page number; the adapter resolves pages to
    the service's stable keyset cursors. The Mini App uses cursors directly.
    """

    async def facts_page(
        self,
        claim: object,
        *,
        owner_id: UUID | None = None,
        page: int = 1,
        search: str | None = None,
        tag: str | None = None,
        order: str = "title_asc",
        limit: int = FACT_PAGE_SIZE,
    ) -> dict: ...

    async def fact_detail(self, claim: object, fact_id: UUID) -> dict: ...

    async def shared_collections(self, claim: object) -> dict: ...

    async def dissection_requests(self, claim: object) -> dict: ...


def _statistic_label(status: object) -> str:
    return STATISTIC_LABELS.get(str(status), "knowledge.statistic.pending")


def facts_message(page: dict, localization: LocalizationService, locale: str) -> MessageModel:
    """List projection: bounded page, tag names and statistic state."""
    items = page.get("items", [])
    rows: list[tuple[InlineButtonModel, ...]] = []
    for item in items:
        title = str(item.get("title", ""))
        fact_id = str(item.get("fact_id", ""))
        rows.append(
            (
                InlineButtonModel(
                    text=title[:60],
                    callback_data=f"{FACTS_CALLBACK_PREFIX}:fact:{fact_id}",
                ),
            )
        )
    next_page = page.get("next_page")
    total = int(page.get("total", len(items)))
    navigation: tuple[InlineButtonModel, ...] = ()
    if next_page is not None:
        navigation = navigation + (
            InlineButtonModel(
                text=localization.text("knowledge.facts.more", locale, total=total),
                callback_data=f"{FACTS_CALLBACK_PREFIX}:page:{int(next_page)}",
            ),
        )
    current_page = int(page.get("page", 1))
    if current_page > 1:
        navigation = (
            InlineButtonModel(
                text=localization.text("knowledge.facts.previous", locale),
                callback_data=f"{FACTS_CALLBACK_PREFIX}:page:{current_page - 1}",
            ),
        ) + navigation
    if navigation:
        rows.append(navigation)
    lines = []
    if not items:
        lines.append(localization.text("knowledge.facts.empty", locale))
    for item in items:
        tags = ", ".join(item.get("tag_names", ()))
        label = localization.text(_statistic_label(item.get("statistic_status")), locale)
        entry = localization.text(
            "knowledge.facts.entry", locale, title=str(item.get("title", "")), statistics=label
        )
        if tags:
            entry += "\n" + localization.text("knowledge.facts.tags", locale, tags=tags)
        lines.append(entry)
    keyboard = InlineKeyboardModel(rows=tuple(rows)) if rows else None
    return MessageModel(
        localization.text("knowledge.facts.title", locale, total=total) + "\n\n"
        + "\n\n".join(lines),
        keyboard=keyboard,
    )


def fact_detail_message(
    detail: dict, localization: LocalizationService, locale: str
) -> MessageModel:
    """Detail projection; provenance and links render as plain escaped text."""
    title = str(detail.get("title", ""))
    description = str(detail.get("description", ""))
    links = detail.get("links", ())
    origin = str(detail.get("origin", "manual"))
    editable = bool(detail.get("editable", False))
    lines = [
        localization.text("knowledge.fact.title", locale, title=title),
        description,
    ]
    for link in links:
        lines.append(
            localization.text(
                "knowledge.fact.link",
                locale,
                title=str(link.get("title", "")),
                url=str(link.get("url", "")),
            )
        )
    lines.append(
        localization.text(
            "knowledge.fact.origin", locale, origin=localization.text(
                f"knowledge.origin.{origin}", locale
            )
        )
    )
    if not editable:
        lines.append(localization.text("knowledge.fact.read_only", locale))
    return MessageModel("\n".join(lines))


def shared_message(shared: dict, localization: LocalizationService, locale: str) -> MessageModel:
    grants = shared.get("grants", ())
    lines = [localization.text("knowledge.shared.title", locale)]
    if not grants:
        lines.append(localization.text("knowledge.shared.empty", locale))
    for grant in grants:
        lines.append(
            localization.text(
                "knowledge.shared.entry",
                locale,
                owner=str(grant.get("owner_name", grant.get("owner_id", ""))),
            )
        )
    return MessageModel("\n".join(lines))


def dissection_status_message(
    requests: dict, localization: LocalizationService, locale: str
) -> MessageModel:
    items = requests.get("items", ())
    lines = [localization.text("knowledge.dissection.title", locale)]
    if not items:
        lines.append(localization.text("knowledge.dissection.empty", locale))
    for item in items:
        status = str(item.get("status", "pending"))
        applied = len(item.get("applied_fact_ids", ()))
        lines.append(
            localization.text(
                "knowledge.dissection.entry",
                locale,
                packet=str(item.get("packet_name", item.get("packet_id", ""))),
                status=localization.text(f"knowledge.dissection.status.{status}", locale),
                applied=applied,
            )
        )
        if item.get("failed_candidate_ids"):
            lines.append(
                localization.text(
                    "knowledge.dissection.partial", locale
                )
            )
    return MessageModel("\n".join(lines))


def _error_message(
    error: Exception, localization: LocalizationService, locale: str
) -> MessageModel:
    if isinstance(error, PermissionError):
        return MessageModel(localization.text("knowledge.error.forbidden", locale))
    if isinstance(error, LookupError):
        return MessageModel(localization.text("knowledge.error.not_found", locale))
    return MessageModel(localization.text("knowledge.error.invalid", locale))


@router.message(Command("facts"))
async def handle_facts(
    message: Message,
    knowledge_backend: KnowledgeBackend,
    localization: LocalizationService,
    locale: str,
    telegram_update_claim: object,
) -> None:
    try:
        page = await knowledge_backend.facts_page(telegram_update_claim)
    except (PermissionError, LookupError, ValueError) as error:
        await send_message_model(message, _error_message(error, localization, locale))
        return
    await send_message_model(message, facts_message(page, localization, locale))


async def _answer_callback(
    callback: CallbackQuery,
    model: MessageModel,
) -> None:
    if callback.message is not None:
        await send_message_model(callback.message, model)
    await callback.answer()


@router.callback_query(lambda query: (query.data or "").startswith(f"{FACTS_CALLBACK_PREFIX}:"))
async def handle_knowledge_callback(
    callback: CallbackQuery,
    knowledge_backend: KnowledgeBackend,
    localization: LocalizationService,
    locale: str,
    telegram_update_claim: object,
) -> None:
    # Service-level checks authorize every read; stale references only fail.
    data = callback.data or ""
    try:
        if data.startswith(f"{FACTS_CALLBACK_PREFIX}:page:"):
            page_number = int(data.removeprefix(f"{FACTS_CALLBACK_PREFIX}:page:"))
            if page_number < 1:
                raise ValueError("Invalid facts page")
            page = await knowledge_backend.facts_page(
                telegram_update_claim, page=page_number
            )
            model = facts_message(page, localization, locale)
        elif data.startswith(f"{FACTS_CALLBACK_PREFIX}:fact:"):
            fact_id = UUID(data.removeprefix(f"{FACTS_CALLBACK_PREFIX}:fact:"))
            detail = await knowledge_backend.fact_detail(
                telegram_update_claim, fact_id
            )
            model = fact_detail_message(detail, localization, locale)
        else:
            model = _error_message(ValueError("Unknown action"), localization, locale)
    except (PermissionError, LookupError, ValueError):
        await callback.answer(localization.text("knowledge.error.not_found", locale))
        return
    await _answer_callback(callback, model)

